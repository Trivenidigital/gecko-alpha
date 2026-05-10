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


# ----- Per-coin skip telemetry (post-merge user-feedback fix) -----


async def test_slow_burn_all_coins_throw_emits_visible_telemetry(db):
    """Post-merge fix: if every coin throws (e.g., CG schema regression that
    breaks all coercions), the watcher must emit visible telemetry — NOT
    silently report zero detections.

    Validates the three observability paths added by the follow-up:
    1. WARNING `slow_burn_all_results_skipped` (humans grep journalctl)
    2. INFO `slow_burn_detected` summary even with count=0 (dashboard parses)
    3. heartbeat counter slow_burn_coins_skipped_total bumps (heartbeat tick)
    """
    import structlog
    from scout.heartbeat import _heartbeat_stats, _reset_heartbeat_stats

    _reset_heartbeat_stats()

    # Coin shape passes filters but `total_volume` value will explode the
    # internal float coercion path indirectly — use a payload that survives
    # filters but trips the INSERT (mismatched-type market_cap that survives
    # _safe_float but causes downstream issues). Simulate with volume=NaN-like.
    bad_coins = [
        {
            "id": "boom-1",
            "symbol": "B1",
            "name": "Boom",
            # _safe_float(object()) → TypeError → caught and returns None
            # → defaults to 0 → fails 7d threshold.
            # To force the per-coin try/except path, raise INSIDE the coin
            # loop. Use a malformed `id` that's unhashable (a list) — coin.get
            # will return it but db.execute will fail downstream.
            "price_change_percentage_7d_in_currency": 80.0,
            "price_change_percentage_1h_in_currency": 2.0,
            "market_cap": 10_000_000,
            "current_price": 0.5,
            "total_volume": 200_000,
        },
    ]
    # Force INSERT failure by closing the connection (drastic but reliable).
    # The per-coin try/except should catch the failure and continue.
    saved_conn = db._conn
    saved_execute = saved_conn.execute

    fail_count = [0]
    select_failed = [False]

    async def boom_execute(sql, *args, **kwargs):
        if "INSERT INTO slow_burn_candidates" in sql:
            fail_count[0] += 1
            raise RuntimeError("simulated INSERT failure")
        return await saved_execute(sql, *args, **kwargs)

    saved_conn.execute = boom_execute
    try:
        with structlog.testing.capture_logs() as captured:
            results = await detect_slow_burn_7d(db, bad_coins)
    finally:
        saved_conn.execute = saved_execute

    assert results == []
    events = [e["event"] for e in captured]
    # 1. WARNING for the all-skipped pathology
    assert "slow_burn_all_results_skipped" in events
    # 2. INFO summary fired even though count=0
    assert "slow_burn_detected" in events
    summary = next(e for e in captured if e["event"] == "slow_burn_detected")
    assert summary["count"] == 0
    assert summary["coins_skipped"] >= 1
    # 3. heartbeat counter bumped
    assert _heartbeat_stats["slow_burn_coins_skipped_total"] >= 1


async def test_slow_burn_partial_skip_still_reports_results(db):
    """Mixed cycle: some coins succeed, some skip. Both surface in summary."""
    from scout.heartbeat import _reset_heartbeat_stats, _heartbeat_stats

    _reset_heartbeat_stats()

    good_coin = _coin(id="good")
    bad_coin = _coin(id="bad")

    saved_conn = db._conn
    saved_execute = saved_conn.execute

    async def selective_boom(sql, *args, **kwargs):
        if "INSERT INTO slow_burn_candidates" in sql:
            params = args[0] if args else ()
            if params and params[0] == "bad":
                raise RuntimeError("simulated INSERT failure for bad coin")
        return await saved_execute(sql, *args, **kwargs)

    saved_conn.execute = selective_boom
    try:
        results = await detect_slow_burn_7d(db, [good_coin, bad_coin])
    finally:
        saved_conn.execute = saved_execute

    assert len(results) == 1
    assert results[0]["coin_id"] == "good"
    assert _heartbeat_stats["slow_burn_detected_total"] == 1
    assert _heartbeat_stats["slow_burn_coins_skipped_total"] == 1
