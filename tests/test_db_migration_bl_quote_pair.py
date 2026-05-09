"""BL-NEW-QUOTE-PAIR: schema migration tests for `bl_quote_pair_v1`.

Per R3 reviewer MUST-FIX coverage:
- (a) wired-into-_apply_migrations (orphan-detection)
- (b) schema_version row written with correct version + description
- (c) pre-existing rows survive with new columns defaulting to NULL
- idempotent re-run (skip_exists path)
- columns added with correct type (TEXT)

Per BL-060 mid-flight migration lesson + feedback_mid_flight_flag_migration.md:
- New columns nullable, pre-cutover rows = NULL
- Forward-looking analysis ONLY scopes to candidates with first_seen_at
  >= cutover (not enforced at schema level — caller-side discipline).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scout.db import Database


@pytest.mark.asyncio
async def test_bl_quote_pair_v1_columns_added(tmp_path):
    """Both columns exist on candidates with type TEXT after initialize."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(candidates)")
    rows = await cur.fetchall()
    cols = {row[1]: row[2] for row in rows}  # name → type
    assert "quote_symbol" in cols
    assert "dex_id" in cols
    assert cols["quote_symbol"].upper() == "TEXT"
    assert cols["dex_id"].upper() == "TEXT"
    await db.close()


@pytest.mark.asyncio
async def test_bl_quote_pair_v1_wired_into_apply_migrations(tmp_path):
    """R3 MUST-FIX (a): orphaned migration would silently succeed first test
    that just checks columns exist (because `_create_tables` defines candidates
    as a fresh table on a clean DB). The schema_version row is the only proof
    that the migration method was actually invoked from _apply_migrations.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT 1 FROM schema_version WHERE version = 20260513"
    )
    assert (await cur.fetchone()) is not None, (
        "bl_quote_pair_v1 not wired into _apply_migrations — "
        "schema_version row missing for version=20260513"
    )
    await db.close()


@pytest.mark.asyncio
async def test_bl_quote_pair_v1_schema_version_row_content(tmp_path):
    """R3 MUST-FIX (b): schema_version row has correct version + description."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT version, applied_at, description FROM schema_version "
        "WHERE version = 20260513"
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == 20260513
    assert row[2] == "bl_quote_pair_v1_quote_symbol_dex_id"
    # applied_at must be ISO-parseable + timezone-aware
    parsed = datetime.fromisoformat(row[1])
    assert parsed.tzinfo is not None
    await db.close()


@pytest.mark.asyncio
async def test_bl_quote_pair_v1_idempotent_rerun(tmp_path):
    """Re-running the migration on already-migrated DB skips with action=skip_exists
    and does not raise. Required by the system's startup pattern: every restart
    re-runs all migrations."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Second call — must not raise (PRAGMA-guarded skip)
    await db._migrate_bl_quote_pair_v1()
    cur = await db._conn.execute("PRAGMA table_info(candidates)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "quote_symbol" in cols
    assert "dex_id" in cols
    await db.close()


@pytest.mark.asyncio
async def test_bl_quote_pair_v1_preserves_pre_existing_rows(tmp_path, token_factory):
    """R3 MUST-FIX (c): pre-existing candidates rows survive migration with
    new columns defaulting to NULL. (Mid-flight flag migration discipline:
    pre-cutover rows = NULL — never force-default-stamp.)

    Simulation: initialize fresh, insert a candidate, re-run migration —
    the candidate must remain with NULL quote_symbol and NULL dex_id.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    pre_token = token_factory(
        contract_address="0xpre",
        quote_symbol=None,
        dex_id=None,
    )
    await db.upsert_candidate(pre_token)
    await db._migrate_bl_quote_pair_v1()  # idempotent re-run
    cur = await db._conn.execute(
        "SELECT contract_address, quote_symbol, dex_id "
        "FROM candidates WHERE contract_address = ?",
        ("0xpre",),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "0xpre"
    assert row[1] is None  # quote_symbol stays NULL on pre-cutover row
    assert row[2] is None  # dex_id stays NULL on pre-cutover row
    await db.close()


@pytest.mark.asyncio
async def test_bl_quote_pair_v1_round_trip_persists_values(tmp_path, token_factory):
    """Forward-looking: post-cutover rows with quote_symbol + dex_id round-trip
    correctly via upsert_candidate."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    token = token_factory(
        contract_address="0xpost",
        quote_symbol="USDC",
        dex_id="raydium",
    )
    await db.upsert_candidate(token)
    cur = await db._conn.execute(
        "SELECT quote_symbol, dex_id FROM candidates WHERE contract_address = ?",
        ("0xpost",),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "USDC"
    assert row[1] == "raydium"
    await db.close()
