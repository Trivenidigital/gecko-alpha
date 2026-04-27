"""BL-063 moonshot schema migration tests.

Per BL-060 mid-flight migration lesson:
- New columns nullable, pre-cutover rows = NULL
- A/B comparisons scope to opened_at >= cutover_ts (NOT row age)
- CREATE INDEX lives in migration step (CREATE TABLE IF NOT EXISTS is a
  no-op for existing tables, so an index next to the column would never
  be applied to prod).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scout.db import Database


@pytest.mark.asyncio
async def test_bl063_migration_adds_columns(tmp_path):
    """Migration adds moonshot_armed_at and original_trail_drawdown_pct columns."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(paper_trades)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "moonshot_armed_at" in cols
    assert "original_trail_drawdown_pct" in cols
    await db.close()


@pytest.mark.asyncio
async def test_bl063_migration_inserts_cutover_row(tmp_path):
    """A 'bl063_moonshot' row exists in paper_migrations after init."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT name, cutover_ts FROM paper_migrations WHERE name = 'bl063_moonshot'"
    )
    row = await cur.fetchone()
    assert row is not None
    name, cutover_ts = row
    assert name == "bl063_moonshot"
    parsed = datetime.fromisoformat(cutover_ts)
    assert parsed.tzinfo is not None
    await db.close()


@pytest.mark.asyncio
async def test_bl063_migration_creates_partial_index(tmp_path):
    """Partial index on moonshot_armed_at is created."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='idx_paper_trades_moonshot_armed_at'"
    )
    row = await cur.fetchone()
    assert row is not None
    await db.close()


@pytest.mark.asyncio
async def test_bl063_migration_idempotent(tmp_path):
    """Re-running initialize is a no-op (no duplicate cutover row, no error)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db.close()
    db2 = Database(tmp_path / "t.db")
    await db2.initialize()
    cur = await db2._conn.execute(
        "SELECT COUNT(*) FROM paper_migrations WHERE name = 'bl063_moonshot'"
    )
    (count,) = await cur.fetchone()
    assert count == 1
    await db2.close()


@pytest.mark.asyncio
async def test_bl063_pre_existing_rows_have_null(tmp_path):
    """A trade row inserted before migration retains NULL moonshot fields after."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Insert a trade row directly to simulate "already there before migration".
    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity,
            tp_pct, sl_pct, tp_price, sl_price,
            status, opened_at)
           VALUES ('tok','TOK','Token','solana','first_signal','{}',
                   1.0, 100.0, 100.0, 20.0, 10.0, 1.2, 0.9,
                   'open', ?)""",
        (now_iso,),
    )
    await db._conn.commit()
    cur = await db._conn.execute(
        "SELECT moonshot_armed_at, original_trail_drawdown_pct "
        "FROM paper_trades WHERE token_id='tok'"
    )
    armed_at, original_trail = await cur.fetchone()
    assert armed_at is None
    assert original_trail is None
    await db.close()
