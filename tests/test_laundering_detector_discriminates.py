"""A detector that only ever says "clean" has not been shown to say anything else.

The structural reviewer's positive control, promoted out of scratch because the
reasoning outlives this PR.

`#559` moved the coverage-mark write onto its own connection to escape the
shared-connection laundering seam: on the shared connection, `commit()` commits
whatever a sibling coroutine has pending, so a sibling's `rollback()` silently
becomes a partial commit. Asking "is `_record_coverage_baseline` clean?" and
getting "yes" proves nothing on its own -- a broken detector returns "clean"
for everything. So the same method is run, in the same process at the same
revision, against a subject that DOES launder.

Deliberately NOT asserted here: `db.cache_prices` currently launders on its
dominant path (`main.py`, every pipeline cycle, concurrently with
`run_chain_tracker`'s bare `BEGIN`/`rollback()`). That is real, pre-existing,
and tracked as BL-NEW-CACHE-PRICES-SHARED-CONNECTION-LAUNDERING. It is not used
as the positive control because a test whose control is a live bug starts
failing the day the bug is fixed -- it would pin the defect in place and punish
the repair. The synthetic launderer below cannot be "fixed" out from under the
test.
"""

import pytest

from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


async def _survives_sibling_rollback(db, op) -> int:
    """Run `op`, then roll the sibling back. Returns surviving sibling rows.

    Identical method for every subject. A sibling coroutine leaves an
    uncommitted INSERT on the shared connection; the operation under test runs;
    the sibling rolls back. If the operation committed on the shared
    connection, it laundered the sibling's write and the row survives a
    rollback that should have discarded it.
    """
    conn = db._conn
    await conn.execute("CREATE TABLE IF NOT EXISTS sibling_work (v TEXT)")
    await conn.commit()
    await conn.execute("DELETE FROM sibling_work")
    await conn.commit()

    await conn.execute("INSERT INTO sibling_work VALUES ('half-finished')")
    await op()
    await conn.rollback()

    cur = await conn.execute("SELECT COUNT(*) FROM sibling_work")
    return (await cur.fetchone())[0]


async def test_the_detector_can_actually_detect_laundering(db):
    """Positive control. Without this the clean result below is unfalsifiable."""

    async def launders():
        # The seam itself, in miniature: any commit on the SHARED connection
        # also commits the sibling's pending INSERT.
        await db._conn.execute("CREATE TABLE IF NOT EXISTS unrelated (v TEXT)")
        await db._conn.commit()

    survivors = await _survives_sibling_rollback(db, launders)
    assert survivors == 1, (
        "the harness reported clean against a subject that definitely "
        "launders -- it cannot tell the two apart, so any clean verdict it "
        "produces is meaningless"
    )


async def test_the_coverage_baseline_write_does_not_launder(db):
    """The invariant #559 established: the ratchet write is on its own connection.

    Connection A's COMMIT cannot commit connection B's pending work, which is
    why a dedicated connection is an escape rather than a relocation -- and why
    the read-back inside `_record_coverage_baseline` is sound here while the
    same pattern on a shared connection was not.
    """

    async def subject():
        await db._record_coverage_baseline("gainers_comparisons", 0.5, 100)

    survivors = await _survives_sibling_rollback(db, subject)
    assert survivors == 0, (
        "the coverage-mark write laundered a sibling's uncommitted INSERT -- "
        "it is committing on the shared connection again"
    )


async def test_the_baseline_write_still_actually_wrote(db):
    """Guards the cheap way to pass the test above: doing nothing at all.

    A no-op launders nothing, so the clean verdict has to be paired with proof
    the write happened.
    """
    await db._record_coverage_baseline("gainers_comparisons", 0.5, 100)
    cur = await db._conn.execute(
        "SELECT best_rate, population FROM recompute_coverage_baseline "
        "WHERE source_table='gainers_comparisons'"
    )
    row = await cur.fetchone()
    assert row is not None, "no baseline row was written at all"
    assert row[0] == pytest.approx(0.5)
    assert row[1] == 100
