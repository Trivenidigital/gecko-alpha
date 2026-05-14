"""Tests for DexScreener ingestion."""

import pytest
import aiohttp
from aioresponses import aioresponses

from scout.ingestion.dexscreener import fetch_trending, get_last_watchdog_samples


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
    samples = {sample.source: sample for sample in get_last_watchdog_samples()}
    assert samples["dexscreener:boosts"].raw_count == 1
    assert samples["dexscreener:tokens"].raw_count == 1
    assert samples["dexscreener:tokens"].usable_count == 0


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
    sample = get_last_watchdog_samples()[-1]
    assert sample.source == "dexscreener:boosts"
    assert sample.raw_count == 0
    assert sample.error == "no_boosts_or_fetch_failed"


async def test_fetch_trending_token_detail_outage_samples_token_starvation(
    mock_aiohttp, settings_factory
):
    mock_aiohttp.get(
        DEXSCREENER_TRENDING_URL,
        payload=[
            {"tokenAddress": "0xabc", "chainId": "solana"},
            {"tokenAddress": "0xdef", "chainId": "solana"},
        ],
    )
    mock_aiohttp.get("https://api.dexscreener.com/tokens/v1/solana/0xabc", status=500)
    mock_aiohttp.get("https://api.dexscreener.com/tokens/v1/solana/0xabc", status=500)
    mock_aiohttp.get("https://api.dexscreener.com/tokens/v1/solana/0xabc", status=500)
    mock_aiohttp.get("https://api.dexscreener.com/tokens/v1/solana/0xdef", status=500)
    mock_aiohttp.get("https://api.dexscreener.com/tokens/v1/solana/0xdef", status=500)
    mock_aiohttp.get("https://api.dexscreener.com/tokens/v1/solana/0xdef", status=500)

    settings = settings_factory(
        MIN_MARKET_CAP=10000,
        MAX_MARKET_CAP=500000,
        MAX_TOKEN_AGE_DAYS=7,
    )
    async with aiohttp.ClientSession() as session:
        tokens = await fetch_trending(session, settings)

    assert tokens == []
    samples = {sample.source: sample for sample in get_last_watchdog_samples()}
    assert samples["dexscreener:boosts"].raw_count == 2
    assert samples["dexscreener:tokens"].raw_count == 0
    assert samples["dexscreener:tokens"].error == "no_detail_payloads"


async def test_fetch_trending_handles_429_with_backoff(
    mock_aiohttp, settings_factory, patch_module_sleep
):
    patch_module_sleep("scout.ingestion.dexscreener")
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
