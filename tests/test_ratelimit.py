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


async def test_concurrent_callers():
    limiter = RateLimiter(max_calls=5, period=1.0)
    results = []

    async def caller(name):
        await limiter.acquire()
        results.append(name)

    # Fire 5 concurrent callers -- all should succeed quickly
    await asyncio.gather(*(caller(f"c{i}") for i in range(5)))
    assert len(results) == 5
