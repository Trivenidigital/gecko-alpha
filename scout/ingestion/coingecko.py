"""CoinGecko ingestion module -- polls /coins/markets and /search/trending."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import aiohttp
import structlog

from scout.heartbeat import increment_mcap_null_with_price
from scout.models import CandidateToken
from scout.ratelimit import coingecko_limiter

if TYPE_CHECKING:
    from scout.config import Settings

logger = structlog.get_logger()

CG_BASE = "https://api.coingecko.com/api/v3"
MAX_RETRIES = 3
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)

# Module-level store for raw /coins/markets responses.
# Populated by fetch_top_movers(); consumed by main.py for price caching.
last_raw_markets: list[dict] = []
# Populated by fetch_trending(); consumed by main.py for price caching.
last_raw_trending: list[dict] = []
# Populated by fetch_by_volume(); consumed by main.py for price caching.
last_raw_by_volume: list[dict] = []


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
        "per_page": "250",
        "page": "1",
        "sparkline": "false",
        "price_change_percentage": "1h,24h,7d",
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

    # Store raw response for price cache consumption by main.py
    global last_raw_markets
    last_raw_markets = list(raw_by_id.values())

    tokens: list[CandidateToken] = []
    for raw in raw_by_id.values():
        # BL-075 Phase A (2026-05-03): track silent-rejection rate at the
        # mcap=0 floor. CoinGecko occasionally returns market_cap=null
        # for tokens with active price action (the RIV-shape blind spot).
        if (raw.get("market_cap") in (None, 0)) and (raw.get("current_price") or 0) > 0:
            increment_mcap_null_with_price()
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
    settings: Settings,
) -> list[CandidateToken]:
    """Poll /search/trending. Returns tokens with cg_trending_rank set.

    NOTE: No market cap filter is applied here. The trending endpoint does not
    return market cap data, and these tokens are valuable for the cg_trending_rank
    signal regardless of cap. The scorer's market_cap_range signal naturally
    handles filtering at the scoring stage.
    """
    params: dict[str, str] = {}
    if settings.COINGECKO_API_KEY:
        params["x_cg_demo_api_key"] = settings.COINGECKO_API_KEY

    data = await _get_with_backoff(
        session, f"{CG_BASE}/search/trending", params or None
    )
    if not data or not isinstance(data, dict):
        logger.warning("cg_no_data", endpoint="search/trending")
        return []

    coins = data.get("coins", [])
    ranked_items: list[tuple[int, dict]] = []
    ids: list[str] = []
    for rank, entry in enumerate(coins[:15], start=1):
        item = entry.get("item", {})
        cg_id = item.get("id")
        if not cg_id:
            continue
        ranked_items.append((rank, item))
        ids.append(cg_id)

    market_rows_by_id: dict[str, dict] = {}
    if ids:
        market_params = {
            "vs_currency": "usd",
            "ids": ",".join(ids),
            "sparkline": "false",
            "price_change_percentage": "1h,24h,7d",
        }
        if settings.COINGECKO_API_KEY:
            market_params["x_cg_demo_api_key"] = settings.COINGECKO_API_KEY
        market_data = await _get_with_backoff(
            session, f"{CG_BASE}/coins/markets", market_params
        )
        if isinstance(market_data, list):
            market_rows_by_id = {
                raw["id"]: raw
                for raw in market_data
                if isinstance(raw, dict) and raw.get("id")
            }

    tokens: list[CandidateToken] = []
    raw_trending: list[dict] = []
    for rank, item in ranked_items:
        cg_id = item.get("id", "unknown")
        raw = market_rows_by_id.get(cg_id)
        if raw:
            token = CandidateToken.from_coingecko(raw).model_copy(
                update={"cg_trending_rank": rank}
            )
            raw_trending.append(raw)
        else:
            token = CandidateToken(
                contract_address=cg_id,
                chain="coingecko",
                token_name=item.get("name", "Unknown"),
                ticker=item.get("symbol", "???"),
                cg_trending_rank=rank,
                holder_count=0,
                holder_growth_1h=0,
            )
            item_data = item.get("data", {})
            if item_data:
                raw_trending.append(
                    {
                        "id": cg_id,
                        "symbol": item.get("symbol"),
                        "name": item.get("name"),
                        "current_price": item_data.get("price"),
                        "price_change_percentage_24h": item_data.get(
                            "price_change_percentage_24h", {}
                        ).get("usd"),
                        "market_cap": None,
                    }
                )
        tokens.append(token)

    global last_raw_trending
    last_raw_trending = raw_trending

    logger.info("cg_candidates_fetched", count=len(tokens), source="search/trending")
    return tokens


async def fetch_by_volume(
    session: aiohttp.ClientSession,
    settings: "Settings",
) -> list[CandidateToken]:
    """Fetch tokens sorted by volume (catches tokens with activity spike regardless of price change).

    Fetches page 1 + page 2 in parallel (top 500 by volume) and unions the
    results. This broadens the universe so the gainers tracker (which sorts
    the combined raw markets by 24h change and takes top 20) can surface
    mid-cap gainers that fall outside the top 250.

    Uses a wider market cap range than fetch_top_movers to catch mid-cap tokens
    like CommonWealth that have high volume but aren't in the micro-cap fringe.
    The upper bound is the LOSERS/GAINERS tracker max (500M) rather than the
    strict MAX_MARKET_CAP used for the main pipeline.
    """
    logger.info("cg_fetch_attempted", endpoint="coins/markets:volume_desc")

    base_params = {
        "vs_currency": "usd",
        "order": "volume_desc",
        "per_page": "250",
        "sparkline": "false",
        "price_change_percentage": "1h,24h,7d",
    }
    if settings.COINGECKO_API_KEY:
        base_params["x_cg_demo_api_key"] = settings.COINGECKO_API_KEY

    page_count = max(1, int(settings.COINGECKO_VOLUME_SCAN_PAGES))
    pages = await asyncio.gather(
        *[
            _get_with_backoff(
                session,
                f"{CG_BASE}/coins/markets",
                {**base_params, "page": str(page)},
            )
            for page in range(1, page_count + 1)
        ],
        return_exceptions=True,
    )

    raw_by_id: dict[str, dict] = {}
    for data in pages:
        if isinstance(data, Exception) or not data or not isinstance(data, list):
            continue
        for raw in data:
            cg_id = raw.get("id", "")
            if cg_id and cg_id not in raw_by_id:
                raw_by_id[cg_id] = raw

    if not raw_by_id:
        logger.warning("cg_no_data", endpoint="coins/markets:volume_desc")
        return []

    # Store raw response for price cache & losers/gainers tracker
    global last_raw_by_volume
    last_raw_by_volume = list(raw_by_id.values())

    tokens: list[CandidateToken] = []
    for raw in raw_by_id.values():
        # BL-075 Phase A (2026-05-03): same silent-rejection counter as
        # fetch_top_movers — tracks tokens with mcap=null/0 + price>0.
        if (raw.get("market_cap") in (None, 0)) and (raw.get("current_price") or 0) > 0:
            increment_mcap_null_with_price()
        token = CandidateToken.from_coingecko(raw)
        # Use wider cap range: keep anything with mcap > MIN_MARKET_CAP
        # (upper bound filtering is done at scoring/gate stage)
        if token.market_cap_usd < settings.MIN_MARKET_CAP:
            continue
        tokens.append(token)

    # Sort by volume descending
    tokens.sort(key=lambda t: t.volume_24h_usd or 0, reverse=True)

    logger.info(
        "cg_volume_scan_returned",
        count=len(tokens),
        source="coins/markets:volume_desc",
        raw_fetched=len(raw_by_id),
    )
    return tokens
