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


async def test_suspend_clears_tg_alert_eligible_jointly(tmp_path, settings_factory):
    """V3-I2 PR-stage fold: auto-suspend clears BOTH enabled AND
    tg_alert_eligible (R2-I1 joint flag maintenance)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT enabled, tg_alert_eligible FROM signal_params "
        "WHERE signal_type='gainers_early'"
    )
    pre = await cur.fetchone()
    assert pre[0] == 1 and pre[1] == 1
    for _ in range(30):
        await _insert_closed_trade(db, signal_type="gainers_early", pnl_usd=-100)
    await db._conn.commit()
    s = settings_factory(SIGNAL_PARAMS_ENABLED=True)
    suspended = await maybe_suspend_signals(db, s, session=None)
    assert any(r["signal_type"] == "gainers_early" for r in suspended)
    cur = await db._conn.execute(
        "SELECT enabled, tg_alert_eligible FROM signal_params "
        "WHERE signal_type='gainers_early'"
    )
    row = await cur.fetchone()
    assert row[0] == 0 and row[1] == 0
    cur = await db._conn.execute(
        "SELECT old_value, new_value FROM signal_params_audit "
        "WHERE signal_type='gainers_early' AND field_name='tg_alert_eligible' "
        "ORDER BY id DESC LIMIT 1"
    )
    audit = await cur.fetchone()
    assert audit is not None and audit[0] == "1" and audit[1] == "0"
    await db.close()


async def test_suspend_no_falsified_audit_for_already_zero(tmp_path, settings_factory):
    """V1-I1 PR-stage fold: signals starting at tg_alert_eligible=0
    (e.g., trending_catch) don't get a falsified '1->0' audit row."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT tg_alert_eligible FROM signal_params "
        "WHERE signal_type='trending_catch'"
    )
    assert (await cur.fetchone())[0] == 0
    for _ in range(30):
        await _insert_closed_trade(db, signal_type="trending_catch", pnl_usd=-100)
    await db._conn.commit()
    s = settings_factory(SIGNAL_PARAMS_ENABLED=True)
    await maybe_suspend_signals(db, s, session=None)
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM signal_params_audit "
        "WHERE signal_type='trending_catch' AND field_name='tg_alert_eligible'"
    )
    assert (await cur.fetchone())[0] == 0
    await db.close()


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
    Drawdown -$600 (deep), net -$300 (below pnl_threshold -$200). Combined
    gate fires on second disjunct (drawdown <= hard_loss AND net < pnl_threshold)."""
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
        "AND field_name='enabled' ORDER BY applied_at DESC LIMIT 1"
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


async def test_hard_loss_does_not_kill_borderline_negative_with_deep_drawdown(
    tmp_path, settings_factory
):
    """Reviewer MUST-FIX (statistical): sparse-data signal at (n=2, net=-$10,
    dd=-$510) must NOT hard-kill via the no-MIN_TRADES-floor path.

    The tightened second disjunct (``net_pnl < pnl_threshold`` instead of
    ``<= 0``) closes this gap. With pnl_threshold=-$200 and net=-$10, the
    second disjunct evaluates False (-$10 is not < -$200), so the rule
    correctly defers to the pnl_threshold path which has a MIN_TRADES floor.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Build a 2-trade sparse-data sequence: +$250 win then -$260 loss.
    # Running: 0 → +250 → -10. Peak = +250, trough = -10. Drawdown = -260.
    # Bump it deeper: use peak +$500 then crash to -$10 → dd=-$510.
    await _insert_closed_trade(db, signal_type="gainers_early", pnl_usd=500)
    await _insert_closed_trade(db, signal_type="gainers_early", pnl_usd=-510)
    await db._conn.commit()

    s = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        SIGNAL_SUSPEND_HARD_LOSS_USD=-500.0,
        SIGNAL_SUSPEND_PNL_THRESHOLD_USD=-200.0,
        SIGNAL_SUSPEND_MIN_TRADES=50,  # threshold path floor blocks pnl_threshold
    )
    suspended = await maybe_suspend_signals(db, s, session=None)
    # n=2 < MIN_TRADES → pnl_threshold also defers. Net result: no fire.
    assert (
        suspended == []
    ), f"Sparse borderline-negative must not hard-kill; got {suspended}"
    await db.close()


def test_pnl_threshold_branch_has_alerter_import_statically():
    """Reviewer MUST-FIX (code, conf 100): the pnl_threshold Telegram branch
    in scout/trading/auto_suspend.py must include a local
    ``from scout import alerter`` import. Without it, calling
    maybe_suspend_signals(session=...) when only pnl_threshold fires raises
    NameError because the hard_loss branch's local import never executed.

    We verify this structurally by reading the source rather than running
    it — the runtime test would require importing scout.alerter, which
    triggers Windows OpenSSL Applink (the very reason both branches use
    deferred local imports in the first place; see auto_suspend.py:38-40
    + 142-148 for the rationale).
    """
    import inspect

    from scout.trading import auto_suspend

    src = inspect.getsource(auto_suspend.maybe_suspend_signals)
    # Split source into two halves around the pnl_threshold branch sentinel.
    sentinel = "# Threshold-based suspension"
    assert sentinel in src, f"sentinel not found; refactor needed: {sentinel!r}"
    pnl_branch_src = src[src.index(sentinel) :]
    # The pnl_threshold branch must contain its own deferred alerter import
    # (the hard_loss branch's import doesn't carry into this scope).
    assert "from scout import alerter" in pnl_branch_src, (
        "pnl_threshold branch missing local alerter import — would NameError "
        "when only this branch fires with session != None. Add "
        "`from scout import alerter` inside the pnl_threshold "
        "`if session is not None:` block."
    )


async def test_revive_signal_with_baseline_on_already_enabled_signal(
    tmp_path,
):
    """Reviewer coverage gap (c): reviving an already-enabled signal still
    stamps a fresh baseline and writes an audit row showing 1→1. Useful
    for operator who wants to reset the rolling-drawdown window without
    a prior suspension."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # gainers_early starts enabled=1 (from seed). No prior suspend.
    cur = await db._conn.execute(
        "SELECT enabled, drawdown_baseline_at FROM signal_params "
        "WHERE signal_type='gainers_early'"
    )
    enabled_before, baseline_before = await cur.fetchone()
    assert enabled_before == 1
    assert baseline_before is None

    await db.revive_signal_with_baseline(
        "gainers_early", reason="operator: reset drawdown window"
    )

    cur = await db._conn.execute(
        "SELECT enabled, drawdown_baseline_at FROM signal_params "
        "WHERE signal_type='gainers_early'"
    )
    enabled_after, baseline_after = await cur.fetchone()
    assert enabled_after == 1
    assert baseline_after is not None  # baseline stamped

    cur = await db._conn.execute(
        "SELECT old_value, new_value FROM signal_params_audit "
        "WHERE signal_type='gainers_early' AND field_name='enabled' "
        "ORDER BY applied_at DESC LIMIT 1"
    )
    old, new = await cur.fetchone()
    assert old == "1"  # was already enabled
    assert new == "1"
    await db.close()


async def test_baseline_picks_max_when_both_calibration_and_baseline_set(
    tmp_path, settings_factory
):
    """Reviewer coverage gap (b): when both last_calibration_at and
    drawdown_baseline_at are populated, the rolling window must start at
    MAX(both, 30d_default). Verify by setting last_cal far in past and
    baseline recently — losses before baseline must be excluded."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # 5 deep losses 5d ago (within 30d window AND past last_cal=15d ago,
    # but BEFORE the recent baseline).
    five_days_ago_close = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    five_days_ago_open = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()
    for i in range(5):
        await db._conn.execute(
            """INSERT INTO paper_trades
               (token_id, symbol, name, chain, signal_type, signal_data,
                entry_price, amount_usd, quantity, tp_pct, sl_pct,
                tp_price, sl_price, status, exit_price, pnl_usd, pnl_pct,
                peak_pct, opened_at, closed_at)
               VALUES (?, 'TOK', 'T', 'coingecko', 'gainers_early', '{}',
                       1.0, 100.0, 100.0, 20.0, 15.0, 1.2, 0.85,
                       'closed_sl', 0.0, -200.0, -67.0, 5.0, ?, ?)""",
            (f"loss-{i}", five_days_ago_open, five_days_ago_close),
        )
    # last_cal 15d ago, baseline 1d ago
    last_cal = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
    baseline = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    await db._conn.execute(
        "UPDATE signal_params SET last_calibration_at=?, drawdown_baseline_at=? "
        "WHERE signal_type='gainers_early'",
        (last_cal, baseline),
    )
    await db._conn.commit()

    s = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        SIGNAL_SUSPEND_HARD_LOSS_USD=-500.0,
    )
    suspended = await maybe_suspend_signals(db, s, session=None)
    # 5d-old losses are between last_cal (15d) and baseline (1d).
    # Window MAX(15d, 1d, 30d) = baseline (1d ago). Losses excluded.
    assert (
        suspended == []
    ), f"baseline (1d ago) > last_cal (15d ago) should be window floor; got {suspended}"
    await db.close()


async def test_empty_window_does_not_trigger_either_gate(tmp_path, settings_factory):
    """Reviewer coverage gap (d): when n=0 (no closed trades in window),
    _rolling_stats returns (0, 0.0, 0.0) and neither gate fires."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # No trades inserted at all.
    s = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        SIGNAL_SUSPEND_HARD_LOSS_USD=-500.0,
        SIGNAL_SUSPEND_PNL_THRESHOLD_USD=-200.0,
        SIGNAL_SUSPEND_MIN_TRADES=50,
    )
    suspended = await maybe_suspend_signals(db, s, session=None)
    assert suspended == []
    cur = await db._conn.execute("SELECT COUNT(*) FROM signal_params WHERE enabled = 1")
    assert (await cur.fetchone())[0] > 0  # all signals still enabled
    await db.close()


# BL-NEW-REVIVAL-COOLOFF: cool-off between consecutive operator revivals


async def test_revive_first_time_never_blocks(tmp_path, settings_factory):
    """Signal with NO prior revival audit row — must succeed regardless of cool-off."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0, suspended_reason='auto_suspend' "
        "WHERE signal_type='gainers_early'"
    )
    await db._conn.commit()
    s = settings_factory(SIGNAL_REVIVAL_MIN_SOAK_DAYS=7)
    # First revival — no cool-off applies
    await db.revive_signal_with_baseline(
        "gainers_early", reason="first revival", settings=s
    )
    cur = await db._conn.execute(
        "SELECT enabled FROM signal_params WHERE signal_type='gainers_early'"
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()


async def test_revive_within_cooloff_window_raises(tmp_path, settings_factory):
    """Second revival within SIGNAL_REVIVAL_MIN_SOAK_DAYS must raise ValueError."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(SIGNAL_REVIVAL_MIN_SOAK_DAYS=7)
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0 WHERE signal_type='gainers_early'"
    )
    await db._conn.commit()
    await db.revive_signal_with_baseline("gainers_early", reason="first", settings=s)
    # Re-suspend (simulate auto_suspend re-firing)
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0, suspended_reason='auto_suspend' "
        "WHERE signal_type='gainers_early'"
    )
    await db._conn.commit()
    with pytest.raises(ValueError, match="cool-off"):
        await db.revive_signal_with_baseline(
            "gainers_early", reason="second within window", settings=s
        )
    await db.close()


async def test_revive_after_cooloff_window_allows(tmp_path, settings_factory):
    """Backdate the prior revival audit row > 7 days; second revival succeeds."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(SIGNAL_REVIVAL_MIN_SOAK_DAYS=7)
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0 WHERE signal_type='gainers_early'"
    )
    await db._conn.commit()
    await db.revive_signal_with_baseline("gainers_early", reason="first", settings=s)
    # Backdate the audit row 8 days ago
    eight_days_ago = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    await db._conn.execute(
        "UPDATE signal_params_audit SET applied_at=? "
        "WHERE signal_type='gainers_early' AND applied_by='operator'",
        (eight_days_ago,),
    )
    # Re-suspend
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0, suspended_reason='auto_suspend' "
        "WHERE signal_type='gainers_early'"
    )
    await db._conn.commit()
    # Should succeed — past cool-off
    await db.revive_signal_with_baseline(
        "gainers_early", reason="second after window", settings=s
    )
    cur = await db._conn.execute(
        "SELECT enabled FROM signal_params WHERE signal_type='gainers_early'"
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()


async def test_revive_force_true_bypasses_cooloff(tmp_path, settings_factory):
    """force=True must bypass the cool-off check."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(SIGNAL_REVIVAL_MIN_SOAK_DAYS=7)
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0 WHERE signal_type='gainers_early'"
    )
    await db._conn.commit()
    await db.revive_signal_with_baseline("gainers_early", reason="first", settings=s)
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0 WHERE signal_type='gainers_early'"
    )
    await db._conn.commit()
    # Should succeed even though within window
    await db.revive_signal_with_baseline(
        "gainers_early", reason="emergency override", force=True, settings=s
    )
    cur = await db._conn.execute(
        "SELECT enabled FROM signal_params WHERE signal_type='gainers_early'"
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()


async def test_revive_force_true_audit_marks_bypass(tmp_path, settings_factory):
    """The bypass revival's audit row must contain a marker so operators
    reading history can see the cool-off was overridden."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(SIGNAL_REVIVAL_MIN_SOAK_DAYS=7)
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0 WHERE signal_type='gainers_early'"
    )
    await db._conn.commit()
    await db.revive_signal_with_baseline("gainers_early", reason="first", settings=s)
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0 WHERE signal_type='gainers_early'"
    )
    await db._conn.commit()
    await db.revive_signal_with_baseline(
        "gainers_early", reason="emergency override", force=True, settings=s
    )
    cur = await db._conn.execute(
        "SELECT reason FROM signal_params_audit WHERE signal_type='gainers_early' "
        "AND applied_by='operator' ORDER BY applied_at DESC LIMIT 1"
    )
    reason = (await cur.fetchone())[0]
    assert (
        "force" in reason.lower() or "bypass" in reason.lower()
    ), f"force=True audit must mark bypass; got: {reason}"
    await db.close()


async def test_revive_cooloff_independent_per_signal(tmp_path, settings_factory):
    """Reviving signal A within window does NOT block reviving signal B."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(SIGNAL_REVIVAL_MIN_SOAK_DAYS=7)
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0 "
        "WHERE signal_type IN ('gainers_early', 'losers_contrarian')"
    )
    await db._conn.commit()
    await db.revive_signal_with_baseline("gainers_early", reason="first GE", settings=s)
    # Should succeed despite gainers_early being within cool-off
    await db.revive_signal_with_baseline(
        "losers_contrarian", reason="first LC", settings=s
    )
    cur = await db._conn.execute(
        "SELECT signal_type, enabled FROM signal_params "
        "WHERE signal_type IN ('gainers_early', 'losers_contrarian') ORDER BY signal_type"
    )
    rows = await cur.fetchall()
    assert all(r[1] == 1 for r in rows)
    await db.close()


async def test_revive_force_true_no_warning_on_first_revival(
    tmp_path, settings_factory
):
    """Per design-stage operator-experience reviewer RECOMMEND + PR-stage
    code reviewer CRITICAL fix: force=True on a signal with NO prior
    revival audit row must NOT emit the revive_signal_force_bypass
    WARNING (no actual bypass occurred). It should emit
    revive_signal_force_no_prior at INFO instead.

    Uses structlog.testing.capture_logs() rather than pytest's caplog
    because the project uses structlog.PrintLoggerFactory() — caplog
    only captures stdlib logging. See test_trading_analytics.py:255
    for the canonical pattern."""
    from structlog.testing import capture_logs

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(SIGNAL_REVIVAL_MIN_SOAK_DAYS=7)
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0 WHERE signal_type='gainers_early'"
    )
    await db._conn.commit()
    with capture_logs() as captured:
        await db.revive_signal_with_baseline(
            "gainers_early",
            reason="defensive force on first revival",
            force=True,
            settings=s,
        )
    bypass_events = [
        e for e in captured if e.get("event") == "revive_signal_force_bypass"
    ]
    assert not bypass_events, (
        "force=True on first-ever revival must not log "
        f"revive_signal_force_bypass at WARNING — that path should only "
        f"fire when an actual bypass occurred. Got: {bypass_events}"
    )
    # Confirm the alternative INFO path WAS emitted.
    no_prior_events = [
        e for e in captured if e.get("event") == "revive_signal_force_no_prior"
    ]
    assert no_prior_events, (
        "force=True on first revival must emit revive_signal_force_no_prior "
        f"at INFO; got captured events: {[e.get('event') for e in captured]}"
    )
    await db.close()


async def test_revive_force_true_warning_on_actual_bypass(tmp_path, settings_factory):
    """Companion to the no-warning test: when a prior revival DOES exist
    AND force=True, the revive_signal_force_bypass WARNING must fire."""
    from structlog.testing import capture_logs

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(SIGNAL_REVIVAL_MIN_SOAK_DAYS=7)
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0 WHERE signal_type='gainers_early'"
    )
    await db._conn.commit()
    # First revival to create the audit row.
    await db.revive_signal_with_baseline("gainers_early", reason="first", settings=s)
    # Re-suspend then force-revive within the cool-off window.
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0 WHERE signal_type='gainers_early'"
    )
    await db._conn.commit()
    with capture_logs() as captured:
        await db.revive_signal_with_baseline(
            "gainers_early", reason="force bypass", force=True, settings=s
        )
    bypass_events = [
        e for e in captured if e.get("event") == "revive_signal_force_bypass"
    ]
    assert bypass_events, (
        "force=True bypassing an actual cool-off must emit WARNING "
        f"revive_signal_force_bypass; got: {[e.get('event') for e in captured]}"
    )
    # Verify structured fields are populated.
    evt = bypass_events[0]
    assert evt.get("signal_type") == "gainers_early"
    assert evt.get("prior_revival_at") is not None
    await db.close()


async def test_revive_after_operator_suspend_then_revive_first_time_succeeds(
    tmp_path, settings_factory
):
    """Per design-stage policy reviewer Q4: an operator-issued suspension
    writes a 1→0 audit row (NOT a 0→1 row). The cool-off filter
    `old_value='0' AND new_value='1'` excludes the suspension row, so the
    FIRST post-suspend revival succeeds regardless of cool-off."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(SIGNAL_REVIVAL_MIN_SOAK_DAYS=7)
    # Simulate operator-issued suspension: writes 1→0 audit row + flips enabled.
    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0, suspended_at=?, "
        "suspended_reason='operator: testing' WHERE signal_type='gainers_early'",
        (now_iso,),
    )
    await db._conn.execute(
        "INSERT INTO signal_params_audit "
        "(signal_type, field_name, old_value, new_value, reason, applied_by, applied_at) "
        "VALUES ('gainers_early', 'enabled', '1', '0', 'operator manual suspend', "
        "'operator', ?)",
        (now_iso,),
    )
    await db._conn.commit()
    # First revival — must succeed, even though there's a recent operator
    # audit row (it's the suspend, not a prior revive).
    await db.revive_signal_with_baseline(
        "gainers_early", reason="post-operator-suspend revival", settings=s
    )
    cur = await db._conn.execute(
        "SELECT enabled FROM signal_params WHERE signal_type='gainers_early'"
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()


# --- §2.9 silent-rendering fix (parse_mode=None + dispatched/delivered logs) ---
#
# Trigger evidence: trending_catch was auto-suspended 2026-05-11T01:00:26Z via
# hard_loss; signal_params_audit ID 22 confirms the path fired. Live curl-direct
# replay against Telegram API confirmed HTTP 200 with rendered body
# `trendingcatch ... (hardloss)` and italics entities consuming the underscores.
# Operator did not recognize the mangled alert. Fix: pass parse_mode=None so the
# message renders as plain text. Defense-in-depth: dispatched/delivered structured
# logs make every fire traceable in journalctl even when delivery succeeds (the
# alerter only logs on failure).


def _install_fake_alerter(monkeypatch, capture: list):
    """Replace ``scout.alerter`` for the auto_suspend's local-import path.

    Two-pronged patch needed because Python's ``from scout import alerter``
    resolves in two steps:

      1. ``getattr(scout, "alerter")`` — wins if scout.alerter is already
         loaded and bound as an attribute on the scout package (Linux CI:
         other tests in the session have already imported scout.alerter).
      2. Falls through to ``sys.modules["scout.alerter"]`` — wins if the
         submodule wasn't yet imported (Windows local: real import crashes
         on aiohttp's OpenSSL Applink load per
         ``feedback_windows_venv_openssl_state.md``, so we never let it
         get that far).

    Patching only sys.modules works on Windows but is bypassed on Linux;
    patching only the attribute works on Linux but errors on Windows
    (attribute access on a not-yet-imported submodule triggers import).
    Both paths handled here.
    """
    import sys
    import types
    import scout  # parent package is safe to import; doesn't pull aiohttp

    async def _capture_send(text, session, settings, **kwargs):
        capture.append({"text": text, **kwargs})

    fake = types.ModuleType("scout.alerter")
    fake.send_telegram_message = _capture_send
    monkeypatch.setitem(sys.modules, "scout.alerter", fake)
    monkeypatch.setattr(scout, "alerter", fake, raising=False)


async def test_hard_loss_alert_uses_plain_text_and_traces(
    tmp_path, settings_factory, monkeypatch
):
    """§2.9 fix — hard_loss Telegram alert MUST pass parse_mode=None and emit
    dispatched + delivered structured logs."""
    import structlog

    db = Database(tmp_path / "t.db")
    await db.initialize()
    for _ in range(20):
        await _insert_closed_trade(db, signal_type="gainers_early", pnl_usd=-50)
    await db._conn.commit()

    captured_kwargs: list[dict] = []
    _install_fake_alerter(monkeypatch, captured_kwargs)

    s = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        SIGNAL_SUSPEND_PNL_THRESHOLD_USD=-200.0,
        SIGNAL_SUSPEND_HARD_LOSS_USD=-500.0,
        SIGNAL_SUSPEND_MIN_TRADES=50,
    )

    fake_session = object()  # non-None — exercises the alerter branch
    with structlog.testing.capture_logs() as log_events:
        suspended = await maybe_suspend_signals(db, s, session=fake_session)

    assert any(
        x["signal_type"] == "gainers_early" and x["reason"] == "hard_loss"
        for x in suspended
    ), "hard_loss path must fire for n=20 / net=-$1000 cohort"

    assert len(captured_kwargs) == 1, (
        f"expected exactly 1 send_telegram_message call; got " f"{len(captured_kwargs)}"
    )
    payload = captured_kwargs[0]
    assert payload.get("parse_mode") is None, (
        f"parse_mode MUST be None to avoid Markdown silent-rendering of "
        f"underscore-containing signal names (e.g., gainers_early, hard_loss). "
        f"Got parse_mode={payload.get('parse_mode')!r}. See §2.9 finding."
    )
    assert "gainers_early" in payload["text"]
    assert "hard_loss" in payload["text"]

    events = {e["event"] for e in log_events}
    assert "auto_suspend_alert_dispatched" in events, (
        "dispatched trace log missing — needed for journalctl observability "
        "of every alert fire regardless of delivery outcome"
    )
    assert "auto_suspend_alert_delivered" in events, (
        "delivered trace log missing — needed to distinguish "
        "alerter-returned-cleanly from hung/exception path"
    )
    dispatched = next(
        e for e in log_events if e["event"] == "auto_suspend_alert_dispatched"
    )
    assert dispatched["signal_type"] == "gainers_early"
    assert dispatched["reason"] == "hard_loss"

    await db.close()


async def test_pnl_threshold_alert_uses_plain_text_and_traces(
    tmp_path, settings_factory, monkeypatch
):
    """§2.9 fix — pnl_threshold Telegram alert MUST pass parse_mode=None and
    emit dispatched + delivered structured logs."""
    import structlog

    db = Database(tmp_path / "t.db")
    await db.initialize()
    # 60 small losers — pnl_threshold path, not hard_loss
    for _ in range(60):
        await _insert_closed_trade(db, signal_type="gainers_early", pnl_usd=-10)
    await db._conn.commit()

    captured_kwargs: list[dict] = []
    _install_fake_alerter(monkeypatch, captured_kwargs)

    s = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        SIGNAL_SUSPEND_PNL_THRESHOLD_USD=-200.0,
        SIGNAL_SUSPEND_HARD_LOSS_USD=-5000.0,  # well below net=-$600 so HL skipped
        SIGNAL_SUSPEND_MIN_TRADES=50,
    )

    fake_session = object()
    with structlog.testing.capture_logs() as log_events:
        suspended = await maybe_suspend_signals(db, s, session=fake_session)

    assert any(
        x["signal_type"] == "gainers_early" and x["reason"] == "pnl_threshold"
        for x in suspended
    ), "pnl_threshold path must fire for n=60 / net=-$600 cohort"

    assert len(captured_kwargs) == 1
    payload = captured_kwargs[0]
    assert payload.get("parse_mode") is None, (
        f"parse_mode MUST be None on pnl_threshold path too. "
        f"Got parse_mode={payload.get('parse_mode')!r}."
    )
    assert "pnl_threshold" in payload["text"]

    events = {e["event"] for e in log_events}
    assert "auto_suspend_alert_dispatched" in events
    assert "auto_suspend_alert_delivered" in events

    await db.close()
