"""Tests for BL-NEW-DASHBOARD-X-ALERTS-RESOLVER-INDEX migration.

Verifies that the functional indexes on UPPER(symbol) exist after
Database.initialize() and that the x_alerts symbol-resolver query
shape uses them (EXPLAIN QUERY PLAN should report SEARCH not SCAN).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scout.db import Database


@pytest.fixture
async def db(tmp_path: Path):
    db_path = tmp_path / "test.db"
    d = Database(db_path)
    await d.initialize()
    yield d, str(db_path)
    await d.close()


async def test_volume_history_cg_symbol_upper_index_exists(db):
    """Migration must create idx_vol_hist_cg_symbol_upper."""
    _d, db_path = db
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='volume_history_cg'"
        ).fetchall()
    finally:
        conn.close()
    names = {r[0] for r in rows}
    assert "idx_vol_hist_cg_symbol_upper" in names, (
        f"functional index missing; found: {names}"
    )


async def test_gainers_snapshots_symbol_upper_index_exists(db):
    _d, db_path = db
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='gainers_snapshots'"
        ).fetchall()
    finally:
        conn.close()
    names = {r[0] for r in rows}
    assert "idx_gainers_snap_symbol_upper" in names


async def test_volume_history_cg_resolver_query_uses_index(db):
    """Resolver query shape against volume_history_cg must SEARCH via the
    new functional index, not SCAN the 2.5M-row table.

    SQLite's query planner is cost-based — on an EMPTY table it'll
    prefer SCAN because scanning 0 rows is cheap. Seed a few rows + ANALYZE
    so the planner has statistics that reflect the prod scale where the
    index lookup is the right choice.
    """
    _d, db_path = db
    conn = sqlite3.connect(db_path)
    try:
        # Seed enough rows that planner prefers index over scan.
        for i in range(100):
            conn.execute(
                "INSERT INTO volume_history_cg "
                "(coin_id, symbol, name, volume_24h, recorded_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"coin{i}", f"SYM{i}", f"name{i}", 1.0, "2026-05-21T00:00:00"),
            )
        conn.commit()
        conn.execute("ANALYZE volume_history_cg")
        conn.commit()
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT DISTINCT coin_id FROM volume_history_cg "
            "WHERE UPPER(symbol) = 'BTC' "
            "AND COALESCE(coin_id, '') != '' "
            "ORDER BY recorded_at DESC LIMIT 25"
        ).fetchall()
    finally:
        conn.close()
    plan_text = " ".join(str(r) for r in plan)
    assert "SEARCH" in plan_text or "USING INDEX" in plan_text, (
        f"resolver query falls back to SCAN — index not used. plan={plan_text}"
    )
    # And it should reference our specific index.
    assert "idx_vol_hist_cg_symbol_upper" in plan_text, plan_text


async def test_migration_is_idempotent(tmp_path):
    """Re-initializing the same DB must not re-run the migration."""
    db_path = tmp_path / "idem.db"

    d1 = Database(db_path)
    await d1.initialize()
    await d1.close()

    # Second initialize: should be no-op (paper_migrations row exists).
    d2 = Database(db_path)
    await d2.initialize()
    await d2.close()

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM paper_migrations "
            "WHERE name IN ('vol_hist_cg_symbol_upper_idx_v1', "
            "'gainers_snap_symbol_upper_idx_v1')"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 2, f"expected both migration rows, got {rows}"
