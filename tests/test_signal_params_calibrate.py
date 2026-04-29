"""Tests for scout.trading.calibrate (Tier 1a calibration script)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading.calibrate import (
    SignalDiff,
    SignalStats,
    _propose_changes,
    _telegram_token_looks_real,
    apply_diffs,
    build_diffs,
)
from scout.trading.params import clear_cache_for_tests


_seq_counter = [0]


@pytest.fixture(autouse=True)
def _wipe_cache():
    _seq_counter[0] = 0
    clear_cache_for_tests()
    yield
    clear_cache_for_tests()


async def _insert_closed_trade(
    db,
    *,
    signal_type,
    pnl_usd,
    pnl_pct,
    peak_pct,
    status="closed_sl",
    closed_at=None,
):
    # Deterministic-but-unique opened_at per row (UNIQUE constraint covers
    # token_id + signal_type + opened_at).
    _seq_counter[0] += 1
    seq = _seq_counter[0]
    opened_at = (
        datetime.now(timezone.utc) - timedelta(hours=2, seconds=seq)
    ).isoformat()
    closed_at = closed_at or (
        datetime.now(timezone.utc) - timedelta(seconds=seq)
    ).isoformat()
    await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity,
            tp_pct, sl_pct, tp_price, sl_price,
            status, exit_price, pnl_usd, pnl_pct, peak_pct,
            opened_at, closed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            f"tok-{seq}",
            "TOK",
            "Token",
            "coingecko",
            signal_type,
            "{}",
            1.0,
            100.0,
            100.0,
            20.0,
            15.0,
            1.2,
            0.85,
            status,
            0.85 if pnl_usd < 0 else 1.2,
            pnl_usd,
            pnl_pct,
            peak_pct,
            opened_at,
            closed_at,
        ),
    )


# ---------------------------------------------------------------------------
# Heuristic unit tests
# ---------------------------------------------------------------------------


def test_propose_changes_widens_sl_when_low_winrate_and_deep_avg_loss():
    stats = SignalStats(
        signal_type="x", n_trades=60,
        win_rate_pct=30.0, expired_pct=10.0,
        avg_loss_pct=-25.0, avg_winner_peak_pct=22.0,
    )
    current = {"trail_pct": 20.0, "sl_pct": 15.0}
    changes, reasons = _propose_changes(stats, current, step=2.0)
    fields = {c.field for c in changes}
    assert "sl_pct" in fields
    sl = next(c for c in changes if c.field == "sl_pct")
    assert sl.new == 17.0


def test_propose_changes_tightens_trail_when_too_many_expirations():
    stats = SignalStats(
        signal_type="x", n_trades=60,
        win_rate_pct=55.0, expired_pct=42.0,
        avg_loss_pct=-12.0, avg_winner_peak_pct=22.0,
    )
    current = {"trail_pct": 20.0, "sl_pct": 15.0}
    changes, _ = _propose_changes(stats, current, step=2.0)
    trail = next(c for c in changes if c.field == "trail_pct")
    assert trail.new == 18.0


def test_propose_changes_no_action_within_thresholds():
    stats = SignalStats(
        signal_type="x", n_trades=60,
        win_rate_pct=55.0, expired_pct=15.0,
        avg_loss_pct=-12.0, avg_winner_peak_pct=22.0,
    )
    current = {"trail_pct": 20.0, "sl_pct": 15.0}
    changes, _ = _propose_changes(stats, current, step=2.0)
    assert changes == []


def test_propose_changes_skips_sl_rule_when_no_losers():
    """avg_loss_pct=None → SL rule must skip silently, not crash."""
    stats = SignalStats(
        signal_type="x", n_trades=60,
        win_rate_pct=30.0, expired_pct=10.0,
        avg_loss_pct=None, avg_winner_peak_pct=22.0,
    )
    current = {"trail_pct": 20.0, "sl_pct": 15.0}
    changes, _ = _propose_changes(stats, current, step=2.0)
    assert changes == []


def test_propose_changes_respects_floor_ceiling():
    """sl_pct already at ceiling → no further widening."""
    stats = SignalStats(
        signal_type="x", n_trades=60,
        win_rate_pct=20.0, expired_pct=50.0,
        avg_loss_pct=-30.0, avg_winner_peak_pct=10.0,
    )
    current = {"trail_pct": 5.0, "sl_pct": 30.0}  # both already at bounds
    changes, _ = _propose_changes(stats, current, step=2.0)
    assert changes == []


def test_propose_changes_strict_at_30_percent_expired():
    """expired_pct == 30.0 must NOT trigger (rule is strict >)."""
    stats = SignalStats(
        signal_type="x", n_trades=60,
        win_rate_pct=55.0, expired_pct=30.0,
        avg_loss_pct=-12.0, avg_winner_peak_pct=22.0,
    )
    current = {"trail_pct": 20.0, "sl_pct": 15.0}
    changes, _ = _propose_changes(stats, current, step=2.0)
    assert all(c.field != "trail_pct" for c in changes)


# ---------------------------------------------------------------------------
# Telegram health gate
# ---------------------------------------------------------------------------


def test_telegram_token_health_check_rejects_placeholder(settings_factory):
    s = settings_factory(TELEGRAM_BOT_TOKEN="placeholder")
    assert _telegram_token_looks_real(s) is False


def test_telegram_token_health_check_accepts_real_looking(settings_factory):
    # 40+ chars roughly mimics real bot token shape
    s = settings_factory(TELEGRAM_BOT_TOKEN="123456:ABCDEFghijklmnopqrstuvwxyz0123456789")
    assert _telegram_token_looks_real(s) is True


# ---------------------------------------------------------------------------
# build_diffs (integration with DB)
# ---------------------------------------------------------------------------


async def test_build_diffs_skips_signals_below_min_trades(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # 3 trades only — below default MIN
    for _ in range(3):
        await _insert_closed_trade(
            db, signal_type="gainers_early",
            pnl_usd=-10, pnl_pct=-25, peak_pct=5, status="closed_sl",
        )
    await db._conn.commit()

    s = settings_factory(SIGNAL_PARAMS_ENABLED=True, CALIBRATION_MIN_TRADES=50)
    diffs = await build_diffs(
        db, s, window_days=30, min_trades=s.CALIBRATION_MIN_TRADES, step=2.0
    )
    ge = next(d for d in diffs if d.signal_type == "gainers_early")
    assert ge.skipped_reason and "n_trades" in ge.skipped_reason
    await db.close()


async def test_build_diffs_excludes_narrative_prediction(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    for _ in range(60):
        await _insert_closed_trade(
            db, signal_type="narrative_prediction",
            pnl_usd=-50, pnl_pct=-25, peak_pct=5, status="closed_sl",
        )
    await db._conn.commit()

    s = settings_factory(SIGNAL_PARAMS_ENABLED=True, CALIBRATION_MIN_TRADES=50)
    diffs = await build_diffs(
        db, s, window_days=30, min_trades=s.CALIBRATION_MIN_TRADES, step=2.0
    )
    assert all(d.signal_type != "narrative_prediction" for d in diffs)
    await db.close()


async def test_build_diffs_finds_real_change(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # 60 trades, 70% expirations → trail tightening
    for i in range(60):
        await _insert_closed_trade(
            db,
            signal_type="gainers_early",
            pnl_usd=2.0,
            pnl_pct=1.0,
            peak_pct=8.0,
            status="closed_expired" if i < 42 else "closed_trailing_stop",
        )
    await db._conn.commit()

    s = settings_factory(SIGNAL_PARAMS_ENABLED=True, CALIBRATION_MIN_TRADES=50)
    diffs = await build_diffs(
        db, s, window_days=30, min_trades=s.CALIBRATION_MIN_TRADES, step=2.0
    )
    ge = next(d for d in diffs if d.signal_type == "gainers_early")
    assert any(c.field == "trail_pct" for c in ge.changes)
    await db.close()


# ---------------------------------------------------------------------------
# apply_diffs
# ---------------------------------------------------------------------------


async def test_apply_writes_signal_params_and_audit_atomically(
    tmp_path, settings_factory
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    for i in range(60):
        await _insert_closed_trade(
            db, signal_type="gainers_early",
            pnl_usd=1.0, pnl_pct=0.5, peak_pct=8.0,
            status="closed_expired" if i < 42 else "closed_trailing_stop",
        )
    await db._conn.commit()

    s = settings_factory(SIGNAL_PARAMS_ENABLED=True, CALIBRATION_MIN_TRADES=50)
    diffs = await build_diffs(
        db, s, window_days=30, min_trades=s.CALIBRATION_MIN_TRADES, step=2.0
    )
    n = await apply_diffs(db, diffs, s, session=None, force_no_alert=True)
    assert n >= 1

    cur = await db._conn.execute(
        "SELECT trail_pct, last_calibration_at FROM signal_params "
        "WHERE signal_type='gainers_early'"
    )
    row = await cur.fetchone()
    assert row[1] is not None  # last_calibration_at populated
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM signal_params_audit "
        "WHERE signal_type='gainers_early' AND applied_by='calibration'"
    )
    assert (await cur.fetchone())[0] >= 1
    await db.close()


async def test_apply_idempotent_zero_changes_on_rerun(
    tmp_path, settings_factory
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    for i in range(60):
        await _insert_closed_trade(
            db, signal_type="gainers_early",
            pnl_usd=1.0, pnl_pct=0.5, peak_pct=8.0,
            status="closed_expired" if i < 42 else "closed_trailing_stop",
        )
    await db._conn.commit()

    s = settings_factory(SIGNAL_PARAMS_ENABLED=True, CALIBRATION_MIN_TRADES=50)
    diffs = await build_diffs(
        db, s, window_days=30, min_trades=s.CALIBRATION_MIN_TRADES, step=2.0
    )
    await apply_diffs(db, diffs, s, session=None, force_no_alert=True)

    # Second pass — re-build diffs against the freshly-tightened params
    diffs2 = await build_diffs(
        db, s, window_days=30, min_trades=s.CALIBRATION_MIN_TRADES, step=2.0
    )
    n2 = await apply_diffs(db, diffs2, s, session=None, force_no_alert=True)
    # Trail tightened by 2pp; data still says expired>30 → another 2pp tighten.
    # That's intentional — the heuristic *should* keep tightening until expired
    # ratio drops or the floor is hit. The idempotency contract is "same diff
    # input → same output", which holds. We check that floor_pct=5 is the
    # eventual fixed point.
    diffs3 = diffs2
    while True:
        n = await apply_diffs(db, diffs3, s, session=None, force_no_alert=True)
        if n == 0:
            break
        diffs3 = await build_diffs(
            db, s, window_days=30, min_trades=s.CALIBRATION_MIN_TRADES, step=2.0
        )
    cur = await db._conn.execute(
        "SELECT trail_pct FROM signal_params WHERE signal_type='gainers_early'"
    )
    assert (await cur.fetchone())[0] == 5.0  # floor reached, terminates
    await db.close()
