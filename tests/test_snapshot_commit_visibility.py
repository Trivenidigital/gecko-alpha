"""B1-residual: durable post-commit visibility for price snapshots.

`source_call_price_snapshots.created_at` is stamped at INSERT while the writer
commits a whole cycle at once, so a row inserted before an `as_of` and committed
after it satisfied every knowability bound while having been genuinely unknowable
at `as_of`. These tests pin the fix: a row counts only once a marker written in a
SEPARATE transaction AFTER the data commit exists at or before `as_of`.

The asymmetry is the whole design and each test names which side it defends:
conservative LATE visibility is acceptable, FUTURE LEAKAGE is not.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.source_quality.ledger import _fetch_snapshot_rows
from scout.source_quality.snapshot_writer import _allocate_batch_id, _publish_batch

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
KEY = "ethereum|0xabc"


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "vis.db")
    await d.initialize()
    yield d
    await d.close()


async def _insert_snapshot(db, *, created_at, snapshot_at, batch_id, price=1.0):
    await db._conn.execute(
        "INSERT INTO source_call_price_snapshots "
        "(identity_key, identity_kind, chain, price, snapshot_at, source, "
        " created_at, batch_id) "
        "VALUES (?, 'contract', 'ethereum', ?, ?, 'gt', ?, ?)",
        (KEY, price, snapshot_at.isoformat(), created_at.isoformat(), batch_id),
    )
    await db._conn.commit()


async def _read(db, as_of):
    return await _fetch_snapshot_rows(
        db._conn, KEY, identity_kind="contract", as_of=as_of
    )


async def _set_epoch(db, ts):
    await db._conn.execute(
        "UPDATE source_call_snapshot_visibility_epoch SET epoch_cutover_ts = ? "
        "WHERE id = 1",
        (ts.isoformat(),),
    )
    await db._conn.commit()


# --------------------------------------------------------------------------
# THE DEFECT ITSELF
# --------------------------------------------------------------------------


async def test_row_inserted_before_as_of_but_committed_after_is_not_visible(db):
    """THE B1-RESIDUAL SCENARIO, exactly.

    Insert timestamp and snapshot timestamp both precede `as_of` — the row
    passes every pre-existing bound. Its batch is published AFTER `as_of`, so it
    was not knowable then, and the reader must exclude it. Before this mechanism
    the same row was returned, and a historical feature could read a price the
    decision could not have seen.
    """
    as_of = NOW.isoformat()
    await _insert_snapshot(
        db,
        created_at=NOW - timedelta(minutes=10),  # inserted BEFORE as_of
        snapshot_at=NOW - timedelta(minutes=10),  # observed BEFORE as_of
        batch_id=1,
    )
    # ... but the cycle only committed, and published, afterwards.
    await _publish_batch(
        db._conn,
        batch_id=1,
        visible_at=(NOW + timedelta(minutes=5)).isoformat(),
        rows_written=1,
    )

    assert await _read(db, as_of) == [], "future leakage: unknowable row returned"

    # Both legacy bounds are satisfied — proving the exclusion comes from the
    # visibility gate and not from some other filter.
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM source_call_price_snapshots "
        "WHERE identity_key = ? AND snapshot_at <= ? AND created_at <= ?",
        (KEY, as_of, as_of),
    )
    assert (await cur.fetchone())[0] == 1


async def test_the_same_row_becomes_visible_once_as_of_passes_the_marker(db):
    """Conservative LATE visibility is the accepted cost: the row is not lost,
    it simply becomes knowable at the marker rather than at insert."""
    await _insert_snapshot(
        db,
        created_at=NOW - timedelta(minutes=10),
        snapshot_at=NOW - timedelta(minutes=10),
        batch_id=1,
    )
    visible_at = NOW + timedelta(minutes=5)
    await _publish_batch(
        db._conn, batch_id=1, visible_at=visible_at.isoformat(), rows_written=1
    )

    assert await _read(db, NOW.isoformat()) == []
    later = await _read(db, (visible_at + timedelta(seconds=1)).isoformat())
    assert len(later) == 1 and later[0]["price"] == pytest.approx(1.0)


async def test_unpublished_batch_is_invisible_at_every_as_of(db):
    """CONSERVATIVE CRASH CASE: the process died between the data commit and the
    marker commit.

    The rows are durable but no marker exists. INVISIBLE IS THE ACCEPTED
    OUTCOME — the alternative (assume visible) is exactly the leak. They stay
    invisible until an operator repair stamps the marker.
    """
    await _insert_snapshot(
        db,
        created_at=NOW - timedelta(hours=2),
        snapshot_at=NOW - timedelta(hours=2),
        batch_id=7,  # stamped, never published
    )

    for probe in (NOW, NOW + timedelta(days=365)):
        assert await _read(db, probe.isoformat()) == [], "orphan batch became visible"

    # And the documented repair restores them, so the data is recoverable.
    await _publish_batch(
        db._conn, batch_id=7, visible_at=NOW.isoformat(), rows_written=1
    )
    assert len(await _read(db, (NOW + timedelta(seconds=1)).isoformat())) == 1


# --------------------------------------------------------------------------
# EPOCH RULE — do not strand existing history, do not grandfather future bugs
# --------------------------------------------------------------------------


async def test_pre_epoch_rows_remain_readable(db):
    """Every row in production today has batch_id NULL. Stranding them would
    delete the substrate's whole history at migration time."""
    await _set_epoch(db, NOW)
    await _insert_snapshot(
        db,
        created_at=NOW - timedelta(days=30),  # written long before the mechanism
        snapshot_at=NOW - timedelta(days=30),
        batch_id=None,
    )
    rows = await _read(db, NOW.isoformat())
    assert len(rows) == 1, "pre-epoch history was stranded by the migration"


async def test_post_epoch_unstamped_rows_are_not_grandfathered(db):
    """The other half of the epoch rule, and the one that keeps it honest.

    A NULL-batch row created AFTER the cutover is a writer bug. Grandfathering
    NULL unconditionally would mean any future stamping regression silently
    restores the original always-visible leak — so the conservative reading of a
    bug is INVISIBLE.
    """
    await _set_epoch(db, NOW - timedelta(days=1))
    await _insert_snapshot(
        db,
        created_at=NOW,  # AFTER the cutover, yet unstamped
        snapshot_at=NOW,
        batch_id=None,
    )
    assert (
        await _read(db, (NOW + timedelta(days=1)).isoformat()) == []
    ), "an unstamped post-epoch row was treated as always-visible"


# --------------------------------------------------------------------------
# READER SPLIT — decision readers gate, coverage readers must not
# --------------------------------------------------------------------------


async def test_reads_without_as_of_are_ungated(db):
    """Coverage/observability readers must see committed rows immediately.

    Gating them would make coverage UNDER-REPORT during the marker-lag window
    and page an operator about a lane that is working correctly.
    """
    await _insert_snapshot(
        db,
        created_at=NOW,
        snapshot_at=NOW,
        batch_id=3,  # unpublished
    )
    ungated = await _fetch_snapshot_rows(db._conn, KEY, identity_kind="contract")
    assert len(ungated) == 1, "a non-as-of read was gated"
    assert await _read(db, (NOW + timedelta(days=1)).isoformat()) == []


# --------------------------------------------------------------------------
# WRITER ORDERING
# --------------------------------------------------------------------------


async def test_marker_exactly_at_as_of_is_visible(db):
    """Boundary, pinned deliberately: `visible_at == as_of` counts as VISIBLE.

    Surfaced by a surviving mutation (`<=` -> `<`). Neither side leaks the
    future, so this is a consistency decision rather than a safety one — and the
    predicate's other two bounds (`snapshot_at <= as_of`, `created_at <= epoch`)
    are inclusive, so this one is too. Pinned so it cannot drift silently into
    the other convention.
    """
    await _insert_snapshot(
        db,
        created_at=NOW - timedelta(minutes=10),
        snapshot_at=NOW - timedelta(minutes=10),
        batch_id=1,
    )
    await _publish_batch(
        db._conn, batch_id=1, visible_at=NOW.isoformat(), rows_written=1
    )

    assert len(await _read(db, NOW.isoformat())) == 1, "exact-boundary marker excluded"
    one_earlier = (NOW - timedelta(microseconds=1)).isoformat()
    assert await _read(db, one_earlier) == [], "a marker in the future was admitted"


async def test_each_writer_cycle_publishes_its_own_batch(db, settings_factory):
    """CYCLE N, not cycle 1 — through the REAL writer, not the helper.

    Answers the standing blind-spot question directly: markers must keep being
    published as cycles accumulate, each cycle's rows must carry that cycle's id,
    and an `as_of` between two cycles must see the first and not the second.
    """
    from test_source_call_snapshot_writer import (
        RecordingFetcher,
        RecordingResolver,
        _insert_source_call,
        _seed_price_cache,
    )

    from scout.source_quality.snapshot_writer import write_price_snapshots

    s = settings_factory()
    await _insert_source_call(
        db._conn,
        event_id="cyc",
        resolved_state="resolved",
        call_ts=(NOW - timedelta(hours=1)).isoformat(),
        source_type="tg",
        token_id="cycler",
    )

    seen_batches = []
    for run in range(3):
        await _seed_price_cache(
            db._conn,
            "cycler",
            1.0 + run,
            (NOW - timedelta(minutes=2) + timedelta(seconds=run)).isoformat(),
        )
        stats = await write_price_snapshots(
            db._conn,
            now=NOW + timedelta(minutes=run),
            resolve_pool=RecordingResolver(result=None),
            fetch_ohlcv=RecordingFetcher(),
        )
        assert stats["cg_snapshots_written"] == 1, f"cycle {run} wrote nothing"
        seen_batches.append(stats["batch_id"])

    assert seen_batches == sorted(set(seen_batches)), "batch ids repeated or went back"

    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM source_call_snapshot_batches WHERE batch_id IN "
        "(?, ?, ?)",
        tuple(seen_batches),
    )
    assert (await cur.fetchone())[0] == 3, "a later cycle stopped publishing markers"

    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM source_call_price_snapshots WHERE batch_id IS NULL"
    )
    assert (await cur.fetchone())[0] == 0, "the writer emitted an unstamped row"


async def test_the_live_lane_cannot_price_from_an_unpublished_batch(db):
    """B-1: the LANE, not the helper.

    The gate was fully correct and completely dead: the live call site passed
    "contract" as the third POSITIONAL, binding `identity_kind` and leaving
    `as_of` at None, so production skipped the predicate entirely. Every test I
    had called the helper directly with `as_of=`, so all of them passed while the
    real lane leaked. This drives `refresh_source_call_outcomes` itself.
    """
    from scout.source_quality.ledger import refresh_source_call_outcomes

    call_ts = NOW - timedelta(hours=2)
    await db._conn.execute(
        "INSERT INTO source_calls "
        "(source_type, source_id, source_event_id, token_id, symbol, "
        " contract_address, chain, call_ts, call_kind, cluster_identity, "
        " cluster_identity_kind, duplicate_cluster_key, resolved_state, "
        " outcome_status, missing_fields) "
        "VALUES ('tg','kol','b1-evt',NULL,NULL,'0xabc','ethereum',?, "
        " 'ca_call','cid','contract','dck-b1','resolved','pending','[]')",
        (call_ts.isoformat(),),
    )
    # A price that WOULD resolve the call — in a batch that was never published.
    await _insert_snapshot(
        db,
        created_at=call_ts,
        snapshot_at=call_ts,
        batch_id=42,  # stamped, no marker
        price=5.0,
    )
    await db._conn.commit()

    await refresh_source_call_outcomes(db._conn, now=NOW + timedelta(days=2))

    cur = await db._conn.execute(
        "SELECT price_at_call FROM source_calls WHERE source_event_id = 'b1-evt'"
    )
    priced = (await cur.fetchone())[0]
    assert priced is None, (
        "the live lane priced a call from an UNPUBLISHED batch — the gate is "
        "not reaching production"
    )


async def test_epoch_grandfather_uses_production_shaped_created_at(db):
    """B-2: the fixture must insert EXACTLY as production does.

    `created_at` in production comes from the DDL default `datetime('now')` —
    space-separated. My original fixtures supplied a Python `.isoformat()`
    string, a shape production never produces, so they could not see that the
    epoch comparison was mixing formats: ' ' (0x20) sorts before 'T' (0x54), so
    a Python-stamped cutover was later than every same-day SQLite timestamp and
    grandfathered rows it had to exclude.

    Here the DDL default fires — no created_at supplied at all.
    """
    # The epoch here is whatever THE MIGRATION stamped — deliberately NOT
    # overridden. An earlier version of this test set the cutover itself, which
    # made it blind to the producer bug: overriding the value replaces the very
    # thing under test. The row below is created microseconds AFTER that epoch,
    # so it must NOT be grandfathered; under a Python-stamped cutover the
    # space-vs-'T' ordering says it was, and this test fails.
    await db._conn.execute(
        "INSERT INTO source_call_price_snapshots "
        "(identity_key, identity_kind, chain, price, snapshot_at, source, batch_id) "
        "VALUES (?, 'contract', 'ethereum', 9.0, ?, 'gt', NULL)",
        (KEY, (NOW - timedelta(days=2)).isoformat()),
    )
    await db._conn.commit()

    cur = await db._conn.execute(
        "SELECT created_at FROM source_call_price_snapshots WHERE identity_key = ?",
        (KEY,),
    )
    created_at = (await cur.fetchone())[0]
    assert "T" not in created_at, (
        f"fixture is not production-shaped: {created_at!r} — production writes "
        "the DDL default datetime('now')"
    )

    assert (
        await _read(db, (NOW + timedelta(days=10)).isoformat()) == []
    ), "a post-epoch unstamped row was grandfathered via a format mismatch"


async def test_epoch_boundary_is_strict_at_the_same_second(db):
    """F-1: the strict `<` epoch bound, pinned from BOTH sides.

    `datetime('now')` has one-second resolution, so a row written in the same
    second as the migration compares EQUAL to the cutover; under `<=` it would be
    grandfathered — treated as always-visible — which is the admitting direction
    and the fourth leak found this round. Flipping back to `<=` left all 70 tests
    green, so the bound was undefended.

    COUPLING, worth knowing before anyone "simplifies" either half: under `<=`
    the COALESCE fallback is what keeps the missing-epoch case closed, because
    the `s.created_at` variant would then compare `created_at <= created_at` and
    fail WIDE OPEN. The `''` fallback does not depend on the operator's
    strictness; this one test defends both guards.
    """
    cur = await db._conn.execute(
        "SELECT epoch_cutover_ts FROM source_call_snapshot_visibility_epoch WHERE id=1"
    )
    epoch = (await cur.fetchone())[0]
    # SQLite arithmetic keeps the same producer shape — no format mixing here.
    cur = await db._conn.execute("SELECT datetime(?, '-1 second')", (epoch,))
    one_earlier = (await cur.fetchone())[0]

    for label, created in (
        ("same-second", epoch),
        ("one second earlier", one_earlier),
    ):
        await db._conn.execute("DELETE FROM source_call_price_snapshots")
        await db._conn.execute(
            "INSERT INTO source_call_price_snapshots "
            "(identity_key, identity_kind, chain, price, snapshot_at, source, "
            " created_at) "
            "VALUES (?, 'contract', 'ethereum', 1.0, ?, 'gt', ?)",
            (KEY, NOW.isoformat(), created),
        )
        await db._conn.commit()

        seen = await _read(db, (NOW + timedelta(days=400)).isoformat())
        if label == "same-second":
            assert seen == [], "same-second row grandfathered — the bound is <="
        else:
            assert len(seen) == 1, "a genuinely pre-epoch row was stranded"


async def test_missing_epoch_row_fails_closed(db):
    """I-2: no epoch row must admit NOTHING, not everything."""
    await db._conn.execute("DELETE FROM source_call_snapshot_visibility_epoch")
    await db._conn.execute(
        "INSERT INTO source_call_price_snapshots "
        "(identity_key, identity_kind, chain, price, snapshot_at, source, batch_id) "
        "VALUES (?, 'contract', 'ethereum', 1.0, ?, 'gt', NULL)",
        (KEY, (NOW - timedelta(days=5)).isoformat()),
    )
    await db._conn.commit()

    assert (
        await _read(db, (NOW + timedelta(days=10)).isoformat()) == []
    ), "a missing epoch row failed OPEN"


async def test_writer_data_is_already_durable_when_the_marker_is_published(
    db, tmp_path, monkeypatch
):
    """I-3 / N3, on the WRITER — the version that actually discriminates.

    My first attempt exercised the test helper, not `write_price_snapshots`, so
    the combined-commit mutation sailed through it. The discriminator has to be
    observed from OUTSIDE the writer's connection at the moment of publish: under
    the shipped ordering the data commit has already happened, so a second
    connection sees the rows; under a combined commit it sees none.
    """
    import aiosqlite

    from scout.source_quality import snapshot_writer as SW

    from test_source_call_snapshot_writer import (
        RecordingFetcher,
        RecordingResolver,
        _insert_source_call,
        _seed_price_cache,
    )

    # CONTRACT identity, deliberately. The CG lane commits immediately after its
    # own writes, so CG rows are durable before publish no matter what the
    # contract loop does — a CG fixture cannot see this ordering at all. Only
    # contract rows depend on the final commit that precedes the marker.
    from test_source_call_snapshot_writer import _candle, _pool

    await _insert_source_call(
        db._conn,
        event_id="n3",
        resolved_state="eligible_contract",
        call_ts=(NOW - timedelta(hours=1)).isoformat(),
        source_type="tg",
        contract_address="0xdurable",
        chain="ethereum",
    )

    observed: dict[str, int] = {}
    real_publish = SW._publish_batch

    async def spy(conn, *, batch_id, visible_at, rows_written):
        outside = await aiosqlite.connect(tmp_path / "vis.db")
        try:
            cur = await outside.execute(
                "SELECT COUNT(*) FROM source_call_price_snapshots WHERE batch_id = ?",
                (batch_id,),
            )
            observed["rows_visible_outside"] = (await cur.fetchone())[0]
        finally:
            await outside.close()
        return await real_publish(
            conn, batch_id=batch_id, visible_at=visible_at, rows_written=rows_written
        )

    monkeypatch.setattr(SW, "_publish_batch", spy)
    stats = await SW.write_price_snapshots(
        db._conn,
        now=NOW,
        resolve_pool=RecordingResolver(result=_pool(network="eth", pool_address="P")),
        fetch_ohlcv=RecordingFetcher(result=[_candle(close=3.3)]),
    )

    assert stats["snapshots_written"] == 1
    assert observed.get("rows_visible_outside") == 1, (
        "the marker was published while the data was still uncommitted — a "
        "second connection could not see the rows the marker makes visible"
    )


async def test_duplicate_batch_id_raises_instead_of_being_discarded(db):
    """I-1: a batch-id collision must be LOUD.

    `INSERT OR IGNORE` silently discarded the second marker, leaving the later
    cycle's rows published under the earlier cycle's (earlier) visible_at —
    backdated visibility, which is the leak wearing a different hat.
    """
    import sqlite3

    from scout.source_quality.snapshot_writer import _publish_batch

    await _publish_batch(
        db._conn, batch_id=21, visible_at=NOW.isoformat(), rows_written=1
    )
    with pytest.raises(sqlite3.IntegrityError):
        await _publish_batch(
            db._conn,
            batch_id=21,
            visible_at=(NOW + timedelta(minutes=5)).isoformat(),
            rows_written=1,
        )


async def test_a_second_connection_sees_data_without_marker(db, tmp_path):
    """I-3: my "known-unpinned" claim on M6 was WRONG, and the discriminator is
    the same one the LOOP-1 durability lesson names — an observable OUTSIDE the
    writer's own view.

    Between the data commit and the marker commit a SECOND connection sees rows
    with no marker (1/0). Under a single combined commit it sees neither (0/0).
    That distinguishes the two orderings cleanly, so the ordering IS pinnable and
    I should not have called it structurally unobservable.
    """
    import aiosqlite

    from scout.source_quality.snapshot_writer import _publish_batch

    path = str(db.path) if hasattr(db, "path") else None
    await _insert_snapshot(
        db, created_at=NOW, snapshot_at=NOW, batch_id=11
    )  # data commit happened

    observer = await aiosqlite.connect(path or (tmp_path / "vis.db"))
    observer.row_factory = aiosqlite.Row
    try:
        cur = await observer.execute(
            "SELECT COUNT(*) FROM source_call_price_snapshots WHERE batch_id = 11"
        )
        rows_seen = (await cur.fetchone())[0]
        cur = await observer.execute(
            "SELECT COUNT(*) FROM source_call_snapshot_batches WHERE batch_id = 11"
        )
        markers_seen = (await cur.fetchone())[0]
        assert (rows_seen, markers_seen) == (1, 0), (
            "expected data-visible/marker-absent between the two commits; got "
            f"{(rows_seen, markers_seen)}"
        )

        await _publish_batch(
            db._conn, batch_id=11, visible_at=NOW.isoformat(), rows_written=1
        )
        cur = await observer.execute(
            "SELECT COUNT(*) FROM source_call_snapshot_batches WHERE batch_id = 11"
        )
        assert (await cur.fetchone())[0] == 1, "marker not durable after publish"
    finally:
        await observer.close()


async def test_visible_at_comes_from_publish_time_not_row_time(db):
    """I-4: the clock source for `visible_at` is the single assumption the
    conservative direction rests on.

    If it were derived from row/snapshot time it would BACKDATE visibility to
    before the commit — the leak, reintroduced through the marker itself.
    """
    from scout.source_quality.snapshot_writer import write_price_snapshots

    from test_source_call_snapshot_writer import (
        RecordingFetcher,
        RecordingResolver,
        _insert_source_call,
        _seed_price_cache,
    )

    old = NOW - timedelta(days=10)
    await _insert_source_call(
        db._conn,
        event_id="clock",
        resolved_state="resolved",
        call_ts=(NOW - timedelta(hours=1)).isoformat(),
        source_type="tg",
        token_id="clocker",
    )
    # Deliberately OLD observation time; visible_at must NOT follow it.
    await _seed_price_cache(db._conn, "clocker", 1.0, old.isoformat())

    before = datetime.now(timezone.utc)
    stats = await write_price_snapshots(
        db._conn,
        now=NOW,
        resolve_pool=RecordingResolver(result=None),
        fetch_ohlcv=RecordingFetcher(),
        max_price_cache_age_min=60 * 24 * 30,  # admit the old row
    )
    after = datetime.now(timezone.utc)

    assert stats["cg_snapshots_written"] == 1
    cur = await db._conn.execute(
        "SELECT visible_at FROM source_call_snapshot_batches WHERE batch_id = ?",
        (stats["batch_id"],),
    )
    visible_at = datetime.fromisoformat((await cur.fetchone())[0])
    assert before <= visible_at <= after, (
        f"visible_at {visible_at} is not publish-time — a row-derived marker "
        "backdates visibility to before the commit"
    )
    assert visible_at > old, "visible_at was backdated to the observation time"


async def test_allocated_batch_id_never_reuses_an_orphaned_id(db):
    """A crashed cycle leaves a stamped id with no batch row. Reusing it would
    retroactively publish the orphan rows under a later cycle's marker — the
    early-visibility this whole mechanism exists to prevent."""
    await _insert_snapshot(
        db, created_at=NOW, snapshot_at=NOW, batch_id=5  # orphan, unpublished
    )
    assert await _allocate_batch_id(db._conn) == 6

    await _publish_batch(
        db._conn, batch_id=9, visible_at=NOW.isoformat(), rows_written=0
    )
    assert await _allocate_batch_id(db._conn) == 10


async def test_fetch_snapshot_rows_gate_params_are_keyword_only(db):
    """The defect that made this gate inert in prod, as an executable guard.

    A call passing `"contract"` as the third positional bound `identity_kind`
    and left `as_of` at None, so the visibility predicate was skipped entirely
    — and every test still passed, because positional and keyword calls agree
    until the arguments are reordered. Fixing that one call site left the
    SHAPE that permits it; the `*` is what removes the shape. Nothing else in
    the suite fails if someone deletes it, so this does.
    """
    with pytest.raises(TypeError, match="positional"):
        await _fetch_snapshot_rows(db._conn, KEY, "contract")

    # The keyword form is the one that works, on the same inputs.
    assert await _fetch_snapshot_rows(db._conn, KEY, identity_kind="contract") == []
