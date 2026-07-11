**New primitives introduced:**
- `scout/live/live_evaluator.py` — `evaluate_open_live_trades()` + `live_evaluator_loop()` (live-mode symmetric twin of `shadow_evaluator`)
- `reconcile_open_live_trades()` in `scout/live/reconciliation.py` — boot/periodic live-orphan reconciler
- `ExchangeAdapter.place_exit_order()` + `ExchangeAdapter.fetch_order_by_client_id()` (adapter surface; Binance impl, others raise `NotImplementedError`)
- `make_exit_client_order_id()` in `scout/live/idempotency.py` — deterministic exit-order idempotency key
- `LIVE_CLOSER_ENABLED` Settings flag (default True) — fail-closed interim guard lever
- new structlog events (`live_trade_closed`, `live_exit_needs_review`, `live_boot_live_reconciliation_done`, `live_orphan_*`, `live_stuck_open`) + `live_metrics_daily` counters (`live_closes_*`, `live_exit_review`, `live_orphan_*`)

# Design — Live Exit / Close / Reconcile (LIVE-02) + Daily-Loss Live Union (LIVE-04)

**Status:** design-first (per PR #400 disposition ruling — S1-2 is "the big one, DESIGN-FIRST
before code"). Implemented in the same branch behind the existing default-OFF live flags. **Zero
runtime behavior change** until `LIVE_MODE=live` + `LIVE_TRADING_ENABLED=True` +
`LIVE_USE_ROUTING_LAYER=True` + `LIVE_USE_REAL_SIGNED_REQUESTS=True` are all set — which the
**standing live-enable gate** (`tasks/findings_live_trading_m1_audit_2026_07_06.md` header) forbids
until the four S1 fix-PRs are cited. This work builds S1-2 (=LIVE-02) and S1-3 (=LIVE-04); it does
not flip any flag.

**Closes:** LIVE-02 (live close-path + boot reconciler + orphan detection + interim fail-closed
guard), LIVE-04 (daily-loss cap unions live PnL), LIVE-08-lite (non-`filled` terminal statuses at
dispatch + a §12a stuck-open watchdog). Backlog: `tasks/backlog_fable_analysis_2026_07_10.md` (LIVE
lane). Audit source: `tasks/findings_live_trading_m1_audit_2026_07_06.md` S1-2 / S1-3 / S2-3.

**Governing precedent, read first:** the standing **LIVE-ENABLE GATE** sits on top of everything
here; this design adds the *closer/reconciler* the gate's Criterion 6 (S1-2) requires, and the
pre-registered evidence gate in `tasks/plan_pre_live_evidence_gate.md` (C1–C6) still governs any
flip. Exit policy here is **flat TP/SL/duration, identical to shadow** — the deliberate choice from
that doc §3.2 (comparability by construction; the shadow ledger IS the live policy).

---

## Hermes-first analysis

Per `docs/gecko-alpha-alignment.md` §"Hermes-first analysis convention", checked against the Hermes
skill hub (`hermes-agent.nousresearch.com/docs/skills`) + awesome-hermes-agent ecosystem. This is
the **same live-trading domain** already analyzed in `tasks/plan_pre_live_evidence_gate.md`
(Hermes-first §) and `tasks/plan_live_trading_milestone_1.md`; the conclusion is unchanged and is
restated here rather than re-derived.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Exchange order lifecycle (place/poll/reconcile spot orders) | none found — Hermes skills are agent-capability primitives (tools/actions), not a venue-execution SDK; the project already owns a Binance REST adapter (`binance_adapter.py`) with signing, retry taxonomy, idempotency | Extend the in-tree adapter (`place_exit_order`, `fetch_order_by_client_id`). No external primitive. |
| Position reconciliation / orphan recovery after crash | none found — no Hermes skill governs "match local ledger rows to a venue by client_order_id and classify orphans" | Build from scratch against this project's `live_trades` schema + the existing shadow boot-reconciler pattern (`reconcile_open_shadow_trades`). Reuse that structure, do not re-import. |
| Kill-switch / daily-loss accounting | already in-tree (`kill_switch.py`, `maybe_trigger_from_daily_loss`) | Extend the existing query (union `live_trades`). No new primitive. |

**awesome-hermes-agent ecosystem check:** scanned for exchange-execution, order-reconciliation, and
trading-risk-brake skills — none apply (all project-internal money-path logic over a local SQLite
ledger + a private venue adapter). **Verdict:** no Hermes primitive fits; extend in-tree code.

---

## 1. Problem (from the M1 audit)

- **S1-2 / LIVE-02.** `_dispatch_live` (`engine.py`) is **buy-only**: it routes, places a `side="buy"`
  order, awaits fill, and on `filled` only `increment_consecutive`. No live row ever leaves
  `status='open'`; no sell/TP/SL/duration exit exists; the boot reconciler
  (`reconcile_open_shadow_trades`) touches `shadow_trades` only. **A crash between the Binance POST
  and the local update orphans a filled position on a real venue with real money.** This is GA-01's
  "born unexitable" S1, now on the live path.
- **S1-3 / LIVE-04.** `maybe_trigger_from_daily_loss` sums `shadow_trades` only. In live mode the
  engine skips the shadow write; live fills land in `live_trades`, which never accrues realized PnL
  (S1-2). `LIVE_DAILY_LOSS_CAP_USD` — the headline automated brake — **can never fire on a real
  loss.**
- **S2-3 / LIVE-08.** `_dispatch_live` handles only `status=="filled"`; `timeout`/`partial`/`rejected`
  buys fall through, leaving permanent `'open'` rows that (post-S1-1) monotonically inflate Gate 7's
  exposure sum until every signal rejects `exposure_cap`.

## 2. Decisions

### 2.1 Exit policy = flat TP / SL / max-duration, identical to shadow (comparability by construction)

The live evaluator resolves thresholds through the **same** `LiveConfig.resolve_tp_pct() /
resolve_sl_pct() / resolve_max_duration_hours()` the shadow evaluator uses (`LIVE_* → PAPER_* →
default`). The close decision is the same ordered first-cross check as
`shadow_evaluator.evaluate_open_shadow_trades` and `reconciliation._close_crossed_row`:

```
pnl_pct = (fresh_price / entry_fill_price - 1) * 100
closed_tp        if tp  is not None and pnl_pct >=  tp
closed_sl        if sl  is not None and pnl_pct <= -sl
closed_duration  if max_dur is not None and (now - created_at) >= max_dur
else             hold
```

Rationale: `tasks/plan_pre_live_evidence_gate.md` §3.2 chose to **document** the paper-cascade
non-comparability rather than share the cascade helper onto the money path. The shadow ledger is the
promotion-evidence base and it already IS the flat live policy, so the live evaluator must reproduce
exactly that policy — not the paper trailing/ladder cascade. The eventual shared `decide_exit`
helper (that doc's Appendix B) is explicitly gated behind *this* live evaluator existing; out of
scope here.

### 2.2 Close mechanics = one real venue sell, keyed by a deterministic exit `client_order_id`

- Base quantity to sell = `entry_fill_qty` (the actual filled buy quantity), NOT `size_usd` (which is
  a quote notional). This requires persisting the real entry fill — see §2.3.
- Exit order id = `make_exit_client_order_id(live_trade_id)` → `gecko-x-{live_trade_id}` (≤28 chars,
  Binance limit). **Deterministic per row** → a crash-retry of the exit re-submits the *same* cid;
  Binance `-2010` (duplicate) dedups it and the adapter recovers the existing order via
  `origClientOrderId` GET. This is the exit-side mirror of the entry idempotency already in
  `place_order_request`. It is distinct from the entry cid (`gecko-{id}-{uuid8}`) so entry and exit
  never collide.
- New adapter method `place_exit_order(*, pair, base_qty, client_order_id, timeout_sec) ->
  OrderConfirmation`: signed `POST /api/v3/order` `side=SELL type=MARKET quantity=<base_qty>`
  (`-2010` recovery), then poll `GET /api/v3/order?origClientOrderId` to a terminal state. It does
  **not** write a `live_trades` row (unlike the buy path's `record_pending_order`) — the evaluator
  owns the ledger UPDATE. Non-Binance adapters raise `NotImplementedError` (M1 is Binance-only;
  LIVE-11 parks the others).

### 2.3 Persist the real entry fill (prerequisite for a correct close)

Today `_dispatch_live` on `filled` writes nothing about the fill to `live_trades` (only
`fill_slippage_bps` is written, by the adapter). The close path needs `entry_fill_price` +
`entry_fill_qty`. Fix: on `confirmation.status == "filled"`, `_dispatch_live` writes
`entry_fill_price` + `entry_fill_qty` (from the confirmation) to the row keyed by cid, *then*
`increment_consecutive`. `entry_fill_price`/`entry_fill_qty` are existing NULLable columns in the
base `live_trades` DDL — no migration.

### 2.4 Realized PnL

```
realized_pnl_usd = exit_fill_price * exit_filled_qty - entry_fill_price * entry_fill_qty
realized_pnl_pct = (exit_fill_price / entry_fill_price - 1) * 100
```

Sign-correct: a sell above entry books positive. Written with the terminal `status`
(`closed_tp`/`closed_sl`/`closed_duration`), `exit_order_id`, `exit_fill_price`, `closed_at` under
`db._txn_lock`. All are existing NULLable `live_trades` columns — no migration.

### 2.5 Daily-loss cap unions live PnL (LIVE-04)

`maybe_trigger_from_daily_loss` keeps its shadow sum and, **when `LIVE_TRADING_ENABLED=True`**, adds
`SUM(realized_pnl_usd)` over `live_trades WHERE status LIKE 'closed_%' AND date(closed_at) =
date('now')`. The combined today-UTC sum is compared to `-LIVE_DAILY_LOSS_CAP_USD`. Gating on the
flag matches the audit and avoids touching the shadow-soak path when live is off (live_trades is
empty in shadow/paper mode anyway; the flag makes the intent explicit and the negative test precise).
The kill-switch `date(closed_at)=date('now')` predicate mirrors the existing shadow query verbatim
(SQLite accepts the `T`-separated ISO8601 timestamps the ledger stores; INF-04 datetime-normalization
does not regress — same-form both sides).

### 2.6 Interim fail-closed guard (LIVE-02 "no closer ⇒ crash")

`LiveEngine.__init__` already CRASHES on `LIVE_USE_ROUTING_LAYER=True` without signed / without a
routing layer. Extend it: **crash if `mode=='live'` AND `LIVE_USE_ROUTING_LAYER=True` AND
`LIVE_CLOSER_ENABLED=False`.** A live routing engine that can buy but whose close loop is disabled is
exactly the buy-only orphan-money state this whole design removes; refuse to boot into it.
`LIVE_CLOSER_ENABLED` defaults True. `scout/main.py` spawns `live_evaluator_loop` only when the flag
is True — so disabling the closer for maintenance while leaving routing on = boot crash (fail-closed),
not silent buy-only drift. (The residual "flag True but loop never spawned" case is covered at
runtime by the §2.8 stuck-open watchdog; the flag guard is the cheap structural half.)

### 2.7 Boot + periodic reconciler (`reconcile_open_live_trades`)

Symmetric to `reconcile_open_shadow_trades` (always emits a terminal
`live_boot_live_reconciliation_done` log; never throws). For each `live_trades WHERE status='open'`,
query the venue by the **entry** `client_order_id` via `adapter.fetch_order_by_client_id(pair, cid)`
and classify:

| Venue state | Local state | Class | Action | §12b alert |
|---|---|---|---|---|
| FILLED | `entry_fill_price` NULL | **filled-venue / open-local** (crash after POST, before persist) | persist entry fill from venue order; leave `open` (evaluator manages exit) | `live_orphan_recovered_fill` |
| FILLED | `entry_fill_price` set | healthy open awaiting exit | leave `open`; count resumed | — |
| PARTIALLY_FILLED | any | **partial** | `status='needs_manual_review'` | `live_orphan_partial` |
| CANCELED/EXPIRED/REJECTED or order-not-found | any | **missing** (buy never produced a position) | `status='needs_manual_review'` | `live_orphan_no_fill` |
| cid NULL | any | malformed | `status='needs_manual_review'` | `live_orphan_no_cid` |
| adapter error / unknown | any | transient | leave `open`, log `live_boot_live_reconciliation_row_err`, continue | — |

Boot invocation runs alongside `reconcile_open_shadow_trades` in `main.py` (live/shadow modes). The
same function is called at the head of every `live_evaluator_loop` tick's evaluator pass (periodic
reconcile) so a mid-run crash is recovered within one interval, not only at restart.

### 2.8 Non-`filled` terminal statuses + stuck-open watchdog (LIVE-08-lite)

- **At dispatch (buy).** `_dispatch_live` handles every terminal `OrderConfirmation.status`:
  `filled` → persist entry fill + `increment_consecutive` (§2.3); `partial`/`rejected`/`timeout` →
  `status='needs_manual_review'` + `live_dispatch_terminal_needs_review` WARN + metric. No live row is
  left dangling `open` from a non-clean buy → Gate 7 exposure cannot silently inflate (the S2-3 lockout
  is closed).
- **At exit (sell).** Only a clean `filled` sell terminalizes the row to `closed_*`. A
  `partial`/`rejected`/`timeout` sell → `status='needs_manual_review'` + `live_exit_needs_review` WARN
  + §12b alert; the row stops auto-cycling (fail-closed to the operator). This deliberately avoids an
  automatic exit-retry loop: with a deterministic exit cid, a truly-rejected sell would recover the
  same rejected order forever, and a partial would risk re-selling more than is held. Auto-retry with a
  fresh cid + a balance query is a **follow-up** (needs a `remaining_qty` column — >60 LOC, deferred;
  see §4). For a first live venue at $50 sizes with rare fires, fail-closed-to-human on any non-clean
  fill is the correct M1 posture.
- **Stuck-open watchdog (§12a).** `evaluate_open_live_trades` warns once/day (`live_stuck_open`) when
  an `open` live row is older than `max_duration + grace` — the live twin of `shadow_soak_frozen`.
  This surfaces a closer that is enabled-but-not-running (the residual case §2.6's flag guard cannot
  see) and any exit that keeps failing.

### 2.9 §12b alert plumbing

The reconciler + evaluator take an optional `alert_hook: Callable[[str], Awaitable[None]] | None`.
`main.py` wires the same plain-text (`parse_mode=None`) sender used for the kill-switch hook — these
are automated state changes to money-bearing rows (auto-terminalize / orphan-recover), squarely §12b.
Every fire is wrapped in the `*_alert_dispatched` / `*_alert_delivered` / `*_alert_failed` log triplet
(mirror `KillSwitch._emit_alert`) and NEVER raises (the DB state change has already committed). Hookless
construction (tests, paper mode) = log-only, no send.

## 3. Testing (TDD, red-first)

- `test_live_evaluator.py`: TP/SL/duration close writes correct terminal status + sign-correct
  realized PnL via a mock `place_exit_order`; not-crossed row stays open; sell `partial`/`rejected`
  → `needs_manual_review` + no `closed_*`; daily-loss re-check invoked after close; stuck-open warns.
- `test_live_reconciliation.py`: zero rows still logs `_done`; filled-venue/open-local recovers +
  persists fill + alerts; partial/missing → `needs_manual_review` + alerts; healthy open resumed;
  adapter error leaves open + logs row_err + still `_done`.
- `test_daily_cap.py` (extend): seeded **live** loss > cap trips the kill (LIVE-04 negative test);
  live loss ignored when `LIVE_TRADING_ENABLED=False`; combined shadow+live sum.
- `test_live_engine_dispatch.py` (extend): `__init__` crashes on routing+`LIVE_CLOSER_ENABLED=False`;
  `filled` persists `entry_fill_price`/`entry_fill_qty`; `rejected`/`partial`/`timeout` buy →
  `needs_manual_review`.
- `test_binance_adapter.py` / signed (extend, aioresponses, CI-and-local): `place_exit_order` submits a
  SELL + polls to filled; `-2010` recovery; `fetch_order_by_client_id` maps status + order-not-found.
- Mocks: money-path doubles use `spec=`/typed confirmations per the safety-critical-mocks lesson
  (`feedback_spec_mocks_for_safety_critical`).

**Windows note:** contrary to the historical `reference_windows_openssl_workaround` memory, this
worktree runs the full `tests/live/` suite locally (aiohttp 3.13.3 imports, aioresponses works) —
red/green was verified locally here, not only in CI. Command: `python -m pytest tests/live/
tests/test_live_engine_dispatch.py -q` (venv python; run from repo root so `scout` is importable).

## 4. Explicitly out of scope (follow-ups, noted not built)

- **Automatic exit-retry with a fresh cid + balance reconcile** after a failed/partial sell — needs a
  `remaining_qty` (or partial-fill ledger) column on `live_trades` (registry-style migration) and a
  balance query loop; >60 LOC, deferred. M1 posture is fail-closed-to-operator (§2.8).
- **Shared `decide_exit(policy=flat|cascade)` helper** unifying paper + shadow + live exits
  (`plan_pre_live_evidence_gate.md` Appendix B) — gated behind this live evaluator existing; the flat
  policy is duplicated (not shared) here on purpose to keep the money path simple.
- **Multi-venue exits** — `place_exit_order` is Binance-only; ccxt/others raise (LIVE-11 park).

## Approvals log

| Action | Class | Approval record | Status |
|---|---|---|---|
| Author this design + implement LIVE-02/04/08-lite behind default-OFF flags | implementation (money-path, flags OFF) | task dispatch (orchestrator) | executed |
| Any live flag flip | flag/prod-state | — | **NOT approved; gated on live-enable gate + `plan_pre_live_evidence_gate` C1–C6 + recorded operator approval** |

No flag flipped. No prod state touched. Behavior is inert until all four live flags are set, which the
standing gate forbids absent the four cited S1 fix-PR numbers.
