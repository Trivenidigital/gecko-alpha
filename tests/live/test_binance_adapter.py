"""Tests for BinanceSpotAdapter (spec §7, §8, §9, §10.1)."""

from decimal import Decimal
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.config import Settings
from scout.live.binance_adapter import BinanceSpotAdapter
from scout.live.exceptions import (
    LiveError,
    RateLimitError,
    VenueTransientError,
)


def _settings():
    return Settings(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )


async def test_fetch_exchange_info_row_happy_path():
    with aioresponses() as m:
        m.get(
            "https://api.binance.com/api/v3/exchangeInfo?symbol=WBTCUSDT",
            payload={
                "symbols": [{
                    "symbol": "WBTCUSDT",
                    "status": "TRADING",
                    "baseAsset": "WBTC",
                    "quoteAsset": "USDT",
                }]
            },
            headers={"X-MBX-USED-WEIGHT-1M": "12"},
        )
        adapter = BinanceSpotAdapter(_settings())
        row = await adapter.fetch_exchange_info_row("WBTCUSDT")
        assert row is not None and row["status"] == "TRADING"
        await adapter.close()


async def test_fetch_exchange_info_row_returns_none_on_404():
    """Legacy name — Binance returns 400 + code=-1121 for unknown symbols."""
    with aioresponses() as m:
        m.get(
            "https://api.binance.com/api/v3/exchangeInfo?symbol=ZZZZZUSDT",
            status=400,
            payload={"code": -1121, "msg": "Invalid symbol."},
        )
        adapter = BinanceSpotAdapter(_settings())
        assert await adapter.fetch_exchange_info_row("ZZZZZUSDT") is None
        await adapter.close()


async def test_fetch_depth_returns_parsed_depth():
    with aioresponses() as m:
        m.get(
            "https://api.binance.com/api/v3/depth?symbol=WBTCUSDT&limit=100",
            payload={
                "bids": [["100.0", "1.0"], ["99.5", "2.0"]],
                "asks": [["100.5", "1.0"], ["101.0", "2.0"]],
            },
            headers={"X-MBX-USED-WEIGHT-1M": "20"},
        )
        adapter = BinanceSpotAdapter(_settings())
        depth = await adapter.fetch_depth("WBTCUSDT")
        assert depth.pair == "WBTCUSDT"
        assert depth.asks[0].price == Decimal("100.5")
        assert depth.bids[0].price == Decimal("100.0")
        assert depth.mid == Decimal("100.25")
        await adapter.close()


async def test_semaphore_shrinks_at_80pct_weight():
    """Spec §9.1: when used weight >= 960 (80%), semaphore drops to 3."""
    with aioresponses() as m:
        m.get(
            "https://api.binance.com/api/v3/depth?symbol=X&limit=100",
            payload={"bids": [["1","1"]], "asks": [["1.01","1"]]},
            headers={"X-MBX-USED-WEIGHT-1M": "965"},
        )
        adapter = BinanceSpotAdapter(_settings())
        await adapter.fetch_depth("X")
        assert adapter._current_semaphore_cap == 3
        await adapter.close()


async def test_429_respects_retry_after():
    """Spec §9.1+§10.1: 429 raises RateLimitError (subclass of
    VenueTransientError) on first hit — no in-call retry; the governor
    handles backoff for subsequent calls. (Updated from the legacy
    ClientResponseError expectation to match the new taxonomy.)"""
    with aioresponses() as m:
        m.get(
            "https://api.binance.com/api/v3/ticker/price?symbol=X",
            status=429,
            headers={"Retry-After": "5"},
        )
        m.get(
            "https://api.binance.com/api/v3/ticker/price?symbol=X",
            payload={"symbol": "X", "price": "1.0"},
            headers={"X-MBX-USED-WEIGHT-1M": "10"},
        )
        adapter = BinanceSpotAdapter(_settings())
        with pytest.raises(RateLimitError):
            await adapter.fetch_price("X")
        await adapter.close()


async def test_send_order_raises_not_implemented_in_shadow():
    """Spec §1.3: BL-055 never sends real orders. Even constructed in shadow
    mode, send_order must raise NotImplementedError so an accidental call
    path cannot escape to Binance."""
    adapter = BinanceSpotAdapter(_settings())
    try:
        with pytest.raises(NotImplementedError):
            await adapter.send_order(pair="WBTCUSDT", side="BUY",
                                     size_usd=Decimal("100"))
    finally:
        await adapter.close()


# --- Retry taxonomy (spec §10.1) -----------------------------------------

async def test_http_get_retries_5xx_three_times():
    """5xx → up to 3 retries with backoff [1.0, 2.0, 4.0] then raise
    VenueTransientError."""
    with aioresponses() as m:
        for _ in range(4):
            m.get(
                "https://api.binance.com/api/v3/ticker/price?symbol=X",
                status=502,
            )
        adapter = BinanceSpotAdapter(_settings())
        # Patch the backoff sleep so the test doesn't actually wait 1+2+4s.
        adapter._retry_sleep = AsyncMock()
        with pytest.raises(VenueTransientError):
            await adapter.fetch_price("X")
        # Three retry sleeps should have fired (one per 5xx).
        assert adapter._retry_sleep.await_count == 3
        await adapter.close()


async def test_http_get_no_retry_on_4xx():
    """400/401/403 (other than -1121) → raise immediately, no retry."""
    with aioresponses() as m:
        m.get(
            "https://api.binance.com/api/v3/ticker/price?symbol=X",
            status=400,
            payload={"code": -9999, "msg": "bad request"},
        )
        adapter = BinanceSpotAdapter(_settings())
        adapter._retry_sleep = AsyncMock()
        with pytest.raises((aiohttp.ClientResponseError, LiveError)):
            await adapter.fetch_price("X")
        assert adapter._retry_sleep.await_count == 0
        await adapter.close()


async def test_fetch_exchange_info_returns_none_on_1121():
    """Binance body {code: -1121, msg: "Invalid symbol."} on 400 →
    fetch_exchange_info_row returns None, no retry, no raise."""
    with aioresponses() as m:
        m.get(
            "https://api.binance.com/api/v3/exchangeInfo?symbol=NOPEUSDT",
            status=400,
            payload={"code": -1121, "msg": "Invalid symbol."},
        )
        adapter = BinanceSpotAdapter(_settings())
        adapter._retry_sleep = AsyncMock()
        assert await adapter.fetch_exchange_info_row("NOPEUSDT") is None
        assert adapter._retry_sleep.await_count == 0
        await adapter.close()


async def test_http_get_raises_rate_limit_on_429():
    """429 → raise RateLimitError on first occurrence (no in-call retry;
    governor opens the gate at next call)."""
    with aioresponses() as m:
        m.get(
            "https://api.binance.com/api/v3/ticker/price?symbol=X",
            status=429,
            headers={"Retry-After": "5"},
        )
        adapter = BinanceSpotAdapter(_settings())
        with pytest.raises(RateLimitError):
            await adapter.fetch_price("X")
        await adapter.close()


async def test_429_increments_binance_rate_limit_hits_metric(tmp_path):
    """On 429, adapter increments live_metrics_daily binance_rate_limit_hits
    before raising RateLimitError.

    TODO(BL-055 Task 11): re-enable once scout.live.metrics.inc exists.
    """
    from scout.db import Database
    db = Database(tmp_path / "t.db"); await db.initialize()
    with aioresponses() as m:
        m.get(
            "https://api.binance.com/api/v3/ticker/price?symbol=X",
            status=429,
            headers={"Retry-After": "2"},
        )
        adapter = BinanceSpotAdapter(_settings(), db=db)
        with pytest.raises(RateLimitError):
            await adapter.fetch_price("X")
        cur = await db._conn.execute(
            "SELECT value FROM live_metrics_daily "
            "WHERE metric='binance_rate_limit_hits'"
        )
        row = await cur.fetchone()
        assert row is not None and row[0] >= 1
        await adapter.close()
    await db.close()


async def test_http_get_retries_network_error():
    """aiohttp.ClientConnectorError / asyncio.TimeoutError → retry 3x then
    raise VenueTransientError."""
    with aioresponses() as m:
        for _ in range(4):
            m.get(
                "https://api.binance.com/api/v3/ticker/price?symbol=X",
                exception=aiohttp.ClientConnectorError(
                    connection_key=None, os_error=OSError()
                ),
            )
        adapter = BinanceSpotAdapter(_settings())
        adapter._retry_sleep = AsyncMock()
        with pytest.raises(VenueTransientError):
            await adapter.fetch_price("X")
        assert adapter._retry_sleep.await_count == 3
        await adapter.close()


async def test_rate_governor_opens_gate_after_10s():
    """Spec §9.1: weight >= 1140 (95%) → gate closes, then reopens ~10s later
    so concurrent requests resume in parallel (not 10s-serial). Asserts the
    asyncio.Event-based backpressure pattern."""
    import asyncio
    with aioresponses() as m:
        m.get(
            "https://api.binance.com/api/v3/ticker/price?symbol=Y",
            payload={"symbol": "Y", "price": "1.0"},
            headers={"X-MBX-USED-WEIGHT-1M": "1145"},
        )
        adapter = BinanceSpotAdapter(_settings())
        # Patch the 10s pause into a fast tick for the test.
        adapter._RATE_LIMIT_PAUSE_SEC = 0.05
        await adapter.fetch_price("Y")  # triggers gate close + reopen task
        assert not adapter._rate_limit_gate.is_set()
        # Wait long enough for the reopen task to fire.
        await asyncio.sleep(0.1)
        assert adapter._rate_limit_gate.is_set()
        await adapter.close()
