"""Shared async rate limiter for CoinGecko API calls.

All modules making CoinGecko requests should acquire from this limiter
to stay within the free tier (30 req/min). Uses a token bucket algorithm.
"""

import asyncio
import random
import time
from collections.abc import Callable
from collections import deque

import structlog

logger = structlog.get_logger()


class RateLimiter:
    """Token bucket rate limiter. Async-safe."""

    def __init__(
        self,
        max_calls: int = 25,
        period: float = 60.0,
        *,
        min_interval_seconds: float = 0.0,
        jitter_seconds: float = 0.0,
        random_fn: Callable[[], float] = random.random,
    ):
        """Allow max_calls within period seconds. Default: 25/min (buffer under 30/min limit)."""
        self._max_calls = max_calls
        self._period = period
        self._min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._jitter_seconds = max(0.0, float(jitter_seconds))
        self._random_fn = random_fn
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._backoff_until: float = 0.0
        self._last_acquire_at: float | None = None

    def configure(
        self,
        *,
        max_calls: int,
        period: float,
        min_interval_seconds: float,
        jitter_seconds: float,
    ) -> None:
        """Reconfigure in place so modules holding this singleton see new settings."""
        self._max_calls = max_calls
        self._period = period
        self._min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._jitter_seconds = max(0.0, float(jitter_seconds))
        self._timestamps.clear()
        self._backoff_until = 0.0
        self._last_acquire_at = None

    async def acquire(self) -> None:
        """Wait until a request slot is available."""
        async with self._lock:
            now = time.monotonic()

            # Global backoff check — any caller that hit 429 can force
            # all other callers to back off for a fixed window.
            if self._backoff_until > now:
                wait = self._backoff_until - now
                logger.info("rate_limiter_global_backoff", wait_seconds=round(wait, 1))
                await asyncio.sleep(wait)
                now = time.monotonic()

            # Purge old timestamps
            while self._timestamps and self._timestamps[0] < now - self._period:
                self._timestamps.popleft()

            if len(self._timestamps) >= self._max_calls:
                # Wait until the oldest timestamp expires
                wait_time = self._timestamps[0] + self._period - now
                if wait_time > 0:
                    logger.info(
                        "rate_limiter_waiting", wait_seconds=round(wait_time, 1)
                    )
                    await asyncio.sleep(wait_time)
                    # Re-purge after sleep
                    now = time.monotonic()
                    while self._timestamps and self._timestamps[0] < now - self._period:
                        self._timestamps.popleft()

            if self._last_acquire_at is not None and self._min_interval_seconds > 0:
                spacing = self._min_interval_seconds + (
                    self._jitter_seconds * self._random_fn()
                )
                wait_time = self._last_acquire_at + spacing - now
                if wait_time > 0:
                    logger.info(
                        "rate_limiter_spacing",
                        wait_seconds=round(wait_time, 3),
                        min_interval_seconds=self._min_interval_seconds,
                        jitter_seconds=self._jitter_seconds,
                    )
                    await asyncio.sleep(wait_time)
                    now = time.monotonic()

            self._last_acquire_at = time.monotonic()
            self._timestamps.append(self._last_acquire_at)

    async def report_429(self, backoff_seconds: float = 30.0) -> None:
        """Called by any caller that received a 429. Forces all callers to back off."""
        async with self._lock:
            self._backoff_until = max(
                self._backoff_until,
                time.monotonic() + backoff_seconds,
            )
        logger.warning("rate_limiter_429_reported", backoff=backoff_seconds)

    async def reset(self) -> None:
        """Clear all tracked timestamps and backoff state. For tests only."""
        async with self._lock:
            self._timestamps.clear()
            self._backoff_until = 0.0
            self._last_acquire_at = None


# Default singleton — can be overridden for testing or reconfigured from settings.
coingecko_limiter = RateLimiter(max_calls=25, period=60.0)


def configure_from_settings(settings) -> None:
    """Update the singleton limiter from config.

    Called once at startup from scout.main to honour the
    COINGECKO_RATE_LIMIT_PER_MIN config knob without creating a
    circular import between scout.config and scout.ratelimit.
    """
    coingecko_limiter.configure(
        max_calls=settings.COINGECKO_RATE_LIMIT_PER_MIN,
        period=60.0,
        min_interval_seconds=settings.COINGECKO_MIN_REQUEST_INTERVAL_SEC,
        jitter_seconds=settings.COINGECKO_REQUEST_JITTER_SEC,
    )
