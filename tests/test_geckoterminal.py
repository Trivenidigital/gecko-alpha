"""Tests for GeckoTerminal ingestion."""

import pytest
import aiohttp
from aioresponses import aioresponses

from scout.ingestion.geckoterminal import fetch_trending_pools


@pytest.fixture
def mock_aiohttp():
    with aioresponses() as m:
        yield m


GECKO_BASE = "https://api.geckoterminal.com/api/v2"

SAMPLE_POOL = {
    "id": "solana_0xpool1",
    "attributes": {
        "name": "MemeToken / SOL",
        "base_token_price_usd": "0.01",
        "fdv_usd": "75000",
        "reserve_in_usd": "15000",
        "volume_usd": {"h24": "60000"},
        "pool_created_at": "2026-03-17T10:00:00Z",
    },
    "relationships": {
        "base_token": {"data": {"id": "solana_0xmemeaddr"}},
    },
}


async def test_fetch_trending_pools_returns_candidates(mock_aiohttp, settings_factory):
    url = f"{GECKO_BASE}/networks/solana/trending_pools"
    mock_aiohttp.get(url, payload={"data": [SAMPLE_POOL]})

    settings = settings_factory(
        CHAINS=["solana"], MIN_MARKET_CAP=10000, MAX_MARKET_CAP=500000
    )
    async with aiohttp.ClientSession() as session:
        tokens = await fetch_trending_pools(session, settings)

    assert len(tokens) == 1
    assert tokens[0].contract_address == "0xmemeaddr"
    assert tokens[0].chain == "solana"
    assert tokens[0].market_cap_usd == 75000


async def test_fetch_trending_pools_multiple_chains(mock_aiohttp, settings_factory):
    settings = settings_factory(
        CHAINS=["solana", "eth"], MIN_MARKET_CAP=10000, MAX_MARKET_CAP=500000
    )

    sol_url = f"{GECKO_BASE}/networks/solana/trending_pools"
    eth_url = f"{GECKO_BASE}/networks/eth/trending_pools"

    mock_aiohttp.get(sol_url, payload={"data": [SAMPLE_POOL]})
    mock_aiohttp.get(eth_url, payload={"data": []})

    async with aiohttp.ClientSession() as session:
        tokens = await fetch_trending_pools(session, settings)

    assert len(tokens) == 1


async def test_fetch_trending_pools_filters_market_cap(mock_aiohttp, settings_factory):
    big_pool = {
        **SAMPLE_POOL,
        "attributes": {**SAMPLE_POOL["attributes"], "fdv_usd": "1000000"},
    }
    url = f"{GECKO_BASE}/networks/solana/trending_pools"
    mock_aiohttp.get(url, payload={"data": [big_pool]})

    settings = settings_factory(
        CHAINS=["solana"], MIN_MARKET_CAP=10000, MAX_MARKET_CAP=500000
    )
    async with aiohttp.ClientSession() as session:
        tokens = await fetch_trending_pools(session, settings)

    assert len(tokens) == 0


async def test_fetch_trending_pools_handles_api_error(mock_aiohttp, settings_factory):
    url = f"{GECKO_BASE}/networks/solana/trending_pools"
    mock_aiohttp.get(url, status=500)

    settings = settings_factory(
        CHAINS=["solana"], MIN_MARKET_CAP=10000, MAX_MARKET_CAP=500000
    )
    async with aiohttp.ClientSession() as session:
        tokens = await fetch_trending_pools(session, settings)

    assert tokens == []
