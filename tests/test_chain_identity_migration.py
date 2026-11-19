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
    # initialize() already archived the (then empty) table, so drop and retake
    # it now that the row exists.
    await db._conn.execute("DROP TABLE IF EXISTS gainers_comparisons_legacy_prefix_v1")
    await db._conn.commit()

    # The archive is an UNGATED startup step, not part of the migration -- see
    # test_the_archive_is_taken_even_when_the_MIGRATION_MARKER_ALREADY_EXISTS.
    await db.archive_legacy_prefix_comparisons()

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


async def test_the_archive_is_taken_even_when_the_MIGRATION_MARKER_ALREADY_EXISTS(db):
    """The blocker both reviewers found independently — and the same mistake twice.

    The archive was retrofitted INSIDE the migration, whose first action is an
    early-return on its `paper_migrations` marker. So on any database where the
    EARLIER build of this branch had already run, the marker existed, the
    migration returned at `..._skip_already_applied`, and the archive was never
    taken — silently, under a log line that reads like success.

    I had already fixed exactly that gating for the stamping step and left it in
    place for archiving, which is the irreversible half.

    This builds the state the reviewers reproduced: columns present, rows
    stamped legacy, MARKER PRESENT, archives dropped. Every other archive test
    in this file deletes the marker first, so all of them exercise the fresh
    path only and none could see this.
    """
    await _legacy_row(db, "vanar-chain-2", 8736.2)
    for t in ("gainers_comparisons", "losers_comparisons", "trending_comparisons"):
        await db._conn.execute(f"DROP TABLE IF EXISTS {t}_legacy_prefix_v1")
    await db._conn.commit()

    cur = await db._conn.execute(
        "SELECT 1 FROM paper_migrations WHERE name='chain_identity_semantics_v1'"
    )
    assert await cur.fetchone(), "fixture did not reproduce the marker-present state"

    # The migration itself is now a no-op on this DB...
    await db._migrate_chain_identity_semantics_v1()
    cur = await db._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='gainers_comparisons_legacy_prefix_v1'"
    )
    assert not await cur.fetchone(), "fixture invalid: the migration still archived"

    # ...but the ungated step still protects the evidence.
    created = await db.archive_legacy_prefix_comparisons()
    assert "gainers_comparisons_legacy_prefix_v1" in created

    # And it survives what the tracker actually does.
    await db._conn.execute(
        "DELETE FROM gainers_comparisons WHERE coin_id = ?", ("vanar-chain-2",)
    )
    await db._conn.commit()
    cur = await db._conn.execute(
        "SELECT chains_lead_minutes FROM gainers_comparisons_legacy_prefix_v1 "
        "WHERE coin_id='vanar-chain-2'"
    )
    row = await cur.fetchone()
    assert row is not None and row[0] == 8736.2


async def test_archiving_is_idempotent_and_does_not_reset_an_existing_archive(db):
    """Running every startup must not overwrite the snapshot with current state."""
    await _legacy_row(db, "pepe", 1.0)
    first = await db.archive_legacy_prefix_comparisons()
    assert first == {}, "archives already existed from initialize(); nothing to do"

    # A row added AFTER the snapshot must not appear in it, and the snapshot
    # must not be retaken.
    await _legacy_row(db, "post-snapshot", 2.0)
    assert await db.archive_legacy_prefix_comparisons() == {}
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM gainers_comparisons_legacy_prefix_v1 "
        "WHERE coin_id='post-snapshot'"
    )
    assert (await cur.fetchone())[
        0
    ] == 0, "the archive was retaken, so it is no longer the PRE-CUTOVER snapshot"


async def test_concurrent_startup_does_not_crash_the_loser_of_the_archive_race(db):
    """Two processes both see "archive absent" and both proceed to create it.

    Review found this by running it: the `sqlite_master` check and the CREATE
    are separated by an await, and on the first boot after deploy the pipeline,
    the dashboard and several cron entry points all call `initialize()` at
    once. The loser used to raise `table ... already exists` straight out of
    `initialize()`, where no caller catches it.

    The race is made deterministic here rather than hoped for: a competitor
    creates the archive in the exact window, immediately after the guard's
    SELECT has been issued and before the CREATE runs.
    """
    # `initialize()` already archived, so simulate a genuine first boot: drop
    # the archives, then give the surface a row worth preserving. (Skipping
    # this is how the first draft of this test failed for the wrong reason --
    # the competitor collided with a pre-existing table instead of racing.)
    for t in ("gainers", "losers", "trending"):
        await db._conn.execute(f"DROP TABLE IF EXISTS {t}_comparisons_legacy_prefix_v1")
    await db._conn.commit()
    await _legacy_row(db, "vanar-chain-2", 8740.0)

    # The competitor must win AFTER the guard has read sqlite_master, so hook
    # the CREATE itself rather than the SELECT. Hooking the SELECT looks
    # equivalent and is not: aiosqlite runs `fetchall()` later on the
    # connection thread, so the guard would observe the competitor's table,
    # take its `continue` branch, and never reach the CREATE at all -- a test
    # that passes with the fix reverted. It did, until this was corrected.
    real_execute = db._conn.execute
    fired = []

    async def racing_execute(sql, *args, **kwargs):
        if "gainers_comparisons_legacy_prefix_v1 AS SELECT" in sql and not fired:
            fired.append(sql)
            await real_execute(
                "CREATE TABLE gainers_comparisons_legacy_prefix_v1 AS "
                "SELECT * FROM gainers_comparisons"
            )
        return await real_execute(sql, *args, **kwargs)

    db._conn.execute = racing_execute
    try:
        out = await db.archive_legacy_prefix_comparisons()
    finally:
        db._conn.execute = real_execute

    # Pin that the window was actually hit. A mutation run that never mutated
    # and a killed mutant look identical without this.
    assert fired, "the injected race never fired; this test proved nothing"

    # The loser survives, and the winner's archive is intact and complete.
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM gainers_comparisons_legacy_prefix_v1"
    )
    assert (await cur.fetchone())[0] == 1
    assert isinstance(out, dict)
