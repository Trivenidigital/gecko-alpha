# gecko-alpha

**Standalone CoinGecko early pump detection pipeline.** This is NOT coinpump-scout.
Scaffold was copied from `C:\projects\coinpump-scout` — that project must NEVER be modified.

## Architecture

6-stage async pipeline:
1. **Ingestion** — CoinGecko (primary) + DexScreener + GeckoTerminal in parallel via `asyncio.gather()`
2. **Aggregation** — Dedup by contract_address
3. **Scoring** — 8 quantitative signals (0-100, capped)
4. **MiroFish** — Narrative simulation with Claude haiku-4-5 fallback
5. **Gate** — Conviction threshold: `quant * 0.6 + narrative * 0.4`
6. **Alert** — Telegram (required) + Discord (optional)

## CoinGecko Layer

**Endpoints (free Demo tier only, 30 req/min):**
- `GET /coins/markets` — sorted by 1h change, top 50
- `GET /search/trending` — trending coins list

**3 New Scoring Signals:**
| Signal | Condition | Points | Config Key |
|--------|-----------|--------|------------|
| momentum_ratio | 1h/24h price change > 0.6 | +20 | MOMENTUM_RATIO_THRESHOLD |
| vol_acceleration | volume / 7d_avg > 5.0 | +25 | MIN_VOL_ACCEL_RATIO |
| cg_trending_rank | rank <= 10 in trending | +15 | — |

**Rate limiting:** Tracks call timestamps, enforces 30/min. Exponential backoff on 429 (2s, 4s, 8s).

## Key Commands

```bash
uv run pytest --tb=short -q              # Run all tests
uv run python -m scout.main --dry-run --cycles 1  # Dry run
uv run black scout/ tests/               # Format code
```

## Coding Conventions

- **Async everywhere** — aiohttp for HTTP, aiosqlite for DB, asyncio.gather() for parallelism
- **Pydantic v2** — BaseSettings for config (.env), BaseModel for data
- **structlog** — JSON structured logging, no print()
- **TDD** — Write failing test first, then implement
- **Dependency injection** — Pass settings/session as args, no global state
- **Type hints** on all public functions
- **Domain exceptions** — Raise from scout.exceptions, never swallow silently

## What NOT To Do

- No global aiohttp sessions (pass session as parameter)
- No synchronous HTTP calls (no `requests` library — aiohttp only)
- No hardcoded thresholds (must come from Settings / .env)
- No committing `.env` or files with real API keys
- Never call `/coins/top_gainers_losers` (paid Pro endpoint)
- No `os.getenv()` in business logic (use Settings)

## MiroFish Integration

- Timeout: 180s (MIROFISH_TIMEOUT_SEC)
- On timeout/connection error: fallback to Claude haiku-4-5 (scout/mirofish/fallback.py)
- Max 50 jobs/day enforced in gate.py
- Never block alerts waiting for MiroFish

## Test Patterns

- **aioresponses** for HTTP mocks (DexScreener, GeckoTerminal, CoinGecko, GoPlus)
- **pytest-asyncio** auto mode (asyncio_mode = "auto" in pyproject.toml)
- **tmp_path** for DB fixtures (aiosqlite)
- **conftest.py** fixtures: `settings_factory()`, `token_factory()` with override support
- Every public function gets a corresponding test
- Existing scaffold tests must never regress

## Project Structure

```
scout/
  ingestion/
    dexscreener.py      # DexScreener boosts poller
    geckoterminal.py    # GeckoTerminal trending pools
    coingecko.py        # CoinGecko markets + trending (NEW)
  mirofish/
    client.py           # MiroFish REST client
    fallback.py         # Claude haiku fallback
    seed_builder.py     # Build simulation seed
  models.py             # CandidateToken + MiroFishResult
  config.py             # Pydantic BaseSettings
  scorer.py             # 8-signal quantitative scorer
  aggregator.py         # Dedup
  gate.py               # Conviction gate
  alerter.py            # Telegram + Discord delivery
  safety.py             # GoPlus security check
  db.py                 # Async SQLite layer
  main.py               # Pipeline orchestrator
tests/
  conftest.py           # Shared fixtures
  test_*.py             # One per module
```
