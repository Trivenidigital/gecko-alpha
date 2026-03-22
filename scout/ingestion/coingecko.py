"""CoinGecko ingestion module -- polls /coins/markets and /search/trending."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import TYPE_CHECKING

import structlog

from scout.models import CandidateToken

if TYPE_CHECKING:
    import aiohttp
    from scout.config import Settings

logger = structlog.get_logger()

CG_BASE = "https://api.coingecko.com/api/v3"
MAX_RETRIES = 3
_call_timestamps: deque[float] = deque()
_rate_lock = asyncio.Lock()


async def _throttle() -> None:
    """Enforce 30 calls/min rate limit for CoinGecko free Demo tier.

    Uses asyncio.Lock to prevent concurrent coroutines from exceeding the cap.
    """
    async with _rate_lock:
        now = time.monotonic()
        # Remove timestamps older than 60 seconds
        while _call_timestamps and _call_timestamps[0] < now - 60:
            _call_timestamps.popleft()
        if len(_call_timestamps) >= 30:
            sleep_time = 60 - (now - _call_timestamps[0])
            if sleep_time > 0:
                logger.warning("cg_rate_limit_hit", sleep_seconds=round(sleep_time, 1))
                await asyncio.sleep(sleep_time)
                # Re-prune after sleep so the window is recalculated accurately
                post_sleep = time.monotonic()
                while _call_timestamps and _call_timestamps[0] < post_sleep - 60:
                    _call_timestamps.popleft()
        _call_timestamps.append(time.monotonic())


async def _get_with_backoff(
    session: aiohttp.ClientSession,
    url: str,
    params: dict | None = None,
) -> dict | list | None:
    """GET with exponential backoff on 429. Returns parsed JSON or None."""
    for attempt in range(MAX_RETRIES + 1):
        await _throttle()
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 429:
                    backoff = 2 ** (attempt + 1)
                    logger.warning(
                        "cg_429_backoff", attempt=attempt, backoff_s=backoff
                    )
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(backoff)
                        continue
                    return None
                if resp.status >= 400:
                    logger.warning("cg_http_error", status=resp.status, url=url)
                    return None
                return await resp.json()
        except Exception as exc:
            logger.warning("cg_request_error", error=str(exc), url=url)
            return None
    return None


async def fetch_top_movers(
    session: aiohttp.ClientSession,
    settings: Settings,
) -> list[CandidateToken]:
    """Poll /coins/markets sorted by 1h change. Returns filtered CandidateTokens."""
    params = {
        "vs_currency": "usd",
        "order": "volume_desc",  # Free tier: volume_desc is valid; we sort by 1h change client-side
        "per_page": "50",
        "page": "1",
        "sparkline": "false",
        "price_change_percentage": "1h,24h",
    }
    if settings.COINGECKO_API_KEY:
        params["x_cg_demo_api_key"] = settings.COINGECKO_API_KEY
    data = await _get_with_backoff(session, f"{CG_BASE}/coins/markets", params)
    if not data or not isinstance(data, list):
        logger.warning("cg_no_data", endpoint="coins/markets")
        return []

    tokens: list[CandidateToken] = []
    for raw in data:
        token = CandidateToken.from_coingecko(raw)
        # Apply market cap filter
        if token.market_cap_usd < settings.MIN_MARKET_CAP:
            continue
        if token.market_cap_usd > settings.MAX_MARKET_CAP:
            continue
        tokens.append(token)

    # Sort by 1h price change descending (client-side since free API may not support this order)
    tokens.sort(key=lambda t: t.price_change_1h or 0, reverse=True)

    logger.info("cg_candidates_fetched", count=len(tokens), source="coins/markets")
    return tokens


async def fetch_trending(
    session: aiohttp.ClientSession,
    settings: Settings,  # kept for interface consistency with other ingestion sources
) -> list[CandidateToken]:
    """Poll /search/trending. Returns tokens with cg_trending_rank set.

    NOTE: No market cap filter is applied here. The trending endpoint does not
    return market cap data, and these tokens are valuable for the cg_trending_rank
    signal regardless of cap. The scorer's market_cap_range signal naturally
    handles filtering at the scoring stage.
    """
    data = await _get_with_backoff(session, f"{CG_BASE}/search/trending")
    if not data or not isinstance(data, dict):
        logger.warning("cg_no_data", endpoint="search/trending")
        return []

    coins = data.get("coins", [])
    tokens: list[CandidateToken] = []
    for rank, entry in enumerate(coins[:15]):
        item = entry.get("item", {})
        cg_id = item.get("id", "unknown")
        token = CandidateToken(
            contract_address=cg_id,
            chain="coingecko",
            token_name=item.get("name", "Unknown"),
            ticker=item.get("symbol", "???"),
            cg_trending_rank=rank + 1,  # 1-indexed: position 1 = most trending
            holder_count=0,
            holder_growth_1h=0,
        )
        tokens.append(token)

    logger.info("cg_candidates_fetched", count=len(tokens), source="search/trending")
    return tokens
