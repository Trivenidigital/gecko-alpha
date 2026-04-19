"""Tests for the 5 narrative rotation database tables."""

import pytest
import aiosqlite

from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.db")
    await database.initialize()
    yield database
    await database.close()


NARRATIVE_TABLES = [
    "category_snapshots",
    "narrative_signals",
    "predictions",
    "agent_strategy",
    "learn_logs",
]


async def test_narrative_tables_created(db: Database):
    """All 5 narrative tables exist after initialize()."""
    cursor = await db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    rows = await cursor.fetchall()
    table_names = {row[0] for row in rows}
    for table in NARRATIVE_TABLES:
        assert table in table_names, f"Missing table: {table}"


async def test_insert_category_snapshot(db: Database):
    """Insert a category_snapshot and read it back."""
    await db._conn.execute(
        """INSERT INTO category_snapshots
           (category_id, name, market_cap, market_cap_change_24h,
            volume_24h, coin_count, market_regime, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("defi", "DeFi", 1e9, 5.2, 2e8, 120, "bull", "2026-04-09T00:00:00Z"),
    )
    await db._conn.commit()

    cursor = await db._conn.execute(
        "SELECT category_id, name, market_cap FROM category_snapshots WHERE category_id = ?",
        ("defi",),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "defi"
    assert row[1] == "DeFi"
    assert row[2] == 1e9


async def test_insert_prediction_unique_constraint(db: Database):
    """Inserting duplicate (category_id, coin_id, predicted_at) raises IntegrityError."""
    params = (
        "defi",
        "DeFi",
        "bitcoin",
        "BTC",
        "Bitcoin",
        50000.0,
        50000.0,
        80,
        "high",
        "high",
        "reason",
        "bull",
        1,
        0,
        0,
        "{}",
        None,
        "2026-04-09T00:00:00Z",
    )
    sql = """INSERT INTO predictions
             (category_id, category_name, coin_id, symbol, name,
              market_cap_at_prediction, price_at_prediction,
              narrative_fit_score, staying_power, confidence, reasoning,
              market_regime, trigger_count, is_control, is_holdout,
              strategy_snapshot, strategy_snapshot_ab, predicted_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    await db._conn.execute(sql, params)
    await db._conn.commit()

    with pytest.raises(aiosqlite.IntegrityError):
        await db._conn.execute(sql, params)
        await db._conn.commit()


async def test_insert_agent_strategy(db: Database):
    """Insert a strategy row and verify locked=0 default."""
    await db._conn.execute(
        """INSERT INTO agent_strategy (key, value, updated_at, updated_by)
           VALUES (?, ?, ?, ?)""",
        ("hit_threshold_pct", "15.0", "2026-04-09T00:00:00Z", "init"),
    )
    await db._conn.commit()

    cursor = await db._conn.execute(
        "SELECT key, value, locked FROM agent_strategy WHERE key = ?",
        ("hit_threshold_pct",),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "hit_threshold_pct"
    assert row[1] == "15.0"
    assert row[2] == 0  # default locked=0
