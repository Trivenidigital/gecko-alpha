"""Spec §6.2 — transactional daily loss cap + §11.5 concurrent-close race."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from scout.config import Settings
from scout.db import Database
from scout.live.kill_switch import KillSwitch, maybe_trigger_from_daily_loss


def _s(cap_usd=50, *, live_enabled=False):
    return Settings(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        LIVE_DAILY_LOSS_CAP_USD=Decimal(cap_usd),
        LIVE_TRADING_ENABLED=live_enabled,
    )


async def _seed_closed_live(db: Database, pnl: float, close_date: str | None = None):
    """Seed one closed live_trade (+ the required paper_trades parent row).

    LIVE-04: the daily-loss cap must count real live PnL when
    LIVE_TRADING_ENABLED=True. live_trades has NOT NULL columns
    (paper_trade_id FK, coin_id, symbol, venue, pair, signal_type, size_usd,
    status, created_at); realized_pnl_usd + closed_at carry the loss.
    """
    assert db._conn is not None
    if close_date is None:
        close_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    await db._conn.execute(
        "INSERT OR IGNORE INTO paper_trades "
        "(id, token_id, symbol, name, chain, signal_type, signal_data, "
        " entry_price, amount_usd, quantity, "
        " tp_price, sl_price, status, opened_at) "
        "VALUES (1, 'tok', 'TOK', 'Tok', 'eth', 'first_signal', '{}', "
        " 1.0, 100.0, 100.0, 1.2, 0.9, 'open', ?)",
        (f"{close_date}T00:00:00Z",),
    )
    await db._conn.execute(
        "INSERT INTO live_trades "
        "(paper_trade_id, coin_id, symbol, venue, pair, signal_type, size_usd, "
        " status, realized_pnl_usd, created_at, closed_at) "
        "VALUES (1,'c','L','binance','LUSDT','fs','100','closed_sl',?,?,?)",
        (str(pnl), f"{close_date}T00:00:00Z", f"{close_date}T00:30:00Z"),
    )
    await db._conn.commit()


async def _seed_closed(db: Database, pnl: float, close_date: str | None = None):
    """Seed one closed shadow_trade (+ the required paper_trades parent row).

    paper_trades has many NOT NULL columns and shadow_trades.paper_trade_id is
    a FK with ON DELETE RESTRICT, so we INSERT OR IGNORE the parent first.
    """
    assert db._conn is not None
    if close_date is None:
        close_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Parent row — only inserted once per db instance.
    await db._conn.execute(
        "INSERT OR IGNORE INTO paper_trades "
        "(id, token_id, symbol, name, chain, signal_type, signal_data, "
        " entry_price, amount_usd, quantity, "
        " tp_price, sl_price, status, opened_at) "
        "VALUES (1, 'tok', 'TOK', 'Tok', 'eth', 'first_signal', '{}', "
        " 1.0, 100.0, 100.0, 1.2, 0.9, 'open', ?)",
        (f"{close_date}T00:00:00Z",),
    )
    await db._conn.execute(
        "INSERT INTO shadow_trades "
        "(paper_trade_id, coin_id, symbol, venue, pair, signal_type, size_usd, "
        " status, realized_pnl_usd, created_at, closed_at) "
        "VALUES (1,'c','S','binance','SUSDT','fs','100','closed_sl',?,?,?)",
        (str(pnl), f"{close_date}T00:00:00Z", f"{close_date}T00:30:00Z"),
    )
    await db._conn.commit()


async def test_single_close_under_cap_does_not_trigger(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_closed(db, -25.0)
    ks = KillSwitch(db)
    triggered = await maybe_trigger_from_daily_loss(db, ks, _s(50))
    assert triggered is False
    assert await ks.is_active() is None
    await db.close()


async def test_single_close_over_cap_triggers(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_closed(db, -60.0)
    ks = KillSwitch(db)
    triggered = await maybe_trigger_from_daily_loss(db, ks, _s(50))
    assert triggered is True
    assert await ks.is_active() is not None
    await db.close()


async def test_two_concurrent_closes_trigger_exactly_once(tmp_path):
    """Spec §11.5: A=-$30, B=-$25 each racing close → one kill, idempotent."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_closed(db, -30.0)
    await _seed_closed(db, -25.0)
    ks = KillSwitch(db)
    results = await asyncio.gather(
        maybe_trigger_from_daily_loss(db, ks, _s(50)),
        maybe_trigger_from_daily_loss(db, ks, _s(50)),
    )
    assert sum(results) == 1
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM kill_events WHERE cleared_at IS NULL"
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()


async def test_live_loss_over_cap_triggers_when_enabled(tmp_path):
    """LIVE-04 negative test: a real live loss > cap trips the kill switch when
    LIVE_TRADING_ENABLED=True. Before the fix the cap summed shadow_trades only,
    so live fills were invisible and the brake never fired."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_closed_live(db, -60.0)
    ks = KillSwitch(db)
    triggered = await maybe_trigger_from_daily_loss(db, ks, _s(50, live_enabled=True))
    assert triggered is True
    assert await ks.is_active() is not None
    await db.close()


async def test_live_loss_ignored_when_live_disabled(tmp_path):
    """LIVE-04: with LIVE_TRADING_ENABLED=False the live union is not applied —
    a live loss does not trip the (shadow-scoped) cap."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_closed_live(db, -60.0)
    ks = KillSwitch(db)
    triggered = await maybe_trigger_from_daily_loss(db, ks, _s(50, live_enabled=False))
    assert triggered is False
    assert await ks.is_active() is None
    await db.close()


async def test_combined_shadow_and_live_sum_trips_cap(tmp_path):
    """LIVE-04: shadow + live closed losses on the same UTC day sum together —
    each under cap alone, jointly over."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_closed(db, -30.0)  # shadow leg
    await _seed_closed_live(db, -25.0)  # live leg
    ks = KillSwitch(db)
    triggered = await maybe_trigger_from_daily_loss(db, ks, _s(50, live_enabled=True))
    assert triggered is True
    assert await ks.is_active() is not None
    await db.close()


async def test_kill_trigger_errors_metric_on_failure(tmp_path, monkeypatch):
    """If ks.trigger() raises, maybe_trigger_from_daily_loss must increment
    kill_trigger_errors and log live_kill_trigger_failed at ERROR before
    re-raising."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_closed(db, -60.0)
    ks = KillSwitch(db)

    async def _boom(**_kw):
        raise RuntimeError("simulated kill store failure")

    monkeypatch.setattr(ks, "trigger", _boom)
    with pytest.raises(RuntimeError):
        await maybe_trigger_from_daily_loss(db, ks, _s(50))
    cur = await db._conn.execute(
        "SELECT value FROM live_metrics_daily WHERE metric='kill_trigger_errors'"
    )
    row = await cur.fetchone()
    assert row is not None and row[0] == 1
    await db.close()
