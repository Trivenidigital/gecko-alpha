"""Evidence preservation for the identity-semantics cutover (ruling C).

> Do not overwrite historical lead-time evidence in place. Preserve old results
> as legacy_prefix_semantics or equivalent.

The marker alone does NOT satisfy that, which is what review caught: the
trackers do not UPDATE these rows, they `DELETE FROM <table> WHERE coin_id = ?`
and re-INSERT on every recompute. Before the semantics change that was
value-neutral; after it, the recompute writes canonical values over a legacy row
and the original is simply gone -- "an UPDATE that destroys the previous
evidence", spelled DELETE+INSERT.

The first time `vanar-chain-2` re-enters the 24h gainers window, its +6.07-day
fabricated lead disappears: the single most valuable falsification artefact the
impact study produced.
"""

import pytest

from scout.db import Database
from scout.identity import CANONICAL_SEMANTICS, LEGACY_SEMANTICS


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


async def _legacy_row(db, coin_id, lead):
    await db._conn.execute(
        """INSERT INTO gainers_comparisons
           (coin_id, symbol, name, appeared_on_gainers_at,
            detected_by_chains, chains_lead_minutes, is_gap, created_at)
           VALUES (?, 'X', 'X', '2026-08-01T00:00:00+00:00', 1, ?, 0,
                   '2026-08-01T00:00:00+00:00')""",
        (coin_id, lead),
    )
    await db._conn.commit()


async def test_existing_rows_are_stamped_legacy_not_recomputed(db):
    """The stamp is a MARKER. It must not touch a single evidence column."""
    await _legacy_row(db, "vanar-chain-2", 8736.2)
    await db._conn.execute(
        "UPDATE gainers_comparisons SET chains_identity_semantics=NULL"
    )
    await db._conn.execute(
        "DELETE FROM paper_migrations WHERE name='chain_identity_semantics_v1'"
    )
    await db._conn.commit()

    await db._migrate_chain_identity_semantics_v1()

    cur = await db._conn.execute(
        "SELECT chains_identity_semantics, detected_by_chains, chains_lead_minutes "
        "FROM gainers_comparisons WHERE coin_id='vanar-chain-2'"
    )
    row = await cur.fetchone()
    assert row[0] == LEGACY_SEMANTICS
    assert row[1] == 1, "the stamp altered detected_by_chains"
    assert row[2] == 8736.2, "the stamp altered the historical lead"


async def test_the_legacy_rows_are_ARCHIVED_before_a_tracker_can_delete_them(db):
    """THE fix for the blocker. The marker alone does not survive DELETE+INSERT."""
    await _legacy_row(db, "vanar-chain-2", 8736.2)
    await db._conn.execute(
        "DELETE FROM paper_migrations WHERE name='chain_identity_semantics_v1'"
    )
    await db._conn.execute("DROP TABLE IF EXISTS gainers_comparisons_legacy_prefix_v1")
    await db._conn.commit()

    await db._migrate_chain_identity_semantics_v1()

    # Now simulate what the tracker actually does on every recompute.
    await db._conn.execute(
        "DELETE FROM gainers_comparisons WHERE coin_id = ?", ("vanar-chain-2",)
    )
    await db._conn.commit()

    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM gainers_comparisons WHERE coin_id='vanar-chain-2'"
    )
    assert (await cur.fetchone())[0] == 0, "fixture failed to reproduce the deletion"

    cur = await db._conn.execute(
        "SELECT chains_lead_minutes FROM gainers_comparisons_legacy_prefix_v1 "
        "WHERE coin_id='vanar-chain-2'"
    )
    row = await cur.fetchone()
    assert row is not None, "the fabricated-lead artefact was destroyed"
    assert row[0] == 8736.2


async def test_archive_covers_all_three_surfaces(db):
    for t in ("gainers_comparisons", "losers_comparisons", "trending_comparisons"):
        cur = await db._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (f"{t}_legacy_prefix_v1",),
        )
        assert await cur.fetchone(), f"{t} has no legacy archive"


async def test_rerun_does_not_clobber_canonical_rows_back_to_legacy(db):
    await _legacy_row(db, "pepe", 10.0)
    await db._conn.execute(
        "UPDATE gainers_comparisons SET chains_identity_semantics=? WHERE coin_id='pepe'",
        (CANONICAL_SEMANTICS,),
    )
    await db._conn.execute(
        "DELETE FROM paper_migrations WHERE name='chain_identity_semantics_v1'"
    )
    await db._conn.commit()

    await db._migrate_chain_identity_semantics_v1()

    cur = await db._conn.execute(
        "SELECT chains_identity_semantics FROM gainers_comparisons WHERE coin_id='pepe'"
    )
    assert (await cur.fetchone())[0] == CANONICAL_SEMANTICS


async def test_unmarked_rows_are_stamped_even_after_the_migration_marker_exists(db):
    """The rollback hole: old code writes NULL, the one-shot migration never returns.

    NULL would otherwise become a third, undocumented semantics state.
    """
    await _legacy_row(db, "rollback-row", 5.0)
    await db._conn.execute(
        "UPDATE gainers_comparisons SET chains_identity_semantics=NULL "
        "WHERE coin_id='rollback-row'"
    )
    await db._conn.commit()

    # Migration marker is present, so the migration itself is a no-op here.
    await db._migrate_chain_identity_semantics_v1()
    cur = await db._conn.execute(
        "SELECT chains_identity_semantics FROM gainers_comparisons WHERE coin_id='rollback-row'"
    )
    assert (await cur.fetchone())[0] is None, "fixture did not reproduce the hole"

    stamped = await db.stamp_unmarked_chain_semantics()
    assert stamped.get("gainers_comparisons") == 1
    cur = await db._conn.execute(
        "SELECT chains_identity_semantics FROM gainers_comparisons WHERE coin_id='rollback-row'"
    )
    assert (await cur.fetchone())[0] == LEGACY_SEMANTICS


async def test_stamping_with_nothing_to_do_leaves_no_open_transaction(db):
    """A zero-row UPDATE still opens a transaction; skipping the commit dangles it.

    That is what broke the NEXT migration's BEGIN EXCLUSIVE with "cannot start a
    transaction within a transaction" -- the same `if rows:` shape that has bitten
    this codebase before.
    """
    assert await db.stamp_unmarked_chain_semantics() == {}
    # Must not raise.
    await db._conn.execute("BEGIN EXCLUSIVE")
    await db._conn.execute("ROLLBACK")
