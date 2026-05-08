"""Adaptive-trail tests (2026-04-28 strategy review).

Closed-trade audit on 631 paper trades found that trades peaking at
+10–20% had a 67% win rate but **gave back ~10pp on average** before
expiring at slight loss. The 20%-wide BL-061 trail was tuned for
moonshot tolerance and didn't catch modest peakers fading.

Fix: tighter trail when peak_pct < threshold (default 8% trail, 20%
threshold). When peak ≥ threshold, the full PAPER_LADDER_TRAIL_PCT
applies. Post-moonshot, the moonshot trail always wins.

Invariant order in evaluator.py:
    1. Moonshot armed?              → MOONSHOT_TRAIL_DRAWDOWN_PCT
    2. Else peak < threshold?       → LADDER_TRAIL_PCT_LOW_PEAK
    3. Else                         → LADDER_TRAIL_PCT
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scout.db import Database
from scout.trading.evaluator import evaluate_paper_trades
from scout.trading.paper import PaperTrader


async def _seed_price(db: Database, token_id: str, price: float) -> None:
    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        (token_id, price, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()


async def _open_post_leg1(
    db: Database, trader: PaperTrader, *, token_id: str, settings, peak_price: float
) -> int:
    """Open trade, fire leg 1 (arms floor), set peak_price to a known value."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id=token_id,
        symbol=token_id.upper(),
        name=token_id.title(),
        chain="coingecko",
        signal_type="first_signal",
        signal_data={},
        current_price=1.00,
        amount_usd=100.0,
        tp_pct=200.0,  # high so leg 1/2 don't fire on the test prices
        sl_pct=99.0,  # high so SL doesn't fire
        slippage_bps=0,
        signal_combo="first_signal",
    )
    assert trade_id is not None
    # Fire leg 1 manually to arm the floor (so trail-stop is eligible).
    await trader.execute_partial_sell(
        db=db,
        trade_id=trade_id,
        leg=1,
        sell_qty_frac=settings.PAPER_LADDER_LEG_1_QTY_FRAC,
        current_price=1.05,  # +5% — small move just to arm leg 1
        slippage_bps=0,
    )
    # Stamp peak_price/peak_pct directly — bypasses evaluator's peak update.
    peak_pct = (peak_price - 1.00) / 1.00 * 100
    await db._conn.execute(
        "UPDATE paper_trades SET peak_price = ?, peak_pct = ? WHERE id = ?",
        (peak_price, peak_pct, trade_id),
    )
    await db._conn.commit()
    return trade_id


@pytest.mark.asyncio
async def test_low_peak_uses_tight_trail(tmp_path, settings_factory):
    """Trade with peak +15% (below 20% threshold). With LOW_PEAK trail at
    8%, a price drop of 9% from peak fires the trail. The full 20% trail
    would NOT have fired."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_LADDER_TRAIL_PCT=20.0,
        PAPER_LADDER_TRAIL_PCT_LOW_PEAK=8.0,
        PAPER_LADDER_LOW_PEAK_THRESHOLD_PCT=20.0,
        # Match the prod-intended config from #2 — Leg 1 at +10% so the
        # ladder actually engages on modest peakers. Trail check gate
        # (peak_pct >= LEG_1_PCT) requires this to be ≤ test peaks.
        PAPER_LADDER_LEG_1_PCT=10.0,
        # Bump leg 2 well above test scenarios so it doesn't fire and
        # short-circuit the trail check via `continue`.
        PAPER_LADDER_LEG_2_PCT=999.0,
    )
    trade_id = await _open_post_leg1(
        db, trader, token_id="lp1", settings=settings, peak_price=1.15  # +15%
    )
    # Drop to 1.04 (= -9.6% from peak 1.15). Tight trail (8%) fires;
    # full trail (20%) would not.
    await _seed_price(db, "lp1", 1.04)
    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?", (trade_id,)
    )
    status, reason = await cur.fetchone()
    assert reason == "trailing_stop", (
        f"low-peak trade with 9.6% drop from peak must fire tight trail; "
        f"got reason={reason!r}, status={status!r}"
    )
    assert status == "closed_trailing_stop"
    await db.close()


@pytest.mark.asyncio
async def test_high_peak_uses_full_trail(tmp_path, settings_factory):
    """Trade with peak +25% (above threshold). Tight low-peak trail does
    NOT apply; only the full 20% trail fires. A 9% drop from peak should
    NOT fire (full trail width is 20%)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_LADDER_TRAIL_PCT=20.0,
        PAPER_LADDER_TRAIL_PCT_LOW_PEAK=8.0,
        PAPER_LADDER_LOW_PEAK_THRESHOLD_PCT=20.0,
        # Match the prod-intended config from #2 — Leg 1 at +10% so the
        # ladder actually engages on modest peakers. Trail check gate
        # (peak_pct >= LEG_1_PCT) requires this to be ≤ test peaks.
        PAPER_LADDER_LEG_1_PCT=10.0,
        # Bump leg 2 well above test scenarios so it doesn't fire and
        # short-circuit the trail check via `continue`.
        PAPER_LADDER_LEG_2_PCT=999.0,
    )
    trade_id = await _open_post_leg1(
        db, trader, token_id="hp1", settings=settings, peak_price=1.25  # +25%
    )
    # Drop to 1.14 (= -8.8% from peak 1.25). Tight (8%) WOULD fire if
    # applied; full (20%) does not. Trade must remain open.
    await _seed_price(db, "hp1", 1.14)
    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    (status,) = await cur.fetchone()
    assert status == "open", (
        f"high-peak trade with -8.8% drop must NOT fire (full trail is 20%); "
        f"got status={status!r}"
    )
    await db.close()


@pytest.mark.asyncio
async def test_high_peak_full_trail_fires_on_wider_drop(tmp_path, settings_factory):
    """Sanity: high-peak trade with -22% drop from peak DOES fire."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_LADDER_TRAIL_PCT=20.0,
        PAPER_LADDER_TRAIL_PCT_LOW_PEAK=8.0,
        PAPER_LADDER_LOW_PEAK_THRESHOLD_PCT=20.0,
        # Match the prod-intended config from #2 — Leg 1 at +10% so the
        # ladder actually engages on modest peakers. Trail check gate
        # (peak_pct >= LEG_1_PCT) requires this to be ≤ test peaks.
        PAPER_LADDER_LEG_1_PCT=10.0,
        # Bump leg 2 well above test scenarios so it doesn't fire and
        # short-circuit the trail check via `continue`.
        PAPER_LADDER_LEG_2_PCT=999.0,
    )
    trade_id = await _open_post_leg1(
        db, trader, token_id="hp2", settings=settings, peak_price=2.00  # +100%
    )
    # Drop to 1.55 (= -22.5% from peak 2.00). Full trail (20%) fires.
    # Use a deep peak so trail price (1.60) stays above entry, otherwise
    # the floor-exit branch would fire first in the cascade.
    await _seed_price(db, "hp2", 1.55)
    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT exit_reason FROM paper_trades WHERE id = ?", (trade_id,)
    )
    (reason,) = await cur.fetchone()
    assert reason == "trailing_stop"
    await db.close()


@pytest.mark.asyncio
async def test_moonshot_overrides_low_peak_trail(tmp_path, settings_factory):
    """Once moonshot armed, the moonshot trail (e.g. 30%) wins regardless
    of whether peak crossed the low-peak threshold. Moonshot threshold is
    ALWAYS above low-peak threshold per the cross-field validator."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_LADDER_TRAIL_PCT=20.0,
        PAPER_LADDER_TRAIL_PCT_LOW_PEAK=8.0,
        PAPER_LADDER_LOW_PEAK_THRESHOLD_PCT=20.0,
        PAPER_MOONSHOT_ENABLED=True,
        PAPER_MOONSHOT_THRESHOLD_PCT=40.0,
        PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT=30.0,
    )
    trade_id = await _open_post_leg1(
        db,
        trader,
        token_id="ms1",
        settings=settings,
        peak_price=1.50,  # +50% past moonshot
    )
    # Drop to 1.10 (= -26.7% from peak 1.50). Moonshot trail (30%) does
    # NOT fire. Without moonshot, low-peak trail (8%) WOULD have fired.
    # Confirms moonshot overrides peak-tier picker.
    await _seed_price(db, "ms1", 1.10)
    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT status, moonshot_armed_at FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    status, armed_at = await cur.fetchone()
    assert armed_at is not None, "moonshot must arm at peak ≥ 40%"
    assert status == "open", (
        f"moonshot trail (30%) must override low-peak (8%) — trade "
        f"with -26.7% drop should still be open; got status={status!r}"
    )
    await db.close()


# ---------------------------------------------------------------------------
# Validator tests
# ---------------------------------------------------------------------------


def test_validator_rejects_low_peak_trail_geq_full_trail(settings_factory):
    """Inverted invariant: low_peak >= full would mean modest peakers
    have looser trail than runners. Reject at config load."""
    with pytest.raises(ValueError, match="LADDER_TRAIL_PCT_LOW_PEAK"):
        settings_factory(
            PAPER_LADDER_TRAIL_PCT=12.0,
            PAPER_LADDER_TRAIL_PCT_LOW_PEAK=15.0,  # > full
        )


def test_validator_rejects_zero_low_peak_trail(settings_factory):
    with pytest.raises(ValueError, match="LADDER_TRAIL_PCT_LOW_PEAK"):
        settings_factory(PAPER_LADDER_TRAIL_PCT_LOW_PEAK=0.0)


def test_validator_rejects_low_peak_threshold_at_or_above_moonshot(
    settings_factory,
):
    with pytest.raises(ValueError, match="LOW_PEAK_THRESHOLD_PCT"):
        settings_factory(
            PAPER_LADDER_LOW_PEAK_THRESHOLD_PCT=50.0,  # >= moonshot 40
            PAPER_MOONSHOT_ENABLED=True,
            PAPER_MOONSHOT_THRESHOLD_PCT=40.0,
        )


def test_validator_allows_low_peak_threshold_geq_moonshot_when_disabled(
    settings_factory,
):
    """When moonshot disabled, the cross-check is moot. No raise."""
    s = settings_factory(
        PAPER_LADDER_LOW_PEAK_THRESHOLD_PCT=50.0,
        PAPER_MOONSHOT_ENABLED=False,
    )
    assert s.PAPER_LADDER_LOW_PEAK_THRESHOLD_PCT == 50.0
