"""Tests for scout.spikes.detector -- volume spike detection."""

import pytest

from scout.db import Database
from scout.spikes.detector import (
    detect_7d_momentum,
    detect_spikes,
    get_momentum_7d_stats,
    get_recent_momentum_7d,
    get_recent_spikes,
    get_spike_stats,
    record_volume,
)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


# -- Helpers --

def _make_raw_coin(coin_id: str, volume: float, mcap: float = 100_000_000, price: float = 1.0):
    return {
        "id": coin_id,
        "symbol": coin_id[:3],
        "name": coin_id.title(),
        "total_volume": volume,
        "market_cap": mcap,
        "current_price": price,
    }


# -- Tests --

async def test_record_volume_inserts_rows(db):
    raw = [_make_raw_coin("alpha", 1_000_000), _make_raw_coin("beta", 2_000_000)]
    count = await record_volume(db, raw)
    assert count == 2

    cursor = await db._conn.execute("SELECT COUNT(*) FROM volume_history_cg")
    row = await cursor.fetchone()
    assert row[0] == 2


async def test_record_volume_skips_zero_volume(db):
    raw = [_make_raw_coin("zero", 0)]
    count = await record_volume(db, raw)
    assert count == 0


async def test_record_volume_skips_missing_id(db):
    raw = [{"symbol": "x", "total_volume": 100}]
    count = await record_volume(db, raw)
    assert count == 0


async def test_detect_spikes_no_data(db):
    spikes = await detect_spikes(db)
    assert spikes == []


async def test_detect_spikes_finds_spike(db):
    # Insert 7 days of low volume, then one high volume
    for i in range(7):
        await db._conn.execute(
            """INSERT INTO volume_history_cg
               (coin_id, symbol, name, volume_24h, market_cap, price, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now', ?))""",
            ("spike-coin", "SPK", "Spike", 100_000, 50_000_000, 0.5, f"-{i+1} days"),
        )
    # Latest entry with 10x volume
    await db._conn.execute(
        """INSERT INTO volume_history_cg
           (coin_id, symbol, name, volume_24h, market_cap, price, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        ("spike-coin", "SPK", "Spike", 1_000_000, 50_000_000, 0.5),
    )
    await db._conn.commit()

    spikes = await detect_spikes(db, min_spike_ratio=5.0, max_mcap=500_000_000)
    assert len(spikes) == 1
    assert spikes[0].coin_id == "spike-coin"
    assert spikes[0].spike_ratio >= 5.0


async def test_detect_spikes_respects_max_mcap(db):
    # Insert data with mcap > max_mcap
    for i in range(5):
        await db._conn.execute(
            """INSERT INTO volume_history_cg
               (coin_id, symbol, name, volume_24h, market_cap, price, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now', ?))""",
            ("big-cap", "BIG", "BigCap", 100_000, 1_000_000_000, 10.0, f"-{i+1} days"),
        )
    await db._conn.execute(
        """INSERT INTO volume_history_cg
           (coin_id, symbol, name, volume_24h, market_cap, price, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        ("big-cap", "BIG", "BigCap", 1_000_000, 1_000_000_000, 10.0),
    )
    await db._conn.commit()

    spikes = await detect_spikes(db, min_spike_ratio=5.0, max_mcap=500_000_000)
    assert len(spikes) == 0


async def test_detect_spikes_dedup_same_day(db):
    # Insert spike-worthy data
    for i in range(5):
        await db._conn.execute(
            """INSERT INTO volume_history_cg
               (coin_id, symbol, name, volume_24h, market_cap, price, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now', ?))""",
            ("dedup-coin", "DDP", "Dedup", 100_000, 50_000_000, 1.0, f"-{i+1} days"),
        )
    await db._conn.execute(
        """INSERT INTO volume_history_cg
           (coin_id, symbol, name, volume_24h, market_cap, price, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        ("dedup-coin", "DDP", "Dedup", 1_000_000, 50_000_000, 1.0),
    )
    await db._conn.commit()

    # First call detects the spike
    spikes1 = await detect_spikes(db)
    assert len(spikes1) == 1

    # Second call on same day should not duplicate
    spikes2 = await detect_spikes(db)
    assert len(spikes2) == 0


async def test_get_recent_spikes(db):
    await db._conn.execute(
        """INSERT INTO volume_spikes
           (coin_id, symbol, name, current_volume, avg_volume_7d,
            spike_ratio, market_cap, price, price_change_24h, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        ("test-coin", "TST", "Test", 500_000, 50_000, 10.0, 10_000_000, 0.1, 25.0),
    )
    await db._conn.commit()

    recent = await get_recent_spikes(db, limit=10)
    assert len(recent) == 1
    assert recent[0]["coin_id"] == "test-coin"
    assert recent[0]["spike_ratio"] == 10.0


async def test_get_spike_stats(db):
    # Insert a spike
    await db._conn.execute(
        """INSERT INTO volume_spikes
           (coin_id, symbol, name, current_volume, avg_volume_7d,
            spike_ratio, market_cap, price, price_change_24h, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        ("stats-coin", "STS", "Stats", 500_000, 100_000, 5.0, 10_000_000, 0.1, 10.0),
    )
    await db._conn.commit()

    stats = await get_spike_stats(db)
    assert stats["spikes_today"] == 1
    assert stats["spikes_this_week"] == 1
    assert stats["avg_spike_ratio"] == 5.0


async def test_record_volume_prunes_old_data(db):
    # Insert a record older than 7 days
    await db._conn.execute(
        """INSERT INTO volume_history_cg
           (coin_id, symbol, name, volume_24h, market_cap, price, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now', '-10 days'))""",
        ("old-coin", "OLD", "Old", 100_000, 50_000_000, 1.0),
    )
    await db._conn.commit()

    # Record new volume (triggers prune)
    await record_volume(db, [_make_raw_coin("new-coin", 200_000)])

    cursor = await db._conn.execute(
        "SELECT COUNT(*) FROM volume_history_cg WHERE coin_id = 'old-coin'"
    )
    row = await cursor.fetchone()
    assert row[0] == 0


# -- 7-Day Momentum Scanner Tests --


def _make_7d_coin(
    coin_id: str,
    change_7d: float,
    mcap: float = 100_000_000,
    change_24h: float = 5.0,
    volume: float = 1_000_000,
    price: float = 1.0,
):
    return {
        "id": coin_id,
        "symbol": coin_id[:3],
        "name": coin_id.title(),
        "price_change_percentage_7d_in_currency": change_7d,
        "price_change_percentage_24h": change_24h,
        "market_cap": mcap,
        "current_price": price,
        "total_volume": volume,
    }


async def test_detect_7d_momentum_finds_runner(db):
    raw = [_make_7d_coin("pandora", 438.0, mcap=200_000_000)]
    results = await detect_7d_momentum(db, raw, min_7d_change=100.0, max_mcap=500_000_000)
    assert len(results) == 1
    assert results[0]["coin_id"] == "pandora"
    assert results[0]["price_change_7d"] == 438.0
    assert results[0]["symbol"] == "PAN"


async def test_detect_7d_momentum_skips_below_threshold(db):
    raw = [_make_7d_coin("slow-mover", 50.0)]
    results = await detect_7d_momentum(db, raw, min_7d_change=100.0)
    assert len(results) == 0


async def test_detect_7d_momentum_skips_mega_cap(db):
    raw = [_make_7d_coin("bitcoin", 150.0, mcap=1_000_000_000)]
    results = await detect_7d_momentum(db, raw, min_7d_change=100.0, max_mcap=500_000_000)
    assert len(results) == 0


async def test_detect_7d_momentum_skips_zero_mcap(db):
    raw = [_make_7d_coin("no-cap", 200.0, mcap=0)]
    results = await detect_7d_momentum(db, raw, min_7d_change=100.0)
    assert len(results) == 0


async def test_detect_7d_momentum_dedup_same_day(db):
    raw = [_make_7d_coin("pandora", 438.0)]
    results1 = await detect_7d_momentum(db, raw, min_7d_change=100.0)
    assert len(results1) == 1

    # Second call same day should not duplicate
    results2 = await detect_7d_momentum(db, raw, min_7d_change=100.0)
    assert len(results2) == 0


async def test_detect_7d_momentum_persists_to_db(db):
    raw = [_make_7d_coin("pandora", 438.0)]
    await detect_7d_momentum(db, raw, min_7d_change=100.0)

    cursor = await db._conn.execute("SELECT COUNT(*) FROM momentum_7d")
    row = await cursor.fetchone()
    assert row[0] == 1


async def test_get_recent_momentum_7d(db):
    await db._conn.execute(
        """INSERT INTO momentum_7d
           (coin_id, symbol, name, price_change_7d, price_change_24h,
            market_cap, current_price, volume_24h, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        ("pandora", "PANDORA", "Pandora", 438.0, 25.0, 200_000_000, 30.0, 5_000_000),
    )
    await db._conn.commit()

    recent = await get_recent_momentum_7d(db, limit=10)
    assert len(recent) == 1
    assert recent[0]["coin_id"] == "pandora"
    assert recent[0]["price_change_7d"] == 438.0


async def test_get_momentum_7d_stats(db):
    await db._conn.execute(
        """INSERT INTO momentum_7d
           (coin_id, symbol, name, price_change_7d, price_change_24h,
            market_cap, current_price, volume_24h, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        ("pandora", "PANDORA", "Pandora", 438.0, 25.0, 200_000_000, 30.0, 5_000_000),
    )
    await db._conn.commit()

    stats = await get_momentum_7d_stats(db)
    assert stats["detections_today"] == 1
    assert stats["detections_this_week"] == 1
    assert stats["avg_7d_change"] == 438.0


async def test_detect_7d_momentum_skips_missing_id(db):
    raw = [{"symbol": "x", "price_change_percentage_7d_in_currency": 200.0, "market_cap": 100_000}]
    results = await detect_7d_momentum(db, raw, min_7d_change=100.0)
    assert len(results) == 0
