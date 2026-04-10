"""Shared async rate limiter for CoinGecko API calls.

All modules making CoinGecko requests should acquire from this limiter
to stay within the free tier (30 req/min). Uses a token bucket algorithm.
"""

import asyncio
import time
from collections import deque

import structlog

logger = structlog.get_logger()


class RateLimiter:
    """Token bucket rate limiter. Async-safe."""

    def __init__(self, max_calls: int = 25, period: float = 60.0):
        """Allow max_calls within period seconds. Default: 25/min (buffer under 30/min limit)."""
        self._max_calls = max_calls
        self._period = period
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a request slot is available."""
        async with self._lock:
            now = time.monotonic()
            # Purge old timestamps
            while self._timestamps and self._timestamps[0] < now - self._period:
                self._timestamps.popleft()

            if len(self._timestamps) >= self._max_calls:
                # Wait until the oldest timestamp expires
                wait_time = self._timestamps[0] + self._period - now
                if wait_time > 0:
                    logger.info("rate_limiter_waiting", wait_seconds=round(wait_time, 1))
                    await asyncio.sleep(wait_time)
                    # Re-purge after sleep
                    now = time.monotonic()
                    while self._timestamps and self._timestamps[0] < now - self._period:
                        self._timestamps.popleft()

            self._timestamps.append(time.monotonic())


# Singleton instance shared by all CoinGecko callers
coingecko_limiter = RateLimiter(max_calls=25, period=60.0)
