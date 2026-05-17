**New primitives introduced:** NONE. Analysis-only PR — `tasks/findings_first_signal_retirement_decision_2026_05_17.md` (decision doc) + backlog flip on `BL-NEW-FIRST-SIGNAL-RETIREMENT-DECISION` + possibly one operator-instruction block for revival via the existing `scout.trading.auto_suspend` revival helper. No new code, no Settings, no schema.

# Plan: BL-NEW-FIRST-SIGNAL-RETIREMENT-DECISION

**Backlog item:** `BL-NEW-FIRST-SIGNAL-RETIREMENT-DECISION` (filed 2026-05-17 as cycle 7 Finding 3 follow-up; updated post-V36 fold to drop the "structurally non-eligible" framing). Cycle 9 of autonomous backlog knockdown.

**Goal:** Resolve the 16-day `first_signal` silence by deciding between RETIRE / REVIVE-AND-SOAK / DEFER. Produce a findings doc with the resolution + recommended operator action.

**Architecture:** None — analysis + findings. Compose with the existing `auto_suspend` revival helper if the decision is REVIVE.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Signal-revival decision frameworks | None | Project-internal trading decision; no Hermes skill applicable |
| Auto-suspend audit-trail interpretation | None | Project-internal — `scout/trading/auto_suspend.py` is the source of truth |

awesome-hermes-agent: 404 (consistent prior). **Verdict:** custom project-internal decision doc.

## Drift verdict

NET-NEW. No existing `tasks/findings_*first_signal*.md`. The cycle-7 findings doc (`findings_live_evaluable_signal_audit_2026_05_17.md`) Finding 3 raised the question but didn't drill the cause. Cycle 8 is on a different signal (`chain_completed`). No parallel session is touching first_signal per master HEAD inspection at `63aa13b`.

## Investigation results (data already gathered)

### (a) signal_params row — auto-suspended

```
signal_type:       first_signal
enabled:           0
suspended_at:      2026-05-02T01:00:18.077622+00:00
suspended_reason:  hard_loss
updated_by:        auto_suspend
```

### (b) signal_params_audit row id=14

```
field_name:   enabled (1 → 0)
reason:       hard_loss: max_drawdown $-593 (n=253)
applied_by:   auto_suspend
applied_at:   2026-05-02T01:00:18.077622+00:00
```

### (c) Most-recent first_signal trades

| trade_id | opened_at | signal_data |
|---|---|---|
| 1525 | 2026-05-01T19:52:31Z | `{"signals": ["momentum_ratio","cg_trending_rank"]}` |
| 1521 | 2026-05-01T14:22:50Z | (same) |
| 1477 | 2026-04-30T11:11:26Z | (same) |
| 1451 | 2026-04-29T06:20:30Z | (same) |
| 1419 | 2026-04-28T14:58:42Z | (same) |

3 trades on 2026-05-01 → auto-suspend at 2026-05-02T01:00Z (3.5h after the last one) → silence since.

### (d) All-time first_signal cohort PnL

| status | n | total_pnl_usd | avg_pnl_usd |
|---|---:|---:|---:|
| `closed_expired` | 198 | -$1,213.20 | -$6.13 |
| `closed_sl` | 14 | -$922.30 | -$65.88 |
| `closed_moonshot_trail` | 1 | $55.93 | $55.93 |
| `closed_peak_fade` | 2 | $132.41 | $66.20 |
| `closed_tp` | 6 | $511.32 | $85.22 |
| `closed_trailing_stop` | 35 | $1,303.64 | $37.25 |
| **TOTAL** | **256** | **-$132.20** | **-$0.52** |

Winners: 44 trades (`tp + trailing_stop + moonshot_trail + peak_fade`) = 17.2% win rate
Losers: 212 trades (`expired + sl`)
Net EV per trade: ≈ -$0.52 (slightly negative)

### (e) Cumulative PnL at suspend time

```
cumul_pnl @ 2026-05-02T01:00:18Z = -$57.85
```

**V39 SHOULD-FIX precision:** `max_drawdown` per `auto_suspend.py:86-94` is the **peak-to-trough drop in running cumulative PnL** within the rolling window — NOT the absolute trough. The reason field's `$-593` figure means cumulative PnL fell $593 from its running peak. At suspend time the net was -$57.85, so the trough sat near (peak - $593) with subsequent intra-window recovery of $535 back toward zero. Doesn't change the gate-fires conclusion (the second arm reads `max_drawdown ≤ hard_loss` directly).

## The critical context — pre-PR-#79 vs current auto-suspend logic

Per memory `project_bl_autosuspend_fix_2026_05_06.md`: "PR #79 2026-05-06: combined-gate hard_loss + drawdown_baseline_at + revival helper. 3 soak windows end 2026-05-13."

PR #79 (2026-05-06) added the **combined gate** that explicitly requires both `max_drawdown ≤ hard_loss` AND `net_pnl < pnl_threshold` (current `scout/trading/auto_suspend.py:236-237`):

```python
fires_hard_loss = net_pnl <= hard_loss or (
    max_drawdown <= hard_loss and net_pnl < pnl_threshold
)
```

**first_signal was auto-suspended 2026-05-02 — BEFORE PR #79 shipped.** It fired under the PRE-fix logic that was subsequently recognized as too aggressive (false-positive on max_drawdown alone when net_pnl was recovering).

If first_signal were re-evaluated TODAY under the current combined gate:
- `net_pnl = -$132` → `-132 ≤ -500`? **NO** (first arm fails)
- `max_drawdown = -$593` → `-593 ≤ -500`? YES
- `net_pnl < -$200`? `-132 < -200`? **NO** (second arm fails)
- Combined: NEITHER arm fires → **would NOT auto-suspend today**

**MIN_TRADES floor (V39 SHOULD-FIX note):** `SIGNAL_SUSPEND_MIN_TRADES=50` gates ONLY the `pnl_threshold` rule path (`auto_suspend.py:298`); both arms of `fires_hard_loss` bypass it. At n=256, MIN_TRADES is moot for either path — the conclusion is robust to that parameter.

**Conclusion:** first_signal's suspension is an artifact of pre-fix auto-suspend over-aggression, NOT a current-rules violation.

## Why keep paper-trading first_signal? (V38 MUST-FIX — affirmative argument)

The pure "false-positive suspension" argument explains WHY suspension was unjust; it doesn't explain WHY revival is worth a paper-slot. The affirmative case:

| Tail | n | total | avg/trade |
|---|---:|---:|---:|
| Positive (`closed_tp + closed_trailing_stop + moonshot_trail + peak_fade`) | 44 | **+$2,003.30** | **+$45.53** |
| Negative (`closed_expired + closed_sl`) | 212 | **-$2,135.50** | **-$10.07** |

The signal has a real positive-tail edge — winners average +$45 per trade (concentrated in `closed_tp` $85 + `closed_trailing_stop` $37). The drag comes from `closed_expired` (198 trades × -$6.13 each = -$1,213) — i.e., trades that ride out the full `max_duration_hours=168` without hitting TP or SL. That's a tail-decay problem, not a "the signal picks losers" problem.

This makes first_signal a regime-dependent edge worth observing under the new combined gate: if the post-revival expired-rate stays comparable to pre-suspend, EV stays slightly negative and Option B (retire) becomes data-supported. If expired-rate drops (e.g., market becomes more directional), EV could flip positive. The 14d soak window IS the measurement.

If the soak confirms regression, the cycle-9 finding establishes a clean retirement record. If the soak surfaces regime improvement, the operator has a paper-validated re-entry. Either way, Option A produces durable evidence that pure DEFER (Option C) cannot.

## Decision tree

| Option | Action | Rationale | Cost |
|---|---|---|---|
| **A: REVIVE-AND-SOAK (recommended)** | `Database.revive_signal_with_baseline(signal_type='first_signal', force=False)` + reset `drawdown_baseline_at` to now + soak 14d | Suspension was a pre-fix false-positive; current combined gate would NOT re-fire. Revival helper exists exactly for this case (per memory). 14-day soak window lets the new combined gate observe whether genuine regression vs noise. | 1 SQL operation; soak passive |
| **B: RETIRE in code** | Remove `trade_first_signals` from the dispatch path; remove `FIRST_SIGNAL_MIN_SIGNAL_COUNT` Settings | -$0.52 EV, 17% win rate, 16 days silent already with no operator pain. Reduces code surface. | ~30min code+test; PR cycle |
| **C: DEFER** | Leave enabled=0; revisit at next live-trading roadmap revisit (gated on BL-055) | Lowest cost, but leaves dispatcher code path live, paper-trade slot tied up if revived later, and operator has to re-examine context | None now; revisit cost later |

## Recommendation: Option A (REVIVE-AND-SOAK)

Rationale:
1. **Suspension was a false-positive under retired logic** — the revival helper was added by PR #79 explicitly for this kind of case (per memory `project_bl_autosuspend_fix_2026_05_06.md`).
2. **All-time net PnL is slightly negative (-$132)** but not catastrophic. The signal has a real positive-tail (`tp + trailing_stop` = $1,815 across 41 trades, avg +$44). Heavy losses concentrate in `closed_expired` (198 trades × -$6 each = expected-value drag from time-decay-without-tp).
3. **Current combined gate would not re-fire.** Revival is statistically safe.
4. **Decision-locked window** — soak 14d, re-evaluate at 2026-05-31. If hard_loss DOES re-trip under current gate, that's genuine regression evidence and Option B becomes data-supported.

## Tasks

### Task 1: Findings doc (no design step needed — pure decision artifact)

- [ ] Write `tasks/findings_first_signal_retirement_decision_2026_05_17.md` containing:
  - All evidence from §Investigation results (a)–(e) above
  - Pre-PR-#79 vs current logic comparison (§Critical context)
  - Recommendation Option A with concrete operator action
  - Pre-registered re-evaluation criteria at 2026-05-31

### Task 2: Backlog close + memory checkpoint

- [ ] Flip `BL-NEW-FIRST-SIGNAL-RETIREMENT-DECISION` to SHIPPED-WITH-DECISION
- [ ] Memory checkpoint `project_first_signal_revival_decision_2026_05_31.md` with revival query, soak window, decision criteria

### Task 3 (deferred to operator): execute revival via the helper

**V38 MUST-FIX:** use `Database.revive_signal_with_baseline` (`scout/db.py:4056`) instead of raw SQL. The helper enforces (a) the `SIGNAL_REVIVAL_MIN_SOAK_DAYS=7` cool-off (irrelevant for first revival but sets the right precedent), (b) joint `tg_alert_eligible` restore via `DEFAULT_ALLOW_SIGNALS` lookup at `db.py:4204-4216` (for first_signal, `restored_to=0` is logged since it's not in DEFAULT_ALLOW_SIGNALS — that's correct existing behavior, but the helper handles the decision atomically), (c) `BEGIN EXCLUSIVE` transaction wrapping, (d) audit row written with consistent format, (e) `signal_revived_tg_eligible` + `revive_signal_force_*` structlog observability hooks.

```bash
# Operator-only, NOT part of this PR. After PR merge:
ssh root@srilu-vps
cd /root/gecko-alpha
/root/.local/bin/uv run python -c "
import asyncio
from scout.db import Database
from scout.config import get_settings
async def revive():
    db = Database('/root/gecko-alpha/scout.db')
    await db.connect()
    await db.revive_signal_with_baseline(
        'first_signal',
        reason='cycle9 revive-and-soak — pre-PR-#79 false-positive; 14d soak ends 2026-05-31',
        operator='operator_cycle9_manual',
        settings=get_settings(),
    )
    await db.close()
asyncio.run(revive())
"
sudo systemctl restart gecko-pipeline   # pick up enabled=1 in the live process
```

(Operator runs this manually; the PR ships the findings doc, not the action.)

The helper logs `signal_revived_tg_eligible` with `restored_to=0` for first_signal (not in DEFAULT_ALLOW_SIGNALS); confirms operator opted out of TG alerts on this signal pre-existing.

## Pre-registered re-evaluation criteria at 2026-05-31 (V38 SHOULD-FIX folds: data-bound + reframed KEEP threshold)

**Expected fire-count at 14d (V38 §11 fold):** pre-suspend rate was 5 trades in the 3.5 days before suspend (1.43/day). At that rate, **14d expected n ≈ 20**. The new combined gate doesn't gate trade-firing (only auto-suspend), so the dispatcher-fire rate is determined by upstream candidate availability — pre-suspend rate is the best estimator. Per CLAUDE.md §11 (soak windows are data-bound, not calendar-bound):

- If `n < 10` at 14d → **auto-EXTEND to 28d** (data threshold not met; n too low to verdict)
- If `n ≥ 10` at 14d → run the verdict table below

| Observation @ 14d post-revival (n ≥ 10) | Verdict | Action |
|---|---|---|
| `first_signal` auto-suspends again under current gate | RETIRE (Option B confirmed) | File `BL-NEW-FIRST-SIGNAL-RETIRE-CODE`; the signal genuinely regressed beyond pre-suspend |
| Cumulative PnL ≥ 0 AND positive-tail win rate ≥ 17% (no regression vs all-time) | KEEP-PAPER (validated research surface) | Close cycle 9 cleanly |
| Cumulative PnL ≥ +$200 AND positive-tail win rate ≥ 17% AND avg winner ≥ +$30 | KEEP-PAPER + flag for live-trading roadmap revisit | Memory note |
| Cumulative PnL < 0 BUT positive-tail win rate ≥ 17% (in-line with all-time edge) | EXTEND-SOAK 14d | File `BL-NEW-FIRST-SIGNAL-EXTEND-SOAK` |
| Cumulative PnL < 0 AND positive-tail win rate < 17% (regressing) | RETIRE (Option B) | File `BL-NEW-FIRST-SIGNAL-RETIRE-CODE` |
| No trades fire (`n == 0` in 14d) | INVESTIGATE then RETIRE | File dispatcher-availability investigation |

**V38 SHOULD-FIX rationale:** the threshold "Cumulative PnL ≥ 0 AND positive-tail win rate ≥ 17%" demands the signal NOT REGRESS vs its all-time profile. The prior "Cumulative PnL ≥ +$200" threshold sat ~7σ above EV per V38's note and mechanically biased toward retirement. The "≥ 17%" floor matches the all-time positive-tail rate; "≥ 0 cumulative PnL" demands non-regression. The +$200 threshold is preserved as the HIGHER bar that escalates to "flag for live-roadmap revisit."

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Revival triggers immediate hard_loss re-fire | Low | Low | Combined gate math above; if it does fire, that's the Option B trigger condition — clean signal |
| Operator forgets to run the revival SQL | Medium | Low | Memory checkpoint pre-registers the query and 14d window |
| 14d soak produces ambiguous result | Medium | Low | EXTEND-SOAK option in decision criteria |
| `drawdown_baseline_at` reset disrupts other signal_params bookkeeping | Very Low | Low | The column is per-signal_type; revival of first_signal does not affect other signals |

## Out of scope

- Changes to `auto_suspend.py` logic itself — current logic shipped via PR #79 is correct
- Investigation of why pre-PR-#79 logic over-fired on first_signal specifically (the why is documented in memory; no further action needed)
- Changes to `FIRST_SIGNAL_MIN_SIGNAL_COUNT` admission gate
- Auto-revival policy — leave revival as operator-manual to maintain audit trail

## Deployment

Doc-only PR. No service restart, no schema change. Operator runs the revival SQL manually post-merge if the decision is REVIVE. Findings doc + backlog + memory checkpoint are committed.
