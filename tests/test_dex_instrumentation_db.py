"""Schema tests for the DEX-outcome instrumentation tables (C1).

Observe-only: these tables capture linkage/entry-mcap/proxy data; nothing here
feeds the scorer or gate.
"""

import pytest

from scout.db import Database

SCHEMA_VERSION = 20260629


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "dex_instr.db")
    await d.initialize()
    yield d
    await d.close()


async def _columns(db: Database, table: str) -> set[str]:
    cur = await db._conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def test_contract_coin_map_table_created(db):
    cols = await _columns(db, "contract_coin_map")
    assert {
        "contract_address",
        "chain",
        "coin_id",
        "resolved_at",
        "source",
        "confidence",
    } <= cols


async def test_entry_mcap_snapshots_table_created(db):
    cols = await _columns(db, "entry_mcap_snapshots")
    assert {
        "contract_address",
        "chain",
        "first_seen_at",
        "mcap_usd_at_entry",
        "liquidity_usd_at_entry",
        "token_age_days_at_entry",
        "captured_at",
    } <= cols


async def test_txns_h1_buys_snapshots_table_created(db):
    cols = await _columns(db, "txns_h1_buys_snapshots")
    assert {
        "contract_address",
        "txns_h1_buys",
        "txns_h1_sells",
        "source",
        "scanned_at",
    } <= cols


async def test_schema_version_row_recorded(db):
    cur = await db._conn.execute(
        "SELECT description FROM schema_version WHERE version = ?", (SCHEMA_VERSION,)
    )
    row = await cur.fetchone()
    assert row is not None


async def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "idem.db"
    for _ in range(3):
        d = Database(path)
        await d.initialize()
        await d.close()
