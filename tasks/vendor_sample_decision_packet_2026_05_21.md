# Vendor Sample Decision Packet — Source-Call Price-Coverage Expansion

**Date:** 2026-05-21
**BL:** `BL-NEW-SOURCE-CALL-PRICE-COVERAGE-EXPANSION` (DESIGN-SHIPPED, PR #208)
**Status:** Implementation gated on this packet's operator decision.
**Operator action required:** approve OR reject the single sample call described below. No code change ships from this packet itself.

## Background (1 paragraph)

PR #208 shipped a design for extending `source_calls` forward-window price coverage so per-source ranking has the data to be honest. Current prod state: 1316 `source_calls` rows, 14 with `price_at_call`, **zero** with `forward_30m_pct` / `forward_1h_pct` / `forward_6h_pct` / `forward_24h_pct`. The dashboard's `not_rankable_label` reads "no sources rankable yet — 20 below min_sample=10" — partly because no source has the required coverage. Per the design's plan/design review folds, the smallest reversible next step is one approved vendor sample call to validate timestamp semantics + candle availability before any persistent code lands.

## The decision

**Authorize exactly ONE call to ONE vendor for the test payload below**, OR reject and re-park the design. The packet does not authorize a second call.

## Recommended primary vendor: GoldRush (formerly Covalent)

Reasoning:
- Both GoldRush and CoinGecko MCP/onchain are tier-2 candidates per the design's Hermes-first table. GoldRush's OHLCV GraphQL endpoint exposes both candle timestamps and explicit candle-boundary semantics in a single payload.
- The design's plan review flagged "candle availability semantics" as the critical unknown that distinguishes acceptable from unacceptable vendors. GoldRush returns the candle boundary timestamp + close price + volume in a single record, which the design's `provider_timestamp_semantics` field needs to record verbatim.
- Free tier covers ~100k credits/month; one sample call is rate-limit-safe.
- API key required (operator-provisioned; not in this packet).

CoinGecko MCP/onchain remains the fallback if GoldRush sample fails validation.

## The exact sample call

### Endpoint
```
GET https://api.covalenthq.com/v1/pricing/historical_by_addresses_v2/{chain_name}/USD/{token_address}/
```

### Single, concrete payload (operator may swap token_address if a different test target is preferred)

```
chain_name: solana-mainnet
token_address: 5z3EqYQo9HiCEs3R84RCDMu2n7anpDMxRhdK8PSWmrRC   # POPCAT — a paper-trade winner present in prod's source_calls
from: 2026-05-15T00:00:00Z
to:   2026-05-15T00:30:00Z
quote-currency: USD
&prices-at-asc=true
```

Headers:
```
Authorization: Bearer ${GOLDRUSH_API_KEY}
```

### Expected single-call cost
- 1 credit per address per quote-currency per day (per published rate sheet).
- Token-address × 1 day × 1 quote → **1 credit**, well under free-tier daily budget.

## Expected response fields (the 6 to validate)

| Field | Why critical |
|---|---|
| `data.items[].contract_decimals` | Required to convert raw amounts. Must match prod's stored `decimals` for the token, or the price column needs a decimal-conversion step. |
| `data.items[].prices[].date` | Candle boundary timestamp. **MUST be UTC ISO-8601.** This is the `provider_timestamp_semantics` validation. |
| `data.items[].prices[].price` | The numeric price. Must be a number, not a string-wrapped number. |
| `data.items[].prices[].source` | If absent: vendor doesn't expose per-candle source; we synthesize as "goldrush". If present: pass through to `trust_tier` derivation. |
| `error` (top-level) | If `true`, we record `vendor_error` reason in the design's `missing_fields` JSON. |
| HTTP rate-limit headers (`X-RateLimit-Remaining`, `X-RateLimit-Reset`) | Validates the free-tier budget model. |

## Cost / rate-limit risk

| Risk | Mitigation |
|---|---|
| Credit overrun (production-grade backfill needs ~10k credits) | This packet authorizes 1 call; the design's `--allow-network` + provider-specific budget flags gate the production path. The sample is not the backfill. |
| Vendor API key leakage | Operator-side: paste into a 0600-mode tmpfile, never `echo`/`set` it. Sample script (when written) accepts key via stdin or file path, not CLI arg. |
| Timezone drift (candle timestamps in local time) | Validation criterion below: reject sample if any timestamp lacks `Z` or `+00:00` suffix. |
| Price = 0 or null for the sampled token at the sampled time | Re-run with a different token (e.g., SOL) before rejecting GoldRush entirely. |

## Temporal-integrity validation criteria

The sample is ACCEPTED only if all 5 hold:

1. **Boundary timestamps are UTC.** `data.items[].prices[].date` parses as ISO-8601 with explicit `+00:00` or `Z`. No naïve datetimes.
2. **Candle alignment matches the design's 30m primary horizon.** The sample's price list either includes a 00:30:00 boundary candle, OR the vendor explicitly documents the candle interval and the operator confirms the interval can map to 30m.
3. **`contract_decimals` matches prod's stored decimals** for the sampled token (cross-checked via `sqlite3 scout.db "SELECT decimals FROM ... WHERE token_id='...'"`).
4. **Price is numeric, > 0.** No string-wrapped numbers; no nulls for the sampled minute.
5. **Rate-limit headers report remaining > 99% of daily budget after the call.** Confirms the cost model.

## Accept / reject rubric

| Outcome | Action |
|---|---|
| All 5 criteria pass | File a follow-up PR (design implementation, code) that wires GoldRush as the primary OHLCV provider. Implementation MUST gate on `--allow-network` + budget flags per the design doc. |
| Criterion 1 fails (non-UTC) | Reject GoldRush. Sample the CoinGecko MCP/onchain endpoint instead. |
| Criterion 2 fails (no 30m mapping) | Reject GoldRush for source-call horizons; consider for daily-only coverage in a later iteration. Re-park design. |
| Criterion 3 fails (decimal mismatch) | Add a decimal-normalization layer to the design, then re-evaluate sample. |
| Criterion 4 fails (price null / 0) | Re-run sample with SOL token. If still fails, reject GoldRush. |
| Criterion 5 fails (budget burn > 1%) | Reject GoldRush — the cost model is wrong. |

## What this packet does NOT do

- Does not call any vendor API.
- Does not commit any vendor key, sample response, or expected-hash to git.
- Does not start implementation. The follow-up PR is gated on operator approval of this packet AND a successful sample evaluation.

## What changes if operator approves

1. Operator provides `GOLDRUSH_API_KEY` via secret-hygiene (stdin / 0600 tmpfile, never CLI arg).
2. Future session runs the sample script (TBD; ~30 LOC).
3. Sample response cached under `tasks/vendor_samples/goldrush_popcat_2026_05_15_30m.json` (gitignored).
4. Validation criteria 1-5 above evaluated; result documented in a follow-up note.
5. If accepted: implementation PR begins from `tasks/design_source_call_price_coverage_expansion_2026_05_21.md` §implementation order.
6. If rejected: re-park; consider CoinGecko MCP sample (new packet).
