"""SIG-05 stop-loss fill realism (feat/stop-fill-realism), part 1.

Root cause: the evaluator books a stop-triggered close at ``current_price``
(evaluator.py:732/1006). Between 30-min eval cycles a token can gap far
through the stop, so a *fresh* snapshot at -40% books the "fill" at -40%
even though the stop sat at -10%. Measured drain: stops filled -28.1% avg
on a -10% config.

Fix (flag-gated, fail-closed): when ``PAPER_STOP_FILL_SLIPPAGE_MODEL`` is on,
a stop close books at ``max(current_price, sl_price*(1 - PAPER_STOP_GAP_BPS/
10000))`` — near the stop with a bounded gap allowance instead of an
arbitrarily-deep crash snapshot. The raw observed price is recorded in the
exit provenance detail (``exit_provenance='stop_gap_model'`` + a
``stop_fill_slippage_model`` decision event) so realized-vs-modeled stays
auditable. Default off preserves the exact pre-existing fill.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scout.db import Database
from scout.trading.evaluator import evaluate_paper_trades
from scout.trading.paper import PaperTrader


async def _seed_price(db: Database, token_id: str, price: float) -> None:
    """Seed a FRESH price_cache row (age 0) — the SIG-05 scenario is a
    fresh-but-crashed snapshot, not a stale >1h row."""
    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        (token_id, price, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()


async def _open_trade(
    db: Database,
    trader: PaperTrader,
    *,
    token_id: str = "tok",
    entry: float = 1.00,
    sl_pct: float = 10.0,
) -> int:
    trade_id = await trader.execute_buy(
        db=db,
        token_id=token_id,
        symbol=token_id.upper(),
        name=token_id.title(),
        chain="coingecko",
        signal_type="first_signal",
        signal_data={},
        current_price=entry,
        amount_usd=300.0,
        tp_pct=40.0,
        sl_pct=sl_pct,
        slippage_bps=0,
        signal_combo="first_signal",
    )
    assert trade_id is not None
    return trade_id


async def _closed_row(db: Database, trade_id: int):
    cur = await db._conn.execute(
        "SELECT status, exit_price, exit_reason, exit_provenance, pnl_pct "
        "FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    return await cur.fetchone()


# --- part 1: stop-fill slippage model ---------------------------------------


@pytest.mark.asyncio
async def test_stop_fill_flag_off_books_crash_price(tmp_path, settings_factory):
    """Default off: the stop still books at the deep crash snapshot (the
    pre-fix behavior this feature exists to correct — pinned so the flag
    genuinely gates the change)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_SLIPPAGE_BPS=0,
        PAPER_STOP_FILL_SLIPPAGE_MODEL=False,
    )
    trade_id = await _open_trade(db, trader, sl_pct=10.0)  # sl_price = 0.90
    await _seed_price(db, "tok", 0.60)  # -40%, far through the stop

    await evaluate_paper_trades(db, settings)

    status, exit_price, reason, prov, pnl_pct = await _closed_row(db, trade_id)
    assert status == "closed_sl"
    assert reason == "stop_loss"
    assert exit_price == pytest.approx(0.60)
    assert prov == "market"
    assert pnl_pct == pytest.approx(-40.0, abs=0.1)
    await db.close()


@pytest.mark.asyncio
async def test_stop_fill_flag_on_books_gap_bounded(tmp_path, settings_factory):
    """Flag on + deep crash: fill is clamped to sl_price*(1-gap) instead of
    the crash snapshot, and the provenance marks the modeled fill."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_SLIPPAGE_BPS=0,
        PAPER_STOP_FILL_SLIPPAGE_MODEL=True,
        PAPER_STOP_GAP_BPS=300,
    )
    trade_id = await _open_trade(db, trader, sl_pct=10.0)  # sl_price = 0.90
    await _seed_price(db, "tok", 0.60)  # -40% crash

    await evaluate_paper_trades(db, settings)

    status, exit_price, reason, prov, pnl_pct = await _closed_row(db, trade_id)
    # gap_floor = 0.90 * (1 - 300/10000) = 0.873
    assert status == "closed_sl"
    assert reason == "stop_loss"
    assert exit_price == pytest.approx(0.873)
    assert prov == "stop_gap_model"
    assert pnl_pct == pytest.approx(-12.7, abs=0.1)
    await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_price", [0.60, 0.40, 0.10])
async def test_stop_fill_gap_bound_respected_regardless_of_depth(
    tmp_path, settings_factory, crash_price
):
    """The bound is a hard floor: no matter how deep the crash snapshot, the
    modeled fill never books worse than sl_price*(1-gap)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_SLIPPAGE_BPS=0,
        PAPER_STOP_FILL_SLIPPAGE_MODEL=True,
        PAPER_STOP_GAP_BPS=300,
    )
    trade_id = await _open_trade(db, trader, sl_pct=10.0)  # sl_price = 0.90
    await _seed_price(db, "tok", crash_price)

    await evaluate_paper_trades(db, settings)

    _, exit_price, _, prov, _ = await _closed_row(db, trade_id)
    assert exit_price == pytest.approx(0.873)  # clamped to gap_floor
    assert prov == "stop_gap_model"
    await db.close()


@pytest.mark.asyncio
async def test_stop_fill_within_gap_uses_observed_price(tmp_path, settings_factory):
    """When the observed price is only slightly through the stop (still above
    the gap floor), the model uses the real observed price and does NOT stamp
    the modeled provenance — the gap only bounds, never improves, a fill."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_SLIPPAGE_BPS=0,
        PAPER_STOP_FILL_SLIPPAGE_MODEL=True,
        PAPER_STOP_GAP_BPS=300,
    )
    trade_id = await _open_trade(db, trader, sl_pct=10.0)  # sl_price = 0.90
    # 0.88 is below the stop (0.90) but above the gap floor (0.873).
    await _seed_price(db, "tok", 0.88)

    await evaluate_paper_trades(db, settings)

    status, exit_price, reason, prov, _ = await _closed_row(db, trade_id)
    assert status == "closed_sl"
    assert reason == "stop_loss"
    assert exit_price == pytest.approx(0.88)
    assert prov == "market"
    await db.close()


@pytest.mark.asyncio
async def test_stop_fill_records_raw_observed_price_for_audit(
    tmp_path, settings_factory
):
    """The raw observed (crash) price is recorded in a decision event so
    realized-vs-modeled remains auditable."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_SLIPPAGE_BPS=0,
        PAPER_STOP_FILL_SLIPPAGE_MODEL=True,
        PAPER_STOP_GAP_BPS=300,
    )
    trade_id = await _open_trade(db, trader, sl_pct=10.0)
    await _seed_price(db, "tok", 0.60)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT event_data FROM trade_decision_events "
        "WHERE paper_trade_id = ? AND reason = 'stop_fill_slippage_model'",
        (trade_id,),
    )
    row = await cur.fetchone()
    assert row is not None, "expected a stop_fill_slippage_model decision event"
    payload = json.loads(row[0])
    assert payload["raw_observed_price"] == pytest.approx(0.60)
    assert payload["modeled_fill_price"] == pytest.approx(0.873)
    assert payload["sl_price"] == pytest.approx(0.90)
    assert payload["gap_bps"] == 300
    await db.close()


@pytest.mark.asyncio
async def test_stop_fill_no_audit_event_when_within_gap(tmp_path, settings_factory):
    """No modeled-fill event is recorded when the model does not adjust the
    fill (observed price above the gap floor)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_SLIPPAGE_BPS=0,
        PAPER_STOP_FILL_SLIPPAGE_MODEL=True,
        PAPER_STOP_GAP_BPS=300,
    )
    trade_id = await _open_trade(db, trader, sl_pct=10.0)
    await _seed_price(db, "tok", 0.88)  # within the gap

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM trade_decision_events "
        "WHERE paper_trade_id = ? AND reason = 'stop_fill_slippage_model'",
        (trade_id,),
    )
    (count,) = await cur.fetchone()
    assert count == 0
    await db.close()


# --- regression: only stop_loss closes are re-priced ------------------------


async def _open_armed_runner(
    db: Database, trader: PaperTrader, *, token_id: str, settings
) -> int:
    """Open a trade and fire leg 1 so the floor is armed (post-leg-1 trail
    eligibility). Mirrors tests/test_moonshot_exit.py."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id=token_id,
        symbol=token_id.upper(),
        name=token_id.title(),
        chain="coingecko",
        signal_type="first_signal",
        signal_data={},
        current_price=1.00,
        amount_usd=300.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=0,
        signal_combo="first_signal",
    )
    assert trade_id is not None
    await trader.execute_partial_sell(
        db=db,
        trade_id=trade_id,
        leg=1,
        sell_qty_frac=settings.PAPER_LADDER_LEG_1_QTY_FRAC,
        current_price=1.30,
        slippage_bps=0,
    )
    return trade_id


@pytest.mark.asyncio
async def test_trailing_stop_close_untouched_by_stop_model(tmp_path, settings_factory):
    """Regression: a trailing_stop close is NOT re-priced even with the stop
    model on — the model only rewrites close_reason == 'stop_loss'."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_SLIPPAGE_BPS=0,
        PAPER_MOONSHOT_ENABLED=False,
        PAPER_LADDER_TRAIL_PCT=12.0,
        PAPER_STOP_FILL_SLIPPAGE_MODEL=True,
        PAPER_STOP_GAP_BPS=300,
    )
    trade_id = await _open_armed_runner(db, trader, token_id="tr", settings=settings)
    await _seed_price(db, "tr", 1.50)  # peak +50%
    await evaluate_paper_trades(db, settings)
    # -15% off peak (1.275) — past the 12% baseline trail, above the entry
    # floor (1.00), so trailing_stop (not floor) fires.
    await _seed_price(db, "tr", 1.275)
    await evaluate_paper_trades(db, settings)

    status, exit_price, reason, prov, _ = await _closed_row(db, trade_id)
    assert status == "closed_trailing_stop"
    assert reason == "trailing_stop"
    assert exit_price == pytest.approx(1.275)  # observed price, NOT gap-bounded
    assert prov == "market"
    await db.close()


@pytest.mark.asyncio
async def test_peak_fade_close_untouched_by_stop_model(tmp_path, settings_factory):
    """Regression: a peak_fade close books at the observed price with the
    stop model on."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_SLIPPAGE_BPS=0,
        PEAK_FADE_ENABLED=True,
        PEAK_FADE_MIN_PEAK_PCT=10.0,
        PEAK_FADE_RETRACE_RATIO=0.7,
        PAPER_STOP_FILL_SLIPPAGE_MODEL=True,
        PAPER_STOP_GAP_BPS=300,
    )
    trade_id = await _open_trade(db, trader, token_id="pf", sl_pct=10.0)
    # Peak +20% with both 6h/24h checkpoints faded < 0.7*peak (14%). Floor
    # never armed (no leg fired), so the ladder cascade falls through to
    # peak_fade.
    await db._conn.execute(
        "UPDATE paper_trades SET peak_price = ?, peak_pct = ?, "
        "checkpoint_6h_pct = ?, checkpoint_24h_pct = ? WHERE id = ?",
        (1.20, 20.0, 5.0, 5.0, trade_id),
    )
    await db._conn.commit()
    # 0.95 is above the stop (0.90) so SL does not fire; below leg 1 (+25%).
    await _seed_price(db, "pf", 0.95)

    await evaluate_paper_trades(db, settings)

    status, exit_price, reason, prov, _ = await _closed_row(db, trade_id)
    assert status == "closed_peak_fade"
    assert reason == "peak_fade"
    assert exit_price == pytest.approx(0.95)  # observed price, NOT gap-bounded
    assert prov == "market"
    await db.close()
