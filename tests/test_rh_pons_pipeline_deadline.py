"""RH capture runs as a dedicated worker, never inside the detection cycle."""

import asyncio
import re
import signal
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
import structlog
from pydantic import ValidationError

from scout.db import Database
from scout.main import run_cycle
from tests.test_main_cryptopanic_integration import _mk_db


async def test_run_cycle_makes_no_rh_call_even_when_enabled(
    settings_factory, token_factory
):
    settings = settings_factory(
        RH_PONS_COLLECTOR_ENABLED=True, DEX_DISCOVERY_ENABLED=False
    )
    db = _mk_db()
    token = token_factory()
    forbidden = AsyncMock(side_effect=AssertionError("RH polled inside run_cycle"))

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
        stack.enter_context(patch("scout.main.rh_pons.poll_once", new=forbidden))
        stack.enter_context(patch("scout.main.rh_pons.run_rh_pons_loop", new=forbidden))
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
        async with asyncio.timeout(5):
            stats = await run_cycle(settings, db, AsyncMock(), dry_run=True)

    forbidden.assert_not_awaited()
    aggregate.assert_called_once()
    assert stats["tokens_scanned"] == 1
    assert all(
        call.args[0] != "rh_pons"
        for call in db.upsert_ingest_watchdog_state.await_args_list
    )


def test_poll_deadline_config_bounds(settings_factory):
    assert settings_factory().RH_PONS_POLL_TIMEOUT_SEC == 30
    for timeout in (0, -1, 301):
        with pytest.raises(ValidationError):
            settings_factory(RH_PONS_POLL_TIMEOUT_SEC=timeout)


def test_main_spawns_rh_loop_only_behind_flag():
    source = re.sub(
        r"\s+",
        " ",
        (Path(__file__).resolve().parents[1] / "scout" / "main.py").read_text(
            encoding="utf-8"
        ),
    )
    matches = list(re.finditer(r"rh_pons\.(run_rh_pons_loop|poll_once)", source))
    assert [m.group(1) for m in matches] == ["run_rh_pons_loop"]
    window = source[max(0, matches[0].start() - 600) : matches[0].start()]
    assert "if settings.RH_PONS_COLLECTOR_ENABLED:" in window


@pytest.fixture
def isolated_main(monkeypatch, tmp_path):
    """Run the real scout.main.main() startup/shutdown with inert externals."""
    for key, value in {
        "TELEGRAM_BOT_TOKEN": "t",
        "TELEGRAM_CHAT_ID": "c",
        "ANTHROPIC_API_KEY": "k",
        "DB_PATH": str(tmp_path / "pipeline.db"),
        "TRADING_ENABLED": "false",
        "LIVE_MODE": "paper",
        "SCAN_INTERVAL_SECONDS": "60",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)  # no stray .env is read
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    yield monkeypatch
    for sig, handler in previous.items():
        signal.signal(sig, handler)
    structlog.reset_defaults()


@pytest.mark.parametrize("enabled", [True, False])
async def test_real_main_spawns_rh_worker_and_cancels_it_on_shutdown(
    isolated_main, enabled
):
    isolated_main.setenv("RH_PONS_COLLECTOR_ENABLED", "true" if enabled else "false")
    started = asyncio.Event()
    cancelled = asyncio.Event()
    received = {}

    async def fake_loop(session, db, settings, **kwargs):
        received.update(session=session, db=db, settings=settings)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def fake_cycle(settings, db, session, **kwargs):
        if enabled:
            await asyncio.wait_for(started.wait(), 10)
        return {"tokens_scanned": 0, "candidates_promoted": 0, "alerts_fired": 0}

    from scout import main as scout_main

    with ExitStack() as stack:
        stack.enter_context(patch.object(scout_main, "run_cycle", fake_cycle))
        stack.enter_context(
            patch.object(scout_main.rh_pons, "run_rh_pons_loop", fake_loop)
        )
        for name in ("_maybe_announce_tg_alerts", "_maybe_announce_m1_5c"):
            stack.enter_context(patch.object(scout_main, name, AsyncMock()))
        stack.enter_context(patch.object(scout_main, "poll_enrollments", AsyncMock()))
        stack.enter_context(
            patch.object(
                scout_main,
                "_run_feedback_schedulers",
                AsyncMock(return_value=("", "", "")),
            )
        )
        rc = await asyncio.wait_for(scout_main.main(["--dry-run", "--cycles", "1"]), 60)

    assert rc == 0
    if enabled:
        assert started.is_set()
        assert cancelled.is_set(), "RH worker not cancelled at shutdown"
        assert isinstance(received["db"], Database)
        assert isinstance(received["session"], aiohttp.ClientSession)
        assert received["settings"].RH_PONS_COLLECTOR_ENABLED
    else:
        assert not started.is_set()
