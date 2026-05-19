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


async def test_actionability_stamp_does_not_change_live_handoff(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    le = AsyncMock()
    le.is_eligible = lambda st: True
    pt = PaperTrader(live_engine=le)
    trade_id = await pt.execute_buy(
        db=db,
        token_id="non-actionable-live-handoff",
        symbol="NAL",
        name="Non Actionable Live Handoff",
        chain="coingecko",
        signal_type="losers_contrarian",
        signal_data={"mcap": 20_000_000},
        current_price=1.0,
        amount_usd=300,
        tp_pct=20,
        sl_pct=10,
        signal_combo="losers_contrarian",
        lead_time_vs_trending_min=None,
        lead_time_vs_trending_status=None,
    )
    await asyncio.sleep(0)
    assert trade_id is not None
    le.on_paper_trade_opened.assert_called_once()
    handoff = le.on_paper_trade_opened.call_args.args[0]
    assert handoff.id == trade_id
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
    )
    le.on_paper_trade_opened.assert_not_called()
    await db.close()
