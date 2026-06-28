# Findings — Same-asset under-gate cohort (F1, clean-join rebuild)

**Date:** 2026-06-28
**Status:** READ-ONLY. No prod thresholds changed. No code shipped. No paid API enabled.
**Supersedes the fuzzy-join numerator in:** `findings_slow_burn_under_gate_backtest_2026_06_28.md`
**Companion spec:** `spec_holder_growth_unphantoming_and_slow_burn_scoring_2026_06_28.md`
**Operator constraints honored:** same-asset joins only (no fuzzy names as primary evidence);
Solana/Base/copycat/duplicate artifacts separated; alert-ceiling design targets soft 15 / hard 20
gate-candidates/day.

---

## 0. TL;DR — the clean join reframes the whole problem

1. **Same-asset, no fuzzy names:** the only clean key available is **CG-native** rows, where
   `score_history.contract_address` **is** the CoinGecko `coin_id`. Joined to coin_id-keyed mcap
   surfaces, the clean cohort is **672 coins**.
2. **In the cleanly-measurable corpus, big winners barely exist.** Only **3 of 672** ran ≥10×
   (bitads 49.9×, the-black-bull/ANSEM 21×, main-street-usd 11.5×) — **all scored ≤18.** Over the
   80-day `predictions` cross-check, the **max tracked move was +91% (<2×); zero ran 10×.** The CG
   corpus is a poor hunting ground: by the time a token is CG-listed/trending, the move is mostly done.
3. **The real upside lives in a corpus we CANNOT cleanly measure.** ANSEM measured at the **CG stage
   = 21×** ($4.3M entry); at the **DEX stage = 477×** ($190K entry). That **22× gap is the
   early-catch premium** — and it sits in the **DEX-mint corpus (pump.fun, etc.) that has no durable
   `contract↔coin_id` link**, so its outcomes are unmeasurable without fuzzy name joins.
4. **Only threshold-only is retrospectively quantifiable; it looks weak.** Candidate-volume-by-
   threshold is clean (below). But the winner-yield it would buy is ~zero in the measurable corpus,
   and the holder-proxy and age/vol-liq counterfactuals **cannot be simulated retrospectively** (the
   data to do so was never retained).
5. **Therefore the next step is instrumentation, not lever selection.** A defensible gate
   recalibration or lane decision is **blocked** until we durably store (a) a `contract↔coin_id`
   resolver and (b) entry-mcap snapshots for DEX rows. This is the real F1 verdict.

---

## 1. Data model & why a full 30-day DEX cohort is not constructible

| Table | Key | mcap? | Retention | Role |
|---|---|---|---|---|
| `score_history` | contract_address | no | **21d** (06-07→) | scorer spine (2.66M rows) |
| `candidates` | contract_address | yes | **7d** (pruned) | only source of DEX entry mcap |
| `gainers_snapshots` | coin_id | yes | 7d | CG outcome |
| `momentum_7d` | coin_id | yes | 30d (sparse, 338) | CG outcome |
| `conviction_watchlist_snapshots` | coin_id | yes | 9d | CG outcome |
| `predictions` | coin_id | yes (+peak_change_pct) | **80d** (1,873 outcomes) | CG outcome (narrative layer) |
| `trending_snapshots` | coin_id | **no** (rank only) | 30d | not usable for mcap |

**Three structural blockers to a clean 30-day DEX cohort:**
- **B1 — no `contract↔coin_id` map.** DEX rows are on-chain mints; outcome surfaces are coin_id-keyed.
  No durable resolver links them (resolver_cache/symbol_aliases are symbol-level; chain_matches is
  not a map). The ANSEM link only worked via token-name — the exact fuzzy join the operator excluded.
- **B2 — DEX entry mcap is pruned to 7d.** `score_history` has no mcap; `candidates` (the only DEX
  mcap source) is pruned. For any DEX contract scored >7d ago, entry mcap is unrecoverable.
- **B3 — `trending_snapshots` (the one 30d coin_id surface) carries no mcap.**

**Consequence:** the clean same-asset cohort is **CG-native only**, over the 21-day score window
(outcome cross-checked to 80d via `predictions`). The DEX corpus — where ANSEM-class upside lives —
is **out of reach of clean measurement today.**

---

## 2. Candidate volume by threshold (clean; all 2,664 scored contracts, 21d)

Peak score per contract, `score_history`, 2026-06-07→06-28:

| Threshold | Contracts (≥T) | **gate candidates/day** | vs operator ceiling (soft 15 / hard 20) |
|---:|---:|---:|---|
| **65 (current)** | 1 | **0.05** | gate effectively never fires (1 in 21 days) |
| 55 | 8 | 0.4 | — |
| 50 | 33 | 1.6 | well within ceiling; catches ANSEM (peak 50) |
| **40** | 276 | **13** | **within soft ceiling — leading candidate** |
| 25 | 707 | 34 | **exceeds hard 20 → watchlist/soak only** |
| 10 | 1,695 | 81 | far over ceiling |

**Reading:** the operator's soft-15/day ceiling lands the gate at **~40** (≈13/day). Threshold **50**
(≈1.6/day) is the most conservative setting that still catches an ANSEM-stage-50 peak. Anything ≤25
(≥34/day) breaks the hard ceiling and must route to watchlist/soak, not direct alerts.

*Caveat:* "candidates/day" = distinct contracts whose peak ever crossed T, ÷21. With 24h dedup this
approximates distinct tokens/day; true per-cycle gate entries are higher but de-duplicated downstream.

---

## 3. Winner / fizzle by threshold (clean CG-native same-asset, n=672)

Outcome = `max(market_cap)/min(market_cap)` across coin_id-keyed surfaces (gainers + momentum +
watchlist). Score band = peak `score_history` score.

| Band | n | ran ≥10× | ≥25× | ≥50× | ran 2–10× | fizzle (<2×) |
|---|---:|---:|---:|---:|---:|---:|
| 0–9 | 210 | 0 | 0 | 0 | 7 | 203 |
| 10–24 | 450 | **3** | 1 | 0 | 31 | 416 |
| 25–39 | 10 | 0 | 0 | 0 | 0 | 10 |
| 40–49 | 1 | 0 | 0 | 0 | 0 | 1 |
| 50+ | 0 | — | — | — | — | — |

**The 3 clean ≥10× winners (all scored ≤18):**

| coin_id | peak score | entry mcap | peak mcap | multiple |
|---|---:|---:|---:|---:|
| bitads | 10 | $411K | $20.5M | 49.9× |
| the-black-bull (ANSEM) | 10 | $4.31M | $90.7M | 21.0× |
| main-street-usd | 18 | $6.71M | $77.3M | 11.5× |

**Two hard facts:**
- **No CG-native coin scored ≥50** (high scores come from the DEX/Solana corpus, see §4). The clean
  winners scored 10–18 — score-rank carries essentially no precision in the measurable corpus.
- **80-day `predictions` cross-check (n=392 scorer∩prediction coins): max peak +91% (<2×), ZERO
  10×.** Independent confirmation that the CG corpus rarely multi-baggers.

**Precision by band (clean):** ≈3/450 in the only band with winners (10–24) ≈ **0.7%**, and
**0%** everywhere else. Lowering the gate to admit these bands buys almost no winners *in the corpus
we can measure.*

---

## 4. Separation of Solana / Base / copycat / duplicate (per operator)

- **CG-native (clean, §3):** 672 coins. The only same-asset-joinable corpus. Low scores, low winner rate.
- **DEX (Solana/Base mints):** the **high scorers live here** — of 33 contracts with peak ≥50, the
  overwhelming majority are Solana (solana_bonus + vol_liq + buy_pressure stack). **Outcomes
  unmeasurable** (B1/B2). This is the ANSEM corpus.
- **Copycat / duplicate:** the fuzzy-join artifacts that inflated the first pass to "6 winners"
  (tensor = Base $18K copycat ≠ Solana TNSR; myro/return-to-memes/catwifhat = low-mcap Solana dupes
  of large CG tokens) **do not appear in this clean cohort at all** — the coin_id join structurally
  excludes them. This is the methodological win: same-asset joins drop the contamination.

**Measurement-point thesis (the core insight):** the same asset yields wildly different multiples by
where it is measured. ANSEM = **21× at CG-stage entry ($4.3M)** vs **477× at DEX-stage entry
($190K)**. The prize is the early DEX entry — precisely the measurement point we cannot currently
join to outcomes.

---

## 5. Counterfactual — threshold-only vs holder-proxy vs age/vol-liq retune

| Lever | Retrospectively quantifiable? | Verdict on available evidence |
|---|---|---|
| **Threshold-only** | ✅ yes (§2 volume, §3 yield) | **Weak.** Buys volume (13/day at 40) for ~0.7% precision in the measurable corpus; admits mostly DEX tokens whose outcomes we can't yet validate. Catches ANSEM only at DEX-stage peak 50, or as a *late* 21× at CG-stage. |
| **Holder-proxy (DexScreener buy-txn growth)** | ❌ **No** — proxy data was never collected; cannot re-derive `txns_h1_buys` deltas historically | Hypothesis intact; **requires forward instrumentation to evaluate.** |
| **Age / vol-liq retune** | ❌ **No** — `candidates` (token state) pruned to 7d; cannot re-score historical tokens under new thresholds | Hypothesis intact; **requires forward instrumentation to evaluate.** |

**Meta-finding:** only the *weakest* lever (threshold-only) is measurable today, and the two
promising levers cannot be A/B'd against history because the inputs were never retained. **Choosing
a lever now would be guessing.**

---

## 6. Acceptance-criteria check (operator's gate for this finding)

| Criterion | Status |
|---|---|
| Same-asset joins only | ✅ coin_id↔coin_id; fuzzy names excluded; DEX limitation documented (B1) |
| 30-day denominator | ⚠️ **Partial** — 21d score-side, up to 80d outcome-side (predictions); a clean **30-day DEX** cohort is **not constructible** (B1–B3), which is itself the headline finding |
| Candidate volume by threshold | ✅ §2 |
| Winner count by threshold | ✅ §3 (3 total ≥10×, all ≤score 18) |
| FP / fizzle by threshold | ✅ §3 (fizzle 619/671; precision ≈0.7% best band) |
| Gate candidates/day per setting | ✅ §2 (T40≈13/day within soft ceiling; T25≈34/day exceeds hard 20) |
| Counterfactual threshold/proxy/retune | ✅ §5 — only threshold-only is retro-measurable |

---

## 7. Verdict & recommended next step (read-only → instrumentation, still no thresholds)

**The gate is unreachable (1 alert in 21 days), but lowering it is not yet justified by evidence:**
the cleanly-measurable corpus barely produces winners, and the corpus that does (early DEX) is
unmeasurable. **Do not recalibrate the gate or pick a lever on current data.**

**Prerequisite instrumentation (read-only/observe-only builds; no threshold or alert change):**
- **I1 — durable `contract↔coin_id` resolver.** Persist the CG `/coins/{id}.platforms` mapping
  (DEX mint → coin_id) at ingest so DEX early-catches can later be joined to coin_id outcomes
  without fuzzy names. *Removes blocker B1.*
- **I2 — entry-mcap retention.** Snapshot first-seen mcap per contract into a non-pruned table
  (or extend `score_history` with mcap). *Removes blocker B2.*
- **I3 — the free buy-txn proxy** (`txns_h1_buys` snapshots, per companion spec Track 1) — both a
  candidate signal AND the data needed to evaluate the holder-proxy counterfactual.

Once I1–I3 have accumulated ~2–4 weeks, re-run this cohort on the DEX corpus to get the **real**
numerator/denominator, then evaluate Track 3 (gate at ~40, ≈13/day, within the soft ceiling) and the
proxy/retune counterfactuals against actual DEX outcomes. Per operator rule, any setting that admits
>20/day to the gate routes to **watchlist/soak, not direct alerting.**

This composes with global CLAUDE.md §12a: the inability to run this analysis is itself a
pipeline-instrumentation gap (tables shipped without the retention/linkage needed to validate them).

---

## 8. Caveats

- **C1:** clean cohort is CG-native only; conclusions about the DEX corpus are by *inference*
  (it holds the high scorers and the one verified early winner) pending I1–I3.
- **C2:** entry mcap = min observed on a CG surface = a post-discovery floor, so §3 multiples are
  *lower bounds* (they understate true early-stage moves — the ANSEM 21× vs 477× gap quantifies this).
- **C3:** `predictions` cross-check covers coins evaluated by the narrative layer (sub-$30M
  watchlist), a subset; it corroborates but does not exhaust the CG corpus.
