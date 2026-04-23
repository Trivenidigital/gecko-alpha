"""Tests for trailing-stop exit logic on paper trades."""

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading.evaluator import evaluate_paper_trades
from tests.test_trading_evaluator import _insert_trade, _seed_price


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


def _settings_factory(tmp_path, **overrides):
    from scout.config import Settings

    defaults = dict(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_CHAT_ID="test",
        ANTHROPIC_API_KEY="test",
        DB_PATH=tmp_path / "test.db",
        PAPER_TP_PCT=40.0,
        PAPER_SL_PCT=20.0,
        PAPER_SLIPPAGE_BPS=0,
        PAPER_MAX_DURATION_HOURS=48,
        PAPER_TRAILING_ENABLED=True,
        PAPER_TRAILING_ACTIVATION_PCT=10.0,
        PAPER_TRAILING_DRAWDOWN_PCT=10.0,
        PAPER_TRAILING_FLOOR_PCT=3.0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


async def _set_peak(db, trade_id, peak_price, peak_pct):
    await db._conn.execute(
        "UPDATE paper_trades SET peak_price = ?, peak_pct = ? WHERE id = ?",
        (peak_price, peak_pct, trade_id),
    )
    await db._conn.commit()


async def _fetch(db, trade_id):
    cursor = await db._conn.execute(
        "SELECT status, exit_reason, peak_price, peak_pct FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    return await cursor.fetchone()


async def test_trailing_stop_closes_when_price_drops_from_peak(db, tmp_path):
    """If peak hit >= activation and price falls by drawdown%, close at trailing_stop."""
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    trade_id = await _insert_trade(
        db, "bitcoin", 100.0, opened, tp_price=140.0, sl_price=80.0
    )
    await _set_peak(db, trade_id, 120.0, 20.0)
    # Current price 107 is peak-10.8% and above entry+3% floor=103.
    await _seed_price(db, "bitcoin", 107.0)

    await evaluate_paper_trades(db, settings)

    row = await _fetch(db, trade_id)
    assert row[0] == "closed_trailing_stop"
    assert row[1] == "trailing_stop"


async def test_trailing_stop_does_not_fire_below_activation(db, tmp_path):
    """If peak < activation_pct, trailing stop does not fire."""
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    trade_id = await _insert_trade(
        db, "bitcoin", 100.0, opened, tp_price=140.0, sl_price=80.0
    )
    await _set_peak(db, trade_id, 105.0, 5.0)
    await _seed_price(db, "bitcoin", 94.5)

    await evaluate_paper_trades(db, settings)

    row = await _fetch(db, trade_id)
    assert row[0] == "open"
    assert row[1] is None


async def test_trailing_stop_respects_floor_for_non_long_hold(db, tmp_path):
    """Trailing stop skips firing below entry+floor on a normal trade (regular SL owns the downside)."""
    settings = _settings_factory(tmp_path, PAPER_TRAILING_FLOOR_PCT=5.0)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    trade_id = await _insert_trade(
        db, "bitcoin", 100.0, opened, tp_price=140.0, sl_price=80.0
    )
    await _set_peak(db, trade_id, 115.0, 15.0)
    # Price at 102 is below entry+5%=105 floor AND below drawdown (115*0.9=103.5).
    await _seed_price(db, "bitcoin", 102.0)

    await evaluate_paper_trades(db, settings)

    row = await _fetch(db, trade_id)
    assert row[0] == "open"
    assert row[1] is None


async def test_trailing_stop_floor_blocked_emits_log(db, tmp_path, capsys):
    """When trailing would fire but price is below floor, emit trailing_stop_floor_blocked."""
    settings = _settings_factory(tmp_path, PAPER_TRAILING_FLOOR_PCT=5.0)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    trade_id = await _insert_trade(
        db, "bitcoin", 100.0, opened, tp_price=140.0, sl_price=80.0
    )
    await _set_peak(db, trade_id, 115.0, 15.0)
    await _seed_price(db, "bitcoin", 102.0)

    await evaluate_paper_trades(db, settings)

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "trailing_stop_floor_blocked" in combined
    row = await _fetch(db, trade_id)
    assert row[0] == "open"


async def test_trailing_stop_disabled_by_flag(db, tmp_path):
    """PAPER_TRAILING_ENABLED=False bypasses trailing stop entirely."""
    settings = _settings_factory(tmp_path, PAPER_TRAILING_ENABLED=False)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    trade_id = await _insert_trade(
        db, "bitcoin", 100.0, opened, tp_price=140.0, sl_price=80.0
    )
    await _set_peak(db, trade_id, 120.0, 20.0)
    await _seed_price(db, "bitcoin", 107.0)

    await evaluate_paper_trades(db, settings)

    row = await _fetch(db, trade_id)
    assert row[0] == "open"
    assert row[1] is None


async def test_trailing_stop_long_hold_bypasses_floor(db, tmp_path):
    """long_hold (sl_price=0) must close on drawdown even when below entry+floor.

    Otherwise a long_hold that pumps then bleeds below entry has neither SL nor
    trailing protection and only closes at 48h expiry.
    """
    settings = _settings_factory(tmp_path, PAPER_TRAILING_FLOOR_PCT=3.0)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    trade_id = await _insert_trade(
        db,
        "ethereum",
        100.0,
        opened,
        signal_type="long_hold",
        tp_price=200.0,
        sl_price=0.0,
        tp_pct=100.0,
        sl_pct=0.0,
    )
    await _set_peak(db, trade_id, 130.0, 30.0)
    # 101 is below drawdown (130*0.9=117) AND below floor (103), yet long_hold
    # must still close.
    await _seed_price(db, "ethereum", 101.0)

    await evaluate_paper_trades(db, settings)

    row = await _fetch(db, trade_id)
    assert row[0] == "closed_trailing_stop"
    assert row[1] == "trailing_stop"


async def test_trailing_stop_applies_to_long_hold_above_floor(db, tmp_path):
    """long_hold still closes on drawdown when above floor (sanity check)."""
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    trade_id = await _insert_trade(
        db,
        "ethereum",
        100.0,
        opened,
        signal_type="long_hold",
        tp_price=200.0,
        sl_price=0.0,
        tp_pct=100.0,
        sl_pct=0.0,
    )
    await _set_peak(db, trade_id, 130.0, 30.0)
    await _seed_price(db, "ethereum", 116.0)  # -10.8% from peak, +16% vs entry

    await evaluate_paper_trades(db, settings)

    row = await _fetch(db, trade_id)
    assert row[0] == "closed_trailing_stop"
    assert row[1] == "trailing_stop"


async def test_peak_persists_across_eval_cycles(db, tmp_path):
    """Peak must survive across evaluate_paper_trades calls (no re-arm)."""
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    trade_id = await _insert_trade(
        db, "bitcoin", 100.0, opened, tp_price=140.0, sl_price=80.0
    )
    # First cycle: price pumps to 125 -> peak becomes 125 (+25%)
    await _seed_price(db, "bitcoin", 125.0)
    await evaluate_paper_trades(db, settings)

    row = await _fetch(db, trade_id)
    assert row[0] == "open"
    assert row[2] == pytest.approx(125.0)
    assert row[3] == pytest.approx(25.0)

    # Second cycle: price retreats to 110, peak must stay 125 (monotonic)
    await _seed_price(db, "bitcoin", 110.0)
    await evaluate_paper_trades(db, settings)

    row = await _fetch(db, trade_id)
    # Either trailing fired (peak-12% < drawdown=12.5%, still above floor=103)
    # or remained open; regardless, peak should still be 125.
    assert row[2] == pytest.approx(125.0)


async def test_peak_is_monotonic_non_decreasing(db, tmp_path):
    """Peak resets only upward: +20 -> +5 -> +15 leaves peak at +20."""
    settings = _settings_factory(tmp_path, PAPER_TRAILING_ENABLED=False)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    trade_id = await _insert_trade(
        db, "bitcoin", 100.0, opened, tp_price=140.0, sl_price=80.0
    )

    await _seed_price(db, "bitcoin", 120.0)
    await evaluate_paper_trades(db, settings)
    row = await _fetch(db, trade_id)
    assert row[2] == pytest.approx(120.0)

    await _seed_price(db, "bitcoin", 105.0)
    await evaluate_paper_trades(db, settings)
    row = await _fetch(db, trade_id)
    assert row[2] == pytest.approx(120.0)  # unchanged

    await _seed_price(db, "bitcoin", 115.0)
    await evaluate_paper_trades(db, settings)
    row = await _fetch(db, trade_id)
    assert row[2] == pytest.approx(120.0)  # still unchanged


async def test_trailing_stop_activation_boundary_exact(db, tmp_path):
    """peak_pct exactly equal to activation_pct should arm the trailing stop."""
    settings = _settings_factory(
        tmp_path, PAPER_TRAILING_ACTIVATION_PCT=10.0, PAPER_TRAILING_DRAWDOWN_PCT=5.0
    )
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    trade_id = await _insert_trade(
        db, "bitcoin", 100.0, opened, tp_price=140.0, sl_price=80.0
    )
    await _set_peak(db, trade_id, 110.0, 10.0)  # exactly at activation
    # drawdown=5% -> threshold=110*0.95=104.5; current=104 is below
    await _seed_price(db, "bitcoin", 104.0)

    await evaluate_paper_trades(db, settings)

    row = await _fetch(db, trade_id)
    assert row[0] == "closed_trailing_stop"


async def test_per_trade_error_does_not_skip_others(db, tmp_path):
    """A row-level exception must not prevent other trades from evaluating.

    Simulates a corrupt row by writing a non-ISO opened_at string, which makes
    datetime.fromisoformat raise inside the per-trade body. Without per-trade
    try/except, this would skip TP/SL processing on every later trade in the
    batch.
    """
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)

    # Trade A: will trigger TP (price >= tp_price)
    trade_a = await _insert_trade(
        db, "bitcoin", 100.0, opened, tp_price=140.0, sl_price=80.0
    )
    # Trade B: opened_at corrupted — datetime.fromisoformat raises ValueError
    trade_b = await _insert_trade(
        db, "ethereum", 100.0, opened, tp_price=140.0, sl_price=80.0
    )
    await db._conn.execute(
        "UPDATE paper_trades SET opened_at = 'not-a-date' WHERE id = ?",
        (trade_b,),
    )
    await db._conn.commit()

    await _seed_price(db, "bitcoin", 145.0)
    await _seed_price(db, "ethereum", 105.0)

    await evaluate_paper_trades(db, settings)

    row_a = await _fetch(db, trade_a)
    row_b = await _fetch(db, trade_b)
    assert row_a[0] == "closed_tp"  # TP fired despite B being broken
    assert row_b[0] == "open"  # B skipped, not blown up


async def test_partial_tp_creates_long_hold_via_real_split(db, tmp_path):
    """Exercise the partial-TP -> long_hold creation path end-to-end.

    The child long_hold must start with peak_price/peak_pct = NULL (not inherit
    the parent's peak) so its own trailing stop cannot fire on the creation cycle.
    """
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    parent_id = await _insert_trade(
        db,
        "bitcoin",
        100.0,
        opened,
        signal_type="first_signal",
        tp_price=140.0,
        sl_price=80.0,
        tp_pct=40.0,
        sl_pct=20.0,
    )
    await _set_peak(db, parent_id, 120.0, 20.0)
    await _seed_price(db, "bitcoin", 145.0)  # trips TP

    await evaluate_paper_trades(db, settings)

    # Parent must be closed_tp
    parent = await _fetch(db, parent_id)
    assert parent[0] == "closed_tp"

    # A child long_hold row must have been created for the same token
    cur = await db._conn.execute(
        "SELECT id, signal_type, peak_price, peak_pct, status, would_be_live "
        "FROM paper_trades "
        "WHERE token_id = ? AND signal_type = 'long_hold'",
        ("bitcoin",),
    )
    child = await cur.fetchone()
    assert child is not None, "long_hold child not created by partial-TP split"
    assert child[1] == "long_hold"
    assert child[2] is None  # peak_price starts NULL
    assert child[3] is None  # peak_pct starts NULL
    assert child[4] == "open"
    # BL-060: rollover must stamp would_be_live=NULL (continuation, not a new
    # admission) so it is excluded from the A/B cohort at the SQL layer.
    # evaluator.py passes min_quant_score=0 for this reason.
    assert (
        child[5] is None
    ), f"long_hold rollover must stamp would_be_live=NULL; got {child[5]}"
