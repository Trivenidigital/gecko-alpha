from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp
import structlog

from gecko.settings import GeckoSettings

log = structlog.get_logger("gecko.http")


def _retryable(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


class UpstreamError(Exception):
    def __init__(self, status: int, url: str):
        super().__init__(f"upstream {status} for {url}")
        self.status = status
        self.url = url


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
                if not _retryable(resp.status):
                    raise UpstreamError(resp.status, url)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last = 0
            log.warning("http_transport_error", url=url, error=repr(exc))
        if attempt < retries:
            delay = backoff[min(attempt, len(backoff) - 1)]
            log.warning("http_retry", url=url, status=last, delay=delay)
            await sleep(delay)
    raise UpstreamError(last, url)
