"""Per plan Task 18: happy-path tests for the three live loops.

The three loops under test:
 * shadow_evaluator_loop - periodic close-check every TRADE_EVAL_INTERVAL_SEC
 * override_staleness_loop - daily UTC 12:00 audit of venue_overrides
 * live_metrics_rollup_loop - daily UTC 00:30 summary of live_metrics_daily

Each loop uses a ``_sleep_until`` / ``asyncio.sleep`` hook; tests monkeypatch
those to zero so the inner work runs once before we cancel the task.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from scout.config import Settings
from scout.db import Database
from scout.live.config import LiveConfig
from scout.live.kill_switch import KillSwitch
from scout.live.loops import (
    compute_next_run_utc,
    live_metrics_rollup_loop,
    override_staleness_loop,
    shadow_evaluator_loop,
)
from scout.live.metrics import inc


def _make_settings(**overrides) -> Settings:
    base = dict(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        LIVE_MODE="shadow",
        LIVE_TP_PCT=Decimal("20"),
        LIVE_SL_PCT=Decimal("10"),
        LIVE_MAX_DURATION_HOURS=24,
        LIVE_TRADE_AMOUNT_USD=Decimal("100"),
        LIVE_DAILY_LOSS_CAP_USD=Decimal("50"),
    )
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# compute_next_run_utc
# ---------------------------------------------------------------------------


def test_compute_next_run_utc_wraps_past_target():
    now = datetime(2026, 4, 23, 13, 0, tzinfo=timezone.utc)
    nxt = compute_next_run_utc(now, target_hour=12, target_minute=0)
    assert nxt == datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)


def test_compute_next_run_utc_uses_today_for_future_target():
    now = datetime(2026, 4, 23, 10, 0, tzinfo=timezone.utc)
    nxt = compute_next_run_utc(now, target_hour=12, target_minute=0)
    assert nxt == datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc)


def test_compute_next_run_utc_same_time_wraps():
    """When now == target, next run is tomorrow (target <= now)."""
    now = datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc)
    nxt = compute_next_run_utc(now, target_hour=12, target_minute=0)
    assert nxt == datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# shadow_evaluator_loop
# ---------------------------------------------------------------------------


async def test_shadow_evaluator_loop_runs_one_iteration_then_cancels(
    tmp_path, monkeypatch
):
    """Loop calls the scanner once, sleeps, then we cancel."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        settings = _make_settings()
        adapter = MagicMock()
        config = LiveConfig(settings)
        ks = KillSwitch(db)

        call_count = {"n": 0}

        async def fake_scan(**kwargs):
            call_count["n"] += 1
            return 0

        monkeypatch.setattr("scout.live.loops.evaluate_open_shadow_trades", fake_scan)

        task = asyncio.create_task(
            shadow_evaluator_loop(
                db=db,
                adapter=adapter,
                config=config,
                ks=ks,
                settings=settings,
                interval_sec=0.01,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert call_count["n"] >= 1
    finally:
        await db.close()


async def test_shadow_evaluator_loop_swallows_exceptions(tmp_path, monkeypatch):
    """Loop continues after an exception - does not crash."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        settings = _make_settings()
        adapter = MagicMock()
        config = LiveConfig(settings)
        ks = KillSwitch(db)

        call_count = {"n": 0}

        async def failing_scan(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("boom")
            return 0

        monkeypatch.setattr(
            "scout.live.loops.evaluate_open_shadow_trades", failing_scan
        )

        task = asyncio.create_task(
            shadow_evaluator_loop(
                db=db,
                adapter=adapter,
                config=config,
                ks=ks,
                settings=settings,
                interval_sec=0.01,
            )
        )
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # First iteration raised; loop must have reached at least a 2nd call.
        assert call_count["n"] >= 2
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# live_metrics_rollup_loop
# ---------------------------------------------------------------------------


async def test_live_metrics_rollup_loop_posts_summary_when_fired(tmp_path, monkeypatch):
    """Inner work runs once - we patch _sleep_until to return immediately so
    the daily wait collapses to zero."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        # Seed a metric row so the summary is non-empty.
        # Spec §10.3: engine emits shadow_rejects_<reason>; use a
        # production-realistic key so tests assert real behavior.
        await inc(db, "shadow_rejects_exposure_cap", by=3)

        settings = _make_settings()
        session = MagicMock()  # aiohttp-like, not actually hit

        send_mock = AsyncMock()
        monkeypatch.setattr("scout.live.loops.send_telegram_message", send_mock)
        monkeypatch.setattr(
            "scout.live.loops._sleep_until", AsyncMock(return_value=None)
        )

        task = asyncio.create_task(
            live_metrics_rollup_loop(db=db, session=session, settings=settings)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # At least one send call happened, with the metric in the payload.
        assert send_mock.await_count >= 1
        text_arg = send_mock.await_args_list[0].args[0]
        assert "shadow_rejects_exposure_cap" in text_arg
    finally:
        await db.close()


async def test_live_metrics_rollup_loop_tolerates_empty_metrics(tmp_path, monkeypatch):
    """Empty live_metrics_daily must never crash the loop."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        settings = _make_settings()
        session = MagicMock()
        send_mock = AsyncMock()
        monkeypatch.setattr("scout.live.loops.send_telegram_message", send_mock)
        monkeypatch.setattr(
            "scout.live.loops._sleep_until", AsyncMock(return_value=None)
        )

        task = asyncio.create_task(
            live_metrics_rollup_loop(db=db, session=session, settings=settings)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Should not have crashed - cancellation raised as expected.
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# override_staleness_loop
# ---------------------------------------------------------------------------


async def test_override_staleness_loop_probes_overrides_when_fired(
    tmp_path, monkeypatch
):
    """Collapse the sleep so the inner probe runs once."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        # Seed an active override.
        now_iso = datetime.now(timezone.utc).isoformat()
        assert db._conn is not None
        await db._conn.execute(
            "INSERT INTO venue_overrides "
            "(symbol, venue, pair, note, disabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("FOO", "binance", "FOOUSDT", "op-note", 0, now_iso, now_iso),
        )
        await db._conn.commit()

        settings = _make_settings()
        adapter = MagicMock()
        # Return None - pair is NOT listed, so it will be flagged stale.
        adapter.fetch_exchange_info_row = AsyncMock(return_value=None)

        monkeypatch.setattr(
            "scout.live.loops._sleep_until", AsyncMock(return_value=None)
        )

        task = asyncio.create_task(
            override_staleness_loop(adapter=adapter, db=db, settings=settings)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Adapter was probed for the active row.
        assert adapter.fetch_exchange_info_row.await_count >= 1
    finally:
        await db.close()


async def test_override_staleness_loop_skips_disabled(tmp_path, monkeypatch):
    """A disabled override is not probed."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        assert db._conn is not None
        await db._conn.execute(
            "INSERT INTO venue_overrides "
            "(symbol, venue, pair, note, disabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("BAR", "binance", "BARUSDT", "disabled-row", 1, now_iso, now_iso),
        )
        await db._conn.commit()

        settings = _make_settings()
        adapter = MagicMock()
        adapter.fetch_exchange_info_row = AsyncMock(return_value={"symbol": "BARUSDT"})

        monkeypatch.setattr(
            "scout.live.loops._sleep_until", AsyncMock(return_value=None)
        )

        task = asyncio.create_task(
            override_staleness_loop(adapter=adapter, db=db, settings=settings)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Disabled row was never probed.
        assert adapter.fetch_exchange_info_row.await_count == 0
    finally:
        await db.close()
