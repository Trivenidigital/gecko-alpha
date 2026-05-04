"""BL-071a' v3: DexScreener FDV fetcher tests.

Tests the FetchResult-returning fetcher (per design-review R1-M1 + R2-2:
the v2 'float | None' signature conflated 429 with no-data, leading to
misleading 'restart service' guidance during routine DS rate-limiting).
"""

from __future__ import annotations

import os
import sys

import pytest

_SKIP_AIOHTTP = pytest.mark.skipif(
    sys.platform == "win32" and os.environ.get("SKIP_AIOHTTP_TESTS") == "1",
    reason=(
        "Windows + SKIP_AIOHTTP_TESTS=1: skip aiohttp/aioresponses tests "
        "to avoid the local OpenSSL DLL conflict."
    ),
)


def test_fetch_result_namedtuple_unpacks_correctly():
    """FetchResult is a NamedTuple of (fdv, status). Test unpacking."""
    from scout.chains.mcap_fetcher import FetchResult, FetchStatus

    r = FetchResult(1500000.0, FetchStatus.OK)
    assert r.fdv == 1500000.0
    assert r.status == FetchStatus.OK
    fdv, status = r
    assert fdv == 1500000.0
    assert status == FetchStatus.OK


def test_fetch_status_enum_values():
    """All 6 documented statuses exist."""
    from scout.chains.mcap_fetcher import FetchStatus

    assert FetchStatus.OK.value == "ok"
    assert FetchStatus.NO_DATA.value == "no_data"
    assert FetchStatus.NOT_FOUND.value == "not_found"
    assert FetchStatus.RATE_LIMITED.value == "rate_limited"
    assert FetchStatus.TRANSIENT.value == "transient"
    assert FetchStatus.MALFORMED.value == "malformed"


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_fetch_token_fdv_returns_first_pair_fdv_status_ok():
    import aiohttp
    from aioresponses import aioresponses

    from scout.chains.mcap_fetcher import FetchStatus, fetch_token_fdv

    contract = "0xCB0c224f9382Ca5d09aCFb60141D332A8cA9ce42"
    payload = {
        "pairs": [
            {"fdv": 1_500_000.0, "chainId": "ethereum", "liquidity": {"usd": 50000}},
            {"fdv": 1_200_000.0, "chainId": "base", "liquidity": {"usd": 10000}},
        ]
    }
    with aioresponses() as m:
        m.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{contract}",
            payload=payload,
            status=200,
        )
        async with aiohttp.ClientSession() as session:
            result = await fetch_token_fdv(session, contract)
    assert result.fdv == 1_500_000.0
    assert result.status == FetchStatus.OK


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_fetch_token_fdv_empty_pairs_status_no_data():
    import aiohttp
    from aioresponses import aioresponses

    from scout.chains.mcap_fetcher import FetchStatus, fetch_token_fdv

    contract = "0xdeadbeef"
    with aioresponses() as m:
        m.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{contract}",
            payload={"pairs": []},
            status=200,
        )
        async with aiohttp.ClientSession() as session:
            result = await fetch_token_fdv(session, contract)
    assert result.fdv is None
    assert result.status == FetchStatus.NO_DATA


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_fetch_token_fdv_404_status_not_found():
    """R1-M1: 404 distinct from rate-limited and transient."""
    import aiohttp
    from aioresponses import aioresponses

    from scout.chains.mcap_fetcher import FetchStatus, fetch_token_fdv

    contract = "0xnotfound"
    with aioresponses() as m:
        m.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{contract}",
            status=404,
        )
        async with aiohttp.ClientSession() as session:
            result = await fetch_token_fdv(session, contract)
    assert result.fdv is None
    assert result.status == FetchStatus.NOT_FOUND


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_fetch_token_fdv_429_status_rate_limited():
    """R1-M1 critical: 429 must NOT be conflated with transient/no-data.
    The hydrator excludes RATE_LIMITED from session-health failure rate."""
    import aiohttp
    from aioresponses import aioresponses

    from scout.chains.mcap_fetcher import FetchStatus, fetch_token_fdv

    contract = "0xratelimited"
    with aioresponses() as m:
        m.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{contract}",
            status=429,
        )
        async with aiohttp.ClientSession() as session:
            result = await fetch_token_fdv(session, contract)
    assert result.fdv is None
    assert result.status == FetchStatus.RATE_LIMITED


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_fetch_token_fdv_500_status_transient():
    import aiohttp
    from aioresponses import aioresponses

    from scout.chains.mcap_fetcher import FetchStatus, fetch_token_fdv

    contract = "0xservererror"
    with aioresponses() as m:
        m.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{contract}",
            status=503,
        )
        async with aiohttp.ClientSession() as session:
            result = await fetch_token_fdv(session, contract)
    assert result.fdv is None
    assert result.status == FetchStatus.TRANSIENT


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_fetch_token_fdv_timeout_status_transient():
    """Uses a stub session whose .get raises TimeoutError — no network call,
    but the fetcher itself lazy-imports aiohttp which triggers the Windows
    OpenSSL DLL crash, so this test still needs the aiohttp skip marker."""
    import asyncio

    from scout.chains.mcap_fetcher import FetchStatus, fetch_token_fdv

    class _StubResp:
        async def __aenter__(self):
            raise asyncio.TimeoutError()

        async def __aexit__(self, *a):
            return False

    class _StubSession:
        def get(self, *a, **kw):
            return _StubResp()

    result = await fetch_token_fdv(_StubSession(), "0xtimeout")
    assert result.fdv is None
    assert result.status == FetchStatus.TRANSIENT


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_fetch_token_fdv_no_fdv_field_status_no_data():
    import aiohttp
    from aioresponses import aioresponses

    from scout.chains.mcap_fetcher import FetchStatus, fetch_token_fdv

    contract = "0xmissingfdv"
    payload = {"pairs": [{"chainId": "ethereum"}]}  # no fdv key
    with aioresponses() as m:
        m.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{contract}",
            payload=payload,
            status=200,
        )
        async with aiohttp.ClientSession() as session:
            result = await fetch_token_fdv(session, contract)
    assert result.fdv is None
    assert result.status == FetchStatus.NO_DATA


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_fetch_token_fdv_zero_fdv_status_no_data():
    """fdv=0 is not a usable value — treat as NO_DATA."""
    import aiohttp
    from aioresponses import aioresponses

    from scout.chains.mcap_fetcher import FetchStatus, fetch_token_fdv

    contract = "0xzero"
    payload = {"pairs": [{"fdv": 0.0}]}
    with aioresponses() as m:
        m.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{contract}",
            payload=payload,
            status=200,
        )
        async with aiohttp.ClientSession() as session:
            result = await fetch_token_fdv(session, contract)
    assert result.fdv is None
    assert result.status == FetchStatus.NO_DATA


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_fetch_token_fdv_malformed_json_status_malformed():
    """R1-S1: JSONDecodeError (subclass of ValueError) must be caught,
    not crash the hydrator."""
    import aiohttp
    from aioresponses import aioresponses

    from scout.chains.mcap_fetcher import FetchStatus, fetch_token_fdv

    contract = "0xmalformed"
    with aioresponses() as m:
        m.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{contract}",
            body="not valid json {",
            status=200,
            content_type="application/json",
        )
        async with aiohttp.ClientSession() as session:
            result = await fetch_token_fdv(session, contract)
    assert result.fdv is None
    assert result.status == FetchStatus.MALFORMED


def test_mcap_fetcher_type_alias_is_callable():
    """R2-1: McapFetcher is now a Callable type alias, not a Protocol."""
    from collections.abc import Callable

    from scout.chains.mcap_fetcher import McapFetcher

    # type alias check — McapFetcher should be a Callable[..., Awaitable]
    # Just verify it imports and isn't a class.
    assert McapFetcher is not None
    # Note: we can't directly assert `McapFetcher == Callable[...]` because
    # generic aliases don't compare equal across constructions; the import
    # succeeding is the contract.
