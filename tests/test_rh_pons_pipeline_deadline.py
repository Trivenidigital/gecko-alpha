"""Observe-only RH polling must not stall ordinary candidate processing."""

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from scout.main import run_cycle
from tests.test_main_cryptopanic_integration import _mk_db


async def test_poll_deadline_cancels_poll_and_pipeline_continues(
    settings_factory, token_factory
):
    settings = settings_factory(
        RH_PONS_COLLECTOR_ENABLED=True, DEX_DISCOVERY_ENABLED=False
    )
    # Keep wall-clock regression fast without relaxing production validation.
    settings = settings.model_copy(update={"RH_PONS_POLL_TIMEOUT_SEC": 0.01})
    db = _mk_db()
    token = token_factory()
    cancelled = asyncio.Event()

    async def stuck_poll(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with ExitStack() as stack:
        for name in ("fetch_trending", "fetch_trending_pools"):
            stack.enter_context(
                patch("scout.main." + name, new=AsyncMock(return_value=[]))
            )
        stack.enter_context(
            patch(
                "scout.main._fetch_coingecko_lanes",
                new=AsyncMock(return_value=([], [], [], [], [], [])),
            )
        )
        stack.enter_context(
            patch("scout.main.rh_pons.poll_once", side_effect=stuck_poll)
        )
        aggregate = stack.enter_context(
            patch("scout.main.aggregate", return_value=[token])
        )
        stack.enter_context(
            patch("scout.main.enrich_holders", new=AsyncMock(return_value=token))
        )
        stack.enter_context(patch("scout.main.score", return_value=(75, [])))
        stack.enter_context(
            patch(
                "scout.main.evaluate", new=AsyncMock(return_value=(False, 40.0, token))
            )
        )
        async with asyncio.timeout(1):
            stats = await run_cycle(settings, db, AsyncMock(), dry_run=True)

    assert cancelled.is_set()
    aggregate.assert_called_once()
    assert stats["tokens_scanned"] == 1
    assert db.upsert_candidate.await_count >= 1
    db.upsert_ingest_watchdog_state.assert_not_awaited()


def test_poll_deadline_config_bounds(settings_factory):
    assert settings_factory().RH_PONS_POLL_TIMEOUT_SEC == 30
    for timeout in (0, -1, 301):
        with pytest.raises(ValidationError):
            settings_factory(RH_PONS_POLL_TIMEOUT_SEC=timeout)
