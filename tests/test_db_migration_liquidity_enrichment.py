"""BL-NEW-TODAYS-FOCUS-LIQUIDITY-VENUE-FACTS Phase 1a-i: schema migration
tests for `bl_new_liquidity_enrichment_v1`.

Mirrors test_db_migration_bl_quote_pair.py coverage:
- (a) wired-into-_apply_migrations (orphan detection via schema_version row)
- (b) schema_version row written with correct version + description
- (c) paper_migrations row written with cutover_ts (Phase 1 measurement
      substrate anchors on this row)
- (d) pre-existing rows survive with new columns defaulting to NULL
- idempotent re-run (skip_exists path)
- columns added with correct types (REAL / TEXT / TEXT / TEXT)
- no DEFAULT clauses — absence-vs-zero semantics preserved
- description-mismatch post-assertion catches version-collision

Per design doc tasks/design_liquidity_enrichment_b2_2026_05_29.md and
operator guardrail #4 (nullable + non-behavioral) + #6 (no consumer).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from scout.db import Database


@pytest.mark.asyncio
async def test_liquidity_enrichment_v1_all_four_columns_added(tmp_path):
    """4 columns exist on candidates with correct types after initialize."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(candidates)")
    rows = await cur.fetchall()
    cols = {row[1]: row[2] for row in rows}  # name → type
    assert "liquidity_usd_enriched" in cols
    assert "liquidity_enriched_source" in cols
    assert "liquidity_enriched_at" in cols
    assert "liquidity_enriched_confidence" in cols
    assert cols["liquidity_usd_enriched"].upper() == "REAL"
    assert cols["liquidity_enriched_source"].upper() == "TEXT"
    assert cols["liquidity_enriched_at"].upper() == "TEXT"
    assert cols["liquidity_enriched_confidence"].upper() == "TEXT"
    await db.close()


@pytest.mark.asyncio
async def test_liquidity_enrichment_v1_columns_nullable_no_default(tmp_path):
    """All 4 enrichment columns have no DEFAULT clause and accept NULL.

    Per design: nullable preserves absence-vs-zero semantics. Pre-cutover
    rows MUST be NULL (per feedback_mid_flight_flag_migration.md), and
    "writer visited but DexScreener returned no pair" must be
    distinguishable from "writer never visited."
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(candidates)")
    rows = await cur.fetchall()
    # PRAGMA table_info returns: (cid, name, type, notnull, dflt_value, pk)
    by_name = {row[1]: row for row in rows}
    for col in (
        "liquidity_usd_enriched",
        "liquidity_enriched_source",
        "liquidity_enriched_at",
        "liquidity_enriched_confidence",
    ):
        assert by_name[col][3] == 0, f"{col} should be nullable (notnull=0)"
        assert by_name[col][4] is None, f"{col} should have NO default value"
    await db.close()


@pytest.mark.asyncio
async def test_liquidity_enrichment_v1_wired_into_apply_migrations(tmp_path):
    """Schema_version row 20260529 is the orphan-detection signal: it
    only exists if _migrate_liquidity_enrichment_v1 was actually called
    from _apply_migrations."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT 1 FROM schema_version WHERE version = 20260529"
    )
    assert (await cur.fetchone()) is not None, (
        "bl_new_liquidity_enrichment_v1 not wired into _apply_migrations — "
        "schema_version row missing for version=20260529"
    )
    await db.close()


@pytest.mark.asyncio
async def test_liquidity_enrichment_v1_schema_version_row_content(tmp_path):
    """Schema_version row has correct version + description."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT version, applied_at, description FROM schema_version "
        "WHERE version = 20260529"
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == 20260529
    assert row[2] == "bl_new_liquidity_enrichment_v1_candidates_enrichment_cols"
    # applied_at must be ISO-parseable + timezone-aware
    parsed = datetime.fromisoformat(row[1])
    assert parsed.tzinfo is not None
    await db.close()


@pytest.mark.asyncio
async def test_liquidity_enrichment_v1_paper_migrations_row_written(tmp_path):
    """The paper_migrations row IS the canonical cutover_ts anchor for
    the Phase 1 measurement substrate (per design's coverage SQL).

    Distinct from schema_version: paper_migrations.cutover_ts is what
    the watchdog and the eventual Phase 2 coverage gate query."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT cutover_ts FROM paper_migrations "
        "WHERE name = 'bl_new_liquidity_enrichment_v1'"
    )
    row = await cur.fetchone()
    assert row is not None, (
        "paper_migrations row missing — measurement substrate "
        "cannot anchor cutover_ts"
    )
    parsed = datetime.fromisoformat(row[0])
    assert parsed.tzinfo is not None
    await db.close()


@pytest.mark.asyncio
async def test_liquidity_enrichment_v1_idempotent_rerun(tmp_path):
    """Re-running the migration on already-migrated DB skips with
    action=skip_exists and does not raise. The system's startup pattern
    re-runs every migration on every restart."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._migrate_liquidity_enrichment_v1()  # second invocation
    cur = await db._conn.execute("PRAGMA table_info(candidates)")
    cols = {row[1] for row in await cur.fetchall()}
    assert {
        "liquidity_usd_enriched",
        "liquidity_enriched_source",
        "liquidity_enriched_at",
        "liquidity_enriched_confidence",
    }.issubset(cols)
    await db.close()


@pytest.mark.asyncio
async def test_liquidity_enrichment_v1_preserves_pre_existing_rows(
    tmp_path, token_factory
):
    """Pre-existing candidates rows survive migration with new columns
    defaulting to NULL. (Mid-flight migration discipline: pre-cutover
    rows = NULL, never force-default-stamp.)"""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    pre_token = token_factory(contract_address="0xpre")
    await db.upsert_candidate(pre_token)
    await db._migrate_liquidity_enrichment_v1()  # idempotent re-run
    cur = await db._conn.execute(
        "SELECT contract_address, liquidity_usd_enriched, "
        "liquidity_enriched_source, liquidity_enriched_at, "
        "liquidity_enriched_confidence "
        "FROM candidates WHERE contract_address = ?",
        ("0xpre",),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "0xpre"
    # All 4 enrichment columns NULL on pre-cutover row
    assert row[1] is None, "liquidity_usd_enriched should be NULL pre-cutover"
    assert row[2] is None, "liquidity_enriched_source should be NULL pre-cutover"
    assert row[3] is None, "liquidity_enriched_at should be NULL pre-cutover"
    assert row[4] is None, (
        "liquidity_enriched_confidence should be NULL pre-cutover"
    )
    await db.close()


@pytest.mark.asyncio
async def test_liquidity_enrichment_v1_description_mismatch_raises(tmp_path):
    """Version-collision case: external tool pre-seeds version=20260529
    with a different description. INSERT OR IGNORE would silently skip;
    post-assertion catches the mismatch."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        "UPDATE schema_version SET description = ? WHERE version = ?",
        ("some_other_migration_v999", 20260529),
    )
    await db._conn.commit()
    with pytest.raises(RuntimeError, match="description mismatch"):
        await db._migrate_liquidity_enrichment_v1()
    await db.close()


@pytest.mark.asyncio
async def test_liquidity_enrichment_v1_existing_liquidity_usd_untouched(
    tmp_path, token_factory
):
    """Anti-scope: this migration must NOT modify the existing
    liquidity_usd column or its semantics (operator guardrail: nullable
    + non-behavioral; design's decoupled-columns approach).
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    pre_token = token_factory(
        contract_address="0xpre",
        liquidity_usd=50_000.0,
    )
    await db.upsert_candidate(pre_token)
    await db._migrate_liquidity_enrichment_v1()  # idempotent
    cur = await db._conn.execute(
        "SELECT liquidity_usd FROM candidates WHERE contract_address = ?",
        ("0xpre",),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == 50_000.0, (
        "existing liquidity_usd value must NOT be touched by migration"
    )
    await db.close()
