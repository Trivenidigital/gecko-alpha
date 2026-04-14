"""Tests for scout.gainers.tracker -- top gainers tracking and comparison."""

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
        ("spiked-coin", "SPK", "Spiked", 2_000_000, 200_000, 10.0, 50_000_000, 1.0, 30.0),
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
            (coin_id, coin_id[:3].upper(), coin_id.title(), 30.0,
             1 if not is_gap else 0,
             60.0 if not is_gap else None,
             is_gap),
        )
    await db._conn.commit()

    stats = await get_gainers_stats(db)
    assert stats["total_tracked"] == 2
    assert stats["caught"] == 1
    assert stats["missed"] == 1
    assert stats["hit_rate_pct"] == 50.0
