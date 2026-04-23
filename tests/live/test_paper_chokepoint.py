"""Task 19: PaperTrader optional LiveEngine chokepoint."""
import asyncio
from unittest.mock import AsyncMock

from scout.db import Database
from scout.trading.paper import PaperTrader


async def test_paper_trader_no_live_engine_unchanged(tmp_path):
    """Default (live_engine=None): execute_buy works identically."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    pt = PaperTrader()
    trade_id = await pt.execute_buy(
        db=db,
        token_id="c",
        symbol="S",
        name="N",
        chain="eth",
        signal_type="first_signal",
        signal_data={},
        current_price=1.0,
        amount_usd=100,
        tp_pct=40,
        sl_pct=20,
        signal_combo="",
        lead_time_vs_trending_min=None,
        lead_time_vs_trending_status=None,
        live_eligible_cap=20,
        min_quant_score=0,
    )
    assert trade_id is not None
    await db.close()


async def test_paper_trader_dispatches_to_live_engine_when_allowlisted(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    le = AsyncMock()
    le.is_eligible = lambda st: True  # NOTE: not AsyncMock — is_eligible is sync
    pt = PaperTrader(live_engine=le)
    await pt.execute_buy(
        db=db,
        token_id="c",
        symbol="S",
        name="N",
        chain="eth",
        signal_type="first_signal",
        signal_data={},
        current_price=1.0,
        amount_usd=100,
        tp_pct=40,
        sl_pct=20,
        signal_combo="",
        lead_time_vs_trending_min=None,
        lead_time_vs_trending_status=None,
        live_eligible_cap=20,
        min_quant_score=0,
    )
    # Task is scheduled — allow the event loop to run it.
    await asyncio.sleep(0)
    le.on_paper_trade_opened.assert_called_once()
    # Also verify the handoff carries the right identifiers.
    handoff = le.on_paper_trade_opened.call_args.args[0]
    assert handoff.signal_type == "first_signal"
    assert handoff.symbol == "S"
    assert handoff.coin_id == "c"
    await db.close()


async def test_paper_trader_skips_dispatch_when_not_eligible(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    le = AsyncMock()
    le.is_eligible = lambda st: False
    pt = PaperTrader(live_engine=le)
    await pt.execute_buy(
        db=db,
        token_id="c",
        symbol="S",
        name="N",
        chain="eth",
        signal_type="volume_spike",
        signal_data={},
        current_price=1.0,
        amount_usd=100,
        tp_pct=40,
        sl_pct=20,
        signal_combo="",
        lead_time_vs_trending_min=None,
        lead_time_vs_trending_status=None,
        live_eligible_cap=20,
        min_quant_score=0,
    )
    le.on_paper_trade_opened.assert_not_called()
    await db.close()
