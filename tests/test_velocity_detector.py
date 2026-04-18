"""Tests for scout.velocity.detector -- CoinGecko 1h-velocity alerter."""

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.velocity.detector import (
    detect_velocity,
    format_velocity_alert,
)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


def _coin(
    coin_id: str,
    *,
    price_change_1h: float = 40.0,
    price_change_24h: float = 60.0,
    market_cap: float = 10_000_000,
    total_volume: float = 5_000_000,
    current_price: float = 0.001,
    symbol: str | None = None,
    name: str | None = None,
) -> dict:
    return {
        "id": coin_id,
        "symbol": symbol or coin_id[:3],
        "name": name or coin_id.title(),
        "price_change_percentage_1h_in_currency": price_change_1h,
        "price_change_percentage_24h": price_change_24h,
        "market_cap": market_cap,
        "total_volume": total_volume,
        "current_price": current_price,
    }


class _Settings:
    VELOCITY_ALERTS_ENABLED = True
    VELOCITY_MIN_1H_PCT = 30.0
    VELOCITY_MIN_MCAP = 500_000
    VELOCITY_MAX_MCAP = 50_000_000
    VELOCITY_MIN_VOL_MCAP_RATIO = 0.2
    VELOCITY_DEDUP_HOURS = 4
    VELOCITY_TOP_N = 10


# -- Filter tests --


async def test_detect_velocity_accepts_qualifying_coin(db):
    coins = [_coin("rocket")]
    detections = await detect_velocity(db, coins, _Settings())
    assert len(detections) == 1
    assert detections[0]["coin_id"] == "rocket"
    assert detections[0]["price_change_1h"] == 40.0


async def test_detect_velocity_skips_below_1h_threshold(db):
    coins = [_coin("slow", price_change_1h=15.0)]
    detections = await detect_velocity(db, coins, _Settings())
    assert detections == []


async def test_detect_velocity_skips_below_min_mcap(db):
    coins = [_coin("dust", market_cap=100_000)]
    detections = await detect_velocity(db, coins, _Settings())
    assert detections == []


async def test_detect_velocity_skips_above_max_mcap(db):
    coins = [_coin("mega", market_cap=200_000_000)]
    detections = await detect_velocity(db, coins, _Settings())
    assert detections == []


async def test_detect_velocity_skips_low_vol_mcap_ratio(db):
    # volume/mcap = 100k / 10M = 0.01 < 0.2
    coins = [_coin("stale", total_volume=100_000, market_cap=10_000_000)]
    detections = await detect_velocity(db, coins, _Settings())
    assert detections == []


async def test_detect_velocity_skips_missing_fields(db):
    coins = [
        {"id": "noop", "symbol": "x", "name": "x"},
        {"id": None, "price_change_percentage_1h_in_currency": 50.0},
        _coin("ok"),
    ]
    detections = await detect_velocity(db, coins, _Settings())
    assert {d["coin_id"] for d in detections} == {"ok"}


async def test_detect_velocity_limits_to_top_n(db):
    class Cfg(_Settings):
        VELOCITY_TOP_N = 3

    coins = [
        _coin(f"c{i}", price_change_1h=30.0 + i) for i in range(10)
    ]
    detections = await detect_velocity(db, coins, Cfg())
    assert len(detections) == 3
    # highest 1h change first
    assert detections[0]["coin_id"] == "c9"
    assert detections[-1]["coin_id"] == "c7"


# -- Dedup tests --


async def test_detect_velocity_dedups_recent_alert(db):
    coins = [_coin("dedup-me")]
    # First call: records the alert
    first = await detect_velocity(db, coins, _Settings())
    assert len(first) == 1
    # Second call within the dedup window: should be filtered
    second = await detect_velocity(db, coins, _Settings())
    assert second == []


async def test_detect_velocity_allows_after_dedup_window(db):
    coins = [_coin("old-alert")]
    # Manually insert a stale alert outside the window
    stale = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    await db._conn.execute(
        """INSERT INTO velocity_alerts
           (coin_id, symbol, name, price_change_1h, price_change_24h,
            market_cap, volume_24h, vol_mcap_ratio, current_price, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("old-alert", "OLD", "Old", 50.0, 80.0, 10_000_000, 5_000_000, 0.5, 0.001, stale),
    )
    await db._conn.commit()
    detections = await detect_velocity(db, coins, _Settings())
    assert len(detections) == 1
    assert detections[0]["coin_id"] == "old-alert"


# -- Formatting --


def test_format_velocity_alert_includes_core_fields():
    detection = {
        "coin_id": "asteroid",
        "symbol": "AST",
        "name": "Asteroid",
        "price_change_1h": 125.5,
        "price_change_24h": 650.0,
        "market_cap": 8_500_000,
        "volume_24h": 4_200_000,
        "vol_mcap_ratio": 0.49,
        "current_price": 0.00042,
    }
    msg = format_velocity_alert([detection])
    assert "AST" in msg
    assert "Asteroid" in msg
    assert "125.5" in msg
    assert "coingecko.com/en/coins/asteroid" in msg
