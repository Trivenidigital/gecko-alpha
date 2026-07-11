"""Tests for the ``retire_dead_tables_v1`` migration (NAR-06 + INF-07).

Opt-in-destructive: the ``DROP TABLE`` statements are IRREVERSIBLE, so they
fire only when the operator sets ``RETIRE_DEAD_TABLES_ENABLED`` (plumbed via
``Database.initialize(retire_dead_tables=...)``). The migration is fail-closed
— it records nothing in ``paper_migrations`` / ``schema_version`` until the
drops actually run, so a later deploy with the flag on still performs them.

``social_signals`` is deliberately EXCLUDED from the drop set: it retains a
live reader in ``scout/trending/tracker.py`` (the "social 4th tier" of the
trending-comparison tracker) independent of the retired LunarCrush loop.
"""

from __future__ import annotations

import pytest

from scout.db import Database

_DEAD_TABLES = (
    "social_baselines",
    "social_credit_ledger",
    "gainers_comparisons_bak_20260602051857",
    "paper_trades_junk_backfill",
)
_MIGRATION_NAME = "retire_dead_tables_v1"
_SCHEMA_VERSION = 20260711


async def _table_exists(db: Database, name: str) -> bool:
    cur = await db._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return await cur.fetchone() is not None


async def _seed_dead_tables(db: Database) -> None:
    """Simulate a legacy prod DB that still carries the retired tables."""
    for table in _DEAD_TABLES:
        await db._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} (id INTEGER PRIMARY KEY)"
        )
    await db._conn.commit()


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "retire.db")
    await d.initialize()  # retire_dead_tables defaults off
    yield d
    await d.close()


async def test_social_signals_retained_on_fresh_init(db):
    """social_signals has a live reader (trending tracker) -> never retired."""
    assert await _table_exists(db, "social_signals")


async def test_fresh_init_omits_retired_table_create_statements(db):
    """Fresh install: the retired tables' CREATE statements were removed."""
    assert not await _table_exists(db, "social_baselines")
    assert not await _table_exists(db, "social_credit_ledger")


async def test_flag_off_no_drops_and_not_recorded(db):
    await _seed_dead_tables(db)

    await db._migrate_retire_dead_tables_v1(enabled=False)

    for table in _DEAD_TABLES:
        assert await _table_exists(db, table), f"{table} must survive when flag off"
    cur = await db._conn.execute(
        "SELECT 1 FROM paper_migrations WHERE name=?", (_MIGRATION_NAME,)
    )
    assert (
        await cur.fetchone() is None
    ), "fail-closed: must NOT record when the flag is off"


async def test_flag_on_drops_and_records(db):
    await _seed_dead_tables(db)

    await db._migrate_retire_dead_tables_v1(enabled=True)

    for table in _DEAD_TABLES:
        assert not await _table_exists(db, table), f"{table} must be dropped"
    # social_signals is excluded from the drop set.
    assert await _table_exists(db, "social_signals")

    cur = await db._conn.execute(
        "SELECT 1 FROM paper_migrations WHERE name=?", (_MIGRATION_NAME,)
    )
    assert await cur.fetchone() is not None
    cur = await db._conn.execute(
        "SELECT description FROM schema_version WHERE version=?", (_SCHEMA_VERSION,)
    )
    row = await cur.fetchone()
    assert row is not None and row[0] == _MIGRATION_NAME


async def test_flag_on_missing_tables_is_noop_but_records(db):
    """enabled=True on a DB without the dead tables: DROP IF EXISTS no-ops."""
    for table in _DEAD_TABLES:
        assert not await _table_exists(db, table)

    await db._migrate_retire_dead_tables_v1(enabled=True)

    cur = await db._conn.execute(
        "SELECT 1 FROM paper_migrations WHERE name=?", (_MIGRATION_NAME,)
    )
    assert await cur.fetchone() is not None


async def test_idempotent_once_recorded_reruns_skip(db):
    await _seed_dead_tables(db)
    await db._migrate_retire_dead_tables_v1(enabled=True)

    # A table re-appearing post-retirement must NOT be re-dropped: once the
    # migration is recorded, re-runs skip regardless of the flag.
    await db._conn.execute(
        "CREATE TABLE paper_trades_junk_backfill (id INTEGER PRIMARY KEY)"
    )
    await db._conn.commit()

    await db._migrate_retire_dead_tables_v1(enabled=True)  # must not raise

    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_migrations WHERE name=?", (_MIGRATION_NAME,)
    )
    assert (await cur.fetchone())[0] == 1
    assert await _table_exists(db, "paper_trades_junk_backfill")


async def test_initialize_flag_on_drops_legacy_table(tmp_path):
    """End-to-end through initialize(): flag-on drops a legacy table."""
    path = tmp_path / "e2e.db"
    d = Database(path)
    await d.initialize()  # flag off
    await d._conn.execute("CREATE TABLE social_baselines (id INTEGER PRIMARY KEY)")
    await d._conn.commit()
    await d.close()

    d2 = Database(path)
    await d2.initialize(retire_dead_tables=True)
    exists = await _table_exists(d2, "social_baselines")
    await d2.close()

    assert not exists


async def test_initialize_flag_off_preserves_legacy_table(tmp_path):
    """End-to-end: default initialize() leaves legacy tables untouched."""
    path = tmp_path / "e2e_off.db"
    d = Database(path)
    await d.initialize()
    await d._conn.execute("CREATE TABLE social_baselines (id INTEGER PRIMARY KEY)")
    await d._conn.commit()
    await d.close()

    d2 = Database(path)
    await d2.initialize()  # flag off
    exists = await _table_exists(d2, "social_baselines")
    await d2.close()

    assert exists
