import pytest
import structlog
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


async def test_pre_cutover_rows_use_old_policy(tmp_path, settings_factory):
    from scout.trading.paper import PaperTrader
    from scout.trading.evaluator import evaluate_paper_trades
    from datetime import timedelta

    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory()
    trade_id = await trader.execute_buy(
        db=db, token_id="old", symbol="OLD", name="Old", chain="coingecko",
        signal_type="gainers_early", signal_data={}, current_price=1.00,
        amount_usd=300.0, tp_pct=40.0, sl_pct=10.0, slippage_bps=0,
        signal_combo="gainers_early",
    )
    # Backdate created_at to 1 day ago and null-out ladder state to simulate
    # a pre-BL-061 row that pre-dates the migration.
    old_ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    await db._conn.execute(
        "UPDATE paper_trades SET created_at = ?, "
        "remaining_qty = NULL, floor_armed = NULL, realized_pnl_usd = NULL "
        "WHERE id = ?",
        (old_ts, trade_id),
    )
    # Move the cutover_ts forward so the backdated row is definitively pre-cutover
    now_ts = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "UPDATE paper_migrations SET cutover_ts = ? WHERE name = 'bl061_ladder'",
        (now_ts,),
    )
    await db._conn.commit()

    # Price at +26% — new policy would fire leg 1; old policy should NOT partial-sell
    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        ("old", 1.26, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT leg_1_filled_at, status FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    leg1, status = await cur.fetchone()
    assert leg1 is None, "pre-cutover row must not fire ladder legs"
    assert status == "open", "pre-cutover row still open (old policy TP at +40%, not +25%)"
    await db.close()


async def test_ladder_leg_fired_log_includes_peak_pct(tmp_path, settings_factory):
    """ladder_leg_fired and floor_activated logs must carry peak_pct for later calibration."""
    from scout.trading.paper import PaperTrader
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings_factory()  # normalize knobs; not used here
    trade_id = await trader.execute_buy(
        db=db, token_id="pk", symbol="PK", name="Peak", chain="coingecko",
        signal_type="gainers_early", signal_data={}, current_price=1.00,
        amount_usd=300.0, tp_pct=40.0, sl_pct=15.0, slippage_bps=0,
        signal_combo="gainers_early",
    )
    # Seed a peak of +28% before firing leg 1 at +26% current
    await db._conn.execute(
        "UPDATE paper_trades SET peak_price = 1.28, peak_pct = 28.0 WHERE id = ?",
        (trade_id,),
    )
    await db._conn.commit()

    with structlog.testing.capture_logs() as logs:
        ok = await trader.execute_partial_sell(
            db=db, trade_id=trade_id, leg=1, sell_qty_frac=0.30,
            current_price=1.26, slippage_bps=0,
        )
    assert ok is True
    fired = [e for e in logs if e["event"] == "ladder_leg_fired"]
    activated = [e for e in logs if e["event"] == "floor_activated"]
    assert fired and fired[0]["peak_pct_at_fire"] == 28.0
    assert activated and activated[0]["peak_pct_at_activation"] == 28.0
    await db.close()


async def test_partial_sell_race_lost_when_another_writer_fills_first(
    tmp_path, settings_factory
):
    """If WHERE leg_N_filled_at IS NULL filters the row out, log partial_sell_race_lost."""
    from scout.trading.paper import PaperTrader
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings_factory()
    trade_id = await trader.execute_buy(
        db=db, token_id="rc", symbol="RC", name="Race", chain="coingecko",
        signal_type="gainers_early", signal_data={}, current_price=1.00,
        amount_usd=300.0, tp_pct=40.0, sl_pct=15.0, slippage_bps=0,
        signal_combo="gainers_early",
    )

    # Inject a race: after the SELECT inside execute_partial_sell reads
    # already_filled=None, another writer stamps leg_1_filled_at before our
    # UPDATE runs. The UPDATE's WHERE clause then filters it out.
    #
    # Sentinel "SELECT entry_price" matches the first SELECT in
    # execute_partial_sell (scout/trading/paper.py — the one that reads
    # entry_price, quantity, remaining_qty, realized_pnl_usd, leg_N_filled_at,
    # peak_pct). If that SELECT's leading column changes or gets split across
    # subqueries, this match stops firing and the race never gets injected —
    # update the sentinel in lockstep with the paper.py SELECT.
    orig_execute = db._conn.execute
    state = {"select_seen": False}

    async def racing_execute(sql, *args, **kwargs):
        result = await orig_execute(sql, *args, **kwargs)
        if (not state["select_seen"]) and "SELECT entry_price" in str(sql):
            state["select_seen"] = True
            await orig_execute(
                "UPDATE paper_trades SET leg_1_filled_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), trade_id),
            )
        return result

    db._conn.execute = racing_execute  # type: ignore[assignment]
    try:
        with structlog.testing.capture_logs() as logs:
            ok = await trader.execute_partial_sell(
                db=db, trade_id=trade_id, leg=1, sell_qty_frac=0.30,
                current_price=1.25, slippage_bps=0,
            )
    finally:
        db._conn.execute = orig_execute  # type: ignore[assignment]

    assert ok is False
    assert any(e["event"] == "partial_sell_race_lost" for e in logs)
    await db.close()


async def test_bl062_cutover_ts_returns_iso_timestamp(tmp_path):
    from scout.db import Database
    from scout.trading.evaluator import _load_bl062_cutover_ts
    from datetime import datetime

    db = Database(tmp_path / "t.db")
    await db.initialize()
    ts = await _load_bl062_cutover_ts(db._conn)
    assert ts is not None, "loader must return the cutover_ts written by migration"
    parsed = datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None
    await db.close()


async def test_bl062_cutover_ts_returns_none_when_missing(tmp_path):
    from scout.db import Database
    from scout.trading.evaluator import _load_bl062_cutover_ts

    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Simulate a corrupted DB where the row was manually deleted
    await db._conn.execute(
        "DELETE FROM paper_migrations WHERE name = 'bl062_peak_fade'"
    )
    await db._conn.commit()
    ts = await _load_bl062_cutover_ts(db._conn)
    assert ts is None
    await db.close()
