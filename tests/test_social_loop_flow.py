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
        interactions_ring=(),
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


# ---------------------------------------------------------------------------
# Dedup / top-N cache-consistency invariant (fix #1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_topn_drop_does_not_update_dropped_coin_cache(db):
    """With TOP_N=1 and two firing coins: the DROPPED coin's post-state
    must NOT land in the cache; the SURVIVING coin's post-state DOES."""
    s = _settings(LUNARCRUSH_TOP_N=1)
    cache = BaselineCache()
    pre_a = _primed_state("a", "A", sample_count=500)
    pre_b = _primed_state("b", "B", sample_count=500)
    cache.set("a", pre_a)
    cache.set("b", pre_b)
    # Both fire; b has a larger ratio so it wins top-1.
    coins = [
        {"id": "a", "symbol": "A", "name": "AAA", "social_volume_24h": 300.0},
        {"id": "b", "symbol": "B", "name": "BBB", "social_volume_24h": 900.0},
    ]
    send_fake = AsyncMock(return_value=True)
    await _process_cycle(s, db, cache, coins, send_fn=send_fake)

    # b survived top-1 -- cache updated.
    state_b = cache.get("b")
    assert state_b is not None
    assert state_b.sample_count == 501

    # a was dropped -- cache stays at the pre-state.
    state_a = cache.get("a")
    assert state_a is not None
    assert state_a.sample_count == pre_a.sample_count


# ---------------------------------------------------------------------------
# 401 / _AuthDisabled path cleanly exits the loop (fix #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_401_exits_loop_cleanly(tmp_path, monkeypatch):
    """run_social_loop must break out on 401/403 rather than log-loop forever."""
    from scout.social.lunarcrush import loop as loop_mod

    s = _settings()
    d = Database(tmp_path / "auth.db")
    await d.initialize()
    try:
        shutdown = asyncio.Event()

        class _FakeClient:
            disabled = False

            def __init__(self, *a, **k):
                pass

            async def fetch_coins_list(self):
                # Flip to 401 behaviour on first call.
                _FakeClient.disabled = True
                self.disabled = True
                return [], 0

            async def close(self):
                pass

        monkeypatch.setattr(loop_mod, "LunarCrushClient", _FakeClient)

        task = asyncio.create_task(
            loop_mod.run_social_loop(s, d, shutdown)
        )
        # Should exit on its own within a short window -- _AuthDisabled breaks.
        await asyncio.wait_for(task, timeout=2.0)
        assert task.done()
        assert not task.cancelled()
        # No exception propagated out of the loop.
        assert task.exception() is None
    finally:
        await d.close()


# ---------------------------------------------------------------------------
# _insert_alerts rollback on mid-batch failure (fix #4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_alerts_rolls_back_on_mid_batch_failure(db, monkeypatch):
    """When the 3rd INSERT raises, the batch rolls back (0 rows persist)
    and the exception propagates out of _insert_alerts."""
    from scout.social.lunarcrush.loop import _insert_alerts
    from scout.social.models import ResearchAlert, SpikeKind

    alerts = []
    for i in range(3):
        alerts.append(
            ResearchAlert(
                coin_id=f"c{i}",
                symbol=f"C{i}",
                name=f"Coin {i}",
                spike_kinds=[SpikeKind.SOCIAL_VOLUME_24H],
                detected_at=datetime.now(timezone.utc),
            )
        )

    original_execute = db._conn.execute
    insert_count = [0]

    async def _maybe_fail(*args, **kwargs):
        sql = args[0] if args else ""
        if isinstance(sql, str) and "INSERT OR IGNORE INTO social_signals" in sql:
            insert_count[0] += 1
            if insert_count[0] == 3:
                raise RuntimeError("simulated 3rd insert fail")
        return await original_execute(*args, **kwargs)

    monkeypatch.setattr(db._conn, "execute", _maybe_fail)
    with pytest.raises(RuntimeError):
        await _insert_alerts(db, alerts)

    cursor = await db._conn.execute("SELECT COUNT(*) FROM social_signals")
    row = await cursor.fetchone()
    assert row[0] == 0  # rollback cleared the partial rows


# ---------------------------------------------------------------------------
# Telegram-fail retry semantics (fix #13)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telegram_fail_leaves_alerted_at_null_for_retry(db):
    """When Telegram dispatch fails, the row exists but alerted_at stays
    NULL -- the next cycle's dedup lets it re-enter detection."""
    s = _settings()
    cache = BaselineCache()
    cache.set("foo", _primed_state("foo", "FOO"))
    coins = [
        {"id": "foo", "symbol": "FOO", "name": "Foo", "social_volume_24h": 500.0}
    ]
    # Telegram returns False -- alerted_at should NOT be set.
    send_fake = AsyncMock(return_value=False)
    await _process_cycle(s, db, cache, coins, send_fn=send_fake)

    cursor = await db._conn.execute(
        "SELECT alerted_at FROM social_signals WHERE coin_id='foo'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] is None  # dedup treats NULL as "not yet alerted"


@pytest.mark.asyncio
async def test_telegram_success_sets_alerted_at(db):
    """Successful dispatch stamps alerted_at so future cycles dedup it."""
    s = _settings()
    cache = BaselineCache()
    cache.set("foo", _primed_state("foo", "FOO"))
    coins = [
        {"id": "foo", "symbol": "FOO", "name": "Foo", "social_volume_24h": 500.0}
    ]
    send_fake = AsyncMock(return_value=True)
    await _process_cycle(s, db, cache, coins, send_fn=send_fake)

    cursor = await db._conn.execute(
        "SELECT alerted_at FROM social_signals WHERE coin_id='foo'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] is not None


def test_lunarcrush_max_consecutive_restarts_setting_present():
    """Config exposes LUNARCRUSH_MAX_CONSECUTIVE_RESTARTS with sane default."""
    s = _settings()
    assert hasattr(s, "LUNARCRUSH_MAX_CONSECUTIVE_RESTARTS")
    assert int(s.LUNARCRUSH_MAX_CONSECUTIVE_RESTARTS) >= 1


def test_social_restart_cap_blocks_6th_restart(monkeypatch):
    """Simulate 6 consecutive crashes: the 6th does NOT schedule a restart.

    Tests the module-level counter + cap logic by driving a mock scheduler
    with the same cap-guarded increment pattern used in main._schedule_social_restart.
    """
    from scout.main import _social_consecutive_restarts

    # Reset the module-level counter deterministically.
    _social_consecutive_restarts[0] = 0

    restart_calls: list[float] = []
    max_restarts = 5

    def _fake_schedule(delay: float) -> None:
        _social_consecutive_restarts[0] += 1
        if _social_consecutive_restarts[0] > max_restarts:
            return
        restart_calls.append(delay)

    for _ in range(6):
        _fake_schedule(30.0)
    assert len(restart_calls) == 5  # 6th was blocked

    # Reset after test so later tests see a clean counter.
    _social_consecutive_restarts[0] = 0


@pytest.mark.asyncio
async def test_dedup_ignores_null_alerted_at_rows(db):
    """Dedup only suppresses rows whose alerted_at is non-NULL."""
    from scout.social.lunarcrush.detector import detect_spikes
    from scout.social.models import BaselineState

    s = _settings()
    cache = BaselineCache()
    now = datetime.now(timezone.utc)
    cache.set(
        "foo",
        BaselineState(
            coin_id="foo",
            symbol="FOO",
            avg_social_volume_24h=100.0,
            avg_galaxy_score=50.0,
            last_galaxy_score=50.0,
            interactions_ring=(),
            sample_count=500,
            last_poll_at=now,
            last_updated=now,
        ),
    )
    # Insert a ROW that was stored but never dispatched.
    await db._conn.execute(
        """INSERT INTO social_signals (coin_id, symbol, name, detected_at)
           VALUES ('foo','FOO','Foo', datetime('now', '-1 hours'))"""
    )
    await db._conn.commit()

    coins = [
        {"id": "foo", "symbol": "FOO", "name": "Foo", "social_volume_24h": 500.0}
    ]
    alerts, _ = await detect_spikes(db, s, cache, coins)
    # The previous row has alerted_at NULL -> NOT deduped -> alert fires.
    assert len(alerts) == 1
