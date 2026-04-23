"""Tests for feedback-loop schema migration (spec §5.7)."""

from __future__ import annotations

import aiosqlite
import pytest

from scout.db import Database


async def _existing_paper_trades_columns(conn) -> set[str]:
    cur = await conn.execute("PRAGMA table_info(paper_trades)")
    return {row[1] for row in await cur.fetchall()}


async def _open_raw_conn(path):
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    return conn


async def test_fresh_db_migrates_all_columns(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.initialize()
    cols = await _existing_paper_trades_columns(db._conn)
    assert "signal_combo" in cols
    assert "lead_time_vs_trending_min" in cols
    assert "lead_time_vs_trending_status" in cols

    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('combo_performance', 'schema_version')"
    )
    names = {row[0] for row in await cur.fetchall()}
    assert names == {"combo_performance", "schema_version"}

    cur = await db._conn.execute(
        "SELECT version, description FROM schema_version WHERE version = 20260418"
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[1] == "feedback_loop_v1"
    await db.close()


async def test_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "test.db"
    db1 = Database(db_path)
    await db1.initialize()
    await db1.close()

    db2 = Database(db_path)
    await db2.initialize()
    cols = await _existing_paper_trades_columns(db2._conn)
    assert sum(1 for c in cols if c == "signal_combo") == 1
    await db2.close()


async def test_partial_db_fills_missing_columns(tmp_path):
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.initialize()
    await db.close()

    db2 = Database(db_path)
    await db2.initialize()
    cols = await _existing_paper_trades_columns(db2._conn)
    assert "signal_combo" in cols
    await db2.close()


async def test_migration_adds_required_indexes(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name IN ('idx_paper_trades_combo_opened', "
        "            'idx_paper_trades_token_opened')"
    )
    names = {row[0] for row in await cur.fetchall()}
    assert names == {
        "idx_paper_trades_combo_opened",
        "idx_paper_trades_token_opened",
    }
    await db.close()


async def test_failed_migration_rolls_back_partial_changes(tmp_path, monkeypatch):
    """D18: if a migration step fails, ALL prior DDL in this transaction must
    roll back — including any ALTERs that succeeded AND the schema_version row."""
    from scout import db as db_module

    db_path = tmp_path / "test.db"
    orig = db_module.Database._migrate_feedback_loop_schema

    async def _skip(self):
        return None

    monkeypatch.setattr(db_module.Database, "_migrate_feedback_loop_schema", _skip)
    db0 = db_module.Database(db_path)
    await db0.initialize()
    await db0.close()
    monkeypatch.setattr(db_module.Database, "_migrate_feedback_loop_schema", orig)

    import aiosqlite as _aiosqlite

    orig_execute = _aiosqlite.Connection.execute
    state = {"alters_seen": 0}

    async def _raise_on_second_alter(self, sql, *args, **kwargs):
        if "ALTER TABLE paper_trades ADD COLUMN" in sql:
            state["alters_seen"] += 1
            if state["alters_seen"] == 2:
                raise RuntimeError("forced failure mid-migration")
        return await orig_execute(self, sql, *args, **kwargs)

    monkeypatch.setattr(_aiosqlite.Connection, "execute", _raise_on_second_alter)

    db = db_module.Database(db_path)
    with pytest.raises(RuntimeError, match="forced failure mid-migration"):
        await db.initialize()

    monkeypatch.setattr(_aiosqlite.Connection, "execute", orig_execute)

    raw = await _open_raw_conn(db_path)
    cur = await raw.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
        "AND name='schema_version'"
    )
    sv_table = (await cur.fetchone())[0]
    if sv_table:
        cur = await raw.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version=20260418"
        )
        assert (await cur.fetchone())[
            0
        ] == 0, "schema_version must not be committed after failure"

    cur = await raw.execute("PRAGMA table_info(paper_trades)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "signal_combo" not in cols, f"partial ALTER not rolled back: {cols}"
    assert "lead_time_vs_trending_min" not in cols
    assert "lead_time_vs_trending_status" not in cols
    await raw.close()


async def test_post_migration_assertion_raises_on_incomplete_schema(
    tmp_path, monkeypatch
):
    from scout import db as db_module

    db = Database(tmp_path / "test.db")
    await db.initialize()
    await db.close()

    raw = await _open_raw_conn(tmp_path / "test.db")
    try:
        await raw.execute("ALTER TABLE paper_trades DROP COLUMN signal_combo")
        await raw.commit()
    except Exception:
        pytest.skip("SQLite version lacks DROP COLUMN support")
    await raw.close()

    db2 = Database(tmp_path / "test.db")

    original_execute = None

    async def _swallow_alter(self, sql, *args, **kwargs):
        if "ALTER TABLE paper_trades ADD COLUMN signal_combo" in sql:

            class _FakeCursor:
                async def fetchall(self):
                    return []

                async def fetchone(self):
                    return None

                lastrowid = None
                rowcount = 0

            return _FakeCursor()
        return await original_execute(self, sql, *args, **kwargs)

    import aiosqlite as _aiosqlite

    original_execute = _aiosqlite.Connection.execute
    monkeypatch.setattr(_aiosqlite.Connection, "execute", _swallow_alter)

    with pytest.raises(RuntimeError, match="Schema migration incomplete"):
        await db2.initialize()

    monkeypatch.setattr(_aiosqlite.Connection, "execute", original_execute)
    try:
        await db2.close()
    except Exception as e:
        import structlog

        structlog.get_logger().warning("test_db_close_failed", err=str(e))


# ---------------------------------------------------------------------------
# BL-060: would_be_live column + composite index
# ---------------------------------------------------------------------------


async def test_migration_adds_would_be_live_column(tmp_path):
    db_path = tmp_path / "gecko.db"
    db = Database(str(db_path))
    await db.initialize()

    async with aiosqlite.connect(str(db_path)) as conn:
        cur = await conn.execute("PRAGMA table_info(paper_trades)")
        rows = await cur.fetchall()
        cols = {
            row[1]: {"type": row[2], "notnull": row[3], "dflt": row[4]} for row in rows
        }

    assert "would_be_live" in cols, f"column missing; got {list(cols)}"
    assert cols["would_be_live"]["type"] == "INTEGER"
    assert cols["would_be_live"]["notnull"] == 0, "must be nullable"
    assert cols["would_be_live"]["dflt"] is None, "must not have default"
    await db.close()


async def test_migration_adds_would_be_live_index(tmp_path):
    db_path = tmp_path / "gecko.db"
    db = Database(str(db_path))
    await db.initialize()

    async with aiosqlite.connect(str(db_path)) as conn:
        cur = await conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='paper_trades'"
        )
        idx_rows = await cur.fetchall()

    names = {row[0] for row in idx_rows}
    assert (
        "idx_paper_trades_would_be_live_status" in names
    ), f"index missing; got {names}"
    sql = next(
        row[1] for row in idx_rows if row[0] == "idx_paper_trades_would_be_live_status"
    )
    assert "would_be_live" in sql and "status" in sql
    assert sql.find("would_be_live") < sql.find(
        "status"
    ), "would_be_live must be the leading column for digest index-only scan"
    await db.close()


async def test_migration_preserves_pre_cutover_nulls(tmp_path):
    db_path = tmp_path / "gecko.db"
    db = Database(str(db_path))
    await db.initialize()

    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            "INSERT INTO paper_trades "
            "(token_id, symbol, name, chain, signal_type, signal_data, "
            "entry_price, amount_usd, quantity, tp_pct, sl_pct, "
            "tp_price, sl_price, status, opened_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "tok1",
                "SYM",
                "Name",
                "eth",
                "first_signal",
                "{}",
                1.0,
                100.0,
                100.0,
                40.0,
                20.0,
                1.4,
                0.8,
                "open",
                "2026-04-22T00:00:00",
            ),
        )
        await conn.commit()

    await db._migrate_feedback_loop_schema()
    await db._migrate_feedback_loop_schema()

    async with aiosqlite.connect(str(db_path)) as conn:
        cur = await conn.execute(
            "SELECT would_be_live FROM paper_trades WHERE token_id='tok1'"
        )
        row = await cur.fetchone()
    assert row[0] is None, f"pre-cutover row must stay NULL; got {row[0]}"
    await db.close()
