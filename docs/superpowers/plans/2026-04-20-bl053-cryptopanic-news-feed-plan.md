# BL-053 — CryptoPanic News Feed Watcher — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a feature-flagged CryptoPanic news-feed watcher that tags candidate tokens with news context (count, sentiment, macro flag, ticker-only-confidence) and persists post history for future analysis. Research-only — no alerts, scoring signal shipped-but-gated.

**Architecture:** New `scout/news/` package (schemas + fetcher + enricher) hooks into `run_cycle()` after aggregation. News fetch runs concurrently with holder/vol_7d enrichment as an `asyncio.create_task` with a 10s await timeout. Posts persist to a new additive `cryptopanic_posts` SQLite table. Four new `CandidateToken` fields tag matched candidates by ticker (collision-tagged via `news_tag_confidence`).

**Tech Stack:** Python 3.11+, aiohttp, aiosqlite, pydantic v2, structlog, pytest-asyncio, aioresponses.

**Spec:** `docs/superpowers/specs/2026-04-20-bl053-cryptopanic-news-feed-design.md`

---

## Pre-flight

- [ ] **Step 0.1: Verify baseline tests pass**

Run: `uv run pytest --tb=short -q 2>&1 | tail -5`
Expected: Baseline green (~836 passed).

- [ ] **Step 0.2: Confirm branch**

Run: `git rev-parse --abbrev-ref HEAD`
Expected: `feat/bl-053-cryptopanic-news-feed`

---

## Task 1: Config additions

Add the six BL-053 settings. Default-off — the whole feature is inert after merge.

**Files:**
- Modify: `scout/config.py` (insert new section near other feature-flag groups)
- Modify: `.env.example` (append new section at end)
- Test: `tests/test_config_cryptopanic.py` (new)

- [ ] **Step 1.1: Write failing test**

Create `tests/test_config_cryptopanic.py`:

```python
"""Tests for BL-053 CryptoPanic config additions."""

import os
from unittest.mock import patch

from scout.config import Settings


def test_cryptopanic_defaults():
    s = Settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
    assert s.CRYPTOPANIC_ENABLED is False
    assert s.CRYPTOPANIC_API_TOKEN == ""
    assert s.CRYPTOPANIC_FETCH_FILTER == "hot"
    assert s.CRYPTOPANIC_MACRO_MIN_CURRENCIES == 4
    assert s.CRYPTOPANIC_SCORING_ENABLED is False
    assert s.CRYPTOPANIC_RETENTION_DAYS == 7


def test_cryptopanic_env_overrides():
    env = {
        "TELEGRAM_BOT_TOKEN": "t",
        "TELEGRAM_CHAT_ID": "c",
        "ANTHROPIC_API_KEY": "k",
        "CRYPTOPANIC_ENABLED": "true",
        "CRYPTOPANIC_API_TOKEN": "abc123",
        "CRYPTOPANIC_FETCH_FILTER": "important",
        "CRYPTOPANIC_MACRO_MIN_CURRENCIES": "5",
        "CRYPTOPANIC_SCORING_ENABLED": "true",
        "CRYPTOPANIC_RETENTION_DAYS": "14",
    }
    with patch.dict(os.environ, env, clear=False):
        s = Settings()
    assert s.CRYPTOPANIC_ENABLED is True
    assert s.CRYPTOPANIC_API_TOKEN == "abc123"
    assert s.CRYPTOPANIC_FETCH_FILTER == "important"
    assert s.CRYPTOPANIC_MACRO_MIN_CURRENCIES == 5
    assert s.CRYPTOPANIC_SCORING_ENABLED is True
    assert s.CRYPTOPANIC_RETENTION_DAYS == 14
```

- [ ] **Step 1.2: Verify test fails**

Run: `uv run pytest tests/test_config_cryptopanic.py -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'CRYPTOPANIC_ENABLED'` (or similar).

- [ ] **Step 1.3: Add config block**

In `scout/config.py`, insert immediately after the `LUNARCRUSH_*` block (around line 180, just before `# -------- Paper Trading Engine --------`):

```python
    # -------- CryptoPanic News Feed (BL-053) --------
    # Research-only news tagging for candidate tokens. Free CryptoPanic v1 tier
    # requires a free API token; if empty, fetch short-circuits to [] without
    # hitting the network. Scoring signal exists but is gated by
    # CRYPTOPANIC_SCORING_ENABLED (off by default); flipping it on in a future
    # PR will require a SCORER_MAX_RAW bump from 198 to 208.
    CRYPTOPANIC_ENABLED: bool = False
    CRYPTOPANIC_API_TOKEN: str = ""
    CRYPTOPANIC_FETCH_FILTER: str = "hot"  # hot|rising|bullish|bearish|important
    CRYPTOPANIC_MACRO_MIN_CURRENCIES: int = 4
    CRYPTOPANIC_SCORING_ENABLED: bool = False
    CRYPTOPANIC_RETENTION_DAYS: int = 7
```

- [ ] **Step 1.4: Append `.env.example` section**

Append to the END of `.env.example`:

```bash

# === CryptoPanic News Feed (BL-053) ===
# Research-only: tags candidate tokens with news context. Free token required
# (https://cryptopanic.com/developers/api/keys — leave blank to disable).
CRYPTOPANIC_ENABLED=false
CRYPTOPANIC_API_TOKEN=
CRYPTOPANIC_FETCH_FILTER=hot
CRYPTOPANIC_MACRO_MIN_CURRENCIES=4
CRYPTOPANIC_SCORING_ENABLED=false
CRYPTOPANIC_RETENTION_DAYS=7
```

- [ ] **Step 1.5: Verify test passes**

Run: `uv run pytest tests/test_config_cryptopanic.py -v`
Expected: PASS (2 tests).

- [ ] **Step 1.6: Commit**

```bash
git add scout/config.py .env.example tests/test_config_cryptopanic.py
git commit -m "feat(bl-053): add CryptoPanic feature-flag config block"
```

---

## Task 2: CandidateToken field additions

Add the four new optional fields. All default to `None` so pre-existing tokens and tests are unaffected.

**Files:**
- Modify: `scout/models.py` (add fields near cg_trending_rank)
- Test: `tests/test_models_cryptopanic_fields.py` (new)

- [ ] **Step 2.1: Write failing test**

Create `tests/test_models_cryptopanic_fields.py`:

```python
"""Tests for BL-053 CandidateToken field additions."""

from scout.models import CandidateToken


def test_cryptopanic_fields_default_to_none():
    t = CandidateToken(
        contract_address="0xtest",
        chain="ethereum",
        token_name="Test",
        ticker="TST",
    )
    assert t.news_count_24h is None
    assert t.latest_news_sentiment is None
    assert t.macro_news_flag is None
    assert t.news_tag_confidence is None


def test_cryptopanic_fields_accept_values():
    t = CandidateToken(
        contract_address="0xtest",
        chain="ethereum",
        token_name="Test",
        ticker="TST",
        news_count_24h=3,
        latest_news_sentiment="bullish",
        macro_news_flag=False,
        news_tag_confidence="ticker_only",
    )
    assert t.news_count_24h == 3
    assert t.latest_news_sentiment == "bullish"
    assert t.macro_news_flag is False
    assert t.news_tag_confidence == "ticker_only"


def test_cryptopanic_fields_literal_rejects_invalid():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CandidateToken(
            contract_address="0xtest",
            chain="ethereum",
            token_name="Test",
            ticker="TST",
            latest_news_sentiment="excited",  # not in Literal
        )


def test_cryptopanic_fields_roundtrip_serialization():
    t = CandidateToken(
        contract_address="0xtest",
        chain="ethereum",
        token_name="Test",
        ticker="TST",
        news_count_24h=2,
        latest_news_sentiment="bearish",
        macro_news_flag=True,
        news_tag_confidence="ticker_only",
    )
    dumped = t.model_dump()
    restored = CandidateToken(**dumped)
    assert restored.news_count_24h == 2
    assert restored.latest_news_sentiment == "bearish"
    assert restored.macro_news_flag is True
    assert restored.news_tag_confidence == "ticker_only"


def test_token_factory_default_has_none_news_fields(token_factory):
    """conftest token_factory() should not populate the new fields."""
    t = token_factory()
    assert t.news_count_24h is None
    assert t.latest_news_sentiment is None
    assert t.macro_news_flag is None
    assert t.news_tag_confidence is None
```

- [ ] **Step 2.2: Verify test fails**

Run: `uv run pytest tests/test_models_cryptopanic_fields.py -v`
Expected: FAIL — `AttributeError` or `ValidationError: Extra inputs` on the fields.

- [ ] **Step 2.3: Add fields to model**

In `scout/models.py`, at the top add to imports:

```python
from typing import Literal
```

Then inside `CandidateToken`, insert immediately after the `cg_trending_rank` line (currently line 43):

```python

    # CryptoPanic news tags (BL-053)
    news_count_24h: int | None = None
    latest_news_sentiment: Literal["bullish", "bearish", "neutral"] | None = None
    macro_news_flag: bool | None = None
    news_tag_confidence: Literal["ticker_only"] | None = None
```

- [ ] **Step 2.4: Verify test passes**

Run: `uv run pytest tests/test_models_cryptopanic_fields.py -v`
Expected: PASS (5 tests).

- [ ] **Step 2.5: Verify no regression**

Run: `uv run pytest tests/test_models.py tests/test_scorer.py tests/test_aggregator.py -q`
Expected: all pass (existing tests untouched).

- [ ] **Step 2.6: Commit**

```bash
git add scout/models.py tests/test_models_cryptopanic_fields.py
git commit -m "feat(bl-053): add news tagging fields to CandidateToken"
```

---

## Task 3: CryptoPanicPost schema + sentiment + macro helpers

A pure-data module: pydantic model for a post + two classification helpers. No I/O.

**Files:**
- Create: `scout/news/__init__.py`
- Create: `scout/news/schemas.py`
- Test: `tests/test_cryptopanic_schemas.py` (new)

- [ ] **Step 3.1: Write failing test**

Create `tests/test_cryptopanic_schemas.py`:

```python
"""Tests for CryptoPanicPost schema + classification helpers."""

from scout.news.schemas import (
    CryptoPanicPost,
    classify_sentiment,
    classify_macro,
    parse_post,
)


def test_parse_post_minimal():
    raw = {
        "id": 123,
        "title": "Hello",
        "url": "https://cryptopanic.com/news/123",
        "published_at": "2026-04-20T12:00:00Z",
        "currencies": [{"code": "BTC", "title": "Bitcoin"}],
        "votes": {"positive": 5, "negative": 1},
    }
    post = parse_post(raw)
    assert post.post_id == 123
    assert post.title == "Hello"
    assert post.currencies == ["BTC"]
    assert post.votes_positive == 5
    assert post.votes_negative == 1


def test_parse_post_currencies_null_treated_as_empty():
    raw = {
        "id": 9,
        "title": "t",
        "url": "u",
        "published_at": "2026-04-20T00:00:00Z",
        "currencies": None,
        "votes": {},
    }
    post = parse_post(raw)
    assert post.currencies == []


def test_parse_post_skips_missing_code():
    raw = {
        "id": 9,
        "title": "t",
        "url": "u",
        "published_at": "2026-04-20T00:00:00Z",
        "currencies": [{"code": "BTC"}, {"title": "no code"}, {"code": ""}],
        "votes": {},
    }
    post = parse_post(raw)
    assert post.currencies == ["BTC"]


def test_parse_post_missing_required_returns_none():
    raw = {"id": 9}  # no title/url/published_at
    assert parse_post(raw) is None


def test_sentiment_bullish():
    assert classify_sentiment(positive=5, negative=1) == "bullish"


def test_sentiment_bearish():
    assert classify_sentiment(positive=0, negative=3) == "bearish"


def test_sentiment_neutral_when_both_zero():
    assert classify_sentiment(positive=0, negative=0) == "neutral"


def test_sentiment_neutral_at_tie():
    assert classify_sentiment(positive=4, negative=4) == "neutral"


def test_sentiment_neutral_when_delta_below_threshold():
    assert classify_sentiment(positive=3, negative=2) == "neutral"  # delta=1 < 2


def test_sentiment_exact_threshold_bullish():
    assert classify_sentiment(positive=3, negative=1) == "bullish"  # delta=2 meets >=


def test_macro_empty_currencies_is_macro():
    assert classify_macro([], threshold=4) is True


def test_macro_below_threshold_not_macro():
    assert classify_macro(["BTC", "ETH", "SOL"], threshold=4) is False


def test_macro_at_threshold_is_macro():
    assert classify_macro(["BTC", "ETH", "SOL", "AVAX"], threshold=4) is True
```

- [ ] **Step 3.2: Verify test fails**

Run: `uv run pytest tests/test_cryptopanic_schemas.py -v`
Expected: FAIL — `ModuleNotFoundError: scout.news`.

- [ ] **Step 3.3: Create package marker**

Create `scout/news/__init__.py` (empty file):

```python
"""CryptoPanic news feed watcher (BL-053)."""
```

- [ ] **Step 3.4: Create schema module**

Create `scout/news/schemas.py`:

```python
"""CryptoPanic post schema + classification helpers. Pure, no I/O."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Sentiment = Literal["bullish", "bearish", "neutral"]

# Delta threshold for vote-based sentiment classification. Using integer
# deltas rather than ratios keeps fresh posts (low vote counts) from being
# miscategorized into bullish/bearish on noise.
_SENTIMENT_DELTA = 2


class CryptoPanicPost(BaseModel):
    """Normalized view of a CryptoPanic post.

    Raw posts have more fields (source, slug, kind, ...) but we only keep
    what's needed for tagging + persistence.
    """

    post_id: int
    title: str
    url: str
    published_at: str  # ISO8601
    currencies: list[str] = Field(default_factory=list)
    votes_positive: int = 0
    votes_negative: int = 0


def classify_sentiment(positive: int, negative: int) -> Sentiment:
    """Classify a post as bullish/bearish/neutral from vote deltas."""
    if positive >= negative + _SENTIMENT_DELTA:
        return "bullish"
    if negative >= positive + _SENTIMENT_DELTA:
        return "bearish"
    return "neutral"


def classify_macro(currencies: list[str], *, threshold: int) -> bool:
    """A post is macro if it tags zero or >=threshold currencies."""
    n = len(currencies)
    return n == 0 or n >= threshold


def parse_post(raw: dict) -> CryptoPanicPost | None:
    """Parse a raw CryptoPanic post dict into a CryptoPanicPost.

    Returns None when required fields (id / title / url / published_at) are
    missing. `currencies: null` is coerced to []. Currency entries with
    empty or missing `code` are dropped.
    """
    post_id = raw.get("id")
    title = raw.get("title")
    url = raw.get("url")
    published_at = raw.get("published_at")
    if not (
        isinstance(post_id, int)
        and isinstance(title, str)
        and isinstance(url, str)
        and isinstance(published_at, str)
    ):
        return None

    raw_currencies = raw.get("currencies") or []
    codes: list[str] = []
    for c in raw_currencies:
        if not isinstance(c, dict):
            continue
        code = c.get("code")
        if isinstance(code, str) and code:
            codes.append(code)

    votes = raw.get("votes") or {}
    return CryptoPanicPost(
        post_id=post_id,
        title=title,
        url=url,
        published_at=published_at,
        currencies=codes,
        votes_positive=int(votes.get("positive") or 0),
        votes_negative=int(votes.get("negative") or 0),
    )
```

- [ ] **Step 3.5: Verify tests pass**

Run: `uv run pytest tests/test_cryptopanic_schemas.py -v`
Expected: PASS (12 tests).

- [ ] **Step 3.6: Commit**

```bash
git add scout/news/__init__.py scout/news/schemas.py tests/test_cryptopanic_schemas.py
git commit -m "feat(bl-053): add CryptoPanicPost schema + sentiment/macro helpers"
```

---

## Task 4: Async fetch with retries + dedup

HTTP fetch with aioresponses-based tests covering every error path in spec §12.

**Files:**
- Create: `scout/news/cryptopanic.py`
- Test: `tests/test_cryptopanic_fetch.py` (new)

- [ ] **Step 4.1: Write failing test**

Create `tests/test_cryptopanic_fetch.py`:

```python
"""Tests for async CryptoPanic fetcher."""

import aiohttp
import pytest
from aioresponses import aioresponses
from structlog.testing import capture_logs

from scout.config import Settings
from scout.news.cryptopanic import fetch_cryptopanic_posts

BASE = "https://cryptopanic.com/api/v1/posts/"


def _settings(**overrides):
    defaults = dict(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        CRYPTOPANIC_ENABLED=True,
        CRYPTOPANIC_API_TOKEN="tok",
    )
    defaults.update(overrides)
    return Settings(**defaults)


async def test_disabled_flag_short_circuits():
    s = _settings(CRYPTOPANIC_ENABLED=False)
    async with aiohttp.ClientSession() as session:
        result = await fetch_cryptopanic_posts(session, s)
    assert result == []


async def test_missing_token_short_circuits_without_network():
    s = _settings(CRYPTOPANIC_API_TOKEN="")
    with capture_logs() as logs:
        async with aiohttp.ClientSession() as session:
            result = await fetch_cryptopanic_posts(session, s)
    assert result == []
    assert any(log["event"] == "cryptopanic_auth_missing" for log in logs)


async def test_fetch_happy_path():
    s = _settings()
    body = {
        "results": [
            {
                "id": 1,
                "title": "A",
                "url": "u1",
                "published_at": "2026-04-20T10:00:00Z",
                "currencies": [{"code": "BTC"}],
                "votes": {"positive": 5, "negative": 1},
            },
            {
                "id": 2,
                "title": "B",
                "url": "u2",
                "published_at": "2026-04-20T09:00:00Z",
                "currencies": [],
                "votes": {},
            },
        ]
    }
    with aioresponses() as m:
        m.get(BASE, payload=body, status=200, repeat=True)
        async with aiohttp.ClientSession() as session:
            result = await fetch_cryptopanic_posts(session, s)
    assert len(result) == 2
    assert result[0].post_id == 1


async def test_fetch_empty_results():
    s = _settings()
    with aioresponses() as m:
        m.get(BASE, payload={"results": []}, status=200, repeat=True)
        async with aiohttp.ClientSession() as session:
            result = await fetch_cryptopanic_posts(session, s)
    assert result == []


async def test_fetch_malformed_body_returns_empty():
    s = _settings()
    with aioresponses() as m:
        m.get(BASE, body="not json", status=200, repeat=True)
        async with aiohttp.ClientSession() as session:
            result = await fetch_cryptopanic_posts(session, s)
    assert result == []


async def test_fetch_401_returns_empty():
    s = _settings()
    with aioresponses() as m:
        m.get(BASE, status=401, repeat=True)
        async with aiohttp.ClientSession() as session:
            result = await fetch_cryptopanic_posts(session, s)
    assert result == []


async def test_fetch_429_retries_then_empty():
    s = _settings()
    with aioresponses() as m:
        m.get(BASE, status=429, repeat=True)
        async with aiohttp.ClientSession() as session:
            result = await fetch_cryptopanic_posts(session, s)
    assert result == []


async def test_fetch_5xx_retries_then_empty():
    s = _settings()
    with aioresponses() as m:
        m.get(BASE, status=503, repeat=True)
        async with aiohttp.ClientSession() as session:
            result = await fetch_cryptopanic_posts(session, s)
    assert result == []


async def test_fetch_200_after_429_succeeds():
    s = _settings()
    body = {"results": [{"id": 1, "title": "T", "url": "u", "published_at": "2026-04-20T00:00:00Z", "currencies": [], "votes": {}}]}
    with aioresponses() as m:
        m.get(BASE, status=429)  # first call: 429
        m.get(BASE, payload=body, status=200)  # retry: 200
        async with aiohttp.ClientSession() as session:
            result = await fetch_cryptopanic_posts(session, s)
    assert len(result) == 1
    assert result[0].post_id == 1


async def test_fetch_dedups_duplicate_post_ids_in_batch():
    s = _settings()
    body = {
        "results": [
            {"id": 5, "title": "first", "url": "u5", "published_at": "2026-04-20T00:00:00Z", "currencies": [], "votes": {}},
            {"id": 5, "title": "dup", "url": "u5", "published_at": "2026-04-20T00:00:00Z", "currencies": [], "votes": {}},
            {"id": 6, "title": "second", "url": "u6", "published_at": "2026-04-20T00:00:00Z", "currencies": [], "votes": {}},
        ]
    }
    with aioresponses() as m:
        m.get(BASE, payload=body, status=200, repeat=True)
        async with aiohttp.ClientSession() as session:
            result = await fetch_cryptopanic_posts(session, s)
    assert [p.post_id for p in result] == [5, 6]
```

- [ ] **Step 4.2: Verify test fails**

Run: `uv run pytest tests/test_cryptopanic_fetch.py -v`
Expected: FAIL — `ModuleNotFoundError` or `ImportError: cannot import name 'fetch_cryptopanic_posts'`.

- [ ] **Step 4.3: Implement fetcher**

Create `scout/news/cryptopanic.py`:

```python
"""Async CryptoPanic fetcher + candidate enricher (BL-053)."""

from __future__ import annotations

import asyncio

import aiohttp
import structlog

from scout.config import Settings
from scout.news.schemas import CryptoPanicPost, parse_post

logger = structlog.get_logger(__name__)

BASE_URL = "https://cryptopanic.com/api/v1/posts/"
MAX_RETRIES = 3
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)


async def fetch_cryptopanic_posts(
    session: aiohttp.ClientSession,
    settings: Settings,
) -> list[CryptoPanicPost]:
    """Fetch hot/rising/etc posts from CryptoPanic v1.

    Short-circuits to [] when the feature is disabled or the token is empty.
    Never raises — all network / parse / auth errors return [].
    """
    if not settings.CRYPTOPANIC_ENABLED:
        return []
    if not settings.CRYPTOPANIC_API_TOKEN:
        logger.warning("cryptopanic_auth_missing")
        return []

    params = {
        "auth_token": settings.CRYPTOPANIC_API_TOKEN,
        "filter": settings.CRYPTOPANIC_FETCH_FILTER,
        "public": "true",
    }

    logger.info("cryptopanic_fetch_started", filter=settings.CRYPTOPANIC_FETCH_FILTER)

    raw_results: list[dict] = []
    for attempt in range(MAX_RETRIES):
        try:
            async with session.get(
                BASE_URL, params=params, timeout=REQUEST_TIMEOUT
            ) as resp:
                if resp.status in (401, 403):
                    logger.warning(
                        "cryptopanic_fetch_failed",
                        status=resp.status,
                        error="auth",
                    )
                    return []
                if resp.status == 429 or resp.status >= 500:
                    wait = 2 ** (attempt + 1)
                    logger.warning(
                        "cryptopanic_retry",
                        status=resp.status,
                        wait=wait,
                        attempt=attempt + 1,
                    )
                    await asyncio.sleep(wait)
                    continue
                if resp.status != 200:
                    logger.warning("cryptopanic_fetch_failed", status=resp.status)
                    return []
                try:
                    data = await resp.json()
                except Exception as e:
                    logger.warning("cryptopanic_fetch_failed", error=f"json:{e!s}")
                    return []
                raw_results = data.get("results") or []
                break
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            wait = 2 ** (attempt + 1)
            logger.warning(
                "cryptopanic_retry",
                error=str(e),
                wait=wait,
                attempt=attempt + 1,
            )
            await asyncio.sleep(wait)
    else:
        logger.warning("cryptopanic_fetch_failed", error="retries_exhausted")
        return []

    posts: list[CryptoPanicPost] = []
    seen: set[int] = set()
    for raw in raw_results:
        post = parse_post(raw)
        if post is None:
            continue
        if post.post_id in seen:
            continue
        seen.add(post.post_id)
        posts.append(post)

    logger.info("cryptopanic_fetch_completed", count=len(posts))
    return posts
```

- [ ] **Step 4.4: Verify tests pass**

Run: `uv run pytest tests/test_cryptopanic_fetch.py -v`
Expected: PASS (10 tests). Retry tests take ~12s total due to backoff sleeps.

- [ ] **Step 4.5: Commit**

```bash
git add scout/news/cryptopanic.py tests/test_cryptopanic_fetch.py
git commit -m "feat(bl-053): async CryptoPanic fetcher with retries + dedup"
```

---

## Task 5: DB schema + persist + prune

New table, idempotent migration, idempotent insert, bounded-age prune.

**Files:**
- Modify: `scout/db.py` (add schema + two methods)
- Test: `tests/test_cryptopanic_db.py` (new)

- [ ] **Step 5.1: Write failing test**

Create `tests/test_cryptopanic_db.py`:

```python
"""Tests for cryptopanic_posts table + persist/prune methods."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.news.schemas import CryptoPanicPost


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    await d.initialize()
    yield d
    await d.close()


def _post(post_id: int, published_at: str, title: str = "t") -> CryptoPanicPost:
    return CryptoPanicPost(
        post_id=post_id,
        title=title,
        url=f"u/{post_id}",
        published_at=published_at,
        currencies=["BTC"],
        votes_positive=1,
        votes_negative=0,
    )


async def test_initialize_is_idempotent(tmp_path):
    path = str(tmp_path / "t.db")
    d1 = Database(path)
    await d1.initialize()
    await d1.close()
    # Second init on same file must not raise
    d2 = Database(path)
    await d2.initialize()
    await d2.close()


async def test_insert_cryptopanic_post(db):
    p = _post(1, "2026-04-20T10:00:00Z")
    inserted = await db.insert_cryptopanic_post(p, is_macro=False, sentiment="bullish")
    assert inserted == 1
    rows = await db.fetch_all_cryptopanic_posts()
    assert len(rows) == 1
    assert rows[0]["post_id"] == 1
    assert rows[0]["sentiment"] == "bullish"
    assert rows[0]["is_macro"] == 0
    assert json.loads(rows[0]["currencies_json"]) == ["BTC"]


async def test_insert_dup_post_id_is_idempotent(db):
    p = _post(42, "2026-04-20T10:00:00Z")
    await db.insert_cryptopanic_post(p, is_macro=False, sentiment="bullish")
    await db.insert_cryptopanic_post(p, is_macro=True, sentiment="bearish")
    rows = await db.fetch_all_cryptopanic_posts()
    assert len(rows) == 1  # second insert ignored


async def test_prune_cryptopanic_posts_keeps_recent(db):
    fresh = datetime.now(timezone.utc).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    await db.insert_cryptopanic_post(_post(1, fresh), is_macro=False, sentiment="neutral")
    await db.insert_cryptopanic_post(_post(2, stale), is_macro=False, sentiment="neutral")
    pruned = await db.prune_cryptopanic_posts(keep_days=7)
    assert pruned == 1
    rows = await db.fetch_all_cryptopanic_posts()
    assert len(rows) == 1
    assert rows[0]["post_id"] == 1
```

- [ ] **Step 5.2: Verify test fails**

Run: `uv run pytest tests/test_cryptopanic_db.py -v`
Expected: FAIL — `AttributeError: 'Database' object has no attribute 'insert_cryptopanic_post'`.

- [ ] **Step 5.3: Add table to `_create_tables`**

In `scout/db.py`, inside `_create_tables` at the end of the `executescript("""...""")` string — i.e. just before the closing `""")` — append (matching the existing indentation level):

```sql

            CREATE TABLE IF NOT EXISTS cryptopanic_posts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id         INTEGER UNIQUE NOT NULL,
                title           TEXT NOT NULL,
                url             TEXT NOT NULL,
                published_at    TEXT NOT NULL,
                currencies_json TEXT NOT NULL,
                is_macro        INTEGER NOT NULL,
                sentiment       TEXT NOT NULL,
                votes_positive  INTEGER NOT NULL DEFAULT 0,
                votes_negative  INTEGER NOT NULL DEFAULT 0,
                fetched_at      TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS ix_cryptopanic_published_at
                ON cryptopanic_posts(published_at DESC);
```

- [ ] **Step 5.4: Add persist + prune + fetch methods**

Append to the `Database` class in `scout/db.py` (after existing methods, before file end):

```python
    async def insert_cryptopanic_post(
        self,
        post,  # scout.news.schemas.CryptoPanicPost
        *,
        is_macro: bool,
        sentiment: str,
    ) -> int:
        """INSERT OR IGNORE a CryptoPanic post. Returns rowcount (0 or 1)."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        fetched_at = datetime.now(timezone.utc).isoformat()
        cur = await self._conn.execute(
            """
            INSERT OR IGNORE INTO cryptopanic_posts (
                post_id, title, url, published_at, currencies_json,
                is_macro, sentiment, votes_positive, votes_negative, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                post.post_id,
                post.title,
                post.url,
                post.published_at,
                json.dumps(post.currencies),
                1 if is_macro else 0,
                sentiment,
                post.votes_positive,
                post.votes_negative,
                fetched_at,
            ),
        )
        await self._conn.commit()
        return cur.rowcount or 0

    async def fetch_all_cryptopanic_posts(self) -> list[dict]:
        """Return all rows (test helper)."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cur = await self._conn.execute(
            "SELECT * FROM cryptopanic_posts ORDER BY published_at DESC"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def prune_cryptopanic_posts(self, *, keep_days: int) -> int:
        """Delete rows with published_at older than keep_days. Returns rowcount."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM cryptopanic_posts WHERE published_at < ?",
            (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount or 0
```

Also add `timedelta` to the `datetime` import at the top of `scout/db.py`:

```python
from datetime import datetime, timedelta, timezone
```

- [ ] **Step 5.5: Verify tests pass**

Run: `uv run pytest tests/test_cryptopanic_db.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5.6: Verify DB init regression**

Run: `uv run pytest tests/test_db.py -q`
Expected: all existing DB tests still pass.

- [ ] **Step 5.7: Commit**

```bash
git add scout/db.py tests/test_cryptopanic_db.py
git commit -m "feat(bl-053): add cryptopanic_posts table + insert/prune methods"
```

---

## Task 6: Enrichment (match posts to candidates)

Given `list[CandidateToken]` + `list[CryptoPanicPost]` + settings, return the tagged list (pure function). Uses most-recent matched post for `latest_news_sentiment`, any-macro for `macro_news_flag`.

**Files:**
- Modify: `scout/news/cryptopanic.py` (add enrichment function)
- Test: `tests/test_cryptopanic_enrichment.py` (new)

- [ ] **Step 6.1: Write failing test**

Create `tests/test_cryptopanic_enrichment.py`:

```python
"""Tests for CryptoPanic candidate enrichment (tagging)."""

import pytest

from scout.config import Settings
from scout.models import CandidateToken
from scout.news.cryptopanic import enrich_candidates_with_news
from scout.news.schemas import CryptoPanicPost


def _settings(**overrides):
    defaults = dict(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        CRYPTOPANIC_MACRO_MIN_CURRENCIES=4,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _token(ticker: str) -> CandidateToken:
    return CandidateToken(
        contract_address="0x" + ticker.lower(),
        chain="ethereum",
        token_name=ticker,
        ticker=ticker,
    )


def _post(pid: int, published_at: str, currencies: list[str], pos=0, neg=0):
    return CryptoPanicPost(
        post_id=pid,
        title="t",
        url=f"u/{pid}",
        published_at=published_at,
        currencies=currencies,
        votes_positive=pos,
        votes_negative=neg,
    )


def test_no_posts_leaves_fields_none():
    s = _settings()
    tokens = [_token("PEPE")]
    out = enrich_candidates_with_news(tokens, [], s)
    assert out[0].news_count_24h is None
    assert out[0].latest_news_sentiment is None
    assert out[0].macro_news_flag is None
    assert out[0].news_tag_confidence is None


def test_no_match_leaves_fields_none():
    s = _settings()
    tokens = [_token("PEPE")]
    posts = [_post(1, "2026-04-20T10:00:00Z", ["DOGE"], pos=5)]
    out = enrich_candidates_with_news(tokens, posts, s)
    assert out[0].news_count_24h is None
    assert out[0].news_tag_confidence is None


def test_case_insensitive_match():
    s = _settings()
    tokens = [_token("PEPE")]
    posts = [_post(1, "2026-04-20T10:00:00Z", ["pepe"], pos=5)]
    out = enrich_candidates_with_news(tokens, posts, s)
    assert out[0].news_count_24h == 1
    assert out[0].latest_news_sentiment == "bullish"
    assert out[0].macro_news_flag is False
    assert out[0].news_tag_confidence == "ticker_only"


def test_counts_multiple_matches():
    s = _settings()
    tokens = [_token("BTC")]
    posts = [
        _post(1, "2026-04-20T10:00:00Z", ["BTC"], pos=5, neg=1),
        _post(2, "2026-04-20T08:00:00Z", ["BTC"], pos=0, neg=3),
    ]
    out = enrich_candidates_with_news(tokens, posts, s)
    assert out[0].news_count_24h == 2


def test_sentiment_comes_from_most_recent_post():
    s = _settings()
    tokens = [_token("BTC")]
    posts = [
        _post(1, "2026-04-20T08:00:00Z", ["BTC"], pos=5, neg=0),  # older, bullish
        _post(2, "2026-04-20T10:00:00Z", ["BTC"], pos=0, neg=5),  # newer, bearish
    ]
    out = enrich_candidates_with_news(tokens, posts, s)
    assert out[0].latest_news_sentiment == "bearish"


def test_macro_flag_true_if_any_matched_post_is_macro():
    s = _settings(CRYPTOPANIC_MACRO_MIN_CURRENCIES=4)
    tokens = [_token("ETH")]
    posts = [
        _post(1, "2026-04-20T10:00:00Z", ["ETH"]),  # token-specific
        _post(2, "2026-04-20T09:00:00Z", ["BTC", "ETH", "SOL", "AVAX"]),  # macro
    ]
    out = enrich_candidates_with_news(tokens, posts, s)
    assert out[0].macro_news_flag is True


def test_empty_currencies_list_is_macro():
    s = _settings()
    tokens = [_token("ETH")]
    posts = [_post(1, "2026-04-20T10:00:00Z", [])]  # no currencies → macro → no match
    out = enrich_candidates_with_news(tokens, posts, s)
    # Macro post with no currencies doesn't match any specific ticker
    assert out[0].news_count_24h is None


def test_preserves_existing_fields():
    s = _settings()
    tokens = [
        CandidateToken(
            contract_address="0xbtc",
            chain="ethereum",
            token_name="BTC",
            ticker="BTC",
            quant_score=42,
        )
    ]
    posts = [_post(1, "2026-04-20T10:00:00Z", ["BTC"], pos=5)]
    out = enrich_candidates_with_news(tokens, posts, s)
    assert out[0].quant_score == 42  # untouched
```

- [ ] **Step 6.2: Verify test fails**

Run: `uv run pytest tests/test_cryptopanic_enrichment.py -v`
Expected: FAIL — `ImportError: cannot import name 'enrich_candidates_with_news'`.

- [ ] **Step 6.3: Append enrichment function**

Append to `scout/news/cryptopanic.py` (after `fetch_cryptopanic_posts`):

```python
from scout.models import CandidateToken
from scout.news.schemas import classify_macro, classify_sentiment


def enrich_candidates_with_news(
    tokens: list[CandidateToken],
    posts: list[CryptoPanicPost],
    settings: Settings,
) -> list[CandidateToken]:
    """Tag candidates with news context from the fetched posts batch.

    Pure — no I/O. Case-insensitive ticker match. `news_tag_confidence` is
    always 'ticker_only' for any tagged candidate; untagged candidates keep
    None for all four fields. Sentiment comes from the most-recently-
    published matched post.
    """
    if not tokens or not posts:
        return tokens

    macro_threshold = settings.CRYPTOPANIC_MACRO_MIN_CURRENCIES

    # Index posts by ticker.upper(). Posts with empty currencies (macro-only)
    # are not indexed — they cannot match a specific candidate by ticker.
    by_ticker: dict[str, list[CryptoPanicPost]] = {}
    for p in posts:
        for code in p.currencies:
            by_ticker.setdefault(code.upper(), []).append(p)

    if not by_ticker:
        return tokens

    tagged_count = 0
    out: list[CandidateToken] = []
    for tok in tokens:
        matches = by_ticker.get(tok.ticker.upper(), [])
        if not matches:
            out.append(tok)
            continue

        matches_sorted = sorted(matches, key=lambda m: m.published_at, reverse=True)
        newest = matches_sorted[0]
        sentiment = classify_sentiment(newest.votes_positive, newest.votes_negative)
        macro = any(classify_macro(m.currencies, threshold=macro_threshold) for m in matches)

        out.append(
            tok.model_copy(
                update={
                    "news_count_24h": len(matches),
                    "latest_news_sentiment": sentiment,
                    "macro_news_flag": macro,
                    "news_tag_confidence": "ticker_only",
                }
            )
        )
        tagged_count += 1

    logger.info(
        "cryptopanic_tokens_tagged",
        candidate_count=len(tokens),
        tagged_count=tagged_count,
    )
    return out
```

- [ ] **Step 6.4: Verify tests pass**

Run: `uv run pytest tests/test_cryptopanic_enrichment.py -v`
Expected: PASS (8 tests).

- [ ] **Step 6.5: Commit**

```bash
git add scout/news/cryptopanic.py tests/test_cryptopanic_enrichment.py
git commit -m "feat(bl-053): enrich candidates with news tags (ticker-match)"
```

---

## Task 7: Gated scoring signal

Ship the code path for Signal 13 but keep it behind `CRYPTOPANIC_SCORING_ENABLED`. SCORER_MAX_RAW **not** changed in this PR.

**Files:**
- Modify: `scout/scorer.py` (append Signal 13)
- Test: `tests/test_scorer_cryptopanic_gated.py` (new)

- [ ] **Step 7.1: Write failing test**

Create `tests/test_scorer_cryptopanic_gated.py`:

```python
"""Tests for BL-053 gated CryptoPanic scoring signal."""

from scout.scorer import score


def _tagged_token(token_factory, **extra):
    return token_factory(
        liquidity_usd=50000.0,
        volume_24h_usd=50000.0,
        news_count_24h=2,
        latest_news_sentiment="bullish",
        macro_news_flag=False,
        news_tag_confidence="ticker_only",
        **extra,
    )


def test_signal_silent_when_flag_false(settings_factory, token_factory):
    s = settings_factory(CRYPTOPANIC_SCORING_ENABLED=False)
    token = _tagged_token(token_factory)
    _, signals = score(token, s)
    assert "cryptopanic_bullish" not in signals


def test_signal_fires_when_flag_true_and_conditions_met(settings_factory, token_factory):
    s = settings_factory(CRYPTOPANIC_SCORING_ENABLED=True)
    token = _tagged_token(token_factory)
    _, signals = score(token, s)
    assert "cryptopanic_bullish" in signals


def test_signal_silent_when_bearish(settings_factory, token_factory):
    s = settings_factory(CRYPTOPANIC_SCORING_ENABLED=True)
    token = _tagged_token(token_factory, latest_news_sentiment="bearish")
    _, signals = score(token, s)
    assert "cryptopanic_bullish" not in signals


def test_signal_silent_when_macro(settings_factory, token_factory):
    s = settings_factory(CRYPTOPANIC_SCORING_ENABLED=True)
    token = _tagged_token(token_factory, macro_news_flag=True)
    _, signals = score(token, s)
    assert "cryptopanic_bullish" not in signals


def test_signal_silent_when_no_news(settings_factory, token_factory):
    s = settings_factory(CRYPTOPANIC_SCORING_ENABLED=True)
    token = _tagged_token(token_factory, news_count_24h=0)
    _, signals = score(token, s)
    assert "cryptopanic_bullish" not in signals


def test_score_still_bounded_100_even_when_flag_active(settings_factory, token_factory):
    """Guard: enabling the gated signal without bumping SCORER_MAX_RAW should
    never produce a score > 100 because of the min(points, 100) ceiling."""
    s = settings_factory(CRYPTOPANIC_SCORING_ENABLED=True)
    token = _tagged_token(token_factory)
    points, _ = score(token, s)
    assert 0 <= points <= 100
```

- [ ] **Step 7.2: Verify test fails**

Run: `uv run pytest tests/test_scorer_cryptopanic_gated.py -v`
Expected: FAIL — `test_signal_fires_when_flag_true_and_conditions_met` fails because the signal isn't implemented yet.

- [ ] **Step 7.3: Append signal to scorer**

In `scout/scorer.py`, locate the "Signal 12: Score velocity bonus" block (around line 172-177) and insert the new block **before** the velocity signal (so "Signal 12" becomes "Signal 13" in spirit, though we keep them as code comments only — signal order doesn't affect scoring correctness because points are summed):

Actually, to preserve deterministic test output and keep signal-count semantics consistent, add Signal 13 **between** the solana bonus (Signal 11) and the score_velocity (Signal 12) sections. Find:

```python
    # Signal 12: Score velocity bonus -- 10 points
```

And insert immediately before it:

```python
    # Signal 13: CryptoPanic bullish news (BL-053) -- 10 points, gated.
    # SCORER_MAX_RAW is NOT bumped in this PR — the ceiling-clamp
    # `min(points, 100)` at the end of score() keeps outputs well-formed
    # while the flag is off. Flipping CRYPTOPANIC_SCORING_ENABLED to True
    # is an operator-visible distribution shift and should ship with
    # SCORER_MAX_RAW=208 + recalibrated tests in a follow-up PR.
    if (
        settings.CRYPTOPANIC_SCORING_ENABLED
        and token.latest_news_sentiment == "bullish"
        and (token.news_count_24h or 0) >= 1
        and not token.macro_news_flag
    ):
        points += 10
        signals.append("cryptopanic_bullish")
        logger.info(
            "cryptopanic_sigfire",
            token=token.ticker,
            contract_address=token.contract_address,
            sentiment=token.latest_news_sentiment,
            news_count_24h=token.news_count_24h,
        )

```

- [ ] **Step 7.4: Verify tests pass**

Run: `uv run pytest tests/test_scorer_cryptopanic_gated.py -v`
Expected: PASS (6 tests).

- [ ] **Step 7.5: Verify no regression**

Run: `uv run pytest tests/test_scorer.py -q`
Expected: all existing scorer tests still pass (signal is flag-gated off by default).

- [ ] **Step 7.6: Commit**

```bash
git add scout/scorer.py tests/test_scorer_cryptopanic_gated.py
git commit -m "feat(bl-053): add gated cryptopanic_bullish scoring signal (off by default)"
```

---

## Task 8: Wire into run_cycle

Plug fetch + enrich + persist into `scout/main.py`. Must be strictly additive: flag off → zero behaviour change.

**Files:**
- Modify: `scout/main.py` (add import + integration block in `run_cycle`)
- Test: `tests/test_main_cryptopanic_integration.py` (new)

- [ ] **Step 8.1: Write failing integration test**

Create `tests/test_main_cryptopanic_integration.py`:

```python
"""Integration test: run_cycle with CryptoPanic enabled."""

from unittest.mock import AsyncMock, patch

import pytest

from scout.db import Database
from scout.main import run_cycle
from scout.models import CandidateToken
from scout.news.schemas import CryptoPanicPost


def _btc_candidate() -> CandidateToken:
    return CandidateToken(
        contract_address="0xbtctest",
        chain="ethereum",
        token_name="Bitcoin",
        ticker="BTC",
        market_cap_usd=50000.0,
        liquidity_usd=30000.0,
        volume_24h_usd=100000.0,
    )


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    await d.initialize()
    yield d
    await d.close()


async def test_run_cycle_fetches_and_tags_when_enabled(settings_factory, db):
    settings = settings_factory(
        MIN_SCORE=1,
        CRYPTOPANIC_ENABLED=True,
        CRYPTOPANIC_API_TOKEN="tok",
    )

    hot_posts = [
        CryptoPanicPost(
            post_id=1,
            title="BTC moons",
            url="u",
            published_at="2026-04-20T10:00:00Z",
            currencies=["BTC"],
            votes_positive=10,
            votes_negative=0,
        )
    ]

    import aiohttp

    async with aiohttp.ClientSession() as session:
        with (
            patch("scout.main.fetch_trending", new=AsyncMock(return_value=[_btc_candidate()])),
            patch("scout.main.fetch_trending_pools", new=AsyncMock(return_value=[])),
            patch("scout.main.cg_fetch_top_movers", new=AsyncMock(return_value=[])),
            patch("scout.main.cg_fetch_trending", new=AsyncMock(return_value=[])),
            patch("scout.main.cg_fetch_by_volume", new=AsyncMock(return_value=[])),
            patch("scout.main.enrich_holders", new=AsyncMock(side_effect=lambda tok, s, st: tok)),
            patch(
                "scout.main.fetch_cryptopanic_posts",
                new=AsyncMock(return_value=hot_posts),
            ),
            patch("scout.main.evaluate", new=AsyncMock(return_value=(False, 0.0, _btc_candidate()))),
        ):
            await run_cycle(settings, db, session, dry_run=True)

    # DB row persisted
    rows = await db.fetch_all_cryptopanic_posts()
    assert len(rows) == 1
    assert rows[0]["post_id"] == 1

    # Candidate upserted with news tags
    cur = await db._conn.execute(
        "SELECT * FROM candidates WHERE contract_address = ?",
        ("0xbtctest",),
    )
    row = await cur.fetchone()
    assert row is not None


async def test_run_cycle_no_ops_when_disabled(settings_factory, db):
    settings = settings_factory(
        MIN_SCORE=1,
        CRYPTOPANIC_ENABLED=False,
    )

    import aiohttp

    mock_fetch = AsyncMock(return_value=[])

    async with aiohttp.ClientSession() as session:
        with (
            patch("scout.main.fetch_trending", new=AsyncMock(return_value=[_btc_candidate()])),
            patch("scout.main.fetch_trending_pools", new=AsyncMock(return_value=[])),
            patch("scout.main.cg_fetch_top_movers", new=AsyncMock(return_value=[])),
            patch("scout.main.cg_fetch_trending", new=AsyncMock(return_value=[])),
            patch("scout.main.cg_fetch_by_volume", new=AsyncMock(return_value=[])),
            patch("scout.main.enrich_holders", new=AsyncMock(side_effect=lambda tok, s, st: tok)),
            patch("scout.main.fetch_cryptopanic_posts", new=mock_fetch),
            patch("scout.main.evaluate", new=AsyncMock(return_value=(False, 0.0, _btc_candidate()))),
        ):
            await run_cycle(settings, db, session, dry_run=True)

    # Fetch should NOT have been called when flag is False
    mock_fetch.assert_not_called()
    rows = await db.fetch_all_cryptopanic_posts()
    assert rows == []
```

- [ ] **Step 8.2: Verify test fails**

Run: `uv run pytest tests/test_main_cryptopanic_integration.py -v`
Expected: FAIL — `AttributeError: module 'scout.main' has no attribute 'fetch_cryptopanic_posts'`.

- [ ] **Step 8.3: Add import to main.py**

In `scout/main.py`, near the other `from scout.ingestion...` imports (around line 34-36), add:

```python
from scout.news.cryptopanic import (
    enrich_candidates_with_news,
    fetch_cryptopanic_posts,
)
```

- [ ] **Step 8.4: Wire fetch + enrich + persist**

In `scout/main.py` `run_cycle`, locate the block:

```python
    # Stage 2: Aggregate
    all_candidates = aggregate(
        list(dex_tokens)
        + list(gecko_tokens)
        + list(cg_movers)
        + list(cg_trending)
        + list(cg_by_volume)
    )
    stats["tokens_scanned"] = len(all_candidates)

    # Enrich holders (concurrently)
    enriched = list(
        await asyncio.gather(
            *[enrich_holders(token, session, settings) for token in all_candidates]
        )
    )
```

Immediately after the `all_candidates = aggregate(...)` line and BEFORE the holder-enrichment gather, add:

```python

    # Kick off CryptoPanic fetch concurrently with enrichment (if enabled).
    # Never raises — short-circuits to [] on any failure.
    cryptopanic_task = None
    if settings.CRYPTOPANIC_ENABLED:
        cryptopanic_task = asyncio.create_task(
            fetch_cryptopanic_posts(session, settings)
        )
```

Then, AFTER the existing vol_7d enrichment loop (the `for i, token in enumerate(enriched):` block that logs `vol_7d_avg`) and BEFORE `# Stage 3: Score`, add:

```python

    # Await CryptoPanic fetch (launched before enrichment) with a 10s cap
    # so a stalled third-party call cannot extend the cycle indefinitely.
    if cryptopanic_task is not None:
        try:
            cp_posts = await asyncio.wait_for(cryptopanic_task, timeout=10.0)
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning("cryptopanic_fetch_failed", error=str(e))
            cp_posts = []
        if cp_posts:
            # Persist posts (idempotent INSERT OR IGNORE)
            from scout.news.schemas import classify_macro, classify_sentiment

            for post in cp_posts:
                try:
                    sentiment = classify_sentiment(
                        post.votes_positive, post.votes_negative
                    )
                    is_macro = classify_macro(
                        post.currencies,
                        threshold=settings.CRYPTOPANIC_MACRO_MIN_CURRENCIES,
                    )
                    await db.insert_cryptopanic_post(
                        post, is_macro=is_macro, sentiment=sentiment
                    )
                except Exception:
                    logger.exception("cryptopanic_persist_error", post_id=post.post_id)
            # Tag candidates
            enriched = enrich_candidates_with_news(enriched, cp_posts, settings)
```

- [ ] **Step 8.5: Verify integration tests pass**

Run: `uv run pytest tests/test_main_cryptopanic_integration.py -v`
Expected: PASS (2 tests).

- [ ] **Step 8.6: Verify no regression across the suite**

Run: `uv run pytest --tb=short -q 2>&1 | tail -10`
Expected: all tests pass (baseline count + ~40 new BL-053 tests).

- [ ] **Step 8.7: Commit**

```bash
git add scout/main.py tests/test_main_cryptopanic_integration.py
git commit -m "feat(bl-053): wire CryptoPanic fetch/enrich/persist into run_cycle"
```

---

## Task 9: Hourly prune wiring

The hourly maintenance block already handles other prunes. Add `prune_cryptopanic_posts` wrapped in try/except so silent failure at least logs.

**Files:**
- Modify: `scout/main.py` (extend the `hourly tasks` block in `_pipeline_loop`)

- [ ] **Step 9.1: Locate the hourly block**

Open `scout/main.py` and find the `# Hourly tasks: outcome check + DB prune` section inside `_pipeline_loop` (around line 830).

- [ ] **Step 9.2: Add prune call**

After the existing `db.prune_old_candidates` try/except block, inside the same `if now - last_outcome_check >= outcome_check_interval:` guard, append:

```python

                        # BL-053: prune CryptoPanic posts older than retention cap
                        if settings.CRYPTOPANIC_ENABLED:
                            try:
                                pruned_cp = await db.prune_cryptopanic_posts(
                                    keep_days=settings.CRYPTOPANIC_RETENTION_DAYS
                                )
                                if pruned_cp:
                                    logger.info(
                                        "cryptopanic_pruned",
                                        rows_deleted=pruned_cp,
                                    )
                            except Exception:
                                logger.exception("cryptopanic_prune_failed")
```

- [ ] **Step 9.3: Verify full suite still passes**

Run: `uv run pytest --tb=short -q 2>&1 | tail -5`
Expected: all pass.

- [ ] **Step 9.4: Commit**

```bash
git add scout/main.py
git commit -m "feat(bl-053): prune cryptopanic_posts in hourly maintenance block"
```

---

## Task 10: Dry-run smoke + final review

Make sure the full pipeline boots and exits cleanly with the feature OFF (default production behaviour) and ON.

**Files:** no file changes — verification + black formatter only.

- [ ] **Step 10.1: Default-off smoke test**

Run: `uv run python -m scout.main --dry-run --cycles 1 2>&1 | tee /tmp/bl053_offcheck.log | tail -20`
Acceptance:
- Process exits 0 within ~30 seconds.
- `/tmp/bl053_offcheck.log` must NOT contain `cryptopanic_fetch_started` or `cryptopanic_tokens_tagged` events.
- If either appears, something is firing with the default flag off — FAIL, investigate.

- [ ] **Step 10.2: Flag-on smoke test (skipped if no token available)**

If a real `.env` with `CRYPTOPANIC_API_TOKEN=<yours>` and `CRYPTOPANIC_ENABLED=true` is accessible, run:

```bash
CRYPTOPANIC_ENABLED=true uv run python -m scout.main --dry-run --cycles 1 2>&1 | tee /tmp/bl053_oncheck.log | tail -40
```

Acceptance (any one of):
- `cryptopanic_fetch_completed count=>=1` → ✅ end-to-end verified.
- `cryptopanic_auth_missing` → ✅ token not set, graceful skip.
- `cryptopanic_fetch_failed` → ⚠ external service; note in PR, do not block.

If no real `.env` is available, skip this step and document in the PR body.

- [ ] **Step 10.3: Format**

Run: `uv run black scout/ tests/`
Expected: files formatted in place. If anything under `tests/` that we didn't touch changes, stash-pop to avoid scope creep.

- [ ] **Step 10.4: Re-run full suite after format**

Run: `uv run pytest --tb=short -q 2>&1 | tail -5`
Expected: all pass.

- [ ] **Step 10.5: Commit format (if any changes)**

```bash
git add -A
git diff --cached --stat
git commit -m "style(bl-053): apply black formatting" || echo "nothing to format"
```

- [ ] **Step 10.6: Push branch**

```bash
git push -u origin feat/bl-053-cryptopanic-news-feed
```

---

## Post-implementation (controller task, not implementer)

- Dispatch parallel spec-compliance + code-quality reviewers for the full branch diff.
- Address any blocking findings (implementer re-dispatched with specific fix-list).
- Create PR targeting master. Template must link:
  - Spec: `docs/superpowers/specs/2026-04-20-bl053-cryptopanic-news-feed-design.md`
  - Plan: `docs/superpowers/plans/2026-04-20-bl053-cryptopanic-news-feed-plan.md`
- Dispatch parallel PR reviewers.
- **Do NOT merge. Do NOT deploy.**

---

## Files summary

**New files (8):**
- `scout/news/__init__.py`
- `scout/news/schemas.py`
- `scout/news/cryptopanic.py`
- `tests/test_config_cryptopanic.py`
- `tests/test_models_cryptopanic_fields.py`
- `tests/test_cryptopanic_schemas.py`
- `tests/test_cryptopanic_fetch.py`
- `tests/test_cryptopanic_db.py`
- `tests/test_cryptopanic_enrichment.py`
- `tests/test_scorer_cryptopanic_gated.py`
- `tests/test_main_cryptopanic_integration.py`

**Modified files (5):**
- `scout/config.py`
- `.env.example`
- `scout/models.py`
- `scout/db.py`
- `scout/scorer.py`
- `scout/main.py`

**Expected test delta:** +~40 tests (~836 → ~876 passing).
