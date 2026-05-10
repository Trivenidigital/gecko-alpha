"""Tests for scout/spikes/detector.py::detect_slow_burn_7d (BL-075 Phase B).

Coverage matrix per design + R3 reviewer fixes:
- Happy path canonical shape
- 7d threshold boundary parametrized
- 1h SYMMETRIC boundary (R1 MUST-FIX): both directions of high volatility rejected
- Volume floor boundary parametrized (R3 MUST-FIX)
- Velocity-shape rejection (1h-high → not slow burn)
- Mcap mega-cap rejection
- Mcap-unknown cohort fires + structlog event captured (R3 CRITICAL caplog→capture_logs)
- Dedup within window
- Cross-detector overlap flag — both positive AND negative (R3 MUST-FIX)
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import structlog

from scout.db import Database
from scout.spikes.detector import detect_slow_burn_7d


def _coin(**overrides) -> dict:
    base = {
        "id": "test-coin",
        "symbol": "TEST",
        "name": "Test Coin",
        "price_change_percentage_7d_in_currency": 80.0,
        "price_change_percentage_1h_in_currency": 2.0,
        "price_change_percentage_24h": 12.0,
        "market_cap": 10_000_000,
        "current_price": 0.5,
        "total_volume": 200_000,
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _reset_heartbeat_for_slow_burn():
    """R5 MUST-FIX: heartbeat _heartbeat_stats is module-level global; without
    this autouse reset, any earlier-running test that exercises the detector
    pollutes counter state and breaks
    test_slow_burn_increments_heartbeat_counter (asserts == 1)."""
    from scout.heartbeat import _reset_heartbeat_stats

    _reset_heartbeat_stats()
    yield
    _reset_heartbeat_stats()


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "t.db"))
    await database.initialize()
    yield database
    await database.close()


# ----- Happy path -----


async def test_slow_burn_fires_on_canonical_shape(db):
    results = await detect_slow_burn_7d(db, [_coin()])
    assert len(results) == 1
    assert results[0]["coin_id"] == "test-coin"
    assert results[0]["price_change_7d"] == 80.0
    assert results[0]["also_in_momentum_7d"] == 0


# ----- 7d boundary -----


@pytest.mark.parametrize(
    "change_7d, should_fire",
    [(49.99, False), (50.0, True), (50.01, True)],
)
async def test_slow_burn_7d_threshold_boundary(db, change_7d, should_fire):
    coin = _coin(
        price_change_percentage_7d_in_currency=change_7d, id=f"test-{change_7d}"
    )
    results = await detect_slow_burn_7d(db, [coin])
    assert (len(results) == 1) is should_fire


# ----- 1h SYMMETRIC boundary (R1 MUST-FIX) -----


@pytest.mark.parametrize(
    "change_1h, should_fire",
    [
        (4.99, True),
        (5.0, True),
        (5.01, False),
        (-4.99, True),
        (-5.0, True),
        (-5.01, False),
        (-8.0, False),  # R1 example: -8% retrace, NOT a slow burn
    ],
)
async def test_slow_burn_1h_symmetric_boundary(db, change_1h, should_fire):
    coin = _coin(
        price_change_percentage_1h_in_currency=change_1h,
        id=f"test-1h-{change_1h}",
    )
    results = await detect_slow_burn_7d(db, [coin])
    assert (len(results) == 1) is should_fire


# ----- Velocity-shape rejection -----


async def test_velocity_shape_does_not_fire(db):
    """7d=80% + 1h=15% → concentrated pump, not slow burn → no fire."""
    coin = _coin(price_change_percentage_1h_in_currency=15.0, id="velocity")
    results = await detect_slow_burn_7d(db, [coin])
    assert len(results) == 0


# ----- Volume floor (R3 MUST-FIX boundary parametrize) -----


@pytest.mark.parametrize(
    "volume_24h, should_fire",
    [
        (99_999, False),
        (100_000, True),
        (100_001, True),
    ],
)
async def test_slow_burn_volume_boundary(db, volume_24h, should_fire):
    coin = _coin(total_volume=volume_24h, id=f"vol-{volume_24h}")
    results = await detect_slow_burn_7d(db, [coin])
    assert (len(results) == 1) is should_fire


async def test_volume_below_floor_does_not_fire(db):
    coin = _coin(total_volume=50_000, id="illiquid")
    results = await detect_slow_burn_7d(db, [coin])
    assert len(results) == 0


# ----- Mega-cap floor -----


async def test_mega_cap_does_not_fire(db):
    coin = _coin(market_cap=1_000_000_000, id="megacap")
    results = await detect_slow_burn_7d(db, [coin])
    assert len(results) == 0


# ----- Mcap-unknown cohort (Phase A blind-spot fix) -----


async def test_mcap_unknown_fires_with_null_market_cap(db):
    """RIV-style: CG returns null mcap. Detector must fire + emit structlog event.

    R3 CRITICAL: use structlog.testing.capture_logs (caplog is vacuous on
    structlog events).
    """
    coin = _coin(market_cap=None, id="riv-style")
    with structlog.testing.capture_logs() as captured:
        results = await detect_slow_burn_7d(db, [coin])
    assert len(results) == 1
    assert results[0]["market_cap"] is None
    events = [e["event"] for e in captured]
    assert (
        "slow_burn_mcap_unknown" in events
    ), f"slow_burn_mcap_unknown event missing; captured events: {events}"


async def test_mcap_zero_fires_with_null_normalized(db):
    """CG sometimes returns 0 instead of null — same blind-spot cohort.

    Detector normalizes 0 to None for the row, so downstream queries can
    cleanly use IS NULL semantics.
    """
    coin = _coin(market_cap=0, id="zero-mcap")
    results = await detect_slow_burn_7d(db, [coin])
    assert len(results) == 1
    assert results[0]["market_cap"] is None


# ----- Dedup -----


async def test_dedup_within_window_skips_second_detection(db):
    """Same coin twice within dedup_days → only first fires."""
    coin = _coin(id="dedup-test")
    results1 = await detect_slow_burn_7d(db, [coin])
    results2 = await detect_slow_burn_7d(db, [coin])
    assert len(results1) == 1
    assert len(results2) == 0


# ----- Cross-detector overlap flag (R1 MUST-FIX, R3 MUST-FIX negative case) -----


async def test_also_in_momentum_7d_flag_set_when_overlap(db):
    """Coin already in momentum_7d → also_in_momentum_7d=1 in slow_burn row."""
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO momentum_7d
           (coin_id, symbol, name, price_change_7d, price_change_24h,
            market_cap, current_price, volume_24h, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("overlap", "TEST", "Test", 120.0, 30.0, 5_000_000, 0.5, 300_000, now),
    )
    await db._conn.commit()
    coin = _coin(id="overlap")
    results = await detect_slow_burn_7d(db, [coin])
    assert len(results) == 1
    assert results[0]["also_in_momentum_7d"] == 1


async def test_also_in_momentum_7d_flag_zero_when_no_overlap(db):
    """R3 MUST-FIX: explicit negative case — coin NOT in momentum_7d → flag=0.

    Pre-seeds a DIFFERENT coin into momentum_7d to confirm query specificity
    (a bug where the query always returns a row would fall through to the
    default value 0; this test catches both over-fire AND never-set bugs).
    """
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO momentum_7d
           (coin_id, symbol, name, price_change_7d, price_change_24h,
            market_cap, current_price, volume_24h, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("other-coin", "OTHER", "Other", 200.0, 50.0, 10_000_000, 1.0, 500_000, now),
    )
    await db._conn.commit()
    coin = _coin(id="no-overlap")
    results = await detect_slow_burn_7d(db, [coin])
    assert len(results) == 1
    assert results[0]["also_in_momentum_7d"] == 0


# ----- Heartbeat counter increment (R4 MUST-FIX live observability) -----


async def test_slow_burn_increments_heartbeat_counter(db):
    """R4 MUST-FIX: detector must call increment_slow_burn_detected on commit.

    Without this, an env-mismatch silent-disable goes undetected until the
    D+3 SQL query.
    """
    from scout.heartbeat import _heartbeat_stats, _reset_heartbeat_stats

    _reset_heartbeat_stats()
    coin = _coin(id="counter-test")
    await detect_slow_burn_7d(db, [coin])
    assert _heartbeat_stats["slow_burn_detected_total"] == 1
