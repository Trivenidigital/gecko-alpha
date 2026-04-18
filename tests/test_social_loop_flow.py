"""Tests for scout.social.lunarcrush.loop -- transactional buffered commits."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from scout.config import Settings
from scout.db import Database
from scout.social.baselines import BaselineCache
from scout.social.lunarcrush.loop import _process_cycle
from scout.social.models import BaselineState, ResearchAlert, SpikeKind


def _settings(**overrides) -> Settings:
    defaults = dict(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        LUNARCRUSH_ENABLED=True,
        LUNARCRUSH_API_KEY="lc_key",
        LUNARCRUSH_BASELINE_MIN_HOURS=24,
        LUNARCRUSH_BASELINE_MIN_SAMPLES=288,
        LUNARCRUSH_POLL_INTERVAL=300,
        LUNARCRUSH_SOCIAL_SPIKE_RATIO=2.0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _primed_state(coin_id: str, symbol: str, sample_count: int = 500) -> BaselineState:
    now = datetime.now(timezone.utc)
    return BaselineState(
        coin_id=coin_id,
        symbol=symbol,
        avg_social_volume_24h=100.0,
        avg_galaxy_score=50.0,
        last_galaxy_score=50.0,
        interactions_ring=[],
        sample_count=sample_count,
        last_poll_at=now,
        last_updated=now,
    )


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "loop.db")
    await d.initialize()
    yield d
    await d.close()


# ---------------------------------------------------------------------------
# Transactional buffered-commit flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_success_telegram_success_cache_updated(db):
    """Happy path: both DB INSERT and Telegram succeed -> cache updated."""
    s = _settings()
    cache = BaselineCache()
    cache.set("foo", _primed_state("foo", "FOO"))
    coins = [
        {"id": "foo", "symbol": "FOO", "name": "Foo", "social_volume_24h": 500.0}
    ]
    send_fake = AsyncMock(return_value=True)
    await _process_cycle(s, db, cache, coins, send_fn=send_fake)
    state = cache.get("foo")
    # Firing coins' buffered states are committed after successful DB INSERT.
    assert state is not None
    # sample_count is the progress invariant; spike-exclusion freezes avg but
    # still advances the sample count per design spec §5.4.
    assert state.sample_count == 501
    send_fake.assert_awaited_once()


@pytest.mark.asyncio
async def test_db_failure_no_telegram_and_cache_not_updated(db, monkeypatch):
    """If the DB INSERT fails, Telegram is NOT called and firing-coin cache
    entry stays on its pre-update value (buffered-commit pattern)."""
    s = _settings()
    cache = BaselineCache()
    original_state = _primed_state("foo", "FOO")
    cache.set("foo", original_state)
    coins = [
        {"id": "foo", "symbol": "FOO", "name": "Foo", "social_volume_24h": 500.0}
    ]
    send_fake = AsyncMock(return_value=True)

    # Force the insert helper to raise.
    async def _fail_insert(*args, **kwargs):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(
        "scout.social.lunarcrush.loop._insert_alerts",
        _fail_insert,
    )
    await _process_cycle(s, db, cache, coins, send_fn=send_fake)
    state = cache.get("foo")
    assert state is not None
    # Avg stayed at 100 because the buffered post-state was NOT committed.
    assert state.avg_social_volume_24h == original_state.avg_social_volume_24h
    send_fake.assert_not_awaited()


@pytest.mark.asyncio
async def test_db_success_telegram_fail_cache_still_updated(db):
    """DB INSERT succeeds, Telegram fails -> cache IS updated (alert treated as sent)."""
    s = _settings()
    cache = BaselineCache()
    cache.set("foo", _primed_state("foo", "FOO"))
    coins = [
        {"id": "foo", "symbol": "FOO", "name": "Foo", "social_volume_24h": 500.0}
    ]
    send_fake = AsyncMock(return_value=False)
    await _process_cycle(s, db, cache, coins, send_fn=send_fake)
    state = cache.get("foo")
    assert state is not None
    # Post-state was committed -- sample_count advanced from the primed 500.
    assert state.sample_count == 501


@pytest.mark.asyncio
async def test_non_firing_cache_updated_regardless(db):
    """A coin that doesn't fire still gets its baseline sample_count bumped."""
    s = _settings()
    cache = BaselineCache()
    cache.set("foo", _primed_state("foo", "FOO", sample_count=42))
    coins = [
        {"id": "foo", "symbol": "FOO", "name": "Foo", "social_volume_24h": 110.0}
    ]
    send_fake = AsyncMock(return_value=True)
    await _process_cycle(s, db, cache, coins, send_fn=send_fake)
    state = cache.get("foo")
    assert state is not None
    assert state.sample_count == 43


# ---------------------------------------------------------------------------
# Task restart on crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_done_callback_recreates_task_on_exception():
    """The loop wrapper re-creates the task with a 30s backoff on uncaught crash."""
    from scout.social.lunarcrush.loop import _make_done_callback

    restart_calls: list[float] = []

    def _fake_create_task(delay: float) -> None:
        restart_calls.append(delay)

    cb = _make_done_callback(restarter=_fake_create_task, backoff_seconds=30)

    class _CrashTask:
        def __init__(self):
            self._exc = RuntimeError("crash")

        def cancelled(self):
            return False

        def exception(self):
            return self._exc

    cb(_CrashTask())
    assert restart_calls == [30]
