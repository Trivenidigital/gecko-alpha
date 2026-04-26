"""BL-063 moonshot exit-path integration tests.

Verifies the evaluator wiring:
- arm fires when peak_pct >= MOONSHOT_THRESHOLD_PCT and flag is on
- trailing-stop uses widened drawdown when armed
- close status is 'closed_moonshot_trail' on trail exit when armed
- non-armed and disabled-flag paths preserve existing behaviour
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


async def _open_armed_runner(
    db: Database, trader: PaperTrader, *, token_id: str, settings
) -> int:
    """Open a trade, fire leg 1 (floor armed), and seed peak_pct above threshold.

    Returns the trade_id, ready for evaluator to consider trailing/moonshot.
    """
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
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=0,
        signal_combo="first_signal",
    )
    assert trade_id is not None
    # Fire leg 1 manually so floor is armed (post-leg-1 trail eligibility).
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
async def test_moonshot_arms_at_threshold_when_enabled(tmp_path, settings_factory):
    """When peak_pct >= threshold and flag on, evaluator arms the moonshot."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_MOONSHOT_ENABLED=True,
        PAPER_MOONSHOT_THRESHOLD_PCT=40.0,
        PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT=30.0,
        PAPER_LADDER_TRAIL_PCT=12.0,
    )
    trade_id = await _open_armed_runner(db, trader, token_id="m1", settings=settings)
    # Push price to +50% — peak_pct will be updated to ~50 inside the evaluator
    # which triggers the arm path.
    await _seed_price(db, "m1", 1.50)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT moonshot_armed_at, original_trail_drawdown_pct, status "
        "FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    armed_at, original_trail, status = await cur.fetchone()
    assert armed_at is not None
    assert original_trail == pytest.approx(12.0)
    # The trade is still open — armed, not closed (price hasn't trailed off
    # the peak yet).
    assert status == "open"
    await db.close()


@pytest.mark.asyncio
async def test_moonshot_disabled_does_not_arm(tmp_path, settings_factory):
    """Flag off => moonshot never arms even past the threshold."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(PAPER_MOONSHOT_ENABLED=False)
    trade_id = await _open_armed_runner(db, trader, token_id="m2", settings=settings)
    await _seed_price(db, "m2", 1.50)  # +50%

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT moonshot_armed_at FROM paper_trades WHERE id = ?", (trade_id,)
    )
    (armed_at,) = await cur.fetchone()
    assert armed_at is None
    await db.close()


@pytest.mark.asyncio
async def test_moonshot_trail_widens_after_arm(tmp_path, settings_factory):
    """Once armed, a -15% drawdown from peak does NOT trigger trail
    (would have under default 12% trail), but a -35% drawdown does."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_MOONSHOT_ENABLED=True,
        PAPER_MOONSHOT_THRESHOLD_PCT=40.0,
        PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT=30.0,
        PAPER_LADDER_TRAIL_PCT=12.0,
    )
    trade_id = await _open_armed_runner(db, trader, token_id="m3", settings=settings)

    # Push to +50% to arm; peak becomes 1.50.
    await _seed_price(db, "m3", 1.50)
    await evaluate_paper_trades(db, settings)
    # Sanity-check setup so failures here surface as setup bugs, not as
    # spurious trail-formula assertions later.
    cur = await db._conn.execute(
        "SELECT floor_armed, peak_price FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    floor_armed, peak_price = await cur.fetchone()
    assert floor_armed == 1, "leg 1 should have armed the floor"
    assert peak_price == pytest.approx(1.50, rel=1e-6)

    # Drawdown of -15% from peak: 1.50 * 0.85 = 1.275 — past the 12% baseline
    # trail (1.32) but well within the 30% moonshot trail (1.05). Should NOT close.
    await _seed_price(db, "m3", 1.275)
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    (status,) = await cur.fetchone()
    assert (
        status == "open"
    ), "Moonshot trail should be wider — 15% drawdown shouldn't close"

    # Drawdown past the 30% moonshot trail (1.50 * 0.70 = 1.05). Price 1.04
    # is below the trail threshold but ABOVE the entry-price floor (1.00),
    # so the moonshot-trail branch fires before the BL-061 floor exit.
    await _seed_price(db, "m3", 1.04)
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?", (trade_id,)
    )
    status, exit_reason = await cur.fetchone()
    assert status == "closed_moonshot_trail"
    assert exit_reason == "trailing_stop"
    await db.close()


@pytest.mark.asyncio
async def test_non_armed_trail_uses_baseline(tmp_path, settings_factory):
    """When moonshot is disabled, the trail uses PAPER_LADDER_TRAIL_PCT
    and closes as 'closed_trailing_stop' (regression gate for BL-061)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_MOONSHOT_ENABLED=False,
        PAPER_LADDER_TRAIL_PCT=12.0,
    )
    trade_id = await _open_armed_runner(db, trader, token_id="m4", settings=settings)
    await _seed_price(db, "m4", 1.50)  # +50% peak
    await evaluate_paper_trades(db, settings)
    # -15% from peak — past 12% baseline trail, should close.
    await _seed_price(db, "m4", 1.275)
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?", (trade_id,)
    )
    status, exit_reason = await cur.fetchone()
    assert status == "closed_trailing_stop"
    assert exit_reason == "trailing_stop"
    await db.close()


@pytest.mark.asyncio
async def test_pre_bl061_trade_never_arms(tmp_path, settings_factory):
    """A trade with created_at BEFORE the BL-061 cutover must not arm
    even with PAPER_MOONSHOT_ENABLED=True. BL-060 mid-flight migration
    lesson: A/B is scoped to opened_at >= cutover_ts."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Push the bl061 cutover into the future so the inserted row is "pre-cutover".
    future_iso = "2099-01-01T00:00:00+00:00"
    await db._conn.execute(
        "UPDATE paper_migrations SET cutover_ts = ? WHERE name = 'bl061_ladder'",
        (future_iso,),
    )
    await db._conn.commit()

    trader = PaperTrader()
    settings = settings_factory(PAPER_MOONSHOT_ENABLED=True)
    trade_id = await trader.execute_buy(
        db=db,
        token_id="pre",
        symbol="PRE",
        name="Pre",
        chain="coingecko",
        signal_type="first_signal",
        signal_data={},
        current_price=1.00,
        amount_usd=100.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=0,
        signal_combo="first_signal",
    )
    await _seed_price(db, "pre", 1.50)  # +50% peak — past the moonshot threshold

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT moonshot_armed_at, status FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    armed_at, status = await cur.fetchone()
    assert armed_at is None, "pre-cutover trades must skip the BL-063 path entirely"
    # Status will be set by the legacy cascade (TP at +50% > 20% TP threshold)
    # but the assertion we care about is the absence of armed_at.
    await db.close()


@pytest.mark.asyncio
async def test_floor_exit_pre_empts_moonshot_trail(tmp_path, settings_factory):
    """When price drops below entry while moonshot is armed, the BL-061
    floor exit fires first — NOT closed_moonshot_trail. Locks in the
    elif-chain ordering in the evaluator."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_MOONSHOT_ENABLED=True,
        PAPER_MOONSHOT_THRESHOLD_PCT=40.0,
        PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT=30.0,
    )
    trade_id = await _open_armed_runner(db, trader, token_id="fp", settings=settings)

    # Arm the moonshot via a +50% pass
    await _seed_price(db, "fp", 1.50)
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT moonshot_armed_at FROM paper_trades WHERE id = ?", (trade_id,)
    )
    (armed_at,) = await cur.fetchone()
    assert armed_at is not None

    # Hard drop below entry — floor exit must win over moonshot trail.
    await _seed_price(db, "fp", 0.95)
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?", (trade_id,)
    )
    status, exit_reason = await cur.fetchone()
    assert status == "closed_floor"
    assert exit_reason == "floor"
    await db.close()


@pytest.mark.asyncio
async def test_moonshot_arm_and_leg_2_same_tick(tmp_path, settings_factory):
    """When peak_pct >= max(LEG_2_PCT, MOONSHOT_THRESHOLD) on a single tick,
    moonshot arms AND leg 2 fires (in that order, with leg 2 hitting `continue`
    before the trail check)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_MOONSHOT_ENABLED=True,
        PAPER_MOONSHOT_THRESHOLD_PCT=40.0,
        PAPER_LADDER_LEG_1_PCT=25.0,
        PAPER_LADDER_LEG_2_PCT=50.0,
    )
    trade_id = await _open_armed_runner(db, trader, token_id="al", settings=settings)
    # +55% covers both LEG_2 (>=50) and MOONSHOT_THRESHOLD (>=40)
    await _seed_price(db, "al", 1.55)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT moonshot_armed_at, leg_2_filled_at FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    armed_at, leg_2_filled = await cur.fetchone()
    assert armed_at is not None, "arm fires before the leg 2 continue"
    assert leg_2_filled is not None, "leg 2 still fires on the same pass"
    await db.close()


@pytest.mark.asyncio
async def test_moonshot_trail_wins_over_peak_fade(tmp_path, settings_factory):
    """When both the moonshot trail and peak-fade conditions could fire on
    the same pass, the trail wins (close_reason is set first in the cascade)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_MOONSHOT_ENABLED=True,
        PAPER_MOONSHOT_THRESHOLD_PCT=40.0,
        PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT=30.0,
        PEAK_FADE_ENABLED=True,
        PEAK_FADE_MIN_PEAK_PCT=10.0,
        PEAK_FADE_RETRACE_RATIO=0.7,
    )
    trade_id = await _open_armed_runner(db, trader, token_id="pf", settings=settings)

    # Arm at +50%
    await _seed_price(db, "pf", 1.50)
    await evaluate_paper_trades(db, settings)
    # Pre-fill 6h + 24h checkpoints below the peak-fade retrace threshold so
    # peak-fade WOULD be eligible to fire on the next pass.
    await db._conn.execute(
        "UPDATE paper_trades SET checkpoint_6h_pct = ?, checkpoint_24h_pct = ? "
        "WHERE id = ?",
        (5.0, 5.0, trade_id),  # both below 50 * 0.7 = 35
    )
    await db._conn.commit()

    # Drop below the moonshot trail (1.50 * 0.7 = 1.05). 1.04 is below trail
    # AND above floor (1.00). Both moonshot trail and peak-fade would fire,
    # but trail is checked first and sets close_reason.
    await _seed_price(db, "pf", 1.04)
    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    (status,) = await cur.fetchone()
    assert (
        status == "closed_moonshot_trail"
    ), "moonshot trail must fire before peak-fade in the cascade"
    await db.close()
