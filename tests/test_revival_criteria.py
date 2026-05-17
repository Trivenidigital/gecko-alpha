"""Tests for scout.trading.revival_criteria (BL-NEW-LOSERS-CONTRARIAN-REVIVAL-CRITERIA-TIGHTENING).

Test taxonomy:
- T1: dataclasses + 4-value enum
- T2-5: pure-function diagnostics
- T6: Wilson lower bound
- T7: bootstrap LB
- T8: cutover split + find_latest_regime_cutover (incl. denylist + operator-revival skip)
- T9: Settings keys + validators
- T10: DB helpers (fetch_closed_trades, signal_type_exists, find_existing_keep_verdict, recent_trade_rate)
- T11: orchestrator (evaluate_revival_criteria — BELOW / STRATIFICATION / PASS / FAIL paths)
- T12: CLI helpers (validate, escape, parse_cutover_iso, emit_sql)

Per CLAUDE.md test pattern: tmp_path for aiosqlite; pytest-asyncio auto mode.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.config import Settings
from scout.db import Database
from scout.trading.revival_criteria import (
    ClosedTrade,
    RevivalCriteriaResult,
    RevivalVerdict,
    WindowDiagnostics,
    _emit_soak_verdict_sql,
    _parse_cutover_iso,
    _sql_escape,
    _validate_signal_type,
    compute_bootstrap_lb_per_trade,
    compute_exit_machinery_contribution,
    compute_expired_loss_frequency,
    compute_no_breakout_and_loss_rate,
    compute_recent_trade_rate,
    compute_stop_loss_frequency,
    compute_wilson_lb,
    evaluate_revival_criteria,
    fetch_closed_trades,
    find_existing_keep_verdict,
    find_latest_regime_cutover,
    signal_type_exists,
    split_at_cutover_boundary,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _trade(
    *,
    peak_pct: float | None = 10.0,
    exit_reason: str = "peak_fade",
    pnl_usd: float = 5.0,
    closed_at: datetime | None = None,
    tid: int = 1,
) -> ClosedTrade:
    return ClosedTrade(
        id=tid,
        signal_type="losers_contrarian",
        pnl_usd=pnl_usd,
        pnl_pct=pnl_usd,
        peak_pct=peak_pct,
        exit_reason=exit_reason,
        closed_at=closed_at or datetime(2026, 5, 15, tzinfo=timezone.utc),
    )


async def _seed_trade(
    db,
    *,
    token_id: str,
    signal_type: str,
    exit_reason: str,
    pnl_usd: float,
    peak_pct: float | None,
    closed_at_iso: str,
    opened_at_iso: str | None = None,
):
    """Raw paper_trades insert helper for DB-layer tests."""
    opened_at_iso = opened_at_iso or closed_at_iso
    await db._conn.execute(
        """INSERT INTO paper_trades (
            token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
            status, exit_reason, pnl_usd, pnl_pct, peak_pct,
            opened_at, closed_at, created_at
        ) VALUES (?, 'SYM', 'Name', 'solana', ?, '{}',
            1.0, 100.0, 100.0, 20.0, 25.0, 1.2, 0.75,
            ?, ?, ?, ?, ?,
            ?, ?, ?)""",
        (
            token_id,
            signal_type,
            f"closed_{exit_reason}",
            exit_reason,
            pnl_usd,
            pnl_usd,
            peak_pct,
            opened_at_iso,
            closed_at_iso,
            opened_at_iso,
        ),
    )


async def _seed_cohort(
    db,
    *,
    signal_type: str,
    n: int,
    base_pnl: float,
    peak_pct: float,
    exit_reason: str,
    start_day: int,
    spread_days: int = 14,
):
    """Seed N trades for one signal across a `spread_days`-day window starting `start_day`."""
    for i in range(n):
        day_offset = (i / max(n - 1, 1)) * spread_days
        closed_dt = datetime(2026, 4, start_day, tzinfo=timezone.utc) + timedelta(
            days=day_offset
        )
        opened_dt = closed_dt - timedelta(hours=4)
        await _seed_trade(
            db,
            token_id=f"t_{signal_type}_{exit_reason}_{start_day}_{i}",
            signal_type=signal_type,
            exit_reason=exit_reason,
            pnl_usd=base_pnl,
            peak_pct=peak_pct,
            closed_at_iso=closed_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            opened_at_iso=opened_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    await db._conn.commit()


async def _seed_audit(
    db,
    *,
    signal_type: str,
    field_name: str,
    old_value: str | None,
    new_value: str,
    applied_by: str,
    applied_at: str,
    reason: str = "test",
):
    await db._conn.execute(
        "INSERT INTO signal_params_audit "
        "(signal_type, field_name, old_value, new_value, reason, applied_by, applied_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (signal_type, field_name, old_value, new_value, reason, applied_by, applied_at),
    )
    await db._conn.commit()


# --------------------------------------------------------------------------
# T1: dataclasses + 4-value enum
# --------------------------------------------------------------------------


def test_revival_verdict_has_4_values():
    assert {v.value for v in RevivalVerdict} == {
        "pass",
        "fail",
        "below_min_trades",
        "stratification_infeasible",
    }


def test_dataclasses_construct():
    trade = _trade()
    assert trade.signal_type == "losers_contrarian"
    diag = WindowDiagnostics(
        start_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        end_at=datetime(2026, 5, 8, tzinfo=timezone.utc),
        n=50,
        net_pnl_usd=100.0,
        per_trade_usd=2.0,
        win_pct=60.0,
        win_pct_wilson_lb=52.0,
        per_trade_bootstrap_lb=1.20,
        no_breakout_and_loss_rate=0.10,
        stop_loss_frequency=0.20,
        expired_loss_frequency=0.05,
        exit_machinery_contribution=0.65,
    )
    assert diag.n == 50
    result = RevivalCriteriaResult(
        signal_type="losers_contrarian",
        verdict=RevivalVerdict.PASS,
        n_trades=120,
        cutover_at=datetime(2026, 5, 3, tzinfo=timezone.utc),
        cutover_source="signal_params_audit:auto_suspend:enabled",
        cutover_age_days=14,
        window_a=diag,
        window_b=diag,
        failure_reasons=[],
        evaluated_at=datetime(2026, 5, 17, tzinfo=timezone.utc),
    )
    assert result.verdict is RevivalVerdict.PASS
    assert result.cutover_age_days == 14


# --------------------------------------------------------------------------
# T2: compute_no_breakout_and_loss_rate (V5 fold: AND pnl<0)
# --------------------------------------------------------------------------


def test_no_breakout_and_loss_zero_when_all_breakouts():
    trades = [_trade(peak_pct=10.0, pnl_usd=5.0), _trade(peak_pct=20.0, pnl_usd=10.0)]
    assert compute_no_breakout_and_loss_rate(trades, threshold_pct=5.0) == 0.0


def test_no_breakout_and_loss_one_when_all_low_peak_AND_loss():
    trades = [
        _trade(peak_pct=1.0, pnl_usd=-30.0),
        _trade(peak_pct=4.5, pnl_usd=-10.0),
    ]
    assert compute_no_breakout_and_loss_rate(trades, threshold_pct=5.0) == 1.0


def test_no_breakout_and_loss_excludes_low_peak_BUT_positive_pnl():
    """V5 fold: tight-trail winner with peak<=5% but positive pnl is NOT a failure."""
    trades = [_trade(peak_pct=3.0, pnl_usd=5.0)]
    assert compute_no_breakout_and_loss_rate(trades, threshold_pct=5.0) == 0.0


def test_no_breakout_and_loss_treats_null_peak_as_no_breakout_when_loss():
    trades = [_trade(peak_pct=None, pnl_usd=-10.0), _trade(peak_pct=10.0, pnl_usd=5.0)]
    assert compute_no_breakout_and_loss_rate(trades, threshold_pct=5.0) == 0.5


# --------------------------------------------------------------------------
# T3: compute_stop_loss_frequency + compute_expired_loss_frequency
# --------------------------------------------------------------------------


def test_stop_loss_frequency_counts_stop_loss_exits():
    trades = [
        _trade(exit_reason="stop_loss"),
        _trade(exit_reason="peak_fade"),
        _trade(exit_reason="stop_loss"),
        _trade(exit_reason="trailing_stop"),
    ]
    assert compute_stop_loss_frequency(trades) == 0.5


def test_stop_loss_frequency_zero_on_empty():
    assert compute_stop_loss_frequency([]) == 0.0


def test_expired_loss_frequency_counts_both_variants_with_negative_pnl():
    trades = [
        _trade(exit_reason="expired", pnl_usd=-10.0),
        _trade(exit_reason="expired", pnl_usd=5.0),  # positive expired — not a loss
        _trade(exit_reason="expired_stale_price", pnl_usd=-2.0),
        _trade(exit_reason="peak_fade", pnl_usd=15.0),
    ]
    assert compute_expired_loss_frequency(trades) == 0.5


def test_expired_loss_frequency_zero_on_empty():
    assert compute_expired_loss_frequency([]) == 0.0


# --------------------------------------------------------------------------
# T4: compute_exit_machinery_contribution (V6 fold: broader numerator)
# --------------------------------------------------------------------------


def test_exit_machinery_contribution_aggregates_three_reasons():
    trades = [
        _trade(exit_reason="peak_fade", pnl_usd=60.0),
        _trade(exit_reason="trailing_stop", pnl_usd=40.0),
        _trade(exit_reason="moonshot_trail", pnl_usd=20.0),
        _trade(exit_reason="tp", pnl_usd=10.0),  # not exit-machinery
        _trade(exit_reason="stop_loss", pnl_usd=-30.0),
    ]
    assert abs(compute_exit_machinery_contribution(trades) - 120 / 130) < 1e-9


def test_exit_machinery_contribution_excludes_negative_machinery_trades():
    trades = [
        _trade(exit_reason="peak_fade", pnl_usd=-10.0),  # negative — excluded
        _trade(exit_reason="trailing_stop", pnl_usd=20.0),
    ]
    assert compute_exit_machinery_contribution(trades) == 1.0


def test_exit_machinery_contribution_zero_when_no_positive_trades():
    trades = [_trade(exit_reason="stop_loss", pnl_usd=-50.0)]
    assert compute_exit_machinery_contribution(trades) == 0.0


def test_exit_machinery_contribution_zero_on_empty():
    assert compute_exit_machinery_contribution([]) == 0.0


# --------------------------------------------------------------------------
# T5: compute_wilson_lb
# --------------------------------------------------------------------------


def test_wilson_lb_zero_when_zero_wins():
    assert compute_wilson_lb(wins=0, n=50, z=1.96) == 0.0


def test_wilson_lb_below_point_estimate():
    lb = compute_wilson_lb(wins=60, n=100, z=1.96)
    assert 0.49 < lb < 0.52
    assert lb < 0.60


def test_wilson_lb_approaches_point_at_large_n():
    lb_small = compute_wilson_lb(wins=30, n=50, z=1.96)
    lb_large = compute_wilson_lb(wins=600, n=1000, z=1.96)
    assert lb_large > lb_small


def test_wilson_lb_handles_n_zero():
    assert compute_wilson_lb(wins=0, n=0, z=1.96) == 0.0


# --------------------------------------------------------------------------
# T6: compute_bootstrap_lb_per_trade
# --------------------------------------------------------------------------


def test_bootstrap_lb_positive_for_strongly_positive_sample():
    pnls = [10.0] * 100
    lb = compute_bootstrap_lb_per_trade(pnls, n_resamples=2000, seed=42)
    assert 9.5 < lb < 10.5


def test_bootstrap_lb_negative_for_mixed_negative_sample():
    pnls = [5.0] * 50 + [-15.0] * 50
    lb = compute_bootstrap_lb_per_trade(pnls, n_resamples=2000, seed=42)
    assert lb < 0.0


def test_bootstrap_lb_deterministic_with_seed():
    pnls = [1.0, 2.0, 3.0, 4.0, 5.0] * 20
    lb1 = compute_bootstrap_lb_per_trade(pnls, n_resamples=2000, seed=42)
    lb2 = compute_bootstrap_lb_per_trade(pnls, n_resamples=2000, seed=42)
    assert lb1 == lb2


# --------------------------------------------------------------------------
# T7: split_at_cutover_boundary + find_latest_regime_cutover
# --------------------------------------------------------------------------


def _make_trades_across(start, n, step_days):
    return [
        _trade(closed_at=start + timedelta(days=i * step_days), tid=i) for i in range(n)
    ]


def test_split_at_cutover_partitions_correctly():
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    trades = _make_trades_across(base, 120, 0.2)
    cutover = base + timedelta(days=10)
    result = split_at_cutover_boundary(
        trades, cutover_at=cutover, min_window_days=7, min_window_trades=50
    )
    assert result is not None
    a, b = result
    assert all(t.closed_at < cutover for t in a)
    assert all(t.closed_at >= cutover for t in b)


def test_split_returns_none_when_either_window_too_small():
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    trades = _make_trades_across(base, 40, 0.1)
    cutover = base + timedelta(days=2)
    assert (
        split_at_cutover_boundary(
            trades, cutover_at=cutover, min_window_days=7, min_window_trades=50
        )
        is None
    )


def test_split_returns_none_when_window_span_too_short():
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    trades = [_trade(closed_at=base + timedelta(hours=i), tid=i) for i in range(100)]
    cutover = base + timedelta(hours=50)
    assert (
        split_at_cutover_boundary(
            trades, cutover_at=cutover, min_window_days=7, min_window_trades=50
        )
        is None
    )


@pytest.mark.asyncio
async def test_find_latest_regime_cutover_skips_operator_revival(tmp_path):
    """V3 C#6: operator-revival is OUTCOME of regime change, not the cutover itself."""
    db = Database(str(tmp_path / "scout.db"))
    await db.connect()
    await _seed_audit(
        db,
        signal_type="losers_contrarian",
        field_name="enabled",
        old_value="1",
        new_value="0",
        applied_by="auto_suspend",
        applied_at="2026-05-01T00:00:00Z",
        reason="hard_loss",
    )
    await _seed_audit(
        db,
        signal_type="losers_contrarian",
        field_name="enabled",
        old_value="0",
        new_value="1",
        applied_by="operator",
        applied_at="2026-05-06T00:00:00Z",
        reason="op revive",
    )
    cutover_at, source = await find_latest_regime_cutover(db, "losers_contrarian")
    assert cutover_at == datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert "auto_suspend" in source
    await db.close()


@pytest.mark.asyncio
async def test_find_latest_regime_cutover_returns_calibrate_field(tmp_path):
    """V3 C#1: denylist (not allowlist) must accept calibrate.py's dynamic field names."""
    db = Database(str(tmp_path / "scout.db"))
    await db.connect()
    await _seed_audit(
        db,
        signal_type="losers_contrarian",
        field_name="leg_1_pct",
        old_value="25.0",
        new_value="30.0",
        applied_by="calibrate",
        applied_at="2026-05-10T00:00:00Z",
    )
    cutover_at, source = await find_latest_regime_cutover(db, "losers_contrarian")
    assert cutover_at == datetime(2026, 5, 10, tzinfo=timezone.utc)
    assert "leg_1_pct" in source
    await db.close()


@pytest.mark.asyncio
async def test_find_latest_regime_cutover_skips_soak_verdict(tmp_path):
    """soak_verdict rows are denylisted — they're CONSEQUENCES of evaluation,
    not regime triggers. Even though they're newest, the test must return None."""
    db = Database(str(tmp_path / "scout.db"))
    await db.connect()
    await _seed_audit(
        db,
        signal_type="losers_contrarian",
        field_name="soak_verdict",
        old_value=None,
        new_value="keep_on_provisional_until_2026-06-15T00:00:00Z",
        applied_by="operator",
        applied_at="2026-05-15T00:00:00Z",
    )
    cutover_at, source = await find_latest_regime_cutover(db, "losers_contrarian")
    assert cutover_at is None
    assert source == "no_audit_events"
    await db.close()


@pytest.mark.asyncio
async def test_find_latest_regime_cutover_returns_none_when_no_audit(tmp_path):
    db = Database(str(tmp_path / "scout.db"))
    await db.connect()
    cutover_at, source = await find_latest_regime_cutover(db, "losers_contrarian")
    assert cutover_at is None
    assert source == "no_audit_events"
    await db.close()


# --------------------------------------------------------------------------
# T9: Settings defaults
# --------------------------------------------------------------------------


def test_settings_has_revival_criteria_defaults():
    s = Settings()
    assert s.REVIVAL_CRITERIA_MIN_TRADES == 100
    assert s.REVIVAL_CRITERIA_MIN_WINDOW_DAYS == 7
    assert s.REVIVAL_CRITERIA_MIN_WINDOW_TRADES == 50
    assert s.REVIVAL_CRITERIA_NO_BREAKOUT_PEAK_PCT == 5.0
    assert s.REVIVAL_CRITERIA_MAX_NO_BREAKOUT_AND_LOSS == 0.40
    assert s.REVIVAL_CRITERIA_EXIT_MACHINERY_MIN == 0.70
    assert s.REVIVAL_CRITERIA_WIN_WILSON_LB_MIN == 0.55
    assert s.REVIVAL_CRITERIA_BOOTSTRAP_RESAMPLES == 10_000
    assert s.REVIVAL_CRITERIA_VERDICT_EXPIRY_DAYS == 30


def test_settings_revival_criteria_validators_reject_invalid():
    with pytest.raises(ValueError, match="must be >= 1"):
        Settings(REVIVAL_CRITERIA_MIN_TRADES=0)
    with pytest.raises(ValueError, match="must be in"):
        Settings(REVIVAL_CRITERIA_MAX_NO_BREAKOUT_AND_LOSS=2.0)
    with pytest.raises(ValueError, match=">= 0"):
        Settings(REVIVAL_CRITERIA_NO_BREAKOUT_PEAK_PCT=-1.0)


# --------------------------------------------------------------------------
# T10: DB layer
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_closed_trades_returns_typed_rows(tmp_path):
    db = Database(str(tmp_path / "scout.db"))
    await db.connect()
    await _seed_trade(
        db,
        token_id="t1",
        signal_type="losers_contrarian",
        exit_reason="peak_fade",
        pnl_usd=50.0,
        peak_pct=25.0,
        closed_at_iso="2026-05-15T00:00:00Z",
    )
    await db._conn.commit()
    trades = await fetch_closed_trades(db, signal_type="losers_contrarian")
    assert len(trades) == 1
    assert trades[0].exit_reason == "peak_fade"
    assert trades[0].pnl_usd == 50.0
    assert trades[0].peak_pct == 25.0
    await db.close()


@pytest.mark.asyncio
async def test_fetch_closed_trades_null_peak_round_trips(tmp_path):
    """V3 B#6: NULL peak_pct must survive SQL → dataclass round-trip."""
    db = Database(str(tmp_path / "scout.db"))
    await db.connect()
    await _seed_trade(
        db,
        token_id="t_null",
        signal_type="losers_contrarian",
        exit_reason="stop_loss",
        pnl_usd=-25.0,
        peak_pct=None,
        closed_at_iso="2026-05-15T00:00:00Z",
    )
    await db._conn.commit()
    trades = await fetch_closed_trades(db, signal_type="losers_contrarian")
    assert len(trades) == 1
    assert trades[0].peak_pct is None
    assert trades[0].pnl_usd == -25.0
    rate = compute_no_breakout_and_loss_rate(trades, threshold_pct=5.0)
    assert rate == 1.0
    await db.close()


@pytest.mark.asyncio
async def test_signal_type_exists(tmp_path):
    db = Database(str(tmp_path / "scout.db"))
    await db.connect()
    # Database.connect() seeds default signal_params rows
    assert await signal_type_exists(db, "losers_contrarian") is True
    assert await signal_type_exists(db, "nonexistent_signal_xyz") is False
    await db.close()


@pytest.mark.asyncio
async def test_find_existing_keep_verdict_returns_none_when_no_row(tmp_path):
    db = Database(str(tmp_path / "scout.db"))
    await db.connect()
    assert await find_existing_keep_verdict(db, "losers_contrarian") is None
    await db.close()


@pytest.mark.asyncio
async def test_find_existing_keep_verdict_returns_most_recent_keep(tmp_path):
    db = Database(str(tmp_path / "scout.db"))
    await db.connect()
    await _seed_audit(
        db,
        signal_type="losers_contrarian",
        field_name="soak_verdict",
        old_value=None,
        new_value="keep_on_provisional_until_2026-06-15T00:00:00Z",
        applied_by="operator",
        applied_at="2026-05-15T00:00:00Z",
    )
    result = await find_existing_keep_verdict(db, "losers_contrarian")
    assert result is not None
    iso, value = result
    assert iso == "2026-05-15T00:00:00Z"
    assert value.startswith("keep_on_provisional_until_")
    await db.close()


@pytest.mark.asyncio
async def test_compute_recent_trade_rate_returns_zero_on_empty(tmp_path):
    db = Database(str(tmp_path / "scout.db"))
    await db.connect()
    rate = await compute_recent_trade_rate(db, "losers_contrarian", lookback_days=7)
    assert rate == 0.0
    await db.close()


# --------------------------------------------------------------------------
# T11: orchestrator
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_returns_below_min_trades_when_n_under_floor(tmp_path):
    db = Database(str(tmp_path / "scout.db"))
    await db.connect()
    await _seed_audit(
        db,
        signal_type="losers_contrarian",
        field_name="enabled",
        old_value="1",
        new_value="0",
        applied_by="auto_suspend",
        applied_at="2026-04-15T00:00:00Z",
    )
    for i in range(5):
        await _seed_trade(
            db,
            token_id=f"t{i}",
            signal_type="losers_contrarian",
            exit_reason="peak_fade",
            pnl_usd=15.0,
            peak_pct=20.0,
            closed_at_iso=f"2026-05-{10+i:02d}T00:00:00Z",
        )
    await db._conn.commit()
    result = await evaluate_revival_criteria(db, "losers_contrarian", Settings())
    assert result.verdict is RevivalVerdict.BELOW_MIN_TRADES
    assert "n_trades=5" in " ".join(result.failure_reasons)
    await db.close()


@pytest.mark.asyncio
async def test_evaluate_returns_stratification_infeasible_when_no_audit_cutover(
    tmp_path,
):
    db = Database(str(tmp_path / "scout.db"))
    await db.connect()
    await _seed_cohort(
        db,
        signal_type="losers_contrarian",
        n=120,
        base_pnl=15.0,
        peak_pct=20.0,
        exit_reason="peak_fade",
        start_day=1,
        spread_days=28,
    )
    result = await evaluate_revival_criteria(db, "losers_contrarian", Settings())
    assert result.verdict is RevivalVerdict.STRATIFICATION_INFEASIBLE
    assert "no regime cutover" in " ".join(result.failure_reasons).lower()
    await db.close()


@pytest.mark.asyncio
async def test_evaluate_returns_pass_when_all_gates_clear(tmp_path):
    db = Database(str(tmp_path / "scout.db"))
    await db.connect()
    await _seed_audit(
        db,
        signal_type="losers_contrarian",
        field_name="enabled",
        old_value="1",
        new_value="0",
        applied_by="auto_suspend",
        applied_at="2026-04-15T00:00:00Z",
    )
    # Window A (pre-cutover): 60 winners across April 1-14
    await _seed_cohort(
        db,
        signal_type="losers_contrarian",
        n=60,
        base_pnl=20.0,
        peak_pct=25.0,
        exit_reason="peak_fade",
        start_day=1,
        spread_days=13,
    )
    # Window B (post-cutover): 60 winners across April 15-28
    await _seed_cohort(
        db,
        signal_type="losers_contrarian",
        n=60,
        base_pnl=20.0,
        peak_pct=25.0,
        exit_reason="peak_fade",
        start_day=15,
        spread_days=13,
    )
    settings = Settings(REVIVAL_CRITERIA_BOOTSTRAP_RESAMPLES=500)
    result = await evaluate_revival_criteria(db, "losers_contrarian", settings)
    assert result.verdict is RevivalVerdict.PASS, result.failure_reasons
    assert result.window_a is not None and result.window_b is not None
    assert result.cutover_age_days is not None
    await db.close()


@pytest.mark.asyncio
async def test_evaluate_fails_when_window_b_bootstrap_lb_negative(tmp_path):
    db = Database(str(tmp_path / "scout.db"))
    await db.connect()
    await _seed_audit(
        db,
        signal_type="losers_contrarian",
        field_name="enabled",
        old_value="1",
        new_value="0",
        applied_by="auto_suspend",
        applied_at="2026-04-15T00:00:00Z",
    )
    await _seed_cohort(
        db,
        signal_type="losers_contrarian",
        n=60,
        base_pnl=20.0,
        peak_pct=25.0,
        exit_reason="peak_fade",
        start_day=1,
        spread_days=13,
    )
    await _seed_cohort(
        db,
        signal_type="losers_contrarian",
        n=60,
        base_pnl=-25.0,
        peak_pct=3.0,
        exit_reason="stop_loss",
        start_day=15,
        spread_days=13,
    )
    settings = Settings(REVIVAL_CRITERIA_BOOTSTRAP_RESAMPLES=500)
    result = await evaluate_revival_criteria(db, "losers_contrarian", settings)
    assert result.verdict is RevivalVerdict.FAIL
    assert any("window_b" in r for r in result.failure_reasons)
    await db.close()


@pytest.mark.asyncio
async def test_evaluate_uses_explicit_cutover_override(tmp_path):
    db = Database(str(tmp_path / "scout.db"))
    await db.connect()
    await _seed_cohort(
        db,
        signal_type="losers_contrarian",
        n=60,
        base_pnl=20.0,
        peak_pct=25.0,
        exit_reason="peak_fade",
        start_day=1,
        spread_days=13,
    )
    await _seed_cohort(
        db,
        signal_type="losers_contrarian",
        n=60,
        base_pnl=20.0,
        peak_pct=25.0,
        exit_reason="peak_fade",
        start_day=15,
        spread_days=13,
    )
    override = datetime(2026, 4, 15, tzinfo=timezone.utc)
    settings = Settings(REVIVAL_CRITERIA_BOOTSTRAP_RESAMPLES=500)
    result = await evaluate_revival_criteria(
        db, "losers_contrarian", settings, cutover_override=override
    )
    assert result.verdict is RevivalVerdict.PASS, result.failure_reasons
    assert result.cutover_source == "operator_override"
    await db.close()


# --------------------------------------------------------------------------
# T12: CLI helpers
# --------------------------------------------------------------------------


def test_validate_signal_type_accepts_known_signals():
    _validate_signal_type("losers_contrarian")
    _validate_signal_type("gainers_early")


def test_validate_signal_type_rejects_injection():
    with pytest.raises(ValueError, match="signal_type"):
        _validate_signal_type("x'; DROP TABLE")
    with pytest.raises(ValueError, match="signal_type"):
        _validate_signal_type("Caps")
    with pytest.raises(ValueError, match="signal_type"):
        _validate_signal_type("")


def test_sql_escape_doubles_single_quotes():
    assert _sql_escape("it's") == "it''s"
    assert _sql_escape("'") == "''"
    assert _sql_escape("clean") == "clean"


def test_parse_cutover_iso_accepts_valid_iso():
    dt = _parse_cutover_iso("2026-05-15T00:00:00Z")
    assert dt == datetime(2026, 5, 15, tzinfo=timezone.utc)


def test_parse_cutover_iso_rejects_empty_and_garbage():
    import argparse as _ap

    with pytest.raises(_ap.ArgumentTypeError):
        _parse_cutover_iso("")
    with pytest.raises(_ap.ArgumentTypeError):
        _parse_cutover_iso("not-an-iso")


def test_parse_cutover_iso_normalizes_naive_to_utc():
    """PR-stage reviewer #2 finding #19: naive ISO must become tz-aware
    so downstream subtraction against tz-aware audit cutovers doesn't crash."""
    dt = _parse_cutover_iso("2026-05-01T00:00:00")
    assert dt.tzinfo is not None
    assert dt == datetime(2026, 5, 1, tzinfo=timezone.utc)


def test_emit_sql_returns_none_on_fail():
    result = RevivalCriteriaResult(
        signal_type="losers_contrarian",
        verdict=RevivalVerdict.FAIL,
        n_trades=120,
        cutover_at=None,
        cutover_source="x",
        cutover_age_days=None,
        window_a=None,
        window_b=None,
        failure_reasons=["whatever"],
        evaluated_at=datetime(2026, 5, 17, tzinfo=timezone.utc),
    )
    assert _emit_soak_verdict_sql(result, operator="operator", settings=Settings()) is None


def test_emit_sql_returns_safe_insert_on_pass():
    diag = WindowDiagnostics(
        start_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        end_at=datetime(2026, 4, 15, tzinfo=timezone.utc),
        n=60,
        net_pnl_usd=900.0,
        per_trade_usd=15.0,
        win_pct=70.0,
        win_pct_wilson_lb=58.0,
        per_trade_bootstrap_lb=8.0,
        no_breakout_and_loss_rate=0.10,
        stop_loss_frequency=0.15,
        expired_loss_frequency=0.05,
        exit_machinery_contribution=0.75,
    )
    result = RevivalCriteriaResult(
        signal_type="losers_contrarian",
        verdict=RevivalVerdict.PASS,
        n_trades=120,
        cutover_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        cutover_source="signal_params_audit:auto_suspend:enabled",
        cutover_age_days=16,
        window_a=diag,
        window_b=diag,
        failure_reasons=[],
        evaluated_at=datetime(2026, 5, 17, tzinfo=timezone.utc),
    )
    sql = _emit_soak_verdict_sql(result, operator="operator", settings=Settings())
    assert sql is not None
    assert "BEGIN IMMEDIATE" in sql
    assert "PRAGMA busy_timeout=30000" in sql
    assert "COMMIT" in sql
    assert "NULL" in sql
    assert "keep_on_provisional_until_2026-06-16" in sql
    assert "losers_contrarian" in sql
    assert "BL-NEW-REVIVAL-COOLOFF" in sql
    # PR-stage reviewer #3 finding #4: microsecond truncation in verdict string
    assert ".000000" not in sql
    assert ".703324" not in sql
