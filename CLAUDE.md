# gecko-alpha

**Standalone CoinGecko early pump detection pipeline.** This is NOT coinpump-scout.
Scaffold was copied from `C:\projects\coinpump-scout` — that project must NEVER be modified.

## Architecture

6-stage async pipeline:
1. **Ingestion** — CoinGecko (primary) + DexScreener + GeckoTerminal in parallel via `asyncio.gather()`
2. **Aggregation** — Dedup by contract_address
3. **Scoring** — 11 quantitative signals (normalized 178→100, co-occurrence multiplier)
4. **MiroFish** — Narrative simulation with Claude haiku-4-5 fallback
5. **Gate** — Conviction threshold: `quant * 0.6 + narrative * 0.4`
6. **Alert** — Telegram (required) + Discord (optional)

## CoinGecko Layer

**Endpoints (free Demo tier only, 30 req/min):**
- `GET /coins/markets` — sorted by 1h change, top 50
- `GET /search/trending` — trending coins list

**Net-new Scoring Signals (post-scaffold):**
| Signal | Condition | Points (raw) | Config Key |
|--------|-----------|--------------|------------|
| momentum_ratio | 1h/24h price change > 0.6 | +20 | MOMENTUM_RATIO_THRESHOLD |
| vol_acceleration | volume / 7d_avg > 5.0 | +25 | MIN_VOL_ACCEL_RATIO |
| cg_trending_rank | rank <= 10 in trending | +15 | — |
| stable_paired_liq | quote_symbol ∈ {USDC,USDT,DAI,FDUSD,USDe,PYUSD,RLUSD,sUSDe} AND liquidity_usd ≥ $50K | +5 (≈+2 normalized) | STABLE_PAIRED_BONUS / STABLE_PAIRED_LIQ_THRESHOLD_USD / STABLE_QUOTE_SYMBOLS |

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

### Plan/Design Document Conventions

Every plan, design, or spec document under `tasks/` matching `plan_*.md`,
`design_*.md`, or `spec_*.md` MUST begin with:

`**New primitives introduced:** [list, or NONE]`

Mechanically enforced by `.claude/hooks/check-new-primitives.py` — the hook
blocks any `Write` / `Edit` / `MultiEdit` / `NotebookEdit` to a gated file
that lacks the line. The marker is matched case-insensitively, ignoring
formatting variations (`**New Primitives Introduced:**`, missing bold,
extra whitespace) so typos don't block. Markers inside ```code fences```
do NOT count — they must appear in real prose.

If a file matches the gated pattern but isn't a real plan (e.g., scratch
notes accidentally named `plan_x.md`), include the bypass comment:
`<!-- new-primitives-check: bypass -->`. Bypasses are logged to
`.claude/hooks/bypass.log` for PR-time review.

For deployed-pattern reference (so you don't reinvent existing primitives),
see `docs/gecko-alpha-alignment.md`.

**Important limitation:** the hook checks the marker EXISTS. It does NOT
validate that the listed primitives are TRUTHFUL or COMPLETE. Human PR
review verifies accuracy.

## What NOT To Do

- No global aiohttp sessions (pass session as parameter)
- No synchronous HTTP calls (no `requests` library — aiohttp only)
- No hardcoded thresholds (must come from Settings / .env)
- No committing `.env` or files with real API keys
- Never call `/coins/top_gainers_losers` (paid Pro endpoint)
- No `os.getenv()` in business logic (use Settings)
- Do not auto-bump Telethon in dependabot PRs — upstream is archived (Feb 2026); manual review required for every version change. Fallback path is fork-at-pinned-version, NOT Hydrogram (GPL + pre-1.0). See BL-064 spec.
- Never commit `*.session` files — they authenticate as the user (full Telegram identity); treat as secret material, mode 0600, exclude from backup tarballs.
- Do not pass user-supplied or signal-name strings to `alerter.send_telegram_message` with the default `parse_mode="Markdown"`. Signal names contain `_` (`gainers_early`, `hard_loss`, `trending_catch`) which Telegram MarkdownV1 parses as italics markers, mangling the message body without returning an error. Pass `parse_mode=None` for system-health alerts, OR `_escape_md(value)` for user-data fields inside intentionally-formatted messages. See global CLAUDE.md §12b for the rule; `scout/trading/auto_suspend.py` for the worked example.
- Every automated state change reversing operator-applied state must emit `*_alert_dispatched` + `*_alert_delivered` structured logs around the Telegram call. The default alerter logs only on failure, so success is silent — making "no logs" ambiguous between "delivered cleanly" and "skipped." See global CLAUDE.md §12b.

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
  scorer.py             # 11-signal quantitative scorer
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
