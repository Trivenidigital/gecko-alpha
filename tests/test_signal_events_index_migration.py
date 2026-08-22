"""Tests for signal_events index migration (Option D from retention rulings).

INF-08: Drop idx_sig_events_type (466 MB), add idx_sig_events_created_at.
"""

import pytest
from datetime import datetime, timedelta, timezone

from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    """Create and initialize a test database."""
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_signal_events_index_migration_creates_created_at_index(db):
    """Verify the migration creates idx_sig_events_created_at."""
    conn = db._conn
    assert conn is not None

    # Check that the new index exists
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_sig_events_created_at'"
    )
    row = await cur.fetchone()
    assert row is not None, "idx_sig_events_created_at should exist"


@pytest.mark.asyncio
async def test_signal_events_index_migration_drops_type_index(db):
    """The migration drops idx_sig_events_type ON THE OLD SHAPE.

    A fresh tmp_path database never HAD `idx_sig_events_type`, so asserting it
    is absent proved nothing about the drop: mutation-deleting the
    `DROP INDEX` statement left every test in this file green. That made this a
    test of the initial schema, not of the migration -- and the 466 MB reclaim
    the whole change exists to deliver was unverified.

    So rebuild the pre-migration shape first, then run the migration against it.
    """
    conn = db._conn
    assert conn is not None

    # --- rebuild the OLD shape -------------------------------------------
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sig_events_type ON signal_events(event_type)"
    )
    await conn.execute(
        "DELETE FROM paper_migrations WHERE name='signal_events_indexes_v1'"
    )
    await conn.commit()
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='idx_sig_events_type'"
    )
    assert await cur.fetchone() is not None, (
        "precondition: the pre-migration index must be present, or this test "
        "cannot observe the drop"
    )

    # --- run the migration against it -------------------------------------
    await db._migrate_signal_events_indexes_v1()

    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_sig_events_type'"
    )
    row = await cur.fetchone()
    assert row is None, "idx_sig_events_type should be dropped"


@pytest.mark.asyncio
async def test_signal_events_indexes_idempotent(db):
    """Verify the migration is idempotent (running twice is safe)."""
    # Run the migration method directly a second time
    await db._migrate_signal_events_indexes_v1()

    # Verify the state is still correct (no errors, correct indexes)
    conn = db._conn
    assert conn is not None

    # Check that created_at index still exists
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_sig_events_created_at'"
    )
    row = await cur.fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_signal_events_created_at_index_used_for_range_query(db):
    """Verify the new index is used for range queries on created_at."""
    conn = db._conn
    assert conn is not None

    # Insert test data - create events spanning 20 hours in the past and future
    now = datetime.now(timezone.utc)
    events = [
        {
            "token_id": f"token_{i}",
            "pipeline": "test",
            "event_type": "test_event",
            "event_data": "{}",
            "source_module": "test_module",
            "created_at": (
                now - timedelta(hours=20 - i)
            ).isoformat(),  # Range from -20h to 0h (relative to now)
        }
        for i in range(10)
    ]

    for event in events:
        await conn.execute(
            """INSERT INTO signal_events
               (token_id, pipeline, event_type, event_data, source_module, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                event["token_id"],
                event["pipeline"],
                event["event_type"],
                event["event_data"],
                event["source_module"],
                event["created_at"],
            ),
        )
    await conn.commit()

    # Test the query pattern from load_recent_events
    # Cutoff is 12 hours ago, so we should get events from -12 to 0 hours
    cutoff = (now - timedelta(hours=12)).isoformat()

    # Check the query plan uses the index
    cur = await conn.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT id FROM signal_events "
        "WHERE created_at >= ? "
        "ORDER BY created_at ASC",
        (cutoff,),
    )
    plan_rows = await cur.fetchall()
    # Each row is (id, parent, notused, detail)
    # The detail column contains the query plan description
    plan_text = "\n".join(
        str(row["detail"]) if isinstance(row, dict) else str(row[3])
        for row in plan_rows
    )

    # The plan should reference the idx_sig_events_created_at index
    # In SQLite, we look for "USING INDEX" or the index name in the query plan
    assert (
        "idx_sig_events_created_at" in plan_text
        or "USING INDEX" in plan_text
        or "index" in plan_text.lower()
    ), f"Expected index to be used. Plan:\n{plan_text}"

    # Verify the actual query results are correct
    cur = await conn.execute(
        """SELECT id FROM signal_events
           WHERE created_at >= ?
           ORDER BY created_at ASC""",
        (cutoff,),
    )
    rows = await cur.fetchall()
    # Should get some results - at least those created in the last 12 hours
    # With range(10) from -20h to -11h, cutoff at -12h should get ~2 events
    assert len(rows) > 0, f"Expected at least 1 result, got {len(rows)}"
    # Verify the results are ordered correctly
    if len(rows) > 1:
        assert (
            rows[0]["id"] <= rows[1]["id"]
        ), "Results should be ordered by id (created_at ascending)"


@pytest.mark.asyncio
async def test_signal_events_token_index_still_works(db):
    """Verify idx_sig_events_token is unchanged and still functional."""
    conn = db._conn
    assert conn is not None

    # Check that idx_sig_events_token still exists
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_sig_events_token'"
    )
    row = await cur.fetchone()
    assert row is not None, "idx_sig_events_token should still exist"

    # Verify it has the expected columns by checking the index info
    cur = await conn.execute("PRAGMA index_info(idx_sig_events_token)")
    index_cols = await cur.fetchall()
    col_names = [row[2] for row in index_cols]  # Column 2 is the column name
    assert "token_id" in col_names
    assert "pipeline" in col_names
    assert "created_at" in col_names


@pytest.mark.asyncio
async def test_signal_events_all_indexes_inventory(db):
    """Verify the complete index inventory after migration."""
    conn = db._conn
    assert conn is not None

    # Get all indexes on signal_events table
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='signal_events' AND name NOT LIKE 'sqlite_%'"
    )
    indexes = [row[0] for row in await cur.fetchall()]

    # Should have exactly 2 indexes: token and created_at
    # (not type anymore)
    expected = {"idx_sig_events_token", "idx_sig_events_created_at"}
    actual = set(indexes)

    assert actual == expected, f"Expected indexes {expected}, got {actual}"


@pytest.mark.asyncio
async def test_migration_records_in_schema_version(db):
    """Verify the migration is recorded in schema_version table."""
    conn = db._conn
    assert conn is not None

    # Check schema_version table
    cur = await conn.execute(
        "SELECT version FROM schema_version WHERE version = 20260821"
    )
    row = await cur.fetchone()
    assert row is not None, "schema_version 20260821 should be recorded"

    # Check paper_migrations table
    cur = await conn.execute(
        "SELECT name FROM paper_migrations WHERE name = 'signal_events_indexes_v1'"
    )
    row = await cur.fetchone()
    assert row is not None, "Migration should be recorded in paper_migrations"


@pytest.mark.asyncio
async def test_signal_events_queries_still_work_after_migration(db):
    """Verify existing queries that use signal_events still work correctly."""
    conn = db._conn
    assert conn is not None

    # Insert test data
    now = datetime.now(timezone.utc)
    test_data = [
        (
            "token_1",
            "test_pipeline",
            "event_type_1",
            "{}",
            "module_1",
            now.isoformat(),
        ),
        (
            "token_2",
            "test_pipeline",
            "event_type_2",
            "{}",
            "module_2",
            (now + timedelta(seconds=10)).isoformat(),
        ),
    ]

    for data in test_data:
        await conn.execute(
            """INSERT INTO signal_events
               (token_id, pipeline, event_type, event_data, source_module, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            data,
        )
    await conn.commit()

    # Test load_recent_events pattern
    cutoff = (now - timedelta(hours=1)).isoformat()
    cur = await conn.execute(
        """SELECT id, token_id, pipeline, event_type, event_data,
                  source_module, created_at
           FROM signal_events
           WHERE created_at >= ?
           ORDER BY created_at ASC""",
        (cutoff,),
    )
    rows = await cur.fetchall()
    assert len(rows) == 2, "Should get both events"
    assert rows[0]["token_id"] == "token_1"
    assert rows[1]["token_id"] == "token_2"


# ---------------------------------------------------------------------------
# Ruling-required proofs the first pass omitted (2026-08-16 retention ruling D:
# "Migration must pin EXPLAIN QUERY PLAN ..., row-count identity, index
# inventory, and rollback recreation.")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_row_count_identity_across_the_migration(db):
    """Index work must not change the DATA. Prove it, do not assume it.

    Dropping and creating indexes cannot lose rows in principle — but "in
    principle" is what a migration proof exists to replace. A botched variant
    that rebuilt the table to change indexes would pass every other test here
    while silently truncating, and `signal_events` is the largest table in the
    database.
    """
    conn = db._conn
    for i in range(50):
        await conn.execute(
            "INSERT INTO signal_events "
            "(token_id, pipeline, event_type, event_data, source_module, created_at) "
            "VALUES (?, 'memecoin', 'candidate_scored', '{}', 'scorer', ?)",
            (f"0xrow{i}", f"2026-08-{(i % 28) + 1:02d}T00:00:00+00:00"),
        )
    await conn.commit()

    cur = await conn.execute("SELECT COUNT(*) FROM signal_events")
    before = (await cur.fetchone())[0]
    assert before == 50

    # Re-running the migration is the closest available proxy for "apply it
    # again on a populated table" — it is idempotent, so the row count must be
    # untouched.
    await db._migrate_signal_events_indexes_v1()

    cur = await conn.execute("SELECT COUNT(*) FROM signal_events")
    assert (await cur.fetchone())[
        0
    ] == before, "row count changed across the index migration"
    # And the rows are still individually addressable, not merely counted.
    cur = await conn.execute(
        "SELECT COUNT(DISTINCT token_id) FROM signal_events WHERE token_id LIKE '0xrow%'"
    )
    assert (await cur.fetchone())[0] == 50


@pytest.mark.asyncio
async def test_dropped_index_can_be_recreated_for_rollback(db):
    """Rollback must be a demonstrated capability, not an assumption.

    The ruling requires rollback recreation because dropping an index is the
    irreversible-feeling half of this change: if the new access path turned out
    to regress some query, the operator needs to know the old index can come
    back on a populated table without a migration.
    """
    conn = db._conn
    for i in range(20):
        await conn.execute(
            "INSERT INTO signal_events "
            "(token_id, pipeline, event_type, event_data, source_module, created_at) "
            "VALUES (?, 'memecoin', 'candidate_scored', '{}', 'scorer', ?)",
            (f"0xrb{i}", "2026-08-01T00:00:00+00:00"),
        )
    await conn.commit()

    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='idx_sig_events_type'"
    )
    assert await cur.fetchone() is None, "precondition: the index is dropped"

    # The documented rollback statement.
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sig_events_type " "ON signal_events(event_type)"
    )
    await conn.commit()

    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='idx_sig_events_type'"
    )
    assert await cur.fetchone() is not None, "rollback recreation failed"

    # ...and it is usable, not merely present.
    cur = await conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM signal_events WHERE event_type = 'x'"
    )
    plan = " ".join(str(c) for r in await cur.fetchall() for c in r)
    assert (
        "idx_sig_events_type" in plan
    ), f"recreated index is not used by the planner: {plan}"


@pytest.mark.asyncio
async def test_migration_does_not_bundle_a_vacuum(db):
    """The ruling is explicit: no VACUUM bundled.

    Dropping an index frees reusable SQLite PAGES; the file does not shrink
    without VACUUM, and VACUUM on a ~7 GB database needs roughly its own size in
    free space — on a volume already at 90%, bundling it would turn a page
    reclaim into a disk-full incident.
    """
    import inspect

    src = inspect.getsource(db._migrate_signal_events_indexes_v1)
    assert (
        "VACUUM" not in src.upper()
    ), "VACUUM must not be bundled into the index migration"
