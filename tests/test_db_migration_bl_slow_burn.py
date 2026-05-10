"""BL-075 Phase B: schema migration tests for `bl_slow_burn_v1`.

Per R3 reviewer MUST-FIX coverage parity with BL-NEW-QUOTE-PAIR's migration
test pattern: orphan-detection / schema_version row content / idempotent
rerun / description-mismatch raises / composite index existence /
heartbeat counter presence.
"""

from __future__ import annotations

import pytest

from scout.db import Database


@pytest.mark.asyncio
async def test_bl_slow_burn_v1_columns_added(tmp_path):
    """Table + columns + nullable mcap exist post-initialize."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(slow_burn_candidates)")
    rows = await cur.fetchall()
    cols = {row[1]: row[2] for row in rows}  # name → type
    assert "coin_id" in cols
    assert "symbol" in cols
    assert "price_change_7d" in cols
    assert "price_change_1h" in cols
    assert "price_change_24h" in cols
    assert "market_cap" in cols
    assert "current_price" in cols
    assert "volume_24h" in cols
    assert "also_in_momentum_7d" in cols
    assert "detected_at" in cols
    # Verify market_cap is nullable (mcap-unknown cohort).
    notnull_idx = 3  # PRAGMA table_info column 3 = notnull flag
    market_cap_row = next(r for r in rows if r[1] == "market_cap")
    assert market_cap_row[notnull_idx] == 0, "market_cap must be nullable"
    await db.close()


@pytest.mark.asyncio
async def test_bl_slow_burn_v1_wired_into_apply_migrations(tmp_path):
    """R3: schema_version row written = migration is in _apply_migrations chain.

    Without this, an orphaned migration (defined but not wired) would silently
    succeed test_bl_slow_burn_v1_columns_added if some other migration
    incidentally created slow_burn_candidates.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT description FROM schema_version WHERE version=20260515"
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "bl_slow_burn_v1_slow_burn_candidates"
    await db.close()


@pytest.mark.asyncio
async def test_bl_slow_burn_v1_idempotent_rerun(tmp_path):
    """R3 MUST-FIX: every restart re-runs all migrations; must not raise."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._migrate_bl_slow_burn_v1()  # second call — must not raise
    cur = await db._conn.execute("PRAGMA table_info(slow_burn_candidates)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "also_in_momentum_7d" in cols
    await db.close()


@pytest.mark.asyncio
async def test_bl_slow_burn_v1_description_mismatch_raises(tmp_path):
    """R3 MUST-FIX: post-assertion catches version-collision case.

    Simulates pre-seeded schema_version row with wrong description that
    INSERT OR IGNORE would silently skip past.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        "UPDATE schema_version SET description = ? WHERE version = ?",
        ("some_other_migration_v999", 20260515),
    )
    await db._conn.commit()
    with pytest.raises(RuntimeError, match="description mismatch"):
        await db._migrate_bl_slow_burn_v1()
    await db.close()


@pytest.mark.asyncio
async def test_bl_slow_burn_v1_composite_index_exists(tmp_path):
    """R4 MUST-FIX regression-lock: composite index for dedup hot-path.

    Without idx_slow_burn_coin_date, the dedup query
    `WHERE coin_id = ? AND date(detected_at) >= ...` scans the full
    coin_id partition (degrades over the 14d soak as the table grows).
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='idx_slow_burn_coin_date'"
    )
    assert (
        await cur.fetchone()
    ) is not None, "composite (coin_id, detected_at) index missing"
    await db.close()


@pytest.mark.asyncio
async def test_bl_slow_burn_v1_detected_at_index_exists(tmp_path):
    """Time-range scan index for dashboard queries."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='idx_slow_burn_detected'"
    )
    assert (await cur.fetchone()) is not None
    await db.close()


def test_slow_burn_heartbeat_counter_present():
    """R4 MUST-FIX: live observability counter must be in heartbeat stats.

    Module-level check (no DB needed) — proves the heartbeat dict has
    the slot. Detector wiring is tested separately in
    tests/test_slow_burn_detector.py::test_slow_burn_increments_heartbeat_counter.
    """
    from scout.heartbeat import _heartbeat_stats

    assert "slow_burn_detected_today" in _heartbeat_stats
