"""Tests for PaperTrader -- simulated trade execution with slippage."""

import inspect

import pytest

from scout.db import Database
from scout.trading.paper import PaperTrader


def test_execute_buy_signature_no_bl060_kwargs():
    """BL-061: execute_buy must no longer accept the BL-060 stamping kwargs."""
    sig = inspect.signature(PaperTrader.execute_buy)
    params = set(sig.parameters.keys())
    assert "live_eligible_cap" not in params
    assert "min_quant_score" not in params


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


async def test_execute_partial_sell_updates_remaining_qty(tmp_path):
    from scout.db import Database
    from scout.trading.paper import PaperTrader

    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    trade_id = await trader.execute_buy(
        db=db,
        token_id="tok",
        symbol="TOK",
        name="Token",
        chain="coingecko",
        signal_type="gainers_early",
        signal_data={},
        current_price=1.0,
        amount_usd=300.0,
        tp_pct=40.0,
        sl_pct=15.0,
        slippage_bps=0,
        signal_combo="gainers_early",
    )
    # Sell 30% of position at $1.25 (leg 1 at +25%)
    ok = await trader.execute_partial_sell(
        db=db,
        trade_id=trade_id,
        leg=1,
        sell_qty_frac=0.30,
        current_price=1.25,
        slippage_bps=0,
    )
    assert ok
    cur = await db._conn.execute(
        "SELECT remaining_qty, floor_armed, realized_pnl_usd, leg_1_filled_at, leg_1_exit_price "
        "FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = await cur.fetchone()
    remaining_qty, floor_armed, realized, leg1_filled, leg1_exit = row
    assert remaining_qty == pytest.approx(
        300.0 * 0.70, rel=1e-6
    )  # 210 units at $1.0 entry
    assert floor_armed == 1
    assert realized == pytest.approx(
        300.0 * 0.30 * 0.25, rel=1e-6
    )  # 30% of 300 * 25% = 22.50
    assert leg1_filled is not None
    assert leg1_exit == pytest.approx(1.25, rel=1e-6)
    await db.close()


async def test_execute_partial_sell_idempotent_on_double_call(tmp_path):
    """Second call for the same leg returns False; DB is only updated once."""
    from scout.db import Database
    from scout.trading.paper import PaperTrader

    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    trade_id = await trader.execute_buy(
        db=db,
        token_id="tok",
        symbol="TOK",
        name="Token",
        chain="coingecko",
        signal_type="gainers_early",
        signal_data={},
        current_price=1.0,
        amount_usd=300.0,
        tp_pct=40.0,
        sl_pct=15.0,
        slippage_bps=0,
        signal_combo="gainers_early",
    )
    first = await trader.execute_partial_sell(
        db=db,
        trade_id=trade_id,
        leg=1,
        sell_qty_frac=0.30,
        current_price=1.25,
        slippage_bps=0,
    )
    second = await trader.execute_partial_sell(
        db=db,
        trade_id=trade_id,
        leg=1,
        sell_qty_frac=0.30,
        current_price=1.25,
        slippage_bps=0,
    )
    assert first is True
    assert second is False
    cur = await db._conn.execute(
        "SELECT remaining_qty, realized_pnl_usd FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = await cur.fetchone()
    # remaining_qty decremented only once: 300 * 0.70 = 210
    assert row[0] == pytest.approx(210.0, rel=1e-6)
    # realized_pnl_usd accumulated only once: 300 * 0.30 * 0.25 = 22.50
    assert row[1] == pytest.approx(22.50, rel=1e-6)
    await db.close()


async def test_execute_sell_peak_fade_sets_closed_peak_fade_status(
    tmp_path,
):
    from scout.db import Database
    from scout.trading.paper import PaperTrader

    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    trade_id = await trader.execute_buy(
        db=db,
        token_id="tok-pf",
        symbol="PF",
        name="PeakFade",
        chain="coingecko",
        signal_type="first_signal",
        signal_data={},
        current_price=1.00,
        amount_usd=100.0,
        tp_pct=20.0,
        sl_pct=15.0,
        slippage_bps=0,
        signal_combo="first_signal+momentum_ratio",
    )
    closed = await trader.execute_sell(
        db=db,
        trade_id=trade_id,
        current_price=1.05,
        reason="peak_fade",
        slippage_bps=0,
    )
    assert closed is True
    cur = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    status, reason = await cur.fetchone()
    assert status == "closed_peak_fade"
    assert reason == "peak_fade"
    await db.close()


async def test_execute_sell_runner_folds_in_realized_legs(tmp_path):
    """Phase C regression: closing a BL-061 ladder runner must fold in the
    realized leg proceeds AND use remaining_qty (not the full original quantity),
    else laddered winners are understated/flipped and realized_pnl_usd is silently
    dropped from every closed-trade consumer (combo_performance, auto-suspend,
    calibration, digests, dashboard PnL)."""
    from scout.db import Database
    from scout.trading.paper import PaperTrader

    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    trade_id = await trader.execute_buy(
        db=db,
        token_id="tok-l",
        symbol="LAD",
        name="Ladder",
        chain="coingecko",
        signal_type="gainers_early",
        signal_data={},
        current_price=1.0,
        amount_usd=300.0,  # 300 units at $1.0
        tp_pct=40.0,
        sl_pct=15.0,
        slippage_bps=0,
        signal_combo="gainers_early",
    )
    # Leg 1: sell 30% at $1.25 -> banks 300*0.30*0.25 = +$22.50, remaining 210 units.
    await trader.execute_partial_sell(
        db=db,
        trade_id=trade_id,
        leg=1,
        sell_qty_frac=0.30,
        current_price=1.25,
        slippage_bps=0,
    )
    # Close the runner at $0.90 (runner-leg loss). CORRECT total:
    #   realized(+22.50) + remaining_qty(210) * (0.90 - 1.0) = 22.50 - 21.0 = +1.50
    # The BUG computed full_qty(300)*(0.90-1.0) = -30.0 and overwrote realized.
    closed = await trader.execute_sell(
        db=db,
        trade_id=trade_id,
        current_price=0.90,
        reason="trailing_stop",
        slippage_bps=0,
    )
    assert closed is True
    cur = await db._conn.execute(
        "SELECT pnl_usd, pnl_pct FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    pnl_usd, pnl_pct = await cur.fetchone()
    assert pnl_usd == pytest.approx(1.50, abs=1e-6)  # NOT -30.0
    # Blended over the full original notional (300 * $1.0): 1.50 / 300 * 100 = 0.50%
    assert pnl_pct == pytest.approx(0.50, abs=1e-4)  # NOT -10.0
    await db.close()


async def test_execute_sell_non_laddered_pnl_unchanged(tmp_path):
    """Backward-compat: a non-laddered trade (remaining_qty == quantity,
    realized_pnl_usd == 0) must produce the same pnl as before the ladder-fold fix."""
    from scout.db import Database
    from scout.trading.paper import PaperTrader

    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    trade_id = await trader.execute_buy(
        db=db,
        token_id="tok-n",
        symbol="NL",
        name="NoLadder",
        chain="coingecko",
        signal_type="gainers_early",
        signal_data={},
        current_price=1.0,
        amount_usd=100.0,  # 100 units at $1.0
        tp_pct=20.0,
        sl_pct=15.0,
        slippage_bps=0,
        signal_combo="gainers_early",
    )
    closed = await trader.execute_sell(
        db=db,
        trade_id=trade_id,
        current_price=1.20,
        reason="take_profit",
        slippage_bps=0,
    )
    assert closed is True
    cur = await db._conn.execute(
        "SELECT pnl_usd, pnl_pct FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    pnl_usd, pnl_pct = await cur.fetchone()
    # 100 units * (1.20 - 1.0) = +20.0; pct = 20%.
    assert pnl_usd == pytest.approx(20.0, abs=1e-6)
    assert pnl_pct == pytest.approx(20.0, abs=1e-4)
    await db.close()


async def test_execute_sell_legacy_partial_tp_uses_shrunk_quantity(tmp_path):
    """Legacy pre-cutover partial-TP shrinks `quantity` (NOT remaining_qty), so the
    held qty = min(remaining_qty, quantity) must close the shrunk quantity. Using
    remaining_qty (still original) would overstate the close. Guards the dual-path
    held-qty contract so a future refactor can't silently revert it."""
    from scout.db import Database
    from scout.trading.paper import PaperTrader

    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    trade_id = await trader.execute_buy(
        db=db,
        token_id="tok-lg",
        symbol="LG",
        name="Legacy",
        chain="coingecko",
        signal_type="gainers_early",
        signal_data={},
        current_price=1.0,
        amount_usd=300.0,  # 300 units at $1.0
        tp_pct=20.0,
        sl_pct=15.0,
        slippage_bps=0,
        signal_combo="gainers_early",
    )
    # Simulate the legacy partial-TP: shrink quantity to the sold portion (90 of
    # 300 units); remaining_qty stays at the original 300, realized stays 0.
    await db._conn.execute(
        "UPDATE paper_trades SET quantity = 90.0 WHERE id = ?", (trade_id,)
    )
    await db._conn.commit()
    closed = await trader.execute_sell(
        db=db,
        trade_id=trade_id,
        current_price=1.20,
        reason="take_profit",
        slippage_bps=0,
    )
    assert closed is True
    cur = await db._conn.execute(
        "SELECT pnl_usd, pnl_pct FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    pnl_usd, pnl_pct = await cur.fetchone()
    # held_qty = min(300, 90) = 90 -> 90 * (1.20 - 1.0) = +18.0; pct = 18/90 = 20%.
    # (Using remaining_qty=300 would WRONGLY give 60.0.)
    assert pnl_usd == pytest.approx(18.0, abs=1e-6)
    assert pnl_pct == pytest.approx(20.0, abs=1e-4)
    await db.close()


# ---------------------------------------------------------------------------
# GA-11: fire-and-forget live-handoff tasks must log exceptions
# ---------------------------------------------------------------------------


async def test_live_handoff_task_exception_is_logged():
    """A failing _pending_live_tasks task must emit live_handoff_task_failed."""
    import asyncio

    import structlog

    from scout.trading.paper import _log_live_handoff_task_exception

    async def _boom():
        raise ValueError("handoff kaput")

    task = asyncio.get_event_loop().create_task(_boom())
    with pytest.raises(ValueError):
        await task

    with structlog.testing.capture_logs() as logs:
        _log_live_handoff_task_exception(task)

    events = [e for e in logs if e["event"] == "live_handoff_task_failed"]
    assert len(events) == 1
    assert "handoff kaput" in events[0]["error"]


async def test_live_handoff_task_cancelled_does_not_log_or_raise():
    """Cancelled tasks must not be reported as failures."""
    import asyncio

    import structlog

    from scout.trading.paper import _log_live_handoff_task_exception

    task = asyncio.get_event_loop().create_task(asyncio.sleep(30))
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    with structlog.testing.capture_logs() as logs:
        _log_live_handoff_task_exception(task)

    assert not [e for e in logs if e["event"] == "live_handoff_task_failed"]
