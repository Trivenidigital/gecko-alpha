"""CoinGecko ingestion module -- polls /coins/markets and /search/trending."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import aiohttp
import structlog

from scout.models import CandidateToken
from scout.ratelimit import coingecko_limiter

if TYPE_CHECKING:
    from scout.config import Settings

logger = structlog.get_logger()

CG_BASE = "https://api.coingecko.com/api/v3"
MAX_RETRIES = 3
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)


async def _get_with_backoff(
    session: aiohttp.ClientSession,
    url: str,
    params: dict | None = None,
) -> dict | list | None:
    """GET with exponential backoff on 429. Returns parsed JSON or None."""
    for attempt in range(MAX_RETRIES + 1):
        await coingecko_limiter.acquire()
        try:
            async with session.get(url, params=params, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status == 429:
                    backoff = 2 ** (attempt + 1)
                    logger.warning("cg_429_backoff", attempt=attempt, backoff_s=backoff)
                    await coingecko_limiter.report_429(backoff_seconds=float(backoff))
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
    """Poll /coins/markets with two strategies to find micro-cap movers.

    Strategy 1: market_cap_asc — smallest listed coins (micro-cap fringe)
    Strategy 2: volume_desc — highest volume (catches pumps in progress)
    Union both lists before applying market cap filter.
    """
    logger.info("cg_fetch_attempted", endpoint="coins/markets")

    base_params = {
        "vs_currency": "usd",
        "per_page": "50",
        "page": "1",
        "sparkline": "false",
        "price_change_percentage": "1h,24h",
    }
    if settings.COINGECKO_API_KEY:
        base_params["x_cg_demo_api_key"] = settings.COINGECKO_API_KEY

    # Two parallel queries: smallest coins + highest volume
    params_small = {**base_params, "order": "market_cap_asc"}
    params_volume = {**base_params, "order": "volume_desc"}

    data_small, data_volume = await asyncio.gather(
        _get_with_backoff(session, f"{CG_BASE}/coins/markets", params_small),
        _get_with_backoff(session, f"{CG_BASE}/coins/markets", params_volume),
        return_exceptions=True,
    )

    # Union both result sets, dedup by CG id
    raw_by_id: dict[str, dict] = {}
    for data in [data_small, data_volume]:
        if isinstance(data, Exception) or not data or not isinstance(data, list):
            continue
        for raw in data:
            cg_id = raw.get("id", "")
            if cg_id and cg_id not in raw_by_id:
                raw_by_id[cg_id] = raw

    if not raw_by_id:
        logger.warning("cg_no_data", endpoint="coins/markets")
        return []

    tokens: list[CandidateToken] = []
    for raw in raw_by_id.values():
        token = CandidateToken.from_coingecko(raw)
        if token.market_cap_usd < settings.MIN_MARKET_CAP:
            continue
        if token.market_cap_usd > settings.MAX_MARKET_CAP:
            continue
        tokens.append(token)

    tokens.sort(key=lambda t: t.price_change_1h or 0, reverse=True)

    logger.info(
        "cg_candidates_returned",
        count=len(tokens),
        source="coins/markets",
        raw_fetched=len(raw_by_id),
        has_api_key=bool(settings.COINGECKO_API_KEY),
    )
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
