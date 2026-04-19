"""Shared test fixtures for CoinPump Scout."""

import os
import sys

import pytest

from scout.config import Settings
from scout.models import CandidateToken


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    """Force-exit on CI once pytest finishes.

    aiosqlite opens a non-daemon worker thread per Connection. Any test that
    forgets `await db.close()` leaks that thread, which blocks the interpreter
    from exiting. On Linux CI this manifested as a 9-minute hang after all
    tests passed; on Windows (local dev) the same leak also reproduces.

    Known remaining explicit leakers are fixed test-by-test (see commits
    touching `await db.close()`). Two leakers in tests were patched; a
    third leak source still lives somewhere in aiosqlite shutdown paths —
    tracked in https://github.com/Trivenidigital/gecko-alpha/issues/31.
    This hook is belt-and-braces: if a future test leaks, CI still exits
    on time rather than burning the job timeout. Local developers don't
    hit this path because it only fires on GHA.

    `trylast=True` lets other sessionfinish plugins (coverage thresholds,
    xdist worker reporting, pytest-html) run their hooks and mutate
    `exitstatus` first; we read the post-mutation value. We also flush
    stdout/stderr before `os._exit` so any late teardown traceback or
    captured output reaches the CI log — `os._exit` bypasses the normal
    threading-shutdown and stream-flush paths.
    """
    if os.environ.get("GITHUB_ACTIONS") == "true":
        sys.stdout.flush()
        sys.stderr.flush()
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
