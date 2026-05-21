# Vendor Sample Decision Packet — Source-Call Price-Coverage Expansion

**Date:** 2026-05-21 (revised after PR #220 reviewer correction)
**BL:** `BL-NEW-SOURCE-CALL-PRICE-COVERAGE-EXPANSION` (DESIGN-SHIPPED, PR #208)
**Status:** Implementation gated on this packet's operator decision.
**Operator action required:** approve OR reject the **schema-verification** sample call described below. No code change ships from this packet itself.

## What changed in this revision

PR #220 reviewer flagged that an earlier draft of this packet:
- claimed GoldRush exposes an "OHLCV GraphQL endpoint" with candle close + volume,
- expected `data.items[].prices[].date` as a UTC ISO-8601 timestamp,
- assumed 30m candle granularity.

All three were wrong. Verified against:
- [GoldRush schema-change page (2026-03-19)](https://goldrush.mintlify.app/changelog/20260319-api-schema-improvements-for-new-api-keys)
- [QuickNode docs for `historical_by_addresses_v2`](https://www.quicknode.com/docs/ethereum/goldrush-wallet-api/v1-pricing-historical_by_addresses_v2-eth-quoteCurrency-contractAddress)

Confirmed reality:
1. **REST**, not GraphQL.
2. **Response field path depends on key vintage:**
   - Keys created **before 2026-04-01 16:00 UTC** → `data[].prices[]` (legacy schema).
   - Keys created **on/after 2026-04-01 16:00 UTC** → `data[].items[]` (new canonical schema; `data[].prices` was removed for new keys).
3. **`date` is date-only** (e.g., `"2025-04-29"`), NOT a UTC timestamp with `Z`/`+00:00`.
4. **No OHLCV.** Each price record has `price` + `pretty_price` only. No open/high/low/close/volume.
5. **Time range query params:** `from` / `to` (date strings) + `prices-at-asc=true|false`.
6. **Cost per call:** not specified in QuickNode docs; the GoldRush rate sheet should be confirmed by the operator before any production-grade backfill.

## Implication for the design's 30m primary horizon

The design (`tasks/design_source_call_price_coverage_expansion_2026_05_21.md`) commits to **30-minute as the primary horizon** for source-call forward returns. The GoldRush `historical_by_addresses_v2` endpoint **cannot satisfy this** — it returns daily price snapshots only.

This forces a decision on the operator BEFORE any vendor sample call is authorized:

| Path | Implication |
|---|---|
| **A. Sample a different GoldRush endpoint that exposes intraday data** | GoldRush *does* publish per-token spot pricing (`/v1/pricing/spot_prices/`) for real-time, but for **backfilling forward windows on historical source-calls**, the operator needs to identify whether GoldRush has a separate intraday/OHLCV product covering Solana memecoin contracts. If not documented in the operator's onboarding, treat this path as blocked. |
| **B. Re-scope the design's primary horizon to daily** | Forward_24h_pct is computable from `historical_by_addresses_v2`'s daily snapshots. Forward_30m / 1h / 6h would be dropped or marked permanently unavailable for the GoldRush path. Design + dashboard's `price_coverage` rendering would need a separate iteration. |
| **C. Sample CoinGecko MCP / GeckoTerminal onchain instead** | The design's Hermes-first table lists this as the tier-2 alternative. CoinGecko/GeckoTerminal onchain endpoints expose pool-level intraday candles for many memecoin pairs. Needs its own packet — schema TBD. |

**This packet recommends Path A's first step only:** verify whether GoldRush has a documented intraday endpoint the operator can sample, before any paid call. If no such endpoint exists in the operator's GoldRush plan, defer to Path B or C in a follow-up packet.

## Schema-verification sample (Path A, step 1)

**Goal of this sample:** verify the actual current response schema + temporal granularity of the GoldRush endpoint the operator's key targets. Decide whether daily granularity is acceptable before any production-grade plumbing.

### Pre-sample question for the operator (no API call yet)

> What is the creation date of your active GoldRush API key? This determines whether the response uses `data[].items[]` (≥2026-04-01) or `data[].prices[]` (legacy).

If unknown: the sample script must accept BOTH paths and report which fired.

### Endpoint to sample

```
GET https://api.covalenthq.com/v1/pricing/historical_by_addresses_v2/{chain_name}/USD/{token_address}/
```

### One concrete payload

```
chain_name: solana-mainnet
token_address: 5z3EqYQo9HiCEs3R84RCDMu2n7anpDMxRhdK8PSWmrRC   # POPCAT — present in prod's source_calls
from: 2026-05-15
to:   2026-05-15                                              # single-day window
quote-currency: USD
&prices-at-asc=true
```

Headers:
```
Authorization: Bearer ${GOLDRUSH_API_KEY}
```

Note: `from` / `to` are date strings per QuickNode docs, not ISO-8601 timestamps.

## Expected response fields — branch by key vintage

The sample script must inspect the response and report **which schema fired**, regardless of which the operator's key uses.

### If new-key schema (`data[].items[]`)

| Field | Purpose |
|---|---|
| `data[].items[].contract_decimals` | Validate decimal handling vs prod's stored decimals. |
| `data[].items[].quote_currency` | Confirm USD. |
| `data[].items[].prices` OR similar — verify by inspection | The schema-change page says `data[].items` is canonical but does not enumerate what's inside. The sample must record the exact JSON path to the price array. |
| Each price record's date / timestamp field | **Most critical:** confirms whether new-key schema has full timestamps or also date-only. |
| Each price record's price field | Verify numeric, > 0. |
| HTTP `X-RateLimit-*` headers | Confirm cost model. |

### If legacy-key schema (`data[].prices[]`)

| Field | Purpose |
|---|---|
| `data[].contract_decimals` | Validate decimal handling. |
| `data[].prices[].date` | Verify date-only string format (per QuickNode docs). |
| `data[].prices[].price` | Verify numeric. |
| `data[].prices[].pretty_price` | Display string, can ignore for our purposes. |
| HTTP `X-RateLimit-*` headers | Confirm cost model. |

## Accept / reject rubric

Acceptance requires BOTH:

1. **The sample response parses correctly** under either the new-key or legacy-key shape (recorded). No 4xx / 5xx.
2. **At least one of:**
   - (a) The response contains intraday timestamps (full ISO-8601 or epoch) AT MOST 30 minutes apart for the sampled day → 30m horizon is feasible; proceed to design implementation against this endpoint.
   - (b) The response is daily-only (per current QuickNode docs) → 30m horizon is **NOT** feasible against this endpoint; operator must explicitly authorize one of (i) re-scoping design's primary horizon to daily, (ii) deferring to a different GoldRush endpoint if one exists with intraday data, (iii) shifting to the CoinGecko MCP / GeckoTerminal sample track.

The decision in (b) is operator-only; the sample script returns the schema evidence and stops.

## Cost / rate-limit risk

| Risk | Mitigation |
|---|---|
| Credit overrun | This packet authorizes **1 call**. Production-grade backfill needs orders of magnitude more (~1 credit/token/day across thousands of source-call tokens) and is separately gated. |
| Vendor API key leakage | Sample script accepts key via stdin or 0600 tmpfile path, never CLI arg. Never `echo`/`set` it. Never write to git. |
| Key-vintage assumption error | Sample script branches on response shape; doesn't assume vintage. Records which fired in cached JSON. |
| Daily-only granularity surprises operator post-implementation | This packet now explicitly surfaces the issue. Path A step 1 confirms before any production-grade work. |
| Cost model not documented in QuickNode | Operator confirms credit-per-call from their GoldRush rate sheet before authorizing the call. |

## What this packet does NOT do

- Does not call any vendor API.
- Does not assume the operator's key vintage.
- Does not claim GoldRush has GraphQL, OHLCV, or volume data (it does not, per verified docs).
- Does not commit any vendor key, sample response, or expected-hash to git.
- Does not start implementation. The follow-up PR is gated on operator approval of this packet AND a successful sample evaluation AND (if daily-only) explicit horizon re-scope authorization.

## What changes if operator approves

1. Operator answers the pre-sample question (key vintage if known) + provides the credit-per-call cost from their rate sheet.
2. Operator provides `GOLDRUSH_API_KEY` via secret-hygiene (stdin / 0600 tmpfile, never CLI arg).
3. Future session writes the ~40 LOC sample script (`scripts/vendor_sample_goldrush.py`), invokes it once, records response under `tasks/vendor_samples/goldrush_popcat_2026_05_15.json` (gitignored).
4. Schema is documented in a follow-up note; acceptance/rejection per the rubric above.
5. If 30m horizon is feasible: implementation PR begins from the design doc.
6. If 30m horizon is infeasible: operator decides between re-scoping to daily, sampling a different GoldRush endpoint, or shifting to CoinGecko MCP sample track.
