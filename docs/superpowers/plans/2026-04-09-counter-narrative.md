# Counter-Narrative Scoring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add adversarial counter-scoring to both pipelines — deterministic red flags + LLM risk synthesis, displayed in alerts, stored for LEARN phase correlation.

**Architecture:** New `scout/counter/` module computes deterministic flags from data thresholds, then calls Claude Haiku (temp=0.3) to synthesize a risk_score + counter_argument. Integrates post-gate in existing pipeline (async follow-up) and inline in narrative agent.

**Tech Stack:** Python 3.12, aiohttp, anthropic SDK, Pydantic v2, structlog, pytest + aioresponses

**Spec:** `docs/superpowers/specs/2026-04-09-counter-narrative-design.md`

---

## File Map

### New files (create)
| File | Responsibility |
|------|---------------|
| `scout/counter/__init__.py` | Package init |
| `scout/counter/models.py` | RedFlag, CounterScore Pydantic models |
| `scout/counter/flags.py` | Deterministic flag computation — narrative + memecoin |
| `scout/counter/detail.py` | CoinGecko `/coins/{id}` fetcher + 30-min in-memory cache |
| `scout/counter/prompts.py` | Static prompts for narrative + memecoin counter-scoring |
| `scout/counter/scorer.py` | Orchestrator: fetch detail → compute flags → call LLM |
| `tests/test_counter_models.py` | Model validation tests |
| `tests/test_counter_flags.py` | All flag threshold tests |
| `tests/test_counter_detail.py` | Cache, fetch, extract tests |
| `tests/test_counter_scorer.py` | Orchestrator + LLM mock tests |

### Modified files
| File | Changes |
|------|---------|
| `scout/config.py` | Add 3 COUNTER_* config fields |
| `scout/db.py` | Add counter columns to `candidates` + `predictions` tables |
| `scout/narrative/models.py` | Add counter fields to NarrativePrediction |
| `scout/main.py` | Wire counter-scoring into both pipeline paths |
| `scout/narrative/strategy.py` | Add `counter_suppress_threshold` to defaults/bounds |
| `.env.example` | Add COUNTER_* env vars |

---

## Task 1: Models + Config

**Files:**
- Create: `scout/counter/__init__.py`
- Create: `scout/counter/models.py`
- Modify: `scout/config.py`
- Modify: `.env.example`
- Test: `tests/test_counter_models.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_counter_models.py
"""Tests for counter-narrative scoring models."""
from datetime import datetime, timezone

from scout.counter.models import CounterScore, RedFlag


def test_red_flag_valid():
    rf = RedFlag(flag="already_peaked", severity="high", detail="+120% in 30d")
    assert rf.flag == "already_peaked"
    assert rf.severity == "high"


def test_red_flag_invalid_severity_defaults_medium():
    rf = RedFlag(flag="test", severity="invalid", detail="test")
    assert rf.severity == "medium"


def test_counter_score_full():
    cs = CounterScore(
        risk_score=45,
        red_flags=[RedFlag(flag="already_peaked", severity="high", detail="test")],
        counter_argument="Token already ran.",
        data_completeness="full",
        counter_scored_at=datetime.now(timezone.utc),
    )
    assert cs.risk_score == 45
    assert len(cs.red_flags) == 1
    assert cs.data_completeness == "full"


def test_counter_score_clamps_high():
    cs = CounterScore(
        risk_score=150,
        red_flags=[],
        counter_argument="",
        data_completeness="partial",
        counter_scored_at=datetime.now(timezone.utc),
    )
    assert cs.risk_score == 100


def test_counter_score_clamps_low():
    cs = CounterScore(
        risk_score=-10,
        red_flags=[],
        counter_argument="",
        data_completeness="pipeline_only",
        counter_scored_at=datetime.now(timezone.utc),
    )
    assert cs.risk_score == 0


def test_counter_score_none_risk():
    cs = CounterScore(
        risk_score=None,
        red_flags=[],
        counter_argument="",
        data_completeness="partial",
        counter_scored_at=datetime.now(timezone.utc),
    )
    assert cs.risk_score is None


def test_counter_score_empty_flags():
    cs = CounterScore(
        risk_score=10,
        red_flags=[],
        counter_argument="No issues found.",
        data_completeness="full",
        counter_scored_at=datetime.now(timezone.utc),
    )
    assert cs.red_flags == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_counter_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scout.counter'`

- [ ] **Step 3: Create package and models**

```python
# scout/counter/__init__.py
"""Counter-Narrative Scoring — adversarial risk analysis for trade signals."""
```

```python
# scout/counter/models.py
"""Pydantic models for counter-narrative scoring."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, field_validator


class RedFlag(BaseModel):
    flag: str
    severity: str
    detail: str

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, v: str) -> str:
        if v not in ("low", "medium", "high"):
            return "medium"
        return v


class CounterScore(BaseModel):
    risk_score: int | None = None
    red_flags: list[RedFlag]
    counter_argument: str
    data_completeness: str
    counter_scored_at: datetime

    @field_validator("risk_score")
    @classmethod
    def clamp_score(cls, v: int | None) -> int | None:
        if v is None:
            return None
        return max(0, min(100, v))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_counter_models.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Add config fields**

Add to `scout/config.py` Settings class after the NARRATIVE_* fields:

```python
    # Counter-Narrative Scoring
    COUNTER_ENABLED: bool = True
    COUNTER_MODEL: str = "claude-haiku-4-5"
    COUNTER_SUPPRESS_THRESHOLD: int = 100
```

Add to `.env.example`:

```
# === Counter-Narrative Scoring ===
COUNTER_ENABLED=true
COUNTER_MODEL=claude-haiku-4-5
COUNTER_SUPPRESS_THRESHOLD=100       # 100 = informational only (never suppress)
```

- [ ] **Step 6: Run full test suite**

Run: `uv run pytest --tb=short -q`
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add scout/counter/__init__.py scout/counter/models.py scout/config.py .env.example tests/test_counter_models.py
git commit -m "feat(counter): add CounterScore/RedFlag models and config"
```

---

## Task 2: Deterministic Flag Computation

**Files:**
- Create: `scout/counter/flags.py`
- Test: `tests/test_counter_flags.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_counter_flags.py
"""Tests for deterministic counter-narrative flag computation."""
from scout.counter.flags import compute_narrative_flags, compute_memecoin_flags
from scout.counter.models import RedFlag


# --- Narrative flags ---

def test_already_peaked_high():
    flags = compute_narrative_flags(
        price_change_30d=120.0,
        commits_4w=50,
        reddit_subs=5000,
        sentiment_up_pct=60.0,
        narrative_fit_score=75,
        token_vol_change_24h=10.0,
        category_vol_growth_pct=15.0,
    )
    peaked = [f for f in flags if f.flag == "already_peaked"]
    assert len(peaked) == 1
    assert peaked[0].severity == "high"


def test_already_peaked_medium():
    flags = compute_narrative_flags(
        price_change_30d=60.0,
        commits_4w=50, reddit_subs=5000,
        sentiment_up_pct=60.0, narrative_fit_score=75,
        token_vol_change_24h=10.0, category_vol_growth_pct=15.0,
    )
    peaked = [f for f in flags if f.flag == "already_peaked"]
    assert len(peaked) == 1
    assert peaked[0].severity == "medium"


def test_no_peaked_flag():
    flags = compute_narrative_flags(
        price_change_30d=20.0,
        commits_4w=50, reddit_subs=5000,
        sentiment_up_pct=60.0, narrative_fit_score=75,
        token_vol_change_24h=10.0, category_vol_growth_pct=15.0,
    )
    peaked = [f for f in flags if f.flag == "already_peaked"]
    assert len(peaked) == 0


def test_dead_project_high():
    flags = compute_narrative_flags(
        price_change_30d=10.0, commits_4w=0,
        reddit_subs=5000, sentiment_up_pct=60.0,
        narrative_fit_score=75,
        token_vol_change_24h=10.0, category_vol_growth_pct=15.0,
    )
    dead = [f for f in flags if f.flag == "dead_project"]
    assert len(dead) == 1
    assert dead[0].severity == "high"


def test_dead_project_medium():
    flags = compute_narrative_flags(
        price_change_30d=10.0, commits_4w=5,
        reddit_subs=5000, sentiment_up_pct=60.0,
        narrative_fit_score=75,
        token_vol_change_24h=10.0, category_vol_growth_pct=15.0,
    )
    dead = [f for f in flags if f.flag == "dead_project"]
    assert len(dead) == 1
    assert dead[0].severity == "medium"


def test_weak_community_high():
    flags = compute_narrative_flags(
        price_change_30d=10.0, commits_4w=50,
        reddit_subs=47, sentiment_up_pct=60.0,
        narrative_fit_score=75,
        token_vol_change_24h=10.0, category_vol_growth_pct=15.0,
    )
    weak = [f for f in flags if f.flag == "weak_community"]
    assert len(weak) == 1
    assert weak[0].severity == "high"


def test_negative_sentiment_high():
    flags = compute_narrative_flags(
        price_change_30d=10.0, commits_4w=50,
        reddit_subs=5000, sentiment_up_pct=35.0,
        narrative_fit_score=75,
        token_vol_change_24h=10.0, category_vol_growth_pct=15.0,
    )
    neg = [f for f in flags if f.flag == "negative_sentiment"]
    assert len(neg) == 1
    assert neg[0].severity == "high"


def test_volume_divergence_high():
    flags = compute_narrative_flags(
        price_change_30d=10.0, commits_4w=50,
        reddit_subs=5000, sentiment_up_pct=60.0,
        narrative_fit_score=75,
        token_vol_change_24h=-15.0,
        category_vol_growth_pct=12.0,
    )
    div = [f for f in flags if f.flag == "volume_divergence"]
    assert len(div) == 1
    assert div[0].severity == "high"


def test_narrative_mismatch_high():
    flags = compute_narrative_flags(
        price_change_30d=10.0, commits_4w=50,
        reddit_subs=5000, sentiment_up_pct=60.0,
        narrative_fit_score=30,
        token_vol_change_24h=10.0, category_vol_growth_pct=15.0,
    )
    mis = [f for f in flags if f.flag == "narrative_mismatch"]
    assert len(mis) == 1
    assert mis[0].severity == "high"


def test_clean_token_no_flags():
    flags = compute_narrative_flags(
        price_change_30d=10.0, commits_4w=50,
        reddit_subs=5000, sentiment_up_pct=60.0,
        narrative_fit_score=75,
        token_vol_change_24h=10.0, category_vol_growth_pct=15.0,
    )
    assert len(flags) == 0


# --- Memecoin flags ---

def test_wash_trading_high():
    flags = compute_memecoin_flags(
        buy_pressure=0.96, liquidity_usd=50000.0,
        token_age_days=2.0, vol_liq_ratio=10.0,
        holder_count=500, goplus_creator_pct=5.0,
        goplus_is_honeypot=False,
    )
    wash = [f for f in flags if f.flag == "wash_trading"]
    assert len(wash) == 1
    assert wash[0].severity == "high"


def test_liquidity_trap_high():
    flags = compute_memecoin_flags(
        buy_pressure=0.55, liquidity_usd=10000.0,
        token_age_days=2.0, vol_liq_ratio=10.0,
        holder_count=500, goplus_creator_pct=5.0,
        goplus_is_honeypot=False,
    )
    trap = [f for f in flags if f.flag == "liquidity_trap"]
    assert len(trap) == 1
    assert trap[0].severity == "high"


def test_honeypot_risk():
    flags = compute_memecoin_flags(
        buy_pressure=0.55, liquidity_usd=50000.0,
        token_age_days=2.0, vol_liq_ratio=10.0,
        holder_count=500, goplus_creator_pct=5.0,
        goplus_is_honeypot=True,
    )
    hp = [f for f in flags if f.flag == "honeypot_risk"]
    assert len(hp) == 1
    assert hp[0].severity == "high"


def test_token_too_new_high():
    flags = compute_memecoin_flags(
        buy_pressure=0.55, liquidity_usd=50000.0,
        token_age_days=0.1, vol_liq_ratio=10.0,
        holder_count=500, goplus_creator_pct=5.0,
        goplus_is_honeypot=False,
    )
    new = [f for f in flags if f.flag == "token_too_new"]
    assert len(new) == 1
    assert new[0].severity == "high"


def test_clean_memecoin_no_flags():
    flags = compute_memecoin_flags(
        buy_pressure=0.55, liquidity_usd=50000.0,
        token_age_days=2.0, vol_liq_ratio=10.0,
        holder_count=500, goplus_creator_pct=5.0,
        goplus_is_honeypot=False,
    )
    assert len(flags) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_counter_flags.py -v`
Expected: FAIL

- [ ] **Step 3: Implement flag computation**

```python
# scout/counter/flags.py
"""Deterministic red flag computation from data thresholds.

Flags are computed BEFORE the LLM call. The LLM does NOT add, remove,
or re-score flags — it only synthesizes them into a risk narrative.
Thresholds are static constants, not learnable.
"""

from __future__ import annotations

from scout.counter.models import RedFlag


def compute_narrative_flags(
    price_change_30d: float,
    commits_4w: int,
    reddit_subs: int,
    sentiment_up_pct: float,
    narrative_fit_score: int,
    token_vol_change_24h: float,
    category_vol_growth_pct: float,
) -> list[RedFlag]:
    """Compute red flags for CoinGecko-listed tokens. Deterministic."""
    flags: list[RedFlag] = []

    # already_peaked
    if price_change_30d > 100:
        flags.append(RedFlag(flag="already_peaked", severity="high",
            detail=f"+{price_change_30d:.0f}% in 30d, may be exhausted"))
    elif price_change_30d > 50:
        flags.append(RedFlag(flag="already_peaked", severity="medium",
            detail=f"+{price_change_30d:.0f}% in 30d"))

    # dead_project
    if commits_4w == 0:
        flags.append(RedFlag(flag="dead_project", severity="high",
            detail="0 commits in 4 weeks"))
    elif commits_4w < 10:
        flags.append(RedFlag(flag="dead_project", severity="medium",
            detail=f"Only {commits_4w} commits in 4 weeks"))

    # weak_community
    if reddit_subs < 100:
        flags.append(RedFlag(flag="weak_community", severity="high",
            detail=f"{reddit_subs} Reddit subscribers"))
    elif reddit_subs < 1000:
        flags.append(RedFlag(flag="weak_community", severity="medium",
            detail=f"{reddit_subs} Reddit subscribers"))

    # negative_sentiment
    if sentiment_up_pct < 40:
        flags.append(RedFlag(flag="negative_sentiment", severity="high",
            detail=f"Only {sentiment_up_pct:.0f}% positive sentiment"))
    elif sentiment_up_pct < 50:
        flags.append(RedFlag(flag="negative_sentiment", severity="medium",
            detail=f"{sentiment_up_pct:.0f}% positive sentiment"))

    # volume_divergence: token declining while category rising
    if token_vol_change_24h < -10.0 and category_vol_growth_pct > 10.0:
        flags.append(RedFlag(flag="volume_divergence", severity="high",
            detail=f"Token vol {token_vol_change_24h:+.0f}% vs category +{category_vol_growth_pct:.0f}%"))

    # narrative_mismatch (from bullish scoring pass)
    if narrative_fit_score < 40:
        flags.append(RedFlag(flag="narrative_mismatch", severity="high",
            detail=f"Narrative fit score only {narrative_fit_score}/100"))
    elif narrative_fit_score < 60:
        flags.append(RedFlag(flag="narrative_mismatch", severity="medium",
            detail=f"Narrative fit score {narrative_fit_score}/100"))

    return flags


def compute_memecoin_flags(
    buy_pressure: float,
    liquidity_usd: float,
    token_age_days: float,
    vol_liq_ratio: float,
    holder_count: int,
    goplus_creator_pct: float,
    goplus_is_honeypot: bool,
) -> list[RedFlag]:
    """Compute red flags for DEX memecoins. Deterministic."""
    flags: list[RedFlag] = []

    # wash_trading
    if buy_pressure > 0.95 or buy_pressure < 0.05:
        flags.append(RedFlag(flag="wash_trading", severity="high",
            detail=f"Buy pressure {buy_pressure:.0%} suggests wash trading"))
    elif buy_pressure > 0.90 or buy_pressure < 0.10:
        flags.append(RedFlag(flag="wash_trading", severity="medium",
            detail=f"Buy pressure {buy_pressure:.0%} is skewed"))

    # deployer_concentration
    if goplus_creator_pct > 20:
        flags.append(RedFlag(flag="deployer_concentration", severity="high",
            detail=f"Deployer holds {goplus_creator_pct:.0f}% of supply"))
    elif goplus_creator_pct > 10:
        flags.append(RedFlag(flag="deployer_concentration", severity="medium",
            detail=f"Deployer holds {goplus_creator_pct:.0f}% of supply"))

    # liquidity_trap
    if liquidity_usd < 15000:
        flags.append(RedFlag(flag="liquidity_trap", severity="high",
            detail=f"Only ${liquidity_usd:,.0f} liquidity"))
    elif liquidity_usd < 30000:
        flags.append(RedFlag(flag="liquidity_trap", severity="medium",
            detail=f"${liquidity_usd:,.0f} liquidity"))

    # token_too_new
    if token_age_days < 0.25:
        flags.append(RedFlag(flag="token_too_new", severity="high",
            detail=f"Token is only {token_age_days * 24:.0f} hours old"))
    elif token_age_days < 0.5:
        flags.append(RedFlag(flag="token_too_new", severity="medium",
            detail=f"Token is {token_age_days * 24:.0f} hours old"))

    # suspicious_volume
    if vol_liq_ratio > 50:
        flags.append(RedFlag(flag="suspicious_volume", severity="high",
            detail=f"Volume/liquidity ratio {vol_liq_ratio:.0f}x"))
    elif vol_liq_ratio > 20:
        flags.append(RedFlag(flag="suspicious_volume", severity="medium",
            detail=f"Volume/liquidity ratio {vol_liq_ratio:.0f}x"))

    # honeypot_risk
    if goplus_is_honeypot:
        flags.append(RedFlag(flag="honeypot_risk", severity="high",
            detail="GoPlus flagged as honeypot"))

    # low_holders
    if holder_count < 50:
        flags.append(RedFlag(flag="low_holders", severity="high",
            detail=f"Only {holder_count} holders"))
    elif holder_count < 200:
        flags.append(RedFlag(flag="low_holders", severity="medium",
            detail=f"{holder_count} holders"))

    return flags
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_counter_flags.py -v`
Expected: All 16 tests PASS

- [ ] **Step 5: Commit**

```bash
git add scout/counter/flags.py tests/test_counter_flags.py
git commit -m "feat(counter): add deterministic flag computation for narrative + memecoin pipelines"
```

---

## Task 3: CoinGecko Detail Fetcher + Cache

**Files:**
- Create: `scout/counter/detail.py`
- Test: `tests/test_counter_detail.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_counter_detail.py
"""Tests for CoinGecko /coins/{id} detail fetcher with cache."""
import asyncio
from datetime import datetime, timezone, timedelta

import pytest
from aioresponses import aioresponses
import aiohttp

from scout.counter.detail import (
    fetch_coin_detail,
    extract_counter_data,
    _detail_cache,
)

SAMPLE_DETAIL = {
    "id": "fetch-ai",
    "market_data": {
        "price_change_percentage_7d": 15.5,
        "price_change_percentage_30d": 85.0,
    },
    "community_data": {
        "reddit_subscribers": 4500,
        "telegram_channel_user_count": 12000,
    },
    "developer_data": {
        "commit_count_4_weeks": 42,
    },
    "sentiment_votes_up_percentage": 72.0,
}


def test_extract_counter_data_full():
    data = extract_counter_data(SAMPLE_DETAIL)
    assert data["commits_4w"] == 42
    assert data["reddit_subscribers"] == 4500
    assert data["telegram_users"] == 12000
    assert data["sentiment_up_pct"] == 72.0
    assert data["price_change_7d"] == 15.5
    assert data["price_change_30d"] == 85.0


def test_extract_counter_data_missing_fields():
    data = extract_counter_data({"id": "empty"})
    assert data["commits_4w"] == 0
    assert data["reddit_subscribers"] == 0
    assert data["sentiment_up_pct"] == 50.0


async def test_fetch_coin_detail_success():
    _detail_cache.clear()
    url = "https://api.coingecko.com/api/v3/coins/fetch-ai"
    with aioresponses() as mocked:
        mocked.get(url, payload=SAMPLE_DETAIL)
        async with aiohttp.ClientSession() as session:
            result = await fetch_coin_detail(session, "fetch-ai")
    assert result is not None
    assert result["id"] == "fetch-ai"


async def test_fetch_coin_detail_cache_hit():
    _detail_cache.clear()
    _detail_cache["cached-coin"] = (datetime.now(timezone.utc), {"id": "cached-coin"})
    async with aiohttp.ClientSession() as session:
        result = await fetch_coin_detail(session, "cached-coin")
    assert result is not None
    assert result["id"] == "cached-coin"


async def test_fetch_coin_detail_cache_expired():
    _detail_cache.clear()
    old_time = datetime.now(timezone.utc) - timedelta(minutes=35)
    _detail_cache["old-coin"] = (old_time, {"id": "old-coin"})
    url = "https://api.coingecko.com/api/v3/coins/old-coin"
    with aioresponses() as mocked:
        mocked.get(url, payload={"id": "old-coin", "fresh": True})
        async with aiohttp.ClientSession() as session:
            result = await fetch_coin_detail(session, "old-coin")
    assert result is not None
    assert result.get("fresh") is True


async def test_fetch_coin_detail_404_returns_none():
    _detail_cache.clear()
    url = "https://api.coingecko.com/api/v3/coins/nonexistent"
    with aioresponses() as mocked:
        mocked.get(url, status=404)
        async with aiohttp.ClientSession() as session:
            result = await fetch_coin_detail(session, "nonexistent")
    assert result is None


async def test_fetch_coin_detail_429_returns_none():
    _detail_cache.clear()
    url = "https://api.coingecko.com/api/v3/coins/ratelimited"
    with aioresponses() as mocked:
        mocked.get(url, status=429)
        async with aiohttp.ClientSession() as session:
            result = await fetch_coin_detail(session, "ratelimited")
    assert result is None
```

- [ ] **Step 2: Run tests, verify fail**

Run: `uv run pytest tests/test_counter_detail.py -v`
Expected: FAIL

- [ ] **Step 3: Implement detail fetcher**

```python
# scout/counter/detail.py
"""CoinGecko /coins/{id} detail fetcher with 30-min in-memory cache."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import aiohttp
import structlog

logger = structlog.get_logger()

_detail_cache: dict[str, tuple[datetime, dict]] = {}
CACHE_TTL_SECONDS = 1800  # 30 minutes


async def fetch_coin_detail(
    session: aiohttp.ClientSession,
    coin_id: str,
    api_key: str = "",
) -> dict | None:
    """Fetch /coins/{id} with 30-min in-memory cache. Returns None on failure."""
    now = datetime.now(timezone.utc)

    if coin_id in _detail_cache:
        cached_at, data = _detail_cache[coin_id]
        if (now - cached_at).total_seconds() < CACHE_TTL_SECONDS:
            return data

    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}"
    params = {
        "localization": "false",
        "tickers": "false",
        "market_data": "true",
        "community_data": "true",
        "developer_data": "true",
        "sparkline": "false",
    }
    headers = {}
    if api_key:
        headers["x-cg-demo-api-key"] = api_key

    try:
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status == 429:
                logger.warning("counter_detail_rate_limited", coin_id=coin_id)
                return None
            if resp.status == 404:
                logger.info("counter_detail_not_found", coin_id=coin_id)
                return None
            resp.raise_for_status()
            data = await resp.json()
            _detail_cache[coin_id] = (now, data)
            await asyncio.sleep(1)  # call spacing (see GH #2)
            return data
    except Exception as e:
        logger.warning("counter_detail_fetch_error", coin_id=coin_id, error=str(e))
        return None


def extract_counter_data(detail: dict) -> dict:
    """Extract fields relevant to counter-scoring from /coins/{id} response."""
    market = detail.get("market_data", {})
    community = detail.get("community_data", {})
    developer = detail.get("developer_data", {})
    sentiment = detail.get("sentiment_votes_up_percentage")

    return {
        "commits_4w": developer.get("commit_count_4_weeks") or 0,
        "reddit_subscribers": community.get("reddit_subscribers") or 0,
        "telegram_users": community.get("telegram_channel_user_count") or 0,
        "sentiment_up_pct": sentiment if sentiment is not None else 50.0,
        "price_change_7d": market.get("price_change_percentage_7d") or 0,
        "price_change_30d": market.get("price_change_percentage_30d") or 0,
    }
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_counter_detail.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add scout/counter/detail.py tests/test_counter_detail.py
git commit -m "feat(counter): add CoinGecko detail fetcher with 30-min in-memory cache"
```

---

## Task 4: Prompts

**Files:**
- Create: `scout/counter/prompts.py`

- [ ] **Step 1: Create prompts module**

```python
# scout/counter/prompts.py
"""Static prompts for counter-narrative scoring. Never modified by the agent."""

COUNTER_SYSTEM = (
    "You are a risk analyst evaluating crypto trades. You receive objective red flags "
    "with pre-computed severities. Your job: synthesize these flags into a risk assessment. "
    "Do NOT add new flags or change severities — they are computed from data. "
    "Return ONLY valid JSON."
)

COUNTER_NARRATIVE_TEMPLATE = """\
Token: {token_name} ({symbol}), ${market_cap:,.0f} mcap, {price_change_24h:+.1f}% 24h
Category: {category_name} (accelerating: {acceleration:+.1f}%)
Narrative fit score (bullish): {narrative_fit_score}/100
Data completeness: {data_completeness}

PRE-COMPUTED RED FLAGS (ground truth — do not modify):
{formatted_flags}

SCORING SCALE:
0-20: No identifiable risk. All data points look healthy.
21-40: Minor concerns that don't invalidate the thesis.
41-60: Meaningful risk — one or more flags warrant caution.
61-80: Strong evidence against this trade. Multiple high-severity flags.
81-100: Clear red flags — high probability of loss.

Based on the red flags above, assign a risk_score and write a 1-2 sentence \
counter_argument explaining why this trade might fail. If there are no red flags, \
assign risk_score 0-20 and note the absence of concerns.

Return ONLY JSON:
{{"risk_score": <int 0-100>, "counter_argument": "<1-2 sentences>"}}"""

COUNTER_MEMECOIN_TEMPLATE = """\
Token: {token_name} ({symbol}) on {chain}
Age: {token_age_hours:.0f} hours, Liquidity: ${liquidity:,.0f}, Volume/Liq: {vol_liq_ratio:.1f}x
Buy pressure: {buy_pressure:.0%}, Holders: {holder_count}
Data completeness: {data_completeness}

PRE-COMPUTED RED FLAGS (ground truth — do not modify):
{formatted_flags}

SCORING SCALE:
0-20: No identifiable risk. All data points look healthy.
21-40: Minor concerns that don't invalidate the thesis.
41-60: Meaningful risk — one or more flags warrant caution.
61-80: Strong evidence against this trade. Multiple high-severity flags.
81-100: Clear red flags — high probability of loss or rug.

Based on the red flags above, assign a risk_score and write a 1-2 sentence \
counter_argument explaining why this trade might fail.

Return ONLY JSON:
{{"risk_score": <int 0-100>, "counter_argument": "<1-2 sentences>"}}"""


def format_flags_for_prompt(flags: list) -> str:
    """Format RedFlag list as text for LLM prompt."""
    if not flags:
        return "(no red flags detected)"
    lines = []
    for f in flags:
        lines.append(f"- [{f.severity.upper()}] {f.flag}: {f.detail}")
    return "\n".join(lines)
```

- [ ] **Step 2: Commit**

```bash
git add scout/counter/prompts.py
git commit -m "feat(counter): add static prompts for narrative + memecoin counter-scoring"
```

---

## Task 5: Scorer (Orchestrator)

**Files:**
- Create: `scout/counter/scorer.py`
- Test: `tests/test_counter_scorer.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_counter_scorer.py
"""Tests for counter-narrative scoring orchestrator."""
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from scout.counter.models import CounterScore, RedFlag
from scout.counter.scorer import (
    score_counter_narrative,
    score_counter_memecoin,
    _parse_counter_response,
)


def test_parse_counter_response_valid():
    text = '{"risk_score": 45, "counter_argument": "Token already ran."}'
    result = _parse_counter_response(text)
    assert result["risk_score"] == 45


def test_parse_counter_response_markdown():
    text = '```json\n{"risk_score": 30, "counter_argument": "Low risk."}\n```'
    result = _parse_counter_response(text)
    assert result["risk_score"] == 30


def test_parse_counter_response_invalid():
    result = _parse_counter_response("not json at all")
    assert result is None


async def test_score_counter_narrative_success():
    mock_client = AsyncMock()
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(
        text='{"risk_score": 55, "counter_argument": "Already peaked."}'
    )]
    mock_client.messages.create = AsyncMock(return_value=mock_msg)

    flags = [RedFlag(flag="already_peaked", severity="high", detail="+120% in 30d")]

    result = await score_counter_narrative(
        token_name="FET", symbol="FET", market_cap=340e6,
        price_change_24h=2.0, category_name="AI",
        acceleration=7.0, narrative_fit_score=82,
        flags=flags, data_completeness="full",
        api_key="fake", model="claude-haiku-4-5",
        client=mock_client,
    )
    assert result.risk_score == 55
    assert result.counter_argument == "Already peaked."
    assert len(result.red_flags) == 1
    assert result.data_completeness == "full"


async def test_score_counter_narrative_api_failure():
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("API down"))

    result = await score_counter_narrative(
        token_name="FET", symbol="FET", market_cap=340e6,
        price_change_24h=2.0, category_name="AI",
        acceleration=7.0, narrative_fit_score=82,
        flags=[], data_completeness="full",
        api_key="fake", model="claude-haiku-4-5",
        client=mock_client,
    )
    assert result.risk_score is None
    assert result.counter_argument == ""


async def test_score_counter_memecoin_success():
    mock_client = AsyncMock()
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(
        text='{"risk_score": 70, "counter_argument": "Likely wash traded."}'
    )]
    mock_client.messages.create = AsyncMock(return_value=mock_msg)

    flags = [RedFlag(flag="wash_trading", severity="high", detail="96% buy pressure")]

    result = await score_counter_memecoin(
        token_name="MEME", symbol="MEME", chain="solana",
        token_age_days=0.5, liquidity_usd=20000.0,
        vol_liq_ratio=15.0, buy_pressure=0.96,
        holder_count=150, flags=flags,
        data_completeness="pipeline_only",
        api_key="fake", model="claude-haiku-4-5",
        client=mock_client,
    )
    assert result.risk_score == 70
    assert len(result.red_flags) == 1


async def test_score_counter_no_flags():
    mock_client = AsyncMock()
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(
        text='{"risk_score": 10, "counter_argument": "No concerns."}'
    )]
    mock_client.messages.create = AsyncMock(return_value=mock_msg)

    result = await score_counter_narrative(
        token_name="FET", symbol="FET", market_cap=340e6,
        price_change_24h=2.0, category_name="AI",
        acceleration=7.0, narrative_fit_score=82,
        flags=[], data_completeness="full",
        api_key="fake", model="claude-haiku-4-5",
        client=mock_client,
    )
    assert result.risk_score == 10
```

- [ ] **Step 2: Run tests, verify fail**

Run: `uv run pytest tests/test_counter_scorer.py -v`
Expected: FAIL

- [ ] **Step 3: Implement scorer**

```python
# scout/counter/scorer.py
"""Counter-narrative scoring orchestrator.

Fetches detail data, computes deterministic flags, calls LLM for risk synthesis.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import anthropic
import structlog

from scout.counter.models import CounterScore, RedFlag
from scout.counter.prompts import (
    COUNTER_SYSTEM,
    COUNTER_NARRATIVE_TEMPLATE,
    COUNTER_MEMECOIN_TEMPLATE,
    format_flags_for_prompt,
)

logger = structlog.get_logger()


def _parse_counter_response(text: str) -> dict | None:
    """Extract JSON from Claude response."""
    try:
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if match:
            return json.loads(match.group(1).strip())
        return json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        return None


async def score_counter_narrative(
    token_name: str,
    symbol: str,
    market_cap: float,
    price_change_24h: float,
    category_name: str,
    acceleration: float,
    narrative_fit_score: int,
    flags: list[RedFlag],
    data_completeness: str,
    api_key: str,
    model: str = "claude-haiku-4-5",
    client: anthropic.AsyncAnthropic | None = None,
) -> CounterScore:
    """Score counter-narrative for a CoinGecko-listed token."""
    now = datetime.now(timezone.utc)

    if client is None:
        client = anthropic.AsyncAnthropic(api_key=api_key)

    prompt = COUNTER_NARRATIVE_TEMPLATE.format(
        token_name=token_name,
        symbol=symbol,
        market_cap=market_cap,
        price_change_24h=price_change_24h,
        category_name=category_name,
        acceleration=acceleration,
        narrative_fit_score=narrative_fit_score,
        data_completeness=data_completeness,
        formatted_flags=format_flags_for_prompt(flags),
    )

    try:
        message = await client.messages.create(
            model=model,
            max_tokens=300,
            temperature=0.3,
            system=COUNTER_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text
        parsed = _parse_counter_response(text)

        if parsed:
            return CounterScore(
                risk_score=parsed.get("risk_score", 50),
                red_flags=flags,
                counter_argument=parsed.get("counter_argument", ""),
                data_completeness=data_completeness,
                counter_scored_at=now,
            )
    except Exception as e:
        logger.warning("counter_narrative_scoring_error", symbol=symbol, error=str(e))

    return CounterScore(
        risk_score=None,
        red_flags=flags,
        counter_argument="",
        data_completeness=data_completeness,
        counter_scored_at=now,
    )


async def score_counter_memecoin(
    token_name: str,
    symbol: str,
    chain: str,
    token_age_days: float,
    liquidity_usd: float,
    vol_liq_ratio: float,
    buy_pressure: float,
    holder_count: int,
    flags: list[RedFlag],
    data_completeness: str,
    api_key: str,
    model: str = "claude-haiku-4-5",
    client: anthropic.AsyncAnthropic | None = None,
) -> CounterScore:
    """Score counter-narrative for a DEX memecoin."""
    now = datetime.now(timezone.utc)

    if client is None:
        client = anthropic.AsyncAnthropic(api_key=api_key)

    prompt = COUNTER_MEMECOIN_TEMPLATE.format(
        token_name=token_name,
        symbol=symbol,
        chain=chain,
        token_age_hours=token_age_days * 24,
        liquidity=liquidity_usd,
        vol_liq_ratio=vol_liq_ratio,
        buy_pressure=buy_pressure,
        holder_count=holder_count,
        data_completeness=data_completeness,
        formatted_flags=format_flags_for_prompt(flags),
    )

    try:
        message = await client.messages.create(
            model=model,
            max_tokens=300,
            temperature=0.3,
            system=COUNTER_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text
        parsed = _parse_counter_response(text)

        if parsed:
            return CounterScore(
                risk_score=parsed.get("risk_score", 50),
                red_flags=flags,
                counter_argument=parsed.get("counter_argument", ""),
                data_completeness=data_completeness,
                counter_scored_at=now,
            )
    except Exception as e:
        logger.warning("counter_memecoin_scoring_error", symbol=symbol, error=str(e))

    return CounterScore(
        risk_score=None,
        red_flags=flags,
        counter_argument="",
        data_completeness=data_completeness,
        counter_scored_at=now,
    )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_counter_scorer.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add scout/counter/scorer.py tests/test_counter_scorer.py
git commit -m "feat(counter): add counter-narrative scoring orchestrator with LLM synthesis"
```

---

## Task 6: Database Schema + Model Updates

**Files:**
- Modify: `scout/db.py`
- Modify: `scout/narrative/models.py`
- Modify: `scout/narrative/strategy.py`

- [ ] **Step 1: Add counter columns to DB tables**

In `scout/db.py`, add counter columns to the `candidates` table CREATE statement (after `signals_fired`):

```sql
counter_risk_score       INTEGER,
counter_flags            TEXT,
counter_argument         TEXT,
counter_data_completeness TEXT,
counter_scored_at        TEXT,
```

Add the same 5 columns to the `predictions` table CREATE statement (after `eval_retry_count`):

```sql
counter_risk_score       INTEGER,
counter_flags            TEXT,
counter_argument         TEXT,
counter_data_completeness TEXT,
counter_scored_at        TEXT,
```

- [ ] **Step 2: Add counter fields to NarrativePrediction model**

In `scout/narrative/models.py`, add to NarrativePrediction class after `evaluated_at`:

```python
    counter_risk_score: int | None = None
    counter_flags: list[dict] | None = None
    counter_argument: str | None = None
    counter_data_completeness: str | None = None
    counter_scored_at: datetime | None = None
```

- [ ] **Step 3: Add counter_suppress_threshold to strategy defaults**

In `scout/narrative/strategy.py`, add to `STRATEGY_DEFAULTS`:

```python
    "counter_suppress_threshold": 100,
```

Add to `STRATEGY_BOUNDS`:

```python
    "counter_suppress_threshold": (0, 100),
```

- [ ] **Step 4: Run full test suite**

Run: `uv run pytest --tb=short -q`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add scout/db.py scout/narrative/models.py scout/narrative/strategy.py
git commit -m "feat(counter): add counter-score columns to DB and model, strategy defaults"
```

---

## Task 7: Pipeline Integration

**Files:**
- Modify: `scout/main.py`

- [ ] **Step 1: Read scout/main.py to find integration points**

Read the existing pipeline alert path (~line 170) and the narrative agent prediction loop (~line 375).

- [ ] **Step 2: Add counter-scoring to existing pipeline**

Add imports at top of `scout/main.py`:

```python
from scout.counter.detail import fetch_coin_detail, extract_counter_data
from scout.counter.flags import compute_narrative_flags, compute_memecoin_flags
from scout.counter.scorer import score_counter_narrative, score_counter_memecoin
```

After the existing `send_alert()` call (~line 173), add async counter follow-up:

```python
        # Counter-score follow-up (async, non-blocking)
        if settings.COUNTER_ENABLED:
            asyncio.create_task(
                _safe_counter_followup(gated_token, signals, session, settings, db)
            )
```

Add the helper function:

```python
async def _safe_counter_followup(
    token, signals, session, settings, db,
):
    """Run counter-score and send follow-up Telegram message. Never raises."""
    try:
        # Compute memecoin flags from pipeline data
        buy_pressure = 0.5
        if token.txns_h1_buys and token.txns_h1_sells:
            total = token.txns_h1_buys + token.txns_h1_sells
            if total > 0:
                buy_pressure = token.txns_h1_buys / total

        vol_liq = token.volume_24h_usd / max(token.liquidity_usd, 1)

        goplus_creator_pct = 0.0
        goplus_is_honeypot = False
        # GoPlus data would come from safety.py if available

        flags = compute_memecoin_flags(
            buy_pressure=buy_pressure,
            liquidity_usd=token.liquidity_usd,
            token_age_days=token.token_age_days,
            vol_liq_ratio=vol_liq,
            holder_count=token.holder_count,
            goplus_creator_pct=goplus_creator_pct,
            goplus_is_honeypot=goplus_is_honeypot,
        )

        counter = await score_counter_memecoin(
            token_name=token.token_name,
            symbol=token.ticker,
            chain=token.chain,
            token_age_days=token.token_age_days,
            liquidity_usd=token.liquidity_usd,
            vol_liq_ratio=vol_liq,
            buy_pressure=buy_pressure,
            holder_count=token.holder_count,
            flags=flags,
            data_completeness="pipeline_only",
            api_key=settings.ANTHROPIC_API_KEY,
            model=settings.COUNTER_MODEL,
        )

        # Send follow-up Telegram message
        if counter.risk_score is not None:
            flag_lines = "\n".join(
                f"- [{f.severity.upper()}] {f.flag}: {f.detail}" for f in counter.red_flags
            )
            msg = (
                f"Risk assessment for {token.ticker}:\n"
                f"Risk: {counter.risk_score}/100 | {counter.data_completeness} data\n"
                f"{flag_lines}\n"
                f'"{counter.counter_argument}"'
            )
            await send_telegram_message(msg, session, settings)

        logger.info("counter_followup_sent", symbol=token.ticker,
                     risk_score=counter.risk_score)

    except Exception as e:
        logger.error("counter_followup_error", symbol=token.ticker, error=str(e))
```

- [ ] **Step 3: Add counter-scoring to narrative agent**

In the narrative agent loop, after `score_token` succeeds and before appending to `prediction_models` (~line 394), add:

```python
                        # Counter-score for narrative picks
                        counter_result = None
                        if settings.COUNTER_ENABLED:
                            detail = await fetch_coin_detail(
                                session, token.coin_id, settings.COINGECKO_API_KEY
                            )
                            if detail:
                                counter_data = extract_counter_data(detail)
                                data_comp = "full"
                            else:
                                counter_data = {
                                    "commits_4w": 0, "reddit_subscribers": 0,
                                    "telegram_users": 0, "sentiment_up_pct": 50.0,
                                    "price_change_7d": 0, "price_change_30d": 0,
                                }
                                data_comp = "partial"

                            narrative_flags = compute_narrative_flags(
                                price_change_30d=counter_data["price_change_30d"],
                                commits_4w=counter_data["commits_4w"],
                                reddit_subs=counter_data["reddit_subscribers"],
                                sentiment_up_pct=counter_data["sentiment_up_pct"],
                                narrative_fit_score=result.get("narrative_fit", 50),
                                token_vol_change_24h=0.0,  # not easily available per-token
                                category_vol_growth_pct=accel.volume_growth_pct,
                            )

                            counter_result = await score_counter_narrative(
                                token_name=token.name,
                                symbol=token.symbol,
                                market_cap=token.market_cap,
                                price_change_24h=token.price_change_24h,
                                category_name=accel.name,
                                acceleration=accel.acceleration,
                                narrative_fit_score=result.get("narrative_fit", 50),
                                flags=narrative_flags,
                                data_completeness=data_comp,
                                api_key=settings.ANTHROPIC_API_KEY,
                                model=settings.COUNTER_MODEL,
                            )
```

Then include counter fields when building the prediction dict/model:

```python
                        # Add to prediction row
                        pred_row["counter_risk_score"] = counter_result.risk_score if counter_result else None
                        pred_row["counter_flags"] = json.dumps([f.model_dump() for f in counter_result.red_flags]) if counter_result else None
                        pred_row["counter_argument"] = counter_result.counter_argument if counter_result else None
                        pred_row["counter_data_completeness"] = counter_result.data_completeness if counter_result else None
                        pred_row["counter_scored_at"] = counter_result.counter_scored_at.isoformat() if counter_result else None
```

- [ ] **Step 4: Update narrative alert format**

In the narrative alert section, update `format_heating_alert` call or modify the alert string to include counter info:

```python
# After each prediction line in the alert, append:
if pred.counter_risk_score is not None:
    net = pred.narrative_fit_score - pred.counter_risk_score
    flag_summary = ", ".join(
        f"{f['flag']} ({f['severity'].upper()})"
        for f in (pred.counter_flags or [])
    )
    # Append to alert line:
    f" | Risk: {pred.counter_risk_score} | Net: {net:+d}"
    f"\n   Flags: {flag_summary}" if flag_summary else ""
```

- [ ] **Step 5: Run full test suite**

Run: `uv run pytest --tb=short -q`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add scout/main.py
git commit -m "feat(counter): wire counter-scoring into both pipelines — async follow-up for existing, inline for narrative"
```

---

## Build Order Summary

| Task | What | Dependencies | 
|------|------|-------------|
| 1 | Models + Config | None |
| 2 | Flag Computation | Task 1 |
| 3 | Detail Fetcher + Cache | Task 1 |
| 4 | Prompts | Task 1 |
| 5 | Scorer (Orchestrator) | Tasks 1-4 |
| 6 | DB Schema + Model Updates | Task 1 |
| 7 | Pipeline Integration | Tasks 1-6 |
