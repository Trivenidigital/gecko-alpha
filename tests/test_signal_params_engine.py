"""Engine integration: per-signal sl_pct stamping + enabled kill switch."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scout.db import Database
from scout.trading.engine import TradingEngine
from scout.trading.params import clear_cache_for_tests


@pytest.fixture(autouse=True)
def _wipe_cache():
    clear_cache_for_tests()
    yield
    clear_cache_for_tests()


async def _seed_price(db, coin_id="tok", price=1.0):
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        (coin_id, price, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()


async def test_engine_stamps_per_signal_sl_pct_when_flag_on(
    tmp_path, settings_factory
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_price(db)

    # Mutate signal_params row so table sl_pct differs from Settings
    await db._conn.execute(
        "UPDATE signal_params SET sl_pct = 22.5 WHERE signal_type = 'gainers_early'"
    )
    await db._conn.commit()

    settings = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        PAPER_SL_PCT=15.0,  # global differs — table value should win
        PAPER_STARTUP_WARMUP_SECONDS=0,
        PAPER_TRADE_AMOUNT_USD=100.0,
        PAPER_MAX_EXPOSURE_USD=10000.0,
        PAPER_TP_PCT=20.0,
    )
    engine = TradingEngine(mode="paper", db=db, settings=settings)
    trade_id = await engine.open_trade(
        token_id="tok",
        symbol="TOK",
        chain="coingecko",
        signal_type="gainers_early",
        signal_data={},
        signal_combo="gainers_early",
    )
    assert trade_id is not None

    cur = await db._conn.execute(
        "SELECT sl_pct FROM paper_trades WHERE id = ?", (trade_id,)
    )
    row = await cur.fetchone()
    assert row[0] == pytest.approx(22.5)
    await db.close()


async def test_engine_uses_settings_sl_pct_when_flag_off(
    tmp_path, settings_factory
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_price(db)

    # Even though the table differs, flag-off path must read Settings.
    await db._conn.execute(
        "UPDATE signal_params SET sl_pct = 99.0 WHERE signal_type = 'gainers_early'"
    )
    await db._conn.commit()

    settings = settings_factory(
        SIGNAL_PARAMS_ENABLED=False,
        PAPER_SL_PCT=15.0,
        PAPER_STARTUP_WARMUP_SECONDS=0,
        PAPER_TRADE_AMOUNT_USD=100.0,
        PAPER_MAX_EXPOSURE_USD=10000.0,
        PAPER_TP_PCT=20.0,
    )
    engine = TradingEngine(mode="paper", db=db, settings=settings)
    trade_id = await engine.open_trade(
        token_id="tok",
        symbol="TOK",
        chain="coingecko",
        signal_type="gainers_early",
        signal_data={},
        signal_combo="gainers_early",
    )
    cur = await db._conn.execute(
        "SELECT sl_pct FROM paper_trades WHERE id = ?", (trade_id,)
    )
    assert (await cur.fetchone())[0] == pytest.approx(15.0)
    await db.close()


async def test_engine_blocks_when_signal_disabled(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_price(db)

    await db._conn.execute(
        "UPDATE signal_params SET enabled = 0 WHERE signal_type = 'gainers_early'"
    )
    await db._conn.commit()

    settings = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        PAPER_STARTUP_WARMUP_SECONDS=0,
        PAPER_TRADE_AMOUNT_USD=100.0,
        PAPER_MAX_EXPOSURE_USD=10000.0,
        PAPER_TP_PCT=20.0,
        PAPER_SL_PCT=15.0,
    )
    engine = TradingEngine(mode="paper", db=db, settings=settings)
    trade_id = await engine.open_trade(
        token_id="tok",
        symbol="TOK",
        chain="coingecko",
        signal_type="gainers_early",
        signal_data={},
        signal_combo="gainers_early",
    )
    assert trade_id is None
    cur = await db._conn.execute("SELECT COUNT(*) FROM paper_trades")
    assert (await cur.fetchone())[0] == 0
    await db.close()


async def test_engine_rejects_unknown_signal_type(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_price(db)

    settings = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        PAPER_STARTUP_WARMUP_SECONDS=0,
        PAPER_TRADE_AMOUNT_USD=100.0,
        PAPER_MAX_EXPOSURE_USD=10000.0,
        PAPER_TP_PCT=20.0,
        PAPER_SL_PCT=15.0,
    )
    engine = TradingEngine(mode="paper", db=db, settings=settings)
    trade_id = await engine.open_trade(
        token_id="tok",
        symbol="TOK",
        chain="coingecko",
        signal_type="totally_made_up",
        signal_data={},
        signal_combo="totally_made_up",
    )
    assert trade_id is None
    cur = await db._conn.execute("SELECT COUNT(*) FROM paper_trades")
    assert (await cur.fetchone())[0] == 0
    await db.close()
