# BL-070 — Entry Stack Gate (refuse trades with insufficient signal confirmation)

**Status:** **SHELVED — re-evaluate after 2026-05-15** (Tier 1a flip soak ends)
**Branch (target):** `feat/bl-070-entry-stack-gate` (not yet created)
**Estimated effort:** 0.5 day
**Date:** 2026-05-01

## Why shelved

Plan went through 2 parallel reviewers (architect + adversarial). Adversarial reviewer's Q10 — "why not just disable the 2 unprofitable signals via Tier 1a flag flip?" — produced a 30d data check that showed:

| Scenario | 30d net | Effort |
|---|---|---|
| Actual (with gainers_early + trending_catch enabled) | −$506 | — |
| Disable both via Tier 1a `enabled=0` | **+$428** | 2 SQL UPDATEs |
| BL-070 stack-gate (theoretical max) | +$722 | half-day build |

**Tier 1a kill captures $933 of swing today with zero code.** BL-070's incremental lift over the Tier 1a baseline is $200-$400 max — uncomfortably close to the abandon threshold the plan set.

**Decision:** flip Tier 1a kills, soak 14d, re-evaluate BL-070 with cleaner data.

## Tier 1a flip executed 2026-05-01T14:06Z

- `signal_params.enabled=0` for `gainers_early` (operator kill — 30d net −$629)
- `signal_params.enabled=0` for `trending_catch` (belt-and-suspenders — already off via .env outer kill)
- Synced all 8 signal_params rows to current prod .env values (leg_1_pct=10.0, leg_1_qty_frac=0.50, trail_pct=20, sl_pct=25, max_duration_hours=168) before flipping `SIGNAL_PARAMS_ENABLED=true`
- Pipeline + dashboard restarted, `trade_skipped_signal_disabled` events confirmed firing post-warmup
- Audit rows in `signal_params_audit` for the operator action

## Resume protocol

When re-evaluating on/after 2026-05-15:
1. Re-run `scripts/backtest_v1_signal_stacking.py` against new soak window data
2. Compare 30d net pre-flip (−$506) vs post-flip 14d
3. Decide: ship BL-070, scope-down further, or close as no-longer-needed
4. If shipping: address the 3 plan-review blockers (drop `paper_trades` source, point-in-time v2 backtest with lookback sensitivity, index audit) and the 5 design-review-pending items

---

## ORIGINAL PLAN (preserved for reference)


---

## Why this exists — backtest evidence (already captured)

`scripts/backtest_v1_signal_stacking.py` ran against last 30 days of paper trades and produced a clear, actionable split:

| Stack count at lifespan | n trades | net pnl | avg peak | avg capture | win rate | expired % |
|---|---|---|---|---|---|---|
| 1 | 316 | **−$1,243** | 7.2% | −1.2% | 40.5% | **81.3%** |
| 2 | 227 | (−$0.55/trade) | 10.2% | +0.4% | 47.6% | 62.1% |
| 3 | 121 | (−$0.96/trade) | 18.0% | +0.2% | 47.1% | 35.5% |
| 4 | 36 | +$6.40/trade | 21.7% | +1.4% | 55.6% | 38.9% |
| 5 | 13 | **+$60.07/trade** | **55.3%** | **+20%** | 46.2% | 46.2% |

- **Stack <2: −$1,243 net across 316 trades.** 81% of these expired flat.
- **Stack ≥2: +$722 net across 398 trades.** Where the system actually works.
- **Net 30d went from +$722 to −$521 because the −$1,243 cohort dragged it down.**

Filtering stack=1 trades out of the entry path alone would have converted **−$521 → +$722 over 30d** — a swing of **$1,243** with zero exit-logic changes.

The screenshot tokens (NOCK, CHIP, ORCA, etc.) all had stack ≥ 2 confirmations, so the proposal does **not** lose any of the real winners.

---

## The proposal — entry-time stack gate

Add a new check inside `engine.open_trade`, before the existing checks:

> Look back at the last `STACK_GATE_LOOKBACK_HOURS` of signal events for the candidate `token_id`. If fewer than `STACK_GATE_MIN_STACK` distinct signal classes have fired in that window (counting the incoming signal as the first), refuse the trade with `trade_skipped_low_stack`.

**Default behavior:**
- `STACK_GATE_ENABLED=False` (no-op on first deploy — match Tier 1a/1b rollout pattern)
- `STACK_GATE_LOOKBACK_HOURS=2` (initial proposal — must be tuned by v2 backtest)
- `STACK_GATE_MIN_STACK=2`

---

## Honest caveat the v2 backtest must answer

The v1 backtest counted distinct signals across the **lifespan of the trade** (open → close, often 24h). Some of those 2nd/3rd signals fired *after* the trade opened — they would not be visible to an entry-time gate that looks backward only.

The actual lift from "entry-time stack ≥ 2" is therefore some fraction of the +$1,243 lifespan-stack lift. Could be 70%, could be 30%. **`scripts/backtest_v2_entry_stack_gate.py` must run before merge** and produce the entry-time number. If the entry-time lift is < $200 over 30d, the gate isn't worth the complexity and we abandon.

The v2 backtest is a hard merge gate, not a nice-to-have.

---

## Scope (this PR)

### In scope
1. New helper `scout/trading/stack_counter.py` — `async def count_stack(db, token_id, lookback_hours, current_signal_type) -> tuple[int, list[str]]`
2. Engine integration — new step in `engine.open_trade` before warmup/price/dup checks (or after warmup, but before DB-heavy duplicate/exposure SQL)
3. Config additions — 3 fields in `scout/config.py` (default OFF)
4. New v2 backtest script — `scripts/backtest_v2_entry_stack_gate.py` that simulates the gate on last 30d
5. Dashboard skip-counter — extend Trading-tab stats with `skipped_by_stack_gate` count for last 24h
6. Tests (~8) — stack counting correctness, gate refuses below threshold, gate accepts at threshold, flag-off behavior unchanged, integration test through `open_trade`

### Out of scope (deliberate)
- Conviction-locked HOLD (BL-067) — separate bigger PR. This is entry-only.
- chain_completed dispatcher (BL-068) — separate.
- Per-signal-type custom thresholds (e.g. high-conviction signals can bypass the gate). Defer until v1 lands and we have observation data.
- Tuning the lookback window per signal-type. v1 uses one global value.
- Auto-tuning of `MIN_STACK` based on observed performance (folds into Tier 1a calibration eventually).

---

## Signal sources counted

Same as the v1 backtest — verified against actual VPS schema:

| Source table | Time column | Token column | Label |
|---|---|---|---|
| `gainers_snapshots` | `snapshot_at` | `coin_id` | gainers |
| `losers_snapshots` | `snapshot_at` | `coin_id` | losers |
| `trending_snapshots` | `snapshot_at` | `coin_id` | trending |
| `chain_matches` | `completed_at` | `token_id` | chains |
| `predictions` | `predicted_at` | `coin_id` | narrative |
| `velocity_alerts` | `detected_at` | `coin_id` | velocity |
| `volume_spikes` | `detected_at` | `coin_id` | volume_spike |
| `tg_social_signals` | `created_at` | `token_id` | tg_social |
| `paper_trades` (other signal_types on same token, distinct from incoming) | `opened_at` | `token_id` | trade:{signal_type} |

The incoming `signal_type` for the trade being decided counts as 1.

---

## Design questions (locked-in answers)

| Q | Decision |
|---|---|
| Where in `engine.open_trade` does the gate run? | Step 0c — after warmup, after Tier 1a `signal_params.enabled` check, before price lookup/duplicate/exposure. Cheap-first ordering: warmup is no-DB; Tier 1a is one cached DB read; stack gate is one DB query. Save the heavier price/exposure checks for after stack passes. |
| Lookback bound | `datetime(now, '-N hours')` SQL with `datetime(col)` wrapper per PR #24 audit |
| Distinct counting | Each source contributes ≤1 to the count (a 100-tick gainers grind = 1 stack point). Same definition as v1 backtest. |
| Behavior when flag OFF | `count_stack()` not called. Engine path identical to today. |
| Behavior when `signal_type` is unknown | `engine.open_trade` already raises/skips on unknown signal types via Tier 1a `get_params()` — no change here. |
| Cache | None. Stack count is a per-trade-decision query (~10 small SQL hits), cheap enough to run fresh. Cache invalidation is harder than the savings warrant. |
| Cooldown bypass | The existing 48h same-token-same-signal-type cooldown still applies BEFORE the stack gate would even be reached on a re-open. No interaction. |

---

## Acceptance criteria

- [ ] `scripts/backtest_v2_entry_stack_gate.py` runs on VPS scout.db and outputs:
  - n trades that would have been skipped at entry-time stack < 2
  - PnL of those trades (must sum to a meaningful negative)
  - Simulated 30d net under the gate vs actual
  - Per-screenshot-token impact (BLEND/ZKJ/MAGA must NOT be skipped — they're the real winners)
- [ ] Entry-time lift > $200 over 30d (otherwise abandon)
- [ ] Existing 1389+ tests still green
- [ ] 8 new tests pass (stack helper + engine integration + flag-off no-op)
- [ ] Migration: none (no schema change)
- [ ] Dashboard `/api/trading/stats` exposes `skipped_by_stack_gate_24h`
- [ ] Default OFF — first deploy is no-op

---

## Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Entry-time lift is much smaller than lifespan-time lift | High | v2 backtest is a HARD merge gate. Abandon if < $200. |
| Stack-counting query is slow (8 sources * trade rate) | Medium | Each source has `(coin_id|token_id, ts)` index. Bounded to 8 single-row LIMIT 1 lookups per call. <50ms total expected. |
| Filtering reduces trade volume too much, starves data | Medium | Default OFF for 24h soak before flip. Trade volume reduction estimated at 44% per v1 backtest — acceptable since the cut trades were net negative. |
| A would-be winner trade is skipped because its 2nd signal hadn't fired yet | High | This is the design — we accept some opportunity cost in exchange for cutting the −$1,243 bleed. v2 backtest will quantify how many actual winners get cut. |
| Trading channels' BL-064 dispatch (when CA-bearing messages arrive) gets blocked because tg_social is the only signal | Medium | Operator decision: a `tg_social` trade with no other co-fired signal is exactly the kind we wanted to avoid post-BL-067. Accept by default. Can add `tg_social` bypass later if curator quality justifies it. |
| Cron / scheduler interaction | Low | Gate is per-trade-decision, not scheduled. No interaction. |

---

## Rollout plan

1. Merge with `STACK_GATE_ENABLED=False`. Migration: none. Zero behavior change. Soak 24h.
2. Flip flag to True. Restart pipeline. Verify in logs: `trade_skipped_low_stack` events appear, `signal_params_hit` style cadence.
3. Monitor 7d:
   - dashboard skip count
   - `paper_trades` net pnl trend
   - distribution of stack counts seen at decision time
4. If signal trends positive, leave on. If neutral/negative, flip OFF (one-line operator change).

---

## Reviewer disposition placeholder

Pending: 2 plan reviewers (code-architect, adversarial general-purpose). Then 2 design reviewers, 3 PR reviewers.
