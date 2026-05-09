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
        db=db,
        token_id="tok",
        symbol="TOK",
        name="Token",
        chain="coingecko",
        signal_type="gainers_early",
        signal_data={},
        current_price=1.00,
        amount_usd=300.0,
        tp_pct=40.0,
        sl_pct=15.0,
        slippage_bps=0,
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
        db=db,
        token_id="tok2",
        symbol="TOK2",
        name="T2",
        chain="coingecko",
        signal_type="gainers_early",
        signal_data={},
        current_price=1.00,
        amount_usd=300.0,
        tp_pct=40.0,
        sl_pct=15.0,
        slippage_bps=0,
        signal_combo="gainers_early",
    )
    await trader.execute_partial_sell(
        db=db,
        trade_id=trade_id,
        leg=1,
        sell_qty_frac=0.30,
        current_price=1.25,
        slippage_bps=0,
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
        db=db,
        token_id="tok3",
        symbol="TOK3",
        name="T3",
        chain="coingecko",
        signal_type="trending_catch",
        signal_data={},
        current_price=1.00,
        amount_usd=300.0,
        tp_pct=40.0,
        sl_pct=15.0,
        slippage_bps=0,
        signal_combo="trending_catch",
    )
    await trader.execute_partial_sell(
        db=db,
        trade_id=trade_id,
        leg=1,
        sell_qty_frac=0.30,
        current_price=1.25,
        slippage_bps=0,
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
        db=db,
        token_id="tok4",
        symbol="TOK4",
        name="T4",
        chain="coingecko",
        signal_type="gainers_early",
        signal_data={},
        current_price=1.00,
        amount_usd=300.0,
        tp_pct=40.0,
        sl_pct=15.0,
        slippage_bps=0,
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
        db=db,
        token_id="tok5",
        symbol="TOK5",
        name="T5",
        chain="coingecko",
        signal_type="gainers_early",
        signal_data={},
        current_price=1.00,
        amount_usd=300.0,
        tp_pct=40.0,
        sl_pct=15.0,
        slippage_bps=0,
        signal_combo="gainers_early",
    )
    await trader.execute_partial_sell(
        db=db,
        trade_id=trade_id,
        leg=1,
        sell_qty_frac=0.30,
        current_price=1.25,
        slippage_bps=0,
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
        db=db,
        token_id="old",
        symbol="OLD",
        name="Old",
        chain="coingecko",
        signal_type="gainers_early",
        signal_data={},
        current_price=1.00,
        amount_usd=300.0,
        tp_pct=40.0,
        sl_pct=10.0,
        slippage_bps=0,
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
    assert (
        status == "open"
    ), "pre-cutover row still open (old policy TP at +40%, not +25%)"
    await db.close()


async def test_ladder_leg_fired_log_includes_peak_pct(tmp_path, settings_factory):
    """ladder_leg_fired and floor_activated logs must carry peak_pct for later calibration."""
    from scout.trading.paper import PaperTrader

    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings_factory()  # normalize knobs; not used here
    trade_id = await trader.execute_buy(
        db=db,
        token_id="pk",
        symbol="PK",
        name="Peak",
        chain="coingecko",
        signal_type="gainers_early",
        signal_data={},
        current_price=1.00,
        amount_usd=300.0,
        tp_pct=40.0,
        sl_pct=15.0,
        slippage_bps=0,
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
            db=db,
            trade_id=trade_id,
            leg=1,
            sell_qty_frac=0.30,
            current_price=1.26,
            slippage_bps=0,
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
        db=db,
        token_id="rc",
        symbol="RC",
        name="Race",
        chain="coingecko",
        signal_type="gainers_early",
        signal_data={},
        current_price=1.00,
        amount_usd=300.0,
        tp_pct=40.0,
        sl_pct=15.0,
        slippage_bps=0,
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
                db=db,
                trade_id=trade_id,
                leg=1,
                sell_qty_frac=0.30,
                current_price=1.25,
                slippage_bps=0,
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


# ---------------------------------------------------------------------------
# BL-062 peak-fade: fire conditions, precedence, remaining_qty, fire-once
# ---------------------------------------------------------------------------


async def _seed_post_leg1_trade(db, token_id, settings):
    """Helper: open a trade and arm the ladder floor via a simulated leg 1 fill.

    Leaves remaining_qty at 70% of original amount_usd, floor_armed=1,
    peak_pct set to the argument peak_at_seed. Caller seeds price_cache
    and checkpoint columns as needed.
    """
    from scout.trading.paper import PaperTrader
    from datetime import datetime, timezone

    trader = PaperTrader()
    trade_id = await trader.execute_buy(
        db=db,
        token_id=token_id,
        symbol=token_id.upper(),
        name=token_id,
        chain="coingecko",
        signal_type="first_signal",
        signal_data={},
        current_price=1.00,
        amount_usd=100.0,
        tp_pct=40.0,
        sl_pct=15.0,
        slippage_bps=0,
        signal_combo="first_signal+momentum_ratio",
    )
    # Simulate leg 1 fill at +25% — arms the floor, reduces qty to 70%
    await trader.execute_partial_sell(
        db=db,
        trade_id=trade_id,
        leg=1,
        sell_qty_frac=settings.PAPER_LADDER_LEG_1_QTY_FRAC,
        current_price=1.25,
        slippage_bps=0,
    )
    # Backdate opened_at/created_at to 25h ago so the 24h checkpoint path is legal.
    # Also move the BL-061 cutover_ts back to 30h ago so the backdated trade is
    # still classified as post-cutover (created_at >= cutover_ts).
    twenty_five_h_ago = datetime.now(timezone.utc).timestamp() - 25 * 3600
    thirty_h_ago = datetime.now(timezone.utc).timestamp() - 30 * 3600
    backdate_iso = datetime.fromtimestamp(twenty_five_h_ago, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    cutover_iso = datetime.fromtimestamp(thirty_h_ago, tz=timezone.utc).isoformat()
    await db._conn.execute(
        "UPDATE paper_trades SET opened_at = ?, created_at = ? WHERE id = ?",
        (backdate_iso, backdate_iso, trade_id),
    )
    await db._conn.execute(
        "UPDATE paper_migrations SET cutover_ts = ? WHERE name = 'bl061_ladder'",
        (cutover_iso,),
    )
    await db._conn.commit()
    return trade_id


async def _set_checkpoints_and_peak(db, trade_id, *, peak_pct, cp_6h_pct, cp_24h_pct):
    """Manually set peak_pct + both checkpoint_*_pct columns."""
    await db._conn.execute(
        "UPDATE paper_trades SET peak_pct = ?, peak_price = ?, "
        "checkpoint_6h_pct = ?, checkpoint_6h_price = ?, "
        "checkpoint_24h_pct = ?, checkpoint_24h_price = ? "
        "WHERE id = ?",
        (
            peak_pct,
            1.0 + peak_pct / 100,
            cp_6h_pct,
            1.0 + cp_6h_pct / 100,
            cp_24h_pct,
            1.0 + cp_24h_pct / 100,
            trade_id,
        ),
    )
    await db._conn.commit()


async def _seed_current_price(db, token_id, price):
    from datetime import datetime, timezone

    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        (token_id, price, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()


async def test_peak_fade_fires_when_both_checkpoints_below_ratio(
    tmp_path, settings_factory
):
    from scout.db import Database
    from scout.trading.evaluator import evaluate_paper_trades

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(
        PEAK_FADE_ENABLED=True,
        PEAK_FADE_MIN_PEAK_PCT=10.0,
        PEAK_FADE_RETRACE_RATIO=0.7,
    )
    trade_id = await _seed_post_leg1_trade(db, "tok-pf1", settings)
    # peak = 20%, 0.7 * 20 = 14. Both cps below 14 → fire.
    await _set_checkpoints_and_peak(
        db, trade_id, peak_pct=20.0, cp_6h_pct=10.0, cp_24h_pct=8.0
    )
    await _seed_current_price(db, "tok-pf1", 1.08)  # current +8%

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT status, exit_reason, peak_fade_fired_at "
        "FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    status, reason, fired_at = await cur.fetchone()
    assert status == "closed_peak_fade"
    assert reason == "peak_fade"
    assert fired_at is not None
    await db.close()


async def test_peak_fade_no_fire_when_peak_below_threshold(tmp_path, settings_factory):
    from scout.db import Database
    from scout.trading.evaluator import evaluate_paper_trades

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(PEAK_FADE_MIN_PEAK_PCT=10.0)
    trade_id = await _seed_post_leg1_trade(db, "tok-pf2", settings)
    # peak = 8% (below threshold) — no fire even with full retrace
    await _set_checkpoints_and_peak(
        db, trade_id, peak_pct=8.0, cp_6h_pct=1.0, cp_24h_pct=0.5
    )
    await _seed_current_price(db, "tok-pf2", 1.01)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT status, peak_fade_fired_at FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    status, fired_at = await cur.fetchone()
    assert fired_at is None
    assert status == "open"
    await db.close()


async def test_peak_fade_no_fire_when_cp_6h_missing(tmp_path, settings_factory):
    from scout.db import Database
    from scout.trading.evaluator import evaluate_paper_trades

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory()
    trade_id = await _seed_post_leg1_trade(db, "tok-pf3", settings)
    # peak ok, cp_24h below ratio, cp_6h NULL → no fire (dual-observation required)
    await db._conn.execute(
        "UPDATE paper_trades SET peak_pct = ?, peak_price = ?, "
        "checkpoint_6h_pct = NULL, checkpoint_6h_price = NULL, "
        "checkpoint_24h_pct = ?, checkpoint_24h_price = ? "
        "WHERE id = ?",
        (20.0, 1.20, 5.0, 1.05, trade_id),
    )
    await db._conn.commit()
    await _seed_current_price(db, "tok-pf3", 1.05)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT peak_fade_fired_at FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    (fired_at,) = await cur.fetchone()
    assert fired_at is None
    await db.close()


async def test_peak_fade_no_fire_when_cp_24h_missing(tmp_path, settings_factory):
    from scout.db import Database
    from scout.trading.evaluator import evaluate_paper_trades

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory()
    trade_id = await _seed_post_leg1_trade(db, "tok-pf4", settings)
    await db._conn.execute(
        "UPDATE paper_trades SET peak_pct = ?, peak_price = ?, "
        "checkpoint_6h_pct = ?, checkpoint_6h_price = ?, "
        "checkpoint_24h_pct = NULL, checkpoint_24h_price = NULL "
        "WHERE id = ?",
        (20.0, 1.20, 5.0, 1.05, trade_id),
    )
    await db._conn.commit()
    await _seed_current_price(db, "tok-pf4", 1.05)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT peak_fade_fired_at FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    (fired_at,) = await cur.fetchone()
    assert fired_at is None
    await db.close()


async def test_peak_fade_no_fire_when_only_one_cp_below_ratio(
    tmp_path, settings_factory
):
    from scout.db import Database
    from scout.trading.evaluator import evaluate_paper_trades

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory()
    trade_id = await _seed_post_leg1_trade(db, "tok-pf5", settings)
    # peak = 20%, threshold = 0.7 * 20 = 14. cp_6h = 10 (below), cp_24h = 16 (above) → no fire
    await _set_checkpoints_and_peak(
        db, trade_id, peak_pct=20.0, cp_6h_pct=10.0, cp_24h_pct=16.0
    )
    await _seed_current_price(db, "tok-pf5", 1.16)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT peak_fade_fired_at FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    (fired_at,) = await cur.fetchone()
    assert fired_at is None
    await db.close()


async def test_peak_fade_disabled_flag_suppresses_fire(tmp_path, settings_factory):
    from scout.db import Database
    from scout.trading.evaluator import evaluate_paper_trades

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(PEAK_FADE_ENABLED=False)
    trade_id = await _seed_post_leg1_trade(db, "tok-pf6", settings)
    await _set_checkpoints_and_peak(
        db, trade_id, peak_pct=20.0, cp_6h_pct=5.0, cp_24h_pct=5.0
    )
    await _seed_current_price(db, "tok-pf6", 1.05)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT peak_fade_fired_at FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    (fired_at,) = await cur.fetchone()
    assert fired_at is None, "PEAK_FADE_ENABLED=False must suppress all fires"
    await db.close()


async def test_peak_fade_sl_wins_when_both_eligible(tmp_path, settings_factory):
    """SL triggers before peak-fade in the precedence chain."""
    from scout.db import Database
    from scout.trading.evaluator import evaluate_paper_trades

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory()
    # Fresh trade (no leg 1) so floor is not armed — SL eligibility path
    from scout.trading.paper import PaperTrader
    from datetime import datetime, timezone

    trader = PaperTrader()
    trade_id = await trader.execute_buy(
        db=db,
        token_id="tok-pf7",
        symbol="PF7",
        name="pf7",
        chain="coingecko",
        signal_type="first_signal",
        signal_data={},
        current_price=1.00,
        amount_usd=100.0,
        tp_pct=40.0,
        sl_pct=15.0,
        slippage_bps=0,
        signal_combo="first_signal+momentum_ratio",
    )
    twenty_five_h = datetime.now(timezone.utc).timestamp() - 25 * 3600
    backdate = datetime.fromtimestamp(twenty_five_h, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    await db._conn.execute(
        "UPDATE paper_trades SET opened_at = ?, created_at = ?, "
        "peak_pct = 20.0, peak_price = 1.20, "
        "checkpoint_6h_pct = 5.0, checkpoint_6h_price = 1.05, "
        "checkpoint_24h_pct = -20.0, checkpoint_24h_price = 0.80 "
        "WHERE id = ?",
        (backdate, backdate, trade_id),
    )
    # Backdate the BL-061 cutover so this backdated trade is classified as
    # post-cutover and actually exercises the BL-061 cascade SL branch
    # (not the legacy pre-cutover SL path).
    thirty_h = datetime.now(timezone.utc).timestamp() - 30 * 3600
    backdate_cutover = datetime.fromtimestamp(thirty_h, tz=timezone.utc).isoformat()
    await db._conn.execute(
        "UPDATE paper_migrations SET cutover_ts = ? WHERE name = 'bl061_ladder'",
        (backdate_cutover,),
    )
    await db._conn.commit()
    # Current price at -20% — trips SL before peak_fade check
    await _seed_current_price(db, "tok-pf7", 0.80)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT exit_reason, peak_fade_fired_at FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    reason, fired_at = await cur.fetchone()
    assert reason == "stop_loss"
    assert fired_at is None, "SL must win; peak_fade must not fire"
    await db.close()


async def test_peak_fade_trail_wins_when_both_eligible(tmp_path, settings_factory):
    """Trailing-stop triggers before peak-fade on the same evaluator pass."""
    from scout.db import Database
    from scout.trading.evaluator import evaluate_paper_trades

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(
        PAPER_LADDER_TRAIL_PCT=12.0,
    )
    trade_id = await _seed_post_leg1_trade(db, "tok-pf8", settings)
    # peak = 30%, trail threshold = 1.30 * 0.88 = 1.144
    # cp_6h = 5, cp_24h = 5 (both below 0.7*30 = 21) — peak_fade eligible
    await _set_checkpoints_and_peak(
        db, trade_id, peak_pct=30.0, cp_6h_pct=5.0, cp_24h_pct=5.0
    )
    # Current price 1.10 → below trail_threshold 1.144 → trail fires first
    await _seed_current_price(db, "tok-pf8", 1.10)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT exit_reason, peak_fade_fired_at FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    reason, fired_at = await cur.fetchone()
    assert reason == "trailing_stop", f"trail must win; got reason={reason}"
    assert fired_at is None
    await db.close()


async def test_peak_fade_fires_when_trail_not_tripped(tmp_path, settings_factory):
    """Trail armed but current price ABOVE trail threshold on this pass;
    peak-fade must still fire based on the 6h/24h observations."""
    from scout.db import Database
    from scout.trading.evaluator import evaluate_paper_trades

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(
        PAPER_LADDER_TRAIL_PCT=12.0,
    )
    trade_id = await _seed_post_leg1_trade(db, "tok-pf9", settings)
    # peak = 20%, trail threshold = 1.20 * 0.88 = 1.056
    # cp_6h = 8, cp_24h = 8 (both below 0.7*20 = 14) — peak_fade eligible
    await _set_checkpoints_and_peak(
        db, trade_id, peak_pct=20.0, cp_6h_pct=8.0, cp_24h_pct=8.0
    )
    # Current price 1.08 → above trail_threshold 1.056 → trail does NOT fire
    await _seed_current_price(db, "tok-pf9", 1.08)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT exit_reason, peak_fade_fired_at FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    reason, fired_at = await cur.fetchone()
    assert reason == "peak_fade"
    assert fired_at is not None
    await db.close()


async def test_peak_fade_closes_remaining_qty_only(tmp_path, settings_factory):
    from scout.db import Database
    from scout.trading.evaluator import evaluate_paper_trades

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory()
    trade_id = await _seed_post_leg1_trade(db, "tok-pf10", settings)
    # Confirm remaining_qty is 70% of original (100 USD * 0.70 = 70 USD)
    cur = await db._conn.execute(
        "SELECT remaining_qty FROM paper_trades WHERE id = ?", (trade_id,)
    )
    (rem_before,) = await cur.fetchone()
    assert rem_before is not None and rem_before > 0
    await _set_checkpoints_and_peak(
        db, trade_id, peak_pct=20.0, cp_6h_pct=8.0, cp_24h_pct=8.0
    )
    await _seed_current_price(db, "tok-pf10", 1.08)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    status, reason = await cur.fetchone()
    assert status == "closed_peak_fade"
    assert reason == "peak_fade"
    await db.close()


async def test_peak_fade_does_not_refire_once_closed(tmp_path, settings_factory):
    """Second evaluator pass on an already-closed trade must be a no-op."""
    from scout.db import Database
    from scout.trading.evaluator import evaluate_paper_trades

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory()
    trade_id = await _seed_post_leg1_trade(db, "tok-pf11", settings)
    await _set_checkpoints_and_peak(
        db, trade_id, peak_pct=20.0, cp_6h_pct=8.0, cp_24h_pct=8.0
    )
    await _seed_current_price(db, "tok-pf11", 1.08)

    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT peak_fade_fired_at FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    (first_fire,) = await cur.fetchone()
    assert first_fire is not None

    # Run again — trade is closed (status != 'open'), SELECT in evaluator
    # filters to status='open', so no second fire attempt.
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT peak_fade_fired_at FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    (second_fire,) = await cur.fetchone()
    assert second_fire == first_fire, "peak_fade_fired_at must not be rewritten"
    await db.close()


async def test_peak_fade_no_fire_when_remaining_qty_is_zero(tmp_path, settings_factory):
    """Belt-and-suspenders: a legacy inconsistent row with status='open'
    but remaining_qty=0 must not trigger a zero-qty peak-fade close."""
    from scout.db import Database
    from scout.trading.evaluator import evaluate_paper_trades

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory()
    trade_id = await _seed_post_leg1_trade(db, "tok-pf12", settings)
    await _set_checkpoints_and_peak(
        db, trade_id, peak_pct=20.0, cp_6h_pct=8.0, cp_24h_pct=8.0
    )
    await _seed_current_price(db, "tok-pf12", 1.08)
    # Force the degenerate state the guard defends against
    await db._conn.execute(
        "UPDATE paper_trades SET remaining_qty = 0 WHERE id = ?", (trade_id,)
    )
    await db._conn.commit()

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT status, peak_fade_fired_at FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    status, fired_at = await cur.fetchone()
    assert fired_at is None, "remaining_qty=0 must block peak-fade fire"
    assert status == "open"
    await db.close()
