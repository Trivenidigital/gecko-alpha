# 6h-checkpoint cut — analysis and proposal

**New primitives introduced:** NONE. Uses the deployed `paper_trades.checkpoint_6h_pct`
column and existing evaluator exit machinery.

**Date:** 2026-08-04
**Status:** ANALYSIS COMPLETE, NOT IMPLEMENTED. Needs a decision before any code.
**Supersedes as critical path:** the MAE/stop-width route in
`findings_signal_suspensions_are_exit_mechanics_2026_08_03.md` (PR #504).

---

## Why this exists

The operator asked whether determining all eight suspended categories requires
waiting two weeks for the `gainers_early` instrumentation run. It does not, and
the premise had two flaws worth recording:

1. **That run only ever covered `gainers_early`.** It was never going to produce
   evidence for the other seven. This was not stated when the run was proposed —
   a real omission.
2. **MAE informs stop *width*, which is the smaller lever.** The larger question
   is whether to still be in a trade at all.

`paper_trades.checkpoint_6h_pct` already exists across **1,866 historical closed
trades spanning every suspended category**, so the larger question is answerable
today with no new data.

MAE reconstruction from history was tested and is **dead**: only **12 of 2,122**
closed trades (0.6%) have a price path in `gainers_snapshots`.

## The finding — the 6h sign separates profit from loss in 8 of 8 signals

| Signal | n neg@6h | net | n pos@6h | net |
|---|---|---|---|---|
| gainers_early | 374 | −$6,882 | 186 | +$3,203 |
| chain_completed | 122 | −$4,261 | 55 | +$1,460 |
| losers_contrarian | 177 | −$2,991 | 130 | +$2,291 |
| first_signal | 135 | −$2,339 | 118 | +$1,348 |
| narrative_prediction | 177 | −$2,080 | 130 | +$1,273 |
| volume_spike | 79 | −$1,663 | 33 | +$1,133 |
| trending_catch | 52 | −$641 | 51 | +$276 |
| slow_burn | 19 | −$468 | 12 | +$207 |
| **Total** | **1,135** | **−$21,325** | **715** | **+$11,191** |

No exceptions. And positions negative at 6h are held an average of **88.7 hours**
(max 720) — the signal arrives at hour 6, the position is carried 3½ more days.

By band, the relationship is monotone:

| 6h reading | n | avg final peak | % never reaching +10% peak | net |
|---|---|---|---|---|
| < −10% | 138 | 7.6 | **75.4%** | −$4,941 |
| −10…0% | 817 | 8.9 | **64.3%** | −$8,751 |
| 0…10% | 585 | 12.3 | 48.7% | +$2,928 |
| ≥ +10% | 134 | **47.1** | **0.0%** | +$8,316 |

## *** THE HEADLINE NUMBER IS NOT THE DECISION NUMBER ***

**−$21,325 is not what a 6h cut saves.** Cutting at 6h realizes the loss *as it
stands at 6h* — not zero. The honest two-sided counterfactual, using
`amount_usd × checkpoint_6h_pct/100` as the realized-at-6h value:

| | |
|---|---|
| Trades negative at 6h | 1,147 |
| Actual net on them | −$21,831 |
| Counterfactual if cut at 6h | −$16,961 |
| **Net improvement** | **+$4,869** |

And the cost side is large: **358 of those 1,147 (31%) recover into winners.**
The rule gives up **$16,079** of profit to avoid **$20,948** of loss — a **1.30:1**
ratio, not a free win.

This is recorded prominently because an earlier verbal summary in-session quoted
the −$21,325 figure as if it were the saving. It is not, and the difference is
4.4×. Same failure mode as the stop-width backtest this document's predecessor
warned about: the measurable side looked decisive, and the unmeasured side was
the one that mattered.

### Threshold sweep — no clean sweet spot

| rule | n cut | net gain | winners cut | profit lost | loss saved | ratio |
|---|---|---|---|---|---|---|
| 6h < 0% | 1,147 | **+$4,869** | 358 | $16,079 | $20,948 | 1.30 |
| 6h < −5% | 436 | +$897 | 103 | $7,007 | $7,904 | 1.13 |
| 6h < −10% | 188 | +$463 | 31 | $2,171 | $2,634 | 1.21 |
| 6h < −15% | 69 | +$483 | 5 | $361 | $844 | **2.34** |

The aggressive rule captures the most dollars but is nearly indiscriminate; the
conservative rule discriminates well and barely moves the needle. The middle
thresholds are worse in absolute terms than either end. **There is no threshold
that is both high-yield and high-precision.**

For scale: lifetime system loss is ≈ **−$11,396**. The best variant recovers
**+$4,869** — meaningful (≈43%) but it does **not** make the system profitable.

## Caveats that must be closed before implementation

1. **The checkpoint is not reliably written.** 256 of 2,122 closed trades (12%)
   have no 6h reading — *including trades held an average of 26.1 hours*, which
   is far past 6h. The write is `if cp_6h is None and elapsed >= 6h` inside the
   evaluator loop, after the stale-price/no-price guards, so a tick that skips on
   price unavailability never records it. Any cut rule must treat "no reading" as
   an explicit third state, not as "not negative". Those 256 trades are
   themselves net **−$781**.
2. **"6h" is really "first evaluator tick at or after 6h."** Causally valid (no
   lookahead) but the drift is bounded by evaluator cadence, which I could not
   establish from config. Unmeasured.
3. **Selection effect.** The rule can only act on trades that survive 6 hours.
   Trades closing sooner (avg hold 26.1h cohort aside) are outside its reach.
4. **In-sample.** Every number here is fitted on the same history it would be
   evaluated against. The 1.30:1 ratio is an upper bound on what forward
   performance would look like.

## Recommendation

**Do not implement the 6h cut on this evidence alone.** It is net positive but
thin, in-sample, and it discards a third of its targets as false positives.

Instead, in order:

1. **Fix the checkpoint write** (caveat 1). A 12% miss rate on the column any
   such rule depends on is a defect independent of whether the rule ships, and it
   silently biases every analysis built on it.
2. **Let the `gainers_early` run produce out-of-sample data.** It is already
   running, free, and now serves a sharper purpose than stop width: it will show
   whether the 6h relationship holds forward. It is no longer the critical path,
   but it is the validation set.
3. **Re-run this sweep on the forward cohort** before committing to a threshold.
4. **Stop treating stop width as the next lever.** MAE is worth having, but on
   these numbers the 6h relationship is a stronger signal and neither is
   transformative on its own.

## What this does NOT establish

- It does not determine the eight suspended categories individually. It shows one
  relationship that holds across all of them.
- It does not identify *why* 941 trades never rally — the entry-selection question
  from PR #504 remains open, and the entry features currently captured (mcap,
  liquidity, age, entry run-up) were all tested and none discriminate.
- It does not make the system profitable. The largest honest improvement found is
  +$4,869 against a −$11,396 lifetime loss.
