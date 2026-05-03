"""BL-075 Phase A: mcap_null_with_price telemetry on heartbeat.

Note: aiohttp + aioresponses are imported lazily inside the ingestion
test (not at module scope) to keep the first 4 tests collectible on
Windows dev envs that have an OpenSSL DLL conflict between Python's
ssl module and aiohttp's bundled OpenSSL. This is a local-env
mitigation, not a production change. Tests behave identically when
collected on Linux/CI.
"""

from __future__ import annotations

import pytest

from scout.heartbeat import (
    _heartbeat_stats,
    _maybe_emit_heartbeat,
    _reset_heartbeat_stats,
    increment_mcap_null_with_price,
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
            "https://api.coingecko.com/api/v3/coins/markets",
            payload=payload,
            status=200,
            repeat=True,
        )
        async with aiohttp.ClientSession() as session:
            await fetch_top_movers(session, s)
    assert _heartbeat_stats["mcap_null_with_price_count"] == 1
