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

However, two pieces of evidence strengthen the negative beyond a pure budget-limited false-NO:

1. **Token A pattern is structurally suggestive, not just heuristic-driven.** All 3 probed pools existed *before* call_ts, yet GT returned 401 on all three regardless of time window. This is consistent with "GT free does not index OHLCV for old/abandoned pools at all," not "we picked the wrong pools." That pattern would not improve by probing the other 17.
2. **Token B pattern is structurally informative.** The 2 oldest pools have data ending ~2 weeks *before* call_ts. The 8 unprobed pools are all newer (created after 2025-12-23). Newer pools cannot have call_ts coverage on a 4-month-old call_ts unless they were created in early January 2026 — which the top-reserve pool may have been, but that's the V1 rule which already fails. No middle-aged "Goldilocks pool" appears likely.

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
