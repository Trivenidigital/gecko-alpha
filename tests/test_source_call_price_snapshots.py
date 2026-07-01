"""Tests for the source_call_price_snapshots table + forward-only CA writer (C2).

DB-only tests run on Windows; the writer is tested via dependency injection
(fake resolve/fetch callables) so no aiohttp import is required here.
"""

import aiosqlite
import pytest

from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "scps.db")
    await d.initialize()
    yield d
    await d.close()


async def _table_info(conn, table):
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1]: row[2] for row in await cur.fetchall()}  # name -> declared type


# --------------------------------------------------------------------------
# Step 1 — DDL migration (acceptance criterion 1)
# --------------------------------------------------------------------------


async def test_snapshots_table_created_with_expected_columns(db):
    cols = await _table_info(db._conn, "source_call_price_snapshots")
    assert cols, "table source_call_price_snapshots was not created"
    assert set(cols) >= {
        "id",
        "identity_key",
        "identity_kind",
        "chain",
        "price",
        "snapshot_at",
        "source",
        "created_at",
    }


async def test_snapshots_identity_kind_check_rejects_unknown(db):
    with pytest.raises(aiosqlite.IntegrityError):
        await db._conn.execute(
            "INSERT INTO source_call_price_snapshots "
            "(identity_key, identity_kind, price, snapshot_at, source) "
            "VALUES ('k', 'BOGUS', 1.0, '2026-07-01T00:00:00Z', 'gt')"
        )


async def test_snapshots_source_check_rejects_unknown(db):
    with pytest.raises(aiosqlite.IntegrityError):
        await db._conn.execute(
            "INSERT INTO source_call_price_snapshots "
            "(identity_key, identity_kind, price, snapshot_at, source) "
            "VALUES ('k', 'contract', 1.0, '2026-07-01T00:00:00Z', 'BOGUS')"
        )


async def test_snapshots_accepts_valid_gt_contract_row(db):
    await db._conn.execute(
        "INSERT INTO source_call_price_snapshots "
        "(identity_key, identity_kind, chain, price, snapshot_at, source) "
        "VALUES ('base|0xabc', 'contract', 'base', 1.5, '2026-07-01T00:00:00Z', 'gt')"
    )
    await db._conn.commit()
    cur = await db._conn.execute(
        "SELECT identity_key, identity_kind, source, price "
        "FROM source_call_price_snapshots"
    )
    row = await cur.fetchone()
    assert row["identity_key"] == "base|0xabc"
    assert row["identity_kind"] == "contract"
    assert row["source"] == "gt"
    assert row["price"] == 1.5


async def test_snapshots_migration_idempotent(db):
    # Re-running the migration is a no-op: no error, table intact, no dupes.
    await db._migrate_source_call_price_snapshots_v1()
    await db._migrate_source_call_price_snapshots_v1()
    cols = await _table_info(db._conn, "source_call_price_snapshots")
    assert "identity_key" in cols


async def test_snapshots_index_present(db):
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='source_call_price_snapshots'"
    )
    names = {row[0] for row in await cur.fetchall()}
    assert any("identity" in n for n in names), f"no identity index found: {names}"
