"""Tests for GeckoTerminal ingestion."""

import pytest
import aiohttp
from aioresponses import aioresponses
from structlog.testing import capture_logs
from yarl import URL

import scout.ingestion.geckoterminal as geckoterminal
from scout.ingestion.geckoterminal import fetch_trending_pools, get_last_watchdog_samples


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
    samples = {sample.source: sample for sample in get_last_watchdog_samples()}
    assert samples["geckoterminal:solana"].raw_count == 1
    assert samples["geckoterminal:solana"].usable_count == 1
    assert samples["geckoterminal:eth"].raw_count == 0
    assert samples["geckoterminal:eth"].usable_count == 0


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


async def test_fetch_trending_pools_exhausts_5xx_retries(
    mock_aiohttp, settings_factory, geckoterminal_sleep_spy
):
    url = f"{GECKO_BASE}/networks/solana/trending_pools"
    mock_aiohttp.get(url, status=500)
    mock_aiohttp.get(url, status=500)
    mock_aiohttp.get(url, status=500)

    settings = settings_factory(
        CHAINS=["solana"], MIN_MARKET_CAP=10000, MAX_MARKET_CAP=500000
    )
    with capture_logs() as logs:
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_trending_pools(session, settings)

    assert tokens == []
    samples = get_last_watchdog_samples()
    assert samples[-1].source == "geckoterminal:solana"
    assert samples[-1].raw_count == 0
    assert samples[-1].error == "http_500"
    assert _request_count(mock_aiohttp, url) == 3
    assert geckoterminal_sleep_spy == [1, 2]
    assert logs[-1] == {
        "chain": "solana",
        "url": url,
        "status": 500,
        "max_attempts": 3,
        "event": "geckoterminal_retries_exhausted",
        "log_level": "warning",
    }


def _request_count(mock_aiohttp, url: str) -> int:
    return len(mock_aiohttp.requests.get(("GET", URL(url)), []))


@pytest.fixture
def geckoterminal_sleep_spy(monkeypatch):
    sleeps: list[float] = []

    async def _sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(geckoterminal.asyncio, "sleep", _sleep)
    return sleeps


async def test_fetch_trending_pools_handles_429_with_backoff(
    mock_aiohttp, settings_factory, geckoterminal_sleep_spy
):
    url = f"{GECKO_BASE}/networks/solana/trending_pools"
    mock_aiohttp.get(url, status=429)
    mock_aiohttp.get(url, payload={"data": [SAMPLE_POOL]})

    settings = settings_factory(
        CHAINS=["solana"], MIN_MARKET_CAP=10000, MAX_MARKET_CAP=500000
    )
    with capture_logs() as logs:
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_trending_pools(session, settings)

    assert len(tokens) == 1
    assert _request_count(mock_aiohttp, url) == 2
    assert geckoterminal_sleep_spy == [1]
    assert logs == [
        {
            "chain": "solana",
            "url": url,
            "status": 429,
            "wait": 1,
            "attempt": 1,
            "max_attempts": 3,
            "event": "geckoterminal_retrying",
            "log_level": "warning",
        }
    ]


async def test_fetch_trending_pools_handles_5xx_with_backoff(
    mock_aiohttp, settings_factory, geckoterminal_sleep_spy
):
    url = f"{GECKO_BASE}/networks/solana/trending_pools"
    mock_aiohttp.get(url, status=503)
    mock_aiohttp.get(url, payload={"data": [SAMPLE_POOL]})

    settings = settings_factory(
        CHAINS=["solana"], MIN_MARKET_CAP=10000, MAX_MARKET_CAP=500000
    )
    with capture_logs() as logs:
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_trending_pools(session, settings)

    assert len(tokens) == 1
    assert _request_count(mock_aiohttp, url) == 2
    assert geckoterminal_sleep_spy == [1]
    assert logs[0]["event"] == "geckoterminal_retrying"
    assert logs[0]["status"] == 503
    assert logs[0]["wait"] == 1


async def test_fetch_trending_pools_exhausts_429_retries(
    mock_aiohttp, settings_factory, geckoterminal_sleep_spy
):
    url = f"{GECKO_BASE}/networks/solana/trending_pools"
    mock_aiohttp.get(url, status=429)
    mock_aiohttp.get(url, status=429)
    mock_aiohttp.get(url, status=429)

    settings = settings_factory(
        CHAINS=["solana"], MIN_MARKET_CAP=10000, MAX_MARKET_CAP=500000
    )
    with capture_logs() as logs:
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_trending_pools(session, settings)

    assert tokens == []
    assert _request_count(mock_aiohttp, url) == 3
    assert geckoterminal_sleep_spy == [1, 2]
    assert [log["event"] for log in logs] == [
        "geckoterminal_retrying",
        "geckoterminal_retrying",
        "geckoterminal_retries_exhausted",
    ]
    assert [log.get("wait") for log in logs[:2]] == [1, 2]
    assert logs[-1] == {
        "chain": "solana",
        "url": url,
        "status": 429,
        "max_attempts": 3,
        "event": "geckoterminal_retries_exhausted",
        "log_level": "warning",
    }


async def test_fetch_trending_pools_continues_after_chain_retry_exhaustion(
    mock_aiohttp, settings_factory, geckoterminal_sleep_spy
):
    sol_url = f"{GECKO_BASE}/networks/solana/trending_pools"
    base_url = f"{GECKO_BASE}/networks/base/trending_pools"
    mock_aiohttp.get(sol_url, status=429)
    mock_aiohttp.get(sol_url, status=429)
    mock_aiohttp.get(sol_url, status=429)
    mock_aiohttp.get(base_url, payload={"data": [SAMPLE_POOL]})

    settings = settings_factory(
        CHAINS=["solana", "base"], MIN_MARKET_CAP=10000, MAX_MARKET_CAP=500000
    )
    with capture_logs() as logs:
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_trending_pools(session, settings)

    assert len(tokens) == 1
    assert tokens[0].chain == "base"
    assert _request_count(mock_aiohttp, sol_url) == 3
    assert _request_count(mock_aiohttp, base_url) == 1
    assert geckoterminal_sleep_spy == [1, 2]
    assert "geckoterminal_retries_exhausted" in [log["event"] for log in logs]


async def test_fetch_trending_pools_does_not_retry_404(
    mock_aiohttp, settings_factory, geckoterminal_sleep_spy
):
    url = f"{GECKO_BASE}/networks/ethereum/trending_pools"
    mock_aiohttp.get(url, status=404)

    settings = settings_factory(
        CHAINS=["ethereum"], MIN_MARKET_CAP=10000, MAX_MARKET_CAP=500000
    )
    with capture_logs() as logs:
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_trending_pools(session, settings)

    assert tokens == []
    assert _request_count(mock_aiohttp, url) == 1
    assert geckoterminal_sleep_spy == []
    assert logs == [
        {
            "chain": "ethereum",
            "url": url,
            "status": 404,
            "event": "geckoterminal_non_retryable_status",
            "log_level": "warning",
        }
    ]


async def test_fetch_trending_pools_transport_error_does_not_retry(
    mock_aiohttp, settings_factory, geckoterminal_sleep_spy
):
    url = f"{GECKO_BASE}/networks/solana/trending_pools"
    mock_aiohttp.get(url, exception=aiohttp.ClientError("connection reset"))

    settings = settings_factory(
        CHAINS=["solana"], MIN_MARKET_CAP=10000, MAX_MARKET_CAP=500000
    )
    with capture_logs() as logs:
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_trending_pools(session, settings)

    assert tokens == []
    assert _request_count(mock_aiohttp, url) == 1
    assert geckoterminal_sleep_spy == []
    assert logs == [
        {
            "chain": "solana",
            "url": url,
            "error": "connection reset",
            "error_type": "ClientError",
            "event": "geckoterminal_request_error",
            "log_level": "warning",
        }
    ]
