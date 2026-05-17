# BL-NEW-FIRST-SIGNAL-RETIREMENT-DECISION — Findings 2026-05-17

**Filed:** 2026-05-17 (cycle 9 of autonomous backlog knockdown)
**Source:** srilu-vps `prod.db` (`/root/gecko-alpha/scout.db`)
**Triggered by:** Finding 3 of `findings_live_evaluable_signal_audit_2026_05_17.md` (cycle 7), post-V36 mechanistic correction.

## TL;DR

`first_signal` was **auto-suspended on 2026-05-02T01:00:18Z** with reason `"hard_loss: max_drawdown $-593 (n=253)"` — under the **pre-PR-#79 auto_suspend logic**. PR #79 (commit `7637ea3`, shipped 2026-05-06) added the combined-gate `(max_drawdown ≤ hard_loss AND net_pnl < pnl_threshold)` requirement that would have prevented this suspension at the time.

If re-evaluated TODAY under current logic:
- `net_pnl = -$132 ≤ -$500`? **NO** (first arm)
- `max_drawdown = -$593 ≤ -$500` AND `net_pnl < -$200`? `-132 < -200`? **NO** (second arm)
- **Gate does NOT fire.** Suspension is an artifact of pre-fix over-aggression.

**Recommendation: Option A — REVIVE-AND-SOAK** for 14 days (decision date 2026-05-31) using the existing `Database.revive_signal_with_baseline` helper.

## Evidence

### (a) signal_params row — auto-suspended

```
signal_type:       first_signal
enabled:           0
suspended_at:      2026-05-02T01:00:18.077622+00:00
suspended_reason:  hard_loss
updated_by:        auto_suspend
tg_alert_eligible: 0  (pre-existing operator opt-out)
```

### (b) signal_params_audit row id=14

```
field_name:   enabled (1 → 0)
reason:       hard_loss: max_drawdown $-593 (n=253)
applied_by:   auto_suspend
applied_at:   2026-05-02T01:00:18.077622+00:00
```

### (c) All-time cohort PnL

| status | n | total | avg |
|---|---:|---:|---:|
| `closed_expired` | 198 | -$1,213.20 | -$6.13 |
| `closed_sl` | 14 | -$922.30 | -$65.88 |
| `closed_moonshot_trail` | 1 | $55.93 | $55.93 |
| `closed_peak_fade` | 2 | $132.41 | $66.20 |
| `closed_tp` | 6 | $511.32 | $85.22 |
| `closed_trailing_stop` | 35 | $1,303.64 | $37.25 |
| **TOTAL** | **256** | **-$132.20** | **-$0.52** |

Winners: 44 (`tp + trailing_stop + moonshot_trail + peak_fade`) = **17.2% win rate**

### (d) Positive-tail edge analysis

| Tail | n | total | avg/trade |
|---|---:|---:|---:|
| Positive | 44 | **+$2,003.30** | **+$45.53** |
| Negative | 212 | **-$2,135.50** | **-$10.07** |

Winners average +$45/trade — concentrated in `closed_tp` ($85) + `closed_trailing_stop` ($37). The drag is concentrated in `closed_expired` (198 trades × -$6.13 = -$1,213) — **tail-decay problem, not picks-losers problem**. The signal has a regime-dependent edge worth observing under the new combined gate.

### (e) Pre-PR-#79 vs current auto-suspend logic

PR #79 (commit `7637ea3`, 2026-05-06) added the combined-gate `(max_drawdown ≤ hard_loss AND net_pnl < pnl_threshold)`. The first_signal suspension fired 4 days BEFORE PR #79 landed.

Current `scout/trading/auto_suspend.py:236-237`:
```python
fires_hard_loss = net_pnl <= hard_loss or (
    max_drawdown <= hard_loss and net_pnl < pnl_threshold
)
```

Settings:
- `SIGNAL_SUSPEND_HARD_LOSS_USD = -500.0`
- `SIGNAL_SUSPEND_PNL_THRESHOLD_USD = -200.0`
- `SIGNAL_SUSPEND_MIN_TRADES = 50` — gates only the `pnl_threshold` rule path; both arms of `fires_hard_loss` bypass it. Moot at n=256.

`max_drawdown` per `auto_suspend.py:86-94` is peak-to-trough drop in running cumulative PnL within the rolling window (NOT absolute trough). The reason field's `$-593` figure means cumulative PnL fell $593 from its running peak; subsequent intra-window recovery brought net to -$57.85 at suspend time.

## Decision: Option A REVIVE-AND-SOAK

| Option | Why not | Why this |
|---|---|---|
| A: REVIVE-AND-SOAK (**recommended**) | — | Suspension was pre-fix false-positive; revival is statistically safe; produces durable evidence either way |
| B: RETIRE in code | Premature without soak — the false-positive suspension prevented current-rules evaluation | — |
| C: DEFER until live-roadmap | Wastes the chance to gather post-fix data; dispatcher path stays live anyway | — |

**V42 SHOULD-FIX — argument independence:** the recommendation rests on FOUR arguments but they're not independent. (a) "suspension was a pre-fix false-positive" is the NECESSARY condition — if rejected (e.g., operator argues the pre-fix gate caught a real regression), Option A collapses to Option C. (b) positive-tail edge ($45/winner) and (d) current-gate-wouldn't-re-fire are re-slicings of the same all-time data. (c) drag-in-expired (tail-decay vs picks-losers) is the only genuinely independent observation; alone it is too thin to outweigh -$0.52 EV. Net: Option A requires (a) to hold; (b)–(d) are sufficiency conditions conditional on (a).

**V42 SHOULD-FIX — paper-slot opportunity cost:** Cycle 7 found `losers_contrarian` + `narrative_prediction` already consume ~48% of post-cutover paper volume at 0% live-eligibility. Reviving first_signal compounds the drag at expected n≈20 over 14d (~1.4 trades/day) — small relative to total paper throughput, but real. If the operator is paper-slot constrained, DEFER becomes relatively more attractive. For the current operator (PAPER_LIVE_ELIGIBLE_SLOTS unchanged, no slot pressure surfaced), this cost is acceptable.

**V42 SHOULD-FIX — KEEP threshold rationale:** "PnL ≥ 0 AND positive-tail win rate ≥ 17%" anchors on NON-REGRESSION. The null hypothesis under Option A is "the signal is its pre-suspend self" (because the suspension was a false-positive, not a regime change). Reject the null only if the soak shows the signal got WORSE; do not require it to spontaneously improve. The higher +$200 + avg-winner ≥ $30 bar is preserved as the SEPARATE escalation gate for live-roadmap revisit.

**V44 SHOULD-FIX — joint reliance on n=1 trade.** Cycle 7's V36 fold and cycle 9's "Tier 1b reachable in principle" both rest on the SAME single observation (trade id=1375, `conviction_locked_stack=3`). The structural argument has not strengthened over the cycle 7 → cycle 9 chain; only the empirical PnL evidence will at the 2026-05-31 verdict. Future-self should recognize that if the soak produces zero new stack≥3 trades, the cycle 7 + cycle 9 framing rests on a single observation in perpetuity.

## Operator action (post-merge, operator-manual)

Per V40 MUST-FIX: use the helper, NOT raw SQL. Per V41 SHOULD-FIX: stop service first to avoid `BEGIN EXCLUSIVE` racing with live writers.

```bash
ssh root@srilu-vps
cd /root/gecko-alpha   # required: get_settings() reads .env from cwd

sudo systemctl stop gecko-pipeline   # avoid BEGIN EXCLUSIVE race

/root/.local/bin/uv run python -c "
import asyncio
from scout.db import Database
from scout.config import get_settings
async def revive():
    db = Database('/root/gecko-alpha/scout.db')
    await db.connect()
    # operator='operator' (helper default) — cool-off filter at db.py:4113
    # matches the literal 'operator' string. Custom values silently bypass
    # BL-NEW-REVIVAL-COOLOFF for future revivals.
    await db.revive_signal_with_baseline(
        'first_signal',
        reason='cycle9 revive-and-soak — pre-PR-#79 false-positive; 14d soak ends 2026-05-31',
        operator='operator',
        settings=get_settings(),
    )
    await db.close()
asyncio.run(revive())
"

sudo systemctl start gecko-pipeline   # clears in-process signal_params cache; picks up enabled=1
# V43 SHOULD-FIX: if the revival python raises (.env mis-config, BEGIN EXCLUSIVE timeout,
# DB integrity check), STILL run `sudo systemctl start gecko-pipeline` before debugging
# — otherwise the service stays stopped until the 09:00 stale-heartbeat watchdog notices.
```

**V43 SHOULD-FIX — `systemctl stop` blast radius:** the ~3-5s window halts the narrative scanner, paper-trade evaluator, cohort_digest hook, and heartbeats. A position hitting TP/SL during the window is benign — the evaluator re-runs every cycle on resume; no checkpoint is lost. The only operator-facing artifact is one missed heartbeat event.

**V43 SHOULD-FIX — adding first_signal to TG alerts during soak:** the helper sets `tg_alert_eligible=0` (first_signal ∉ `DEFAULT_ALLOW_SIGNALS`). If the operator wants TG alerts on first_signal trades during the soak, run separately: `sqlite3 /root/gecko-alpha/scout.db "UPDATE signal_params SET tg_alert_eligible=1 WHERE signal_type='first_signal'"` + restart. (This is OUT-OF-SCOPE of the revival decision; the helper's default opt-out preserves existing operator preference.)

The helper:
- `BEGIN EXCLUSIVE` atomic transaction
- Sets `enabled=1`, `suspended_at=NULL`, `suspended_reason=NULL`, `drawdown_baseline_at=NOW()`, `updated_at=NOW()`, `updated_by='operator'`
- Sets `tg_alert_eligible=0` (first_signal ∉ `DEFAULT_ALLOW_SIGNALS`); logs `signal_revived_tg_eligible restored_to=0`
- Writes audit rows (`field_name='enabled'` old='0' new='1', `field_name='tg_alert_eligible'` old='0' new='0')
- Cool-off check returns no prior `applied_by='operator' AND new_value='1'` row → no check fires on this first revival

## Pre-registered re-evaluation criteria at 2026-05-31

**Expected fire-count at 14d:** pre-suspend rate was 5 trades in 3.5 days = 1.43/day → 14d expected n ≈ 20.

**V42 SHOULD-FIX — early-halt per CLAUDE.md §11c:** if `n ≥ 20` is reached BEFORE the 14d calendar boundary, evaluate immediately rather than running to calendar completion. The soak gate is data-bound on BOTH floor and ceiling.

| Observation post-revival | Verdict | Action |
|---|---|---|
| `n < 10` at 14d (data threshold not met) | EXTEND-SOAK to 28d (data-bound per §11) | Continue soak; re-evaluate at 2026-06-14 |
| `first_signal` auto-suspends again under current gate | RETIRE (Option B confirmed) | File `BL-NEW-FIRST-SIGNAL-RETIRE-CODE` |
| `n ≥ 10` AND `Cumulative PnL ≥ 0` AND `positive-tail win rate ≥ 17%` | KEEP-PAPER (validated research surface) | Close cycle 9 cleanly |
| `n ≥ 10` AND `Cumulative PnL ≥ +$200` AND `avg winner ≥ +$30` | KEEP-PAPER + flag for live-roadmap revisit | Memory note |
| `n ≥ 10` AND `Cumulative PnL < 0` BUT `positive-tail win rate ≥ 17%` | EXTEND-SOAK 14d (regime ambiguity) | File `BL-NEW-FIRST-SIGNAL-EXTEND-SOAK` |
| `n ≥ 10` AND `Cumulative PnL < 0` AND `positive-tail win rate < 17%` | RETIRE (Option B) | File `BL-NEW-FIRST-SIGNAL-RETIRE-CODE` |
| `n == 0` (zero-dispatch) | INVESTIGATE then RETIRE | File dispatcher-availability investigation |

KEEP threshold deliberately demands NON-REGRESSION (PnL ≥ 0 AND positive-tail win rate ≥ 17%), not spontaneous profitability. Per V38 SHOULD-FIX: +$200 threshold sits ~7σ above EV and would mechanically bias toward retirement; preserved here only as the higher escalation bar.

## Hermes-first verdict

No Hermes skill covers signal-revival decision frameworks. Project-internal trading decision. awesome-hermes 404 (consistent).

## Drift verdict

NET-NEW. No prior `tasks/findings_*first_signal*.md`. Cycle 7's findings doc (`findings_live_evaluable_signal_audit_2026_05_17.md`) Finding 3 raised the question but didn't drill cause. No parallel session touching first_signal per master HEAD inspection at `63aa13b`.
