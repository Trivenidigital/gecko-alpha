# Findings — Historical Pool-Selection Probe (BL-NEW-SOURCE-CALL-HISTORICAL-POOL-SELECTION-PROBE)

**New primitives introduced:** NONE — findings-only.

**Date:** 2026-05-22
**Packet:** `tasks/probe_packet_historical_pool_selection_2026_05_22.md` (PR #225, merged `6cef40e9`)
**Operator authorization:** explicit budget — max 10 GT free calls, findings-only, no prod DB writes, 2-3 old tokens, multiple pools per token.
**Prod DB:** untouched. Targets inlined as constants (resolved at packet-write time via PR #225 §3). **No prod writes. No paid APIs. Read-only execution host.**
**Result:** **NO POOL COVERS call_ts FOR ANY PROBED ROW** within the operator-authorized 10-call budget (8 calls used, 1 rate-limit, 7 informative).

## 1. Plan amendments folded before run

Per parallel two-vector plan review (V-A measurement / V-B vendor-safety) on PR #225 packet:

| Amendment | Source | Applied |
|---|---|---|
| Symmetric ±30m window (`limit=12` + post-fetch filter `call_ts-1800 ≤ ts ≤ call_ts+1800`) | V-A #1 | YES |
| `MAX_CALLS=10` script-level counter with `assert calls_made < MAX_CALLS` before each call | V-B #1 | YES |
| No retry / no redirect-follow (custom `NoRedirectHandler`) | V-B #1 / #3 | YES |
| Inter-call `sleep(0.5)` | V-B #2 | YES |
| Targets inlined as constants (no DB read by script) | V-B #4 | YES |
| Deterministic sort tie-break on `pool_address` | V-A #7 | YES |
| Post-cleanup `ls … || echo CLEAN` verification | V-B #5 | YES |
| Record HTTP status alongside body in each JSON | V-A #6 | YES |
| Sanity-check `call_ts_unix ∈ (1.5e9, 2.5e9)` per token | V-A #5 | YES |
| Weakened §7 outcome row 3 interpretation: "0 pools among *the 3 probed per token*, not 0 across all 20" | V-A #3 | YES — see §6 below |
| Pool count co-variate in §7 row 2 | V-A #4 | YES — see §3 |

## 2. Run summary

| Field | Value |
|---|---|
| Run timestamp (UTC) | 2026-05-22T17:32:22Z |
| Total GT free calls made | 8 / 10 budgeted |
| Execution host | srilu-vps (`root@89.167.116.187`) |
| Probe artifact (gitignored cache) | `tasks/vendor_samples/probe_pool_selection_2026_05_22/` |
| `/tmp` artifacts (script + out dir) | removed + verified `CLEAN` post-run |

## 3. Per-token results

### Token A — CIPHER, 2025-10-20 (cornerstone, ~7mo old)

| Field | Value |
|---|---|
| Contract | `Ciphern9cCXtms66s8Mm6wCFC27b2JProRQLYmiLMH3N` |
| `/pools` HTTP status | 200 |
| Pools returned by GT | 20 |
| Top-reserve pool (skipped) | `6H3u1xahPNY6iNFvdRvHaZLcGjFDCox7d7b6jgW546ZW` |
| Probed (3 oldest by `pool_created_at` ASC) | `AJi5VSca...` (2025-09-02), `HnbiJcje...` (2025-09-11), `87XMpvMs...` (2025-09-12) |
| OHLCV result on all 3 probed | **HTTP 401, 0 candles** |
| ANY pool covers call_ts | **FALSE** within budget |

**Structural observation:** all 3 probed pools were created in **September 2025**, ~6 weeks *before* call_ts (2025-10-20). They existed at the time of the call. The 401 is therefore not a "pool didn't exist yet" issue — it is GT free returning *no OHLCV data for these specific pools at any timestamp*. This is consistent with old/abandoned pools being absent from GT's free-tier OHLCV index entirely.

### Token B — 2026-01-06 (~4mo old)

| Field | Value |
|---|---|
| Contract | `3wh5zc1BXzwLaiq4g71q69pM6mpHGo8YYicfma71txu3` |
| `/pools` HTTP status | 200 |
| Pools returned by GT | 10 |
| Top-reserve pool (skipped) | `3GGNTHoRZmimsur31DraTtPhbSJijPKjSwARpDVZiz3J` |
| Probed (2 oldest) | `8H8xAu3q...` (2025-12-23T00:06:20Z), `6caaEuZs...` (2025-12-23T00:07:04Z) |
| OHLCV result | HTTP 200 on both; **5 + 12 candles**; **0 in window** |
| First / last candle ts on pool 1 | 1766448300 (2025-12-22T13:25Z) / 1766449500 (2025-12-22T13:45Z) — **~14.7d before call_ts** |
| First / last candle ts on pool 2 | 1766632200 (2025-12-24T16:30Z) / 1766682300 (2025-12-25T06:25Z) — **~12.4d before call_ts** |
| ANY pool covers call_ts | **FALSE** within budget |

**Structural observation:** both probed pools have data, but only from a brief active window in **late December 2025**, approximately 12–15 days *before* the 2026-01-06 call_ts. The pools went inactive (or were drained/migrated) well before the call. `before_timestamp=call_ts+1800` was honored — GT returned the most recent 12 candles *before* that ts, which happened to be from late December because the pool's activity ended there. The data path is intact; the pools just have no candles at call_ts.

### Token C — 2026-02-09 (~3mo old)

| Field | Value |
|---|---|
| Contract | `J3DpHpw8yT5x1cjwjsJyfas3hXrbjC5FXzQYcfK2rJ8R` |
| `/pools` HTTP status | **429** |
| Pools returned | 0 (rate-limited) |
| Probed | 0 |
| ANY pool covers call_ts | **INCONCLUSIVE** (no data) |

**Structural observation:** GT free's 30/min limit was hit at call #8 — the prior 7 calls including 0.5s inter-call sleep fell within rate budget on this script alone, but srilu-vps may have had other concurrent traffic to GT (pipeline ingestion runs every cycle). Re-run feasibility: yes, but consumes additional budget; deferred since A and B already give a directional read.

## 3.5. Pre-call_ts pool distribution (post-review tightening)

Per V-A reviewer IMPORTANT findings I1/I2/I3, the inference "3-of-N probed and all negative ⇒ remaining N−3 unlikely to help" was load-bearing without a distribution check. Inspecting the cached `/pools` JSON for Tokens A and B (zero additional GT calls; data already in `tasks/vendor_samples/probe_pool_selection_2026_05_22/`):

### Token A — CIPHER, 20 pools by `pool_created_at`

| Bucket | Count | `pool_created_at` range | Probed? |
|---|---|---|---|
| Pre-call_ts | **14** | 2025-09-02 to 2025-10-15 | 3 (oldest) probed; 11 unprobed |
| Post-call_ts | 6 | 2025-10-23 to 2026-02-26 | 0 (cannot have call_ts coverage by construction) |

- Top-reserve pool (skipped, V1 already failed): `6H3u1xahPNY6...` created 2025-09-05, reserve $79,651 — PRE-call_ts, but excluded by packet §6.
- 14 pre-call_ts pools, but 13 of them (including the 3 we probed) have reserves between $737 and $4,602 — consistent with abandoned launch-era pools. Only the V1-rule top-reserve pool ($79K, already failed) and one other (`3C8ir9H85oyJ` $4,602) exceed $4K.
- **11 pre-call_ts pools remain unprobed**: 1 at $79K (V1, already failed), 10 at $700–$4,600. Probing all 10 would consume 10 additional GT calls — a full second budget — and the dominant-mode evidence is that the 3 probed in the same reserve-range (~$1K–$4K) all returned 401. The probability that the 10 unprobed pools in the same reserve-range behave differently is low but not zero.
- **Honest residual ambiguity:** GT could conceivably index OHLCV for some `~$1K-reserve` pools but not others (e.g., based on listing-date-on-GT independent of pool-creation-date). The 3-of-13 negative is a strong but not exhaustive signal.

### Token B — 2026-01-06, 10 pools by `pool_created_at`

| Bucket | Count | `pool_created_at` range | Probed? |
|---|---|---|---|
| Pre-call_ts | **9** | 2025-12-23T00:06 to 2025-12-23T12:31 (single launch day) | 2 (oldest) probed; 7 unprobed |
| Post-call_ts | 1 | 2026-04-08 | 0 (cannot cover call_ts by construction) |

- 9 of 10 pools were created within a **12-hour window on 2025-12-23**, ~14 days before call_ts — this is a launch-day liquidity cluster.
- Top-reserve pool (skipped, V1 already failed): `3GGNTHoRZmim...` created 2025-12-23T00:25, reserve $29,889 — PRE-call_ts but in the same launch-day cluster as the 2 we probed.
- The 2 probed pools (`8H8xAu3q...` $0 and `6caaEuZs...` $6.29 reserve) returned data only from 2025-12-22 to 2025-12-25 — the launch-day cohort went mostly inactive by Christmas, 12+ days before call_ts.
- **7 pre-call_ts pools remain unprobed**, all from the same 12-hour launch-day cluster. They are structurally similar to the 2 probed and to the V1-skipped pool. Whether any of them stayed active longer than 2025-12-25 is the open question.
- **Alternative reading not refuted by current evidence (V-A I2):** "GT data-completeness gap for low-volume pools" cannot be ruled out from this data alone. The 2 probed pools had only 5 + 12 candles total over ~2 days — that low candle count is itself ambiguous between "the pool only traded for 2 days then died" (liveness) and "GT indexed only 5+12 candles of a continuously-traded pool" (completeness). The PR #224 lookback-cap probe found 137/110/239 candles at 180/120/60d back on one CIPHER pool, suggesting GT's indexing is dense on at least some pools — but that says nothing about the indexing density of *these specific* dust-reserve pools.

### What this implies for the recommendation

Tightening §5's "structural negative" claims:

1. **Token A claim "GT free doesn't index OHLCV for old/abandoned pools at all"** — supported by 3-of-13 same-reserve-range pre-call_ts pools all returning 401. Not exhaustive; the 10 unprobed dust-reserve pools could conceivably break the pattern. Hedged to: "GT free does not index OHLCV for the dominant mode of CIPHER's pre-call_ts dust-reserve pools; the 10 unprobed same-cohort pools are likely-but-not-certainly the same."
2. **Token B claim "pools went inactive ~14d before call_ts"** — supported by candle-ts evidence on 2 probed pools from the launch-day cluster. Alternative reading "GT incompletely indexes low-volume pools" not refuted. Hedged to: "pool activity (or GT-indexed activity, which the data cannot distinguish) ended ~14d before call_ts on 2 of 9 launch-day-cluster pools."
3. **Token A claim about unprobed-pool distribution (V-A I3 shape applied to A):** All 11 truly-unprobed pre-call_ts pools sit in the same launch-window + same dust-reserve range as the 3 probed. Structural similarity strengthens but does not guarantee identical behavior.

The recommendation in §7 (PARK) remains the right call given:
- 13 of 14 pre-call_ts pools on Token A are dust-reserve, similar to the 3 probed (all 401).
- All 9 pre-call_ts pools on Token B are from the same 12-hour launch-cluster, similar to the 2 probed (both data-ended ~14d pre-call_ts).
- The "GT data-completeness gap" alternative reading (V-A I2) only argues for *more uncertainty*, not for a different recommendation — under uncertainty, the operator still chooses Path 2 (paid, definitive) or Path 3 (forward-only, no historical claim needed).

## 4. Coverage matrix

| Token | call_ts age | Pools in GT | Probed | Pools with OHLCV ≥1 candle | Pools covering call_ts ± 30m |
|---|---|---|---|---|---|
| A | ~7 months | 20 | 3 (oldest) | 0 (all 401) | **0** |
| B | ~4 months | 10 | 2 (oldest) | 2 | **0** (candles too old) |
| C | ~3 months | unknown (429) | 0 | n/a | **n/a** |

**Total probed pools with call_ts coverage: 0 of 5 informative probes.**

## 5. Outcome interpretation against PR #225 packet §7

PR #225 packet §7 defined three outcome rows. After folding the V-A reviewer amendment (3 of 20 thin → false-NO risk → weaken row 3), the mapping is:

| §7 Row | Outcome | This run |
|---|---|---|
| 1: ≥1 pool covers call_ts for Token A | V1 pool-selection replaceable; design new rule | **Not satisfied** (0/3 for A) |
| 2: 0 for A but ≥1 for B or C | Mixed — older rows uncovered; newer-ish rows recoverable with better selection | **Not satisfied** (0 for B; C inconclusive) |
| 3 (weakened): 0 across all probed pools within budget | Suggestive (not definitive) that GT free is structurally inadequate for the old corpus. False-NO possible since only 3-of-20 (A) and 2-of-10 (B) sampled. | **This is the observed outcome** |

The "weakened" row 3 framing matters: we have **not exhaustively tested 20 / 10 pools per token**. Strictly, the conclusion is "0 of the 3-5 oldest pools by `pool_created_at` ASC cover call_ts" — a heuristic-driven negative, not an exhaustive negative.

However, two pieces of evidence strengthen the negative beyond a pure budget-limited false-NO (tightened in §3.5 against the cached `/pools` JSON):

1. **Token A pattern is structurally suggestive, not just heuristic-driven.** Per §3.5 distribution check, Token A has 14 pre-call_ts pools and 6 post-call_ts (which cannot cover call_ts by construction). Of the 14, 13 — including the 3 probed and the V1-skipped top-reserve pool — fall in a dust-reserve range ($737–$4,602 plus the V1 outlier at $79K). The 3 probed all returned 401. 10 of the remaining 11 are dust-reserve same-cohort pools; structural similarity suggests but does not exhaustively prove same behavior. **Hedge: "the dominant mode of pre-call_ts pools is not indexed by GT free; 10 same-cohort pools remain untested."**
2. **Token B pattern is structurally informative.** Per §3.5, 9 of 10 pools are from a single 12-hour launch-day cluster on 2025-12-23 (14 days before call_ts). 2 of those 9 were probed; both have data ending 2025-12-25. The 7 unprobed pools sit in the same launch cluster and likely-but-not-certainly behave the same. **Hedge: "the launch-day cluster mostly went dormant before call_ts on the 2 of 9 probed pools."**

The V-A reviewer's alternative reading "GT data-completeness gap for low-volume pools" (V-A I2) cannot be ruled out from this data alone — but it only argues for *more uncertainty*, not a different recommendation. Under uncertainty, the operator still chooses Path 2 (paid, definitive) or Path 3 (forward-only, no historical claim needed). See §3.5 closing paragraphs.

## 6. Closes-vs-keeps-open updates

From PR #225 packet §11:

- **BL-NEW-SOURCE-CALL-PRICE-COVERAGE-SAMPLE-CG-PRO** (Path 2, CG Pro paid): now **more compelling** as the historical-corpus rescue path, contrary to PR #225's prediction that a positive probe would obviate it. Decision still operator-cost-tolerance-gated (~$129/mo). Keep PROPOSED.
- **BL-NEW-SOURCE-CALL-FORWARD-ONLY-COVERAGE** (Path 3, GT-free new rows only): remains the no-cost option. Keep PROPOSED.
- **BL-NEW-SOURCE-CALL-HISTORICAL-POOL-SELECTION-PROBE** itself: **CLOSE** as PROBE-RUN-NEGATIVE.

## 7. Recommendation

**PARK historical GT backfill for the old corpus.** The probe did not find a smarter-pool-selection rescue within the operator-authorized budget; the structural pattern on Token A (3 pre-call_ts pools, all 401) plus the structural pattern on Token B (2 oldest pools active before call_ts, inactive at call_ts) is consistent enough to recommend NOT pursuing a V2 pool-selection design against GT free for the old corpus.

**Operator decision required between two paths:**

- **Path 2 (CG Pro, paid):** ~$129/mo Analyst tier. Unknown whether CG Pro has deeper history than GT public, or whether the data is identical behind a paywall. Requires a separate paid-tier sample (`BL-NEW-SOURCE-CALL-PRICE-COVERAGE-SAMPLE-CG-PRO`) before commitment.
- **Path 3 (GT-free forward-only):** zero vendor cost. New `source_calls` rows get full coverage; older rows accept "no forward coverage" permanently. Implementation effort sized in `BL-NEW-SOURCE-CALL-FORWARD-ONLY-COVERAGE`.

This findings document **does not pick a path** — the operator's cost / patience tolerance is the decision input. The probe's job was to rule the V1-rescue path in or out, and it is now ruled out within budget.

## 8. Token C re-probe option

If the operator wants a Token C data point to close that ambiguity, a single `/pools` + 2 `/ohlcv` call (3 calls, well under a fresh 10-call budget) would complete the matrix. Recommendation: **defer** — the directional read from A + B is sufficient; Token C is unlikely to overturn the recommendation.

## 9. Scope discipline

This document is findings-only:
- No code changes (probe script lived only on `/tmp/probe_pool_selection.py`, removed post-run).
- No backlog status changes for unrelated BLs.
- No implementation design for V2 pool selection (would be premature — recommendation is to park, not design).
- No vendor-sample JSON committed to git (`tasks/vendor_samples/` is gitignored from PR #222).

## 10. Backlog entries updated by this PR

- `BL-NEW-SOURCE-CALL-HISTORICAL-POOL-SELECTION-PROBE`: status `PACKET-SHIPPED-AUTHORIZED` → `PROBE-RUN-NEGATIVE`. Closes-with-result.
- `BL-NEW-SOURCE-CALL-PRICE-COVERAGE-SAMPLE-CG-GT`: brief sub-finding update referencing this probe's negative result.

## 11. Rollback

Findings-only. No deployment. Cached JSON files gitignored. `/tmp` cleaned + verified. Zero blast radius.
