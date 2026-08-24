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
    why a dedicated connection is an escape rather than a relocation.

    ASSERTS BOTH HALVES, and the second half is why. A reviewer measured what
    this harness actually does: the sibling's uncommitted INSERT holds a
    RESERVED lock, so the dedicated connection hits SQLITE_BUSY, waits out
    `_BASELINE_WRITE_TIMEOUT_MS`, swallows the error and returns None having
    written nothing. `survivors == 0` was therefore satisfied by a write that
    never happened -- "clean" and "did nothing" were the same observation, and
    the docstring's claims about the read-back were never reached.

    So the outcome must be HONEST as well as clean: no laundering AND the
    payload admits the write was lost.
    """

    async def subject():
        return await db._record_coverage_baseline("gainers_comparisons", 0.5, 100)

    result = {}

    async def run():
        result["returned"] = await subject()

    survivors = await _survives_sibling_rollback(db, run)
    assert survivors == 0, (
        "the coverage-mark write laundered a sibling's uncommitted INSERT -- "
        "it is committing on the shared connection again"
    )
    # Under a held sibling transaction the write CANNOT succeed. It must say so
    # rather than reporting a value it never stored.
    assert result["returned"] is None, (
        "the contended write reported a stored value; under a sibling's "
        "RESERVED lock nothing can have been written"
    )
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM recompute_coverage_baseline "
        "WHERE source_table='gainers_comparisons'"
    )
    assert (await cur.fetchone())[0] == 0, "a row exists that could not have been written"


async def test_the_write_WAITS_OUT_transient_contention(db, tmp_path):
    """`_BASELINE_WRITE_TIMEOUT_MS` must actually buy something.

    Setting it to 0 survived every test in this file, and a reviewer showed the
    component suite runs ~9s FASTER under that mutant because the dead waits
    vanish. In production it means every write contended by the pipeline
    starves, the ratchet mark is never recorded, and collapse detection is
    permanently unarmed -- the `6276d586` consequence reached by a different
    route, and invisible to a suite that only ever tests the uncontended and
    the permanently-contended cases.

    This is the case in between: contention that CLEARS. The sibling holds its
    write briefly and rolls back; the dedicated connection should wait and then
    succeed. With the timeout at 0 it gives up instead.
    """
    import asyncio

    conn = db._conn
    await conn.execute("CREATE TABLE IF NOT EXISTS sibling_work (v TEXT)")
    await conn.commit()
    await conn.execute("INSERT INTO sibling_work VALUES ('brief')")

    async def release_soon():
        await asyncio.sleep(0.2)
        await conn.rollback()

    releaser = asyncio.create_task(release_soon())
    stored = await db._record_coverage_baseline("gainers_comparisons", 0.5, 100)
    await releaser

    assert stored == pytest.approx(0.5), (
        "the write gave up on contention that cleared well inside the timeout "
        "-- the timeout is not buying what it claims to"
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


async def test_the_coverage_baseline_CLEAR_does_not_launder_either(db):
    """`_clear_coverage_baseline` is the sibling write on the same seam.

    The file only ever exercised `_record_coverage_baseline`, but the clear
    also commits, and getting *these writes* off the shared connection was
    #559's whole point. A reviewer showed the omission is live: moving the
    clear back onto `self._conn` and neutralising its close survives the entire
    7,135-test suite.

    One extra subject closes it. The two functions are three lines apart and
    only one was covered -- the same "closed one branch, not the axis" shape
    that produced this PR's other findings.
    """
    await db._conn.execute(
        "INSERT INTO recompute_coverage_baseline "
        "(source_table, best_rate, population, recorded_at) "
        "VALUES ('gainers_comparisons', 0.9, 100, '2026-08-01T00:00:00+00:00')"
    )
    await db._conn.commit()

    result = {}

    async def subject():
        result["returned"] = await db._clear_coverage_baseline("gainers_comparisons")

    survivors = await _survives_sibling_rollback(db, subject)
    assert survivors == 0, (
        "the coverage-mark CLEAR laundered a sibling's uncommitted INSERT -- "
        "it is committing on the shared connection"
    )
    # The SAME vacuity that was fixed for the write, three lines away in the
    # source and in the same commit -- and not carried across to its sibling.
    # Under the sibling's RESERVED lock the clear starves for the full timeout,
    # swallows, returns False and deletes nothing, so `survivors == 0` is again
    # satisfied by a write that never happened. The outcome must be HONEST.
    assert result["returned"] is False, (
        "the contended clear reported success; under a sibling's RESERVED lock "
        "nothing can have been deleted"
    )
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM recompute_coverage_baseline "
        "WHERE source_table='gainers_comparisons'"
    )
    assert (await cur.fetchone())[0] == 1, (
        "the row was deleted despite the clear reporting failure"
    )


async def test_the_CLEAR_waits_out_transient_contention(db):
    """The clear-side analogue of the write's transient-contention test.

    Without it, `PRAGMA busy_timeout = 0` on the clear's connection survives
    the whole suite -- verified by a reviewer. In production that means an
    incomparable mark is never cleared whenever the pipeline holds the lock, so
    the probe returns `incomparable_unresolved` forever and that surface is
    never judged again. Silent, and permanent.

    Note the two adjacent functions have SEPARATE busy_timeout PRAGMAs and
    there are nine such sites in the tree -- two mutation attempts by that
    reviewer landed on the wrong one. Adjacency is what makes this class of
    omission easy.
    """
    import asyncio

    await db._conn.execute(
        "INSERT INTO recompute_coverage_baseline "
        "(source_table, best_rate, population, recorded_at) "
        "VALUES ('gainers_comparisons', 0.9, 100, '2026-08-01T00:00:00+00:00')"
    )
    await db._conn.commit()

    conn = db._conn
    await conn.execute("CREATE TABLE IF NOT EXISTS sibling_work (v TEXT)")
    await conn.commit()
    await conn.execute("INSERT INTO sibling_work VALUES ('brief')")

    async def release_soon():
        await asyncio.sleep(0.2)
        await conn.rollback()

    releaser = asyncio.create_task(release_soon())
    cleared = await db._clear_coverage_baseline("gainers_comparisons")
    await releaser

    assert cleared is True, (
        "the clear gave up on contention that cleared well inside the timeout "
        "-- an incomparable mark would then never be cleared while the "
        "pipeline is writing, and the surface is never judged again"
    )
