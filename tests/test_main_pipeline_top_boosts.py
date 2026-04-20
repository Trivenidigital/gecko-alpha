"""BL-051 integration: run_cycle invokes fetch_top_boosts and threads its
output through apply_boost_decorations."""

from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from scout.ingestion.dexscreener import BoostInfo


@pytest.mark.asyncio
async def test_run_cycle_invokes_fetch_top_boosts_and_wires_decorator(
    settings_factory, tmp_path
):
    from scout import main as main_module
    from scout.db import Database

    settings = settings_factory(DB_PATH=tmp_path / "test.db")
    db = Database(settings.DB_PATH)
    await db.initialize()

    with patch.object(main_module, "fetch_trending", new=AsyncMock(return_value=[])), \
         patch.object(main_module, "fetch_trending_pools", new=AsyncMock(return_value=[])), \
         patch.object(main_module, "cg_fetch_top_movers", new=AsyncMock(return_value=[])), \
         patch.object(main_module, "cg_fetch_trending", new=AsyncMock(return_value=[])), \
         patch.object(main_module, "cg_fetch_by_volume", new=AsyncMock(return_value=[])), \
         patch.object(
             main_module,
             "fetch_top_boosts",
             new=AsyncMock(return_value=[BoostInfo("ethereum", "0xfeed", 1500.0)]),
         ) as mock_top_boosts, \
         patch.object(
             main_module,
             "apply_boost_decorations",
             wraps=main_module.apply_boost_decorations,
         ) as mock_apply:
        async with aiohttp.ClientSession() as session:
            await main_module.run_cycle(settings, db, session, dry_run=True)

    await db.close()

    # The new poller ran exactly once this cycle.
    assert mock_top_boosts.await_count == 1
    # The decorator saw the poller's output exactly once.
    assert mock_apply.call_count == 1
    # The wired-through boost list must reach the decorator unchanged.
    call_args = mock_apply.call_args
    passed_boosts = (
        call_args.args[1]
        if len(call_args.args) > 1
        else call_args.kwargs["boosts"]
    )
    assert len(passed_boosts) == 1
    assert passed_boosts[0].address == "0xfeed"
