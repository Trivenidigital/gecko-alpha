"""Tests for the GeckoTerminal CA->pool + pool-OHLCV client (C0, design #392).

C0 scope: deterministic, source-tagged CA->pool resolution and pool OHLCV
fetch, with an OBSERVABLE provider-error path (PriceProviderError, never a
fake price) that is distinct from the normal "missing pool / no data" empty
return. No source mixing, no changes to source_calls performance fields.
"""

from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.exceptions import PriceProviderError
from scout.ingestion.gt_ohlcv import (
    PRICE_SOURCE,
    OhlcvCandle,
    PoolRef,
    fetch_pool_ohlcv,
    resolve_pool_address,
)

GECKO_BASE = "https://api.geckoterminal.com/api/v2"
CA = "5UUH9RTDiSpq6HKS6bp4NdU9PNJpXRXuiw6ShBTBhgH2"


@pytest.fixture
def mock_aiohttp():
    with aioresponses() as m:
        yield m


def _pools_url(network: str, ca: str) -> str:
    return f"{GECKO_BASE}/networks/{network}/tokens/{ca}/pools"


def _ohlcv_url(network: str, pool: str, timeframe: str = "minute") -> str:
    return (
        f"{GECKO_BASE}/networks/{network}/pools/{pool}/ohlcv/{timeframe}"
        f"?aggregate=1&limit=100"
    )


def _pool(addr: str, reserve: str, base="solana_BASE") -> dict:
    return {
        "id": f"solana_{addr}",
        "type": "pool",
        "attributes": {"address": addr, "reserve_in_usd": reserve, "name": "X / SOL"},
        "relationships": {"base_token": {"data": {"id": base}}},
    }


# --------------------------------------------------------------------------
# resolve_pool_address
# --------------------------------------------------------------------------


async def test_resolve_pool_address_returns_top_pool_source_tagged(mock_aiohttp):
    mock_aiohttp.get(
        _pools_url("solana", CA),
        payload={"data": [_pool("POOL_A", "15000")]},
    )
    async with aiohttp.ClientSession() as session:
        ref = await resolve_pool_address(session, chain="solana", contract_address=CA)

    assert isinstance(ref, PoolRef)
    assert ref.pool_address == "POOL_A"
    assert ref.network == "solana"
    assert ref.source == PRICE_SOURCE == "gt"


async def test_resolve_pool_address_deterministic_picks_highest_reserve(mock_aiohttp):
    # Listed out of order; the highest-reserve pool must win regardless of order.
    mock_aiohttp.get(
        _pools_url("solana", CA),
        payload={
            "data": [
                _pool("POOL_SMALL", "1000"),
                _pool("POOL_BIG", "90000"),
                _pool("POOL_MID", "5000"),
            ]
        },
    )
    async with aiohttp.ClientSession() as session:
        ref = await resolve_pool_address(session, chain="solana", contract_address=CA)

    assert ref.pool_address == "POOL_BIG"
    assert ref.reserve_usd == 90000.0


async def test_resolve_pool_address_missing_pool_returns_none_not_error(mock_aiohttp):
    # No pools is a normal empty result, NOT a provider error.
    mock_aiohttp.get(_pools_url("solana", CA), payload={"data": []})
    async with aiohttp.ClientSession() as session:
        ref = await resolve_pool_address(session, chain="solana", contract_address=CA)

    assert ref is None


async def test_resolve_pool_address_provider_error_raises(mock_aiohttp):
    for _ in range(3):
        mock_aiohttp.get(_pools_url("solana", CA), status=500)
    with patch("scout.ingestion.gt_ohlcv.asyncio.sleep", new=AsyncMock()):
        async with aiohttp.ClientSession() as session:
            with pytest.raises(PriceProviderError) as exc:
                await resolve_pool_address(session, chain="solana", contract_address=CA)
    assert exc.value.source == "geckoterminal"
    assert "500" in exc.value.reason


async def test_resolve_pool_address_malformed_raises(mock_aiohttp):
    # Pool with no address and an unparseable id -> malformed, must not fake one.
    mock_aiohttp.get(
        _pools_url("solana", CA),
        payload={"data": [{"attributes": {"reserve_in_usd": "100"}}]},
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(PriceProviderError):
            await resolve_pool_address(session, chain="solana", contract_address=CA)


async def test_resolve_pool_address_skips_addressless_pool_when_valid_exists(
    mock_aiohttp,
):
    # A malformed (address-less) entry with HIGHER reserve must not shadow a
    # valid pool: resolution returns the valid pool, it does not raise.
    mock_aiohttp.get(
        _pools_url("solana", CA),
        payload={
            "data": [
                {"attributes": {"reserve_in_usd": "99999"}},  # no id, no address
                _pool("POOL_V", "1000"),
            ]
        },
    )
    async with aiohttp.ClientSession() as session:
        ref = await resolve_pool_address(session, chain="solana", contract_address=CA)
    assert ref.pool_address == "POOL_V"


async def test_resolve_pool_address_eth_chain_maps_network(mock_aiohttp):
    # ethereum -> eth network mapping is reused from geckoterminal.
    mock_aiohttp.get(
        _pools_url("eth", CA),
        payload={"data": [_pool("POOL_E", "20000")]},
    )
    async with aiohttp.ClientSession() as session:
        ref = await resolve_pool_address(session, chain="ethereum", contract_address=CA)
    assert ref.network == "eth"
    assert ref.pool_address == "POOL_E"


# --------------------------------------------------------------------------
# fetch_pool_ohlcv
# --------------------------------------------------------------------------


async def test_fetch_pool_ohlcv_returns_ascending_candles_source_tagged(mock_aiohttp):
    # GT returns newest-first; client must return ascending by timestamp.
    mock_aiohttp.get(
        _ohlcv_url("solana", "POOL_A"),
        payload={
            "data": {
                "attributes": {
                    "ohlcv_list": [
                        [1718000460, 0.012, 0.013, 0.011, 0.0125, 7000.0],
                        [1718000400, 0.010, 0.012, 0.009, 0.011, 5000.0],
                    ]
                }
            }
        },
    )
    async with aiohttp.ClientSession() as session:
        candles = await fetch_pool_ohlcv(
            session, network="solana", pool_address="POOL_A"
        )

    assert [c.timestamp for c in candles] == [1718000400, 1718000460]
    assert candles[0].close == 0.011
    assert candles[1].close == 0.0125
    assert candles[0].source == "gt"


async def test_fetch_pool_ohlcv_empty_returns_empty_list_not_error(mock_aiohttp):
    mock_aiohttp.get(
        _ohlcv_url("solana", "POOL_DEAD"),
        payload={"data": {"attributes": {"ohlcv_list": []}}},
    )
    async with aiohttp.ClientSession() as session:
        candles = await fetch_pool_ohlcv(
            session, network="solana", pool_address="POOL_DEAD"
        )
    assert candles == []


async def test_fetch_pool_ohlcv_malformed_raises(mock_aiohttp):
    mock_aiohttp.get(
        _ohlcv_url("solana", "POOL_A"),
        payload={"data": {"attributes": {}}},  # no ohlcv_list
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(PriceProviderError):
            await fetch_pool_ohlcv(session, network="solana", pool_address="POOL_A")


async def test_fetch_pool_ohlcv_provider_error_raises(mock_aiohttp):
    for _ in range(3):
        mock_aiohttp.get(_ohlcv_url("solana", "POOL_A"), status=502)
    with patch("scout.ingestion.gt_ohlcv.asyncio.sleep", new=AsyncMock()):
        async with aiohttp.ClientSession() as session:
            with pytest.raises(PriceProviderError):
                await fetch_pool_ohlcv(session, network="solana", pool_address="POOL_A")


# --------------------------------------------------------------------------
# rate-limit behavior + malformed JSON (shared request path)
# --------------------------------------------------------------------------


async def test_rate_limit_429_then_success(mock_aiohttp):
    mock_aiohttp.get(_pools_url("solana", CA), status=429)
    mock_aiohttp.get(
        _pools_url("solana", CA), payload={"data": [_pool("POOL_A", "15000")]}
    )
    with patch("scout.ingestion.gt_ohlcv.asyncio.sleep", new=AsyncMock()) as slept:
        async with aiohttp.ClientSession() as session:
            ref = await resolve_pool_address(
                session, chain="solana", contract_address=CA
            )
    assert ref.pool_address == "POOL_A"
    slept.assert_awaited()  # backed off before retrying


async def test_rate_limit_exhausted_raises(mock_aiohttp):
    for _ in range(3):
        mock_aiohttp.get(_pools_url("solana", CA), status=429)
    with patch("scout.ingestion.gt_ohlcv.asyncio.sleep", new=AsyncMock()):
        async with aiohttp.ClientSession() as session:
            with pytest.raises(PriceProviderError) as exc:
                await resolve_pool_address(session, chain="solana", contract_address=CA)
    assert "429" in exc.value.reason


async def test_malformed_json_raises(mock_aiohttp):
    mock_aiohttp.get(
        _pools_url("solana", CA), body="<<not json>>", content_type="text/plain"
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(PriceProviderError):
            await resolve_pool_address(session, chain="solana", contract_address=CA)
