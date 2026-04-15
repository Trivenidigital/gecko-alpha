"""Tests for scout.losers.tracker -- top losers tracking and comparison."""

import pytest

from scout.db import Database
from scout.losers.tracker import (
    compare_losers_with_signals,
    get_losers_comparisons,
    get_losers_stats,
    get_recent_losers,
    store_top_losers,
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
    change_24h: float = -20.0,
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

async def test_store_top_losers_basic(db):
    raw = [
        _make_raw_coin("loser-a", change_24h=-25.0),
        _make_raw_coin("loser-b", change_24h=-18.0),
    ]
    count = await store_top_losers(db, raw, max_drop=-15.0)
    assert count == 2

    cursor = await db._conn.execute("SELECT COUNT(*) FROM losers_snapshots")
    row = await cursor.fetchone()
    assert row[0] == 2


async def test_store_top_losers_filters_small_drop(db):
    raw = [
        _make_raw_coin("small-drop", change_24h=-5.0),
    ]
    count = await store_top_losers(db, raw, max_drop=-15.0)
    assert count == 0


async def test_store_top_losers_filters_high_mcap(db):
    raw = [
        _make_raw_coin("big-cap", change_24h=-30.0, mcap=1_000_000_000),
    ]
    count = await store_top_losers(db, raw, max_mcap=500_000_000)
    assert count == 0


async def test_store_top_losers_limits_to_20(db):
    raw = [_make_raw_coin(f"coin-{i}", change_24h=-20.0 - i) for i in range(25)]
    count = await store_top_losers(db, raw, max_drop=-15.0)
    assert count == 20


async def test_compare_losers_no_data(db):
    comparisons = await compare_losers_with_signals(db)
    assert comparisons == []


async def test_compare_losers_marks_gap(db):
    # Insert a loser snapshot
    await db._conn.execute(
        """INSERT INTO losers_snapshots
           (coin_id, symbol, name, price_change_24h, market_cap, volume_24h, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        ("gap-coin", "GAP", "Gap", -25.0, 50_000_000, 1_000_000),
    )
    await db._conn.commit()

    comparisons = await compare_losers_with_signals(db)
    assert len(comparisons) == 1
    assert comparisons[0]["is_gap"] == 1
    assert comparisons[0]["detected_by_narrative"] == 0
    assert comparisons[0]["detected_by_pipeline"] == 0
    assert comparisons[0]["detected_by_chains"] == 0
    assert comparisons[0]["detected_by_spikes"] == 0


async def test_compare_losers_detects_pipeline(db):
    # Insert a loser snapshot
    await db._conn.execute(
        """INSERT INTO losers_snapshots
           (coin_id, symbol, name, price_change_24h, market_cap, volume_24h, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        ("detected-coin", "DET", "Detected", -30.0, 50_000_000, 1_000_000),
    )
    # Insert a candidate that was seen before
    await db._conn.execute(
        """INSERT INTO candidates
           (contract_address, chain, token_name, ticker, first_seen_at)
           VALUES (?, ?, ?, ?, datetime('now', '-2 hours'))""",
        ("detected-coin", "coingecko", "Detected", "DET"),
    )
    await db._conn.commit()

    comparisons = await compare_losers_with_signals(db)
    assert len(comparisons) == 1
    assert comparisons[0]["detected_by_pipeline"] == 1
    assert comparisons[0]["pipeline_lead_minutes"] is not None
    assert comparisons[0]["pipeline_lead_minutes"] > 0
    assert comparisons[0]["is_gap"] == 0


async def test_compare_losers_detects_spikes(db):
    # Insert a loser snapshot
    await db._conn.execute(
        """INSERT INTO losers_snapshots
           (coin_id, symbol, name, price_change_24h, market_cap, volume_24h, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        ("spiked-coin", "SPK", "Spiked", -40.0, 50_000_000, 2_000_000),
    )
    # Insert a volume spike that was detected earlier
    await db._conn.execute(
        """INSERT INTO volume_spikes
           (coin_id, symbol, name, current_volume, avg_volume_7d,
            spike_ratio, market_cap, price, price_change_24h, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '-3 hours'))""",
        ("spiked-coin", "SPK", "Spiked", 2_000_000, 200_000, 10.0, 50_000_000, 1.0, -20.0),
    )
    await db._conn.commit()

    comparisons = await compare_losers_with_signals(db)
    assert len(comparisons) == 1
    assert comparisons[0]["detected_by_spikes"] == 1
    assert comparisons[0]["spikes_lead_minutes"] is not None
    assert comparisons[0]["is_gap"] == 0


async def test_get_recent_losers(db):
    await db._conn.execute(
        """INSERT INTO losers_snapshots
           (coin_id, symbol, name, price_change_24h, market_cap, volume_24h, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        ("recent-l", "RCT", "Recent", -22.0, 50_000_000, 500_000),
    )
    await db._conn.commit()

    recent = await get_recent_losers(db, limit=10)
    assert len(recent) == 1
    assert recent[0]["coin_id"] == "recent-l"


async def test_get_losers_comparisons(db):
    await db._conn.execute(
        """INSERT INTO losers_comparisons
           (coin_id, symbol, name, price_change_24h, appeared_on_losers_at,
            detected_by_narrative, narrative_lead_minutes,
            detected_by_pipeline, pipeline_lead_minutes,
            detected_by_chains, chains_lead_minutes,
            detected_by_spikes, spikes_lead_minutes,
            is_gap)
           VALUES (?, ?, ?, ?, datetime('now'), 0, NULL, 1, 120.0, 0, NULL, 0, NULL, 0)""",
        ("comp-coin", "CMP", "Comp", -28.0),
    )
    await db._conn.commit()

    comps = await get_losers_comparisons(db, limit=10)
    assert len(comps) == 1
    assert comps[0]["detected_by_pipeline"] == 1


async def test_get_losers_stats(db):
    # Insert two comparisons: one caught, one gap
    for coin_id, is_gap in [("caught", 0), ("missed", 1)]:
        await db._conn.execute(
            """INSERT INTO losers_comparisons
               (coin_id, symbol, name, price_change_24h, appeared_on_losers_at,
                detected_by_pipeline, pipeline_lead_minutes, is_gap)
               VALUES (?, ?, ?, ?, datetime('now'), ?, ?, ?)""",
            (coin_id, coin_id[:3].upper(), coin_id.title(), -25.0,
             1 if not is_gap else 0,
             60.0 if not is_gap else None,
             is_gap),
        )
    await db._conn.commit()

    stats = await get_losers_stats(db)
    assert stats["total_tracked"] == 2
    assert stats["caught"] == 1
    assert stats["missed"] == 1
    assert stats["hit_rate_pct"] == 50.0
