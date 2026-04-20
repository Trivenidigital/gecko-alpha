"""Tests for DexScreener ingestion."""

import pytest
import aiohttp
from aioresponses import aioresponses
from unittest.mock import AsyncMock

from scout.ingestion.dexscreener import fetch_trending
from scout.ingestion.dexscreener import (
    fetch_top_boosts,
    TOP_BOOSTS_URL,
    BoostInfo,
)
from scout.ingestion import dexscreener as _dex_module


@pytest.fixture
def mock_aiohttp():
    with aioresponses() as m:
        yield m


DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
DEXSCREENER_TRENDING_URL = "https://api.dexscreener.com/token-boosts/latest/v1"

SAMPLE_PAIR = {
    "baseToken": {"address": "0xabc", "name": "TestCoin", "symbol": "TC"},
    "chainId": "solana",
    "pairCreatedAt": 1710720000000,
    "fdv": 50000,
    "liquidity": {"usd": 10000},
    "volume": {"h24": 80000},
}


async def test_fetch_trending_returns_candidates(mock_aiohttp, settings_factory):
    mock_aiohttp.get(
        DEXSCREENER_TRENDING_URL,
        payload=[
            {"tokenAddress": "0xabc", "chainId": "solana"},
        ],
    )
    mock_aiohttp.get(
        "https://api.dexscreener.com/tokens/v1/solana/0xabc",
        payload=[SAMPLE_PAIR],
    )

    settings = settings_factory(
        MIN_MARKET_CAP=10000,
        MAX_MARKET_CAP=500000,
        MAX_TOKEN_AGE_DAYS=7,
    )
    async with aiohttp.ClientSession() as session:
        tokens = await fetch_trending(session, settings)

    assert len(tokens) >= 1
    assert tokens[0].contract_address == "0xabc"
    assert tokens[0].chain == "solana"


async def test_fetch_trending_filters_by_market_cap(mock_aiohttp, settings_factory):
    too_big = {**SAMPLE_PAIR, "fdv": 1_000_000}
    mock_aiohttp.get(
        DEXSCREENER_TRENDING_URL,
        payload=[
            {"tokenAddress": "0xbig", "chainId": "solana"},
        ],
    )
    mock_aiohttp.get(
        "https://api.dexscreener.com/tokens/v1/solana/0xbig",
        payload=[
            {
                **too_big,
                "baseToken": {"address": "0xbig", "name": "Big", "symbol": "BIG"},
            }
        ],
    )

    settings = settings_factory(
        MIN_MARKET_CAP=10000,
        MAX_MARKET_CAP=500000,
        MAX_TOKEN_AGE_DAYS=7,
    )
    async with aiohttp.ClientSession() as session:
        tokens = await fetch_trending(session, settings)

    assert len(tokens) == 0


async def test_fetch_trending_handles_empty_response(mock_aiohttp, settings_factory):
    mock_aiohttp.get(DEXSCREENER_TRENDING_URL, payload=[])

    settings = settings_factory(
        MIN_MARKET_CAP=10000,
        MAX_MARKET_CAP=500000,
        MAX_TOKEN_AGE_DAYS=7,
    )
    async with aiohttp.ClientSession() as session:
        tokens = await fetch_trending(session, settings)

    assert tokens == []


async def test_fetch_trending_handles_429_with_backoff(mock_aiohttp, settings_factory):
    mock_aiohttp.get(DEXSCREENER_TRENDING_URL, status=429)
    mock_aiohttp.get(
        DEXSCREENER_TRENDING_URL,
        payload=[
            {"tokenAddress": "0xretry", "chainId": "solana"},
        ],
    )
    mock_aiohttp.get(
        "https://api.dexscreener.com/tokens/v1/solana/0xretry",
        payload=[
            {
                **SAMPLE_PAIR,
                "baseToken": {"address": "0xretry", "name": "Retry", "symbol": "RTR"},
            }
        ],
    )

    settings = settings_factory(
        MIN_MARKET_CAP=10000,
        MAX_MARKET_CAP=500000,
        MAX_TOKEN_AGE_DAYS=7,
    )
    async with aiohttp.ClientSession() as session:
        tokens = await fetch_trending(session, settings)

    assert len(tokens) >= 1


# ---------------------------------------------------------------------------
# fetch_top_boosts tests (BL-051 Task 4)
# ---------------------------------------------------------------------------

SAMPLE_TOP_BOOSTS_PAYLOAD = [
    {"chainId": "solana", "tokenAddress": "ADDR1", "totalAmount": 1500.0},
    {"chainId": "base", "tokenAddress": "0xABCDEF", "totalAmount": 800.0},
]


async def test_fetch_top_boosts_happy_path(mock_aiohttp, settings_factory, monkeypatch):
    monkeypatch.setattr(_dex_module.asyncio, "sleep", AsyncMock())
    mock_aiohttp.get(TOP_BOOSTS_URL, payload=SAMPLE_TOP_BOOSTS_PAYLOAD)

    settings = settings_factory()
    async with aiohttp.ClientSession() as session:
        result = await fetch_top_boosts(session, settings)

    assert len(result) == 2
    assert result[0] == BoostInfo(chain="solana", address="ADDR1", total_amount=1500.0)
    assert result[1] == BoostInfo(chain="base", address="0xABCDEF", total_amount=800.0)


async def test_fetch_top_boosts_empty_response(mock_aiohttp, settings_factory, monkeypatch):
    monkeypatch.setattr(_dex_module.asyncio, "sleep", AsyncMock())
    mock_aiohttp.get(TOP_BOOSTS_URL, payload=[])

    settings = settings_factory()
    async with aiohttp.ClientSession() as session:
        result = await fetch_top_boosts(session, settings)

    assert result == []


async def test_fetch_top_boosts_skips_missing_total_amount(mock_aiohttp, settings_factory, monkeypatch):
    monkeypatch.setattr(_dex_module.asyncio, "sleep", AsyncMock())
    payload = [
        {"chainId": "solana", "tokenAddress": "ADDR1"},  # no totalAmount
        {"chainId": "base", "tokenAddress": "0xABC", "totalAmount": 500.0},
    ]
    mock_aiohttp.get(TOP_BOOSTS_URL, payload=payload)

    settings = settings_factory()
    async with aiohttp.ClientSession() as session:
        result = await fetch_top_boosts(session, settings)

    assert len(result) == 1
    assert result[0].address == "0xABC"


async def test_fetch_top_boosts_skips_missing_chain_or_address(mock_aiohttp, settings_factory, monkeypatch):
    monkeypatch.setattr(_dex_module.asyncio, "sleep", AsyncMock())
    payload = [
        {"tokenAddress": "ADDR1", "totalAmount": 1000.0},          # no chainId
        {"chainId": "solana", "totalAmount": 1000.0},               # no tokenAddress
        {"chainId": "base", "tokenAddress": "0xGOOD", "totalAmount": 300.0},
    ]
    mock_aiohttp.get(TOP_BOOSTS_URL, payload=payload)

    settings = settings_factory()
    async with aiohttp.ClientSession() as session:
        result = await fetch_top_boosts(session, settings)

    assert len(result) == 1
    assert result[0].address == "0xGOOD"


async def test_fetch_top_boosts_upstream_error_returns_empty(mock_aiohttp, settings_factory, monkeypatch):
    monkeypatch.setattr(_dex_module.asyncio, "sleep", AsyncMock())
    mock_aiohttp.get(TOP_BOOSTS_URL, status=500)
    mock_aiohttp.get(TOP_BOOSTS_URL, status=500)
    mock_aiohttp.get(TOP_BOOSTS_URL, status=500)

    settings = settings_factory()
    async with aiohttp.ClientSession() as session:
        result = await fetch_top_boosts(session, settings)

    assert result == []


async def test_fetch_top_boosts_populates_module_cache(mock_aiohttp, settings_factory, monkeypatch):
    monkeypatch.setattr(_dex_module.asyncio, "sleep", AsyncMock())
    _dex_module.last_raw_top_boosts.clear()
    mock_aiohttp.get(TOP_BOOSTS_URL, payload=SAMPLE_TOP_BOOSTS_PAYLOAD)

    settings = settings_factory()
    async with aiohttp.ClientSession() as session:
        await fetch_top_boosts(session, settings)

    assert len(_dex_module.last_raw_top_boosts) == 2
    assert _dex_module.last_raw_top_boosts[0]["tokenAddress"] == "ADDR1"


async def test_fetch_top_boosts_cache_preserved_on_failure(mock_aiohttp, settings_factory, monkeypatch):
    monkeypatch.setattr(_dex_module.asyncio, "sleep", AsyncMock())
    # Pre-populate cache with stale data
    stale = [{"chainId": "solana", "tokenAddress": "STALE", "totalAmount": 999.0}]
    _dex_module.last_raw_top_boosts.clear()
    _dex_module.last_raw_top_boosts.extend(stale)

    mock_aiohttp.get(TOP_BOOSTS_URL, status=500)
    mock_aiohttp.get(TOP_BOOSTS_URL, status=500)
    mock_aiohttp.get(TOP_BOOSTS_URL, status=500)

    settings = settings_factory()
    async with aiohttp.ClientSession() as session:
        result = await fetch_top_boosts(session, settings)

    assert result == []
    # Cache must not be cleared on failure
    assert len(_dex_module.last_raw_top_boosts) == 1
    assert _dex_module.last_raw_top_boosts[0]["tokenAddress"] == "STALE"
