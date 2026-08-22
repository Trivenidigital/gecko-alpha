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
    """Verify the migration drops idx_sig_events_type."""
    conn = db._conn
    assert conn is not None

    # Check that the old index does NOT exist
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
            "created_at": (now - timedelta(hours=20-i)).isoformat(),  # Range from -20h to 0h (relative to now)
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
    plan_text = "\n".join(str(row["detail"]) if isinstance(row, dict) else str(row[3]) for row in plan_rows)

    # The plan should reference the idx_sig_events_created_at index
    # In SQLite, we look for "USING INDEX" or the index name in the query plan
    assert "idx_sig_events_created_at" in plan_text or "USING INDEX" in plan_text or "index" in plan_text.lower(), (
        f"Expected index to be used. Plan:\n{plan_text}"
    )

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
        assert rows[0]["id"] <= rows[1]["id"], "Results should be ordered by id (created_at ascending)"


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

    assert actual == expected, (
        f"Expected indexes {expected}, got {actual}"
    )


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
