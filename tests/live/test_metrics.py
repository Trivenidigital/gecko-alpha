"""Tests for scout.live.metrics UPSERT counter helper (BL-055 Task 11)."""

from scout.db import Database
from scout.live.metrics import inc


async def test_inc_creates_row_with_value_one(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await inc(db, "shadow_orders_opened", date_utc="2026-04-23")
    cur = await db._conn.execute(
        "SELECT value FROM live_metrics_daily "
        "WHERE date='2026-04-23' AND metric='shadow_orders_opened'"
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()


async def test_inc_increments_existing_row(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    for _ in range(5):
        await inc(db, "shadow_rejects_no_venue", date_utc="2026-04-23")
    cur = await db._conn.execute(
        "SELECT value FROM live_metrics_daily "
        "WHERE date='2026-04-23' AND metric='shadow_rejects_no_venue'"
    )
    assert (await cur.fetchone())[0] == 5
    await db.close()


async def test_inc_separates_date_buckets(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await inc(db, "shadow_orders_opened", date_utc="2026-04-23")
    await inc(db, "shadow_orders_opened", date_utc="2026-04-24")
    cur = await db._conn.execute(
        "SELECT date, value FROM live_metrics_daily "
        "WHERE metric='shadow_orders_opened' ORDER BY date"
    )
    rows = await cur.fetchall()
    assert [(r[0], r[1]) for r in rows] == [("2026-04-23", 1), ("2026-04-24", 1)]
    await db.close()
