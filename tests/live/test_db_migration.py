"""Tests for BL-055 live-trading schema migration (spec §3.1)."""

from __future__ import annotations

import aiosqlite
import pytest

from scout.db import Database


async def _tables(conn) -> set[str]:
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    return {row[0] for row in await cur.fetchall()}


async def _indexes(conn) -> set[str]:
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    )
    return {row[0] for row in await cur.fetchall()}


async def test_migration_creates_all_bl055_tables(tmp_path):
    db = Database(tmp_path / "gecko.db")
    await db.initialize()
    tables = await _tables(db._conn)
    assert {
        "shadow_trades",
        "live_trades",
        "kill_events",
        "live_control",
        "venue_overrides",
        "resolver_cache",
        "live_metrics_daily",
    } <= tables, f"missing BL-055 tables; got {tables}"
    await db.close()


async def test_migration_creates_bl055_indexes(tmp_path):
    db = Database(tmp_path / "gecko.db")
    await db.initialize()
    idx = await _indexes(db._conn)
    assert "idx_shadow_status_evaluated" in idx
    assert "idx_shadow_closed_at_utc" in idx
    assert "idx_kill_events_active" in idx
    await db.close()


async def test_live_control_has_singleton_row(tmp_path):
    db = Database(tmp_path / "gecko.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT id, active_kill_event_id FROM live_control"
    )
    rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 1
    assert rows[0][1] is None
    await db.close()


async def test_live_control_rejects_non_one_id(tmp_path):
    db = Database(tmp_path / "gecko.db")
    await db.initialize()
    with pytest.raises(aiosqlite.IntegrityError):
        await db._conn.execute("INSERT INTO live_control (id) VALUES (2)")
        await db._conn.commit()
    await db.close()


async def test_shadow_trades_check_constraints(tmp_path):
    db = Database(tmp_path / "gecko.db")
    await db.initialize()
    with pytest.raises(aiosqlite.IntegrityError):
        await db._conn.execute(
            "INSERT INTO shadow_trades "
            "(paper_trade_id, coin_id, symbol, venue, pair, signal_type, "
            " size_usd, status, created_at) "
            "VALUES (1, 'c', 's', 'binance', 'SUSDT', 'first_signal', "
            "'100', 'BAD_STATUS', '2026-04-23T00:00:00Z')"
        )
        await db._conn.commit()
    await db.close()


async def test_paper_trades_fk_restrict(tmp_path):
    """Spec §3.2: paper_trades is append-only via FK ON DELETE RESTRICT.
    Attempt to delete a paper_trades row while a shadow_trades row references
    it must fail."""
    db = Database(tmp_path / "gecko.db")
    await db.initialize()
    # Seed a paper_trades row and a shadow_trades row pointing to it.
    await db._conn.execute(
        "INSERT INTO paper_trades "
        "(token_id, symbol, name, chain, signal_type, signal_data, "
        " entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price, "
        " status, opened_at) "
        "VALUES ('c','S','N','eth','first_signal','{}',1,100,100,40,20,1.4,0.8,"
        "'open','2026-04-23T00:00:00Z')"
    )
    paper_id = (await (await db._conn.execute("SELECT last_insert_rowid()")).fetchone())[0]
    await db._conn.execute(
        "INSERT INTO shadow_trades "
        "(paper_trade_id, coin_id, symbol, venue, pair, signal_type, size_usd, "
        " status, created_at) "
        "VALUES (?, 'c','S','binance','SUSDT','first_signal','100','open', "
        "'2026-04-23T00:00:00Z')",
        (paper_id,),
    )
    await db._conn.commit()
    with pytest.raises(aiosqlite.IntegrityError):
        await db._conn.execute(
            "DELETE FROM paper_trades WHERE id = ?", (paper_id,)
        )
        await db._conn.commit()
    await db.close()


async def test_initialize_upgrades_pre_bl055_db(tmp_path):
    """Regression test — spec §3.1 upgrade path.

    Simulates an existing production DB that has everything up through BL-060
    (paper_trades with would_be_live column) but NONE of the 7 BL-055 tables.
    Database.initialize() on that DB must add every BL-055 table + index + seed
    live_control without touching the pre-existing paper_trades rows. Mirrors
    the failure mode from BL-060 (feedback_ddl_before_alter.md): index-in-
    _create_tables silently skipped on upgrade because CREATE TABLE IF NOT EXISTS
    is a no-op for already-present tables.
    """
    db_path = tmp_path / "preexisting.db"
    # 1. Build a pre-BL-055 schema directly via raw aiosqlite.
    raw = await aiosqlite.connect(db_path)
    await raw.execute("PRAGMA foreign_keys=ON")
    # Minimal pre-BL-055 paper_trades (include all prior columns + BL-060's
    # would_be_live). Use whatever CREATE TABLE matches current master BEFORE
    # the BL-055 migration runs — the point is no shadow_trades/live_trades/etc.
    await raw.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_id TEXT, symbol TEXT, name TEXT, chain TEXT,
            signal_type TEXT, signal_data TEXT,
            entry_price REAL, amount_usd REAL, quantity REAL,
            tp_pct REAL, sl_pct REAL, tp_price REAL, sl_price REAL,
            status TEXT, opened_at TEXT,
            would_be_live INTEGER   -- BL-060 column
        )
    """)
    await raw.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT,
            description TEXT
        )
    """)
    # Seed a paper_trades row so the FK RESTRICT path has data to guard.
    await raw.execute(
        "INSERT INTO paper_trades "
        "(token_id, symbol, name, chain, signal_type, signal_data, "
        " entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, "
        " sl_price, status, opened_at, would_be_live) "
        "VALUES ('c','S','N','eth','first_signal','{}',1,100,100,40,20,"
        "1.4,0.8,'open','2026-04-22T00:00:00Z',1)"
    )
    await raw.commit()
    await raw.close()

    # 2. Now run Database.initialize() against that pre-existing DB.
    db = Database(db_path)
    await db.initialize()

    # 3. All 7 new tables exist.
    tables = await _tables(db._conn)
    assert {
        "shadow_trades", "live_trades", "kill_events", "live_control",
        "venue_overrides", "resolver_cache", "live_metrics_daily",
    } <= tables

    # 4. live_control seed row exists with id=1.
    cur = await db._conn.execute(
        "SELECT id, active_kill_event_id FROM live_control"
    )
    rows = await cur.fetchall()
    assert len(rows) == 1 and rows[0][0] == 1 and rows[0][1] is None

    # 5. Indexes per spec §3.1 exist.
    idx = await _indexes(db._conn)
    assert "idx_shadow_status_evaluated" in idx
    assert "idx_shadow_closed_at_utc" in idx
    assert "idx_kill_events_active" in idx

    # Sanity: the pre-existing paper_trades row is intact.
    cur = await db._conn.execute("SELECT COUNT(*) FROM paper_trades")
    assert (await cur.fetchone())[0] == 1
    await db.close()
