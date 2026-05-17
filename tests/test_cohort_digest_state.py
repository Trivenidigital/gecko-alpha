"""Tests for cohort_digest_state singleton + helpers — BL-NEW-LIVE-ELIGIBLE-WEEKLY-DIGEST cycle 5 commit 2/5."""

import pytest

from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "cohort_digest.db"))
    await database.initialize()
    yield database
    await database.close()


async def test_cohort_digest_state_table_exists(db):
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='cohort_digest_state'"
    )
    row = await cur.fetchone()
    assert row is not None


async def test_cohort_digest_state_seeded_with_null_row(db):
    """V30 MUST-FIX: row exists post-migration with both nullable fields = NULL.
    Required so stamp helpers' INSERT OR REPLACE can preserve other field via
    sub-SELECT."""
    state = await db.cohort_digest_read_state()
    assert state == {"last_digest_date": None, "last_final_block_fired_at": None}


async def test_cohort_digest_state_singleton_constraint(db):
    """CHECK (marker = 1) raises IntegrityError on attempted second row."""
    import sqlite3

    with pytest.raises((sqlite3.IntegrityError, Exception)):
        await db._conn.execute(
            "INSERT INTO cohort_digest_state (marker, last_digest_date) VALUES (2, '2026-05-17')"
        )
        await db._conn.commit()


async def test_paper_trades_closed_at_index_exists(db):
    """V30 MUST-FIX: partial index on closed_at for cohort-query plan."""
    cur = await db._conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND name='idx_paper_trades_closed_at'"
    )
    row = await cur.fetchone()
    assert row is not None, "idx_paper_trades_closed_at not created"
    assert "closed_at IS NOT NULL" in (row[1] or ""), (
        "Index should be partial (WHERE closed_at IS NOT NULL)"
    )


async def test_stamp_last_digest_date_persists(db):
    await db.cohort_digest_stamp_last_digest_date("2026-05-18")
    state = await db.cohort_digest_read_state()
    assert state["last_digest_date"] == "2026-05-18"
    assert state["last_final_block_fired_at"] is None


async def test_stamp_final_block_fired_persists(db):
    await db.cohort_digest_stamp_final_block_fired("2026-06-08T09:00:00+00:00")
    state = await db.cohort_digest_read_state()
    assert state["last_final_block_fired_at"] == "2026-06-08T09:00:00+00:00"
    assert state["last_digest_date"] is None


async def test_stamp_last_digest_date_preserves_final_block_field(db):
    """V30 MUST-FIX critical: INSERT OR REPLACE uses sub-SELECT to preserve
    the other column. A naive UPSERT would NULL the unrelated column."""
    await db.cohort_digest_stamp_final_block_fired("2026-06-08T09:00:00+00:00")
    await db.cohort_digest_stamp_last_digest_date("2026-06-15")
    state = await db.cohort_digest_read_state()
    assert state["last_digest_date"] == "2026-06-15"
    assert state["last_final_block_fired_at"] == "2026-06-08T09:00:00+00:00"


async def test_stamp_final_block_preserves_last_digest_date(db):
    """Reverse direction of the preservation invariant."""
    await db.cohort_digest_stamp_last_digest_date("2026-06-01")
    await db.cohort_digest_stamp_final_block_fired("2026-06-08T09:00:00+00:00")
    state = await db.cohort_digest_read_state()
    assert state["last_digest_date"] == "2026-06-01"
    assert state["last_final_block_fired_at"] == "2026-06-08T09:00:00+00:00"


async def test_migration_idempotent(tmp_path):
    """Re-running initialize on the same DB must not double-create rows
    or raise (idempotent INSERT OR IGNORE)."""
    path = str(tmp_path / "idem.db")
    db1 = Database(path)
    await db1.initialize()
    await db1.cohort_digest_stamp_last_digest_date("2026-05-17")
    await db1.close()

    db2 = Database(path)
    await db2.initialize()
    state = await db2.cohort_digest_read_state()
    # Pre-existing data preserved — INSERT OR IGNORE didn't clobber.
    assert state["last_digest_date"] == "2026-05-17"

    cur = await db2._conn.execute("SELECT COUNT(*) FROM cohort_digest_state")
    count = (await cur.fetchone())[0]
    assert count == 1
    await db2.close()
