"""Shared test fixtures for CoinPump Scout."""

import aiosqlite.core
import pytest
import sys

from scout.config import Settings
from scout.models import CandidateToken

# Fix for issue #31 — aiosqlite interpreter-shutdown hang.
#
# aiosqlite.Connection spawns a worker Thread that is NOT a daemon. On
# interpreter shutdown Python waits for all non-daemon threads to exit.
# In tests (pytest-asyncio auto mode) each test gets its own event loop;
# when a Connection is closed, the worker thread tries to post a result
# via call_soon_threadsafe to a loop that pytest-asyncio has since
# closed, raising RuntimeError('Event loop is closed') from inside the
# thread's try/except. That exception propagates out of the worker and
# the thread dies — but the underlying sqlite3 file is still unclosed
# on some paths, and other aiosqlite internals have left pending work
# that keeps the interpreter alive on shutdown for ~9 minutes on CI.
#
# Force the worker thread to be a daemon in tests only. Production code
# still uses the non-daemon default so a clean shutdown path for real
# data writes is preserved. This must run before any aiosqlite.Connection
# is instantiated; conftest.py module-level is early enough because
# scout.db only creates Connections lazily inside test bodies.
_OrigThread = aiosqlite.core.Thread


class _DaemonThread(_OrigThread):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.daemon = True


aiosqlite.core.Thread = _DaemonThread


@pytest.fixture(autouse=True)
def _reset_signal_sources_cache():
    """BL-067: per-test reset of `scout.trading.conviction` module cache.

    The `_signal_sources_missing` set is module-level — without a reset
    fixture, a missing-table cached during one test silently propagates
    to all subsequent tests in the same session, causing backtest tests
    to see stack=0 for tables that were missing in a different DB.

    Try/except for TDD-friendliness — works before the module exists.
    """
    try:
        from scout.trading.conviction import clear_missing_sources_cache_for_tests

        clear_missing_sources_cache_for_tests()
    except ImportError:
        pass
    yield


@pytest.fixture(autouse=True)
async def _reset_coingecko_limiter_state():
    """Keep shared CoinGecko cooldown state from leaking between tests."""

    async def _reset_known_limiters() -> None:
        from scout import ratelimit

        limiters = {ratelimit.coingecko_limiter}
        for module_name in (
            "scout.ingestion.coingecko",
            "scout.social.telegram.resolver",
            "scout.secondwave.detector",
            "scout.counter.detail",
            "scout.narrative.evaluator",
            "scout.narrative.observer",
            "scout.narrative.predictor",
        ):
            module = sys.modules.get(module_name)
            limiter = getattr(module, "coingecko_limiter", None) if module else None
            if limiter is not None:
                limiters.add(limiter)

        for limiter in limiters:
            reset = getattr(limiter, "reset", None)
            if reset is not None:
                await reset()

    await _reset_known_limiters()
    yield
    await _reset_known_limiters()


@pytest.fixture
def settings_factory():
    def _make(**overrides):
        defaults = dict(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            ANTHROPIC_API_KEY="k",
        )
        defaults.update(overrides)
        return Settings(**defaults)

    return _make


@pytest.fixture
def patch_module_sleep(monkeypatch):
    """Return a helper that short-circuits ``asyncio.sleep`` in specific modules.

    Usage::

        def test_x(patch_module_sleep):
            patch_module_sleep("scout.ingestion.coingecko", "scout.ratelimit")
            ...

    Builds a ``types.SimpleNamespace`` clone of the real ``asyncio`` module with
    ``sleep`` replaced by an instant no-op, then monkey-patches the target
    modules' ``asyncio`` attribute to that clone. The real ``asyncio`` module is
    untouched — aiohttp, pytest-asyncio, and other libs keep working normally.
    """
    import asyncio as _asyncio_mod
    import importlib
    import types

    fake_asyncio = types.SimpleNamespace(
        **{
            n: getattr(_asyncio_mod, n)
            for n in dir(_asyncio_mod)
            if not n.startswith("_")
        }
    )

    async def _instant(_):
        return None

    fake_asyncio.sleep = _instant

    def _apply(*module_paths):
        for path in module_paths:
            mod = importlib.import_module(path)
            monkeypatch.setattr(mod, "asyncio", fake_asyncio)

    return _apply


@pytest.fixture
def token_factory():
    def _make(**overrides):
        defaults = dict(
            contract_address="0xtest",
            chain="solana",
            token_name="Test",
            ticker="TST",
            token_age_days=1.0,
            market_cap_usd=50000.0,
            liquidity_usd=10000.0,
            volume_24h_usd=80000.0,
            holder_count=100,
            holder_growth_1h=25,
        )
        defaults.update(overrides)
        return CandidateToken(**defaults)

    return _make
