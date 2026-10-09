from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp
import structlog

from gecko.settings import GeckoSettings

log = structlog.get_logger("gecko.http")
RETRY_STATUSES = {429, 500, 502, 503, 504}


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
            if resp.status not in RETRY_STATUSES:
                raise UpstreamError(resp.status, url)
        if attempt < retries:
            delay = backoff[min(attempt, len(backoff) - 1)]
            log.warning("http_retry", url=url, status=last, delay=delay)
            await sleep(delay)
    raise UpstreamError(last, url)
