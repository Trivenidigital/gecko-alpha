# gecko-alpha

CoinGecko early pump detection pipeline with MiroFish narrative simulation. Scans DexScreener, GeckoTerminal, and CoinGecko for micro-cap tokens, scores them across 11 quantitative signals, runs narrative analysis via Claude haiku-4-5, and alerts to Telegram when conviction thresholds are met.

## Quick Start

```bash
git clone https://github.com/Trivenidigital/gecko-alpha.git && cd gecko-alpha
cp .env.example .env   # Edit with your API keys
uv sync --all-extras
```

### Run the pipeline

```bash
uv run python -m scout.main                          # Live mode (sends Telegram alerts)
uv run python -m scout.main --dry-run                 # Dry run (no alerts sent)
uv run python -m scout.main --dry-run --cycles 1      # Single cycle test
uv run python -m scout.main --min-score-override 20   # Override MIN_SCORE for testing
```

### Run the dashboard

```bash
cd dashboard/frontend && npm install && npm run build && cd ../..
uv run uvicorn dashboard.main:app --port 8000
# Open http://localhost:8000
```

### Run both at once

```bash
./start.sh
```

## Architecture

6-stage async pipeline:

1. **Ingestion** — CoinGecko (dual query: market_cap_asc + volume_desc) + DexScreener + GeckoTerminal in parallel via `asyncio.gather()`
2. **Aggregation** — Dedup by contract_address, preserve enrichment fields (trending rank, price changes)
3. **Scoring** — 11 quantitative signals (normalized 178 → 100 scale, co-occurrence multiplier)
4. **MiroFish** — Narrative simulation with Claude haiku-4-5 fallback (calibrated rubric, quantitative context)
5. **Gate** — Conviction score = quant × 0.6 + narrative × 0.4, threshold ≥ 22
6. **Alerter** — Telegram + Discord delivery with GoPlus safety check + duplicate suppression

### Scoring Signals (11)

| Signal | Points | Source |
|--------|--------|--------|
| vol_liq_ratio | 30 | DexScreener/GeckoTerminal |
| market_cap_range | 2-8 (tiered) | All sources |
| holder_growth | 25 | Helius/Moralis |
| token_age | 0-10 (bell curve) | DexScreener/GeckoTerminal |
| social_mentions | 15 | (Phase 5) |
| buy_pressure | 15 | DexScreener txns |
| momentum_ratio | 20 | DexScreener/CoinGecko |
| vol_acceleration | 25 | DB rolling 7d avg |
| cg_trending_rank | 15 | CoinGecko trending |
| solana_bonus | 5 | Chain detection |
| score_velocity | 10 | DB score history |

### Current Thresholds

- `MIN_SCORE=25` — Minimum quant score to trigger narrative analysis
- `CONVICTION_THRESHOLD=22` — Minimum conviction to fire alert
- `MIN_LIQUIDITY_USD=15000` — Hard disqualifier below this

### Dashboard

React + Vite frontend served by FastAPI at `localhost:8000`. Five panels: stat bar, pipeline funnel, candidates table with signal badges, signal hit rate bars, and alert feed with outcome tracking. WebSocket live updates every 5 seconds.

### Daily Summary

Telegram digest fires at midnight UTC with: alerts fired, win rate (4h+ window), top signal combination, and top 3 conviction tokens.

## Testing

```bash
uv run pytest --tb=short -q              # Full suite (148 tests)
uv run pytest tests/test_scorer.py -v    # Scorer only
uv run pytest tests/test_dashboard_api.py # Dashboard API only
```

## Configuration

All settings via environment variables — see `.env.example` for the full list.

## Disclaimer

This is a research and alerting tool. Alerts do not constitute financial advice. Always do your own research.
