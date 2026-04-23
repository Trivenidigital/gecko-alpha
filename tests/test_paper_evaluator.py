import pytest
from datetime import datetime, timezone
from scout.db import Database
from scout.trading.evaluator import _load_bl061_cutover_ts


async def test_cutover_ts_returns_iso_timestamp(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ts = await _load_bl061_cutover_ts(db._conn)
    assert ts is not None
    parsed = datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None
    await db.close()


async def test_ladder_leg_1_fires_at_25_percent(tmp_path, settings_factory):
    from scout.trading.paper import PaperTrader
    from scout.trading.evaluator import evaluate_paper_trades
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_LADDER_LEG_1_PCT=25.0,
        PAPER_LADDER_LEG_1_QTY_FRAC=0.30,
        PAPER_LADDER_LEG_2_PCT=50.0,
        PAPER_LADDER_TRAIL_PCT=12.0,
        PAPER_SL_PCT=15.0,
    )
    trade_id = await trader.execute_buy(
        db=db, token_id="tok", symbol="TOK", name="Token", chain="coingecko",
        signal_type="gainers_early", signal_data={}, current_price=1.00,
        amount_usd=300.0, tp_pct=40.0, sl_pct=15.0, slippage_bps=0,
        signal_combo="gainers_early",
    )
    # Seed price_cache at +26% (above leg 1 threshold)
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        ("tok", 1.26, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT leg_1_filled_at, floor_armed, remaining_qty FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    leg1, floor_armed, remaining = await cur.fetchone()
    assert leg1 is not None
    assert floor_armed == 1
    assert remaining == pytest.approx(300.0 * 0.70, rel=1e-6)
    await db.close()


async def test_ladder_leg_2_fires_at_50_percent(tmp_path, settings_factory):
    from scout.trading.paper import PaperTrader
    from scout.trading.evaluator import evaluate_paper_trades
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory()
    trade_id = await trader.execute_buy(
        db=db, token_id="tok2", symbol="TOK2", name="T2", chain="coingecko",
        signal_type="gainers_early", signal_data={}, current_price=1.00,
        amount_usd=300.0, tp_pct=40.0, sl_pct=15.0, slippage_bps=0,
        signal_combo="gainers_early",
    )
    await trader.execute_partial_sell(
        db=db, trade_id=trade_id, leg=1, sell_qty_frac=0.30,
        current_price=1.25, slippage_bps=0,
    )
    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        ("tok2", 1.55, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT leg_2_filled_at FROM paper_trades WHERE id = ?", (trade_id,)
    )
    (leg_2,) = await cur.fetchone()
    assert leg_2 is not None
    await db.close()


async def test_floor_blocks_below_entry_close(tmp_path, settings_factory):
    from scout.trading.paper import PaperTrader
    from scout.trading.evaluator import evaluate_paper_trades
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory()
    trade_id = await trader.execute_buy(
        db=db, token_id="tok3", symbol="TOK3", name="T3", chain="coingecko",
        signal_type="trending_catch", signal_data={}, current_price=1.00,
        amount_usd=300.0, tp_pct=40.0, sl_pct=15.0, slippage_bps=0,
        signal_combo="trending_catch",
    )
    await trader.execute_partial_sell(
        db=db, trade_id=trade_id, leg=1, sell_qty_frac=0.30,
        current_price=1.25, slippage_bps=0,
    )
    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        ("tok3", 0.98, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    (status,) = await cur.fetchone()
    assert status == "closed_floor"
    await db.close()


async def test_sl_at_15_fires_pre_leg_1(tmp_path, settings_factory):
    from scout.trading.paper import PaperTrader
    from scout.trading.evaluator import evaluate_paper_trades
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory()
    trade_id = await trader.execute_buy(
        db=db, token_id="tok4", symbol="TOK4", name="T4", chain="coingecko",
        signal_type="gainers_early", signal_data={}, current_price=1.00,
        amount_usd=300.0, tp_pct=40.0, sl_pct=15.0, slippage_bps=0,
        signal_combo="gainers_early",
    )
    # Drop to 0.849 → past -15% SL (sl_price = 1.0 * 0.85 = 0.85)
    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        ("tok4", 0.849, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    (status,) = await cur.fetchone()
    assert status == "closed_sl"
    await db.close()


async def test_trailing_stop_on_runner_only_after_leg_1(tmp_path, settings_factory):
    from scout.trading.paper import PaperTrader
    from scout.trading.evaluator import evaluate_paper_trades
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory()
    trade_id = await trader.execute_buy(
        db=db, token_id="tok5", symbol="TOK5", name="T5", chain="coingecko",
        signal_type="gainers_early", signal_data={}, current_price=1.00,
        amount_usd=300.0, tp_pct=40.0, sl_pct=15.0, slippage_bps=0,
        signal_combo="gainers_early",
    )
    await trader.execute_partial_sell(
        db=db, trade_id=trade_id, leg=1, sell_qty_frac=0.30,
        current_price=1.25, slippage_bps=0,
    )
    # Manually set peak to +45% (post-leg-1)
    await db._conn.execute(
        "UPDATE paper_trades SET peak_price = 1.45, peak_pct = 45.0 WHERE id = ?",
        (trade_id,),
    )
    # Retrace to 1.25 — down 13.8% from peak 1.45, past 12% trail threshold
    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        ("tok5", 1.25, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    (status,) = await cur.fetchone()
    assert status == "closed_trailing_stop"
    await db.close()
