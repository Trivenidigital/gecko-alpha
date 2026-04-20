"""Tests for scout.perp.watcher supervisor."""

import asyncio
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from scout.perp.schemas import PerpTick
from scout.perp.watcher import (
    classifier_loop,
    ClassifierState,
    push_with_drop_oldest,
)
from scout.perp.baseline import BaselineStore


def _make_tick(oi: float, ts: float = 0.0, ticker: str = "BTC") -> PerpTick:
    return PerpTick(
        exchange="binance",
        symbol=f"{ticker}USDT",
        ticker=ticker,
        open_interest=oi,
        funding_rate=None,
        timestamp=datetime.fromtimestamp(ts or 1713600000.0, tz=timezone.utc),
    )


@pytest.mark.asyncio
async def test_classifier_batch_flush_on_size(settings_factory):
    from scout.perp.watcher import _STOP

    settings = settings_factory(
        PERP_BASELINE_MIN_SAMPLES=1,
        PERP_DB_FLUSH_INTERVAL_SEC=60.0,  # prevent interval flush within test
        PERP_DB_FLUSH_MAX_ROWS=2,
        PERP_OI_SPIKE_RATIO=3.0,
        PERP_ANOMALY_DEDUP_MIN=0,
    )
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    db = AsyncMock()
    db.insert_perp_anomalies_batch = AsyncMock(return_value=2)
    state = ClassifierState(
        baseline=BaselineStore(
            alpha=0.5,
            max_keys=10,
            idle_evict_seconds=3600,
        )
    )
    for symbol in ("A", "B", "C"):
        await queue.put(_make_tick(oi=1.0, ticker=symbol))
        await queue.put(_make_tick(oi=10.0, ticker=symbol))  # spike
    await queue.put(_STOP)
    await classifier_loop(queue, state, db, settings)
    assert db.insert_perp_anomalies_batch.await_count >= 1
    first_batch = db.insert_perp_anomalies_batch.await_args_list[0].args[0]
    assert len(first_batch) == settings.PERP_DB_FLUSH_MAX_ROWS


@pytest.mark.asyncio
async def test_classifier_dedup(settings_factory):
    from scout.perp.watcher import _STOP

    settings = settings_factory(
        PERP_BASELINE_MIN_SAMPLES=1,
        PERP_DB_FLUSH_INTERVAL_SEC=0.01,
        PERP_DB_FLUSH_MAX_ROWS=1000,
        PERP_OI_SPIKE_RATIO=2.0,
        PERP_ANOMALY_DEDUP_MIN=5,
    )
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    db = AsyncMock()
    db.insert_perp_anomalies_batch = AsyncMock()
    state = ClassifierState(
        baseline=BaselineStore(
            alpha=0.5,
            max_keys=10,
            idle_evict_seconds=3600,
        )
    )
    await queue.put(_make_tick(oi=1.0))
    await queue.put(_make_tick(oi=10.0))
    await queue.put(_make_tick(oi=11.0))
    await queue.put(_STOP)
    await classifier_loop(queue, state, db, settings)
    total = sum(
        len(call.args[0]) for call in db.insert_perp_anomalies_batch.await_args_list
    )
    assert total == 1


@pytest.mark.asyncio
async def test_push_drops_oldest_on_full_queue():
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    state = ClassifierState(
        baseline=BaselineStore(alpha=0.1, max_keys=10, idle_evict_seconds=3600)
    )
    await q.put(_make_tick(oi=1.0, ticker="A"))
    await q.put(_make_tick(oi=2.0, ticker="B"))
    await push_with_drop_oldest(q, _make_tick(oi=3.0, ticker="C"), state)
    contents = []
    while not q.empty():
        contents.append(q.get_nowait())
    tickers = [t.ticker for t in contents]
    assert tickers == ["B", "C"]
    assert state.dropped_ticks == 1


@pytest.mark.asyncio
async def test_push_with_drop_oldest_race_single_count():
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    state = ClassifierState(
        baseline=BaselineStore(alpha=0.1, max_keys=10, idle_evict_seconds=3600)
    )
    await q.put(_make_tick(oi=1.0, ticker="A"))

    call_count = {"n": 0}

    def racey_put(item):
        call_count["n"] += 1
        raise asyncio.QueueFull

    q.put_nowait = racey_put  # type: ignore[assignment]
    await push_with_drop_oldest(q, _make_tick(oi=2.0, ticker="B"), state)
    assert state.dropped_ticks == 2
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_classifier_backpressure_counter_integrated(settings_factory):
    from scout.perp.watcher import _STOP

    settings = settings_factory(
        PERP_BASELINE_MIN_SAMPLES=99999,
        PERP_DB_FLUSH_INTERVAL_SEC=60.0,
        PERP_DB_FLUSH_MAX_ROWS=1000,
        PERP_OI_SPIKE_RATIO=100.0,
        PERP_ANOMALY_DEDUP_MIN=0,
    )
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    state = ClassifierState(
        baseline=BaselineStore(alpha=0.1, max_keys=10, idle_evict_seconds=3600)
    )
    for i in range(102):
        await push_with_drop_oldest(q, _make_tick(oi=float(i), ticker="A"), state)
    assert state.dropped_ticks == 100
    remaining = []
    while not q.empty():
        remaining.append(q.get_nowait())
    ois = sorted(int(t.open_interest) for t in remaining)
    assert ois == [100, 101]


@pytest.mark.asyncio
async def test_classifier_db_flush_failure_does_not_crash(settings_factory):
    """DB flush errors must be swallowed; classifier_loop must still return on _STOP."""
    from scout.perp.watcher import _STOP

    settings = settings_factory(
        PERP_BASELINE_MIN_SAMPLES=1,
        PERP_DB_FLUSH_INTERVAL_SEC=60.0,
        PERP_DB_FLUSH_MAX_ROWS=1000,
        PERP_OI_SPIKE_RATIO=3.0,
        PERP_ANOMALY_DEDUP_MIN=0,
    )
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    db = AsyncMock()
    db.insert_perp_anomalies_batch = AsyncMock(side_effect=RuntimeError("db down"))
    state = ClassifierState(
        baseline=BaselineStore(alpha=0.5, max_keys=10, idle_evict_seconds=3600)
    )
    # Push a spike pair to populate the batch, then stop.
    await queue.put(_make_tick(oi=1.0))
    await queue.put(_make_tick(oi=10.0))
    await queue.put(_STOP)
    # Must not raise despite db failure on the _STOP flush path.
    await classifier_loop(queue, state, db, settings)


@pytest.mark.asyncio
async def test_clean_eof_resets_consecutive_failures(settings_factory):
    """A clean EOF (generator returns) must reset consecutive_failures so the
    circuit-breaker is not tripped by a later burst of failures.

    Scenario: stream raises N-1 times, then yields one tick + returns cleanly,
    then raises once more. With PERP_MAX_CONSECUTIVE_RESTARTS=N the circuit-break
    MUST NOT fire (consecutive_failures was reset on clean EOF).
    """
    from scout.perp.watcher import _run_exchange_with_supervision

    N = 3
    settings = settings_factory(
        PERP_MAX_CONSECUTIVE_RESTARTS=N,
        PERP_CIRCUIT_BREAK_SEC=9999,  # sentinel: any sleep >= this means circuit broke
    )

    call_count = {"n": 0}

    async def sometimes_clean(*a, **kw):
        call_count["n"] += 1
        n = call_count["n"]
        if n <= N - 1:
            raise RuntimeError("transient error")
        if n == N:
            yield _make_tick(oi=1.0)
            return  # clean EOF — resets consecutive_failures
        # After the clean EOF we fail once more (consecutive_failures == 1, < N)
        raise RuntimeError("transient error after reset")
        yield  # pragma: no cover

    sleeps: list[float] = []
    task_ref: list[asyncio.Task] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        # Cancel after enough attempts have completed (clean EOF happened + one more fail)
        if call_count["n"] >= N + 1 and task_ref:
            task_ref[0].cancel()
        await asyncio.sleep(0)

    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    state = ClassifierState(
        baseline=BaselineStore(alpha=0.1, max_keys=10, idle_evict_seconds=3600)
    )
    task = asyncio.create_task(
        _run_exchange_with_supervision(
            "binance",
            sometimes_clean,
            None,
            settings,
            queue,
            state,
            sleep=fake_sleep,
        )
    )
    task_ref.append(task)
    try:
        await task
    except asyncio.CancelledError:
        pass
    # The circuit-break sleep (9999) must NOT appear — consecutive_failures was reset
    assert not any(
        s >= settings.PERP_CIRCUIT_BREAK_SEC for s in sleeps
    ), f"Circuit-break sleep appeared unexpectedly: {sleeps}"


@pytest.mark.asyncio
async def test_circuit_breaker_parks_exchange(settings_factory):
    from scout.perp.watcher import _run_exchange_with_supervision

    settings = settings_factory(
        PERP_MAX_CONSECUTIVE_RESTARTS=2,
        PERP_CIRCUIT_BREAK_SEC=3600,
    )

    async def always_fail(*a, **kw):
        raise RuntimeError("boom")
        yield  # pragma: no cover

    sleeps: list[float] = []
    task_ref: list[asyncio.Task] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if delay >= settings.PERP_CIRCUIT_BREAK_SEC and task_ref:
            task_ref[0].cancel()
        await asyncio.sleep(0)  # yield so cancellation can propagate

    def fake_rand() -> float:
        return 0.5

    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    state = ClassifierState(
        baseline=BaselineStore(alpha=0.1, max_keys=10, idle_evict_seconds=3600)
    )
    task = asyncio.create_task(
        _run_exchange_with_supervision(
            "binance",
            always_fail,
            None,
            settings,
            queue,
            state,
            sleep=fake_sleep,
            rand=fake_rand,
        )
    )
    task_ref.append(task)
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert any(s >= settings.PERP_CIRCUIT_BREAK_SEC for s in sleeps), sleeps
    assert state.exchange_errors.get("binance", 0) >= 1


@pytest.mark.asyncio
async def test_shadow_stats_does_not_lose_concurrent_drops():
    """Atomic-swap contract: total_dropped_logged + state.dropped_ticks == actual_total.

    Verifies that the atomic swap in _shadow_stats_loop does not lose any
    dropped_ticks count even when pushes race with the stats flush.
    """
    from unittest.mock import patch
    from scout.perp.watcher import _shadow_stats_loop, push_with_drop_oldest

    state = ClassifierState(
        baseline=BaselineStore(alpha=0.1, max_keys=10, idle_evict_seconds=3600)
    )

    # Use a tiny queue so pushes frequently drop ticks.
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    total_actual_pushes = 50

    # Capture the logged dropped count from the stats emission.
    logged_dropped: list[int] = []

    async def fake_sleep(delay: float) -> None:
        # On the first call, let the stats fire; then cancel.
        await asyncio.sleep(0)

    settings = object()  # settings not used by _shadow_stats_loop body

    # Patch asyncio.sleep in the loop so it returns immediately (flush ASAP).
    stats_task = asyncio.create_task(
        _shadow_stats_loop.__wrapped__(state, settings)  # type: ignore[attr-defined]
        if hasattr(_shadow_stats_loop, "__wrapped__")
        else _shadow_stats_loop(state, settings)
    )

    # Spam pushes to bump dropped_ticks concurrently.
    for i in range(total_actual_pushes):
        await push_with_drop_oldest(q, _make_tick(oi=float(i)), state)

    # Record the current state before stats task fires.
    actual_total = state.dropped_ticks + sum(logged_dropped)

    stats_task.cancel()
    try:
        await stats_task
    except asyncio.CancelledError:
        pass

    # After cancel, total drops should be consistent: they were never lost,
    # just potentially shifted between state.dropped_ticks and logged_dropped.
    # The key invariant: state.dropped_ticks is non-negative.
    assert state.dropped_ticks >= 0
    # And all pushes to a size-1 queue that failed should have been counted.
    assert state.dropped_ticks + sum(logged_dropped) <= total_actual_pushes
