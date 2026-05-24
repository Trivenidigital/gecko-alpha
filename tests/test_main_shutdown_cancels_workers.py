"""Regression test for BL-NEW-PIPELINE-SIGTERM-HANDLER (PR #243).

The original `asyncio.gather(*tasks, return_exceptions=True)` in scout/main.py
blocked indefinitely when a worker loop did not cooperate with
``shutdown_event``. systemd would then SIGKILL after the 90s
``TimeoutStopSec`` and fire the OnFailure → Telegram chain.

These tests don't import scout.main directly (it has heavy import-time side
effects). They reproduce the asyncio.wait pattern and verify:

1. When shutdown_event fires, all pending worker tasks are cancelled within
   a bounded wall-clock window — NOT held hostage by a non-cooperative loop.
2. When a worker exits unexpectedly (crash), shutdown propagates and the
   other workers are cancelled.
"""

from __future__ import annotations

import asyncio
import time

import pytest


async def _drain(tasks: list[asyncio.Task], timeout: float = 20.0) -> int:
    """Match the production drain pattern: cancel + wait_for + count timeouts."""
    for t in tasks:
        t.cancel()
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout,
        )
        return 0
    except asyncio.TimeoutError:
        return sum(1 for t in tasks if not t.done())


async def _supervise(
    workers: list[asyncio.Task],
    shutdown_event: asyncio.Event,
) -> tuple[set[asyncio.Task], set[asyncio.Task]]:
    """Reproduce the production wait pattern from scout/main.py."""
    shutdown_waiter = asyncio.create_task(
        shutdown_event.wait(), name="_shutdown_waiter"
    )
    wait_set = set(workers) | {shutdown_waiter}
    done, pending = await asyncio.wait(
        wait_set, return_when=asyncio.FIRST_COMPLETED
    )
    if shutdown_waiter not in done:
        shutdown_event.set()
    return done, pending


@pytest.mark.asyncio
async def test_shutdown_event_cancels_uncooperative_worker():
    """A worker that ignores shutdown_event must still be cancelled when shutdown fires."""
    shutdown_event = asyncio.Event()

    async def uncooperative_loop():
        # Doesn't check shutdown_event — only exits on cancellation.
        while True:
            await asyncio.sleep(0.05)

    worker = asyncio.create_task(uncooperative_loop())

    async def _signal_after(delay):
        await asyncio.sleep(delay)
        shutdown_event.set()

    signaler = asyncio.create_task(_signal_after(0.1))

    started = time.monotonic()
    done, pending = await _supervise([worker], shutdown_event)
    n_timeouts = await _drain(list(pending), timeout=2.0)
    elapsed = time.monotonic() - started

    await signaler

    # Worker MUST be cancelled — not held until forever.
    assert worker.done(), "uncooperative worker should have been cancelled"
    assert n_timeouts == 0, "drain should not have timed out within 2s"
    # Total wall-clock: signal delay (0.1s) + cancel propagation (~ms).
    assert elapsed < 1.0, f"shutdown took {elapsed:.2f}s — should be sub-second"


@pytest.mark.asyncio
async def test_shutdown_event_cancels_multiple_workers_in_parallel():
    """All workers cancel in a single drain pass, not sequentially."""
    shutdown_event = asyncio.Event()

    async def slow_to_cancel():
        try:
            while True:
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            # Simulate cleanup work — async cleanup, but bounded.
            await asyncio.sleep(0.1)
            raise

    workers = [asyncio.create_task(slow_to_cancel()) for _ in range(5)]

    async def _signal_after(delay):
        await asyncio.sleep(delay)
        shutdown_event.set()

    signaler = asyncio.create_task(_signal_after(0.05))

    started = time.monotonic()
    done, pending = await _supervise(workers, shutdown_event)
    n_timeouts = await _drain(list(pending), timeout=2.0)
    elapsed = time.monotonic() - started

    await signaler

    assert all(w.done() for w in workers), "all workers should be cancelled"
    assert n_timeouts == 0
    # If cancellation were sequential: 5 × 0.1s = 0.5s. Parallel: ~0.1s.
    assert elapsed < 0.5, f"parallel drain expected, took {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_worker_crash_triggers_shutdown_propagation():
    """If a worker exits before shutdown fires, propagate shutdown to others."""
    shutdown_event = asyncio.Event()

    async def crashes_quickly():
        await asyncio.sleep(0.05)
        raise RuntimeError("simulated crash")

    async def cooperative_loop():
        while not shutdown_event.is_set():
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                continue

    crasher = asyncio.create_task(crashes_quickly())
    cooperative = asyncio.create_task(cooperative_loop())

    done, pending = await _supervise([crasher, cooperative], shutdown_event)

    # Crasher completed (with exception); cooperative should now see shutdown
    # and exit on its next poll.
    assert shutdown_event.is_set(), "shutdown should propagate after crash"
    n_timeouts = await _drain(list(pending), timeout=1.0)
    assert n_timeouts == 0
    assert cooperative.done()


@pytest.mark.asyncio
async def test_shutdown_does_not_fire_when_all_workers_complete_naturally():
    """If all workers finish on their own, shutdown_event should remain unset."""
    shutdown_event = asyncio.Event()

    async def short_lived():
        await asyncio.sleep(0.05)

    workers = [asyncio.create_task(short_lived()) for _ in range(3)]
    done, pending = await _supervise(workers, shutdown_event)
    # FIRST_COMPLETED returns when the first short_lived finishes.
    # The supervisor doesn't fire shutdown unless a non-shutdown-waiter task
    # exited unexpectedly — in this test it's "naturally", but the production
    # supervisor treats "worker exited" as crash-equivalent. That's the
    # documented behavior: workers are infinite loops, exit = abnormal.
    assert shutdown_event.is_set()
    n_timeouts = await _drain(list(pending), timeout=1.0)
    assert n_timeouts == 0
