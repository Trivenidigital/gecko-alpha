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
        await _insert_closed_trade(db, signal_type="narrative_prediction", pnl_usd=-100)
    await db._conn.commit()

    s = settings_factory(
        SIGNAL_PARAMS_ENABLED=True, SIGNAL_SUSPEND_HARD_LOSS_USD=-500.0
    )
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


# BL-NEW-AUTOSUSPEND-FIX: combined-gate hard_loss rule + drawdown_baseline_at


async def test_hard_loss_does_not_kill_profitable_signal_with_deep_drawdown(
    tmp_path, settings_factory
):
    """losers_contrarian-style case: signal peaked at +$1500, gave back $880
    to net +$620. Drawdown -$880 (below -$500) but net positive — must NOT fire.

    Old rule fired on drawdown alone — false positive on profitable volatility.
    New combined gate: net > 0 → both disjuncts evaluate False → no fire.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # 15 wins +$100 each (peak +$1500), then 8 losses -$110 each
    # (running -$880 from peak). Final net = $1500 - $880 = +$620.
    for _ in range(15):
        await _insert_closed_trade(db, signal_type="gainers_early", pnl_usd=100)
    for _ in range(8):
        await _insert_closed_trade(db, signal_type="gainers_early", pnl_usd=-110)
    await db._conn.commit()

    s = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        SIGNAL_SUSPEND_HARD_LOSS_USD=-500.0,
        SIGNAL_SUSPEND_PNL_THRESHOLD_USD=-200.0,
        SIGNAL_SUSPEND_MIN_TRADES=50,  # threshold path blocked by floor
    )
    suspended = await maybe_suspend_signals(db, s, session=None)
    assert (
        suspended == []
    ), f"Profitable signal must not be killed for volatility; got {suspended}"
    cur = await db._conn.execute(
        "SELECT enabled FROM signal_params WHERE signal_type='gainers_early'"
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()


async def test_hard_loss_kills_pure_loser_no_min_trades_floor(
    tmp_path, settings_factory
):
    """Catastrophic-bleed escape hatch preserved: 10 losses of -$60 each
    (net -$600) with MIN_TRADES=50. Net <= hard_loss → fires regardless
    of trade-count floor."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    for _ in range(10):
        await _insert_closed_trade(db, signal_type="gainers_early", pnl_usd=-60)
    await db._conn.commit()

    s = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        SIGNAL_SUSPEND_HARD_LOSS_USD=-500.0,
        SIGNAL_SUSPEND_MIN_TRADES=50,
    )
    suspended = await maybe_suspend_signals(db, s, session=None)
    assert any(
        x["signal_type"] == "gainers_early" and x["reason"] == "hard_loss"
        for x in suspended
    )
    await db.close()


async def test_hard_loss_kills_pump_then_crash(tmp_path, settings_factory):
    """Pump-then-dump path: drew to +$300, crashed to -$300.
    Drawdown -$600 (deep), net -$300 (below zero). Combined gate fires
    on second disjunct (drawdown <= hard_loss AND net <= 0)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # 3 wins +$100 (peak +$300), then 6 losses -$100 (final net -$300, dd -$600)
    for _ in range(3):
        await _insert_closed_trade(db, signal_type="gainers_early", pnl_usd=100)
    for _ in range(6):
        await _insert_closed_trade(db, signal_type="gainers_early", pnl_usd=-100)
    await db._conn.commit()

    s = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        SIGNAL_SUSPEND_HARD_LOSS_USD=-500.0,
        SIGNAL_SUSPEND_MIN_TRADES=50,
    )
    suspended = await maybe_suspend_signals(db, s, session=None)
    assert any(
        x["signal_type"] == "gainers_early" and x["reason"] == "hard_loss"
        for x in suspended
    )
    await db.close()


async def test_hard_loss_audit_detail_records_both_metrics(tmp_path, settings_factory):
    """Audit reason string surfaces BOTH net_pnl and max_drawdown so operators
    can debug false-positive concerns. Old format only showed drawdown."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    for _ in range(10):
        await _insert_closed_trade(db, signal_type="gainers_early", pnl_usd=-60)
    await db._conn.commit()

    s = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        SIGNAL_SUSPEND_HARD_LOSS_USD=-500.0,
    )
    await maybe_suspend_signals(db, s, session=None)
    cur = await db._conn.execute(
        "SELECT reason FROM signal_params_audit "
        "WHERE signal_type='gainers_early' AND applied_by='auto_suspend'"
    )
    row = await cur.fetchone()
    assert row is not None
    reason = row[0].lower()
    assert "hard_loss" in reason
    assert "net" in reason  # net_pnl in detail
    assert "drawdown" in reason  # max_drawdown in detail
    await db.close()


async def test_signal_params_has_drawdown_baseline_at_column(tmp_path):
    """Schema migration adds drawdown_baseline_at TEXT column on signal_params."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(signal_params)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "drawdown_baseline_at" in cols
    await db.close()


async def test_drawdown_baseline_at_defaults_null_on_seed(tmp_path):
    """Existing rows after migration have baseline=NULL (no behavior change
    for never-suspended signals)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT signal_type, drawdown_baseline_at FROM signal_params"
    )
    rows = await cur.fetchall()
    assert len(rows) > 0
    for sig, baseline in rows:
        assert baseline is None, f"{sig} should default to NULL; got {baseline!r}"
    await db.close()


async def test_revive_signal_with_baseline_stamps_baseline_and_audit(
    tmp_path,
):
    """Operator revival: enabled 0→1, drawdown_baseline_at = NOW(),
    audit row written, suspended_at/reason cleared."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0, suspended_at=?, "
        "suspended_reason='auto_suspend' WHERE signal_type='gainers_early'",
        ("2026-05-04T01:01:02Z",),
    )
    await db._conn.commit()

    await db.revive_signal_with_baseline(
        "gainers_early",
        reason="operator: post-fix revival",
    )

    cur = await db._conn.execute(
        "SELECT enabled, drawdown_baseline_at, suspended_at, suspended_reason "
        "FROM signal_params WHERE signal_type='gainers_early'"
    )
    enabled, baseline, susp_at, susp_reason = await cur.fetchone()
    assert enabled == 1
    assert baseline is not None
    assert susp_at is None
    assert susp_reason is None
    parsed = datetime.fromisoformat(baseline)
    assert (datetime.now(timezone.utc) - parsed).total_seconds() < 5

    cur = await db._conn.execute(
        "SELECT field_name, old_value, new_value, applied_by, reason "
        "FROM signal_params_audit WHERE signal_type='gainers_early' "
        "ORDER BY applied_at DESC LIMIT 1"
    )
    field, old, new, by, reason = await cur.fetchone()
    assert field == "enabled"
    assert old == "0"
    assert new == "1"
    assert by == "operator"
    assert "post-fix revival" in reason
    await db.close()


async def test_revive_signal_unknown_signal_raises(tmp_path):
    """Unknown signal_type raises ValueError, no DB mutation."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    with pytest.raises(ValueError, match="unknown signal_type"):
        await db.revive_signal_with_baseline("does_not_exist", reason="test")
    await db.close()


async def test_baseline_overrides_30d_window_floor(tmp_path, settings_factory):
    """When drawdown_baseline_at is more recent than the 30d default, the
    window starts at the baseline. Pre-baseline drawdown is excluded so a
    revived signal isn't immediately re-killed by historical losses."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # 10 -$100 closes 25d ago (lifetime drawdown -$1000 in pre-baseline window)
    old_close = (datetime.now(timezone.utc) - timedelta(days=25)).isoformat()
    old_open = (datetime.now(timezone.utc) - timedelta(days=26)).isoformat()
    for i in range(10):
        await db._conn.execute(
            """INSERT INTO paper_trades
               (token_id, symbol, name, chain, signal_type, signal_data,
                entry_price, amount_usd, quantity, tp_pct, sl_pct,
                tp_price, sl_price, status, exit_price, pnl_usd, pnl_pct,
                peak_pct, opened_at, closed_at)
               VALUES (?, 'TOK', 'T', 'coingecko', 'gainers_early', '{}',
                       1.0, 100.0, 100.0, 20.0, 15.0, 1.2, 0.85,
                       'closed_sl', 0.0, -100.0, -33.0, 5.0, ?, ?)""",
            (f"old-{i}", old_open, old_close),
        )
    # Stamp baseline at NOW (excludes the 25d-old losses from window)
    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "UPDATE signal_params SET drawdown_baseline_at = ? "
        "WHERE signal_type='gainers_early'",
        (now_iso,),
    )
    await db._conn.commit()

    s = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        SIGNAL_SUSPEND_HARD_LOSS_USD=-500.0,
    )
    suspended = await maybe_suspend_signals(db, s, session=None)
    # Window is post-baseline (no rows) → no fire
    assert (
        suspended == []
    ), f"Baseline must exclude pre-revival drawdown; got {suspended}"
    await db.close()
