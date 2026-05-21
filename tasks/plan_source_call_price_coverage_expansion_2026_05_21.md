**New primitives introduced:** NONE in this plan/design PR. Candidate future primitive for a later implementation PR: tier-labeled source-call historical price observations backed by an explicit chain+contract identity resolver and bounded forward-window evaluator.

## Hermes-first Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Installed Hermes on srilu | none found for GoldRush/Covalent, OHLCV, or historical token pricing; installed crypto skills are `coin_resolver`, `crypto_narrative_scanner`, `kol_watcher`, `narrative_classifier`, and `narrative_alert_dispatcher` | build gecko-alpha plan/design; no installed Hermes primitive can directly extend `source_calls` pricing |
| Hermes optional blockchain skills | yes - `blockchain/solana`, `blockchain/evm`, and `blockchain/base` in the public optional catalog at `https://hermes-agent.nousresearch.com/docs/reference/optional-skills-catalog`; these are wallet/token/RPC helpers, not source-call forward outcome storage | use as reference only; reject as coverage substrate because they do not provide timestamped OHLCV with source-call lineage |
| GoldRush/Covalent Hermes/MCP | yes - `https://goldrush.dev/agents/hermes-agent/`; available MCP pricing tools include `historical_token_prices` and `pool_spot_prices`, while OHLCV coverage comes from separate GoldRush GraphQL queries | accept for design evaluation as a first external candidate because GoldRush has explicit Hermes-agent integration docs; implementation requires operator approval for credentials/sample call |
| GoldRush OHLCV token/pair queries | yes - `https://goldrush.dev/docs/api-reference/streaming-api/queries/ohlcv-tokens-query` and `https://goldrush.dev/docs/api-reference/streaming-api/queries/ohlcv-pairs-query/` | accept as tier-2 candidate if timestamp, chain, token/pair address, volume, and pool context validate in a sample |
| CoinGecko/GeckoTerminal MCP/onchain OHLCV | yes - official CoinGecko MCP server at `https://docs.coingecko.com/docs/ai-agent-hub/mcp-server`; docs state onchain DEX price/liquidity across 8M+ tokens and 200+ networks plus historical price/OHLCV; CoinGecko skill exists at `https://docs.coingecko.com/docs/skills` | accept as MCP-aligned candidate; evaluate beside GoldRush in design, not downgraded for lack of MCP alignment |
| DexScreener public API | no Hermes skill found; official docs expose latest pairs/token pairs and liquidity fields but no historical OHLCV endpoint | reject for this coverage expansion; latest spot data cannot produce temporally valid historical forward returns unless gecko-alpha first starts caching it prospectively |

Awesome Hermes ecosystem check: `0xNyk/awesome-hermes-agent` lists Hermes infrastructure, registries, dashboards, and ops tools, but no maintained source-call price coverage skill or historical OHLCV workflow that owns gecko-alpha's DB/audit invariants. Verdict: GoldRush/Covalent and CoinGecko are both MCP-aligned candidates; GoldRush is first only because it has explicit Hermes-agent integration docs, not because CoinGecko lacks MCP support.

## Goal

Scope the safest next slice of `BL-NEW-SOURCE-CALL-PRICE-COVERAGE-EXPANSION` without inflating `coverage_rate` through false coverage. The first PR should be plan/design-only unless reviewers conclude a narrow docs/status cleanup is the only reviewer-clean implementation.

## Non-goals

- No trading behavior changes.
- No source ranking, pruning, KOL removal, or actionability consumption.
- No dashboard "best source" surface.
- No live config changes.
- No paid API calls during plan/design.
- No vendor sample calls without explicit operator approval.
- Do not touch `scout/source_quality/ledger.py::_fetch_snapshot_rows` in this PR.

## Drift-check Evidence

### In-tree source lookup

- `scout/source_quality/ledger.py:142` defines `_fetch_snapshot_rows`.
- `scout/source_quality/ledger.py:146-149` reads only `gainers_snapshots` and `losers_snapshots`, selecting `coin_id`, `price_at_snapshot`, `snapshot_at`, and a table name source label.
- `scout/source_quality/ledger.py:501-510` calls `_fetch_snapshot_rows` from `refresh_source_call_outcomes`.
- `scout/source_quality/ledger.py:182-200` enforces at-call timestamp order and stale-at-call suppression.
- `scout/source_quality/ledger.py:202-217` uses bounded forward windows.

Verdict: `_fetch_snapshot_rows` is still the only source-call forward-price lookup. It is intentionally narrow and should not be edited until the design is reviewer-clean.

### Existing tables with historical price-like data

- `scout/db.py:648-655` `price_cache`: `coin_id`, current spot price, market cap, and `updated_at`; no historical series and no source provenance.
- `scout/db.py:657-667` `volume_history_cg`: `coin_id`, `price`, and `recorded_at`; no liquidity or source-call provenance.
- `scout/db.py:671-684` `volume_spikes`: detected event rows with `price`, but not a continuous forward window source.
- `scout/db.py:688-700` `momentum_7d`: detected event rows with `current_price`, but not a continuous forward window source.
- `scout/db.py:704-717` `gainers_snapshots`: `coin_id`, `snapshot_at`, `price_at_snapshot`, market cap, and volume. This is the current tier-1 source.
- `scout/db.py:743-756` `losers_snapshots`: same price-bearing shape as gainers. This is the current tier-1 source.
- Prod schema also has `holder_snapshots`, `volume_snapshots`, and `wallet_snapshots`, but none combine token/contract identity, timestamped price, liquidity, and price-source provenance.

Verdict: no existing table beyond `gainers_snapshots`/`losers_snapshots` satisfies token or contract identity + timestamp + price + liquidity + source provenance.

### Existing source-call substrate

- `scout/db.py:3725-3788` creates `source_calls`.
- `scout/db.py:3749-3752` stores at-call price fields and `price_source`, but no trust tier and no liquidity/pool context.
- `scout/db.py:3762-3768` stores forward returns and 24h extrema.
- `scout/source_quality/ledger.py:528-595` computes source summaries using `min_sample=10`, `min_coverage_rate=0.50`, and the current rank label `rankable_resolvable_cg_board_cohort`.

Prod-copy evidence from `/tmp/scout_price_coverage_expansion_20260521024949.db`:

| Metric | Value |
|---|---:|
| `source_calls` rows | 1,253 |
| TG rows | 857 |
| X rows | 396 |
| rows with `price_at_call` | 14 |
| rows with 1h/6h/24h forward pct | 0 / 0 / 0 |
| `outcome_status='unresolvable'` | 1,237 |
| price sources observed | `losers_snapshots` 12, `gainers_snapshots` 2 |
| X rows with resolved coin id | 0 / 396 |
| X rows with extracted CA | 19 / 396 |
| TG rows with contract+chain | 208 / 857 |
| `gainers_snapshots` prod rows/tokens | 38,238 rows / 209 tokens |
| `losers_snapshots` prod rows/tokens | 37,339 rows / 197 tokens |

Missing-reason counts from prod-copy: `no_time_series=8245`, `pending_window=119`, `stale_at_call=90`, `sparse_forward_window=2`.

Verdict: current coverage is real but too narrow. The next slice must add identity-safe historical pricing; it must not relabel current spot data as historical coverage.

### Backlog/status drift

- `origin/master` is at `df76d851`, merged PR #207: `feat(source-calls): co-ship live-writer + lag-watchdog Telegram alerter + cron activation (#207)`.
- `backlog.md:11-18` still says `BL-NEW-SOURCE-CALL-OUTCOME-LEDGER` is `PR-OPEN 2026-05-20` on draft PR #206.
- `tasks/todo.md:3-15` still has the prior source-call ledger active work section, while master already includes the live writer/watchdog activation commit.

Verdict: docs/status cleanup is valid secondary work if the primary remains plan/design-only.

## Endpoint Coverage Matrix

| Candidate | Historical OHLCV vs spot | Timestamp semantics | Chain coverage | Pool/DEX aggregation | Liquidity fields | Rate limits/cost | EVM/Solana support | Verdict |
|---|---|---|---|---|---|---|---|---|
| Current CG board snapshots | Historical point snapshots only | source-side `snapshot_at` in `gainers_snapshots`/`losers_snapshots` | CoinGecko coin_id, not chain-native | pre-aggregated CG board | market cap and volume, no pool liquidity | already in tree | chain identity indirect | ACCEPT tier-1, already live, limited coverage |
| GoldRush `ohlcvCandlesForToken` | historical OHLCV, one-shot response | candle `timestamp` ISO-8601 | BASE, BSC, ETH, HYPERCORE, HYPEREVM, MEGAETH, MONAD, POLYGON, SOLANA | token-level, with `pair_address` for backing pool | `volume`, `volume_usd`; pool address present; explicit reserve/liquidity not shown in docs | beta, credit cost TBD, docs say no credits currently charged | EVM + Solana listed | ACCEPT tier-2 candidate pending one approved sample and cost guard |
| GoldRush `ohlcvCandlesForPair` | historical OHLCV, one-shot response | candle `timestamp` ISO-8601 | BASE, BSC, ETH, HYPERCORE, HYPEREVM, MEGAETH, MONAD, POLYGON, SOLANA | pair-level, primary DEX pool | `volume`, `volume_usd`; pair address and base/quote metadata | beta, credit cost TBD, docs say no credits currently charged | EVM + Solana listed | ACCEPT tier-2 candidate for pool-confirmed rows |
| GoldRush `searchToken` | current/spot discovery | no historical candle timestamp | BASE, BSC, ETH, MEGAETH, MONAD, POLYGON, SOLANA | sorted by volume; returns pair address | `volume_usd`, `market_cap`, quote/base metadata | beta, credit cost TBD | EVM + Solana listed | ACCEPT only as identity/pair discovery, not forward coverage |
| CoinGecko MCP + GeckoTerminal onchain token/pool OHLCV | historical OHLCV via MCP/API | `[timestamp, open, high, low, close, volume]`; `before_timestamp` supports history; MCP docs advertise historical/OHLCV access | 200+ MCP networks / 250+ API networks via CG onchain docs | token-level aggregated across pools and pool-level OHLCV | token/pool docs expose reserve/liquidity fields such as `total_reserve_in_usd` and `reserve_in_usd` | Demo/Pro credits and rate limits apply; no paid calls allowed here | EVM + Solana via networks | ACCEPT as MCP-aligned candidate; compare against GoldRush in design |
| GeckoTerminal public pool OHLCV | historical OHLCV by pool | epoch seconds per FAQ | GT networks | pool-level only after pool discovery | pool ranking uses `reserve_in_usd` and `volume_usd` | public API likely rate-limited; exact plan impact needs docs/sample | EVM + Solana supported by GT networks | DEFER tier-3 candidate; lower trust and no Hermes-first advantage |
| DexScreener public API | latest spot/pair state only in official reference | no historical candle timestamp | `chainId` path | pair/token-pairs latest state | `liquidity`, `fdv`, `marketCap`, `volume`, `priceUsd` | 300 rpm for latest pair/token endpoints | EVM + Solana by `chainId` | REJECT for historical coverage; can only be future prospective cache |

## OHLCV Temporal Semantics

Do not treat OHLCV rows as instantaneous point prices. A candle can leak future information if the row's timestamp means candle open and the design reads `close`, `high`, or `low` before the candle has closed.

Required future fields:

- `candle_start_at`
- `candle_end_at`
- `available_at`
- `interval_sec`
- `price_basis` (`open`, `close`, or provider-confirmed non-leaking value)
- `provider_timestamp_semantics`

At-call rule: a price is valid only if `available_at <= call_ts`. If a provider cannot prove availability semantics, the row is missing coverage.

Forward-window rule: forward values must use candles whose selected `price_basis` is available inside the bounded horizon window. High/low extrema may be computed only after the full 24h observation window and must be labeled as path extrema, not at-horizon returns.

## Substrate Legitimacy Rules

Any implementation design must enforce these fields and checks before a row counts toward coverage:

- `available_at <= call_ts` for at-call OHLCV-derived price, or `snapshot_at <= call_ts` for true point snapshots.
- Forward windows are bounded by horizon-specific windows only.
- `price_source` and `trust_tier` are recorded.
- DEX-derived rows record `chain`, `contract_address`, `pool_or_pair_address`, `quote_token`, and liquidity/volume context.
- Low-liquidity, stale, ambiguous, or missing-pool rows become `missing_fields`, not covered outcomes.
- Consumers must pass a minimum trust tier and get no silent mixed-tier aggregate.

## Trust-tier Taxonomy

| Tier | Source | Use |
|---|---|---|
| tier-1 | CoinGecko board snapshots from `gainers_snapshots`/`losers_snapshots`; pre-aggregated and market-cap floored | current trusted baseline |
| tier-2 | GoldRush/Covalent historical OHLCV if sample confirms timestamped candle semantics, chain+contract identity, and pool context | proposed first external design target |
| tier-3 | GeckoTerminal/CoinGecko onchain or prospective DexScreener/GT caches when temporally valid but lower trust | defer until tier-2 design is exhausted |

Trust tier is only one dimension. Future rows must also expose `source_family`, `aggregation_mode` (`cg_board`, `token_aggregated`, `pair_pool`, `prospective_spot_cache`), `identity_method`, `temporal_granularity_sec`, and `liquidity_evidence_kind` so consumers cannot accidentally mix pair-level and token-aggregated measurements under one tier.

## Chain Identity Rules

- `dex:chain:contract` resolves only when chain and contract are both present and unambiguous.
- Symbol/cashtag-only rows resolve only if exactly one candidate exists on the claimed chain.
- X rows with only cashtag and no unambiguous chain remain unrankable.
- No "first CoinGecko match by symbol" fallback.
- Contract comparisons must preserve Solana case sensitivity and lowercase only EVM addresses.

## Backfill vs Forward Scope

Recommended scope: design for both, implement neither in this first PR.

- Existing 1,253 `source_calls` should be re-resolved only in an offline/backfill mode with `trust_tier`, `identity_confidence`, and `coverage_label` populated.
- Historical backfill summaries must report old baseline separately from new-source resolved rows so old CG-board-only interpretation is not contaminated.
- Forward writer design should preserve `source_calls` append/update behavior and avoid live trading/config changes.

## Proposed Next Slice

1. Keep this PR plan/design-only unless reviewers downgrade all implementation risk to trivial.
2. In design, specify a new read/write boundary that does not edit `_fetch_snapshot_rows` directly:
   - `source_call_price_observations` or equivalent sidecar table keyed by `source_call_id`, `trust_tier`, `price_source`, source-native identity, candle/availability fields, aggregation mode, and liquidity evidence.
   - A resolver that turns source-call identity into `chain + contract` only under the chain identity rules above.
   - A preview CLI that runs on a prod-copy DB and reports acceptance metrics without mutating prod.
3. GoldRush and CoinGecko/GeckoTerminal are both MCP-aligned candidates. Design should compare both; GoldRush can be tried first only if the operator prefers the Hermes-branded path and cost budget is explicit.
4. Implementation remains gated on one operator-approved sample call per vendor and reviewer-clean schema/test design.

## Operational Cost Guard

Plan/design phase budget: zero vendor API calls and zero paid credits.

Before implementation, the design must specify:

- sample-call budget: max one operator-approved call per vendor, with expected credit cost documented before execution;
- preview budget: max vendor calls, max credits, max wall-clock, and batching/caching strategy;
- historical backfill budget: default disabled; if enabled, max calls/credits must be approved before run;
- forward budget: max calls per cycle/hour/day, provider rate-limit behavior, and fail-closed behavior when rate/cost limits are hit;
- persistence rule: vendor responses used for coverage preview are cached in the prod-copy analysis artifact so repeated previews do not re-spend calls.

## Plan-review Fold

Critical and Important findings folded from the two plan-review vectors:

- OHLCV candles are not point observations. Design must record `candle_start_at`, `candle_end_at`, `available_at`, `interval`, and `price_basis`, and at-call coverage may use only data fully available before `call_ts`.
- The global `unresolvable_rate <= 80%` target conflicts with strict identity eligibility. Design must report total-population unresolved rate separately and judge coverage on a predeclared identity-eligible denominator.
- `coverage>=0.50` is currently 30m-only in `compute_source_quality_summary`; design must define primary and secondary horizon-specific coverage gates.
- Trust tier must not mix source family, aggregation mode, identity method, temporal granularity, and liquidity evidence.
- `min_sample=10` is only an engineering smoke gate, not statistical proof of source quality.
- CoinGecko MCP is MCP-aligned and must be evaluated beside GoldRush.
- Operational cost budgets must be explicit before any implementation or sample call.

## Acceptance Gate

The design is not promotable unless a prod-copy preview can report:

- total-population unresolvable rate, reported without being gamed by excluding hard rows;
- identity-eligible denominator, predeclared before vendor queries;
- identity-eligible unresolvable rate <= 80%;
- at least one source reaches `min_sample=10` and primary-horizon coverage >= 0.50 on the identity-eligible denominator;
- per-horizon coverage for 30m, 1h, 6h, and 24h, with the primary horizon explicitly named;
- covered-vs-uncovered bias tables by source type, identity kind, chain, and trust tier;
- confidence intervals or bootstrap intervals before any future source-quality judgment, ranking, pruning, or dashboard best-source surface;
- every counted row has temporal integrity, trust tier, and chain identity evidence.

A coverage win bought by relaxing any invariant does not count.

`min_sample=10` is a build-readiness smoke threshold only. It does not justify source ranking, KOL pruning, or actionability decisions.

## Verification Plan

- `uv run pytest --tb=short -q` after dependency/TLS issue is resolved.
- `uv run pytest tests/test_source_call_outcome_ledger.py tests/test_source_calls_live_writer.py tests/test_source_calls_lag_watchdog.py -q` if code is touched later.
- Prod-copy SQL only for coverage exploration; no prod DB writes.
- No vendor API call without operator approval.

## Reviewer Requests

Plan reviewers:

- Vector A: measurement/statistical validity, false coverage risk, trust-tier design.
- Vector B: Hermes-first/vendor correctness, operational cost, prod-read-only discipline.

Critical or Important findings must be folded before design.
