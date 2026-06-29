# Findings — Slow-burn / under-gate backtest (ANSEM precision-denominator study)

**Date:** 2026-06-28
**Author:** investigation triggered by "why didn't we catch ANSEM early?"
**Status:** READ-ONLY analysis. No prod thresholds changed. No code shipped.
**Companion spec:** `spec_holder_growth_unphantoming_and_slow_burn_scoring_2026_06_28.md`

---

## 0. TL;DR

1. **The alert gate is currently unreachable.** In the 7-day window (2026-06-21 → 06-28),
   across **1,606 scored contracts**, the **maximum score any token ever reached was 59**
   (DUMPSTR). Only **2 contracts ever exceeded 55**. **Zero reached `MIN_SCORE=65`.**
   Therefore **0 alerts fired** and the conviction gate **never executed once** all week
   (0 candidates with a non-null `conviction_score`).
2. **ANSEM was not a fluke miss — but the "6 winners" numerator is inflated by bad joins.**
   The raw name-join surfaced 6 under-gate tokens that ran ≥10×. Spot-check (per operator
   instruction) confirms **only 1 is unambiguous (ANSEM, 477×)** and **1 probable (Bitads,
   50×)**; the other 4 are **copycat / CG-vs-DEX duplicate name-join artifacts.**
3. **No single lever catches ANSEM.** Even adding the full 25-pt `holder_growth` signal to
   ANSEM's *best-ever* score (50) lifts it only to ~63 — still below 65. Catching it requires
   **gate recalibration AND signal repair**, not one or the other.
4. **A blanket threshold drop is a poor fix.** To catch ANSEM (peak 50) you must drop the gate
   to ≤50, which fires **~16 alerts/day at ~1–2% precision** (1–2 verified winners/week against
   ~112 alerts/week). The two highest-scoring tokens of the week (DUMPSTR 59, Frontdeploy 55)
   were **not** winners — score-rank ≠ outcome at these levels.

**The evidence does NOT yet justify shipping a scorer retune.** It justifies (a) recalibrating
the unreachable gate to the realized distribution, (b) un-phantoming the dead Solana accumulation
signal via a free proxy, and (c) collecting a longer, cleaner-join window before committing to a
dedicated slow-burn lane.

---

## 1. Window & data sources

| Source | Rows | Coverage |
|---|---|---|
| `candidates` | 1,449 | 2026-06-21T18:20 → 06-28T18:37 (≈7 days; older rows pruned) |
| `score_history` | — | peak-score per contract, same window: **1,606 distinct contracts** |
| `gainers_snapshots` | 34,376 | 2026-06-21T01:45 → 06-28T18:38 (outcome/peak-mcap source) |

**Methodology note:** "ran N×" = `max(gainers_snapshots.market_cap) / min(candidates.market_cap_usd)`
joined on `lower(token_name)`/`lower(name)`. The join is **fuzzy** (name-based) — §5 documents the
contamination it introduces. Score bands use **peak score per contract from `score_history`**
(authoritative) except where noted as last-write `candidates.quant_score`.

---

## 2. The unreachable-gate finding (authoritative)

Peak score per contract over the window:

| Peak-score band | # contracts |
|---|---:|
| 0–9   | 648 |
| 10–24 | 675 |
| 25–39 | 169 |
| 40–54 | 112 |
| 55–64 | **2** |
| **65+ (alertable)** | **0** |
| **Total** | **1,606** |

- **Max peak score all week = 59.0** (DUMPSTR, Solana, mcap $34.8K).
- 2nd highest = 55.0 (Frontdeploy, Solana, mcap $47.7K).
- **`MIN_SCORE=65` sits 6 points above the entire realized distribution.**

**Cross-checks (both confirm zero alerts were structurally possible):**
- `alerts` table: **0 rows** since 2026-06-21.
- `candidates` with non-null `conviction_score`: **0** → the gate loop (`main.py:1198`, entered only
  when `points >= MIN_SCORE`, `main.py:1172`) never ran for a single token.

This is the single most important finding: **the system did not "rank ANSEM poorly and alert on
better things." It alerted on nothing.** Every token — winner and dud alike — is locked out by a
gate calibrated above the achievable score ceiling for this corpus.

---

## 3. Why the ceiling is so low (root cause, from the ANSEM trace)

A pure Solana-DEX memecoin can only fire a subset of the 11 signals. The big CoinGecko signals
(`vol_acceleration` 25, `cg_trending_rank` 15) require CG enrichment a DEX row lacks, and
`holder_growth` (25) is dead (§4). The normalization denominator is `SCORER_MAX_RAW=193` — which
assumes signals the corpus structurally cannot reach. Net effect: the realized ceiling is ~59, the
gate is 65, and the gap is unbridgeable without either repairing signals or recalibrating the gate.

ANSEM's own trajectory: peaked at **50** on 06-19 (≈83 raw pts: vol_liq_ratio + token_age + mcap +
buy_pressure + solana_bonus while it was still young), then decayed to **13** by the 06-24 $190K
sighting (token_age cliff zeroed once >7d; vol_liq_ratio fell below 5.0×).

---

## 4. Confirmed phantom: Solana `holder_growth` is structurally dead

- `holder_count > 0` on Solana candidate rows: **0 of 491.**
- Prod `.env`: `HELIUS_API_KEY=` and `MORALIS_API_KEY=` both **empty**.
- `holder_enricher.py:33–39` short-circuits when the key is empty → no write to `holder_snapshots`
  → `holder_growth_1h` never exceeds 0 → the 25-pt signal at `scorer.py:98` **can never fire for
  any Solana token.**

**Impact estimate (ANSEM):** adding the full +25 raw `holder_growth` to ANSEM's best-observed score
of 50 yields ~63 normalized — **still below 65.** Holder repair is *necessary but not sufficient*
on its own; see spec. (Estimate; exact value depends on co-occurrence multiplier interaction.)

---

## 5. Outcome analysis + the 6-winner spot-check (per operator instruction)

### 5a. Raw cohort (name-join, contaminated)

Under-gate tokens (entry mcap < $2M) that "ran" ≥10× by name-join:

| Token | Entry mcap | Peak mcap | Multiple | Score | Spot-check verdict |
|---|---:|---:|---:|---:|---|
| tensor | $18K | $18.1M | 1001× | 31 | **FALSE JOIN** |
| the black bull (ANSEM) | $190K | $90.7M | 477× | 13 | **CONFIRMED** |
| myro | $30K | $4.08M | 136× | 0 | QUESTIONABLE |
| return to memes | $30K | $3.52M | 117× | 0 | QUESTIONABLE |
| catwifhat | $12K | $1.16M | 97× | 6 | QUESTIONABLE |
| bitads | $410K | $20.5M | 50× | 1 | PROBABLE |

### 5b. Identity spot-check (why most are contaminated)

Most names resolve to **two** candidate rows — a cheap Solana/Base DEX row **and** a separate
already-large CoinGecko row — which the name-join conflates:

- **ANSEM — CONFIRMED.** Solana pump.fun mint `9cRCn9rG…pump` ($190K) **is** CG `the-black-bull`
  (symbol ANSEM both sides). The DEX sighting genuinely *preceded* the CG listing → a real early
  catch. Gainers peak $90.7M. ✓
- **Tensor — FALSE.** The $18K **Base** "TENSOR" copycat (`0xae3e…`) was joined to the real
  **Solana TNSR** ($11–18M, coin_id `tensor`). Different assets, different chains. The 1001× is an
  artifact. ✗
- **MYRO / Return-to-Memes / Catwifhat — QUESTIONABLE.** Each has a ~$12–30K Solana row whose
  symbol matches a token already trading at $0.5–3M+ on CG in the same window. The cheap row is
  almost certainly a copycat/duplicate pool, not an early sighting of the eventual winner.
- **Bitads — PROBABLE.** Single CG asset (`bitads`/SN16), $410K → $20.5M (50×). No DEX/CG split
  ambiguity, but it's a CG-sourced row, not an early DEX catch.

### 5c. Verified numerator

**Raw: 6. Verified clean: 1 (ANSEM). Probable: +1 (Bitads). Artifacts: 4.**
The "6 multi-baggers/week" claim from the first pass is **not defensible** — the honest figure is
**1–2 verified under-gate winners in 7 days.** ANSEM remains a genuine, unambiguous 477× miss.

### 5d. Data-quality flags observed

Duplicate token-names (>1 contract) per last-write band: 0–9: **17**, 10–24: **2**, 25–39: **11**.
Many candidate rows carry `liquidity_usd = 0` (CG-sourced) or are DISQUALIFIED_LOW_LIQUIDITY — both
inflate fuzzy-join false positives. **A clean cohort requires same-asset linkage (contract↔coin_id),
not name matching** — see follow-up F1.

---

## 6. Precision by score band & alert-volume cost

Outcome counts by **last-write** `candidates` band (mcap>0), name-join (contaminated — read as
upper bounds):

| Band | # tokens | had follow-up | ran ≥10× | ≥50× | ≥100× |
|---|---:|---:|---:|---:|---:|
| 0–9   | 1,226 | 174 | 4 | 3 | 2 |
| 10–24 | 55 | 5 | 1 | 1 | 1 |
| 25–39 | 105 | 1 | 1 | 1 | 1 |
| 40–54 | 10 | 0 | 0 | 0 | 0 |

After de-contamination, the only band with a *verified* winner is the one containing ANSEM
(last-write 13 → 10–24; peak 50 → 40–54). **Verified precision is ≈1 winner per ~hundreds of
candidates — i.e., score-rank carries almost no signal at these levels.** Corroboration: the two
top-scoring tokens of the week (DUMPSTR 59, Frontdeploy 55) did **not** run.

### Alert-volume if the gate were lowered (peak-score bands, /7-day window → /day)

| New threshold | Contracts crossing/wk | ≈ alerts/day | Catches ANSEM (peak 50)? |
|---|---:|---:|---|
| 55 | 2 | ~0.3 | No |
| 50 | ~? (within 40–54) | ~10–16 | **Yes (barely)** |
| 40 | 114 | ~16 | Yes |
| 25 | 283 | ~40 | Yes |
| 10 | 958 | ~137 | Yes |

**Cost to catch ANSEM by threshold alone: ~16 alerts/day at ~1–2% precision.** This is the
quantified false-positive cost the operator asked for, and it argues against a blanket drop.

---

## 7. Which fix would have caught the cohort?

| Lever | Would it have caught ANSEM? | FP cost |
|---|---|---|
| Lower `MIN_SCORE` to ≤50 | Yes | ~16 alerts/day, ~1–2% precision |
| Un-phantom `holder_growth` only | **No** (lifts ANSEM 50→~63, still <65) | Low, but insufficient alone |
| Holder repair **+** gate recalibration | **Yes**, and more selectively than a blanket drop | TBD — needs soak |
| Relax `token_age` >7d cliff + `vol_liq_ratio` 5.0× | Partially (raises ANSEM's decayed score) | Unproven — needs backtest |
| Dedicated slow-burn lane | Plausibly the cleanest, IF archetype is distinct | Gated on a cleaner-join denominator |

**Conclusion:** the catch requires **gate recalibration as the foundation** (the gate is unreachable
today), with **holder-signal repair** and **age/vol-liq relaxation** as precision-preserving
complements. A dedicated lane should remain deferred until F1 (clean same-asset join) confirms the
archetype is distinct and high-enough precision.

---

## 8. Acceptance-criteria check (operator's gate before implementation)

| Criterion | Status |
|---|---|
| Know alerts/day the proposed fix would add | ✅ ~16/day at thresh 50; ~40/day at 25; ~137/day at 10 |
| Know which fix would have caught the cohort | ✅ gate recalibration (foundation) + holder repair; threshold-alone is poor |
| Know expected false-positive cost | ✅ ~1–2% precision at thresh 50 (1–2 verified winners/wk vs ~112 alerts/wk) |
| Have a holder-data cost/rate-limit decision | ✅ see companion spec §costed-memo — free-tier infeasible; proxy recommended |

---

## 9. Caveats & follow-ups

- **C1 — fuzzy join.** Outcome counts in §6 are name-join upper bounds; only ANSEM/Bitads survive
  spot-check. Treat the 0–9 "winners" as unverified.
- **C2 — short window.** 7 days (candidates pruned beyond that). A monthly window needs
  `score_history` (retained) joined to a retained outcome source, not pruned `candidates`.
- **C3 — survivorship.** This measures winners we under-scored; the denominator's *fizzle* rate
  (~99%) confirms precision is low, but a true precision figure needs the clean join.
- **F1 (follow-up):** rebuild the cohort on **same-asset linkage** (Solana contract ↔ CG coin_id via
  `/coins/{id}.platforms`, per the known cross-source hop) instead of name matching, over a
  `score_history`-based ≥30-day window. This is the prerequisite before any slow-burn-lane decision.
- **F2:** verify whether DUMPSTR/Frontdeploy (the only 55+ tokens) ran or fizzled — sanity-checks
  whether the top of the realized distribution carries outcome signal.
