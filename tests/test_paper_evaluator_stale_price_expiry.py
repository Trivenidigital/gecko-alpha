"""Tests for the stale-price zombie fix.

A trade whose token's `price_cache` row stops updating must STILL be
expirable when it ages past `PAPER_MAX_DURATION_HOURS`. The pre-fix
evaluator skipped the entire iteration on stale/missing price (including
the expiry check), leaving zombie rows `status='open'` forever.

Discovered 2026-04-27 while auditing why the BL-064 "Early Catches"
dashboard showed multi-day winners with no profitable paper trades —
the evaluator simply wasn't running on them past hour ~1 of stale
price_cache.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading.evaluator import evaluate_paper_trades
from scout.trading.paper import PaperTrader


async def _open_trade(
    db: Database,
    trader: PaperTrader,
    *,
    token_id: str,
    opened_hours_ago: float,
) -> int:
    """Open a paper trade with `opened_at` backdated by `opened_hours_ago`."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id=token_id,
        symbol=token_id.upper(),
        name=token_id.title(),
        chain="coingecko",
        signal_type="first_signal",
        signal_data={},
        current_price=1.00,
        amount_usd=100.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=0,
        signal_combo="first_signal",
    )
    backdated = (
        datetime.now(timezone.utc) - timedelta(hours=opened_hours_ago)
    ).isoformat()
    await db._conn.execute(
        "UPDATE paper_trades SET opened_at = ?, created_at = ? WHERE id = ?",
        (backdated, backdated, trade_id),
    )
    await db._conn.commit()
    return trade_id


@pytest.mark.asyncio
async def test_no_price_past_expiry_force_closes(tmp_path, settings_factory):
    """Trade with no price_cache row + opened > max_duration ago →
    force-closed at entry_price with exit_reason='expired_stale_no_price'."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(PAPER_MAX_DURATION_HOURS=24)
    trade_id = await _open_trade(
        db, trader, token_id="zombie_no_price", opened_hours_ago=72
    )

    # NO price_cache row inserted — token has dropped from CG markets entirely
    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT status, exit_reason, exit_price, pnl_pct FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    status, reason, exit_price, pnl_pct = await cur.fetchone()
    assert status == "closed_expired"
    assert reason == "expired_stale_no_price"
    # Closes at entry_price (1.00) → ~zero PnL
    assert exit_price == pytest.approx(1.00, rel=1e-3)
    assert pnl_pct == pytest.approx(0.0, abs=0.5)
    await db.close()


@pytest.mark.asyncio
async def test_stale_price_past_expiry_force_closes_at_stale_snapshot(
    tmp_path, settings_factory
):
    """Trade with stale price_cache (> 1h old) + opened > max_duration ago →
    force-closed at the stale snapshot with exit_reason='expired_stale_price'."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(PAPER_MAX_DURATION_HOURS=24)
    trade_id = await _open_trade(
        db, trader, token_id="zombie_stale", opened_hours_ago=72
    )

    # Insert a STALE price_cache row (24h old)
    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) VALUES (?, ?, ?)",
        ("zombie_stale", 1.50, stale_ts),
    )
    await db._conn.commit()

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT status, exit_reason, exit_price FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    status, reason, exit_price = await cur.fetchone()
    assert status == "closed_expired"
    assert reason == "expired_stale_price"
    # Closes at the stale snapshot (1.50) — the only data we have
    assert exit_price == pytest.approx(1.50, rel=1e-3)
    await db.close()


@pytest.mark.asyncio
async def test_no_price_within_duration_still_skipped(tmp_path, settings_factory):
    """Regression: trade with no price + opened recently (within max_duration)
    is still skipped (not closed). Only zombies past expiry get force-closed."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(PAPER_MAX_DURATION_HOURS=168)
    trade_id = await _open_trade(
        db, trader, token_id="fresh_no_price", opened_hours_ago=2
    )
    # NO price_cache row
    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    (status,) = await cur.fetchone()
    assert status == "open"  # still open — within duration, just no price yet
    await db.close()


@pytest.mark.asyncio
async def test_stale_price_within_duration_still_skipped(tmp_path, settings_factory):
    """Regression: trade with stale price + opened recently → still skipped."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(PAPER_MAX_DURATION_HOURS=168)
    trade_id = await _open_trade(db, trader, token_id="fresh_stale", opened_hours_ago=2)
    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) VALUES (?, ?, ?)",
        ("fresh_stale", 0.95, stale_ts),
    )
    await db._conn.commit()

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    (status,) = await cur.fetchone()
    assert status == "open"
    await db.close()


@pytest.mark.asyncio
async def test_fresh_price_path_unchanged(tmp_path, settings_factory):
    """Regression: trade with fresh price + opened recently still flows
    through the normal evaluator (BL-061 ladder / legacy cascade) — the
    fix only affects the stale/no-price branches, not the happy path."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(PAPER_MAX_DURATION_HOURS=168)
    trade_id = await _open_trade(db, trader, token_id="happy_path", opened_hours_ago=1)
    # Fresh price exactly at entry — no movement, no exit
    fresh_ts = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) VALUES (?, ?, ?)",
        ("happy_path", 1.00, fresh_ts),
    )
    await db._conn.commit()

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    (status,) = await cur.fetchone()
    assert status == "open"  # fresh-price path: stays open, no force-close
    await db.close()
