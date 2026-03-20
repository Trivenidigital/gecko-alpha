# CoinPump Scout

AI-powered pre-pump cryptocurrency token detection system combining quantitative DEX signals with MiroFish multi-agent narrative simulation.

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) for dependency management
- Docker (for MiroFish simulation engine)
- Telegram bot token (for alerts)

## Quick Start

1. Clone and install:
   ```bash
   git clone <repo-url> && cd coinpump-scout
   cp .env.example .env
   # Edit .env with your API keys
   uv sync --all-extras
   ```

2. Start MiroFish (required for narrative scoring):
   ```bash
   docker compose up -d
   curl http://localhost:5001/health  # verify
   ```

3. Run the scanner:
   ```bash
   uv run python -m scout.main
   ```

   Options:
   - `--dry-run` — run pipeline without sending alerts
   - `--cycles N` — run N cycles then exit (0 = infinite)

## Testing

```bash
uv run pytest                          # full suite
uv run pytest tests/test_scorer.py -v  # single file
```

## Architecture

6-stage async pipeline:

1. **Ingestion** — DexScreener + GeckoTerminal polling + Helius/Moralis holder enrichment
2. **Aggregation** — Dedup by contract address, normalize to CandidateToken
3. **Scoring** — 5-signal quantitative model (volume/liquidity, market cap, holder growth, age, social)
4. **MiroFish** — Multi-agent narrative simulation with Claude haiku fallback
5. **Gate** — Conviction score = quant × 0.6 + narrative × 0.4, threshold ≥ 70
6. **Alerter** — Telegram + Discord delivery with GoPlus safety check

## Configuration

All settings via environment variables — see `.env.example` for the full list.

## Disclaimer

This is an internal research and alerting tool. Alerts do not constitute financial advice.
