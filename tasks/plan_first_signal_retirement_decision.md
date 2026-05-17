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

The `max_drawdown = -$593` figure means the cohort dipped TO -$593 at some peak-loss point, even though net was -$57.85 at suspend time (a substantial intra-window recovery).

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

**Conclusion:** first_signal's suspension is an artifact of pre-fix auto-suspend over-aggression, NOT a current-rules violation.

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

### Task 3 (deferred to operator): execute revival

```sql
-- Operator-only, NOT part of this PR. After PR merge:
ssh root@srilu-vps
sqlite3 /root/gecko-alpha/scout.db <<EOF
-- Revival via the helper (post-PR-#79 path) — drawdown baseline reset to now.
UPDATE signal_params SET
    enabled = 1,
    suspended_at = NULL,
    suspended_reason = NULL,
    drawdown_baseline_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    updated_by = 'operator_manual_revival_per_cycle9_decision'
WHERE signal_type = 'first_signal';

INSERT INTO signal_params_audit
    (signal_type, field_name, old_value, new_value, reason, applied_by, applied_at)
VALUES
    ('first_signal', 'enabled', '0', '1',
     'cycle9 revive-and-soak — pre-PR-#79 false-positive; 14d soak ends 2026-05-31',
     'operator_manual',
     strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
EOF
sudo systemctl restart gecko-pipeline  # pick up new signal_params row
```

(Operator runs this manually; not auto-applied by the PR.)

## Pre-registered re-evaluation criteria at 2026-05-31

| Observation over 14d post-revival | Verdict | Action |
|---|---|---|
| `first_signal` auto-suspends again | RETIRE (Option B confirmed) | File `BL-NEW-FIRST-SIGNAL-RETIRE-CODE` |
| Cumulative PnL ≥ +$200 OR ≥ 50 trades with net positive | KEEP-PAPER (validated research surface) | Close cycle 9 cleanly |
| Cumulative PnL between -$200 and +$200 (noisy) | EXTEND-SOAK 14d | File `BL-NEW-FIRST-SIGNAL-EXTEND-SOAK` |
| No trades fire (zero-dispatch in 14d) | RETIRE (Option B) — dispatcher broken or candidates absent | File investigation |

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
