"""Tests for trade_gainers / trade_losers / trade_trending dispatch filters.

These were previously dead code (not called from main.py/agent.py) so the
mcap / rank filters had zero coverage. Cover the filter branches here to
guard against regressions — specifically, NULL market_cap and below-threshold
mcap/rank must skip cleanly without raising.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.config import Settings
from scout.db import Database
from scout.trading.engine import TradingEngine
from scout.trading.signals import (
    trade_gainers,
    trade_losers,
    trade_trending,
)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_CHAT_ID="test",
        ANTHROPIC_API_KEY="test",
        DB_PATH=tmp_path / "test.db",
        TRADING_ENABLED=True,
        TRADING_MODE="paper",
        PAPER_TRADE_AMOUNT_USD=1000.0,
        PAPER_MAX_EXPOSURE_USD=10_000.0,
        PAPER_TP_PCT=20.0,
        PAPER_SL_PCT=10.0,
        PAPER_SLIPPAGE_BPS=50,
        PAPER_MAX_DURATION_HOURS=48,
        PAPER_MIN_MCAP=5_000_000,
        PAPER_MAX_MCAP_RANK=1500,
        PAPER_MAX_OPEN_TRADES=1000,
        PAPER_STARTUP_WARMUP_SECONDS=0,
    )


@pytest.fixture
def engine(db, settings):
    return TradingEngine(mode="paper", db=db, settings=settings)


async def _insert_gainer(db, coin_id, market_cap, price=1.0):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO gainers_snapshots
           (coin_id, symbol, name, price_change_24h, market_cap, volume_24h,
            price_at_snapshot, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (coin_id, coin_id.upper(), coin_id, 25.0, market_cap, 100_000.0, price, now),
    )
    await db._conn.commit()


async def _insert_loser(db, coin_id, market_cap, price=1.0):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO losers_snapshots
           (coin_id, symbol, name, price_change_24h, market_cap, volume_24h,
            price_at_snapshot, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (coin_id, coin_id.upper(), coin_id, -25.0, market_cap, 100_000.0, price, now),
    )
    await db._conn.commit()


async def _insert_trending(db, coin_id, market_cap_rank):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO trending_snapshots
           (coin_id, symbol, name, market_cap_rank, trending_score, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (coin_id, coin_id.upper(), coin_id, market_cap_rank, 1.0, now),
    )
    await db._conn.commit()


async def _seed_price(db, coin_id, price=1.0):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT OR REPLACE INTO price_cache
           (coin_id, current_price, price_change_24h, price_change_7d, market_cap, updated_at)
           VALUES (?, ?, 0, 0, 0, ?)""",
        (coin_id, price, now),
    )
    await db._conn.commit()


async def _open_count(db):
    cursor = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE status = 'open'"
    )
    row = await cursor.fetchone()
    return row[0]


# ---------------- trade_gainers --------------------------------------------


async def test_trade_gainers_opens_trade_when_mcap_above_min(db, engine, settings):
    await _insert_gainer(db, "btc-like", market_cap=10_000_000)
    await trade_gainers(engine, db, min_mcap=5_000_000, settings=settings)
    assert await _open_count(db) == 1


async def test_trade_gainers_skips_below_min_mcap(db, engine, settings):
    await _insert_gainer(db, "micro-cap", market_cap=1_000_000)  # below 5M floor
    await trade_gainers(engine, db, min_mcap=5_000_000, settings=settings)
    assert await _open_count(db) == 0


async def test_trade_gainers_skips_null_mcap(db, engine, settings):
    await _insert_gainer(db, "null-mcap", market_cap=None)
    await trade_gainers(engine, db, min_mcap=5_000_000, settings=settings)
    assert await _open_count(db) == 0


async def test_trade_gainers_respects_threshold_override(db, engine, settings):
    await _insert_gainer(db, "mid-cap", market_cap=2_000_000)
    # Override to $1M — should now open
    await trade_gainers(engine, db, min_mcap=1_000_000, settings=settings)
    assert await _open_count(db) == 1


# ---------------- trade_losers ---------------------------------------------


async def test_trade_losers_opens_trade_when_mcap_above_min(db, engine, settings):
    await _insert_loser(db, "btc-dip", market_cap=10_000_000)
    await trade_losers(engine, db, min_mcap=5_000_000, settings=settings)
    assert await _open_count(db) == 1


async def test_trade_losers_skips_below_min_mcap(db, engine, settings):
    await _insert_loser(db, "micro-dip", market_cap=500_000)
    await trade_losers(engine, db, min_mcap=5_000_000, settings=settings)
    assert await _open_count(db) == 0


async def test_trade_losers_skips_null_mcap(db, engine, settings):
    await _insert_loser(db, "null-dip", market_cap=None)
    await trade_losers(engine, db, min_mcap=5_000_000, settings=settings)
    assert await _open_count(db) == 0


async def test_trade_losers_falls_back_to_price_cache(db, engine, settings):
    """When price_at_snapshot is NULL, loader reads from price_cache."""
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO losers_snapshots
           (coin_id, symbol, name, price_change_24h, market_cap, volume_24h,
            price_at_snapshot, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("null-price", "NP", "null-price", -25.0, 10_000_000, 100_000.0, None, now),
    )
    await db._conn.commit()
    await _seed_price(db, "null-price", price=0.042)
    await trade_losers(engine, db, min_mcap=5_000_000, settings=settings)
    assert await _open_count(db) == 1


# ---------------- trade_trending -------------------------------------------


async def test_trade_trending_opens_when_rank_under_threshold(db, engine, settings):
    await _insert_trending(db, "top-100", market_cap_rank=50)
    await _seed_price(db, "top-100", price=1.0)
    await trade_trending(engine, db, max_mcap_rank=1500, settings=settings)
    assert await _open_count(db) == 1


async def test_trade_trending_skips_above_rank_threshold(db, engine, settings):
    await _insert_trending(db, "rank-2000", market_cap_rank=2000)
    await _seed_price(db, "rank-2000", price=1.0)
    await trade_trending(engine, db, max_mcap_rank=1500, settings=settings)
    assert await _open_count(db) == 0


async def test_trade_trending_skips_null_rank(db, engine, settings):
    await _insert_trending(db, "no-rank", market_cap_rank=None)
    await _seed_price(db, "no-rank", price=1.0)
    await trade_trending(engine, db, max_mcap_rank=1500, settings=settings)
    assert await _open_count(db) == 0


async def test_trade_trending_respects_threshold_override(db, engine, settings):
    await _insert_trending(db, "rank-1200", market_cap_rank=1200)
    await _seed_price(db, "rank-1200", price=1.0)
    # Tighter ceiling — should reject
    await trade_trending(engine, db, max_mcap_rank=1000, settings=settings)
    assert await _open_count(db) == 0


# ---------------- Datetime-window regression --------------------------------
# Bug: Stored timestamps use ISO format ('2026-04-17T06:07:17.297281+00:00')
# while SQLite's datetime('now', ...) returns space-separated form
# ('2026-04-17 06:07:17'). Raw string comparison treats 'T' (0x54) > ' ' (0x20),
# so `snapshot_at >= datetime('now', '-5 minutes')` matches ANY same-day
# snapshot, not just the last 5 minutes. This caused gainers_early to open
# with entry prices taken from early-morning peak snapshots.


async def _insert_gainer_at(db, coin_id, market_cap, price, snapshot_at):
    await db._conn.execute(
        """INSERT INTO gainers_snapshots
           (coin_id, symbol, name, price_change_24h, market_cap, volume_24h,
            price_at_snapshot, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            coin_id,
            coin_id.upper(),
            coin_id,
            25.0,
            market_cap,
            100_000.0,
            price,
            snapshot_at,
        ),
    )
    await db._conn.commit()


async def _insert_loser_at(db, coin_id, market_cap, price, snapshot_at):
    await db._conn.execute(
        """INSERT INTO losers_snapshots
           (coin_id, symbol, name, price_change_24h, market_cap, volume_24h,
            price_at_snapshot, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            coin_id,
            coin_id.upper(),
            coin_id,
            -25.0,
            market_cap,
            100_000.0,
            price,
            snapshot_at,
        ),
    )
    await db._conn.commit()


async def _insert_trending_at(db, coin_id, market_cap_rank, snapshot_at):
    await db._conn.execute(
        """INSERT INTO trending_snapshots
           (coin_id, symbol, name, market_cap_rank, trending_score, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (coin_id, coin_id.upper(), coin_id, market_cap_rank, 1.0, snapshot_at),
    )
    await db._conn.commit()


async def test_trade_gainers_skips_snapshots_older_than_5min_same_day(
    db, engine, settings
):
    """A snapshot stored 2 hours ago (same day) must NOT be picked up."""
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    await _insert_gainer_at(
        db, "stale-gainer", 10_000_000, price=1.0, snapshot_at=stale
    )
    await trade_gainers(engine, db, min_mcap=5_000_000, settings=settings)
    assert await _open_count(db) == 0


async def test_trade_losers_skips_snapshots_older_than_5min_same_day(
    db, engine, settings
):
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    await _insert_loser_at(db, "stale-loser", 10_000_000, price=1.0, snapshot_at=stale)
    await trade_losers(engine, db, min_mcap=5_000_000, settings=settings)
    assert await _open_count(db) == 0


async def test_trade_trending_skips_snapshots_older_than_5min_same_day(
    db, engine, settings
):
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    await _insert_trending_at(db, "stale-trend", market_cap_rank=100, snapshot_at=stale)
    await _seed_price(db, "stale-trend", price=1.0)
    await trade_trending(engine, db, max_mcap_rank=1500, settings=settings)
    assert await _open_count(db) == 0


async def test_trade_gainers_uses_fresh_snapshot_price_not_earlier_peak(
    db, engine, settings
):
    """When both a stale and a fresh snapshot exist, entry must come from fresh one.

    Reproduces the production bug where entries were sourced from the day's
    earliest peak snapshot because DISTINCT + broken time filter returned the
    full day's rows, and the first iterated row won via engine dedup.
    """
    peak_earlier = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    # Earlier peak at $1.75 (would be the stale-entry bug value)
    await _insert_gainer_at(
        db, "two-snap", 10_000_000, price=1.75, snapshot_at=peak_earlier
    )
    # Current snapshot at $1.44
    await _insert_gainer_at(db, "two-snap", 10_000_000, price=1.44, snapshot_at=fresh)
    await trade_gainers(engine, db, min_mcap=5_000_000, settings=settings)
    assert await _open_count(db) == 1
    cur = await db._conn.execute(
        "SELECT entry_price FROM paper_trades WHERE token_id='two-snap' AND status='open'"
    )
    row = await cur.fetchone()
    entry = row[0]
    # Entry must derive from fresh $1.44 (with default 50bps slippage = $1.4472),
    # NOT from the stale $1.75 peak (which would yield ~$1.75875).
    assert entry < 1.60, f"entry {entry} came from stale snapshot, not fresh"
