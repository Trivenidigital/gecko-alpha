# Why every signal is suspended — it is the exit path, not the signals

**Date:** 2026-08-03
**Status:** ANALYSIS COMPLETE — no code changed, nothing revived.
**Trigger:** operator on the signal-evidence board: *"Lot of these categories used
produce lot of good signals. Why all these are suspended?"*

---

## Verdict in one line

**Every suspension was justified on its own evidence, but the cause is common to all
signals: the exit path loses money uniformly, so each signal crosses the PnL
threshold on schedule regardless of detection quality.** 7 of 7 signals are net
positive on managed exits and net negative on unmanaged ones.

**Operational consequence: reviving any signal without changing exits will simply
re-suspend it.**

---

## 1. The suspensions are real, not spurious

All auto-suspend except `tg_social` (operator). Each fired on genuine rolling-window
losses (`signal_params_audit`):

| Signal | Reason | Evidence at kill | When |
|---|---|---|---|
| chain_completed | hard_loss | −$1,714, dd −$2,856, n=144 | 2026-06-06 |
| gainers_early | pnl_threshold | −$204, n=129 | 2026-07-27 |
| losers_contrarian | hard_loss | −$208, dd −$1,412, n=121 | 2026-05-17 |
| slow_burn | hard_loss | −$544, dd −$578, n=32 | 2026-06-17 |
| trending_catch | hard_loss | −$317, dd −$626, n=108 | 2026-05-11 |
| volume_spike | hard_loss | −$554, dd −$686, n=35 | 2026-07-18 |
| first_signal | hard_loss | −$597, dd −$597, n=19 | 2026-06-29 |
| tg_social | **operator** GA-01 containment | −$488.93 / 19 priced closes, 33% WR | 2026-07-03 |

Thresholds (`scout/config.py:1816-1818`): `PNL_THRESHOLD −$200` (requires
`MIN_TRADES 50`), `HARD_LOSS −$500` (**bypasses the trade floor by design**). That
bypass is why `first_signal` was killed at n=19 — working as specified, but a thin
sample.

**The `—` columns on the board are not a defect.** Those signals have been off for
weeks, so their 7/14/30d closed-paper windows are genuinely empty.

## 2. The cause is structural — managed vs unmanaged exits

Split every close into **managed** (`closed_peak_fade`, `closed_trailing_stop`,
`closed_moonshot_trail`, `closed_tp`, `closed_floor`) vs **unmanaged**
(`closed_stop_loss`, `closed_expired`, `closed_time_death`):

| Signal | n managed | net managed | n unmanaged | net unmanaged |
|---|---|---|---|---|
| gainers_early | 342 | **+$10,300** | 330 | −$13,776 |
| chain_completed | 98 | **+$4,718** | 87 | −$7,736 |
| losers_contrarian | 130 | **+$5,368** | 200 | −$6,501 |
| narrative_prediction | 82 | **+$3,332** | 243 | −$4,161 |
| first_signal | 53 | **+$2,243** | 224 | −$2,992 |
| volume_spike | 48 | **+$1,954** | 78 | −$2,844 |
| trending_catch | 21 | **+$695** | 92 | −$1,252 |
| **Total** | **774** | **+$28,610** | **1,054** | **−$39,262** |

**7/7 positive managed, 7/7 negative unmanaged.** If detection were the problem,
managed exits would lose money too. They do not. This corroborates the earlier
trader-persona review finding ("peak_fade only profit lane") on a wider basis.

## 3. Where the money actually goes

| Exit reason | n | net | avg pnl_pct |
|---|---|---|---|
| **stop_loss** | 342 | **−$26,667** | **−26.5%** |
| expired | 753 | −$11,873 | −5.4% |
| expired_stale_price | 129 | −$1,204 | −3.1% |
| expired_stale_no_price | 14 | **$0** | 0.0% |

Two things this rules out:

- **Fabricated closes are not the cause.** `expired_stale_no_price` is n=14 at
  exactly $0 (closed at entry price by design). The dashboard's exclusion notice is
  honest and the excluded volume is negligible.
- **Stops are not gapping.** Realized ≈ configured per signal (gainers_early
  configured 25.0 → realized −25.4; chain_completed 30.0 → −31.5). 319 of 342
  stop-outs land in the −20 to −40 band. The stop is doing exactly what it is set
  to do.

Note `signal_params.sl_pct` is **25–30%** per signal, while `PAPER_SL_PCT` defaults
to **15.0** (`scout/config.py:720`, itself widened from 10.0 under BL-061). The
table overrides the default upward.

## 4. The arithmetic that makes every signal breakeven-negative

gainers_early, lifetime: 55.3% win × +12.6% avg win − 44.7% × −15.6% avg loss
≈ **0.00**. Its observed `avg_realized_pct` is **0.0**.

Winners ~+12–17%, stop-outs −25%: a payoff ratio near 0.5, which needs ~67% win rate
to break even. No signal comes close.

**Authoritative unfiltered lifetime figures** (use these, not any `peak_pct`-filtered
subset — see the correction in §"Correction made mid-analysis"):

| Signal | n | win % | lifetime net | per trade |
|---|---|---|---|---|
| long_hold | 14 | 57.1 | +$13 | +$0.93 |
| tg_social | 24 | 16.7 | −$475 | −$19.77 |
| trending_catch | 113 | 40.7 | −$557 | −$4.93 |
| slow_burn | 39 | 35.9 | −$572 | −$14.68 |
| first_signal | 277 | 40.1 | −$750 | −$2.71 |
| **narrative_prediction** | 325 | 43.7 | **−$829** | −$2.55 |
| volume_spike | 130 | 37.7 | −$877 | −$6.74 |
| losers_contrarian | 330 | 48.8 | −$1,133 | −$3.43 |
| chain_completed | 185 | 50.3 | −$3,018 | −$16.32 |
| gainers_early | 673 | 50.7 | −$3,198 | −$4.75 |

**Every signal is lifetime negative** except `long_hold` at n=14 (+$13 — noise).
Total −$11,396 across 2,110 closes. Note this includes **`narrative_prediction`, the
only signal currently ENABLED**, at −$829 / 43.7%. The live configuration is losing
money too; this is not a situation where good signals are being unfairly held back.

The `peak_pct`-filtered subset overstated every win rate by roughly 5pp
(gainers_early 55.3 → 50.7, narrative_prediction 54.6 → 43.7), which is what made
several signals look near-viable when none are.

Exit-status mix across all 2,110 closes: `expired` 896 (**42%**), managed 800 (38%),
`stop_loss` 342 (16%), `time_death` 67 (3%). **The expired bucket is larger than the
managed-exit bucket.**

**Giveback**, avg peak vs avg realized (subset with `peak_pct` present):

| Signal | avg peak % | avg realized % | giveback pp |
|---|---|---|---|
| long_hold | 22.1 | 3.4 | 18.6 |
| chain_completed | 15.3 | −2.5 | 17.8 |
| slow_burn | 13.2 | −4.0 | 17.1 |
| gainers_early | 16.9 | 0.0 | 16.9 |
| volume_spike | 14.4 | −0.7 | 15.1 |
| losers_contrarian | 13.2 | 0.4 | 12.8 |
| trending_catch | 10.0 | −0.6 | 10.6 |
| first_signal | 10.6 | 0.6 | 10.0 |
| narrative_prediction | 8.8 | 0.6 | 8.2 |

Every signal reaches a usable peak and keeps ~none of it.

## 5. This is the same story as the board entry-basis defect

See `findings_gainers_board_entry_basis_2026_08_03.md` (PR #503). Entering a token
after it has already run — which the board actively promotes, since run-up is scored
as a positive with no ceiling — produces exactly this profile: little upside left
after entry, then a −25% stop. The two findings are the same problem observed from
the entry side and the exit side.

---

## What to evaluate next (nothing here is a recommendation to revive)

Ordered by strength of evidence:

1. **Instrument adverse excursion (MAE) — first, and blocking.** One column on
   `paper_trades`, updated on the same evaluator tick that already maintains
   `peak_pct`. Additive, no behaviour change, accumulates immediately. **This is a
   prerequisite for any stop-width work** — see the correction below.
2. **The `expired` bucket — the largest and answerable today.** 896 trades (**42% of
   all closes**, larger than the managed-exit bucket), −$11,873 at −5.4%, timing out
   inside `PAPER_MAX_DURATION_HOURS=48`. Two explanations with opposite fixes:
   (a) entries into tokens that were never going to move → fix entry criteria;
   (b) 48h too short for the thesis → extend duration. Distinguishable **now** from
   entry-context data without MAE.
3. **Stop width — blocked on (1).** 25–30% against +12–17% winners is a ~0.5 payoff
   ratio. But it cannot be evaluated on existing data (correction below).
4. **Peak capture.** +16.9% reached, 0.0% kept. Known constraint: per memory
   `project_session_2026_05_05_high_peak_park`, per-signal `trail_pct` is dominated
   by the global `MOONSHOT_TRAIL=30` floor at peak ≥40%, so a `trail_pct` change may
   not reach the lever (§9c). Verify before scoping.

### CORRECTION (2026-08-03, same day) — the stop-width backtest is NOT runnable

An earlier revision of this document listed the stop-width backtest as the cheap
first move, "no forward soak needed." **That was wrong.** `paper_trades` carries
`peak_pct` but **no adverse-excursion / trough / MAE column**, and there is no
per-trade price path anywhere (`paper_trade_entry_snapshots` is entry-context only,
n=543; `price_cache` is current-value only; `gainers_snapshots` covers only
gainers-tracked coins and only from the point tracking began).

Consequence, and the reason this matters more than a normal erratum:

- For the 342 rows that closed at `stop_loss` we know they reached −25%, so the
  **saving** from a tighter stop is computable.
- For the other 1,768 closes we cannot know which dipped below −15% en route, so the
  **cost** — trades a tighter stop would newly convert into losses — is *not*
  computable.

A backtest on this data would therefore produce a **one-sided estimate that makes
tightening the stop look strictly beneficial.** Do not run it and do not act on any
number produced that way. Instrument MAE first.

Same family as [[feedback-evidence-that-does-not-discriminate]]: the available half
of the evidence points confidently in one direction, and the missing half is the
half that would argue against.

**Do not revive a signal as a first move — "which one to revive" is the wrong
question.** Every signal is lifetime negative (§4), including the one already
enabled. The auto-suspend measures net PnL, which is dominated by an exit path
common to every signal, so a revived signal re-crosses the threshold on schedule.
There is no good signal being unfairly held back. Fix the mechanics, then revive
with a fresh drawdown baseline.

**What would change the ordering above:** if MAE data (item 1) shows most trades
never approach −25%, the stop is nearly irrelevant and the problem is entry quality
plus expiry — which promotes item 2 above item 3 permanently and de-scopes stop work
entirely.

---

## Exact repro

```sql
-- why each is suspended, with the evidence string
SELECT signal_type, field_name, old_value, new_value, reason, applied_by, applied_at
FROM signal_params_audit WHERE field_name='enabled' ORDER BY applied_at DESC;

-- managed vs unmanaged exits per signal
SELECT signal_type,
 SUM(CASE WHEN status IN ('closed_peak_fade','closed_trailing_stop',
     'closed_moonshot_trail','closed_tp','closed_floor') THEN 1 ELSE 0 END) n_managed,
 ROUND(SUM(CASE WHEN status IN ('closed_peak_fade','closed_trailing_stop',
     'closed_moonshot_trail','closed_tp','closed_floor') THEN pnl_usd ELSE 0 END),0) net_managed,
 SUM(CASE WHEN status IN ('closed_expired','closed_sl','closed_time_death') THEN 1 ELSE 0 END) n_unmanaged,
 ROUND(SUM(CASE WHEN status IN ('closed_expired','closed_sl','closed_time_death') THEN pnl_usd ELSE 0 END),0) net_unmanaged
FROM paper_trades WHERE status LIKE 'closed%' AND pnl_usd IS NOT NULL
GROUP BY signal_type HAVING (n_managed+n_unmanaged) >= 50 ORDER BY net_unmanaged;

-- where the money goes
SELECT status, COALESCE(exit_reason,'(null)') er, COUNT(*) n,
       ROUND(SUM(pnl_usd),0) net, ROUND(AVG(pnl_pct),1) avg_pct
FROM paper_trades WHERE status IN ('closed_expired','closed_sl') AND pnl_usd IS NOT NULL
GROUP BY status, er ORDER BY status, net;

-- configured vs realized stop
SELECT p.signal_type, sp.sl_pct configured, COUNT(*) n,
       ROUND(AVG(p.pnl_pct),1) realized_avg, ROUND(MIN(p.pnl_pct),1) worst
FROM paper_trades p LEFT JOIN signal_params sp ON sp.signal_type=p.signal_type
WHERE p.status='closed_sl' AND p.pnl_pct IS NOT NULL GROUP BY p.signal_type;
```

## Correction made mid-analysis — read this before trusting any subset

I first computed per-signal lifetime PnL on rows filtered by `peak_pct IS NOT NULL`,
and got **first_signal +$238** and **losers_contrarian +$228** — which would have
argued for immediate revival of two suspended signals. Both are wrong. That filter
silently drops `closed_sl` / `closed_expired` rows (7–25 per signal) which carry the
losses. Unfiltered: **first_signal −$749.61 (n=277)**, **losers_contrarian
−$1,132.86 (n=330)**.

`peak_pct IS NULL` correlates with the worst outcomes, so filtering on it is not
neutral — it is a selection on the dependent variable. Use it only for peak-vs-
realized giveback (where the column is required), never for PnL totals.

Same family as `feedback_evidence_that_does_not_discriminate`: the filtered number
was plausible, quotable, and pointed the opposite way from the truth.

## Not verified in this session

- Whether `closed_expired` positions were unresolvable (illiquid / no price) or
  simply flat. Would change whether the fix is "cut earlier" or "do not enter."
- Whether the giveback is recoverable in practice — `MOONSHOT_TRAIL=30` may dominate
  any `trail_pct` change (§9c). Verify the lever is reachable before scoping.
- Fee/slippage share of the gap between `avg_realized_pct ≈ 0` and `net < 0`.
