"""Tests for paper trading database tables."""
import json
from datetime import datetime, timezone

import pytest

from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


async def test_paper_trades_table_exists(db):
    """paper_trades table is created on initialize."""
    cursor = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='paper_trades'"
    )
    row = await cursor.fetchone()
    assert row is not None


async def test_paper_daily_summary_table_exists(db):
    """paper_daily_summary table is created on initialize."""
    cursor = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='paper_daily_summary'"
    )
    row = await cursor.fetchone()
    assert row is not None


async def test_paper_trades_insert_and_read(db):
    """Can insert and read a paper trade."""
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
            status, opened_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("bitcoin", "BTC", "Bitcoin", "coingecko", "volume_spike",
         json.dumps({"spike_ratio": 12.3}),
         50000.0, 1000.0, 0.02, 20.0, 10.0, 60000.0, 45000.0, "open", now),
    )
    await db._conn.commit()
    cursor = await db._conn.execute("SELECT * FROM paper_trades WHERE token_id='bitcoin'")
    row = await cursor.fetchone()
    assert row is not None
    assert dict(row)["entry_price"] == 50000.0


async def test_paper_trades_unique_constraint(db):
    """Duplicate (token_id, signal_type, opened_at) is rejected."""
    now = datetime.now(timezone.utc).isoformat()
    args = ("bitcoin", "BTC", "Bitcoin", "coingecko", "volume_spike",
            json.dumps({}), 50000.0, 1000.0, 0.02, 20.0, 10.0,
            60000.0, 45000.0, "open", now)
    sql = """INSERT INTO paper_trades
             (token_id, symbol, name, chain, signal_type, signal_data,
              entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
              status, opened_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    await db._conn.execute(sql, args)
    await db._conn.commit()
    with pytest.raises(Exception):  # IntegrityError
        await db._conn.execute(sql, args)
        await db._conn.commit()


async def test_paper_trades_status_index(db):
    """Status index exists for efficient open-trade queries."""
    cursor = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_paper_trades_status'"
    )
    row = await cursor.fetchone()
    assert row is not None


async def test_paper_daily_summary_unique_date(db):
    """paper_daily_summary enforces unique date."""
    await db._conn.execute(
        """INSERT INTO paper_daily_summary (date, trades_opened, trades_closed,
           wins, losses, total_pnl_usd) VALUES (?, ?, ?, ?, ?, ?)""",
        ("2026-04-19", 10, 8, 5, 3, 340.0),
    )
    await db._conn.commit()
    with pytest.raises(Exception):
        await db._conn.execute(
            """INSERT INTO paper_daily_summary (date, trades_opened, trades_closed,
               wins, losses, total_pnl_usd) VALUES (?, ?, ?, ?, ?, ?)""",
            ("2026-04-19", 5, 3, 2, 1, 100.0),
        )
        await db._conn.commit()
