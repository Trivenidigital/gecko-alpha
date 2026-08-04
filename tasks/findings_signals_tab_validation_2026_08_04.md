# Signals tab / Top Gainers Tracker — validation

**Date:** 2026-08-04
**Status:** ANALYSIS COMPLETE — no code changed, nothing disabled.
**Trigger:** operator on the Signals tab: *"There are lot of good signals system
producing, not sure whether these are from suspended categories or not."*

---

## Answers to the two questions asked

**1. Are these from suspended categories?** **No — and they are unaffected by
suspension.** `scout/gainers/tracker.py` contains **zero** references to
`signal_params` or `enabled`. The Signals tab is a **detection** surface; the
suspended categories are **paper-trade dispatch** lanes. They are different
subsystems reading different tables. Detection kept producing rows through the
entire 7-day period when the paper pipeline was dead.

The `DETECTED BY` column ("Pipeline", "Narrative", "Early Signal") lists
*detector surfaces*, not the signal categories (`gainers_early`, `first_signal`,
…). The two vocabularies overlap in name only.

**2. Are the signals good?** **The detections are real. The headline metrics are
not evidence that they are.** All three lead metrics would read the same if the
system had no predictive skill at all.

---

## Why the headline numbers do not discriminate

### `GAINERS HIT RATE 1077/1097 (98.2%)` — near-tautological

The endpoint's own docstring (`dashboard/api.py:1021`) says it plainly:

> **"RETROSPECTIVE, not a pre-pump watchlist: every row is a coin that has
> ALREADY appeared on the +20%/24h gainers tracker."**

- **Denominator** = coins that already pumped *and* entered our tracker (1,097).
- **Numerator** = coins we had seen at any prior moment (1,077).

Coins that pumped and we never ingested are not in the denominator at all. With a
broad ingest corpus and an unbounded lookback, this metric reads ~98% whether or
not the system is good. **Ask what it would read if the system had zero skill:
the same.**

### `AVG LEAD 393.5h` — corpus membership, not prediction

`scout/gainers/tracker.py:128`:

```sql
SELECT MIN(detected_at) FROM {table}
 WHERE coin_id = ? AND datetime(detected_at) < datetime(?, '+5 minutes')
```

`MIN(detected_at)` with **no lower bound** — the earliest sighting ever. Lead =
pump time − first-ever appearance in any surface.

Distribution across all 1,097 rows (max lead across surfaces):

| lead | n | share |
|---|---|---|
| < 1h | 51 | 4.6% |
| 1–24h | 144 | 13.1% |
| 1–7d | 153 | 13.9% |
| 7–30d | 308 | 28.1% |
| **> 30d** | **441** | **40.2%** |

Mean **751.3h (31 days)**, max **2,788.8h (116 days)**.

**40% of "early detections" are more than a month early.** CATE's "2677h early"
does not mean the system predicted a pump 111 days out — it means CATE has been
sitting in the corpus for 111 days. A coin ingested once and never acted on
scores an enormous lead the moment it eventually moves.

### `GAIN SINCE DETECTION +3431.7%` — measured from an unreachable price

`detected_price` is the price at that same earliest-ever sighting. So the gain is
measured from a price that may be months old and was never a tradeable entry.

Notional vs actual, same cohort:

| measure | value |
|---|---|
| avg `peak_gain_pct` (from detected_price) | **+92.8%** |
| avg `peak_pct` on real trades (from entry) | **+14.7%** |

A 6.3× gap. The notional figure is not a missed opportunity — it is largely a
baseline artifact, the same class of error as the `pct_from_entry` defect in
PR #503, in the opposite direction.

---

## What was actually realized on these coins

1,855 closed paper trades on coins that appear in `gainers_comparisons`:

| | |
|---|---|
| realized net | **−$8,073.57** |
| avg per trade | −1.4% |
| win rate | 48.1% (893 / 1,855) |
| avg peak after entry | +14.7% |

**Top 10 detections by notional peak gain:**

| symbol | notional peak | trades | realized |
|---|---|---|---|
| ANSEM | +8,519% | 2 | **−$45.03** |
| LAB | +4,916% | 17 | +$294.37 |
| VELVET | +2,250% | 8 | −$28.15 |
| BEAT | +2,249% | 3 | +$14.86 |
| SYN | +2,085% | 2 | −$69.97 |
| BANK | +1,745% | 5 | +$91.14 |
| JOHN | +1,474% | **0** | — |
| $1 | +1,324% | **0** | — |
| SPCTROLL | +1,085% | **0** | — |
| KINS | +1,074% | 1 | −$32.00 |

Net across the top 10: **+$225**. Three of the ten were **never traded at all**
despite moving >1,000%.

The dashboard's own `OUTCOME` column already tells this story honestly —
ANSEM `+3431.7%` gain-since-detection carries `Actionable · $-6.96`. The headline
row and the outcome badge disagree by three orders of magnitude, and the outcome
badge is the truthful one.

---

## New defect found: symbol collision on the display

Four tracker rows share ANSEM-like identities:

| coin_id | displayed symbol | peak gain |
|---|---|---|
| `ansem-s-cat` | HOBBES | +16.2% |
| `ansem-s-minutes` | **ANSEM** | +591.2% |
| `the-black-bull` | **ANSEM** | **+8,519.2%** |
| `ansem-cat` | ANSEMCAT | +2.0% |

**Two distinct `coin_id`s display as "ANSEM".** The headline +8,519% row is
`the-black-bull`, not the coin a reader would assume. The dashboard renders
`symbol`, which is not unique. Severity **MEDIUM** — it misattributes the single
most impressive number on the page.

---

## How this relates to the recent work

| Finding | Relationship |
|---|---|
| PR #504 — exit mechanics | **Corroborated and quantified.** 7/7 signals positive on managed exits, negative on unmanaged. This surface independently shows detection is not the constraint: +92.8% average notional peak, −$8,073 realized. |
| PR #503 — board entry basis | **Sibling defect, same class.** There, `pct_from_entry` was measured against a near-current price, making everything look fresh. Here, gain and lead are measured against an unbounded-lookback price, making everything look early. Both are baseline-choice errors that flatter the system. |
| `gainers_early` revival (2026-08-03) | Unaffected — detection never stopped. The revival restarts *dispatch*, which is what was dead. |

The consistent theme across all three: **the system's detection is genuinely
broad, and every metric that makes it look predictive is measured from a baseline
that guarantees a flattering answer.**

---

## What I am NOT claiming

- **Not** that detection is worthless. 51 rows have sub-1h leads and 144 more are
  1–24h; those are real. ANSEM at 1h early before a 34× move is a genuine catch.
- **Not** that the tracker is lying. Its docstring says "RETROSPECTIVE" and its
  OUTCOME column shows the losses. The surface is honest; the **headline
  aggregates** are the problem.
- **Not** that these numbers were fabricated or that anyone misused them.

---

## Recommended next steps (none executed)

1. **Bound the lead-time lookback.** A lead computed from `MIN(detected_at)` with
   no floor cannot distinguish prediction from corpus membership. A 7-day (or
   24h) cap would make the metric mean what the header claims.
2. **Split the hit rate.** Report "detected < 24h before" separately from
   "detected ever". The former is a skill measure; the latter is coverage.
3. **Show `coin_id`, not just `symbol`,** or disambiguate collisions — the
   headline row is currently misattributed.
4. **Lead with the OUTCOME column.** It is the only figure on the page that
   survived this audit intact.

## Evidence limitations

- Realized PnL is joined on `paper_trades.token_id = gainers_comparisons.coin_id`.
  Coins traded under a different id form would be missed, so −$8,073 is a lower
  bound on coverage, not necessarily on loss.
- Dashboard reports `AVG LEAD 393.5h`; my recomputation over all rows gives
  751.3h. The difference is likely an aggregate/filter choice in the endpoint that
  I did not trace. Both are far above any actionable horizon, so the conclusion is
  unchanged — but the exact figure is unreconciled.
- No claim is made about detector precision (how many detections *don't* pump);
  `gainers_comparisons` only contains coins that already pumped, so that rate is
  not computable from this table.
