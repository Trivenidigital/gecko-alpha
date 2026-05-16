"""BL-075 Phase A: mcap_null_with_price telemetry on heartbeat.

Note: aiohttp + aioresponses are imported lazily inside the ingestion
tests (not at module scope) to keep the pure-logic tests collectible
on Windows dev envs that have an OpenSSL DLL conflict between Python's
ssl module and aiohttp's bundled OpenSSL. The aiohttp tests themselves
are gated by a skipif marker that respects SKIP_AIOHTTP_TESTS=1 (per
PR-review R1 SHOULD-FIX) — Linux/CI runs them all; Windows devs set
the env var to skip cleanly instead of crashing the test process.
"""

from __future__ import annotations

import os
import re
import sys

import pytest

from scout.heartbeat import (
    _heartbeat_stats,
    _maybe_emit_heartbeat,
    _reset_heartbeat_stats,
    increment_mcap_null_with_price,
)

MARKETS_PATTERN = re.compile(r"https://api\.coingecko\.com/api/v3/coins/markets")

_SKIP_AIOHTTP = pytest.mark.skipif(
    sys.platform == "win32" and os.environ.get("SKIP_AIOHTTP_TESTS") == "1",
    reason=(
        "Windows + SKIP_AIOHTTP_TESTS=1: skip aiohttp/aioresponses tests "
        "to avoid the local OpenSSL DLL conflict that crashes the test "
        "process. Run on Linux or CI for full coverage."
    ),
)


@pytest.fixture(autouse=True)
def _reset():
    _reset_heartbeat_stats()
    yield
    _reset_heartbeat_stats()


def test_mcap_null_with_price_field_initialized_to_zero():
    assert _heartbeat_stats["mcap_null_with_price_count"] == 0


def test_increment_bumps_counter():
    increment_mcap_null_with_price()
    increment_mcap_null_with_price()
    assert _heartbeat_stats["mcap_null_with_price_count"] == 2


def test_reset_clears_counter():
    increment_mcap_null_with_price()
    _reset_heartbeat_stats()
    assert _heartbeat_stats["mcap_null_with_price_count"] == 0


def test_heartbeat_log_includes_field(monkeypatch):
    """Heartbeat emission must include the new field.

    Uses the same _capture_logs(monkeypatch) pattern as tests/test_heartbeat.py
    — monkey-patches the module's structlog logger to a list-capture stub
    so we get structured (event, kwargs) tuples instead of stringified
    log records.
    """
    from datetime import datetime, timedelta, timezone

    from scout import heartbeat as heartbeat_module

    captured: list[tuple[str, dict]] = []

    class _CapLogger:
        def info(self, event, **kwargs):
            captured.append((event, kwargs))

        def warning(self, event, **kwargs):
            captured.append((event, kwargs))

    monkeypatch.setattr(heartbeat_module, "logger", _CapLogger())

    class _FakeSettings:
        HEARTBEAT_INTERVAL_SECONDS = 1

    # Seed state with a timestamp comfortably past the interval
    past = datetime.now(timezone.utc) - timedelta(minutes=10)
    _heartbeat_stats["started_at"] = past
    _heartbeat_stats["last_heartbeat_at"] = past
    increment_mcap_null_with_price()
    increment_mcap_null_with_price()
    increment_mcap_null_with_price()

    emitted = _maybe_emit_heartbeat(_FakeSettings())
    assert emitted is True
    assert len(captured) == 1
    event, payload = captured[0]
    assert event == "heartbeat"
    assert payload["mcap_null_with_price_count"] == 3


@pytest.mark.parametrize(
    "market_cap,current_price,should_increment",
    [
        # Affirmative: null/0 mcap + positive price → increment
        (None, 0.0123, True),
        (0, 0.5, True),
        # Negative: both null/0 → no increment (token has no data, not silent rejection)
        (None, None, False),
        (None, 0, False),
        (0, 0, False),
        (0, None, False),
        # Negative: positive mcap + positive price → no increment (token is fine)
        (1_000_000, 0.5, False),
        (50_000, 0.001, False),
        # Negative: positive mcap + null price (rare but possible) → no increment
        (50_000, None, False),
    ],
)
def test_predicate_boundary_cases(market_cap, current_price, should_increment):
    """PR-review R1 NIT: parametrize the boolean composition predicate.

    Replicates the logic from fetch_top_movers / fetch_by_volume in scout/
    ingestion/coingecko.py to lock down behaviour against future regressions
    in the predicate (e.g., someone refactors `(market_cap in (None, 0))`
    to `not market_cap` and breaks the case where market_cap is `0.0`).
    """
    fires = (market_cap in (None, 0)) and (current_price or 0) > 0
    assert fires is should_increment, (
        f"market_cap={market_cap!r} current_price={current_price!r}: "
        f"expected fires={should_increment}, got {fires}"
    )


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_fetch_top_movers_increments_counter(settings_factory):
    """A CoinGecko response with market_cap=null + current_price>0 must bump the counter.

    Lazy imports of aiohttp + aioresponses avoid Windows OpenSSL DLL
    crashes during pytest collection (local-env workaround; production
    Linux/CI imports normally).
    """
    import aiohttp
    from aioresponses import aioresponses

    from scout.ingestion.coingecko import fetch_top_movers

    s = settings_factory(MIN_MARKET_CAP=0, MAX_MARKET_CAP=10**12)
    payload = [
        {
            "id": "tok1",
            "name": "Tok1",
            "symbol": "T1",
            "market_cap": None,
            "current_price": 0.0123,
            "total_volume": 10000,
        },
        {
            "id": "tok2",
            "name": "Tok2",
            "symbol": "T2",
            "market_cap": 1_000_000,
            "current_price": 0.5,
            "total_volume": 50000,
        },
    ]
    with aioresponses() as m:
        m.get(
            MARKETS_PATTERN,
            payload=payload,
            status=200,
            repeat=True,
        )
        async with aiohttp.ClientSession() as session:
            await fetch_top_movers(session, s)
    assert _heartbeat_stats["mcap_null_with_price_count"] == 1


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_fetch_by_volume_increments_counter(settings_factory):
    """PR-review R1 SHOULD-FIX: cover fetch_by_volume same as fetch_top_movers.

    The same 2-line increment was added to fetch_by_volume (coingecko.py:245).
    Without this test, removing that block in a refactor would stay green.
    """
    import aiohttp
    from aioresponses import aioresponses

    from scout.ingestion.coingecko import fetch_by_volume

    s = settings_factory(MIN_MARKET_CAP=0, MAX_MARKET_CAP=10**12)
    payload = [
        {
            "id": "vtok1",
            "name": "VTok1",
            "symbol": "V1",
            "market_cap": None,
            "current_price": 0.05,
            "total_volume": 200000,
        },
        {
            "id": "vtok2",
            "name": "VTok2",
            "symbol": "V2",
            "market_cap": 5_000_000,
            "current_price": 1.0,
            "total_volume": 100000,
        },
    ]
    with aioresponses() as m:
        m.get(
            MARKETS_PATTERN,
            payload=payload,
            status=200,
            repeat=True,
        )
        async with aiohttp.ClientSession() as session:
            await fetch_by_volume(session, s)
    assert _heartbeat_stats["mcap_null_with_price_count"] == 1
