"""F3 — `alert_events_v1` migration, tested from the OLD (pre-table) shape.

A fresh `tmp_path` DB is a weak migration test on its own: it proves the DDL
parses, not that the step lands on a DB that predates it. Every upgrade test
here builds a PRE-migration database with plain `sqlite3` first, asserts the
fixture really lacks `alert_events`, populates the adjacent table the ledger
records against, and only then runs `Database.initialize()`.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from scout.db import Database

# Pre-migration shape: `combo_performance` as it stands at 3af6cd40, with NO
# `alert_events` table anywhere. Kept verbatim so the fixture is the shape a
# deployed DB actually has when this migration first runs.
_OLD_SCHEMA = """
CREATE TABLE combo_performance (
    combo_key                   TEXT NOT NULL,
    window                      TEXT NOT NULL,
    trades                      INTEGER NOT NULL DEFAULT 0,
    wins                        INTEGER NOT NULL DEFAULT 0,
    losses                      INTEGER NOT NULL DEFAULT 0,
    total_pnl_usd               REAL NOT NULL DEFAULT 0,
    avg_pnl_pct                 REAL NOT NULL DEFAULT 0,
    win_rate_pct                REAL NOT NULL DEFAULT 0,
    suppressed                  INTEGER NOT NULL DEFAULT 0,
    suppressed_at               TEXT,
    parole_at                   TEXT,
    parole_trades_remaining     INTEGER,
    refresh_failures            INTEGER NOT NULL DEFAULT 0,
    last_refreshed              TEXT,
    PRIMARY KEY (combo_key, window)
);
"""


def _build_old_shape(db_path, *, combo_rows: int = 3) -> list[tuple]:
    """Create the pre-migration schema and populate it. Returns the inserted
    combo rows so the caller can assert byte-equality after the upgrade."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_OLD_SCHEMA)
    now_iso = datetime.now(timezone.utc).isoformat()
    for i in range(combo_rows):
        conn.execute(
            "INSERT INTO combo_performance "
            "(combo_key, window, trades, wins, losses, total_pnl_usd, "
            " avg_pnl_pct, win_rate_pct, suppressed, last_refreshed) "
            "VALUES (?, '30d', ?, ?, 0, 0, 0, ?, 0, ?)",
            (f"combo_{i}", 10 + i, 5 + i, 50.0 + i, now_iso),
        )
    conn.commit()
    rows = conn.execute(
        "SELECT combo_key, trades, wins, win_rate_pct, last_refreshed "
        "FROM combo_performance ORDER BY combo_key"
    ).fetchall()
    conn.close()
    return rows


async def _table_names(db: Database) -> set[str]:
    cur = await db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {row[0] for row in await cur.fetchall()}


async def _columns(db: Database, table: str) -> set[str]:
    cur = await db._conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


@pytest.mark.asyncio
async def test_upgrade_creates_alert_events_on_old_db(tmp_path):
    """The table lands on a database that predates it."""
    db_path = tmp_path / "old.db"
    _build_old_shape(db_path)

    conn = sqlite3.connect(str(db_path))
    pre_tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    assert "alert_events" not in pre_tables, "fixture is not the OLD shape"

    db = Database(db_path)
    await db.initialize()
    try:
        assert "alert_events" in await _table_names(db)
        assert await _columns(db, "alert_events") == {
            "id",
            "created_at",
            "event_type",
            "combo_key",
            "signal_type",
            "alert_source",
            "transition",
            "detected_at",
            "delivery_result",
            "retry",
            "payload_hash",
            "state_json",
            "detail",
        }
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_upgrade_preserves_existing_rows(tmp_path):
    """Pre-cutover `combo_performance` rows survive the upgrade untouched."""
    db_path = tmp_path / "old.db"
    before = _build_old_shape(db_path, combo_rows=3)

    db = Database(db_path)
    await db.initialize()
    try:
        cur = await db._conn.execute(
            "SELECT combo_key, trades, wins, win_rate_pct, last_refreshed "
            "FROM combo_performance ORDER BY combo_key"
        )
        after = [tuple(r) for r in await cur.fetchall()]
        assert after == [tuple(r) for r in before]

        # No backfill of history — the ledger holds exactly ONE row, its own
        # install epoch, and nothing reconstructed from the past.
        cur = await db._conn.execute("SELECT event_type, created_at FROM alert_events")
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "ledger_installed"
        assert rows[0][1] is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_upgrade_is_idempotent(tmp_path):
    """A second initialize() on the migrated DB is a no-op, not an error, and
    does not discard rows the ledger already holds."""
    db_path = tmp_path / "old.db"
    _build_old_shape(db_path, combo_rows=2)

    db = Database(db_path)
    await db.initialize()
    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO alert_events (created_at, event_type) "
        "VALUES (?, 'refresh_completed')",
        (now_iso,),
    )
    await db._conn.commit()
    await db.close()

    db2 = Database(db_path)
    await db2.initialize()
    try:
        assert "alert_events" in await _table_names(db2)
        # The manually-inserted heartbeat survives, and the epoch row is NOT
        # duplicated — a second epoch would move the watchdog's fallback age
        # forward on every startup and mask a dead writer indefinitely.
        cur = await db2._conn.execute(
            "SELECT COUNT(*) FROM alert_events WHERE event_type='ledger_installed'"
        )
        (epochs,) = await cur.fetchone()
        assert epochs == 1, "re-running initialize() appended a second epoch row"
        cur = await db2._conn.execute(
            "SELECT COUNT(*) FROM alert_events WHERE event_type='refresh_completed'"
        )
        (beats,) = await cur.fetchone()
        assert beats == 1
        cur = await db2._conn.execute("SELECT COUNT(*) FROM combo_performance")
        (combos,) = await cur.fetchone()
        assert combos == 2
    finally:
        await db2.close()


@pytest.mark.asyncio
async def test_indexes_created_inside_the_migration_step(tmp_path):
    """Both indexes ship WITH the DDL (DDL-order lesson), not deferred."""
    db_path = tmp_path / "old.db"
    _build_old_shape(db_path, combo_rows=1)

    db = Database(db_path)
    await db.initialize()
    try:
        cur = await db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name IN ('idx_alert_events_combo_created', "
            "             'idx_alert_events_type_created')"
        )
        assert {r[0] for r in await cur.fetchall()} == {
            "idx_alert_events_combo_created",
            "idx_alert_events_type_created",
        }
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_event_type_check_constraint_is_enforced_by_the_db(tmp_path):
    """The event-type vocabulary is a DB constraint, not writer discipline —
    a typo'd event_type must fail loudly rather than land as an unqueryable row."""
    db_path = tmp_path / "fresh.db"
    db = Database(db_path)
    await db.initialize()
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        with pytest.raises(sqlite3.IntegrityError):
            await db._conn.execute(
                "INSERT INTO alert_events (created_at, event_type) VALUES (?, ?)",
                (now_iso, "not_a_real_event_type"),
            )
        await db._conn.rollback()

        for event_type in (
            "suppression_transition",
            "parole_slot_spent",
            "parole_slot_refunded",
            "reversal_pending_recorded",
            "alert_dispatched",
            "alert_delivered",
            "alert_failed",
            "marker_stamped",
            "marker_cleared",
            "marker_anomaly",
            "refresh_completed",
            "ledger_installed",
        ):
            await db._conn.execute(
                "INSERT INTO alert_events (created_at, event_type) VALUES (?, ?)",
                (now_iso, event_type),
            )
        await db._conn.commit()
        cur = await db._conn.execute("SELECT COUNT(*) FROM alert_events")
        (count,) = await cur.fetchone()
        # 12 inserted here + the 1 epoch row the migration seeded.
        assert count == 13
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_schema_version_stamped(tmp_path):
    """20260814 is allocated to this migration (see docs/migration_versions.md)."""
    db_path = tmp_path / "old.db"
    _build_old_shape(db_path, combo_rows=1)

    db = Database(db_path)
    await db.initialize()
    try:
        cur = await db._conn.execute(
            "SELECT description FROM schema_version WHERE version = 20260814"
        )
        row = await cur.fetchone()
        assert row is not None, "schema_version row for 20260814 was not stamped"
        assert row[0] == "alert_events_v1"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_install_epoch_row_is_seeded_once_with_the_migration_time(tmp_path):
    """The epoch row is what keeps the §12a watchdog quiet across the deploy
    boundary WITHOUT waiving it, so it has to be a true statement: seeded once,
    stamped with the migration time, never refreshed on later startups."""
    db_path = tmp_path / "fresh.db"
    db = Database(db_path)
    await db.initialize()
    try:
        cur = await db._conn.execute(
            "SELECT created_at, detail FROM alert_events "
            "WHERE event_type = 'ledger_installed'"
        )
        rows = await cur.fetchall()
        assert len(rows) == 1
        first_stamp = rows[0][0]
        assert "alert_events_v1" in rows[0][1]
    finally:
        await db.close()

    db2 = Database(db_path)
    await db2.initialize()
    try:
        cur = await db2._conn.execute(
            "SELECT created_at FROM alert_events WHERE event_type = 'ledger_installed'"
        )
        rows = await cur.fetchall()
        assert len(rows) == 1, "a second startup appended another epoch row"
        assert rows[0][0] == first_stamp, "the epoch stamp moved on restart"
    finally:
        await db2.close()


def test_event_types_constant_matches_the_migration_check_constraint():
    """Tripwire. `EVENT_TYPES` is referenced by no production code path, so
    nothing else would notice it drifting from the DB CHECK — and a constant
    that silently disagrees with the schema is worse than no constant, because
    a reader will trust it."""
    import inspect
    import re

    from scout.db import Database as _Database
    from scout.trading.alert_events import EVENT_TYPES

    src = inspect.getsource(_Database._migrate_alert_events_v1)
    body = src[src.index("event_type       TEXT NOT NULL CHECK") :]
    body = body[: body.index("))")]
    in_ddl = set(re.findall(r"'([a-z_]+)'", body))
    assert in_ddl == set(EVENT_TYPES), (
        "EVENT_TYPES and the migration CHECK constraint disagree: "
        f"only in DDL={sorted(in_ddl - set(EVENT_TYPES))}, "
        f"only in EVENT_TYPES={sorted(set(EVENT_TYPES) - in_ddl)}"
    )
