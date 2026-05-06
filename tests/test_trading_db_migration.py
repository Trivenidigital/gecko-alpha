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


async def test_initialize_upgrades_pre_bl060_db(tmp_path):
    """Regression: _create_tables must not reference would_be_live on an
    existing paper_trades table that predates BL-060. The index creation
    belongs in the migration step AFTER ALTER TABLE adds the column.

    Reproduces the production failure where Database.initialize() raised
    `sqlite3.OperationalError: no such column: would_be_live` during
    _create_tables' executescript block on an upgrade from a DB that
    already had paper_trades without the BL-060 column.
    """
    db_path = tmp_path / "gecko.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.executescript(
            """
            CREATE TABLE paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                chain TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                signal_data TEXT NOT NULL,
                entry_price REAL NOT NULL,
                amount_usd REAL NOT NULL,
                quantity REAL NOT NULL,
                tp_pct REAL NOT NULL DEFAULT 20.0,
                sl_pct REAL NOT NULL DEFAULT 10.0,
                tp_price REAL NOT NULL,
                sl_price REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                exit_price REAL,
                exit_reason TEXT,
                pnl_usd REAL,
                pnl_pct REAL,
                peak_price REAL,
                peak_pct REAL,
                opened_at TEXT NOT NULL,
                closed_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(token_id, signal_type, opened_at)
            );
            """
        )
        await conn.commit()

    db = Database(str(db_path))
    await db.initialize()

    cols = await _existing_paper_trades_columns(db._conn)
    assert "would_be_live" in cols, f"migration failed to add column; got {cols}"

    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='idx_paper_trades_would_be_live_status'"
    )
    row = await cur.fetchone()
    assert row is not None, "composite index missing after upgrade"

    await db.close()


# ---------------------------------------------------------------------------
# BL-061: ladder state columns + paper_migrations cutover table
# ---------------------------------------------------------------------------


async def test_paper_migrations_table_created(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    conn = db._conn
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='paper_migrations'"
    )
    row = await cur.fetchone()
    assert row is not None, "paper_migrations table must exist after initialize()"

    cur = await conn.execute(
        "SELECT cutover_ts FROM paper_migrations WHERE name='bl061_ladder'"
    )
    row = await cur.fetchone()
    assert row is not None, "bl061_ladder cutover row must be created on first init"
    assert row[0] is not None
    await db.close()


async def test_bl061_ladder_columns_added(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(paper_trades)")
    cols = {row[1] for row in await cur.fetchall()}
    required = {
        "leg_1_filled_at",
        "leg_1_exit_price",
        "leg_2_filled_at",
        "leg_2_exit_price",
        "remaining_qty",
        "floor_armed",
        "realized_pnl_usd",
    }
    missing = required - cols
    assert not missing, f"missing ladder columns: {missing}"
    await db.close()


# ---------------------------------------------------------------------------
# BL-062: peak_fade_fired_at column + paper_migrations cutover row + index
# ---------------------------------------------------------------------------


async def test_bl062_peak_fade_column_added(tmp_path):
    from scout.db import Database
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(paper_trades)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "peak_fade_fired_at" in cols, (
        f"peak_fade_fired_at column missing from paper_trades; have {sorted(cols)}"
    )
    await db.close()


async def test_bl062_cutover_row_written(tmp_path):
    from scout.db import Database
    from datetime import datetime
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT cutover_ts FROM paper_migrations WHERE name='bl062_peak_fade'"
    )
    row = await cur.fetchone()
    assert row is not None, "bl062_peak_fade row must exist after initialize()"
    parsed = datetime.fromisoformat(row[0])
    assert parsed.tzinfo is not None, "cutover_ts must be ISO with tz"
    await db.close()


async def test_bl062_index_created(tmp_path):
    from scout.db import Database
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='idx_paper_trades_peak_fade_fired_at'"
    )
    row = await cur.fetchone()
    assert row is not None, "idx_paper_trades_peak_fade_fired_at must exist"
    await db.close()


async def test_bl062_migration_idempotent_re_run(tmp_path):
    """Re-initialize an existing DB: no errors, cutover_ts preserved."""
    from scout.db import Database
    db_path = tmp_path / "t.db"
    db = Database(db_path)
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT cutover_ts FROM paper_migrations WHERE name='bl062_peak_fade'"
    )
    (first_ts,) = await cur.fetchone()
    await db.close()

    db2 = Database(db_path)
    await db2.initialize()
    cur = await db2._conn.execute(
        "SELECT cutover_ts FROM paper_migrations WHERE name='bl062_peak_fade'"
    )
    (second_ts,) = await cur.fetchone()
    assert second_ts == first_ts, (
        f"cutover_ts must be preserved across re-init; first={first_ts} second={second_ts}"
    )
    await db2.close()


async def test_bl_hpf_v1_migration_idempotent_re_run(tmp_path):
    """BL-NEW-HPF migration must succeed on second initialize() without error.

    Project pattern — see test_bl062_migration_idempotent_re_run.
    """
    from scout.db import Database

    db = Database(str(tmp_path / "test.db"))
    await db.initialize()
    await db.initialize()  # second run must not raise
    cur = await db._conn.execute(
        "SELECT 1 FROM paper_migrations WHERE name='bl_hpf_v1'"
    )
    assert await cur.fetchone() is not None, "cutover row should exist"
    cur = await db._conn.execute(
        "SELECT version FROM schema_version WHERE description LIKE '%hpf%'"
    )
    assert await cur.fetchone() is not None, "schema_version stamp should exist"
    await db.close()
