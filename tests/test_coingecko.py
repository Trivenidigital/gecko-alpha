"""Tests for CoinGecko ingestion module."""

import re

import pytest
import aiohttp
from aioresponses import aioresponses

from scout.config import Settings
from scout.ingestion.coingecko import fetch_top_movers, fetch_trending
from scout.ratelimit import coingecko_limiter

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
        {
            "item": {
                "id": f"coin-{i}",
                "symbol": f"c{i}",
                "name": f"Coin{i}",
                "market_cap_rank": 100 + i,
                "score": i,
            }
        }
        for i in range(15)
    ]
}

CG_BASE = "https://api.coingecko.com/api/v3"
MARKETS_PATTERN = re.compile(r"https://api\.coingecko\.com/api/v3/coins/markets")
TRENDING_PATTERN = re.compile(r"https://api\.coingecko\.com/api/v3/search/trending")


@pytest.fixture(autouse=True)
async def _clear_rate_limit():
    """Clear shared rate limiter state between tests."""
    await coingecko_limiter.reset()
    yield
    await coingecko_limiter.reset()


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
        mocked.get(MARKETS_PATTERN, payload=COINS_MARKETS_RESPONSE)
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
        mocked.get(TRENDING_PATTERN, payload=TRENDING_RESPONSE)
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_trending(session, settings)

    assert len(tokens) > 0
    assert tokens[0].cg_trending_rank == 1  # 1-indexed
    assert tokens[1].cg_trending_rank == 2


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
        mocked.get(MARKETS_PATTERN, status=429)
        mocked.get(MARKETS_PATTERN, payload=COINS_MARKETS_RESPONSE)
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
        mocked.get(MARKETS_PATTERN, payload=COINS_MARKETS_RESPONSE)
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
        # Non-429 errors return None immediately on first attempt
        mocked.get(MARKETS_PATTERN, status=500)
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_top_movers(session, settings)

    assert tokens == []


@pytest.mark.asyncio
async def test_fetch_trending_outage_returns_empty():
    """NFR: fetch_trending with 500 returns empty list, does not raise."""
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_CHAT_ID="test",
        ANTHROPIC_API_KEY="test",
    )
    with aioresponses() as mocked:
        mocked.get(TRENDING_PATTERN, status=500)
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_trending(session, settings)

    assert tokens == []
