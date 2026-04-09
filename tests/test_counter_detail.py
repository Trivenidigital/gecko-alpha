"""Tests for scout.counter.detail — CoinGecko detail fetcher + cache."""

import re
from datetime import datetime, timezone, timedelta

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.counter.detail import (
    CACHE_TTL_SECONDS,
    CG_DETAIL_URL,
    _detail_cache,
    extract_counter_data,
    fetch_coin_detail,
)

SAMPLE_DETAIL = {
    "id": "bitcoin",
    "sentiment_votes_up_percentage": 72.5,
    "developer_data": {"commit_count_4_weeks": 120},
    "community_data": {
        "reddit_subscribers": 5_000_000,
        "telegram_channel_user_count": 80_000,
    },
    "market_data": {
        "price_change_percentage_7d": 3.5,
        "price_change_percentage_30d": -8.2,
    },
}

URL_PATTERN = re.compile(r"https://api\.coingecko\.com/api/v3/coins/bitcoin")


# ---- extract_counter_data tests ----


def test_extract_counter_data_full():
    result = extract_counter_data(SAMPLE_DETAIL)
    assert result["commits_4w"] == 120
    assert result["reddit_subscribers"] == 5_000_000
    assert result["telegram_users"] == 80_000
    assert result["sentiment_up_pct"] == 72.5
    assert result["price_change_7d"] == 3.5
    assert result["price_change_30d"] == -8.2


def test_extract_counter_data_missing_fields():
    result = extract_counter_data({})
    assert result["commits_4w"] == 0
    assert result["reddit_subscribers"] == 0
    assert result["telegram_users"] == 0
    assert result["sentiment_up_pct"] == 50.0
    assert result["price_change_7d"] == 0
    assert result["price_change_30d"] == 0


# ---- fetch_coin_detail tests ----


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear module-level cache before each test."""
    _detail_cache.clear()
    yield
    _detail_cache.clear()


async def test_fetch_coin_detail_success():
    with aioresponses() as m:
        m.get(URL_PATTERN, payload=SAMPLE_DETAIL)
        async with aiohttp.ClientSession() as session:
            result = await fetch_coin_detail(session, "bitcoin")

    assert result is not None
    assert result["id"] == "bitcoin"
    assert "bitcoin" in _detail_cache


async def test_fetch_coin_detail_cache_hit():
    # Pre-populate cache with fresh entry
    now = datetime.now(timezone.utc)
    _detail_cache["bitcoin"] = (now, SAMPLE_DETAIL)

    with aioresponses() as m:
        # No mock registered — any HTTP call would raise
        async with aiohttp.ClientSession() as session:
            result = await fetch_coin_detail(session, "bitcoin")

    assert result is SAMPLE_DETAIL


async def test_fetch_coin_detail_cache_expired():
    # Pre-populate with an expired entry
    old_time = datetime.now(timezone.utc) - timedelta(seconds=CACHE_TTL_SECONDS + 60)
    _detail_cache["bitcoin"] = (old_time, {"stale": True})

    with aioresponses() as m:
        m.get(URL_PATTERN, payload=SAMPLE_DETAIL)
        async with aiohttp.ClientSession() as session:
            result = await fetch_coin_detail(session, "bitcoin")

    assert result is not None
    assert result["id"] == "bitcoin"
    # Cache should be updated
    _, cached = _detail_cache["bitcoin"]
    assert cached["id"] == "bitcoin"


async def test_fetch_coin_detail_404_returns_none():
    with aioresponses() as m:
        m.get(URL_PATTERN, status=404)
        async with aiohttp.ClientSession() as session:
            result = await fetch_coin_detail(session, "bitcoin")

    assert result is None


async def test_fetch_coin_detail_429_returns_none():
    with aioresponses() as m:
        m.get(URL_PATTERN, status=429)
        async with aiohttp.ClientSession() as session:
            result = await fetch_coin_detail(session, "bitcoin")

    assert result is None
