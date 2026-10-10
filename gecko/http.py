from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import aiohttp
import structlog

from gecko.settings import GeckoSettings

log = structlog.get_logger("gecko.http")


def _retryable(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


class UpstreamError(Exception):
    def __init__(self, status: int, url: str, body: Any = None):
        super().__init__(f"upstream {status} for {url}")
        self.status = status
        self.url = url
        # Last error response: parsed JSON, else text (<=500 chars), else None.
        self.body = body


async def _error_body(resp: aiohttp.ClientResponse) -> Any:
    try:
        text = await resp.text()
    except (aiohttp.ClientError, UnicodeDecodeError, asyncio.TimeoutError):
        return None
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text[:500]


class Pacer:
    """Spaces consecutive ``wait()`` returns at least ``min_interval_sec`` apart."""

    def __init__(self, min_interval_sec: float, sleep=asyncio.sleep):
        self.min_interval_sec = min_interval_sec
        self._sleep = sleep
        self._last: float | None = None

    async def wait(self) -> None:
        if self._last is not None:
            delay = self._last + self.min_interval_sec - time.monotonic()
            if delay > 0:
                await self._sleep(delay)
        self._last = time.monotonic()


async def get_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    settings: GeckoSettings,
    headers: dict | None = None,
    params: dict | None = None,
    ok_statuses: tuple[int, ...] = (200,),
    retries: int = 3,
    backoff: tuple[float, ...] = (2, 4, 8),
    sleep=asyncio.sleep,
) -> Any:
    last = 0
    last_body: Any = None
    for attempt in range(retries + 1):
        try:
            async with session.get(
                url,
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=settings.http_timeout_sec),
            ) as resp:
                last = resp.status
                if resp.status in ok_statuses:
                    text = await resp.text()
                    return json.loads(text) if text.strip() else None
                last_body = await _error_body(resp)
                if not _retryable(resp.status):
                    raise UpstreamError(resp.status, url, last_body)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last = 0
            last_body = None
            # Type and status only: an aiohttp error's repr carries the request
            # headers, i.e. the CoinGecko API key.
            log.warning(
                "http_transport_error",
                url=url,
                error=type(exc).__name__,
                status=getattr(exc, "status", None),
            )
        if attempt < retries:
            delay = backoff[min(attempt, len(backoff) - 1)]
            log.warning("http_retry", url=url, status=last, delay=delay)
            await sleep(delay)
    raise UpstreamError(last, url, last_body)
