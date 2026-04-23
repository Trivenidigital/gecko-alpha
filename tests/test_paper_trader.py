"""Tests for PaperTrader -- simulated trade execution with slippage."""

import json
from datetime import datetime, timezone

import pytest

from scout.db import Database
from scout.trading.paper import PaperTrader


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
def trader():
    return PaperTrader()


async def test_execute_buy_inserts_trade(db, trader):
    """execute_buy creates a paper trade row in the DB."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={"spike_ratio": 12.3},
        current_price=50000.0,
        amount_usd=1000.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=50,
        signal_combo="volume_spike",
        live_eligible_cap=20,
        min_quant_score=0,
    )
    assert trade_id is not None
    cursor = await db._conn.execute(
        "SELECT * FROM paper_trades WHERE id = ?", (trade_id,)
    )
    row = dict(await cursor.fetchone())
    assert row["token_id"] == "bitcoin"
    assert row["status"] == "open"


async def test_execute_buy_applies_slippage(db, trader):
    """Entry price includes slippage: effective_entry = price * (1 + bps/10000)."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        current_price=10000.0,
        amount_usd=1000.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=100,  # 1%
        signal_combo="volume_spike",
        live_eligible_cap=20,
        min_quant_score=0,
    )
    cursor = await db._conn.execute(
        "SELECT entry_price, quantity FROM paper_trades WHERE id = ?", (trade_id,)
    )
    row = dict(await cursor.fetchone())
    # effective_entry = 10000 * (1 + 100/10000) = 10100
    assert row["entry_price"] == pytest.approx(10100.0)
    # quantity = 1000 / 10100
    assert row["quantity"] == pytest.approx(1000.0 / 10100.0)


async def test_execute_buy_computes_tp_sl_prices(db, trader):
    """TP and SL prices are computed from effective entry price."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id="ethereum",
        symbol="ETH",
        name="Ethereum",
        chain="coingecko",
        signal_type="narrative_prediction",
        signal_data={"fit": 85},
        current_price=3000.0,
        amount_usd=1000.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=0,  # no slippage
        signal_combo="narrative_prediction",
        live_eligible_cap=20,
        min_quant_score=0,
    )
    cursor = await db._conn.execute(
        "SELECT tp_price, sl_price FROM paper_trades WHERE id = ?", (trade_id,)
    )
    row = dict(await cursor.fetchone())
    # tp_price = 3000 * (1 + 20/100) = 3600
    assert row["tp_price"] == pytest.approx(3600.0)
    # sl_price = 3000 * (1 - 10/100) = 2700
    assert row["sl_price"] == pytest.approx(2700.0)


async def test_execute_sell_closes_trade(db, trader):
    """execute_sell closes a trade and computes PnL."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        current_price=50000.0,
        amount_usd=1000.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=0,
        signal_combo="volume_spike",
        live_eligible_cap=20,
        min_quant_score=0,
    )
    await trader.execute_sell(
        db=db,
        trade_id=trade_id,
        current_price=60000.0,
        reason="take_profit",
        slippage_bps=0,
    )
    cursor = await db._conn.execute(
        "SELECT status, exit_price, pnl_usd, pnl_pct FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = dict(await cursor.fetchone())
    assert row["status"] == "closed_tp"
    assert row["exit_price"] == pytest.approx(60000.0)
    assert row["pnl_pct"] == pytest.approx(20.0)
    assert row["pnl_usd"] == pytest.approx(200.0)


async def test_execute_sell_applies_exit_slippage(db, trader):
    """Exit price includes slippage: effective_exit = price * (1 - bps/10000)."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        current_price=10000.0,
        amount_usd=1000.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=0,
        signal_combo="volume_spike",
        live_eligible_cap=20,
        min_quant_score=0,
    )
    await trader.execute_sell(
        db=db,
        trade_id=trade_id,
        current_price=12000.0,
        reason="take_profit",
        slippage_bps=100,  # 1% exit slippage
    )
    cursor = await db._conn.execute(
        "SELECT exit_price FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = dict(await cursor.fetchone())
    # effective_exit = 12000 * (1 - 100/10000) = 11880
    assert row["exit_price"] == pytest.approx(11880.0)


async def test_execute_sell_stop_loss_pnl(db, trader):
    """PnL is negative on a stop loss."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        current_price=50000.0,
        amount_usd=1000.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=0,
        signal_combo="volume_spike",
        live_eligible_cap=20,
        min_quant_score=0,
    )
    await trader.execute_sell(
        db=db,
        trade_id=trade_id,
        current_price=45000.0,
        reason="stop_loss",
        slippage_bps=0,
    )
    cursor = await db._conn.execute(
        "SELECT pnl_usd, pnl_pct FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = dict(await cursor.fetchone())
    assert row["pnl_pct"] == pytest.approx(-10.0)
    assert row["pnl_usd"] == pytest.approx(-100.0)
