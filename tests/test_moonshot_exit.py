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
