"""Regression tests for the resource-leak fixes batch (PR-G).

Three fixes:

1. dashboard.db._ro_db / _rw_db — connect() inside try block
2. scout.live.binance_adapter._reopen_tasks — gate-reopen tasks stored to
   prevent GC mid-flight
3. scout.main._counter_followup_tasks — counter-follow-up tasks tracked
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from dashboard import db as dashboard_db
from scout import main as scout_main
from scout.live import binance_adapter as ba_mod


def test_ro_db_acquires_connection_inside_try():
    """Source-level guard: any exception between connect() and yield must
    not leak the connection."""
    src = inspect.getsource(dashboard_db._ro_db)
    # The try: must come BEFORE the await aiosqlite.connect( call.
    try_pos = src.find("try:")
    connect_pos = src.find("await aiosqlite.connect(")
    assert try_pos != -1 and connect_pos != -1
    assert try_pos < connect_pos, (
        "dashboard.db._ro_db must acquire aiosqlite connection INSIDE the "
        "try block so failed row_factory assignment doesn't leak the "
        "connection. Current order has connect before try."
    )


def test_rw_db_acquires_connection_inside_try():
    src = inspect.getsource(dashboard_db._rw_db)
    try_pos = src.find("try:")
    connect_pos = src.find("await aiosqlite.connect(")
    assert try_pos != -1 and connect_pos != -1
    assert try_pos < connect_pos, (
        "dashboard.db._rw_db must acquire aiosqlite connection INSIDE the "
        "try block (same shape as _ro_db)."
    )


def test_binance_adapter_tracks_reopen_tasks():
    """Adapter must have a tracking set for gate-reopen tasks."""
    src = inspect.getsource(ba_mod.BinanceSpotAdapter)
    assert "_reopen_tasks" in src, (
        "BinanceSpotAdapter must store gate-reopen tasks in a tracking set "
        "(_reopen_tasks) so asyncio.create_task() cannot be GC'd mid-flight, "
        "leaving the rate-limit gate closed forever."
    )
    assert ".discard" in src or "_reopen_tasks.discard" in src, (
        "Tracking set must have a discard done-callback to prevent "
        "unbounded growth across many gate-reopen cycles."
    )


def test_counter_followup_tasks_tracked_in_main():
    src = inspect.getsource(scout_main)
    assert "_counter_followup_tasks" in src, (
        "scout/main.py must store counter-followup tasks in "
        "_counter_followup_tasks so asyncio.create_task() cannot be GC'd "
        "before the done-callback runs."
    )


@pytest.mark.asyncio
async def test_binance_reopen_task_completes_under_event_loop_pressure():
    """End-to-end: the gate-reopen task must run even under GC pressure.

    Constructs an adapter, manually triggers the reopen-task path, then
    awaits the discard callback by polling _reopen_tasks. Without the
    tracking-set fix, the task could be GC'd before set() fires.
    """
    from scout.config import Settings

    s = Settings(
        TELEGRAM_BOT_TOKEN="x",
        TELEGRAM_CHAT_ID="1",
        ANTHROPIC_API_KEY="x",
    )
    adapter = ba_mod.BinanceSpotAdapter(settings=s, db=None)
    try:
        # Force pause near-zero so test completes fast.
        adapter._RATE_LIMIT_PAUSE_SEC = 0.05
        adapter._rate_limit_gate.set()  # start open
        # Trigger the gate-close path (weight >= _WEIGHT_GATE_CLOSE=1140).
        await adapter._update_weight_governor(1140)
        # Tracking-set must have the task.
        assert len(adapter._reopen_tasks) == 1
        # Wait for the reopen-task to fire and the discard callback to run.
        for _ in range(50):
            await asyncio.sleep(0.02)
            if not adapter._reopen_tasks:
                break
        assert adapter._reopen_tasks == set(), (
            "_reopen_tasks should auto-clear via discard callback"
        )
        # And the gate should be reopened.
        assert adapter._rate_limit_gate.is_set(), "gate should be reopened"
    finally:
        await adapter._session.close()
