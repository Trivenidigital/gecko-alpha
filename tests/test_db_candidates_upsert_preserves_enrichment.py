"""Phase 1a-ii: verify ``upsert_candidate`` preserves the 4 enrichment
columns across re-ingest.

This is the round_trip_persists_values test the operator required for
Phase 1a-ii. The Phase 1a-i schema landed 4 nullable columns on
``candidates``, but the existing ``INSERT OR REPLACE`` semantics in
``upsert_candidate`` would have silently wiped any cron-written value
to NULL on the next ingest cycle.

The Phase 1a-ii change converts ``upsert_candidate`` to SQLite UPSERT
(``ON CONFLICT(contract_address) DO UPDATE``) with the 4 enrichment
columns intentionally OMITTED from the ``DO UPDATE SET`` clause. This
test asserts:

1. Cron-written enrichment values SURVIVE a subsequent re-upsert.
2. All non-enrichment fields are still updated on re-upsert (existing
   semantics preserved).
3. First-insert path leaves enrichment columns NULL (correct — the
   cron has not visited yet).
4. The same applies regardless of which non-enrichment fields the
   token model carries.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scout.db import Database


@pytest.mark.asyncio
async def test_first_insert_leaves_enrichment_columns_null(tmp_path, token_factory):
    """First INSERT (no conflict) → enrichment columns NULL.

    The cron has not visited yet; the row is correctly unenriched.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    token = token_factory(contract_address="0xfresh")
    await db.upsert_candidate(token)
    cur = await db._conn.execute(
        "SELECT liquidity_usd_enriched, liquidity_enriched_source, "
        "liquidity_enriched_at, liquidity_enriched_confidence "
        "FROM candidates WHERE contract_address = ?",
        ("0xfresh",),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] is None
    assert row[1] is None
    assert row[2] is None
    assert row[3] is None
    await db.close()


@pytest.mark.asyncio
async def test_cron_written_enrichment_survives_reupsert(tmp_path, token_factory):
    """The critical round-trip test.

    Sequence:
      1. Ingest writes a candidate via upsert_candidate.
      2. Cron writes enrichment columns via direct UPDATE
         (simulating the Phase 1a-ii cron).
      3. Next ingest cycle re-calls upsert_candidate for same address.
      4. Enrichment columns must STILL be populated.

    With the prior ``INSERT OR REPLACE`` semantics this test would have
    FAILED — step 3 would have NULL'd all 4 enrichment columns.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    addr = "0xenriched"

    # Step 1: initial ingest.
    initial = token_factory(
        contract_address=addr,
        ticker="OLD",
        liquidity_usd=0.0,
    )
    await db.upsert_candidate(initial)

    # Step 2: simulate the cron writing enrichment via direct UPDATE.
    enriched_at = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "UPDATE candidates SET "
        "  liquidity_usd_enriched = ?, "
        "  liquidity_enriched_source = ?, "
        "  liquidity_enriched_at = ?, "
        "  liquidity_enriched_confidence = ? "
        "WHERE contract_address = ?",
        (123_456.78, "dexscreener_v1", enriched_at, "definite", addr),
    )
    await db._conn.commit()

    # Step 3: next ingest re-upserts with NEW values for non-enrichment
    # fields (simulating a real re-ingest cycle).
    re_ingest = token_factory(
        contract_address=addr,
        ticker="NEW",
        liquidity_usd=999.0,  # CG-sourced row would set 0 here in prod
    )
    await db.upsert_candidate(re_ingest)

    # Step 4: enrichment columns MUST survive.
    cur = await db._conn.execute(
        "SELECT ticker, liquidity_usd, "
        "  liquidity_usd_enriched, liquidity_enriched_source, "
        "  liquidity_enriched_at, liquidity_enriched_confidence "
        "FROM candidates WHERE contract_address = ?",
        (addr,),
    )
    row = await cur.fetchone()
    assert row is not None
    # Non-enrichment fields were updated (existing behavior preserved).
    assert row[0] == "NEW"
    assert row[1] == 999.0
    # Enrichment fields SURVIVED — this is the new behavior.
    assert row[2] == 123_456.78, (
        "liquidity_usd_enriched must SURVIVE re-upsert "
        "(this is the round-trip persistence guarantee)"
    )
    assert row[3] == "dexscreener_v1"
    assert row[4] == enriched_at
    assert row[5] == "definite"
    await db.close()


@pytest.mark.asyncio
async def test_reupsert_still_updates_non_enrichment_fields(tmp_path, token_factory):
    """Anti-regression: the UPSERT must still update all non-enrichment
    fields on conflict (preserving existing upsert semantics).

    Specifically: market_cap_usd, volume_24h_usd, token_name, ticker,
    quote_symbol, dex_id, etc. must all reflect the new token's values after
    the re-upsert. `first_seen_at` is intentionally excluded because it is an
    earliest-sighting contract.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    addr = "0xreupsert"

    initial = token_factory(
        contract_address=addr,
        token_name="OldName",
        ticker="OLD",
        market_cap_usd=100.0,
        volume_24h_usd=200.0,
        quote_symbol="WETH",
        dex_id="uniswap_v3",
    )
    await db.upsert_candidate(initial)

    updated = token_factory(
        contract_address=addr,
        token_name="NewName",
        ticker="NEW",
        market_cap_usd=500.0,
        volume_24h_usd=600.0,
        quote_symbol="USDC",
        dex_id="raydium",
    )
    await db.upsert_candidate(updated)

    cur = await db._conn.execute(
        "SELECT token_name, ticker, market_cap_usd, volume_24h_usd, "
        "  quote_symbol, dex_id "
        "FROM candidates WHERE contract_address = ?",
        (addr,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "NewName"
    assert row[1] == "NEW"
    assert row[2] == 500.0
    assert row[3] == 600.0
    assert row[4] == "USDC"
    assert row[5] == "raydium"
    await db.close()


@pytest.mark.asyncio
async def test_reupsert_preserves_earlier_first_seen_at(tmp_path, token_factory):
    """Re-ingest should not make an old token look newly discovered."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    addr = "0xfirstseen"

    first_seen = datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc)
    later_seen = datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc)
    await db.upsert_candidate(
        token_factory(contract_address=addr, first_seen_at=first_seen)
    )
    await db.upsert_candidate(
        token_factory(contract_address=addr, first_seen_at=later_seen)
    )

    cur = await db._conn.execute(
        "SELECT first_seen_at FROM candidates WHERE contract_address = ?",
        (addr,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == first_seen.isoformat()
    await db.close()


@pytest.mark.asyncio
async def test_reupsert_can_move_first_seen_at_earlier(tmp_path, token_factory):
    """If a better earlier sighting arrives later, store the earliest value."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    addr = "0xfirstseen-earlier"

    first_recorded = datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc)
    earlier_seen = datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc)
    await db.upsert_candidate(
        token_factory(contract_address=addr, first_seen_at=first_recorded)
    )
    await db.upsert_candidate(
        token_factory(contract_address=addr, first_seen_at=earlier_seen)
    )

    cur = await db._conn.execute(
        "SELECT first_seen_at FROM candidates WHERE contract_address = ?",
        (addr,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == earlier_seen.isoformat()
    await db.close()


@pytest.mark.asyncio
async def test_upsert_path_used_is_on_conflict_not_replace(tmp_path, token_factory):
    """Verify the SQL shape via behavioral test:
    INSERT OR REPLACE would have DELETEd the row then re-INSERTed —
    losing any FK dependents and zeroing-out enrichment columns. The
    new ON CONFLICT path leaves the row in place and updates in-place.

    Smoke test: write a row, write enrichment, re-upsert, assert
    enrichment IS preserved. (Stronger version of round-trip test —
    confirms the absence of the historical REPLACE semantics.)
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    addr = "0xupsertshape"

    await db.upsert_candidate(token_factory(contract_address=addr))
    await db._conn.execute(
        # 50000.0 spelled without an underscore separator: SQL underscore
        # literals require SQLite >= 3.46, which not every runtime ships.
        "UPDATE candidates SET liquidity_usd_enriched = 50000.0, "
        "  liquidity_enriched_confidence = 'definite' "
        "WHERE contract_address = ?",
        (addr,),
    )
    await db._conn.commit()
    # Re-upsert (would clobber under REPLACE semantics)
    await db.upsert_candidate(token_factory(contract_address=addr))
    cur = await db._conn.execute(
        "SELECT liquidity_usd_enriched, liquidity_enriched_confidence "
        "FROM candidates WHERE contract_address = ?",
        (addr,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == 50_000.0
    assert row[1] == "definite"
    await db.close()


@pytest.mark.asyncio
async def test_partial_enrichment_preserved_on_reupsert(tmp_path, token_factory):
    """If only some of the 4 enrichment columns are populated (e.g. cron
    wrote `cg_slug_unresolvable` with NULL value), re-upsert must
    preserve BOTH the populated AND the NULL fields, not blanket-NULL
    them based on missing data.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    addr = "0xpartial"

    await db.upsert_candidate(token_factory(contract_address=addr))
    # Cron resolved to unresolvable: confidence set, value/source NULL.
    enriched_at = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "UPDATE candidates SET "
        "  liquidity_enriched_at = ?, "
        "  liquidity_enriched_confidence = 'cg_slug_unresolvable' "
        "WHERE contract_address = ?",
        (enriched_at, addr),
    )
    await db._conn.commit()
    await db.upsert_candidate(token_factory(contract_address=addr))
    cur = await db._conn.execute(
        "SELECT liquidity_usd_enriched, liquidity_enriched_source, "
        "  liquidity_enriched_at, liquidity_enriched_confidence "
        "FROM candidates WHERE contract_address = ?",
        (addr,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] is None  # value was never set; stays NULL
    assert row[1] is None  # source was never set; stays NULL
    assert row[2] == enriched_at  # was set; survives
    assert row[3] == "cg_slug_unresolvable"  # was set; survives
    await db.close()
