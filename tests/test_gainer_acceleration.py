"""Tests for scout.gainers.acceleration -- the gap-fill acceleration detector.

The detector reads existing ``volume_history_cg`` rows (zero CG calls), computes
1h/4h price change + volume expansion per coin, filters on mcap band + thresholds
+ a min-sample floor, dedups via a per-coin cooldown, and persists qualifying
detections to ``gainer_acceleration``. Research-only (no alert / no paper-trade).
"""

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.gainers.acceleration import detect_acceleration


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


def _ts(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


async def _insert_sample(
    db,
    coin_id,
    hours_ago,
    price,
    *,
    volume=1_000_000.0,
    mcap=50_000_000.0,
):
    await db._conn.execute(
        """INSERT INTO volume_history_cg
           (coin_id, symbol, name, volume_24h, market_cap, price, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            coin_id,
            coin_id[:4].upper(),
            coin_id.title(),
            volume,
            mcap,
            price,
            _ts(hours_ago),
        ),
    )


async def _insert_qualifying(db, coin_id, *, mcap=50_000_000.0):
    """A coin that should qualify: 1h +9%, 4h +20%, vol 3x, 4 samples, in band."""
    await _insert_sample(db, coin_id, 4.0, 1.00, volume=1_000_000.0, mcap=mcap)
    await _insert_sample(db, coin_id, 2.5, 1.05, volume=1_500_000.0, mcap=mcap)
    await _insert_sample(db, coin_id, 1.0, 1.10, volume=2_000_000.0, mcap=mcap)
    await _insert_sample(db, coin_id, 0.0, 1.20, volume=3_000_000.0, mcap=mcap)
    await db._conn.commit()


async def _accel_rows(db):
    cur = await db._conn.execute(
        "SELECT coin_id, change_1h, change_4h, vol_expansion, market_cap, "
        "current_price FROM gainer_acceleration ORDER BY coin_id"
    )
    return await cur.fetchall()


# -- Tests --


async def test_no_data_returns_empty(db, settings_factory):
    out = await detect_acceleration(db, settings_factory())
    assert out == []
    assert await _accel_rows(db) == []


async def test_qualifying_coin_detected_and_persisted(db, settings_factory):
    await _insert_qualifying(db, "accel-coin")
    out = await detect_acceleration(db, settings_factory())

    assert len(out) == 1
    d = out[0]
    assert d["coin_id"] == "accel-coin"
    assert d["change_1h"] == pytest.approx(9.0909, abs=0.01)
    assert d["change_4h"] == pytest.approx(20.0, abs=0.01)
    assert d["vol_expansion"] == pytest.approx(3.0, abs=0.01)

    rows = await _accel_rows(db)
    assert len(rows) == 1
    assert rows[0][0] == "accel-coin"


async def test_below_1h_threshold_skipped(db, settings_factory):
    # 4h change strong (+20%) but only +2% in the last hour -> not accelerating.
    await _insert_sample(db, "slow", 4.0, 1.00)
    await _insert_sample(db, "slow", 2.5, 1.10)
    await _insert_sample(db, "slow", 1.0, 1.177)  # 1.20 vs 1.177 ~= +2%
    await _insert_sample(db, "slow", 0.0, 1.20)
    await db._conn.commit()

    out = await detect_acceleration(db, settings_factory())
    assert out == []


async def test_below_4h_threshold_skipped(db, settings_factory):
    # +9% in the last hour but flat over 4h -> not a sustained 4h move.
    await _insert_sample(db, "spike", 4.0, 1.18)
    await _insert_sample(db, "spike", 2.5, 1.16)
    await _insert_sample(db, "spike", 1.0, 1.10)
    await _insert_sample(db, "spike", 0.0, 1.20)  # 4h: 1.20/1.18 ~= +1.7%
    await db._conn.commit()

    out = await detect_acceleration(db, settings_factory())
    assert out == []


async def test_mcap_above_band_skipped(db, settings_factory):
    await _insert_qualifying(db, "too-big", mcap=300_000_000.0)
    out = await detect_acceleration(db, settings_factory())
    assert out == []


async def test_mcap_below_band_skipped(db, settings_factory):
    await _insert_qualifying(db, "too-small", mcap=100_000.0)
    out = await detect_acceleration(db, settings_factory())
    assert out == []


async def test_null_mcap_skipped(db, settings_factory):
    await _insert_qualifying(db, "no-mcap", mcap=None)
    out = await detect_acceleration(db, settings_factory())
    assert out == []


async def test_insufficient_samples_skipped(db, settings_factory):
    # Only 2 samples (< ACCELERATION_MIN_SAMPLES=3) even though the move qualifies.
    await _insert_sample(db, "thin", 4.0, 1.00)
    await _insert_sample(db, "thin", 0.0, 1.20)
    await db._conn.commit()

    out = await detect_acceleration(db, settings_factory())
    assert out == []


async def test_volume_expansion_below_threshold_skipped(db, settings_factory):
    # Price qualifies, but volume only 1.5x (< ACCELERATION_MIN_VOL_EXPANSION=2.0).
    await _insert_sample(db, "flatvol", 4.0, 1.00, volume=2_000_000.0)
    await _insert_sample(db, "flatvol", 2.5, 1.05, volume=2_200_000.0)
    await _insert_sample(db, "flatvol", 1.0, 1.10, volume=2_500_000.0)
    await _insert_sample(db, "flatvol", 0.0, 1.20, volume=3_000_000.0)  # 3.0/2.0 = 1.5x
    await db._conn.commit()

    out = await detect_acceleration(db, settings_factory())
    assert out == []


async def test_cooldown_suppresses_recent_redetect(db, settings_factory):
    await _insert_qualifying(db, "cooldown-coin")
    # A prior detection 1h ago -> within the 4h cooldown -> suppressed.
    await db._conn.execute(
        """INSERT INTO gainer_acceleration
           (coin_id, symbol, name, change_1h, change_4h, vol_expansion,
            market_cap, current_price, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("cooldown-coin", "COOL", "Cooldown", 10.0, 20.0, 3.0, 5e7, 1.1, _ts(1.0)),
    )
    await db._conn.commit()

    out = await detect_acceleration(db, settings_factory())
    assert out == []


async def test_cooldown_expired_allows_redetect(db, settings_factory):
    await _insert_qualifying(db, "old-coin")
    # A prior detection 6h ago -> beyond the 4h cooldown -> re-detect allowed.
    await db._conn.execute(
        """INSERT INTO gainer_acceleration
           (coin_id, symbol, name, change_1h, change_4h, vol_expansion,
            market_cap, current_price, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("old-coin", "OLD", "Old", 10.0, 20.0, 3.0, 5e7, 1.1, _ts(6.0)),
    )
    await db._conn.commit()

    out = await detect_acceleration(db, settings_factory())
    assert len(out) == 1
    assert out[0]["coin_id"] == "old-coin"


async def test_disabled_returns_empty(db, settings_factory):
    await _insert_qualifying(db, "accel-coin")
    out = await detect_acceleration(db, settings_factory(ACCELERATION_ENABLED=False))
    assert out == []
    assert await _accel_rows(db) == []


async def test_top_n_caps_detections(db, settings_factory):
    for i in range(5):
        await _insert_qualifying(db, f"accel-{i}")
    out = await detect_acceleration(db, settings_factory(ACCELERATION_TOP_N=3))
    assert len(out) == 3
