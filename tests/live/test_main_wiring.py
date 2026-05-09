"""Safety + drain tests for the scout/main.py live-mode startup guard (spec §1.3)."""

import asyncio

import pytest
import structlog

from scout.db import Database
from scout.trading.paper import PaperTrader


@pytest.fixture(autouse=True)
def _restore_structlog_after_main_invocation():
    """scout.main() calls structlog.configure() with JSONRenderer. That state
    is global; without reset, downstream tests that assert on console-format
    log output (`key=value`) fail because structlog now emits JSON."""
    yield
    structlog.reset_defaults()


async def test_live_mode_live_raises_runtime_error_without_master_kill(
    monkeypatch, tmp_path
):
    """M1.5a (PR #86): LIVE_MODE=live without LIVE_TRADING_ENABLED=True
    raises RuntimeError at startup (Layer 1 master-kill is the FIRST
    boot-time guard). Replaces M1's expectation of NotImplementedError
    on balance_gate (M1.5a wired balance_gate, so that NotImplementedError
    is gone)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("LIVE_MODE", "live")
    monkeypatch.setenv("BINANCE_API_KEY", "fake-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "fake-secret")
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "guard.db"))

    from scout.main import main as scout_main

    with pytest.raises(RuntimeError, match="LIVE_TRADING_ENABLED"):
        await scout_main(["--dry-run", "--cycles", "1"])


async def test_live_mode_live_without_credentials_raises_runtime_error(
    monkeypatch, tmp_path
):
    """LIVE_MODE=live + LIVE_TRADING_ENABLED=True without creds raises
    RuntimeError matching BINANCE_API_KEY — credential check fires after
    Layer 1 master kill."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("LIVE_MODE", "live")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "True")
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "guard.db"))

    from scout.main import main as scout_main

    with pytest.raises(RuntimeError, match="BINANCE_API_KEY"):
        await scout_main(["--dry-run", "--cycles", "1"])


async def test_live_mode_live_without_signed_flag_raises_runtime_error(
    monkeypatch, tmp_path
):
    """M1.5a (PR #86 V3-I2 fold): LIVE_MODE=live + LIVE_TRADING_ENABLED=True
    + creds present but LIVE_USE_REAL_SIGNED_REQUESTS=False (default) →
    RuntimeError naming the flag. Prevents operator from booting cleanly
    + every signal silently hitting 'live_signed_disabled'."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("LIVE_MODE", "live")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "True")
    monkeypatch.setenv("BINANCE_API_KEY", "fake-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "fake-secret")
    monkeypatch.delenv("LIVE_USE_REAL_SIGNED_REQUESTS", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "guard.db"))

    from scout.main import main as scout_main

    with pytest.raises(RuntimeError, match="LIVE_USE_REAL_SIGNED_REQUESTS"):
        await scout_main(["--dry-run", "--cycles", "1"])


async def test_shutdown_drains_pending_live_tasks(tmp_path):
    """Spec §10.3 — shutdown awaits in-flight shadow-writes before close."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    pt = PaperTrader()

    completed = asyncio.Event()

    async def _slow_handoff():
        await asyncio.sleep(0.25)
        completed.set()

    task = asyncio.create_task(_slow_handoff())
    pt._pending_live_tasks.add(task)
    task.add_done_callback(pt._pending_live_tasks.discard)

    from scout.main import _drain_pending_live_tasks

    await _drain_pending_live_tasks(pt, timeout_sec=5.0)

    assert completed.is_set()
    assert len(pt._pending_live_tasks) == 0
    await db.close()


async def test_drain_timeout_does_not_raise(tmp_path):
    """Drain with short timeout on slow task logs warning + returns."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    pt = PaperTrader()

    async def _very_slow():
        await asyncio.sleep(10)

    task = asyncio.create_task(_very_slow())
    pt._pending_live_tasks.add(task)
    task.add_done_callback(pt._pending_live_tasks.discard)

    from scout.main import _drain_pending_live_tasks

    # Should return without raising even though the task is still running.
    await _drain_pending_live_tasks(pt, timeout_sec=0.05)

    # Clean up the dangling task so it doesn't leak.
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    await db.close()
