"""Derived first-seen substrate (retention option F).

Several consumers derived "when did we first see this token" with
``MIN(created_at)`` over the whole of ``signal_events``. That makes RETENTION
the implicit historical boundary: shorten it and the derived minimum silently
moves forward, changing lead-time attribution with no error anywhere. The
ruling therefore requires the derived state FIRST, consumers migrated, parity
proven -- and retention reopened only afterwards, separately. Nothing here
shortens retention.

The correctness argument for migrating the consumers is that MIN over per-token
minima equals MIN over the raw events for the SAME predicate: a token's earliest
event before X is exactly its first_seen when first_seen < X, and the token
contributes nothing under either formulation otherwise. The differential tests
below assert that equality rather than trusting the argument.
"""

from datetime import datetime, timedelta, timezone

import pytest

from scout.chains.events import emit_event
from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


async def _raw_event(db, token_id, created_at, event_type="candidate_scored"):
    """Insert straight into signal_events, bypassing emit_event.

    Used to build history the substrate has not seen, which is how the backfill
    and the differential comparison are exercised.
    """
    await db._conn.execute(
        """INSERT INTO signal_events
           (token_id, pipeline, event_type, event_data, source_module, created_at)
           VALUES (?, 'memecoin', ?, '{}', 'scorer', ?)""",
        (token_id, event_type, created_at),
    )
    await db._conn.commit()


class _ChainsOn:
    """Minimal stand-in for Settings in the one place safe_emit reads it."""

    CHAINS_ENABLED = True


def _chains_on():
    return _ChainsOn()


async def _first_seen(db, token_id):
    cur = await db._conn.execute(
        "SELECT first_seen_at FROM signal_first_seen WHERE token_id = ?", (token_id,)
    )
    row = await cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# The eight conditions the substrate must survive
# ---------------------------------------------------------------------------


async def test_duplicate_events_do_not_move_the_minimum(db):
    await db.record_signal_first_seen("t", "2026-08-01T00:00:00+00:00")
    await db.record_signal_first_seen("t", "2026-08-01T00:00:00+00:00")
    await db.record_signal_first_seen("t", "2026-08-01T00:00:00+00:00")
    await db._conn.commit()
    assert await _first_seen(db, "t") == "2026-08-01T00:00:00+00:00"


async def test_out_of_order_inserts_converge_on_the_true_minimum(db):
    for ts in (
        "2026-08-05T00:00:00+00:00",
        "2026-08-02T00:00:00+00:00",
        "2026-08-09T00:00:00+00:00",
    ):
        await db.record_signal_first_seen("t", ts)
    await db._conn.commit()
    assert await _first_seen(db, "t") == "2026-08-02T00:00:00+00:00"


async def test_a_LATE_event_with_an_EARLIER_timestamp_lowers_the_minimum(db):
    """THE case that rules out an insert-once cache.

    `emit_event` stamps `created_at` in Python, so two concurrent callers can
    interleave; replay and backfill paths write historical rows deliberately.
    An insert-once cache keeps whichever row landed first and is then
    permanently wrong, with nothing to signal it.
    """
    await db.record_signal_first_seen("t", "2026-08-10T00:00:00+00:00")
    await db._conn.commit()
    assert await _first_seen(db, "t") == "2026-08-10T00:00:00+00:00"

    await db.record_signal_first_seen("t", "2026-08-01T00:00:00+00:00")
    await db._conn.commit()
    assert (
        await _first_seen(db, "t") == "2026-08-01T00:00:00+00:00"
    ), "a later-arriving EARLIER event must lower the minimum"


async def test_identical_timestamps_are_stable(db):
    await db.record_signal_first_seen("a", "2026-08-01T00:00:00+00:00")
    await db.record_signal_first_seen("b", "2026-08-01T00:00:00+00:00")
    await db.record_signal_first_seen("a", "2026-08-01T00:00:00+00:00")
    await db._conn.commit()
    assert await _first_seen(db, "a") == "2026-08-01T00:00:00+00:00"
    assert await _first_seen(db, "b") == "2026-08-01T00:00:00+00:00"


async def test_missing_token_returns_nothing_rather_than_a_wrong_value(db):
    assert await _first_seen(db, "never-seen") is None


async def test_order_independence_property(db):
    """Any permutation of the same observations converges on the same value.

    This is the property that makes concurrent and replayed ingestion safe:
    the fold is commutative and idempotent, so interleaving cannot change the
    result.
    """
    import itertools

    stamps = [
        "2026-08-07T00:00:00+00:00",
        "2026-08-03T00:00:00+00:00",
        "2026-08-11T00:00:00+00:00",
    ]
    for i, perm in enumerate(itertools.permutations(stamps)):
        token = f"perm{i}"
        for ts in perm:
            await db.record_signal_first_seen(token, ts)
        await db._conn.commit()
        assert await _first_seen(db, token) == "2026-08-03T00:00:00+00:00"


async def test_migration_rerun_is_idempotent_and_never_raises_the_minimum(db):
    """A rerun must not lose an EARLIER value already folded in.

    The backfill uses ON CONFLICT ... MIN(existing, excluded), so re-running it
    after a late-earlier event has landed cannot overwrite that event with the
    (higher) grouped minimum of whatever history still exists.
    """
    await _raw_event(db, "t", "2026-08-10T00:00:00+00:00")
    # Fold in an earlier observation that is NOT in signal_events.
    await db.record_signal_first_seen("t", "2026-07-01T00:00:00+00:00")
    await db._conn.commit()
    assert await _first_seen(db, "t") == "2026-07-01T00:00:00+00:00"

    # Re-run the backfill: the grouped MIN over signal_events is 2026-08-10,
    # which is LATER. It must not win.
    await db._conn.execute(
        "DELETE FROM paper_migrations WHERE name='signal_first_seen_v1'"
    )
    await db._conn.commit()
    await db._migrate_signal_first_seen_v1()
    assert (
        await _first_seen(db, "t") == "2026-07-01T00:00:00+00:00"
    ), "a migration rerun raised the minimum -- backfill must fold with MIN"


async def test_restart_preserves_the_substrate(db, tmp_path):
    """It is a table, not a cache: a new connection sees the same values."""
    await db.record_signal_first_seen("t", "2026-08-04T00:00:00+00:00")
    await db._conn.commit()
    await db.close()

    reopened = Database(tmp_path / "test.db")
    await reopened.initialize()
    try:
        cur = await reopened._conn.execute(
            "SELECT first_seen_at FROM signal_first_seen WHERE token_id='t'"
        )
        assert (await cur.fetchone())[0] == "2026-08-04T00:00:00+00:00"
    finally:
        await reopened.close()


# ---------------------------------------------------------------------------
# Writer wiring
# ---------------------------------------------------------------------------


async def test_emit_event_populates_the_substrate(db):
    await emit_event(db, "0xabc", "memecoin", "candidate_scored", {}, "scorer")
    assert await _first_seen(db, "0xabc") is not None


async def test_emit_event_keeps_the_earliest_across_many_emits(db):
    await emit_event(db, "0xabc", "memecoin", "candidate_scored", {}, "scorer")
    first = await _first_seen(db, "0xabc")
    assert first is not None, "nothing to compare -- the writer never ran"
    for _ in range(3):
        await emit_event(db, "0xabc", "memecoin", "candidate_scored", {}, "scorer")
    assert await _first_seen(db, "0xabc") == first

    # One row per token, not one per emit.
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM signal_first_seen WHERE token_id='0xabc'"
    )
    assert (await cur.fetchone())[0] == 1


async def test_the_insert_precedes_the_fold(db, monkeypatch):
    """Pins the ORDER, which was deliberately REVERSED after review.

    An earlier version folded first, on the argument that a failing fold then
    left nothing pending. That argument was refuted: `scout/chains/tracker.py`
    opens its own BEGIN on this shared connection and rolls back on failure, so
    a foreign rollback discards this unit's pending work whatever the order.

    Once repair (not prevention) became the correctness mechanism, fold-first
    was strictly worse: it made the DERIVED table a hard dependency of primary
    data, so a substrate failure would silently stop every signal_events write
    via safe_emit's swallow. The event must always land.
    """
    order: list[str] = []
    real_fold = Database.record_signal_first_seen
    real_exec = type(db._conn).execute

    async def traced_fold(self, token_id, created_at):
        order.append("fold")
        return await real_fold(self, token_id, created_at)

    async def traced_exec(self, sql, *a, **kw):
        if "INSERT INTO signal_events" in str(sql):
            order.append("insert")
        return await real_exec(self, sql, *a, **kw)

    monkeypatch.setattr(Database, "record_signal_first_seen", traced_fold)
    monkeypatch.setattr(type(db._conn), "execute", traced_exec)
    await emit_event(db, "0xorder", "memecoin", "candidate_scored", {}, "scorer")

    assert order == [
        "insert",
        "fold",
    ], f"expected the event insert before the substrate fold, got {order}"


async def test_a_substrate_failure_does_not_stop_event_emission(db, monkeypatch):
    """THE reason the order was reversed.

    A derived table must never be able to halt the thing it is derived from.
    With `safe_emit` swallowing, fold-first would have turned any substrate
    fault into a silent, total stop of signal_events writes.
    """
    from scout.chains.events import safe_emit

    monkeypatch.setattr("scout.config.get_settings", lambda: _chains_on())

    async def boom(self, token_id, created_at):
        raise RuntimeError("substrate unavailable")

    monkeypatch.setattr(Database, "record_signal_first_seen", boom)
    await safe_emit(db, "0xa", "memecoin", "candidate_scored", {}, "scorer")
    await safe_emit(db, "0xb", "memecoin", "candidate_scored", {}, "scorer")
    await db._conn.commit()

    cur = await db._conn.execute("SELECT COUNT(*) FROM signal_events")
    assert (await cur.fetchone())[
        0
    ] >= 1, "a substrate fault stopped event emission entirely"


async def test_reconciliation_closes_the_divergence_safe_emit_hides(db, monkeypatch):
    """The honest end-to-end contract: transiently divergent, then repaired.

    `safe_emit` swallows by design, so a failed fold surfaces nowhere. This
    asserts the state production actually reaches -- an orphaned event -- and
    then that the hourly repair removes it. Claiming the orphan is unreachable
    would be the false version of this test.
    """
    from scout.chains.events import safe_emit

    monkeypatch.setattr("scout.config.get_settings", lambda: _chains_on())
    real = Database.record_signal_first_seen

    async def fail_for_dead(self, token_id, created_at):
        if token_id == "0xdead":
            raise RuntimeError("substrate write failed")
        return await real(self, token_id, created_at)

    monkeypatch.setattr(Database, "record_signal_first_seen", fail_for_dead)
    await safe_emit(db, "0xdead", "memecoin", "x", {}, "scorer")
    await safe_emit(db, "0xgood", "memecoin", "x", {}, "scorer")
    await db._conn.commit()

    # The divergence is real -- assert it exists before asserting it is fixed,
    # so the repair below cannot pass against a state that never diverged.
    assert await _first_seen(db, "0xdead") is None
    monkeypatch.setattr(Database, "record_signal_first_seen", real)

    result = await db.reconcile_signal_first_seen()
    assert result["repaired"] >= 1
    cur = await db._conn.execute("SELECT DISTINCT token_id FROM signal_events")
    rows = await cur.fetchall()
    assert rows, "no events committed -- the test proves nothing"
    for (token,) in rows:
        assert (
            await _first_seen(db, token) is not None
        ), f"committed event for {token} still has no substrate row after repair"


# ---------------------------------------------------------------------------
# DIFFERENTIAL PARITY: old signal_events-derived == new substrate-derived
# ---------------------------------------------------------------------------


_FUZZY_OLD = """SELECT MIN(created_at) FROM signal_events
   WHERE (token_id = ? OR LOWER(token_id) = LOWER(?)
          OR LOWER(token_id) LIKE LOWER(? || '%')
          OR LOWER(?) LIKE LOWER(token_id || '%'))
     AND datetime(created_at) < datetime(?, '+5 minutes')"""

_FUZZY_NEW = """SELECT MIN(first_seen_at) FROM signal_first_seen
   WHERE (token_id = ? OR LOWER(token_id) = LOWER(?)
          OR LOWER(token_id) LIKE LOWER(? || '%')
          OR LOWER(?) LIKE LOWER(token_id || '%'))
     AND datetime(first_seen_at) < datetime(?, '+5 minutes')"""


async def _both(db, coin_id, symbol, cutoff):
    old = await (
        await db._conn.execute(_FUZZY_OLD, (coin_id, symbol, symbol, coin_id, cutoff))
    ).fetchone()
    new = await (
        await db._conn.execute(_FUZZY_NEW, (coin_id, symbol, symbol, coin_id, cutoff))
    ).fetchone()
    return old[0], new[0]


async def _rebackfill(db):
    await db._conn.execute(
        "DELETE FROM paper_migrations WHERE name='signal_first_seen_v1'"
    )
    await db._conn.commit()
    await db._migrate_signal_first_seen_v1()


async def test_differential_parity_on_a_representative_cohort(db):
    """The migration's actual acceptance test: identical answers, same inputs.

    Deliberately includes the shapes the fuzzy predicate exists for -- exact
    id, case differences, prefix matches in BOTH directions -- because the
    match set spans several token_ids and a per-token lookup would not be
    equivalent.
    """
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    cohort = [
        ("bless", 0),
        ("bless-network", 3),
        ("BLESS", 1),
        ("blessing-token", 7),
        ("unrelated", 2),
        ("bl", 4),
        ("pepe", 5),
        ("pepe-2", 6),
    ]
    for token, day_offset in cohort:
        # several events per token, deliberately inserted out of order
        for delta in (5, 0, 9):
            ts = (base + timedelta(days=day_offset, hours=delta)).isoformat()
            await _raw_event(db, token, ts)
    await _rebackfill(db)

    cutoff = (base + timedelta(days=30)).isoformat()
    for coin_id, symbol in [
        ("bless", "bless"),
        ("bless-network", "bless"),
        ("BLESS", "bless"),
        ("pepe", "pepe"),
        ("pepe-2", "pepe"),
        ("unrelated", "unrelated"),
        ("bl", "bl"),
        ("nothing-here", "zzzz"),
    ]:
        old, new = await _both(db, coin_id, symbol, cutoff)
        assert old == new, (
            f"parity mismatch for coin_id={coin_id!r} symbol={symbol!r}: "
            f"old={old!r} new={new!r}"
        )


async def test_differential_parity_holds_at_every_cutoff_boundary(db):
    """The upper bound is where min-of-minima could plausibly diverge.

    If a token's first_seen is after the cutoff it must contribute nothing
    under BOTH formulations; if before, it must contribute the same value.
    Sweeping the cutoff across every event boundary is the cheap way to falsify
    the equivalence rather than argue it.
    """
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    for token, days in [("alpha", 0), ("alpha-two", 4), ("beta", 8)]:
        for d in (0, 2):
            await _raw_event(db, token, (base + timedelta(days=days + d)).isoformat())
    await _rebackfill(db)

    for hours in range(0, 24 * 12, 6):
        cutoff = (base + timedelta(hours=hours)).isoformat()
        old, new = await _both(db, "alpha", "alpha", cutoff)
        assert old == new, f"cutoff {cutoff}: old={old!r} new={new!r}"


async def test_parity_survives_a_late_earlier_event_in_both_paths(db):
    """After a late-earlier RAW event, a rebackfill restores parity.

    This is the honest statement of the invariant: the substrate tracks the
    minimum of everything it has been TOLD about. A raw insert that bypasses
    both emit_event and the backfill is invisible to it by construction -- so
    the test asserts what actually holds rather than a stronger claim.
    """
    base = datetime(2026, 8, 10, tzinfo=timezone.utc)
    await _raw_event(db, "gamma", base.isoformat())
    await _rebackfill(db)

    cutoff = (base + timedelta(days=30)).isoformat()
    old, new = await _both(db, "gamma", "gamma", cutoff)
    assert old == new

    earlier = (base - timedelta(days=9)).isoformat()
    await _raw_event(db, "gamma", earlier)
    await _rebackfill(db)
    old, new = await _both(db, "gamma", "gamma", cutoff)
    assert old == new == earlier


async def test_substrate_survives_history_disappearing(db):
    """The point of the whole exercise.

    Once the consumers read the substrate, deleting old `signal_events` rows
    must NOT move the derived minimum. This is what makes a later retention cut
    reviewable on its own terms -- and it is asserted here WITHOUT changing any
    retention setting.
    """
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    await _raw_event(db, "delta", base.isoformat())
    await _raw_event(db, "delta", (base + timedelta(days=40)).isoformat())
    await _rebackfill(db)

    before = await _first_seen(db, "delta")
    assert before == base.isoformat()

    # Simulate retention removing the old history.
    await db._conn.execute(
        "DELETE FROM signal_events WHERE created_at < ?",
        ((base + timedelta(days=10)).isoformat(),),
    )
    await db._conn.commit()

    cur = await db._conn.execute("SELECT MIN(created_at) FROM signal_events")
    raw_after = (await cur.fetchone())[0]
    assert raw_after != before, "precondition: the raw minimum must have moved"

    assert await _first_seen(db, "delta") == before, (
        "the derived minimum moved when history was pruned -- the substrate is "
        "not actually decoupled from retention"
    )
