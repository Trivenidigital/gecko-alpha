"""W1 — `tg_act_shadow_v1` migration, tested from the OLD table shape.

A fresh `tmp_path` DB is NOT a migration test: `_create_tables()` emits the
current schema, so the ALTER path is never exercised and the test passes
while the upgrade is broken. Every test here builds the PRE-migration
`tg_social_signals` shape with plain `sqlite3` first, inserts rows, and only
then runs `Database.initialize()`.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from scout.db import Database

# Exact pre-migration DDL, copied from `_create_tables` as it stood before
# `resolution_snapshot_json` existed. Kept verbatim (FK clauses included) so
# the fixture is the shape a deployed DB actually has.
_OLD_SCHEMA = """
CREATE TABLE tg_social_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_handle  TEXT NOT NULL,
    msg_id          INTEGER NOT NULL,
    posted_at       TEXT NOT NULL,
    sender          TEXT,
    text            TEXT,
    cashtags        TEXT,
    contracts       TEXT,
    urls            TEXT,
    parsed_at       TEXT NOT NULL,
    UNIQUE(channel_handle, msg_id)
);
CREATE TABLE tg_social_signals (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    message_pk             INTEGER NOT NULL,
    token_id               TEXT NOT NULL,
    symbol                 TEXT NOT NULL,
    contract_address       TEXT,
    chain                  TEXT,
    mcap_at_sighting       REAL,
    resolution_state       TEXT NOT NULL,
    source_channel_handle  TEXT NOT NULL,
    alert_sent_at          TEXT,
    paper_trade_id         INTEGER,
    created_at             TEXT NOT NULL,
    FOREIGN KEY (message_pk) REFERENCES tg_social_messages(id)
);
"""


def _build_old_shape(db_path, *, signal_rows: int = 3) -> list[tuple]:
    """Create the pre-migration schema and populate it. Returns the inserted
    signal rows as tuples so the caller can assert byte-equality after."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_OLD_SCHEMA)
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO tg_social_messages "
        "(id, channel_handle, msg_id, posted_at, parsed_at) VALUES (1, ?, ?, ?, ?)",
        ("@gem", 1, now_iso, now_iso),
    )
    for i in range(signal_rows):
        conn.execute(
            """INSERT INTO tg_social_signals
               (message_pk, token_id, symbol, contract_address, chain,
                mcap_at_sighting, resolution_state, source_channel_handle,
                alert_sent_at, paper_trade_id, created_at)
               VALUES (1, ?, ?, ?, 'solana', ?, 'RESOLVED', '@gem', ?, NULL, ?)""",
            (f"tok-{i}", f"T{i}", f"0xaddr{i}", 50_000.0 + i, now_iso, now_iso),
        )
    conn.commit()
    rows = conn.execute(
        "SELECT id, token_id, symbol, mcap_at_sighting, created_at "
        "FROM tg_social_signals ORDER BY id"
    ).fetchall()
    conn.close()
    return rows


async def _columns(db: Database, table: str) -> set[str]:
    cur = await db._conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


@pytest.mark.asyncio
async def test_upgrade_adds_snapshot_column_to_old_table(tmp_path):
    """The additive column lands on a table that predates it."""
    db_path = tmp_path / "old.db"
    _build_old_shape(db_path)

    conn = sqlite3.connect(str(db_path))
    pre_cols = {r[1] for r in conn.execute("PRAGMA table_info(tg_social_signals)")}
    conn.close()
    assert "resolution_snapshot_json" not in pre_cols, "fixture is not the OLD shape"

    db = Database(db_path)
    await db.initialize()
    try:
        assert "resolution_snapshot_json" in await _columns(db, "tg_social_signals")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_upgrade_preserves_existing_rows_and_nulls_new_column(tmp_path):
    """Pre-cutover rows survive untouched and read NULL — the correct
    pre-cutover state, and outside every generation by construction."""
    db_path = tmp_path / "old.db"
    before = _build_old_shape(db_path, signal_rows=3)

    db = Database(db_path)
    await db.initialize()
    try:
        cur = await db._conn.execute("SELECT COUNT(*) FROM tg_social_signals")
        (count,) = await cur.fetchone()
        assert count == 3

        cur = await db._conn.execute(
            "SELECT id, token_id, symbol, mcap_at_sighting, created_at "
            "FROM tg_social_signals ORDER BY id"
        )
        after = [tuple(r) for r in await cur.fetchall()]
        assert after == [tuple(r) for r in before]

        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM tg_social_signals "
            "WHERE resolution_snapshot_json IS NULL"
        )
        (nulls,) = await cur.fetchone()
        assert nulls == 3
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_upgrade_is_idempotent(tmp_path):
    """A second initialize() on the migrated DB is a no-op, not an error."""
    db_path = tmp_path / "old.db"
    _build_old_shape(db_path, signal_rows=2)

    db = Database(db_path)
    await db.initialize()
    await db.close()

    db2 = Database(db_path)
    await db2.initialize()
    try:
        assert "resolution_snapshot_json" in await _columns(db2, "tg_social_signals")
        cur = await db2._conn.execute("SELECT COUNT(*) FROM tg_social_signals")
        (count,) = await cur.fetchone()
        assert count == 2
    finally:
        await db2.close()


@pytest.mark.asyncio
async def test_shadow_tables_created_with_unique_and_index(tmp_path):
    """`tg_act_shadow` + `tg_act_shadow_generations` exist, and the
    (signal_id, gate_version) UNIQUE is enforced by the DB, not by the writer."""
    db_path = tmp_path / "old.db"
    _build_old_shape(db_path, signal_rows=1)

    db = Database(db_path)
    await db.initialize()
    try:
        cur = await db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('tg_act_shadow', 'tg_act_shadow_generations')"
        )
        assert {r[0] for r in await cur.fetchall()} == {
            "tg_act_shadow",
            "tg_act_shadow_generations",
        }

        cur = await db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_tg_act_shadow_gate_created'"
        )
        assert await cur.fetchone() is not None

        now_iso = datetime.now(timezone.utc).isoformat()
        await db._conn.execute(
            "INSERT INTO tg_act_shadow (signal_id, gate_version, actionable, "
            "reason, features_json, created_at) VALUES (1, 'gv', 1, 'r', '{}', ?)",
            (now_iso,),
        )
        await db._conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            await db._conn.execute(
                "INSERT INTO tg_act_shadow (signal_id, gate_version, actionable, "
                "reason, features_json, created_at) "
                "VALUES (1, 'gv', 0, 'r2', '{}', ?)",
                (now_iso,),
            )
        await db._conn.rollback()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_generations_table_is_keyed_by_gate_version(tmp_path):
    """One row per gate_version — the registry cannot hold two activations
    for the same version, which is what makes re-enable RESUME rather than
    restart a generation."""
    db_path = tmp_path / "fresh.db"
    db = Database(db_path)
    await db.initialize()
    try:
        await db._conn.execute(
            "INSERT INTO tg_act_shadow_generations (gate_version, activated_at) "
            "VALUES ('tg-shadow-v1+abc', '2026-08-12T00:00:00+00:00')"
        )
        await db._conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            await db._conn.execute(
                "INSERT INTO tg_act_shadow_generations "
                "(gate_version, activated_at) "
                "VALUES ('tg-shadow-v1+abc', '2026-08-13T00:00:00+00:00')"
            )
        await db._conn.rollback()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_column_lands_on_a_later_run_when_parent_table_was_absent(
    tmp_path, monkeypatch
):
    """`tg_social_signals` belongs to `_migrate_feedback_loop_schema`. If that
    step is skipped, this migration must not abort the whole bootstrap — and
    the column must still land once the parent table appears.

    Converging late is only acceptable because this migration re-checks the
    PRAGMA on every run instead of short-circuiting on its own marker. If it
    ever gains a marker-based early return, this test is the one that fails.
    """
    from scout import db as db_module

    db_path = tmp_path / "late.db"

    async def _skip(self):
        return None

    monkeypatch.setattr(db_module.Database, "_migrate_feedback_loop_schema", _skip)
    first = Database(db_path)
    await first.initialize()  # must not raise
    try:
        cur = await first._conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='tg_social_signals'"
        )
        (parent_tables,) = await cur.fetchone()
        assert parent_tables == 0, "fixture did not actually skip the parent step"
        # The shadow tables themselves do not depend on the parent existing.
        assert "tg_act_shadow" in await _table_names(first)
    finally:
        await first.close()

    monkeypatch.undo()
    second = Database(db_path)
    await second.initialize()
    try:
        assert "resolution_snapshot_json" in await _columns(second, "tg_social_signals")
    finally:
        await second.close()


async def _table_names(db: Database) -> set[str]:
    cur = await db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {row[0] for row in await cur.fetchall()}
