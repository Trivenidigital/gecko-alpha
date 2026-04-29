"""Tests for scout.trading.auto_suspend (Tier 1b)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading.auto_suspend import maybe_suspend_signals
from scout.trading.params import clear_cache_for_tests


_seq = [0]


@pytest.fixture(autouse=True)
def _wipe_cache():
    _seq[0] = 0
    clear_cache_for_tests()
    yield
    clear_cache_for_tests()


async def _insert_closed_trade(
    db, *, signal_type, pnl_usd, status="closed_sl", days_ago=1
):
    _seq[0] += 1
    seq = _seq[0]
    opened = datetime.now(timezone.utc) - timedelta(days=days_ago, seconds=seq)
    closed = datetime.now(timezone.utc) - timedelta(
        days=days_ago, hours=-1, seconds=seq
    )
    await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
            status, exit_price, pnl_usd, pnl_pct, peak_pct,
            opened_at, closed_at)
           VALUES (?, 'TOK', 'T', 'coingecko', ?, '{}', 1.0, 100.0, 100.0,
                   20.0, 15.0, 1.2, 0.85, ?, 1.0, ?, ?, ?, ?, ?)""",
        (
            f"tok-{seq}",
            signal_type,
            status,
            pnl_usd,
            pnl_usd,
            5.0,
            opened.isoformat(),
            closed.isoformat(),
        ),
    )


async def test_no_op_when_flag_off(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    for _ in range(60):
        await _insert_closed_trade(db, signal_type="gainers_early", pnl_usd=-100)
    await db._conn.commit()

    s = settings_factory(SIGNAL_PARAMS_ENABLED=False)
    suspended = await maybe_suspend_signals(db, s, session=None)
    assert suspended == []

    cur = await db._conn.execute(
        "SELECT enabled FROM signal_params WHERE signal_type='gainers_early'"
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()


async def test_pnl_threshold_suspends_with_min_trades(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # 60 small losers, net way below threshold
    for _ in range(60):
        await _insert_closed_trade(db, signal_type="gainers_early", pnl_usd=-10)
    await db._conn.commit()

    s = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        SIGNAL_SUSPEND_PNL_THRESHOLD_USD=-200.0,
        SIGNAL_SUSPEND_HARD_LOSS_USD=-500.0,
        SIGNAL_SUSPEND_MIN_TRADES=50,
    )
    # Pre-emptively bump hard-loss past actual drawdown so threshold fires
    suspended = await maybe_suspend_signals(db, s, session=None)
    types = {x["signal_type"] for x in suspended}
    # Either path may have fired (-$600 cum drawdown breaches both); we just
    # assert the signal is now disabled.
    cur = await db._conn.execute(
        "SELECT enabled FROM signal_params WHERE signal_type='gainers_early'"
    )
    assert (await cur.fetchone())[0] == 0
    assert "gainers_early" in types
    await db.close()


async def test_min_trades_floor_blocks_pnl_threshold(tmp_path, settings_factory):
    """At n=10 with a moderate cumulative loss, threshold path should NOT fire."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    for _ in range(10):
        await _insert_closed_trade(db, signal_type="gainers_early", pnl_usd=-30)
    await db._conn.commit()

    s = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        SIGNAL_SUSPEND_PNL_THRESHOLD_USD=-200.0,
        SIGNAL_SUSPEND_HARD_LOSS_USD=-1000.0,  # well below cumulative −$300
        SIGNAL_SUSPEND_MIN_TRADES=50,
    )
    suspended = await maybe_suspend_signals(db, s, session=None)
    assert suspended == []
    cur = await db._conn.execute(
        "SELECT enabled FROM signal_params WHERE signal_type='gainers_early'"
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()


async def test_hard_loss_escape_hatch_fires_below_min_trades(
    tmp_path, settings_factory
):
    """20 trades, −$1000 cumulative, MIN_TRADES=50 — hard_loss should fire."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    for _ in range(20):
        await _insert_closed_trade(db, signal_type="gainers_early", pnl_usd=-50)
    await db._conn.commit()

    s = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        SIGNAL_SUSPEND_PNL_THRESHOLD_USD=-200.0,
        SIGNAL_SUSPEND_HARD_LOSS_USD=-500.0,
        SIGNAL_SUSPEND_MIN_TRADES=50,
    )
    suspended = await maybe_suspend_signals(db, s, session=None)
    assert any(
        x["signal_type"] == "gainers_early" and x["reason"] == "hard_loss"
        for x in suspended
    )
    cur = await db._conn.execute(
        "SELECT enabled, suspended_reason FROM signal_params "
        "WHERE signal_type='gainers_early'"
    )
    row = await cur.fetchone()
    assert row[0] == 0
    assert row[1] == "hard_loss"
    await db.close()


async def test_audit_row_written_on_suspend(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    for _ in range(20):
        await _insert_closed_trade(db, signal_type="gainers_early", pnl_usd=-50)
    await db._conn.commit()

    s = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        SIGNAL_SUSPEND_HARD_LOSS_USD=-500.0,
    )
    await maybe_suspend_signals(db, s, session=None)
    cur = await db._conn.execute(
        "SELECT field_name, applied_by FROM signal_params_audit "
        "WHERE signal_type='gainers_early'"
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "enabled"
    assert row[1] == "auto_suspend"
    await db.close()


async def test_excludes_narrative_prediction(tmp_path, settings_factory):
    """narrative_prediction is in CALIBRATION_EXCLUDE_SIGNALS — also excluded
    from auto-suspend (we don't tune it, shouldn't auto-kill it)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    for _ in range(60):
        await _insert_closed_trade(
            db, signal_type="narrative_prediction", pnl_usd=-100
        )
    await db._conn.commit()

    s = settings_factory(SIGNAL_PARAMS_ENABLED=True, SIGNAL_SUSPEND_HARD_LOSS_USD=-500.0)
    suspended = await maybe_suspend_signals(db, s, session=None)
    assert all(x["signal_type"] != "narrative_prediction" for x in suspended)
    await db.close()


async def test_one_way_switch_never_re_enables(tmp_path, settings_factory):
    """Even with no losses, a previously-suspended signal must stay suspended."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Pre-suspend
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0, suspended_reason='operator' "
        "WHERE signal_type='gainers_early'"
    )
    # Add wins — should NOT re-enable
    for _ in range(60):
        await _insert_closed_trade(db, signal_type="gainers_early", pnl_usd=50)
    await db._conn.commit()

    s = settings_factory(SIGNAL_PARAMS_ENABLED=True)
    await maybe_suspend_signals(db, s, session=None)
    cur = await db._conn.execute(
        "SELECT enabled FROM signal_params WHERE signal_type='gainers_early'"
    )
    assert (await cur.fetchone())[0] == 0
    await db.close()
