"""Tests for scout.gainers.tracker -- top gainers tracking and comparison."""

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.gainers.tracker import (
    compare_gainers_with_signals,
    get_gainers_comparisons,
    get_gainers_stats,
    get_recent_gainers,
    store_top_gainers,
)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


# -- Helpers --


def _make_raw_coin(
    coin_id: str,
    change_24h: float = 30.0,
    mcap: float = 100_000_000,
    volume: float = 1_000_000,
):
    return {
        "id": coin_id,
        "symbol": coin_id[:3],
        "name": coin_id.title(),
        "price_change_percentage_24h": change_24h,
        "market_cap": mcap,
        "total_volume": volume,
        "current_price": 1.0,
    }


# -- Tests --


async def test_store_top_gainers_basic(db):
    raw = [
        _make_raw_coin("gainer-a", change_24h=50.0),
        _make_raw_coin("gainer-b", change_24h=25.0),
    ]
    count = await store_top_gainers(db, raw, min_change=20.0)
    assert count == 2

    cursor = await db._conn.execute("SELECT COUNT(*) FROM gainers_snapshots")
    row = await cursor.fetchone()
    assert row[0] == 2


async def test_store_top_gainers_filters_low_change(db):
    raw = [
        _make_raw_coin("low-change", change_24h=10.0),
    ]
    count = await store_top_gainers(db, raw, min_change=20.0)
    assert count == 0


async def test_store_top_gainers_filters_high_mcap(db):
    raw = [
        _make_raw_coin("big-cap", change_24h=50.0, mcap=1_000_000_000),
    ]
    count = await store_top_gainers(db, raw, max_mcap=500_000_000)
    assert count == 0


async def test_store_top_gainers_limits_to_20(db):
    raw = [_make_raw_coin(f"coin-{i}", change_24h=30.0 + i) for i in range(25)]
    count = await store_top_gainers(db, raw, min_change=20.0)
    assert count == 20


async def test_compare_gainers_no_data(db):
    comparisons = await compare_gainers_with_signals(db)
    assert comparisons == []


async def test_compare_gainers_marks_gap(db):
    # Insert a gainer snapshot
    await db._conn.execute(
        """INSERT INTO gainers_snapshots
           (coin_id, symbol, name, price_change_24h, market_cap, volume_24h, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        ("gap-coin", "GAP", "Gap", 40.0, 50_000_000, 1_000_000),
    )
    await db._conn.commit()

    comparisons = await compare_gainers_with_signals(db)
    assert len(comparisons) == 1
    assert comparisons[0]["is_gap"] == 1
    assert comparisons[0]["detected_by_narrative"] == 0
    assert comparisons[0]["detected_by_pipeline"] == 0
    assert comparisons[0]["detected_by_chains"] == 0
    assert comparisons[0]["detected_by_spikes"] == 0


async def test_compare_gainers_detects_pipeline(db):
    # Insert a gainer snapshot
    await db._conn.execute(
        """INSERT INTO gainers_snapshots
           (coin_id, symbol, name, price_change_24h, market_cap, volume_24h, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        ("detected-coin", "DET", "Detected", 50.0, 50_000_000, 1_000_000),
    )
    # Insert a candidate that was seen before
    await db._conn.execute(
        """INSERT INTO candidates
           (contract_address, chain, token_name, ticker, first_seen_at)
           VALUES (?, ?, ?, ?, datetime('now', '-2 hours'))""",
        ("detected-coin", "coingecko", "Detected", "DET"),
    )
    await db._conn.commit()

    comparisons = await compare_gainers_with_signals(db)
    assert len(comparisons) == 1
    assert comparisons[0]["detected_by_pipeline"] == 1
    assert comparisons[0]["pipeline_lead_minutes"] is not None
    assert comparisons[0]["pipeline_lead_minutes"] > 0
    assert comparisons[0]["is_gap"] == 0


async def test_compare_gainers_detects_spikes(db):
    # Insert a gainer snapshot
    await db._conn.execute(
        """INSERT INTO gainers_snapshots
           (coin_id, symbol, name, price_change_24h, market_cap, volume_24h, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        ("spiked-coin", "SPK", "Spiked", 60.0, 50_000_000, 2_000_000),
    )
    # Insert a volume spike that was detected earlier
    await db._conn.execute(
        """INSERT INTO volume_spikes
           (coin_id, symbol, name, current_volume, avg_volume_7d,
            spike_ratio, market_cap, price, price_change_24h, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '-3 hours'))""",
        (
            "spiked-coin",
            "SPK",
            "Spiked",
            2_000_000,
            200_000,
            10.0,
            50_000_000,
            1.0,
            30.0,
        ),
    )
    await db._conn.commit()

    comparisons = await compare_gainers_with_signals(db)
    assert len(comparisons) == 1
    assert comparisons[0]["detected_by_spikes"] == 1
    assert comparisons[0]["spikes_lead_minutes"] is not None
    assert comparisons[0]["is_gap"] == 0


async def test_get_recent_gainers(db):
    await db._conn.execute(
        """INSERT INTO gainers_snapshots
           (coin_id, symbol, name, price_change_24h, market_cap, volume_24h, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        ("recent-g", "RCT", "Recent", 45.0, 50_000_000, 500_000),
    )
    await db._conn.commit()

    recent = await get_recent_gainers(db, limit=10)
    assert len(recent) == 1
    assert recent[0]["coin_id"] == "recent-g"


async def test_get_gainers_comparisons(db):
    await db._conn.execute(
        """INSERT INTO gainers_comparisons
           (coin_id, symbol, name, price_change_24h, appeared_on_gainers_at,
            detected_by_narrative, narrative_lead_minutes,
            detected_by_pipeline, pipeline_lead_minutes,
            detected_by_chains, chains_lead_minutes,
            detected_by_spikes, spikes_lead_minutes,
            is_gap)
           VALUES (?, ?, ?, ?, datetime('now'), 0, NULL, 1, 120.0, 0, NULL, 0, NULL, 0)""",
        ("comp-coin", "CMP", "Comp", 35.0),
    )
    await db._conn.commit()

    comps = await get_gainers_comparisons(db, limit=10)
    assert len(comps) == 1
    assert comps[0]["detected_by_pipeline"] == 1


async def test_get_gainers_stats(db):
    # Insert two comparisons: one caught, one gap
    for coin_id, is_gap in [("caught", 0), ("missed", 1)]:
        await db._conn.execute(
            """INSERT INTO gainers_comparisons
               (coin_id, symbol, name, price_change_24h, appeared_on_gainers_at,
                detected_by_pipeline, pipeline_lead_minutes, is_gap)
               VALUES (?, ?, ?, ?, datetime('now'), ?, ?, ?)""",
            (
                coin_id,
                coin_id[:3].upper(),
                coin_id.title(),
                30.0,
                1 if not is_gap else 0,
                60.0 if not is_gap else None,
                is_gap,
            ),
        )
    await db._conn.commit()

    stats = await get_gainers_stats(db)
    assert stats["total_tracked"] == 2
    assert stats["caught"] == 1
    assert stats["missed"] == 1
    assert stats["hit_rate_pct"] == 50.0


# -- isoformat-T (production format) regression + new-surface crediting --
#
# Production stores snapshot_at AND every detector's detected_at via Python
# datetime.isoformat() ("...T..+00:00"). The lead-time comparison normalizes
# both sides with datetime(); before that fix a bare `<` silently dropped
# same-day detections because 'T' (0x54) sorts after ' ' (0x20), so an
# isoformat-T detected_at compared greater than the space-format datetime()
# bound. The older datetime('now') (space-format) tests above never hit this.


def _iso(hours_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


async def _insert_gainer_iso(db, coin_id):
    await db._conn.execute(
        """INSERT INTO gainers_snapshots
           (coin_id, symbol, name, price_change_24h, market_cap, volume_24h,
            snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            coin_id,
            coin_id[:4].upper(),
            coin_id.title(),
            40.0,
            50_000_000,
            1_000_000,
            _iso(0.0),
        ),
    )


async def test_compare_credits_same_day_isoformat_spike(db):
    """Regression: a same-day spike in isoformat-T (prod format) detected 2h
    before the gainer must be credited (timestamp-normalization fix)."""
    await _insert_gainer_iso(db, "isofmt-coin")
    await db._conn.execute(
        """INSERT INTO volume_spikes
           (coin_id, symbol, name, current_volume, avg_volume_7d,
            spike_ratio, market_cap, price, price_change_24h, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("isofmt-coin", "ISOF", "Isofmt", 2e6, 2e5, 10.0, 5e7, 1.0, 30.0, _iso(2.0)),
    )
    await db._conn.commit()

    comps = await compare_gainers_with_signals(db)
    assert len(comps) == 1
    assert comps[0]["detected_by_spikes"] == 1
    assert comps[0]["spikes_lead_minutes"] == pytest.approx(120.0, abs=1.0)
    assert comps[0]["is_gap"] == 0


async def test_compare_credits_acceleration_surface(db):
    await _insert_gainer_iso(db, "accel-g")
    await db._conn.execute(
        """INSERT INTO gainer_acceleration
           (coin_id, symbol, name, change_1h, change_4h, vol_expansion,
            market_cap, current_price, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("accel-g", "ACCG", "Accel", 10.0, 20.0, 3.0, 5e7, 1.1, _iso(1.0)),
    )
    await db._conn.commit()

    comps = await compare_gainers_with_signals(db)
    assert comps[0]["detected_by_acceleration"] == 1
    assert comps[0]["acceleration_lead_minutes"] == pytest.approx(60.0, abs=1.0)
    assert comps[0]["is_gap"] == 0


async def test_compare_credits_momentum_surface(db):
    await _insert_gainer_iso(db, "mom-g")
    await db._conn.execute(
        """INSERT INTO momentum_7d
           (coin_id, symbol, name, price_change_7d, price_change_24h,
            market_cap, current_price, volume_24h, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("mom-g", "MOMG", "Mom", 50.0, 40.0, 5e7, 1.1, 1e6, _iso(3.0)),
    )
    await db._conn.commit()

    comps = await compare_gainers_with_signals(db)
    assert comps[0]["detected_by_momentum"] == 1
    assert comps[0]["is_gap"] == 0


async def test_compare_credits_slow_burn_surface(db):
    await _insert_gainer_iso(db, "sb-g")
    await db._conn.execute(
        """INSERT INTO slow_burn_candidates
           (coin_id, symbol, name, price_change_7d, price_change_1h,
            price_change_24h, market_cap, current_price, volume_24h,
            also_in_momentum_7d, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("sb-g", "SBG", "Slow", 45.0, 2.0, 30.0, 5e7, 1.1, 1e6, 0, _iso(4.0)),
    )
    await db._conn.commit()

    comps = await compare_gainers_with_signals(db)
    assert comps[0]["detected_by_slow_burn"] == 1
    assert comps[0]["is_gap"] == 0


async def test_compare_credits_velocity_surface(db):
    await _insert_gainer_iso(db, "vel-g")
    await db._conn.execute(
        """INSERT INTO velocity_alerts
           (coin_id, symbol, name, price_change_1h, price_change_24h,
            market_cap, volume_24h, vol_mcap_ratio, current_price, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("vel-g", "VELG", "Vel", 25.0, 40.0, 5e7, 1e6, 0.5, 1.1, _iso(0.5)),
    )
    await db._conn.commit()

    comps = await compare_gainers_with_signals(db)
    assert comps[0]["detected_by_velocity"] == 1
    assert comps[0]["is_gap"] == 0


async def test_stats_union_includes_new_surfaces(db):
    """Average lead must include the new surfaces' lead columns."""
    await db._conn.execute(
        """INSERT INTO gainers_comparisons
           (coin_id, symbol, name, price_change_24h, appeared_on_gainers_at,
            detected_by_acceleration, acceleration_lead_minutes, is_gap)
           VALUES (?, ?, ?, ?, datetime('now'), 1, 90.0, 0)""",
        ("accel-only", "AONL", "AccelOnly", 35.0),
    )
    await db._conn.commit()

    stats = await get_gainers_stats(db)
    assert stats["caught"] == 1
    assert stats["avg_lead_minutes"] == pytest.approx(90.0, abs=0.1)
