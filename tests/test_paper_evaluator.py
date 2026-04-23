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
