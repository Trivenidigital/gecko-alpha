**New primitives introduced:** `COINGECKO_MIDCAP_SCAN_*` config knobs, `fetch_midcap_gainers()` CoinGecko rank-band ingestion lane, and `last_raw_midcap_gainers` raw-row cache.

# CoinGecko Midcap Gainer Scan Design - 2026-05-14

## Goal

Close the exact Top Gainers miss class found in
`tasks/findings_top_gainers_gap_2026_05_14.md`: CoinGecko-listed mid-cap
tokens around rank 470-680 that are not top-volume and not trending, but show
large 24h gains. The target is signal discovery quality, not paper-trade volume.

## Drift-check

| Domain | Existing in tree? | Decision |
|---|---|---|
| CoinGecko top movers | Yes: `fetch_top_movers()` queries `market_cap_asc` and `volume_desc`. | Reuse helpers and raw-row flow; not sufficient for rank-band gainers. |
| CoinGecko volume breadth | Yes: `fetch_by_volume()` scans configurable top-volume pages. | Keep; does not cover low-volume mid-cap gainers found in the audit. |
| CoinGecko trending hydration | Yes: `fetch_trending()` hydrates `/search/trending` IDs through `/coins/markets`. | Keep; does not cover non-trending gainers. |
| Raw-market fan-in | Yes: `_combine_coin_market_rows()` feeds gainers/spikes/momentum/slow-burn/velocity. | Extend with one more raw-row cache. |
| Market-rank-band scan | No exact implementation found. | Build narrowly inside `scout/ingestion/coingecko.py`. |

## Hermes-first analysis

Evidence checked:

- Repo drift: `rg -n "BL-NEW-COINGECKO-MIDCAP-GAINER-SCAN|midcap|mid-cap|market_cap_desc|fetch_by_volume|fetch_top_movers|COINGECKO_VOLUME_SCAN_PAGES" backlog.md tasks docs scout tests`.
- Installed VPS skills: `ssh srilu-vps 'sudo -u gecko-agent -i hermes skills list'`.
- Installed VPS skill grep: searched `/home/gecko-agent/.hermes/skills/**/SKILL.md` for `coingecko`, `coins/markets`, `market_cap_desc`, `gainer`, `goldrush`, `covalent`, `moralis`, and `helius`.
- Public ecosystem: `0xNyk/awesome-hermes-agent`, Hermes docs skill hub, and `coingecko/skills`.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Installed VPS market-breadth runtime | None found. Enabled skills include X/KOL narrative, coin resolver, narrative classifier/dispatcher, but no CoinGecko, top-gainer, market-breadth, GoldRush, Covalent, Helius, or Moralis runtime ingestion skill. | Build inside existing gecko-alpha ingestion. |
| Public Hermes skill hub / ecosystem | CoinGecko Agent SKILL exists, but it is an API/reference skill for agents, not a deployed runtime writer into gecko-alpha's DB/scoring surfaces. | Use as endpoint reference, not runtime replacement. |
| CoinGecko `/coins/markets` API | Official endpoint supports `order=market_cap_desc`, `per_page`, `page`, and `price_change_percentage=1h,24h,7d`. | Use existing aiohttp/backoff/rate limiter. |
| X/KOL narrative signals | Existing Hermes `kol_watcher`, `narrative_classifier`, `coin_resolver`, and `narrative_alert_dispatcher` cover this. | Out of scope; do not duplicate social logic. |

Awesome/evolving ecosystem check: `0xNyk/awesome-hermes-agent` tracks Hermes
resources and registries such as HermesHub, but no listed production runtime
skill replaces this CoinGecko rank-band DB ingestion. The first-party
`coingecko/skills` package should be treated as API guidance.

One-sentence verdict: custom gecko-alpha code is justified because this is a
runtime ingestion/persistence gap, while Hermes currently offers only reference
knowledge for CoinGecko API usage.

## Runtime-state check

VPS `/root/gecko-alpha/.env` does not currently set `COINGECKO_API_KEY`, and
the running `gecko-pipeline` service sees the key as empty. No midcap-scan env
overrides exist yet. That means the code must have safe public-endpoint
defaults and remain within the existing CoinGecko limiter budget.

## Proposed implementation

Add `fetch_midcap_gainers(session, settings)` beside `fetch_by_volume()`.

Default behavior:

- Enabled by default but cadence-gated: every 3rd cycle by default.
- Narrow rank band: pages 2-4 of `/coins/markets` ordered by
  `market_cap_desc` (`per_page=250`), covering approximate ranks 251-1000.
- Keep rows only when all gates pass:
  - `market_cap_rank` is present and inside
    `COINGECKO_MIDCAP_SCAN_MIN_RANK..MAX_RANK`
  - `market_cap >= COINGECKO_MIDCAP_SCAN_MIN_MCAP`
  - `market_cap <= COINGECKO_MIDCAP_SCAN_MAX_MCAP`
  - `total_volume >= COINGECKO_MIDCAP_SCAN_MIN_VOLUME`
  - `price_change_percentage_24h >= COINGECKO_MIDCAP_SCAN_MIN_24H_CHANGE`
- Deduplicate by CoinGecko ID.
- Sort returned `CandidateToken`s by 24h gain descending and cap to
  `COINGECKO_MIDCAP_SCAN_MAX_TOKENS_PER_CYCLE`.
- Populate `last_raw_midcap_gainers` with only gated raw rows so downstream
  signal surfaces do not ingest the whole rank band.
- Clear `last_raw_midcap_gainers` at function start, on disabled/off-cadence
  runs, and when zero pages succeed. Stale raw rows must never replay into
  price cache, detectors, or candidate scoring after an outage.

Conservative defaults:

- `COINGECKO_MIDCAP_SCAN_ENABLED=True`
- `COINGECKO_MIDCAP_SCAN_INTERVAL_CYCLES=3`
- `COINGECKO_MIDCAP_SCAN_START_PAGE=2`
- `COINGECKO_MIDCAP_SCAN_PAGES=3`
- `COINGECKO_MIDCAP_SCAN_MIN_RANK=251`
- `COINGECKO_MIDCAP_SCAN_MAX_RANK=1000`
- `COINGECKO_MIDCAP_SCAN_MIN_24H_CHANGE=25.0`
- `COINGECKO_MIDCAP_SCAN_MIN_VOLUME=250_000.0`
- `COINGECKO_MIDCAP_SCAN_MIN_MCAP=10_000_000.0`
- `COINGECKO_MIDCAP_SCAN_MAX_MCAP=500_000_000.0`
- `COINGECKO_MIDCAP_SCAN_MAX_TOKENS_PER_CYCLE=20`

Rate budget:

- Existing scheduled CoinGecko calls per cycle: top movers 2, trending 1-2,
  volume scan 3, held-position refresh up to 1.
- New lane adds 3 calls every 3 cycles by default, or about 1 call/minute at a
  60s scan interval.
- Worst-case retry amplification is still bounded by the shared CoinGecko
  limiter/backoff; this lane must degrade by returning successful pages and
  clearing stale cache on total outage.

## Data flow

1. `run_cycle()` gathers `cg_fetch_midcap_gainers()` in parallel with existing
   ingestion lanes.
2. Exceptions are handled like other CoinGecko lanes.
3. `last_raw_midcap_gainers` joins price-cache input and `_combine_coin_market_rows()`.
4. The returned `CandidateToken`s are included in `aggregate()` alongside
   `cg_movers`, `cg_trending`, and `cg_by_volume`; otherwise midcap rows would
   reach tracker surfaces but not the normal enrich-score-gate path.
5. Existing gainers, volume spikes, momentum, slow-burn, velocity, candidate
   scoring, and dashboards receive the gated rows without schema changes.

## Tests

TDD targets:

1. `fetch_midcap_gainers()` filters rank-band pages by 24h change, volume, and
   market cap/rank, sorts by 24h gain, and caps output count.
2. Page-level failures preserve successful pages.
3. Disabled/off-cadence/no-data paths return no tokens and clear
   `last_raw_midcap_gainers`.
4. `run_cycle()` includes `last_raw_midcap_gainers` in raw-market fan-in and
   price cache.
5. `run_cycle()` includes returned midcap `CandidateToken`s in the normal
   aggregate/scoring path.
6. Config defaults are present and conservative.

Focused verification:

```bash
uv run pytest tests/test_coingecko.py tests/test_main.py tests/test_main_cryptopanic_integration.py tests/test_gainers_tracker.py tests/test_spikes_detector.py tests/test_slow_burn_detector.py -q
```

## Non-goals

- No paid `/coins/top_gainers_losers` endpoint.
- No new DB table.
- No new scoring signal.
- No X/KOL, TG alert, or paper-trade tuning in this diff.
- No provider replacement with Hermes, GoldRush, Moralis, or Helius.

## Acceptance

- Synthetic tests show at least 2 of the 3 audit miss shapes would pass the
  default gates: Playnance-like and SAFEbit-like. Bityuan-like may pass only
  when its 24h gain is at the screenshot value, not at the lower later audit
  value; that is acceptable because the gate is quality-over-quantity.
- Negative fixtures show low-volume, low-change, out-of-rank, and excessive
  row-count cases are filtered/capped.
- Focused tests pass or any failures are clearly unrelated baseline failures.
- Backlog/todo/memory record the shipped behavior and the Hermes-first basis.
