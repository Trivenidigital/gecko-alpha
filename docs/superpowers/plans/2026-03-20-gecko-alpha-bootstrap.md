# gecko-alpha Bootstrap & CoinGecko Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bootstrap gecko-alpha as a standalone project from the coinpump-scout scaffold, then implement the CoinGecko early pump detection layer (3 new scoring signals, 1 new ingestion module, model + config additions, alerter updates).

**Architecture:** 6-stage async pipeline (Ingestion → Aggregation → Scoring → MiroFish → Gate → Alert). CoinGecko is added as a third ingestion source alongside DexScreener and GeckoTerminal. Three new scoring signals (momentum_ratio, vol_acceleration, cg_trending_rank) are additive to the existing 5-signal scorer.

**Tech Stack:** Python 3.12, aiohttp, pydantic-v2 BaseSettings, aiosqlite, structlog, pytest-asyncio, aioresponses, uv

**Source PRD:** `docs/gecko-alpha_PRD_and_CC_Prompt.docx`

---

## Phase 1: Project Bootstrap (Steps 1, 2, 3, 4, 5, 6)

### Task 1: Copy scaffold and initialise git repo

**Files:**
- Copy: entire `C:\projects\coinpump-scout\*` → `C:\projects\gecko-alpha\`
- Modify: `pyproject.toml` (change name to gecko-alpha)
- Skip: `.git/` directory (fresh repo)

- [ ] **Step 1: Copy scaffold files**

```bash
cd /c/projects/gecko-alpha
cp -r /c/projects/coinpump-scout/* .
cp -r /c/projects/coinpump-scout/.env.example . 2>/dev/null
cp -r /c/projects/coinpump-scout/.gitignore . 2>/dev/null
# Do NOT copy .git/ — gecko-alpha gets its own repo
```

- [ ] **Step 2: Update pyproject.toml — change project name**

Change `name = "coinpump-scout"` to `name = "gecko-alpha"`. Keep all dependencies identical.

- [ ] **Step 3: Initialise fresh git repo**

```bash
git init
git add .gitignore
git commit -m "chore: initialise gecko-alpha repo"
```

- [ ] **Step 4: Install dependencies**

```bash
uv sync --all-extras
```

- [ ] **Step 5: Run scaffold tests — establish green baseline**

```bash
uv run pytest --tb=short -q
```

Expected: ALL existing scaffold tests pass. This is the green baseline. If any fail, fix before proceeding.

- [ ] **Step 6: Commit scaffold as baseline**

```bash
git add -A
git commit -m "chore: import coinpump-scout scaffold as gecko-alpha baseline"
```

---

### Task 2: Write .claude/settings.json (hooks config)

**Files:**
- Create: `.claude/settings.json`

- [ ] **Step 1: Create .claude directory and write settings.json**

NOTE: The hooks matcher syntax below follows the PRD spec. The exact schema may need adjustment against Claude Code's actual hooks API — verify after writing. The file is placed so hooks activate automatically once the environment supports them.

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Write(*.py)",
        "command": "uv run python -m py_compile $file"
      },
      {
        "matcher": "Write(*.py)",
        "command": "uv run black --check --quiet $file"
      },
      {
        "matcher": "Write(tests/*.py)",
        "command": "uv run pytest $file --tb=short -q"
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Bash(*rm -rf*)",
        "exitCode": 2
      },
      {
        "matcher": "Bash(*git push --force*)",
        "exitCode": 2
      },
      {
        "matcher": "Write(.env)",
        "exitCode": 2
      }
    ],
    "Stop": [
      {
        "command": "uv run pytest --tb=short -q 2>&1 | tail -5"
      }
    ]
  }
}
```

- [ ] **Step 2: Commit**

```bash
git add .claude/settings.json
git commit -m "chore: add Claude Code hooks configuration"
```

---

### Task 3: Write .claude/mcp.md (MCP server install runbook)

**Files:**
- Create: `.claude/mcp.md`

- [ ] **Step 1: Write MCP documentation file**

Document all 5 MCP servers with exact install commands for manual execution.

- [ ] **Step 2: Commit**

```bash
git add .claude/mcp.md
git commit -m "docs: add MCP server install runbook"
```

---

### Task 4: Write CLAUDE.md

**Files:**
- Create (overwrite scaffold copy): `CLAUDE.md`

- [ ] **Step 1: Write CLAUDE.md (≤180 lines)**

Must contain all sections from PRD Step 4:
- Project identity (gecko-alpha, NOT coinpump-scout)
- Origin (scaffold copied, coinpump-scout never modified)
- Architecture (6-stage pipeline with CoinGecko primary)
- Key commands
- CoinGecko layer (3 signals, 2 free-tier endpoints)
- Coding conventions
- What NOT to do
- MiroFish fallback
- Test patterns

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: write gecko-alpha CLAUDE.md with CoinGecko context"
```

---

### Task 5: Create subagent definitions

**Files:**
- Create: `.claude/agents/ingestion-agent.md`
- Create: `.claude/agents/scorer-agent.md`
- Create: `.claude/agents/mirofish-agent.md`
- Create: `.claude/agents/db-agent.md`
- Create: `.claude/agents/safety-agent.md`
- Create: `.claude/agents/qa-agent.md`

- [ ] **Step 1: Write all 6 agent files** (per PRD Step 5 specs)
- [ ] **Step 2: Commit**

```bash
git add .claude/agents/
git commit -m "chore: add 6 project-scoped subagent definitions"
```

---

### Task 6: Create slash commands

**Files:**
- Create: `.claude/commands/scan.md`
- Create: `.claude/commands/score.md`
- Create: `.claude/commands/backtest.md`
- Create: `.claude/commands/status.md`
- Create: `.claude/commands/qa.md`

- [ ] **Step 1: Write all 5 command files** (per PRD Step 6 specs)
- [ ] **Step 2: Commit**

```bash
git add .claude/commands/
git commit -m "chore: add 5 slash command definitions"
```

---

## Phase 2: TDD Implementation (Step 9)

### Task 7: Add 4 new fields to CandidateToken + from_coingecko()

**Files:**
- Modify: `scout/models.py`
- Modify: `tests/test_models.py`

- [ ] **Step 1: Write failing tests in tests/test_models.py**

Add these tests:

```python
def test_candidate_token_cg_fields_default_none():
    """New CG fields default to None."""
    token = CandidateToken(
        contract_address="0xabc",
        chain="eth",
        token_name="TestCoin",
        ticker="TEST",
    )
    assert token.price_change_1h is None
    assert token.price_change_24h is None
    assert token.vol_7d_avg is None
    assert token.cg_trending_rank is None


def test_from_coingecko_parses_fields():
    """from_coingecko() maps CoinGecko API response to CandidateToken."""
    raw = {
        "id": "bitcoin",
        "symbol": "btc",
        "name": "Bitcoin",
        "market_cap": 1_000_000_000,
        "total_volume": 50_000_000,
        "price_change_percentage_1h_in_currency": 5.2,
        "price_change_percentage_24h": 12.1,
    }
    token = CandidateToken.from_coingecko(raw)
    assert token.ticker == "btc"
    assert token.token_name == "Bitcoin"
    assert token.market_cap_usd == 1_000_000_000
    assert token.volume_24h_usd == 50_000_000
    assert token.price_change_1h == 5.2
    assert token.price_change_24h == 12.1
    assert token.holder_count == 0
    assert token.holder_growth_1h == 0
    assert token.token_age_days == 0.0


def test_from_coingecko_missing_optional_fields():
    """from_coingecko() handles missing optional fields gracefully."""
    raw = {
        "id": "somecoin",
        "symbol": "some",
        "name": "SomeCoin",
        "market_cap": 100_000,
        "total_volume": 5_000,
    }
    token = CandidateToken.from_coingecko(raw)
    assert token.price_change_1h is None
    assert token.price_change_24h is None
```

- [ ] **Step 2: Run tests — verify they FAIL**

```bash
uv run pytest tests/test_models.py -v -k "cg_fields or from_coingecko"
```

Expected: FAIL (fields don't exist yet, no from_coingecko method)

- [ ] **Step 3: Add 4 new fields to CandidateToken in scout/models.py**

Add after existing fields (before quant_score):

```python
# CoinGecko-specific fields
price_change_1h: float | None = None
price_change_24h: float | None = None
vol_7d_avg: float | None = None
cg_trending_rank: int | None = None
```

- [ ] **Step 4: Add from_coingecko() classmethod to CandidateToken**

```python
@classmethod
def from_coingecko(cls, raw: dict) -> "CandidateToken":
    """Create a CandidateToken from a CoinGecko /coins/markets response item."""
    cg_id = raw.get("id", "unknown")
    return cls(
        contract_address=cg_id,
        chain="coingecko",
        token_name=raw.get("name", "Unknown"),
        ticker=raw.get("symbol", "???"),
        market_cap_usd=float(raw.get("market_cap") or 0),
        volume_24h_usd=float(raw.get("total_volume") or 0),
        price_change_1h=raw.get("price_change_percentage_1h_in_currency"),
        price_change_24h=raw.get("price_change_percentage_24h"),
        liquidity_usd=0.0,
        token_age_days=0.0,
        holder_count=0,
        holder_growth_1h=0,
    )
```

- [ ] **Step 5: Run tests — verify they PASS**

```bash
uv run pytest tests/test_models.py -v -k "cg_fields or from_coingecko"
```

- [ ] **Step 6: Run full test suite — no regressions**

```bash
uv run pytest --tb=short -q
```

- [ ] **Step 7: Commit**

```bash
git add scout/models.py tests/test_models.py
git commit -m "feat: add CoinGecko fields and from_coingecko() to CandidateToken"
```

---

### Task 8: Write tests/test_coingecko.py (all 5 tests must FAIL)

**Files:**
- Create: `tests/test_coingecko.py`

- [ ] **Step 1: Write all 5 test cases from PRD section 9.1**

```python
"""Tests for CoinGecko ingestion module."""
import pytest
import aiohttp
from aioresponses import aioresponses

from scout.config import Settings
from scout.ingestion.coingecko import fetch_top_movers, fetch_trending
from scout.models import CandidateToken


# -- Fixtures --

COINS_MARKETS_RESPONSE = [
    {
        "id": "pump-token",
        "symbol": "pump",
        "name": "PumpToken",
        "market_cap": 200_000,
        "total_volume": 500_000,
        "price_change_percentage_1h_in_currency": 8.5,
        "price_change_percentage_24h": 12.0,
    },
    {
        "id": "tiny-cap",
        "symbol": "tiny",
        "name": "TinyCap",
        "market_cap": 500,  # below MIN_MARKET_CAP
        "total_volume": 100,
        "price_change_percentage_1h_in_currency": 20.0,
        "price_change_percentage_24h": 25.0,
    },
]

TRENDING_RESPONSE = {
    "coins": [
        {"item": {"id": "pump-token", "symbol": "pump", "name": "PumpToken", "market_cap_rank": 150, "score": i}}
        for i in range(15)
    ]
}

CG_BASE = "https://api.coingecko.com/api/v3"


# -- Tests --

@pytest.mark.asyncio
async def test_fetch_top_movers_parses_correctly():
    """FR-01: /coins/markets response parsed into CandidateToken with correct fields."""
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_CHAT_ID="test",
        ANTHROPIC_API_KEY="test",
        MIN_MARKET_CAP=1000,
        MAX_MARKET_CAP=1_000_000,
    )
    with aioresponses() as mocked:
        mocked.get(f"{CG_BASE}/coins/markets", payload=COINS_MARKETS_RESPONSE)
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_top_movers(session, settings)

    # tiny-cap filtered out by market cap
    assert len(tokens) == 1
    t = tokens[0]
    assert t.ticker == "pump"
    assert t.token_name == "PumpToken"
    assert t.market_cap_usd == 200_000
    assert t.volume_24h_usd == 500_000
    assert t.price_change_1h == 8.5
    assert t.price_change_24h == 12.0


@pytest.mark.asyncio
async def test_fetch_trending_populates_rank():
    """FR-02: /search/trending populates cg_trending_rank on returned tokens."""
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_CHAT_ID="test",
        ANTHROPIC_API_KEY="test",
    )
    with aioresponses() as mocked:
        mocked.get(f"{CG_BASE}/search/trending", payload=TRENDING_RESPONSE)
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_trending(session, settings)

    assert len(tokens) > 0
    assert tokens[0].cg_trending_rank == 0
    assert tokens[1].cg_trending_rank == 1


@pytest.mark.asyncio
async def test_429_triggers_backoff():
    """FR-03: HTTP 429 triggers exponential backoff, retries, and eventually succeeds."""
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_CHAT_ID="test",
        ANTHROPIC_API_KEY="test",
        MIN_MARKET_CAP=1000,
        MAX_MARKET_CAP=1_000_000,
    )
    with aioresponses() as mocked:
        # First call: 429, second call: 200
        mocked.get(f"{CG_BASE}/coins/markets", status=429)
        mocked.get(f"{CG_BASE}/coins/markets", payload=COINS_MARKETS_RESPONSE)
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_top_movers(session, settings)

    assert len(tokens) == 1
    assert tokens[0].ticker == "pump"


@pytest.mark.asyncio
async def test_market_cap_filter_applied():
    """FR-01: Tokens outside MIN/MAX_MARKET_CAP are excluded."""
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_CHAT_ID="test",
        ANTHROPIC_API_KEY="test",
        MIN_MARKET_CAP=100_000,
        MAX_MARKET_CAP=300_000,
    )
    with aioresponses() as mocked:
        mocked.get(f"{CG_BASE}/coins/markets", payload=COINS_MARKETS_RESPONSE)
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_top_movers(session, settings)

    # pump-token (200k) passes, tiny-cap (500) filtered
    assert len(tokens) == 1
    assert tokens[0].ticker == "pump"


@pytest.mark.asyncio
async def test_coingecko_outage_does_not_crash_pipeline():
    """NFR: CoinGecko API outage returns empty list, does not raise."""
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_CHAT_ID="test",
        ANTHROPIC_API_KEY="test",
    )
    with aioresponses() as mocked:
        # Non-429 errors (500) return None immediately on first attempt — no retry
        mocked.get(f"{CG_BASE}/coins/markets", status=500)
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_top_movers(session, settings)

    assert tokens == []
```

- [ ] **Step 2: Run tests — verify all FAIL**

```bash
uv run pytest tests/test_coingecko.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'scout.ingestion.coingecko'`

- [ ] **Step 3: Commit failing tests**

```bash
git add tests/test_coingecko.py
git commit -m "test: add 5 failing CoinGecko ingestion tests (red phase)"
```

---

### Task 9: Implement scout/ingestion/coingecko.py

**Files:**
- Create: `scout/ingestion/coingecko.py`

- [ ] **Step 1: Implement the CoinGecko ingestion module**

```python
"""CoinGecko ingestion module — polls /coins/markets and /search/trending."""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from scout.models import CandidateToken

if TYPE_CHECKING:
    import aiohttp
    from scout.config import Settings

logger = structlog.get_logger()

CG_BASE = "https://api.coingecko.com/api/v3"
MAX_RETRIES = 3
_call_timestamps: list[float] = []


async def _throttle() -> None:
    """Enforce 30 calls/min rate limit for CoinGecko free Demo tier."""
    now = time.monotonic()
    # Remove timestamps older than 60 seconds
    while _call_timestamps and _call_timestamps[0] < now - 60:
        _call_timestamps.pop(0)
    if len(_call_timestamps) >= 30:
        sleep_time = 60 - (now - _call_timestamps[0])
        if sleep_time > 0:
            logger.warning("cg_rate_limit_hit", sleep_seconds=round(sleep_time, 1))
            await asyncio.sleep(sleep_time)
    _call_timestamps.append(time.monotonic())


async def _get_with_backoff(
    session: aiohttp.ClientSession,
    url: str,
    params: dict | None = None,
) -> dict | list | None:
    """GET with exponential backoff on 429. Returns parsed JSON or None."""
    for attempt in range(MAX_RETRIES + 1):
        await _throttle()
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 429:
                    backoff = 2 ** (attempt + 1)
                    logger.warning("cg_429_backoff", attempt=attempt, backoff_s=backoff)
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(backoff)
                        continue
                    return None
                if resp.status >= 400:
                    logger.warning("cg_http_error", status=resp.status, url=url)
                    return None
                return await resp.json()
        except Exception as exc:
            logger.warning("cg_request_error", error=str(exc), url=url)
            return None
    return None


async def fetch_top_movers(
    session: aiohttp.ClientSession,
    settings: Settings,
) -> list[CandidateToken]:
    """Poll /coins/markets sorted by 1h change. Returns filtered CandidateTokens."""
    params = {
        "vs_currency": "usd",
        "order": "percent_change_1h_desc",
        "per_page": "50",
        "page": "1",
        "sparkline": "false",
        "price_change_percentage": "1h,24h",
    }
    data = await _get_with_backoff(session, f"{CG_BASE}/coins/markets", params)
    if not data or not isinstance(data, list):
        logger.warning("cg_no_data", endpoint="coins/markets")
        return []

    tokens: list[CandidateToken] = []
    for raw in data:
        token = CandidateToken.from_coingecko(raw)
        # Apply market cap filter
        if token.market_cap_usd is not None:
            if token.market_cap_usd < settings.MIN_MARKET_CAP:
                continue
            if token.market_cap_usd > settings.MAX_MARKET_CAP:
                continue
        else:
            continue  # Skip tokens with no market cap data
        tokens.append(token)

    logger.info("cg_candidates_fetched", count=len(tokens), source="coins/markets")
    return tokens


async def fetch_trending(
    session: aiohttp.ClientSession,
    settings: Settings,
) -> list[CandidateToken]:
    """Poll /search/trending. Returns tokens with cg_trending_rank set."""
    data = await _get_with_backoff(session, f"{CG_BASE}/search/trending")
    if not data or not isinstance(data, dict):
        logger.warning("cg_no_data", endpoint="search/trending")
        return []

    coins = data.get("coins", [])
    tokens: list[CandidateToken] = []
    for rank, entry in enumerate(coins[:15]):
        item = entry.get("item", {})
        cg_id = item.get("id", "unknown")
        token = CandidateToken(
            contract_address=cg_id,
            chain="coingecko",
            token_name=item.get("name", "Unknown"),
            ticker=item.get("symbol", "???"),
            cg_trending_rank=rank,
            holder_count=0,
            holder_growth_1h=0,
        )
        tokens.append(token)

    logger.info("cg_candidates_fetched", count=len(tokens), source="search/trending")
    return tokens
```

- [ ] **Step 2: Run CoinGecko tests — verify all PASS**

```bash
uv run pytest tests/test_coingecko.py -v
```

- [ ] **Step 3: Run full test suite — no regressions**

```bash
uv run pytest --tb=short -q
```

- [ ] **Step 4: Commit**

```bash
git add scout/ingestion/coingecko.py tests/test_coingecko.py
git commit -m "feat: implement CoinGecko ingestion module with rate limiting and backoff"
```

---

### Task 10: Add config knobs

**Files:**
- Modify: `scout/config.py`
- Modify: `.env.example`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing test for new config fields**

Add to `tests/test_config.py`:

```python
def test_coingecko_config_defaults(settings_factory):
    """CoinGecko config knobs have correct defaults."""
    s = settings_factory()
    assert s.MOMENTUM_RATIO_THRESHOLD == 0.6
    assert s.MIN_VOL_ACCEL_RATIO == 5.0
```

- [ ] **Step 2: Run test — verify FAIL**

```bash
uv run pytest tests/test_config.py -v -k "coingecko_config"
```

- [ ] **Step 3: Add fields to Settings in scout/config.py**

```python
# CoinGecko signal thresholds
MOMENTUM_RATIO_THRESHOLD: float = 0.6
MIN_VOL_ACCEL_RATIO: float = 5.0
```

- [ ] **Step 4: Add to .env.example**

```
# CoinGecko Signal Thresholds
MOMENTUM_RATIO_THRESHOLD=0.6      # 1h/24h price ratio gate for momentum signal
MIN_VOL_ACCEL_RATIO=5.0            # Volume / 7d-avg ratio gate for vol acceleration signal
COINGECKO_API_KEY=                  # Optional: CoinGecko Demo API key (free tier)
```

- [ ] **Step 5: Run tests — verify PASS**

```bash
uv run pytest tests/test_config.py -v
```

- [ ] **Step 6: Commit**

```bash
git add scout/config.py .env.example tests/test_config.py
git commit -m "feat: add MOMENTUM_RATIO_THRESHOLD and MIN_VOL_ACCEL_RATIO config knobs"
```

---

### Task 11: Add 3 new scoring signals — tests first

**Files:**
- Modify: `tests/test_scorer.py`
- Modify: `scout/scorer.py`

- [ ] **Step 1: Write 5 failing tests in tests/test_scorer.py**

```python
def test_momentum_ratio_signal_fires(settings_factory, token_factory):
    """1h/24h ratio > 0.6 → +20 pts."""
    s = settings_factory()
    t = token_factory(price_change_1h=8.0, price_change_24h=12.0)
    # ratio = 8/12 = 0.67 > 0.6
    score, signals = score(t, s)
    assert "momentum_ratio" in signals


def test_momentum_ratio_none_safe(settings_factory, token_factory):
    """price_change_1h=None → 0 pts, no exception."""
    s = settings_factory()
    t = token_factory(price_change_1h=None, price_change_24h=10.0)
    score, signals = score(t, s)
    assert "momentum_ratio" not in signals


def test_vol_acceleration_signal_fires(settings_factory, token_factory):
    """volume/7d_avg > 5.0 → +25 pts."""
    s = settings_factory()
    t = token_factory(volume_24h_usd=500_000, vol_7d_avg=80_000)
    # ratio = 500k/80k = 6.25 > 5.0
    score, signals = score(t, s)
    assert "vol_acceleration" in signals


def test_cg_trending_rank_signal_fires(settings_factory, token_factory):
    """cg_trending_rank=5 (<=10) → +15 pts."""
    s = settings_factory()
    t = token_factory(cg_trending_rank=5)
    score, signals = score(t, s)
    assert "cg_trending_rank" in signals


def test_cg_trending_rank_over_10(settings_factory, token_factory):
    """cg_trending_rank=11 (>10) → 0 pts."""
    s = settings_factory()
    t = token_factory(cg_trending_rank=11)
    score, signals = score(t, s)
    assert "cg_trending_rank" not in signals
```

- [ ] **Step 2: Run tests — verify all 5 FAIL**

```bash
uv run pytest tests/test_scorer.py -v -k "momentum or vol_acceleration or cg_trending"
```

- [ ] **Step 3: Add 3 new signals to scout/scorer.py**

Inside the `score()` function, after existing signals:

```python
# Signal 6: Momentum ratio (CoinGecko)
if (
    token.price_change_1h is not None
    and token.price_change_24h is not None
    and token.price_change_24h != 0
):
    ratio = token.price_change_1h / token.price_change_24h
    if ratio > settings.MOMENTUM_RATIO_THRESHOLD:
        score += 20
        signals.append("momentum_ratio")

# Signal 7: Volume acceleration (CoinGecko)
if (
    token.volume_24h_usd is not None
    and token.vol_7d_avg is not None
    and token.vol_7d_avg > 0
):
    vol_ratio = token.volume_24h_usd / token.vol_7d_avg
    if vol_ratio > settings.MIN_VOL_ACCEL_RATIO:
        score += 25
        signals.append("vol_acceleration")

# Signal 8: CG trending rank
if token.cg_trending_rank is not None and token.cg_trending_rank <= 10:
    score += 15
    signals.append("cg_trending_rank")
```

Add `points = min(points, 100)` immediately before `return (points, signals)`. The scaffold has no cap — you must add it explicitly.

- [ ] **Step 4: Run scorer tests — verify all PASS**

```bash
uv run pytest tests/test_scorer.py -v
```

- [ ] **Step 5: Run full test suite — no regressions**

```bash
uv run pytest --tb=short -q
```

- [ ] **Step 6: Commit**

```bash
git add scout/scorer.py tests/test_scorer.py
git commit -m "feat: add momentum_ratio, vol_acceleration, cg_trending_rank scoring signals"
```

---

### Task 12: Wire CoinGecko into main.py

**Files:**
- Modify: `scout/main.py`

- [ ] **Step 1: Add coingecko import and wire into asyncio.gather()**

Add import at top:
```python
from scout.ingestion.coingecko import fetch_top_movers as cg_fetch_top_movers
from scout.ingestion.coingecko import fetch_trending as cg_fetch_trending
```

In the ingestion step, add CoinGecko calls to the existing `asyncio.gather()`:

```python
# Existing:
dex_tokens, gecko_tokens = await asyncio.gather(
    fetch_trending(session, settings),
    fetch_trending_pools(session, settings),
)
# Change to:
dex_tokens, gecko_tokens, cg_movers, cg_trending = await asyncio.gather(
    fetch_trending(session, settings),
    fetch_trending_pools(session, settings),
    cg_fetch_top_movers(session, settings),
    cg_fetch_trending(session, settings),
)
```

Update the candidates merge:
```python
all_candidates = dex_tokens + gecko_tokens + cg_movers + cg_trending
```

- [ ] **Step 2: Run full test suite — no regressions**

```bash
uv run pytest --tb=short -q
```

- [ ] **Step 3: Commit**

```bash
git add scout/main.py
git commit -m "feat: wire CoinGecko ingestion into main pipeline asyncio.gather()"
```

---

### Task 13: Update alerter.py with CoinGecko signal flags

**Files:**
- Modify: `scout/alerter.py`
- Modify: `tests/test_alerter.py`

- [ ] **Step 1: Write failing test for CG signal flags in alert message**

NOTE: The existing `format_alert_message(token, signals)` takes signals as a second argument. Keep this signature. Add CG signal names to the signals list to trigger CG flag rendering.

Add to `tests/test_alerter.py`:

```python
def test_alert_message_includes_momentum_flag(token_factory):
    """AC-08: Momentum flag appears in alert message when signal fired."""
    token = token_factory(
        quant_score=80,
        conviction_score=75.0,
    )
    signals = ["vol_liq_ratio", "momentum_ratio", "vol_acceleration"]
    msg = format_alert_message(token, signals)
    assert "momentum" in msg.lower()


def test_alert_message_includes_vol_spike_flag(token_factory):
    """Vol spike flag appears in alert message when signal fired."""
    token = token_factory(
        quant_score=80,
        conviction_score=75.0,
    )
    signals = ["vol_acceleration"]
    msg = format_alert_message(token, signals)
    assert "vol" in msg.lower() or "volume" in msg.lower()
```

- [ ] **Step 2: Run tests — verify FAIL**

```bash
uv run pytest tests/test_alerter.py -v -k "momentum_flag or vol_spike"
```

- [ ] **Step 3: Update format_alert_message() in scout/alerter.py**

Keep the existing signature `format_alert_message(token, signals)`. After the existing signals rendering section, add CoinGecko-specific flag descriptions:

```python
# CoinGecko signal flags
cg_flags = []
if "momentum_ratio" in signals:
    cg_flags.append("Momentum: 1h gain accelerating vs 24h")
if "vol_acceleration" in signals:
    cg_flags.append("Volume Spike: current vol >> 7d average")
if "cg_trending_rank" in signals:
    cg_flags.append(f"CG Trending: rank #{token.cg_trending_rank or '?'}")
if cg_flags:
    lines.append("CoinGecko Signals:")
    for flag in cg_flags:
        lines.append(f"  {flag}")
```

No changes to CandidateToken model or scorer needed for this task.

- [ ] **Step 4: Run alerter tests — verify PASS**

```bash
uv run pytest tests/test_alerter.py -v
```

- [ ] **Step 7: Run full test suite — no regressions**

```bash
uv run pytest --tb=short -q
```

- [ ] **Step 8: Commit**

```bash
git add scout/alerter.py tests/test_alerter.py
git commit -m "feat: add CoinGecko signal flags to alert messages"
```

---

### Task 14: Final verification

- [ ] **Step 1: Run full test suite**

```bash
uv run pytest --tb=short -q
```

Expected: ALL tests pass (scaffold + new CoinGecko tests)

- [ ] **Step 2: Run dry-run pipeline**

```bash
uv run python -m scout.main --dry-run --cycles 1
```

Expected: Exits cleanly with CoinGecko candidates in structured log output

- [ ] **Step 3: Verify acceptance criteria checklist**

| AC | Description | How to verify |
|----|-------------|---------------|
| AC-01 | All tests pass | `uv run pytest --tb=short -q` → 0 failures |
| AC-02 | Dry-run exits cleanly with CG candidates | `--dry-run --cycles 1` log output |
| AC-03 | Momentum ratio +20 pts | `test_momentum_ratio_signal_fires` PASS |
| AC-04 | Vol acceleration +25 pts | `test_vol_acceleration_signal_fires` PASS |
| AC-05 | CG trending +15 pts | `test_cg_trending_rank_signal_fires` PASS |
| AC-06 | CG outage doesn't crash | `test_coingecko_outage_does_not_crash_pipeline` PASS |
| AC-07 | Config via .env | `test_coingecko_config_defaults` PASS |
| AC-08 | Momentum flag in alert | `test_alert_message_includes_momentum_flag` PASS |

- [ ] **Step 4: Final commit if any loose changes**

```bash
git status
# If clean, done. If changes, commit them.
```
