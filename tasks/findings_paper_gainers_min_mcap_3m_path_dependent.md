**New primitives introduced:** NONE (analysis output, no code primitives)

# findings: path-dependent backtest for $3M floor proposal (PR #83 follow-up)

| Field | Value |
|---|---|
| Triggered by | PR #83 reviewer finding STAT-C1: aggregate-based simulation methodologically unsound |
| Author | claude (session 2026-05-08) |
| Script | `scripts/backtest_paper_gainers_min_mcap.py` (committed alongside this finding) |
| Conclusion | Proposal is ~2× weaker than originally projected; bootstrap CI on annual PnL straddles zero |

## 1. Methodology fix

Original backtest in `tasks/plan_paper_gainers_min_mcap_3m.md` §3 used
`MAX(price)` and `MIN(price)` aggregates over the 168h window — no temporal
ordering. Reviewer flagged that a coin dropping −25% before peaking +75%
would have SL'd in live, but the aggregate banked the peak.

The new script walks forward through prod price snapshots
(`gainers_snapshots ∪ volume_history_cg`, ~1400 rows per coin avg) in
chronological order and applies the BL-061 ladder cascade exactly as
`scout/trading/evaluator.py` does:

`SL → Leg 1 → Leg 2 → Floor → Trail`

Critical fix vs initial path-dependent draft: trail does NOT fire before
leg_1 fills. Pre-leg_1, only SL or max_duration can close the position.
Live evaluator gates the trail on `floor_armed AND peak_pct >= leg_1_pct`
(evaluator.py:548–551).

## 2. Per-coin outcomes (path-dependent, corrected)

| coin_id | entry_pct | entry_mc_M | peak_pct | min_pre_peak | status | exit_pct | gross_pnl@$300 |
|---|---|---|---|---|---|---|---|
| sentio | 27.0 | 4.9 | **27.5** | -3.2 | trail | +8.69 | +$26 |
| unc | 31.3 | 4.2 | 17.6 | -4.0 | trail | +9.24 | +$28 |
| sleepless-ai | 22.5 | 3.0 | 9.1 | -11.9 | max_duration | -5.13 | -$15 |
| space-4 | 20.4 | 3.8 | 6.9 | 0.0 | max_duration | +5.75 | +$17 |
| chronobank | 49.2 | 4.4 | 2.8 | 0.0 | max_duration | +2.77 | +$8 |
| goblincoin | 20.7 | 4.6 | 12.2 | -14.1 | trail | +5.48 | +$16 |
| moby-ai | 48.3 | 3.2 | 7.6 | -13.2 | max_duration | -10.95 | -$33 |
| obol-2 | 44.2 | 3.8 | 0.0 | -0.1 | max_duration | -0.12 | -$0 |
| ben-pasternak (BELIEVE) | 48.5 | 5.0 | 13.0 | 0.0 | trail | +7.43 | +$22 |
| seamless-protocol | 46.4 | 4.4 | 7.8 | -0.5 | max_duration | +1.30 | +$4 |
| rei-network | 20.3 | 3.3 | 0.5 | -18.1 | max_duration | -11.33 | -$34 |
| **nietzschean-penguin (PENGUIN)** | 46.2 | 3.4 | **75.9** | -3.5 | trail | **+30.99** | **+$93** |
| evaa-protocol | 21.5 | 4.8 | 19.5 | -3.1 | trail | +10.58 | +$32 |
| hooked-protocol | 35.7 | 3.1 | 0.2 | -16.6 | max_duration | -12.68 | -$38 |
| panther | 21.3 | 4.2 | 0.0 | -8.2 | max_duration | -7.53 | -$23 |
| lock-in | 45.3 | 3.4 | 18.0 | -7.1 | max_duration | +10.90 | +$33 |

## 3. Aggregate stats (path-dependent vs original aggregate-based)

| Metric | Original (broken) | Path-dependent (correct) | Delta |
|---|---|---|---|
| Strike rate (peak ≥ 20%) | 38% (6/16) | **12.5%** (2/16) | **−68%** |
| SL hits | 0% | 0% | (same) |
| Negative gross PnL count | ~6% (1/16) | 37.5% (6/16) | +6× |
| Mean PnL/trade @ $300 | ~+$20 | **+$8.51** | **−57%** |
| Total PnL/30d @ $300 | +$315 | **+$136** | **−57%** |
| Annualized | +$3,830 | **+$1,657** | **−57%** |

### Honest CIs (the original spec's "±15pp" was off)

- **Strike rate Wilson 95% CI:** [3.5%, 36.0%] — lower bound is far below the spec's 25% promotion gate.
- **Total PnL bootstrap 95% CI** (10,000 resamples): [−$105, +$398] / 30d.
- **Annualized bootstrap 95% CI:** [−$1,279, +$4,846] / yr.

The bootstrap interval **straddles zero**, meaning we cannot statistically
distinguish "modestly profitable" from "modestly loss-making" at n=16.

## 4. Why the original was wrong — GOBLIN walk-through

Original aggregate said: GOBLIN entry $0.00468, peak in window $0.00808 = +72.85%, gross +$80 at $300.

Path-dependent reality:
- Entry 16:11 May 2 at $0.00468
- Drops to −14% within the first hour (no SL — under −25% threshold)
- Crosses +10% at 16:55 → leg_1 fills, 50% closed at +10% gain
- Peaks at +12.24% at 17:12
- Drops to +0.96% at 17:18 — drawdown 11.28% from peak; with peak < 20%, the low-peak trail fires at 8% drawdown → runner closed at +0.96%
- Realized: 50% × +10% + 50% × +0.96% = **+5.48%** ($16, not $80)
- The actual +73% peak that the aggregate captured happened **4 days later** on May 6 — the position had already closed.

The aggregate assumed the trade rode from entry to peak. It didn't. The
trail closes early, and small-cap CG-listed pumps in this MC band show
oscillatory behavior that triggers the 8% low-peak trail before the big
move develops.

## 5. Implications for the proposal

### 5a. PENGUIN is load-bearing

Of the +$136 total realized PnL, **PENGUIN alone contributes +$93 (68%)**. Strip PENGUIN:
- Total: +$43 / 30d
- Mean: +$2.88 / trade
- Annualized: +$523 / yr

PENGUIN was the only coin where peak reached the leg_2 threshold (+50%)
within the 168h window with a smooth-enough path to capture meaningful
trail. The proposal's economics depend on a once-per-month PENGUIN-class
coin.

### 5b. Strike-rate gate fails immediately

Spec §6.1 promotion criterion: strike rate ≥ 25%. Backtest = 12.5%. Wilson
CI lower bound 3.5%. **At the proposed promotion threshold, the proposal
cannot pass even on the existing data — let alone after a noisy n=14 forward soak.**

### 5c. Mean PnL gate is fragile

Spec §6.1: mean PnL/trade > $0. Backtest = +$8.51. Without PENGUIN: +$2.88.
A single SL hit during soak (which didn't happen in this 30d window but
has positive base-rate at $3M MC) would push mean negative.

### 5d. Forward soak cannot meaningfully discriminate

At n=7 (spec sample gate) with true mean $2.88 and trade-level σ ~$31
(standard deviation of the per-coin gross PnL excluding PENGUIN), the
standard error of the mean at n=7 is σ/√n ≈ $11.7. A 95% CI on observed
mean would be roughly ±$23 — several times wider than the gate. **n=7
cannot distinguish $0 from $5; we'd need n ≥ 100 to detect the projected
$8.51/trade with 80% power.**

## 6. Conclusion

The proposal is **not statistically supported** by the existing 30d data.
Three honest options:

### Option A — PARK permanently

Treat PR #83 as a planning artifact. Document in MEMORY that lowering
PAPER_GAINERS_MIN_MCAP from $5M to $3M is **not justified** by the
current ladder + 30d cohort. Re-eligibility criteria:
- New ladder configuration (e.g., tighter trail, longer floor) materially changes the simulated economics; or
- A 60-day cohort produces n ≥ 30 candidates where mean PnL > +$10 with bootstrap LB > 0; or
- Operator manually identifies 3+ specific recent missed pumps the floor
  would have recovered, demonstrating the projection underestimates real-
  world catches.

### Option B — Reduce scope to PENGUIN-class only

Add a per-coin filter: only fire `gainers_early` at $3M floor when the
coin shows additional confirming signals (e.g., volume_acceleration ≥ 5×,
or chain_completed pattern). This trades cohort size for hit-rate. Needs
its own backtest.

### Option C — Do nothing here, pursue CEX feed

The deferred CEX feed addresses USDUC/FOREST-class (post-pump CG indexing)
which has a stronger projection floor than this MC tweak. If we have one
"cheap-win-first" budget to spend, spend it there.

## 7. My recommendation

**Option A — PARK permanently.** The path-dependent rerun did its job:
the original projection was wrong by ~2×, the strike-rate gate fails on
existing data, and the forward soak cannot statistically distinguish the
proposal from a coin flip on PnL. PR #83 stays closed. Memory is updated
with the correct simulation methodology for any future MC-floor proposal,
so this analysis is not re-litigated from scratch.

The mechanism (`PAPER_GAINERS_MIN_MCAP` knob) is still clean — it can be
revived later if a future proposal has stronger evidence. The branch
`feat/paper-min-mcap-3m` stays preserved for that contingency.

## 8. Reproducibility

To re-run:

```bash
# Refresh CSVs from prod (run on your dev machine)
ssh srilu-vps 'sqlite3 -separator "," /root/gecko-alpha/scout.db "WITH e AS ..."' > .candidates.csv
ssh srilu-vps 'sqlite3 -separator "," /root/gecko-alpha/scout.db "WITH e AS ... gainers_snapshots ..."' > .snaps_g.csv
ssh srilu-vps 'sqlite3 -separator "," /root/gecko-alpha/scout.db "WITH e AS ... volume_history_cg ..."' > .snaps_v.csv

# Run the simulation
uv run python scripts/backtest_paper_gainers_min_mcap.py
```

Full SQL queries documented in the script header. Ladder params (LEG_1_PCT,
TRAIL_PCT, etc.) at top of script — update if prod `signal_params` row for
gainers_early changes.
