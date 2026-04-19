"""Shared test fixtures for CoinPump Scout."""

import os

import pytest

from scout.config import Settings
from scout.models import CandidateToken


def pytest_sessionfinish(session, exitstatus):
    """Force-exit on CI once pytest finishes.

    aiosqlite opens a non-daemon worker thread per Connection. Any test that
    forgets `await db.close()` leaks that thread, which blocks the interpreter
    from exiting. On Linux CI this manifested as a 9-minute hang after all
    tests passed; on Windows (local dev) the same leak was historically
    benign because Python's thread shutdown path is different there.

    Known remaining explicit leakers are fixed test-by-test (see commits
    touching `await db.close()`). This hook is belt-and-braces: if a future
    test leaks, CI still exits on time rather than burning the job timeout.
    Local developers don't hit this path because it only fires on GHA.
    """
    if os.environ.get("GITHUB_ACTIONS") == "true":
        os._exit(exitstatus)


@pytest.fixture
def settings_factory():
    def _make(**overrides):
        defaults = dict(
            TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k"
        )
        defaults.update(overrides)
        return Settings(**defaults)

    return _make


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
