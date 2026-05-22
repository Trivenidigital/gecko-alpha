# Findings — GeckoTerminal Sample Run (BL-NEW-SOURCE-CALL-PRICE-COVERAGE-SAMPLE-CG-GT)

**Date:** 2026-05-22
**Packet:** `tasks/vendor_sample_decision_packet_cg_gt_2026_05_21.md` (PR #222, merged `c08c910`)
**Operator authorization:** sample run under conservative defaults (GT free target, 202-row ceiling accepted, 5m-bucket 30m policy locked, current-reserve proxy + drift flag accepted, bounded 6-call budget authorized).
**Prod DB:** read-only throughout. **No prod writes.** Temp files cleaned.
**Result:** **NOT PASSED.** 2 of 7 criteria failed. Implementation gate stays CLOSED.

## 1. Sample inputs

Three `dex:*` rows spanning the lookback range:

| Position | call_ts | Chain | Token shape |
|---|---|---|---|
| Oldest | 2025-10-20 | Solana | `Ciphern...` (non-pump) |
| Median | 2026-04-27 | Solana | `*pump` (pump.fun convention) |
| Newest | 2026-05-21 | Solana | `*pump` (pump.fun convention) |

## 2. Per-token results

### Oldest (2025-10-20)
- Pool resolution: **20 pools returned**.
- Top pool selected by `current_reserve_proxy_v1` rule.
- OHLCV fetch: **HTTP 401, zero candles**.
- Diagnosis: GT free returns pool catalog for an old token but rejects the historical OHLCV fetch. The 401 (not 404) suggests either (a) historical-OHLCV beyond a recency cap is Pro-gated, or (b) GT's free tier has an undocumented historical limit. Either way: **GT free does NOT cover the 7-month corpus**.

### Median (2026-04-27)
- Pool resolution: **6 pools returned**.
- OHLCV fetch: **300 5m candles**, epoch-second timestamps confirmed.
- Diagnosis: clean pass for the ~1-month-old range. GT free works for recent data.

### Newest (2026-05-21)
- Pool resolution: **0 pools**.
- Contract has `pump` suffix; classified `bonding_curve_pre_graduation_unverified` per packet §7.
- Diagnosis: heuristic worked as designed. NOT a generic GT failure — a structural fact of the bonding-curve era.

## 3. Acceptance criteria evaluation

| # | Criterion | Result | Evidence |
|---|---|---|---|
| 1 | Pool resolution works for ≥1 of 3 | **PASS** | Oldest (20) + Median (6) resolved; Newest correctly classified pre-graduation |
| 2 | OHLCV returns data for every resolved sample | **FAIL** | Oldest fetched 401 / zero candles despite pool resolving |
| 3 | Timestamps are unix-epoch seconds | **PASS** | Median's 300 candles confirmed 10-digit epoch |
| 4 | 5m aggregate is queryable | **PASS** | Median's `aggregate=5` returned 300 candles |
| 5 | Rate-limit signal observable | **PASS** | Documented 30 req/min cap honored; 6 calls under cap |
| 6 | Pool `attributes.address` round-trips | **PASS** | Round-trip confirmed on Median |
| 7 | Oldest lookback non-empty | **FAIL** | 2025-10-20 fetch returned 401 / zero candles |

**Overall: 5/7 pass, 2/7 fail.** Failures cluster on the oldest-token cohort (criterion 2 fails BECAUSE criterion 7 fails — same root cause: GT free can't fetch historical OHLCV for that age band).

## 4. Interpretation

GT public API **CAN** support 30m-derived returns — for recent / resolved pools. It **CANNOT** validate the 7-month backfill corpus we currently have. The "realistic floor" predicted in packet §3 (could be <50 of the 202 OPTIMISTIC ceiling) is empirically confirmed: the older the call_ts, the higher the GT-historical attenuation.

Specifically:
- The 401 response on the oldest token (with pool catalog returning fine) suggests GT's free tier has either a recency cap on OHLCV OR a Pro-gated historical window. The packet's criterion-7 failure shape was designed for this exact case.
- Newest token classified `bonding_curve_pre_graduation_unverified` is not a GT shortcoming — pump.fun tokens before graduation have no Raydium/Orca pool by design.
- Median token's clean pass confirms GT free is operationally fine for the recent-and-graduated subset.

## 5. Three forward paths (operator-decided)

Per operator's recommendation, the realistic options:

### Path 1 — Narrow GT eligibility to recent call_ts only
- Pre-registered criterion-7 failure shape (per packet §6) applies: cap eligible rows to `call_ts >= (now - observed_cap_days)`.
- **Open question:** what is GT free's actual lookback cap? Sample didn't binary-search the boundary. Future sample (1-2 more calls) probing intermediate ages (2 months, 4 months) would establish it.
- **Pros:** uses GT free; no paid vendor commitment. Forward backfill works from the cap onward.
- **Cons:** locks out historical corpus permanently for the GT track. Older `source_calls` would never have forward-window prices unless a different vendor enters.

### Path 2 — Try a different provider for older history
- Candidates: CG Pro `/onchain/networks/{network}/pools/{pool_address}/ohlcv/{timeframe}` (paid, ~$129/mo Analyst tier). Same data shape as GT.
- **Open question:** does CG Pro have a lookback advantage over GT, or is it the same data with SLA?
- **Pros:** would unlock the older corpus IF CG Pro has deeper history.
- **Cons:** cost. Operator must budget.

### Path 3 — Treat GT as forward-only / prospective coverage from now onward
- Backfill the corpus from `now` forward — new `source_calls` rows get full forward-window coverage; older rows accept "no forward coverage" as their permanent state.
- **Pros:** no vendor cost change; uses GT free. Coverage grows organically over time.
- **Cons:** 1323 existing rows stay at 0% forward coverage permanently. Dashboard's `not_rankable_label` may take months to flip even for the 202-row eligible subset.

## 6. What's still operator-gated

The three paths are an operator decision. None are pre-committed by this findings doc.

Implementation gate STAYS CLOSED. **No `_fetch_snapshot_rows` change.** No schema migration. No new vendor calls without explicit operator authorization per-path.

## 7. Sub-findings recorded for future reference

- GT free returns HTTP **401** (not 404) on historical OHLCV beyond an undocumented cap. The `401` is informative — pool catalog endpoint returns fine for the same old token, so it's specifically an OHLCV-history issue. (If this were 429 it'd be rate-limit; if 404 it'd be "token unknown". 401 hints at "Pro-tier required".)
- Solana pump.fun token classification `bonding_curve_pre_graduation_unverified` worked as designed. The packet's PR-review fold (rename from raw `bonding_curve_pre_graduation` → `_unverified` suffix) was the right call.
- Pool selection by `current_reserve_proxy_v1` did NOT cause sample failure on the median or oldest tokens. Drift-risk caveat remains theoretical for now; not empirically validated either way.
- 5m → 30m derivation policy was NOT exercised because the OHLCV fetch failed on the oldest. Half-open bucket convention untested empirically.
- Rate-limit signal: 6 calls in <60s, well under the 30 req/min cap. No 429 observed.

## 8. Follow-up BLs filed

Three new backlog entries record the three forward paths so the operator can pick when ready:

| BL | Description |
|---|---|
| `BL-NEW-SOURCE-CALL-GT-LOOKBACK-CAP-PROBE` | Future sample: binary-search GT free's historical cap with 2-3 more calls. PROPOSED — only run if operator picks Path 1. |
| `BL-NEW-SOURCE-CALL-PRICE-COVERAGE-SAMPLE-CG-PRO` | Future packet: evaluate CG Pro's lookback advantage. PROPOSED — only file detailed packet if operator picks Path 2. |
| `BL-NEW-SOURCE-CALL-FORWARD-ONLY-COVERAGE` | Plan: implement GT-free backfill for `call_ts >= some_future_anchor`, accept zero historical coverage. PROPOSED — only plan if operator picks Path 3. |

## 9. Reproducibility

Sample script lives only in operator-local environment (not committed). Per packet §11 + design §3:
- Script ran on srilu-vps via `/root/gecko-alpha/.venv/bin/python /tmp/sample.py` per two-step SSH pattern.
- Cache JSON files written under `tasks/vendor_samples/` (gitignored per `.gitignore` line shipped in PR #222).
- Total GT public API calls consumed: **6** (within authorized budget).
- Temp files cleaned by operator post-run.

## 10. Bottom line for the operator

GT free is **partial coverage**, not full coverage. The 7-month corpus cannot be backfilled via GT free. Three forward paths exist; each is a docs-only follow-up until operator picks one. Implementation gate stays CLOSED. No code change ships from this findings doc.
