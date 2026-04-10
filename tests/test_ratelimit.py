"""Tests for shared CoinGecko rate limiter."""

import asyncio
import time

import pytest

from scout.ratelimit import RateLimiter


async def test_allows_within_limit():
    limiter = RateLimiter(max_calls=5, period=1.0)
    start = time.monotonic()
    for _ in range(5):
        await limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed < 0.5  # should be near-instant


async def test_blocks_over_limit():
    limiter = RateLimiter(max_calls=3, period=1.0)
    for _ in range(3):
        await limiter.acquire()
    start = time.monotonic()
    await limiter.acquire()  # 4th call should wait
    elapsed = time.monotonic() - start
    assert elapsed >= 0.8  # should wait ~1 second


async def test_concurrent_callers_overload():
    """Fire N+5 concurrent callers; the last 5 must wait for the window to roll."""
    n = 5
    limiter = RateLimiter(max_calls=n, period=1.0)
    completions: list[float] = []

    async def caller():
        await limiter.acquire()
        completions.append(time.monotonic())

    start = time.monotonic()
    await asyncio.gather(*(caller() for _ in range(n + 5)))

    # First n callers complete near-instantly.
    fast = [t for t in completions if (t - start) < 0.5]
    slow = [t for t in completions if (t - start) >= 0.8]
    assert len(fast) == n, f"expected {n} fast completions, got {len(fast)}"
    assert len(slow) == 5, f"expected 5 slow completions, got {len(slow)}"


async def test_report_429_forces_global_backoff():
    """After report_429, all subsequent acquires must wait for the backoff."""
    limiter = RateLimiter(max_calls=100, period=60.0)
    await limiter.report_429(backoff_seconds=0.3)

    start = time.monotonic()
    await limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.25, f"expected >=0.25s wait, got {elapsed:.3f}s"


async def test_reset_clears_state():
    """reset() clears timestamps and backoff so a fresh acquire is instant."""
    limiter = RateLimiter(max_calls=2, period=60.0)
    await limiter.acquire()
    await limiter.acquire()
    await limiter.report_429(backoff_seconds=30.0)

    await limiter.reset()

    start = time.monotonic()
    await limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed < 0.1
