"""Async LunarCrush v4 API client.

* Owns its own ``aiohttp.ClientSession`` so a main-pipeline shutdown cannot
  leave us with a closed session (design spec §2).
* 9 req/min token-bucket rate limit (under hard 10/min on Individual tier).
* 429 backoff: 5s -> 10s -> 20s -> 40s capped at 60s.
* 401 / 403 sets ``disabled=True``; next call is a no-op and the loop
  exits cleanly.
"""

from __future__ import annotations

import asyncio
import json as _json
import time
from typing import TYPE_CHECKING, Optional

import aiohttp
import structlog

if TYPE_CHECKING:
    from scout.config import Settings

logger = structlog.get_logger(__name__)

_BACKOFF_SEQUENCE = [5.0, 10.0, 20.0, 40.0]
_BACKOFF_CAP = 60.0


class LunarCrushClient:
    """Minimal async client for the v4 public endpoints we consume."""

    def __init__(
        self,
        settings: "Settings",
        *,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self._settings = settings
        # Vendor isolation: own session unless the caller hands one in
        # (tests sometimes pass a shared mock session, but production uses
        # the client-owned one).
        self._session = session or aiohttp.ClientSession()
        self._owns_session = session is None
        self._rate_limit_per_min = int(
            getattr(settings, "LUNARCRUSH_RATE_LIMIT_PER_MIN", 9)
        )
        self._call_times: list[float] = []
        self.disabled = False

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._settings.LUNARCRUSH_API_KEY}"}

    async def _respect_rate_limit(self) -> None:
        """Token-bucket-ish: if >= N calls in last 60s, sleep until a slot frees."""
        now = time.monotonic()
        cutoff = now - 60.0
        self._call_times = [t for t in self._call_times if t >= cutoff]
        if len(self._call_times) >= self._rate_limit_per_min:
            oldest = self._call_times[0]
            sleep_for = max(0.0, 60.0 - (now - oldest))
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

    async def fetch_coins_list(self) -> tuple[list[dict], int]:
        """Fetch ``/coins/list/v2``. Returns (coins, credit_cost).

        Costs 1 credit per call (see design spec §4). Never raises.
        """
        if self.disabled:
            return [], 0
        if not self._settings.LUNARCRUSH_API_KEY:
            return [], 0

        base = str(self._settings.LUNARCRUSH_BASE_URL).rstrip("/")
        url = f"{base}/coins/list/v2"

        for attempt, backoff in enumerate(_BACKOFF_SEQUENCE + [_BACKOFF_CAP]):
            await self._respect_rate_limit()
            self._call_times.append(time.monotonic())
            try:
                async with self._session.get(url, headers=self._auth_headers()) as resp:
                    status = resp.status
                    if status == 401 or status == 403:
                        logger.warning("lunarcrush_auth_failed", status=status)
                        self.disabled = True
                        return [], 1
                    if status == 429:
                        delay = min(backoff, _BACKOFF_CAP)
                        logger.warning(
                            "lunarcrush_rate_limited",
                            attempt=attempt,
                            retry_in_s=delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    if status >= 500:
                        logger.warning("lunarcrush_server_error", status=status)
                        return [], 1
                    text = await resp.text()
                    try:
                        payload = _json.loads(text)
                    except (ValueError, _json.JSONDecodeError):
                        logger.warning("lunarcrush_malformed_json", body=text[:120])
                        return [], 1
                    if not isinstance(payload, dict):
                        return [], 1
                    coins = payload.get("data", [])
                    if not isinstance(coins, list):
                        return [], 1
                    return coins, 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("lunarcrush_request_error")
                return [], 1
        # Ran out of retries.
        logger.warning("lunarcrush_giving_up_after_retries")
        return [], 1

    async def close(self) -> None:
        if self._owns_session and not self._session.closed:
            await self._session.close()
