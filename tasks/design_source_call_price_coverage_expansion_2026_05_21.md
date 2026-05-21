**New primitives introduced:** DESIGN ONLY. Future implementation candidates: `source_call_price_observations` sidecar table, source-call identity resolution preview, vendor OHLCV preview cache, and trust-tier-aware coverage summary. No code/schema/config/runtime primitive is implemented in this PR.

## Hermes-first Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Installed Hermes on srilu | none found for GoldRush/Covalent, CoinGecko MCP, OHLCV, or historical token pricing in `/home/gecko-agent/.hermes`; installed crypto skills are narrative/resolver skills only | keep custom gecko-alpha DB/audit design |
| Hermes optional blockchain skills | yes - `blockchain/solana`, `blockchain/evm`, and `blockchain/base` | reject as coverage substrate; no source-call lineage or historical OHLCV storage |
| GoldRush/Covalent Hermes/MCP | yes - Hermes-agent integration plus MCP pricing tools and separate OHLCV GraphQL token/pair queries | tier-2 candidate after approved sample validates candle availability semantics |
| CoinGecko MCP + GeckoTerminal onchain | yes - official CoinGecko MCP plus onchain token/pool OHLCV and liquidity docs | MCP-aligned candidate; evaluate beside GoldRush |
| DexScreener public API | no official historical OHLCV in reference; latest spot/pair state only | reject for historical backfill; possible future prospective tier-3 cache only |

Awesome Hermes ecosystem check: no awesome-hermes-agent entry owns gecko-alpha source-call outcome pricing, trust tiers, chain identity, or durable DB audit trail. Verdict: use vendor MCP/skills only as data access/reference; gecko-alpha must own the persistence and measurement invariants.

## Build Decision

Do not build code in this PR.

Reason: the design introduces a new price-observation substrate, provider-specific candle semantics, identity resolution, vendor cost controls, and coverage accounting changes. That is not trivial, and plan review already found two Critical false-coverage risks. The safest next slice is a reviewed design plus status cleanup only.

## Design Goal

Expand `source_calls` price coverage without false coverage by adding a separate historical price-observation layer that can be previewed on a prod-copy DB before any production schema or lookup path changes.

North-star invariant: a row counts as covered only when identity, timestamp availability, trust tier, and liquidity/pool context are all explicit.

## Current State

- `source_calls` exists and is live.
- `refresh_source_call_outcomes` still uses `_fetch_snapshot_rows`.
- `_fetch_snapshot_rows` reads only `gainers_snapshots` and `losers_snapshots`.
- Prod-copy coverage is not acceptable today: 1,253 `source_calls`, 14 at-call prices, zero 1h/6h/24h forward returns, 1,237 unresolvable.
- X identity is the main blocker: 396 X rows, zero `resolved_coin_id`, 19 extracted CAs.
- TG has more chain-native shape: 857 rows, 208 with contract+chain.

## Proposed Future Schema

This schema is for a later implementation PR, not this PR.

```sql
CREATE TABLE source_call_price_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_call_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    price_source TEXT NOT NULL,
    observation_key TEXT NOT NULL,
    coverage_label TEXT NOT NULL CHECK (
        coverage_label IN ('cg_board_baseline','external_historical_preview','forward_external')
    ),
    trust_tier TEXT NOT NULL CHECK (trust_tier IN ('tier-1','tier-2','tier-3')),
    source_family TEXT NOT NULL,
    aggregation_mode TEXT NOT NULL CHECK (
        aggregation_mode IN ('cg_board','token_aggregated','pair_pool','prospective_spot_cache')
    ),
    identity_method TEXT NOT NULL,
    identity_confidence TEXT NOT NULL CHECK (
        identity_confidence IN ('direct_chain_contract','single_candidate_on_claimed_chain','ambiguous','unresolved')
    ),
    canonical_chain TEXT,
    provider_network_id TEXT,
    contract_address TEXT,
    resolution_as_of TEXT,
    resolution_as_of_epoch INTEGER,
    candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
    provider_query_id TEXT,
    provider_query_hash TEXT,
    pool_or_pair_address TEXT,
    quote_token_symbol TEXT,
    quote_token_address TEXT,
    liquidity_usd REAL,
    volume_usd REAL,
    liquidity_evidence_kind TEXT NOT NULL CHECK (
        liquidity_evidence_kind IN ('reserve_usd','volume_usd_only','market_cap_volume_only','none')
    ),
    interval_sec INTEGER,
    provider_timestamp TEXT,
    provider_timestamp_semantics TEXT NOT NULL,
    candle_start_at TEXT,
    candle_start_epoch INTEGER,
    candle_end_at TEXT,
    candle_end_epoch INTEGER,
    available_at TEXT NOT NULL,
    available_epoch INTEGER NOT NULL,
    price_basis TEXT NOT NULL CHECK (price_basis IN ('point','open','close','vwap')),
    price_usd REAL,
    missing_reason TEXT,
    raw_ref TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (source_call_id) REFERENCES source_calls(id) ON DELETE RESTRICT,
    UNIQUE (source_call_id, observation_key),
    CHECK (
        (price_usd IS NOT NULL AND price_usd > 0 AND missing_reason IS NULL)
        OR
        (price_usd IS NULL AND missing_reason IS NOT NULL)
    ),
    CHECK (liquidity_usd IS NULL OR liquidity_usd >= 0),
    CHECK (volume_usd IS NULL OR volume_usd >= 0),
    CHECK (interval_sec IS NULL OR interval_sec > 0)
);

CREATE INDEX idx_scpo_call_available
    ON source_call_price_observations(source_call_id, available_at);

CREATE INDEX idx_scpo_identity
    ON source_call_price_observations(canonical_chain, contract_address, available_epoch);
```

### Why Sidecar

- Avoids editing `_fetch_snapshot_rows` in the first PR.
- Preserves tier-1 CG-board behavior as the existing baseline.
- Lets previews compare provider candidates without polluting `source_calls`.
- Allows multiple observations per source call while summaries can enforce minimum tier and aggregation mode filters.

## Temporal Rules

Point snapshots:

- valid at-call if `snapshot_at <= call_ts`;
- stale if older than the horizon-specific max age;
- current tier-1 CG board snapshots remain under this rule.

OHLCV candles:

- `provider_timestamp_semantics` must be documented from provider docs or sample response.
- `available_at` must be computed conservatively as candle close time unless the provider explicitly proves earlier availability.
- at-call coverage is valid only when `available_at <= call_ts`.
- at-call coverage also needs a provider-specific max age and max interval. Defaults: 30m horizon max age 15m; 1h max age 30m; 6h/24h max age 60m, matching current ledger intent. Wider candles are missing coverage unless the design explicitly proves they are acceptable for that horizon.
- at-call `price_basis='close'` is allowed only for a candle fully closed before `call_ts`.
- `price_basis='open'` may be used for a candle containing `call_ts` only if provider semantics prove open price was known at candle start and no post-call high/low/close is read for at-call.
- forward returns use the first valid observation whose `available_at` is inside the bounded horizon window.
- 24h max favorable/adverse extrema may use high/low only after the entire 24h window has matured, and must be labeled path-extrema, not horizon return.

If any provider cannot prove these semantics, its rows are `missing_reason='unproven_timestamp_semantics'`.

## Identity Rules

Direct identity:

- TG rows with `chain` + `contract_address` can become `identity_confidence='direct_chain_contract'`.
- EVM contracts are lowercased for comparison.
- Solana contracts preserve case.

Single-candidate identity:

- Symbol/cashtag-only rows can resolve only with a claimed chain and exactly one provider candidate on that chain as of `call_ts`.
- The design must record `resolution_as_of`, candidate count, provider query id/hash, and token/pair existence plus liquidity evidence at or before `call_ts`.
- Today's provider search results cannot resolve historical symbol-only calls unless the provider can answer the candidate set as of `call_ts`.

Ambiguous identity:

- Multiple candidates on the claimed chain stay unrankable.
- No claimed chain stays unrankable unless there is direct contract identity.
- No first-match-by-symbol or first-CoinGecko-match fallback.

Chain namespace:

- Store `canonical_chain` using gecko-alpha chain IDs such as `ethereum`, `base`, and `solana`.
- Store provider-specific network IDs separately, such as GeckoTerminal `eth` or provider Solana IDs.
- Same contract address on different EVM chains is not the same asset unless `canonical_chain` also matches.

Current prod-copy implication:

- The first preview should focus on the 208 TG contract+chain rows and 19 X CA rows.
- The 380 X cashtag rows are reported in total denominators but excluded from identity-eligible coverage until deferred resolution improves identity.

## Trust and Filter Model

Trust tier:

- `tier-1`: current CG-board snapshots from `gainers_snapshots` and `losers_snapshots`.
- `tier-2`: GoldRush or CoinGecko MCP/onchain OHLCV only after timestamp semantics and identity are sample-validated.
- `tier-3`: GeckoTerminal public pool OHLCV or future prospective DexScreener/GT cache, if temporally valid but lower trust.

Additional required dimensions:

- `source_family`: `cg_board`, `goldrush`, `coingecko_mcp`, `geckoterminal_public`, `dexscreener_cache`.
- `aggregation_mode`: `cg_board`, `token_aggregated`, `pair_pool`, `prospective_spot_cache`.
- `identity_method`: `tg_direct_contract`, `x_direct_ca`, `symbol_single_candidate`, `unresolved`, `ambiguous`.
- `liquidity_evidence_kind`: `reserve_usd`, `volume_usd_only`, `market_cap_volume_only`, `none`.

Consumers must pass:

- minimum trust tier;
- allowed source families;
- allowed aggregation modes;
- allowed identity confidence values.

No summary may silently mix pair-pool and token-aggregated rows under one trust tier.

Trust-tier ordering is an explicit map, not lexical string comparison:

```python
TRUST_TIER_RANK = {"tier-1": 1, "tier-2": 2, "tier-3": 3}
```

Consumers compare rank values, not the raw string labels.

## Vendor Preview Flow

Plan/design phase:

- zero vendor calls;
- no prod DB writes;
- docs/schema inspection only.

Operator-approved sample phase:

- max one call per vendor;
- record endpoint, request shape, response timestamp semantics, response identity fields, liquidity/volume fields, credit cost, and rate-limit headers if present;
- cache the raw sample under `tasks/vendor_samples/` or another gitignored operator-approved artifact path if it contains no secrets.

Prod-copy preview phase:

1. Copy prod `scout.db` to `/tmp`.
2. Select identity-eligible rows:
   - TG direct contract+chain;
   - X direct CA+chain;
   - symbol/cashtag rows only if a claimed chain exists and exactly one candidate is returned.
3. Query cached/vendor-approved observations under explicit call budget.
4. Populate preview sidecar tables in the prod-copy only.
5. Compute at-call and forward coverage using temporal rules.
6. Emit JSON/Markdown summary with total-population and identity-eligible denominators.

Forward phase:

- disabled until preview passes acceptance;
- if enabled later, writes observations first, then a separate outcome refresher reads observations with explicit filters.

## Operational Cost Guard

Required before implementation:

| Mode | Budget |
|---|---|
| plan/design | 0 vendor calls, 0 paid credits |
| sample | max 1 approved call per vendor |
| preview | explicit max calls/credits/wall-clock in the CLI arguments; default dry-run refuses network |
| historical backfill | disabled by default; requires operator-approved budget and cached resume state |
| forward | max calls per cycle/hour/day; fail closed on provider errors, 429s, or cost ceiling |

No code may default to live vendor calls. Any preview command must require `--allow-network` plus provider-specific budget flags.

## Coverage Metrics

Report these denominators separately:

- total source-call rows;
- total unresolved rows;
- identity-eligible rows;
- vendor-query-eligible rows;
- at-call covered rows;
- per-horizon covered rows for 30m, 1h, 6h, and 24h;
- low-liquidity/stale/ambiguous rows as missing fields.

Primary engineering smoke gate:

- identity-eligible unresolvable rate <= 80%;
- at least one source reaches `min_sample=10`;
- primary horizon is fixed to 30m before preview results are known;
- 30m coverage >= 0.50 on identity-eligible clusters;
- 1h, 6h, and 24h coverage are reported as secondary horizon coverage and cannot be hidden if poor.

Statistical guard:

- `min_sample=10` is not source-quality proof.
- Before source ranking/pruning/actionability use, require bootstrap intervals, covered-vs-uncovered bias tables, and stratification by source type, identity kind, chain, trust tier, and aggregation mode.

## Backfill Interpretation

Historical re-resolution must not rewrite the interpretation of the old CG-board-only baseline.

Design requirement:

- store `coverage_label='cg_board_baseline'`, `coverage_label='external_historical_preview'`, or equivalent;
- summaries show baseline and expanded-source cohorts separately;
- any old row newly resolved by external historical data is labeled as expanded-source, not silently merged into the old baseline.

## Future Migration Shape

- Migration helper name: `_migrate_source_call_price_observations_v1`.
- Migration marker: `paper_migrations.name='bl_source_call_price_observations_v1'`.
- Schema version: choose the next free integer at implementation time and assert collision behavior, matching `_migrate_source_calls_v1`.
- Call order: after `_migrate_source_calls_v1` in `Database.initialize()` because the sidecar references `source_calls`.
- Idempotence: rerunning migration and preview/backfill must preserve row counts via `UNIQUE(source_call_id, observation_key)`.

## Tests for Future Implementation

- Migration idempotence for sidecar table and indexes.
- Preview/backfill idempotence: same cached vendor response rerun does not duplicate observations.
- At-call OHLCV close candle after `call_ts` is rejected.
- At-call closed candle before `call_ts` is accepted.
- Candle containing `call_ts` cannot use `close/high/low` at call.
- Old candle outside max staleness is missing coverage.
- Forward window rejects observations outside bounded window.
- Solana identity remains case-sensitive.
- EVM identity normalizes lowercase.
- Same EVM contract on `ethereum` and `base` does not collide.
- Symbol-only multiple candidates stays ambiguous.
- Symbol-only today's single candidate cannot resolve a historical call unless uniqueness is proven as of `call_ts`.
- Low-liquidity rows become missing fields, not coverage.
- Summary filters by minimum tier and aggregation mode.
- Network preview refuses vendor calls unless explicit budget flags are set.

## Design-review Fold

Critical and Important findings folded from design review:

- Historical symbol/cashtag resolution must be as-of `call_ts`; today's provider candidate set cannot resolve old rows.
- The schema now includes candidate count, provider query identity/hash, resolution time, canonical/provider chain IDs, coverage label, liquidity evidence kind, and an idempotent observation key.
- OHLCV at-call coverage requires provider-specific max age/interval constraints in addition to `available_at <= call_ts`.
- Timestamps intended for SQL ordering get canonical UTC text plus epoch seconds.
- Rows have an explicit valid-vs-missing CHECK shape and basic positive/non-negative value constraints.
- Primary horizon is fixed to 30m before preview.
- Future migration order/version/idempotence is specified.

## Intentional Non-build List

- No `_fetch_snapshot_rows` changes.
- No production migration.
- No source-call outcome recompute against external data.
- No dashboard surface.
- No source ranking, pruning, KOL removal, or actionability use.
- No live config or vendor credentials.
- No paid API calls.

## Design-review Requests

Vector A: temporal leakage, chain identity, backfill contamination.

Vector B: schema/API/runtime simplicity, migration risk if any, testability.

Critical or Important findings must be folded before PR.
