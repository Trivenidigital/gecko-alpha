# Live-Trading Path Gaps — Phase C Review (2026-06-01)

**Status:** FINDINGS-ONLY. These are gaps in the **gated, incomplete** live-execution
path (M1.5b / BL-055), surfaced by the 2026-06-01 autonomous Phase C money-path review.
Live trading is OFF by default (`LIVE_TRADING_ENABLED=False`, `LIVE_USE_ROUTING_LAYER=False`);
every item below is **latent — it bites the moment those flags flip to live.** They were
NOT auto-fixed: building the missing approval/exit/PnL machinery is the BL-055 live-trading
effort, which is deliberately operator-gated (memory: "do NOT flip LIVE_MODE=live until 7d
clean + balance_gate + policy review"). This doc is the close-before-go-live checklist.

The three CRITICALs share one shape: `LiveEngine._dispatch_live` (`scout/live/engine.py:295-482`)
is an honest **buy-only M1.5b skeleton** (its own docstrings say so), and nothing in-tree
*prevents* `LIVE_USE_ROUTING_LAYER=True` from arming it with these gaps.

## CRITICAL-1 — Operator-approval gate is never called on the live order path
`scout/live/approval_thresholds.py::should_require_approval` and
`telegram_approval.py::request_operator_approval` have **zero callers** in dispatch.
`_dispatch_live` goes straight `get_candidates → place_order_request → await_fill`. The
design's four approval gates (esp. the **new-venue** gate: `<30 consecutive_no_correction
fills → require approval`) — the primary protection for the first-ever live trades — are
bypassed. **Fix (BL-055):** in `_dispatch_live`, after `top = candidates[0]` and before
`place_order_request`, call `should_require_approval(...)`; if True, `request_operator_approval`
and abort (write a `live_trades` rejected row, `reason='needs_approval'`) when not granted.

## CRITICAL-2 — Daily-loss-cap kill switch is blind to live P&L
`maybe_trigger_from_daily_loss` (`kill_switch.py:347`) sums realized PnL from
`shadow_trades` ONLY. In live mode the engine writes `live_trades` and skips the
`shadow_trades` happy-path write — so the headline daily-loss circuit breaker
(`LIVE_DAILY_LOSS_CAP_USD`) never sees real losses. `cross_venue_pnl` view is a hardcoded
`0.0` placeholder (`db.py`). **Fix (BL-055):** aggregate realized PnL across `shadow_trades`
+ closed `live_trades` (or implement the real `cross_venue_pnl` view), gated behind
`LIVE_TRADING_ENABLED` so shadow-soak semantics are unchanged. Co-dependent with CRITICAL-3.

## CRITICAL-3 — Live positions are opened but never closed
`_dispatch_live` places a BUY and inserts a `live_trades` row at `status='open'`. No code
ever sets `live_trades.status` to closed or computes its `realized_pnl_usd` — `shadow_evaluator`
(TP/SL/duration closer) and `reconciliation.py` operate on `shadow_trades` only. Once live,
every filled position stays open forever: no take-profit, no stop-loss, no trailing exit.
**Fix (BL-055):** a live evaluator+reconciler symmetric to the shadow ones, scanning open
`live_trades`, placing sell orders, writing `closed_*` + `realized_pnl_usd`. This is the
largest gap.

## HIGH — Orphaned `open` live_trades rows on non-filled terminal status (§12a)
On `await_fill_confirmation` returning `timeout`/`partial`/`rejected`, `_dispatch_live`
(`engine.py:467-481`) only logs + increments the correction counter on `filled`. The `open`
row is never reconciled → inflates the Gate-7 exposure sum (false `exposure_cap` trips) and
diverges DB from venue. No `live_trades` freshness watchdog exists. **Fix:** handle every
terminal status (→ `rejected` / `needs_manual_review`) + add a `live_trades` stuck-open
watchdog (§12a).

## MEDIUM — `live_trades.signal_type` permanently empty
`record_pending_order` is called with `signal_type=""` ("filled by engine layer", but the
engine never does). Breaks approval Gate 1/2 (keyed on signal_type) + per-signal live
analytics. **Fix:** thread the real `signal_type` through `OrderRequest`, or UPDATE
`live_trades.signal_type` right after `place_order_request`.

## LOW — `_dispatch_live` chain_hint always None
`chain_hint = getattr(paper_trade, "chain", None)` but `_PaperTradeHandoff` has no `chain`
field → always None → routing loses the chain disambiguator (latent mis-route once >1 venue).
**Fix:** add `chain` to `_PaperTradeHandoff` (`paper.py:39-45`) + populate at construction.

## Recommended near-term safety net (optional, low-risk)
Extend the existing `engine.py` `__init__` misconfig CRASH (which already refuses
`LIVE_USE_ROUTING_LAYER=True` without `LIVE_USE_REAL_SIGNED_REQUESTS=True`) to **also** refuse
arming until CRITICAL-1..3 are closed — so the flag flip cannot silently activate a buy-only,
uncapped, unapproved live trader. NOT done autonomously here: it changes the operator's ability
to arm a feature they built; the operator should decide whether to add the hard guard or rely
on this checklist + the BL-055 gating discipline.

## Clean (verified, no action)
Secret handling (keys only into HMAC signing / header, never logged), `*.session` handling,
SQL parameterization (no injection in `scout/live/*`), GoPlus fail-open correctly scoped to
the paper-alert gate (money path uses `is_safe_strict` fail-closed), kill-switch concurrency +
§12b alerts (PR #348), idempotent client order IDs + duplicate-order recovery, Binance
retry/auth taxonomy. Async/concurrency hygiene (separate Phase C vector) also came back clean.
