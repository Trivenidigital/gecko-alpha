**New primitives introduced:** NONE (the recommendation is to build nothing yet)

# Re-enable decision: volume_spike, chain_completed, first_signal

**Date:** 2026-09-08 · **Prepared against:** prod `6c56186e` · **Decision owner:** operator

## RULING (operator, 2026-09-08) — BINDING

**`chain_completed`, `first_signal` and `volume_spike` remain DISABLED.**
**Do not use the current 5-trade parole retest to revive them.**

The supported conclusion, stated in the form the operator ruled on:

> **CURRENT EVIDENCE DOES NOT SUPPORT RE-ENABLEMENT, AND THE CURRENT PAROLE
> MECHANISM CANNOT GENERATE RELIABLE EVIDENCE OF RECOVERY.**

Explicitly NOT claimed: that these signals can never work.

The `first_signal` design below is preserved as a **pre-registered future
experimental design only**. It is not authorised, and its sample-size and
duration figures must be re-derived immediately before any future run rather
than treated as timeless constants.

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

The kill-row figures are the gate's own numbers **at the moment it fired**
(`signal_params_audit.reason`), over the window it evaluated. They are not the
same quantity as the lifetime totals in the Evidence section — `chain_completed`
shows −$1,714 over n=144 at its kill and −$3,018 over n=185 lifetime, because
41 further trades closed after the gate tripped. Both are correct; they answer
different questions. Cite the lifetime figure for economics and the kill figure
for what the gate saw.

**Both prior revivals performed WORSE than the signal's own lifetime baseline:**

| signal | lifetime avg/trade | revival-window avg | revival n |
|---|---|---|---|
| volume_spike | −2.25% | **−5.28%** | 35 |
| first_signal | −0.64% | **−9.80%** | 21 |

## Evidence

Regenerate everything below with
`uv run python scripts/analyze_signal_reenable_evidence.py` (committed).
Percentile bootstrap, 10,000 resamples, stdlib `random.Random` seeded **per
call**, rows ordered by `id`. Population: `status LIKE 'closed%' AND pnl_pct
IS NOT NULL`. Data provenance: `max(closed_at) = 2026-08-11T02:38:00Z`.

### Lifetime

| signal | n | mean | 95% CI | net | WR |
|---|---|---|---|---|---|
| chain_completed | 185 | −5.44% | [−10.01, **−0.00**] | −$3,018 | 50.3% |
| first_signal | 277 | −0.64% | [−2.13, +0.95] straddles 0 | −$750 | 40.1% |
| volume_spike | 130 | −2.25% | [−5.85, +1.98] straddles 0 | −$877 | 37.7% |

**`chain_completed`'s CI does not meaningfully exclude zero.** Its upper bound
is −0.00 — sitting *on* the boundary, not clear of it. An earlier revision of
this document claimed "excludes 0" as a headline; that claim was an artifact of
a bootstrap bug (one seeded RNG reused across two loops, so the same sample
produced two different intervals). Corrected. **The case against
`chain_completed` rests on economics — −$3,018 net, −5.44%/trade, negative in
every month, highest throughput — not on interval significance.**

### Recent window — the last 60 days of EACH SIGNAL'S OWN LIFE

Not a common calendar window. The three cover different periods and different
fractions of each signal's span, and are **not comparable to one another**:

| signal | n | mean | 95% CI | window covered |
|---|---|---|---|---|
| chain_completed | 185 | −5.44% | [−10.01, −0.00] | 2026-04-05 .. 06-04 (**its entire 33-day life**) |
| first_signal | 25 | **−8.58%** | **[−15.40, −1.72]** excludes 0 | 2026-04-29 .. 06-28 |
| volume_spike | 90 | −4.75% | [−8.84, +0.70] straddles 0 | 2026-05-15 .. 07-14 |

`chain_completed`'s "recent" row is the same 185 trades as its lifetime row —
its span is shorter than the window. It is listed for completeness, not as a
second observation.

**`first_signal`'s lifetime number is misleading, and this is the one place a
window choice carries a conclusion.** −0.64% lifetime (CI straddling zero)
reads as harmless; the last 60 days of its life are −8.58% on n=25 with a CI
that excludes zero. The lifetime mean is dominated by 254 near-flat April
trades. This is the only signal where lifetime and recent give opposite
verdicts, and the recent window is the decision-relevant one because it
contains the post-revival cohort — but n=25 is small and the reader should
weigh it as such.

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

**Detection is not the constraint.** journald, `gecko-pipeline`, the 3 days to
2026-09-08: chain_completed 1,821 events, first_signal 919, volume_spike 47.

Three caveats that the figure does not carry on its face. These are **detections
on signals that are DISABLED** — detection continues regardless of the trading
gate, so they are an upper bound on what re-enabling would admit, not a trade
rate. journald retention on this box is ~17 days, so this figure becomes
**unverifiable after ~2026-09-25** and cannot be re-derived later. And the
conversion rate from detection to admitted trade is not measured here. The
supported inference is only the weak one: nothing about detection volume would
make a re-enabled `chain_completed` a slow soak.

## The structural finding: the retest cannot answer the question

Per-trade return SD is large relative to the effect.

**Read the numbers below as order-of-magnitude, not as measurements.** They are
normal-approximation intervals. At five observations that approximation is the
*optimistic* end: a small-sample t interval is ~1.42× wider, exact widths are
highly unstable, and a five-observation bootstrap is not something that can
certify recovery at all. The conclusion "five trades cannot discriminate" is
therefore **sound and probably understated** — but no decision should rest on
the specific figure ±31.1% rather than ±44.0%.

| signal | per-trade SD | 5-trade ± (normal) | 5-trade ± (t, df=4) | effect | verdict |
|---|---|---|---|---|---|
| chain_completed | 35.4% | ±31.1% | **±44.0%** | −5.44% | cannot separate from 0 |
| first_signal | 13.2% | ±11.5% | **±16.3%** | −0.64% | cannot separate from 0 |
| volume_spike | 22.6% | ±19.8% | **±28.0%** | −2.25% | cannot separate from 0 |

The qualitative fact carrying the decision is that the interval half-width
exceeds the effect by roughly 3–25×, under either method. No refinement of the
interval arithmetic changes that.

To resolve ±2 percentage points (same caveat — an estimate from observed
variance, not a constant):

| signal | trades needed | days at its own historical rate |
|---|---|---|
| chain_completed | 1,206 | ~218 |
| first_signal | 167 | **~41** |
| volume_spike | 489 | ~301 |

**`first_signal` is the only one where a decisive answer is reachable in a
reasonable window** — and its evidence already points negative.

**167 and ~41 days are NOT constants.** 167 derives from variance observed in a
cohort that ended 2026-06-28; ~41 days derives from that signal's historical
arrival rate while it was enabled. Both must be re-derived at the moment any
prospective experiment is actually considered, not carried forward from this
document. Treat them as an existence proof that a discriminating sample is
reachable in principle, nothing more.

## Selection axes — do not collapse these into one lifetime mean

Every headline here has an axis along which it was selected. Stated explicitly
so a later reader does not reduce this to "the average was negative":

| axis | what it changes |
|---|---|
| **lifetime vs recent** | `first_signal` is −0.64% lifetime (CI straddles 0) and −8.58% recent (CI excludes 0). Opposite conclusions from the same signal. |
| **signal age** | Active spans differ 2.4×: chain_completed 33d, first_signal 68d, volume_spike 80d. Per-signal "lifetime" covers unequal calendar exposure. |
| **sample size** | n = 185 / 277 / 130. `volume_spike`'s straddling CI is partly a power statement, not an innocence statement. |
| **prior revival cohort** | volume_spike's 35 post-revival trades and first_signal's 21 are a *selected* cohort — trades taken after an operator judged the signal worth reviving. They are the most decision-relevant subset and the worst-performing one. |
| **event throughput** | 3-day detection: 1,821 / 919 / 47. Identical per-trade economics imply very different bleed rates. |
| **outcome maturity / censoring** | Checked against an **unfiltered** denominator (`COUNT(*)` with no status clause): 185/185, 277/277, 130/130, zero open, zero other. So the closed-trade filter removed nothing — the check is not the tautology it would be if the denominator had been filtered too. **But it bounds POST-OPEN censoring only.** It cannot see candidates filtered before a trade was ever opened, so it does not rule out selection at admission. Narrower claim than the first revision made. |

## The retest can also CLEAR on noise

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

## FROZEN research design — NOT a green light

**Operator ruling 2026-09-08: do not execute this now.** It is preserved
because `first_signal` is the only signal the present analysis says could reach
a discriminating sample in a practical period — not because it is approved.
Activation requires a separate, explicit decision.

Before any future activation, ALL of the following must be redone rather than
inherited from this document:

- re-estimate the sample requirement from **current** variance (167 is stale the
  moment the distribution moves);
- re-derive expected calendar duration from the **then-current** arrival rate
  (~41 days assumes a rate last observed in June);
- preregister the exact outcome metric and CI method;
- preregister censoring / maturity handling;
- freeze cohort identity before the first trade;
- freeze the hard stop;
- no threshold changes after outcomes are observed;
- a separate activation decision, recorded.

The design below is the shape such an experiment would take.

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
- **`chain_completed` is not a candidate for such a run** from the present
  evidence. Highest detection rate (1,821/3d),
  largest realised loss (−$3,018 lifetime), −5.44%/trade, negative in every month,
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
