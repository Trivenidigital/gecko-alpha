# Vendor Sample Decision Packet — CoinGecko / GeckoTerminal Track

**Date:** 2026-05-21
**BL:** `BL-NEW-SOURCE-CALL-PRICE-COVERAGE-SAMPLE-CG-GT`
**Parent BL:** `BL-NEW-SOURCE-CALL-PRICE-COVERAGE-EXPANSION` (DESIGN-SHIPPED, PR #208)
**Status:** Packet only. Implementation gated on the 5 operator decisions in §3 + sample-pass evidence.
**Operator action required:** answer the 5 decisions in §3, then either authorize the sample call (§4) or reject.

## 1. TL;DR

PR #220 found GoldRush `historical_by_addresses_v2` is daily-only. Operator leans toward path C (CG/GT). This packet evaluates **GeckoTerminal public API** as the first sample target. Recommended because:

- Same data as CoinGecko `/onchain` Pro endpoints (per CG MCP docs).
- Free public tier, no API key required.
- 30 req/min rate limit (sample uses ≤6 calls).
- Minute-aggregate candles (1, 5, 15) — supports deriving 30m as a return between two 5m candles.

**Critical pre-registrations (NOT vendor-flexible):**
- 30m horizon is a **return between two 5m candles**, NEVER an OHLCV composite.
- Pool selection uses **current `reserve_in_usd` as a proxy** for "primary pool at call_ts"; drift caveat surfaced in schema.
- Only **~15% of source_calls** (the `dex:chain:contract` subset) are ever eligible regardless of vendor.

## 2. What this packet IS and IS NOT

### IS:
- Operator-facing decision document.
- The 5 decisions in §3 + the call payload in §4 + the 7 acceptance criteria in §5 + the failure-path rubric in §6.

### IS NOT:
- A vendor API call. **Zero calls made by this packet.**
- A prod DB write. **Read-only sqlite3 URI mode=ro only.**
- A schema migration.
- A `.env` edit.
- Implementation authorization (separate session post-sample).

## 3. Five pre-sample operator decisions

These MUST be answered before the sample script runs.

| # | Decision | Notes |
|---|---|---|
| 1 | **Vendor target if sample passes:** GT free (no SLA, beta) OR CG Pro (paid Analyst+, SLA)? | If CG Pro, budget BEFORE sample run. Schema records vendor as a dimension; switch is reversible. |
| 2 | **202-row V1 ceiling:** ACCEPT and file `BL-NEW-SOURCE-CALL-IDENTITY-RESOLUTION` upstream BL? OR reject this BL until identity is solved first? | Without identity work, dashboard's `not_rankable_label` may never flip. |
| 3 | **30m derivation policy:** confirm pre-registration "close-at-5m-bucket return" — NEVER OHLCV composite. | Locks the measurement; future implementer cannot redefine. |
| 4 | **Pool selection rule v2 (honest):** GT cannot return reserves AT call_ts. V1 uses *current* `reserve_in_usd` as a proxy + `pool_drift_risk_flag` band based on `(now - call_ts).days`. ACCEPT? | Alternative: require operator to manually-curate the pool per token (doesn't scale). |
| 5 | **Sample call budget:** authorize **exactly 6 GT public API calls** (N=3 tokens × 2 calls each: oldest/median/newest `dex:*` `call_ts`)? OR scope down to 1 token (oldest only, 2 calls)? | 6-call budget is well under 30 req/min cap. |

Sample script does not run until each is answered. The packet does not assume answers.

## 4. The exact sample call shape

If all 5 decisions are answered AND operator authorizes the sample, the script runs ≤6 calls against the GT public API.

### 4.1 Endpoint URLs
```
GET https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{contract}/pools
GET https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool_address}/ohlcv/minute?aggregate=5&before_timestamp={unix_ts}&limit=300
```

No `Authorization` header. No `Cookie`. No API key. No secret material at all.

### 4.2 Tokens to sample (resolved from `source_calls.dex:chain:contract` rows)

The script selects three rows at runtime:
- **Token A — oldest `dex:*` row.** call_ts ≈ 2025-10-20T19:22:52+00:00 (~7mo old, lookback boundary test).
- **Token B — median `dex:*` row.** call_ts ≈ early 2026 (mid-range sanity).
- **Token C — newest `dex:*` row.** call_ts ≈ 2026-05-21T16:17:28+00:00 (fresh-token control).

Each token: 1 `/pools` resolve + 1 `/ohlcv/minute?aggregate=5&before_timestamp=...` fetch = 2 calls. Total = 6.

### 4.3 Chain → network translation

Reuses the in-tree `_geckoterminal_network_for_chain()` from `scout/ingestion/geckoterminal.py`. Sample raises explicitly on unsupported chain (no silent skip).

Currently supported chains in source_calls: `solana=136, ethereum=34, bsc=19, base=10, monad=2, hyperevm=1` (only `dex:*` rows). If the random median or oldest token is on an unsupported chain (e.g., `monad`), sample either retries the next token in that age band OR raises (operator choice; default = retry).

### 4.4 Time-range for OHLCV fetch

`before_timestamp = call_ts_unix + 28800` (call_ts + 8h, to capture all forward horizons up to 24h). `limit=300` returns 300 5m candles ≈ 25 hours of data.

### 4.5 Pool selection (v2 honest)

For each token:
1. Sort pools by `attributes.reserve_in_usd` descending.
2. Tiebreak on `attributes.address` lexical.
3. Use top pool. Record `pool_selection_method='current_reserve_proxy_v1'` + `pool_drift_risk_flag` based on `(now - call_ts).days`.

NOTE: This is a proxy. GT public API does NOT expose historical reserves at arbitrary `call_ts`. The drift is explicitly recorded; it is not silently swept under the rug. For tokens with `call_ts > 90 days old`, the operator should expect higher pool-migration risk.

### 4.6 Cache wrapper

Every call writes one JSON file under `tasks/vendor_samples/`:
```
gt_<chain>_<contract[:8]>_<call_ts_yyyymmdd>_<call_type>.json
```
Schema:
```json
{
  "call_url": "...",
  "call_method": "GET",
  "call_ts_utc": "2026-05-21T...",
  "response_status": 200,
  "response_headers": {...},
  "rate_limit_headers": {...},
  "elapsed_ms": 142,
  "response": <raw GT body>,
  "bl": "BL-NEW-SOURCE-CALL-PRICE-COVERAGE-SAMPLE-CG-GT",
  "criterion_evidence_for": [1, 2, 3, 4, 5, 6, 7],
  "prev_call_ts_utc": "2026-05-21T..."
}
```

Two-layer `.gitignore` precheck enforced before any call dispatches:
1. `git check-ignore -v tasks/vendor_samples/` returns a committed source line.
2. `git ls-files --error-unmatch tasks/vendor_samples/` exits non-zero (nothing tracked).

## 5. Acceptance criteria

Sample is ACCEPTED only if all 7 hold:

1. **Pool resolution works for ≥1 of 3 sampled tokens.** GT returns ≥1 pool. (If 0/3 resolve, reject GT entirely.)
2. **OHLCV endpoint returns data.** Each token-day fetch returns 2xx with ≥1 candle in `ohlcv_list`.
3. **Timestamps are unix-epoch seconds.** 10-digit unix (e.g., `1679414400`). Not 13-digit ms. Not ISO.
4. **5m aggregate is queryable.** Path `aggregate=5` returns 2xx for at least one sampled token-day. (Corpus-wide gap rate is implementation-phase, not sample-phase.)
5. **Rate-limit signal is observable.** Either response headers OR documented 30 req/min cap can be honored via clock-based count. NOT a capacity-floor measurement.
6. **Pool `attributes.address` round-trips into OHLCV path.** The address used for `/ohlcv/{pool_address}` is the literal `attributes.address` value from `/pools`, NOT the `id` field (which has format `solana_<addr>` etc. — must be stripped per `scout/models.py:177-182`).
7. **Lookback covers oldest sampled call_ts.** The 2025-10-20 fetch returns non-empty `ohlcv_list`. (Failure → narrow eligible-row set; operator-decided in follow-up plan. NOT a hard reject.)

## 6. Reject / re-scope rubric

| Failure | Path |
|---|---|
| 0/3 tokens resolve to pools | Reject GT. Consider CG Pro OR identity-resolution upstream first. |
| Criterion 2 fails (no OHLCV) | Endpoint mismatch. Re-verify path. |
| Criterion 3 fails (timestamp drift) | Reject GT. Design's `provider_timestamp_semantics` invariant fails. |
| Criterion 4 fails (5m not supported) | Re-scope: shift to 15m × 2 OR shift primary horizon to 15m. Re-park if neither works. |
| Criterion 5 fails (no rate-limit observability) | Sharpen the script's clock-based pacing audit. Not a hard reject. |
| Criterion 6 fails (round-trip uses wrong field) | Implementer error in v2 — fix script, re-run sample. Not a vendor problem. |
| Criterion 7 fails (lookback cap shorter than 7mo) | Empirically record cap, narrow `eligible-row` set, recompute coverage ceiling. Operator-decided in follow-up plan. |
| 1-2/3 tokens resolve, 1-2/3 don't | Record attenuation factor. Proceed only if criterion 1 floor met (≥1). |

## 7. Pre-graduation Solana pump.fun token classification

If a sampled token's contract suffix is `pump` (Solana pump.fun bonding-curve convention) AND `/pools` returns 0 pools, the script classifies as `bonding_curve_pre_graduation` — NOT `gt_no_coverage`. These are structurally unbackfillable on GT until the token graduates to Raydium. Distinct from delisted / lookback-cap failures.

## 8. Risks (operator visibility)

| Risk | Drift / impact |
|---|---|
| Pool selection uses *current* reserves as proxy | High-drift band for `call_ts > 90 days` — explicit `pool_drift_risk_flag` in schema. |
| Pool migration mid-forward-window | V1 unaddressed. Schema records `pool_address` snapshot; `forward_pct_quality='pool_migration_window'` populated by future sweep. |
| GT free is beta — breaking changes possible | Future cron smoke-test for shape drift. Out of sample scope. |
| 5m candle gaps on low-volume tokens | Impl-phase gate, not sample. |
| Chain map gap: bsc/monad/hyperevm | Sample raises explicitly; `BL-NEW-GT-CHAIN-MAP-EXTENSION` filed. |

## 9. Cost / rate-limit risk

| Risk | Mitigation |
|---|---|
| Credit overrun | GT free, no credits. Hard global cap = 6 calls; per-token quota = 2. |
| Vendor API key leakage | GT requires no key. N/A. |
| Rate-limit burst | 250ms minimum sleep between calls; 6 calls fits comfortably in any 60s window. |
| GT TOS / acceptable use | Public API explicitly permits programmatic access; gecko-alpha already uses `/trending_pools`. No new TOS concern. |

## 10. Operational mechanics

### 10.1 Execution host

srilu-vps. Sample script runs via `/root/gecko-alpha/.venv/bin/python /tmp/sample.py` (NOT system `python3` — script imports `aiohttp` + `aiosqlite`).

### 10.2 Two-step SSH pattern (Windows operator)

Per global CLAUDE.md:
```
# Step 1
ssh srilu-vps '/root/gecko-alpha/.venv/bin/python /tmp/sample.py --i-have-read-the-packet > /tmp/sample.out 2>&1' > .ssh_tmp/sample.txt 2>&1

# Step 2
Read .ssh_tmp/sample.txt (locally)
```

### 10.3 Post-run cleanup

After Read step completes:
```
ssh srilu-vps 'rm -f /tmp/sample.py /tmp/sample.out'
```

## 11. What this packet does NOT do

- Does not call any vendor API.
- Does not write to prod DB.
- Does not assume operator's answers to §3.
- Does not commit any vendor response, secret, or credential to git.
- Does not edit `.env`.
- Does not start implementation. The implementation PR is gated on operator approval of this packet AND a successful sample evaluation AND any required follow-up scoping (per §6 rubric).

## 12. What changes if operator approves

1. Operator answers the 5 decisions in §3.
2. Future session writes `scripts/vendor_sample_gt.py` (~80 LOC) per design §3.
3. Operator runs the script via the two-step SSH pattern (§10.2).
4. Sample script writes ≤6 JSON files under `tasks/vendor_samples/` (gitignored).
5. Script evaluates the 7 criteria, produces a findings doc.
6. Acceptance/rejection per §6 rubric.
7. If accepted: implementation PR begins (separate session) — must include §12a freshness-SLO + watchdog as blocking sub-task per design §11.
8. If criterion 7 fails: operator-decided follow-up plan (lookback cap, narrowed eligibility).
9. If rejected: re-park; consider CG Pro packet OR identity-resolution upstream BL.

## 13. Rollback

If sample is run and any criterion fails:
- No prod DB writes (script enforced read-only).
- `tasks/vendor_samples/*.json` is gitignored (precheck enforced).
- Cleanup deletes `/tmp/sample.py` + `/tmp/sample.out` on srilu.

No code revert needed. Nothing was deployed.
