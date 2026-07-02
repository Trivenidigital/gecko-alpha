"""Tests for scout.velocity.detector -- CoinGecko 1h-velocity alerter."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from scout.db import Database
from scout.velocity.detector import (
    alert_velocity_detections,
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

    coins = [_coin(f"c{i}", price_change_1h=30.0 + i) for i in range(10)]
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
        (
            "old-alert",
            "OLD",
            "Old",
            50.0,
            80.0,
            10_000_000,
            5_000_000,
            0.5,
            0.001,
            stale,
        ),
    )
    await db._conn.commit()
    detections = await detect_velocity(db, coins, _Settings())
    assert len(detections) == 1
    assert detections[0]["coin_id"] == "old-alert"


# -- GA-21 claim-then-demote: send failure must not hold the dedup claim --


async def test_alert_velocity_failed_send_demotes_claim(db):
    """A failed Telegram send deletes the just-claimed velocity_alerts row so
    the detection is re-alertable next cycle (mirrors tg_alert_dispatch
    claim-then-demote)."""
    detections = await detect_velocity(db, [_coin("failme")], _Settings())
    assert len(detections) == 1

    with patch(
        "scout.alerter.send_telegram_message",
        new_callable=AsyncMock,
        side_effect=RuntimeError("telegram send failed status=502"),
    ) as mock_send:
        ok = await alert_velocity_detections(
            detections, AsyncMock(), _Settings(), db=db
        )

    assert ok is False
    # Failure must be observable: the shared sender swallows errors unless
    # raise_on_failure=True is threaded through.
    assert mock_send.call_args.kwargs.get("raise_on_failure") is True
    # Claim demoted -> same coin re-detectable within the dedup window.
    again = await detect_velocity(db, [_coin("failme")], _Settings())
    assert len(again) == 1
    assert again[0]["coin_id"] == "failme"


async def test_alert_velocity_successful_send_keeps_dedup(db):
    """A successful send keeps the velocity_alerts row: dedup enforced for
    VELOCITY_DEDUP_HOURS."""
    detections = await detect_velocity(db, [_coin("winner")], _Settings())
    assert len(detections) == 1

    with patch(
        "scout.alerter.send_telegram_message", new_callable=AsyncMock
    ) as mock_send:
        ok = await alert_velocity_detections(
            detections, AsyncMock(), _Settings(), db=db
        )

    assert ok is True
    mock_send.assert_awaited_once()
    again = await detect_velocity(db, [_coin("winner")], _Settings())
    assert again == []


async def test_alert_velocity_demote_only_removes_failed_batch(db):
    """Demote deletes only the failed batch's rows -- an earlier successfully
    delivered coin stays deduped."""
    kept = await detect_velocity(db, [_coin("keeper")], _Settings())
    with patch("scout.alerter.send_telegram_message", new_callable=AsyncMock):
        assert await alert_velocity_detections(kept, AsyncMock(), _Settings(), db=db)

    failed = await detect_velocity(db, [_coin("loser")], _Settings())
    with patch(
        "scout.alerter.send_telegram_message",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        ok = await alert_velocity_detections(failed, AsyncMock(), _Settings(), db=db)
    assert ok is False

    # keeper still deduped; loser re-alertable.
    again = await detect_velocity(db, [_coin("keeper"), _coin("loser")], _Settings())
    assert {d["coin_id"] for d in again} == {"loser"}


async def test_alert_velocity_failed_send_without_db_does_not_crash(db):
    """Back-compat: callers that don't pass db still get the failure signal;
    demote is skipped (claim stays, same as pre-fix behavior)."""
    detections = await detect_velocity(db, [_coin("nodb")], _Settings())
    with patch(
        "scout.alerter.send_telegram_message",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        ok = await alert_velocity_detections(detections, AsyncMock(), _Settings())
    assert ok is False


async def test_alert_velocity_empty_detections_is_noop():
    with patch(
        "scout.alerter.send_telegram_message", new_callable=AsyncMock
    ) as mock_send:
        ok = await alert_velocity_detections([], AsyncMock(), _Settings())
    assert ok is True
    mock_send.assert_not_awaited()


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
