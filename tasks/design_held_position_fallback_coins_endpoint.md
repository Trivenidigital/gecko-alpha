**New primitives introduced:** Proposed `fetch_missing_held_position_prices_via_coins_endpoint` fallback helper; proposed `held_position_coins_fallback_summary` structured log event; no DB schema changes.

# BL-NEW-HELD-POSITION-FALLBACK-COINS-ENDPOINT Design

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| CoinGecko price lookup | yes — optional Hermes blockchain skills mention CoinGecko-backed price lookup (`https://hermes-agent.nousresearch.com/docs/reference/optional-skills-catalog/`) | not a replacement; gecko-alpha needs an in-process held-position refresh fallback that writes existing `price_cache` rows and respects pipeline rate limits |
| EVM/on-chain token pricing | yes — Hermes EVM skill has CoinGecko-backed price lookup (`https://hermes-agent.nousresearch.com/docs/user-guide/skills/optional/blockchain/blockchain-evm`) | not applicable to CoinGecko coin-id held positions across all chains; useful as reference only |
| Project-local held-position cache fallback | none found | design minimal in-tree fallback |

awesome-hermes-agent ecosystem check: reviewed `https://github.com/0xNyk/awesome-hermes-agent`; no listed skill/plugin replaces gecko-alpha's held-position `price_cache` refresh lane. Verdict: custom fallback remains justified if implemented narrowly.

## Evidence Gate

Manual VPS probe on 2026-05-18 after prior 429 window:

```text
ID=pythia
HTTP=200
coin_id=pythia
usd=0.04042913
ID=iagon
HTTP=200
coin_id=iagon
usd=0.02314992
ID=superwalk
ERROR=HTTPError:HTTP Error 429: Too Many Requests
```

Interpretation:
- `/coins/{id}` can recover at least some stale ids missed by `/simple/price`.
- Rate limit remains material; fallback must be capped and diagnostics-first.
- Implementation should still wait for PR #158 post-deploy `simple_price_missing_ids` evidence so the fallback targets live misses, not stale historical assumptions.

## Minimal Runtime Shape

Only run fallback for held-position token ids that:
- were requested from `/simple/price`,
- were not returned by `/simple/price`,
- are present in open paper trades,
- pass `_is_cg_coin_id`, and
- are still stale after excluding ids returned by the current `/simple/price` response.

Proposed cap:
- `HELD_POSITION_COINS_FALLBACK_MAX_IDS_PER_CYCLE = 3`
- default enabled only after explicit operator flag, or ship disabled with dry-run logging first if implementation risk is judged non-trivial.

Proposed request:
- `GET /coins/{id}?localization=false&tickers=false&market_data=true&community_data=false&developer_data=false&sparkline=false`
- Parse `market_data.current_price.usd`.
- Convert into the same raw coin shape used by `db.cache_prices(all_raw)` so the existing cache write path remains the only writer.

## Observability

Emit one summary event per fallback attempt:

```text
held_position_coins_fallback_summary
requested_ids=[...]
recovered_ids=[...]
not_recovered_ids=[...]
rate_limited_count=N
error_count=N
```

Do not add Telegram alerting here. `BL-NEW-HELD-POSITION-STALE-COUNT-ALERT` remains baseline-first and separate.

## Test Plan

- `/simple/price` misses `pythia`; `/coins/pythia` returns USD; resulting raw coin is included in the caller's `cache_prices(all_raw)` payload.
- Fallback does not run for ids that `/simple/price` returned in the same cycle.
- Fallback respects max ids per cycle.
- Fallback handles 429 without raising and logs `rate_limited_count`.
- Fallback handles HTTP 404 / missing `market_data.current_price.usd` as not recovered.
- Fallback is disabled by default if the implementation ships behind a flag.

## Promotion Gate

Promote from design to implementation only after PR #158 is deployed and at least one post-deploy cycle shows non-empty `simple_price_missing_ids`.

Implement if:
- at least one live-missing id is recovered by `/coins/{id}` with usable USD price, and
- expected additional CoinGecko calls are within free-tier headroom.

Defer if:
- `/coins/{id}` returns 429 consistently before recovering any live-missing id,
- recovered payload lacks USD price,
- or post-deploy `simple_price_missing_ids` is empty.
