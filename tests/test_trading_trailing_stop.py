"""Tests for trailing-stop exit logic on paper trades."""

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading.evaluator import evaluate_paper_trades
from tests.test_trading_evaluator import _insert_trade, _seed_price


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


def _settings_factory(tmp_path, **overrides):
    from scout.config import Settings

    defaults = dict(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_CHAT_ID="test",
        ANTHROPIC_API_KEY="test",
        DB_PATH=tmp_path / "test.db",
        PAPER_TP_PCT=40.0,
        PAPER_SL_PCT=20.0,
        PAPER_SLIPPAGE_BPS=0,
        PAPER_MAX_DURATION_HOURS=48,
        PAPER_TRAILING_ENABLED=True,
        PAPER_TRAILING_ACTIVATION_PCT=10.0,
        PAPER_TRAILING_DRAWDOWN_PCT=10.0,
        PAPER_TRAILING_FLOOR_PCT=3.0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


async def test_trailing_stop_closes_when_price_drops_from_peak(db, tmp_path):
    """If peak hit >= activation and price falls by drawdown%, close at trailing_stop."""
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    # Seed trade with existing peak already above activation: peak=+20%, entry=100
    trade_id = await _insert_trade(
        db,
        "bitcoin",
        100.0,
        opened,
        tp_price=140.0,
        sl_price=80.0,
    )
    # Manually set peak to 120 (+20%)
    await db._conn.execute(
        "UPDATE paper_trades SET peak_price = ?, peak_pct = ? WHERE id = ?",
        (120.0, 20.0, trade_id),
    )
    await db._conn.commit()
    # Current price drops to 107 (peak-10.8%, still above entry+3%=103)
    await _seed_price(db, "bitcoin", 107.0)

    await evaluate_paper_trades(db, settings)

    cursor = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] in (
        "closed_tp",
        "closed_sl",
        "closed_expired",
        "closed_trailing_stop",
    )
    assert row[1] == "trailing_stop"


async def test_trailing_stop_does_not_fire_below_activation(db, tmp_path):
    """If peak < activation_pct, trailing stop does not fire."""
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    trade_id = await _insert_trade(
        db,
        "bitcoin",
        100.0,
        opened,
        tp_price=140.0,
        sl_price=80.0,
    )
    # Peak only +5% (below activation=10%)
    await db._conn.execute(
        "UPDATE paper_trades SET peak_price = ?, peak_pct = ? WHERE id = ?",
        (105.0, 5.0, trade_id),
    )
    await db._conn.commit()
    # Price dropped 10% from peak but peak never reached activation
    await _seed_price(db, "bitcoin", 94.5)

    await evaluate_paper_trades(db, settings)

    cursor = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == "open"
    assert row[1] is None


async def test_trailing_stop_respects_floor(db, tmp_path):
    """Trailing stop does not close below entry + floor_pct (to avoid cutting gains below breakeven)."""
    settings = _settings_factory(tmp_path, PAPER_TRAILING_FLOOR_PCT=5.0)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    trade_id = await _insert_trade(
        db,
        "bitcoin",
        100.0,
        opened,
        tp_price=140.0,
        sl_price=80.0,
    )
    # Peak at +15%, but price already below entry+5%=105 (floor)
    await db._conn.execute(
        "UPDATE paper_trades SET peak_price = ?, peak_pct = ? WHERE id = ?",
        (115.0, 15.0, trade_id),
    )
    await db._conn.commit()
    # Price at 102 — below entry+5% floor, let regular SL handle it
    await _seed_price(db, "bitcoin", 102.0)

    await evaluate_paper_trades(db, settings)

    cursor = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = await cursor.fetchone()
    # Must NOT close as trailing_stop; still open (regular SL not yet hit either)
    assert row[1] != "trailing_stop"


async def test_trailing_stop_disabled_by_flag(db, tmp_path):
    """PAPER_TRAILING_ENABLED=False bypasses trailing stop entirely."""
    settings = _settings_factory(tmp_path, PAPER_TRAILING_ENABLED=False)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    trade_id = await _insert_trade(
        db,
        "bitcoin",
        100.0,
        opened,
        tp_price=140.0,
        sl_price=80.0,
    )
    await db._conn.execute(
        "UPDATE paper_trades SET peak_price = ?, peak_pct = ? WHERE id = ?",
        (120.0, 20.0, trade_id),
    )
    await db._conn.commit()
    # Would trigger trailing stop if enabled
    await _seed_price(db, "bitcoin", 107.0)

    await evaluate_paper_trades(db, settings)

    cursor = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == "open"
    assert row[1] is None


async def test_trailing_stop_applies_to_long_hold(db, tmp_path):
    """Trailing stop should also protect long_hold positions (no SL by default)."""
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    # long_hold trades have sl_price=0 (no SL) and tp_price=very high
    trade_id = await _insert_trade(
        db,
        "ethereum",
        100.0,
        opened,
        signal_type="long_hold",
        tp_price=200.0,  # 100% TP
        sl_price=0.0,  # no SL
        tp_pct=100.0,
        sl_pct=0.0,
    )
    # Peak at +30%, then drops 10%
    await db._conn.execute(
        "UPDATE paper_trades SET peak_price = ?, peak_pct = ? WHERE id = ?",
        (130.0, 30.0, trade_id),
    )
    await db._conn.commit()
    await _seed_price(
        db, "ethereum", 116.0
    )  # down ~10.8% from peak, still +16% vs entry

    await evaluate_paper_trades(db, settings)

    cursor = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[1] == "trailing_stop"
