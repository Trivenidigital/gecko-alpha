**New primitives introduced:** Design-doc companion to `tasks/plan_live_trading_milestone_1_5b.md` for BL-NEW-LIVE-HYBRID M1.5b. No new primitives beyond those already declared in the plan. Documents architectural rationale, counter semantics, venue_health gap, operator activation experience, and reversibility model. Read alongside the plan, not in place of it.

# Live Trading Milestone 1.5b — Design Document

## 1. Goals

Close V1 review's CRITICAL findings on routing-layer disconnection (C1) and zero-writer correction counter (C2). Both close with the smallest viable surface area; full V1-C1 closure (Telegram approval gateway runtime hook) defers to M1.5c.

**Primary outcomes:**
1. `LiveEngine` calls `RoutingLayer.get_candidates` under `LIVE_MODE='live'` AND `LIVE_USE_ROUTING_LAYER=True`
2. `signal_venue_correction_count.consecutive_no_correction` has writers on successful fills
3. M1.5a behavior is preserved verbatim under default flag posture (`LIVE_USE_ROUTING_LAYER=False`)

**Non-goals (M1.5c):**
- Telegram approval gateway runtime call (`should_require_approval` invocation from engine)
- Recurring health probe (boot-time smoke is point-in-time)
- Reconciliation worker for orphaned `live_trades` rows
- Auto-trigger of `reset_on_correction` from any path

## 2. Architectural choices

### 2.1 Parallel `_dispatch_live()` vs unifying live + shadow flows

**Chosen:** Parallel. `LiveEngine.on_paper_trade_opened` adds a separate `_dispatch_live()` private method gated behind `mode == "live" AND LIVE_USE_ROUTING_LAYER AND routing is not None`. Existing shadow-mode flow (write `shadow_trades` row) is untouched.

**Why not unify:**
- M1.5a's regression surface is small only because shadow-mode flow is unchanged. Unifying live + shadow into a single dispatch function risks subtle behavior drift in BL-055 soak telemetry that has run for 2+ weeks.
- Reviewer-folding: R1 explicitly cited "engine refactor preserving shadow-mode contract" as the highest-risk axis. Parallel methods isolate the new path.
- Reversibility: a single flag flip (`LIVE_USE_ROUTING_LAYER=False`) returns to M1.5a behavior verbatim because the new path is bypassed at the branch, not unwound after dispatch.

**Tradeoff:** code duplication for shared concerns (logging context, exception classification). Acceptable because the duplication is a few lines and surfaces the live-vs-shadow divergence rather than hiding it.

### 2.2 Removing M1.5a's `assert mode != "live"` guard + adding fail-loud misconfig CRASH

**Chosen:** Remove the assert. Add a NEW boot-time CRASH in `LiveEngine.__init__` for misconfig.

**Why remove:** R1+R2 plan-stage CRITICAL finding C1 — the assert at `engine.py:88-91` blocks ALL paths (including the new `_dispatch_live` branch) under `mode='live'`. Without removal, M1.5b is structurally unreachable.

**Why this is safe:** `main.py` boot guard at line 1062 already enforces `LIVE_TRADING_ENABLED=True` for `mode='live'`. Line 1079 enforces `LIVE_USE_REAL_SIGNED_REQUESTS=True`. Lines 1108-1144 require a successful smoke-check against Binance with SPOT permission. The assert was M1's belt-and-braces guard for the period before any live wiring existed; M1.5b's actual contract is the boot guard.

**Why ADD the misconfig CRASH:** Design-stage R2-C1 + R1-M2 + R2-I3 — three failure modes silently no-op every signal in M1.5b's parallel design:
1. `LIVE_USE_ROUTING_LAYER=True` AND `LIVE_USE_REAL_SIGNED_REQUESTS=False` (operator forgot signed flag) → `place_order_request` raises `NotImplementedError` → engine catches + returns silently → ZERO live trades, ZERO alerts. At ~1.8 signals/hr observed prod rate (43 trades / 24h), an operator who walks away for 8 hours returns to ~14 silently-skipped signals.
2. `LIVE_USE_ROUTING_LAYER=True` AND `routing is None` (main.py wiring forgot kwarg) → `_dispatch_live` branch evaluates False on `routing is not None` → silently no-ops every signal.
3. `LIVE_USE_ROUTING_LAYER=True` AND `LIVE_TRADING_ENABLED=False` — already covered by main.py boot guard, included for defense-in-depth.

The design-stage reviewers correctly identified these as a re-occurrence of the BL-064/BL-065 silent-failure-on-misconfig class of bug. The fix is a single CRASH in engine `__init__`:

```python
def __init__(self, *, config, ..., routing=None) -> None:
    # ... existing assignment ...
    if config.mode == "live":
        flag_routing = getattr(config._s, "LIVE_USE_ROUTING_LAYER", False)
        flag_signed = getattr(config._s, "LIVE_USE_REAL_SIGNED_REQUESTS", False)
        if flag_routing and not flag_signed:
            raise RuntimeError(
                "Misconfig: LIVE_USE_ROUTING_LAYER=True but "
                "LIVE_USE_REAL_SIGNED_REQUESTS=False. Engine would "
                "silently no-op every signal. Set LIVE_USE_REAL_SIGNED_REQUESTS=True "
                "or LIVE_USE_ROUTING_LAYER=False before boot."
            )
        if flag_routing and routing is None:
            raise RuntimeError(
                "Misconfig: LIVE_USE_ROUTING_LAYER=True but routing=None. "
                "Check scout/main.py construction passes routing=live_routing "
                "kwarg to LiveEngine."
            )
```

**Why CRASH not WARN-and-skip:** Crash is bounded by `RestartSec=30s + StartLimitBurst=3` (M1.5a runbook §1), so misconfig produces a tight feedback loop with operator-visible systemd failure + OnFailure Telegram alert (M1.5a runbook §2). WARN-and-skip is unbounded — operator walkaway = arbitrary missed signals. The cost of CRASH is bounded; the cost of WARN-and-skip is unbounded.

**Tradeoff:** the assert was self-documenting at the runtime call site. The replacement runtime comment + the new __init__ misconfig CRASH preserve documentation AND tighten enforcement.

### 2.3 cid derivation in `_dispatch_live`

**Chosen:** Compute `cid = make_client_order_id(paper_trade.id, intent_uuid)` in `_dispatch_live` and pass that as `client_order_id` to `await_fill_confirmation`.

**Why:** R1 plan-stage CRITICAL finding C2 — `binance_adapter.place_order_request:478` writes `cid = "gecko-{paper_trade_id}-{uuid8}"` to `live_trades.client_order_id`. `await_fill_confirmation:628` does a SELECT lookup keyed on `client_order_id`. Passing the raw `intent_uuid` would return zero rows → `RuntimeError` at line 634 ("await_fill_confirmation: no live_trades row for cid=..."). Every M1.5b live dispatch would explode.

**Why import in the method body:** `_dispatch_live` does the import locally to avoid a top-level circular dependency (engine.py is part of the live-module surface). Imports in the method also document the M1.5a primitives being composed.

### 2.4 Counter increment-on-fill ONLY (not partial)

**Chosen:** Increment only on `confirmation.status == "filled"`. Partial fills are NOT counted.

**Why:** Plan-stage R1+R2 finding C3 — Binance's PARTIALLY_FILLED status is per-spec terminal in M1.5a's adapter abstraction (`OrderConfirmation.status='partial'`), but in real Binance behavior, PARTIALLY_FILLED can transition to CANCELED for IOC orders or operator-cancel-remaining flows. M1.5b's `await_fill_confirmation` returns on first terminal observed. If the same order subsequently flips to FILLED or CANCELED from PARTIALLY_FILLED, the counter would either double-count (engine reissues) or be incorrectly counted toward auto-clear (operator subsequently unwinds the partial).

**Verified terminal-status path (R1-M3 fold):** `binance_adapter.py:644-704` polls Binance order status; lines 669-679 map PARTIALLY_FILLED → status='partial' and RETURN immediately (does NOT keep polling for the final state). M1.5b therefore NEVER observes a partial → CANCELED transition — it returns on first PARTIALLY_FILLED. The eventual CANCELED is M1.5c reconciler's job. The terminal status set returned by `await_fill_confirmation` is `{'filled', 'partial', 'rejected', 'timeout'}`. Counter increments only on `'filled'`.

**Tradeoff:** legitimate partial fills (the order really did execute partially and stay there) are not counted toward the operator's auto-clear streak. This understates the streak in the V1 gate; acceptable because the V1 gate threshold is 30 fills and partials are a minority of expected dispatches. Reconciler-domain (M1.5c) can refine.

### 2.5 Counter-reset semantic: zeroes the entire field

**Chosen:** `reset_on_correction` zeros `consecutive_no_correction` to 0 for the entire `(signal_type, venue)` pair on a single correction.

**Why:** matches the field name (`consecutive_no_correction` = "consecutive trades without correction"). Matches V1's gate intent ("trust requires UNBROKEN streak"). Statistically rigorous: a correction means the operator does NOT trust the (signal, venue) pair fully; resetting the streak signals "begin re-earning trust from zero."

**Acknowledged UX cost (R2-C2 fold):** worked example: 30 successful fills → counter=30 → operator unwinds trade #31 → counter=0 → all 30 prior good fills lose auto-clear-approval progress. This matches the strict semantic but feels punitive operationally.

**Mitigation:** docstring acknowledgment + runbook entry. M1.5c may add a separate `total_fills_lifetime` column for dashboard telemetry that survives resets — but this is OUT OF SCOPE for M1.5b.

**Why not soften (e.g., 50% reset, or "reset only the most recent correction window"):** softer semantics would obscure the V1 gate's behavior. Operator should be able to reason about "am I past the 30-fill threshold" by reading a single integer — not by computing windowed deltas.

### 2.6 None/empty `signal_type` coercion to "unknown"

**Chosen:** `signal_type = signal_type or "unknown"` at the head of both `increment_consecutive` and `reset_on_correction`.

**Why:** R1-I7 fold — `paper_trade.signal_type` can theoretically be empty string or None. Cashtag-dispatch path under BL-065 has historically produced empty values when symbol resolution failed. A crash here would block dispatch silently (the engine swallows exceptions). A silent skip would lose the counter increment. Coercing to "unknown" preserves the count under a reserved bucket.

**Tradeoff:** "unknown" entries pollute dashboards. Acceptable because dashboards already filter by signal_type allowlist; "unknown" rows surface as anomalies for operator investigation rather than silent data loss.

### 2.7a BL-055 shadow soak orthogonality under live mode (R1-I1 fold)

**Chosen:** BL-055 shadow-mode soak ENDS when operator flips `LIVE_MODE='live'`. Live mode does NOT also write `shadow_trades` rows.

**Why:** R1-I1 surfaced the design ambiguity. The two ledgers serve different purposes:
- `shadow_trades` (BL-055): "what would have been the live PnL if we had executed?" — paper-money simulation against real Binance prices/depth
- `live_trades` (M1.5b): real money execution outcomes

When operator flips to LIVE_MODE='live', the operator's intent is "execute for real, not simulate." The shadow simulation is no longer load-bearing — actual fill data is the better signal. Continuing shadow_trades writes under live mode would (a) duplicate the engine path complexity, (b) require maintaining the shadow-vs-live PnL diff query as a permanent operator interface (not in scope for M1.5b), (c) blur the operator's mental model of what "live mode" means.

**Tradeoff:** operator loses the ability to compare "shadow's prediction vs live's actual outcome on the same signal." Acceptable because the comparison was BL-055's PRE-go-live discipline; once live, the live data is authoritative. M1.5c reconciler may build a slippage-comparison view from `live_trades.fill_slippage_bps` against historical shadow telemetry if the operator finds that valuable.

**Documented in:** plan §"What this milestone does NOT do" — explicit "BL-055 shadow soak ends at first live signal" note.

### 2.7 Per-venue counter intent

**Chosen:** PK is `(signal_type, venue)` — each venue's counter tracks independently.

**Why:** when M1.5c adds Kraken/Coinbase, each venue has different latency, slippage, and orderbook depth characteristics. Operator's confidence in `(first_signal × binance)` does not transfer to `(first_signal × kraken)`. The first kraken trade requires re-earning operator approval from zero. This is V1's design intent (per `approval_thresholds.py:51-66` — Gate 1 keys on `(signal_type, venue)`).

**Tradeoff:** if a new venue turns out to behave identically to binance, the operator must wait for 30 fills on the new venue before auto-clear. Acceptable because the cost of premature auto-clear (live-money execution on an untrusted venue) dwarfs the cost of 30 trades' worth of approval prompts.

## 3. venue_health gap on first activation

**The gap (R2-I1 fold):** when operator first flips `LIVE_USE_ROUTING_LAYER=True` and `LIVE_MODE=live`:
1. Routing layer queries `venue_health` table → empty (no probe has written rows)
2. `routing.py:144-158` filters venues by score, defaulting unobserved venues to score 0.5
3. Top-scored candidate is picked
4. `place_order_request` proceeds against a venue with NO health validation

**Why this is a real risk:** the boot-time smoke check at `main.py:1108-1144` validates auth + read-only paths but does NOT write a `venue_health` row. So the first dispatch fires against a venue that the routing layer has never validated for liveness.

**Chosen mitigation: runbook documentation, NOT code change.**

**Why:**
- A code change to wire boot-smoke into venue_health is ~5 LOC and tempting. But it conflates "boot smoke succeeded" with "venue is healthy under signal load," which are different invariants. The boot smoke runs once at startup; venue health is dynamic.
- M1.5c's recurring health probe is the correct mitigation. M1.5b's runbook surfaces the gap so operator activates with eyes-open.
- Binance is the only venue in M1.5b. Binance's overall reliability is high; the first-dispatch risk is bounded.

**Mitigation surfaces in:**
1. Plan §"Operator activation prereqs" — explicit warning + verification trade recommendation
2. M1.5c plan (future) — recurring health probe is the structural fix

**Operator activation guidance (R2-I1 actionable fold):**

The advisory "treat the first live dispatch as a verification trade" is not actionable when operator can't predict which signal fires first. Reframed with concrete bounds + verification commands:

- **Pre-flip verification** (run before flipping `LIVE_USE_ROUTING_LAYER=True`):
  ```bash
  ssh root@89.167.116.187 \
    'sqlite3 /root/gecko-alpha/scout.db "SELECT * FROM venue_health;"' > .venue_health.txt
  ```
  Empty result confirms first-time activation (no probe rows yet) — gap is genuinely "first dispatch fires before any health probe."

- **First-dispatch timing bound:** observed prod signal rate is ~1.8 signals/hr (43 paper trades / 24h). First M1.5b live dispatch is expected within ~30 minutes of flag flip. Operator should remain at terminal for 1 hour post-flip and grep journalctl for the first `live_dispatch_terminal` event.

- **Walkaway blast-radius bound** (R2-M3 fold): walkaway exposure = `LIVE_TRADE_AMOUNT_USD × hourly_signal_rate × max_open_per_token × walkaway_hours`. Defaults: `$10 × 2 × 1 × N hours`. **8-hour walkaway = ≤ $160 max exposure.** This bound assumes (a) misconfig CRASH at boot guards against silent-no-op losses (§2.2), (b) per-token cap enforced.

- **First-dispatch verification log greps** (post-flip):
  ```bash
  # Did _dispatch_live fire?
  ssh root@89.167.116.187 'journalctl -u gecko-pipeline --since "1 hour ago" | grep live_dispatch_entered'
  # Was a terminal status reached?
  ssh root@89.167.116.187 'journalctl -u gecko-pipeline --since "1 hour ago" | grep live_dispatch_terminal'
  # Was the counter incremented?
  ssh root@89.167.116.187 'sqlite3 /root/gecko-alpha/scout.db "SELECT * FROM signal_venue_correction_count;"'
  ```

- **Anomaly response:** kill-switch via `LIVE_USE_ROUTING_LAYER=False` flag-flip + `systemctl restart gecko-pipeline`. Window: ~2 seconds.

- **First-24h trade size cap:** `LIVE_TRADE_AMOUNT_USD=10` (already M1.5a runbook recommendation V3-M3)

## 4. Operator activation experience: 4-flag cumulative gating

**The flag layering:**
1. `LIVE_TRADING_ENABLED=True` (M1 master kill — enforces boot-time crash if missing)
2. `LIVE_USE_REAL_SIGNED_REQUESTS=True` (M1.5a runtime body gate — runtime bodies fall back to NotImplementedError if False)
3. `LIVE_USE_ROUTING_LAYER=True` (M1.5b multi-venue dispatch gate — engine `_dispatch_live` branch silently no-ops if False)
4. `LIVE_MODE=live` (mode toggle)

Plus credentials (`BINANCE_API_KEY`, `BINANCE_API_SECRET`) and per-signal `signal_params.live_eligible=1`.

**Chosen: defend the 4-flag posture, not collapse.**

**Why (R2-M2 fold):** each flag corresponds to a distinct invariant that the operator gradually trusts:
- Flag 1: "the entire live-trading subsystem is permitted to construct"
- Flag 2: "signed requests work, smoke check passed, NTP sync verified"
- Flag 3: "routing layer picks well; multi-venue dispatch is desired"
- `LIVE_MODE=live`: "actually run live, not shadow"

Collapsing flags buys nothing (the operator must still verify each invariant) and loses the ability to roll back any single layer without disturbing the others. R2 explicitly recommended NOT collapsing.

**Tradeoff:** more flags = more steps to forget. Mitigated by:
- main.py boot guard fails fast with explicit error per flag
- Runbook explicit checklist
- The M1.5a deployment ran cleanly with this layering after a boot-guard hotfix

## 5. Reversibility model

**Fast revert:** `LIVE_USE_ROUTING_LAYER=False` in `.env` → restart.

**What's reversed:**
- New live dispatches bypass `_dispatch_live` (the `if` branch evaluates False)
- Existing M1.5a single-venue resolver path runs again

**What's NOT reversed (in-flight caveat):**
- Orders already submitted to Binance via `place_order_request` are LIVE on Binance
- The `live_trades` row stays `status='open'`
- Engine restart loses the polling loop in `await_fill_confirmation`
- Operator manual cleanup per `docs/runbooks/live-trading-deploy.md` §6

**Why this is acceptable for M1.5b:**
- M1.5c reconciler is the structural fix
- The window between place_order and await_fill terminal is ~30 seconds (timeout_sec=30.0)
- Operator unlikely to flip the flag during that window without intent
- If they DO flip during a dispatch, the cleanup query is a single SELECT + manual close

**Slower revert (git):** `git revert <PR squash>` requires `LIVE_MODE='paper'` BEFORE the revert (otherwise the restored M1.5a `assert mode != "live"` immediately crashes engine entry under live mode). Documented in plan §Reversibility.

## 6. Test strategy

**New tests (Task 1 + Task 2 + Task 4):**

*Counter helper unit tests:*
- 4 counter helper tests (create-on-first-call, bump-on-subsequent, reset-zeros + records correction_at, per-venue-pair independence)
- 1 None/empty signal_type coercion test (R1-I7)

*Engine `_dispatch_live` unit tests (stubbed routing + adapter):*
- 1 cid format regression test (R1-C2 — verifies await_fill receives full `gecko-{paper_trade_id}-{uuid8}` cid, NOT raw intent_uuid)
- 1 status='filled' counter test (counter += 1)
- 1 status='partial' counter NOT incremented test (R1+R2 C3 regression)
- 1 status='rejected' counter NOT incremented test (R1-I2 — no test in original 13)
- 1 status='timeout' counter NOT incremented test
- 1 no-candidates path test (`live_dispatch_no_venue` event + reject-row written per Q2 fold below)
- 1 NotImplementedError silent return test (defense-in-depth — should not fire in practice because §2.2 misconfig CRASH catches the upstream cause)
- 1 BinanceAuthError mid-session test (R1-I2 — verifies engine engages KillSwitch when API key revoked mid-session, since gates already approved)
- 1 multi-candidate ordering test (R1-I2 — verifies dispatch picks highest `venue_health_score`, not just `[0]`; protects against future refactor that drops the sort)
- 1 mode='live' AND flag=False regression (R1-I6 — does NOT call routing under default flag posture)

*Engine `__init__` misconfig CRASH tests (§2.2):*
- 1 CRASH on `mode='live' AND LIVE_USE_ROUTING_LAYER=True AND LIVE_USE_REAL_SIGNED_REQUESTS=False` (R2-C1)
- 1 CRASH on `mode='live' AND LIVE_USE_ROUTING_LAYER=True AND routing=None` (R2-I3)
- 1 NO crash on `mode='shadow' AND LIVE_USE_ROUTING_LAYER=True AND LIVE_USE_REAL_SIGNED_REQUESTS=False` (shadow mode is exempt — no live trades dispatched)

*Shadow regression:*
- 1 shadow-mode unchanged test (LIVE_MODE='shadow' does not invoke `_dispatch_live` and `shadow_trades` row still written)

*Integration test (Task 4 — R2-C2 fold, NEW):*
- 1 end-to-end happy-path test using REAL `scout.main.py` construction code (lifted into a fixture). Stubs ONLY the Binance HTTP layer (via `aioresponses`). Verifies: operator flips all 4 flags → paper-trade opens → `_dispatch_live` fires → `place_order_request` called → `await_fill_confirmation` returns FILLED → counter increments to 1 → `live_dispatch_terminal` event emitted. Catches the regression class where Task 3 forgets `routing=live_routing` kwarg in main.py and unit tests pass while prod silently no-ops.

**Modified tests:**
- `tests/test_live_master_kill.py` — add `LIVE_USE_ROUTING_LAYER=False` default

**Total: 18 new test cases (was 13). Regression surface: M1.5a tests must stay green.**

**Fixture pattern (R1-I4 fold — first engine-level test file):** `tests/test_live_engine_dispatch.py` introduces the FIRST direct unit-test surface for `LiveEngine`. Document the stub-adapter + stub-routing fixture pattern at the top of the file with a docstring. M1.5c reconciler tests will reuse this fixture; the convention should be visible.

## 7. Open questions — RESOLVED post-design-stage review

**Q1 (RESOLVED — fold):** distinguish `BinanceAuthError` / `BinanceIPBanError` from generic Exception?
- **Resolution:** YES, distinguish. After Gate 10 already approved the dispatch, an in-flight `BinanceAuthError(-2015)` means the API key was revoked mid-session. This is severe enough to engage KillSwitch (`self._kill_switch.engage(reason="binance_auth_revoked_mid_session")`) — subsequent dispatches must NOT fire until operator investigates. `BinanceIPBanError` (HTTP 418) → KillSwitch + Telegram alert. `VenueTransientError` → log INFO + return (next signal will retry, this is a known transient class).

**Q2 (RESOLVED — fold):** write `live_trades` reject row when no candidates?
- **Resolution:** YES, write the row. R2-M1: dashboard `/api/live_trades` is operator's primary observability surface. Without a row, "routing silently returns zero candidates for every signal" is invisible. Engine writes `INSERT INTO live_trades (paper_trade_id, status, reject_reason, ...) VALUES (?, 'rejected', 'no_venue', ...)`. The reject_reason 'no_venue' is already in M1.5a's CHECK constraint — no migration needed.

**Q3 (RESOLVED — fold):** CRASH vs WARN on `LIVE_USE_ROUTING_LAYER=True AND routing=None`?
- **Resolution:** CRASH at engine `__init__`. R2-I3 + R1-M2 both recommend CRASH. Cost-of-crash is bounded by systemd `RestartSec=30s + StartLimitBurst=3` + OnFailure Telegram (M1.5a runbook §1+§2). Cost-of-WARN-and-skip is unbounded (operator walkaway = arbitrary missed signals). Folded into §2.2 above.

**Q4 (RESOLVED — defer):** make `timeout_sec` a Settings field?
- **Resolution:** DEFER to M1.5c. The 30s value is reasonable for Binance spot fills; Settings-ization is M1.5c hygiene, not M1.5b correctness. M1.5a left it as a parameter; M1.5b inherits. If operator finds 30s wrong in early activation, a one-line code-edit + restart is faster than designing a Settings interface for this single value.

## 8. M1.5b deferred items (forward to M1.5c plan)

- Telegram approval gateway runtime hook (V1-C1 approval-half closure)
- Recurring health probe (replaces boot-time-only smoke; closes R2-I1 venue_health gap structurally)
- Reconciliation worker for orphaned `live_trades` rows + in-flight reversibility cleanup
- Automatic `reset_on_correction` triggers (operator-correction window detection)
- `total_fills_lifetime` column for dashboard telemetry that survives resets
- V2 deferred minors: ServiceRunner cancel-log, view CAST symmetry, override-NULL filter, venue_health staleness gate
- `LIVE_AWAIT_FILL_TIMEOUT_SEC` Settings field (if reviewers OK7 above flags it)

## 9. Reviewer-fold summary

### Plan-stage (commit `e6c4b4e`)

| Finding | Reviewer | Severity | Status |
|---|---|---|---|
| Engine assert blocks live | R1+R2 | C1 | Folded — Task 2 Step 1 removes assert |
| cid mismatch in await_fill | R1 | C2 | Folded — `_dispatch_live` uses `make_client_order_id` |
| Partial-fill double-count | R1+R2 | C3 | Folded — counter restricted to `status='filled'` only |
| Counter-reset destroys history | R2 | C2 | Acknowledged in docstring + runbook + design §2.5 |
| WARN log on flag-vs-layer mismatch | R1 | I5 | Superseded by design-stage CRASH fold |
| Test for flag=False+live | R1 | I6 | Folded — explicit test added |
| None signal_type | R1 | I7 | Folded — coerce to "unknown" |
| venue_health gap | R2 | I1 | Folded plan-stage; sharpened design-stage |
| Counter only Binance | R2 | I2 | Per-venue-by-design — design §2.7 |
| V1-C1 partial closure | R2 | I3 | Folded — renamed "V1-C1 routing-half closure" |
| In-flight reversibility | R1+R2 | M | Folded — runbook cross-ref to §6 |
| 4-flag cumulative gating | R2 | M | Defended — design §4 |

### Design-stage (this commit)

| Finding | Reviewer | Severity | Status |
|---|---|---|---|
| Silent no-op on signed-disabled misconfig | R2 | C1 | **Folded — engine __init__ CRASH (§2.2)** |
| No end-to-end happy-path integration test | R2 | C2 | **Folded — Task 4 integration test (§6)** |
| Shadow_trades writer ambiguity under live | R1 | I1 | **Folded — §2.7a explicitly closes BL-055 at first live signal** |
| 4 missing test cases | R1 | I2 | **Folded — added BinanceAuthError, multi-candidate, status='rejected', routing=None+live runtime tests (§6)** |
| RouteCandidate test stub shape | R1 | I3 | **Folded — fixture pattern documented (§6)** |
| First engine-level test file | R1 | I4 | **Folded — fixture pattern docstring requirement (§6)** |
| venue_health mitigation actionable | R2 | I1 | **Folded — pre-flip verification command + walkaway calc + post-flip greps (§3)** |
| Missing entry/candidate-count telemetry | R2 | I2 | **Folded — `live_dispatch_entered` + `live_dispatch_candidates_returned` events (§7 Q1 fold)** |
| CRASH vs WARN on routing=None | R1+R2 | M2/I3 | **Folded — CRASH at engine __init__ (§2.2 + §7 Q3)** |
| Runbook update task missing | R2 | I4 | **Folded — Task 5 in plan (§12 below)** |
| BinanceAuthError → KillSwitch | R1 | M1 | **Folded — §7 Q1** |
| Q2 — write reject row on no-venue | R2 | M1 | **Folded — §7 Q2** |
| partial→CANCELED trace | R1 | M3 | **Folded — §2.4 verified path** |
| timeout_sec Settings-ization | — | Q4 | **Deferred to M1.5c** |

## 10. Blast radius

**Default flag posture (`LIVE_USE_ROUTING_LAYER=False`):** zero new code paths exercised in production. M1.5b is dormant after deploy until operator flips the flag.

**With operator opt-in (`LIVE_USE_ROUTING_LAYER=True` + `LIVE_MODE=live`):** real money trades dispatched against Binance. Per-trade size capped by `LIVE_TRADE_AMOUNT_USD` (operator recommendation: 10 USD for first 24h). Per-token cap by `LIVE_MAX_OPEN_POSITIONS_PER_TOKEN`. Kill-switch primitive in place.

**Unintended-blast-radius checks:**
- Shadow-mode telemetry (BL-055 soak): unchanged — `_dispatch_live` does not fire under `mode='shadow'`
- Paper-trade-driven engine path: unchanged — engine entry's gates + allowlist + write order all unchanged
- M1.5a smoke check: unchanged — runs at boot regardless of `LIVE_USE_ROUTING_LAYER`

## 11. Approval checklist

Before merge:
- [x] Plan-stage 2-reviewer pass complete (folded at e6c4b4e / a4f2fa6)
- [x] Design-stage 2-reviewer pass complete (folded in d2de30b + this commit)
- [ ] All folds applied + test coverage verified
- [ ] Build → PR → 3-vector reviewer pass → merge → deploy
- [ ] M1.5b deploy complete + first signal observed (live OR shadow OR paper as posture dictates)

## 12. Runbook update task (R2-I4 fold)

The plan adds a NEW Task 5 (after Task 4 regression+black, before PR creation) to update `docs/runbooks/live-trading-deploy.md`:

1. **§4 .env activation checklist** — add `LIVE_USE_ROUTING_LAYER=True` as a REQUIRED checklist item
2. **§4 first-dispatch verification block** (NEW) — add the pre-flip verification command (`SELECT * FROM venue_health` returns empty) + walkaway calc + post-flip log greps from design §3
3. **§5 reversibility** — add an in-flight caveat note: "if engine restart happens between place_order_request and await_fill_confirmation, the order is live on Binance with no engine watcher; cleanup per §6"
4. **§6 orphaned trade reconciliation** — extend the SELECT query annotation to cover M1.5b's specific scenario (flag flipped mid-`await_fill`), explicit step-by-step manual remediation
5. **§7 deferred items** — reslate from "M1.5a → M1.5b deferred" to "M1.5b → M1.5c deferred"; remove items closed by M1.5b (engine routing dispatch, correction counter writers); keep items deferred to M1.5c (Telegram approval gateway, recurring health probe, reconciliation worker)
