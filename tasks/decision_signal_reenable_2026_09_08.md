**New primitives introduced:** NONE (the recommendation is to build nothing yet)

# Re-enable decision: volume_spike, chain_completed, first_signal

**Date:** 2026-09-08 · **Prepared against:** prod `6c56186e` · **Decision owner:** operator

## Recommendation

**KEEP ALL THREE DISABLED.** And separately, and more importantly:

> **Do not re-enable any suppressed signal through the parole retest as it is
> currently configured.** A 5-trade retest cannot distinguish any of these
> signals from zero, and 2 wins out of 5 is enough to CLEAR a suppression that
> was earned on 20–185 trades. Re-enabling under that mechanism does not test
> the signal; it launders a coin-flip into a "recovered" verdict.

That second point holds regardless of what you decide about these three, and it
is the finding I would act on first.

## Why each was disabled — this was not a policy choice

All three were killed by the **automated `hard_loss` gate**, on realised losses.
Two had already been revived once by an operator and failed again.

| signal | killed | loss at kill | n | prior |
|---|---|---|---|---|
| volume_spike | 2026-07-18 | −$554, dd −$686 | 35 | killed 05-29 (−$252, n=66); revived 06-22; **re-failed in 3.5 wks** |
| first_signal | 2026-06-29 | −$597, dd −$597 | 19 | killed 05-02 (dd −$593, n=253); revived 05-31; **re-failed in 4 wks** |
| chain_completed | 2026-06-06 | **−$1,714, dd −$2,856** | 144 | never revived |

**Both prior revivals performed WORSE than the signal's own lifetime baseline:**

| signal | lifetime avg/trade | revival-window avg | revival n |
|---|---|---|---|
| volume_spike | −2.25% | **−5.28%** | 35 |
| first_signal | −0.64% | **−9.80%** | 21 |

## Evidence

Lifetime, closed trades, bootstrap 10,000 resamples (seed 20260908):

| signal | n | mean | 95% CI | recent-60d mean | recent CI |
|---|---|---|---|---|---|
| chain_completed | 185 | −5.44% | **[−10.01, −0.00]** excludes 0 | −5.44% | [−9.90, +0.24] |
| first_signal | 277 | −0.64% | [−2.16, +0.95] straddles | **−8.58%** | **[−15.55, −1.75]** excludes 0 |
| volume_spike | 130 | −2.25% | [−5.72, +2.13] straddles | −4.75% | [−8.81, +0.68] |

**`first_signal`'s lifetime number is misleading.** −0.64% with a CI straddling
zero reads as harmless; its recent window is −8.58% with a CI that excludes
zero. The lifetime average is dominated by 254 near-flat April trades and hides
a deterioration. Judge it on the recent window.

**No profitable slice exists.** Per-chain isolation is vacuous — every trade in
all three signals is `chain='coingecko'`, a single corpus. Per-month:

| signal | Apr | May | Jun | Jul |
|---|---|---|---|---|
| chain_completed | — | −4.94% (148) | −7.42% (37) | — |
| first_signal | +0.04% (254) | +8.91% (2) | −9.80% (21) | — |
| volume_spike | −2.79% (10) | −0.94% (85) | −3.33% (4) | −5.53% (31) |

Every signal is negative in every month with usable n. The only non-negative
cells are `first_signal` April (+0.04%, net −$186 — flat, not profitable) and
`first_signal` May (n=2, meaningless).

**Detection is not the constraint.** Over 3 days: chain_completed 1,821 events,
first_signal 919, volume_spike 47. Re-enabling `chain_completed` would not be a
slow soak — it would trade immediately and often.

## The structural finding: the retest cannot answer the question

Per-trade return SD is large relative to the effect. A 5-trade parole retest
yields:

| signal | per-trade SD | 5-trade 95% CI | effect being measured | verdict |
|---|---|---|---|---|
| chain_completed | 35.4% | **±31.1%** | −5.44% | cannot separate from 0 |
| first_signal | 13.2% | **±11.5%** | −0.64% | cannot separate from 0 |
| volume_spike | 22.6% | **±19.8%** | −2.25% | cannot separate from 0 |

To resolve ±2 percentage points:

| signal | trades needed | days at its own historical rate |
|---|---|---|
| chain_completed | 1,206 | ~218 |
| first_signal | 167 | **~41** |
| volume_spike | 489 | ~301 |

**`first_signal` is the only one where a decisive answer is reachable in a
reasonable window** — and its evidence already points negative.

### And the retest can clear on noise

`combo_refresh.py`: **suppress** requires `trades >= 20` AND `WR < 30%`.
**Clear** requires 5 valid retest trades AND `WR >= 30%` over the 30-day
window. For a signal dormant for months that window contains *only* the retest
trades — so **2 wins out of 5 (40%) clears it.**

A suppression earned on 185 trades can be lifted by two lucky trades. That is
not a defect I introduced here; it is live today and it is the reason the first
recommendation above is unconditional.

## What I would do instead

1. **Leave all three disabled.** No action, no code.
2. **Ticket the clearance asymmetry.** Clear should require evidence of
   comparable weight to suppress — at minimum the same `min_trades` floor.
   Until then no parole retest produces a trustworthy verdict for any signal.
3. **If you ever want a real answer**, it needs a counterfactual lane that
   accumulates outcomes for a disabled signal *without* trading it. This does
   not exist: `shadow_trades` requires a `paper_trade_id`, so it shadows real
   paper trades against venue depth and cannot stand in for a disabled signal.
   That would be new work and I am not proposing it now — the expected value is
   low given every month of every signal is negative.

## If you decide to re-enable anyway — pre-registered criteria

Register these BEFORE flipping anything, or the result is unfalsifiable.

- **Candidate:** `first_signal` only. It is the sole signal where a decisive
  answer is reachable (~167 trades / ~41 days) and its per-trade SD (13.2%) is
  less than half the others'.
- **Do NOT rely on the parole retest to judge it.** Flip
  `signal_params.enabled=1` and evaluate on the n below, independently of
  whether `combo_refresh` clears or re-suppresses the combo.
- **Gate:** n ≥ 167 closed trades, judged on a bootstrap CI of mean pnl_pct.
  - **PROMOTE** only if the 95% CI lower bound is > 0.
  - **KILL** if the CI upper bound is < 0, or on any `hard_loss` trip.
  - **INSUFFICIENT_DATA** otherwise — hold disabled, do not read as pass.
- **Hard stop:** abort immediately if cumulative net reaches −$600 (its own
  prior kill threshold) regardless of n.
- **Do not re-enable `chain_completed`.** Highest detection rate (1,821/3d),
  largest realised loss (−$3,018), CI excludes zero, negative in every month,
  and 218 days to resolve. It is the worst risk/return of the three by every
  axis measured.

## What this does not claim

- Not "the exits are the problem." Exit-reason mix is **outcome-conditioned** —
  a trade's exit reason is determined by how it went, so comparing exit reasons
  across signals is selection, not treatment. (`closed_peak_fade` is positive in
  all three; that is not evidence that forcing peak_fade would help.)
- Not "these signals can never work." Two of three have lifetime CIs that
  straddle zero. The claim is narrower: **the current evidence does not support
  re-enabling, and the current retest mechanism cannot generate evidence that
  would.**
- Paper P&L is the only outcome measured here. No live-money conclusion is drawn.
