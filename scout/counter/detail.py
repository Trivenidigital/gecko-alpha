"""CoinGecko coin detail fetcher with in-memory TTL cache."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import aiohttp
import structlog

from scout import cg_api
from scout.coingecko_budget import BUCKET_DISCOVERY, governed_cg_call
from scout.ratelimit import coingecko_limiter

logger = structlog.get_logger()
# calibration era: undocumented -- see BL-NEW-CALIBRATION-ERA-DOC
CACHE_TTL_SECONDS = 1800  # 30 minutes

_detail_cache: dict[str, tuple[datetime, dict]] = {}


async def fetch_coin_detail(
    session: aiohttp.ClientSession,
    coin_id: str,
    api_key: str = "",
    api_tier: str = "demo",
    settings=None,
) -> dict | None:
    """Fetch full coin detail from CoinGecko, with 30-min in-memory cache.

    Returns the parsed JSON dict on success, or None on 404/429/error.
    """
    now = datetime.now(timezone.utc)

    # Check cache
    if coin_id in _detail_cache:
        cached_at, cached_data = _detail_cache[coin_id]
        age_seconds = (now - cached_at).total_seconds()
        if age_seconds < CACHE_TTL_SECONDS:
            logger.debug("cg_detail_cache_hit", coin_id=coin_id)
            return cached_data

    params: dict[str, str] = {
        "localization": "false",
        "tickers": "false",
        "market_data": "true",
        "community_data": "true",
        "developer_data": "true",
        "sparkline": "false",
    }
    headers: dict[str, str] = dict(cg_api.auth_headers(api_key, api_tier))

    url = f"{cg_api.base_url(api_tier)}/coins/{coin_id}"
    # Governed: hand-rolled call, so it borrows the accounting and
    # enforcement instead of _get_with_backoff. DISCOVERY bucket
    # (non-critical) so it can never eat the re-pricing reserve.
    _call = governed_cg_call(BUCKET_DISCOVERY, settings)
    if not _call.allowed:
        return None
    # finish(None) is guaranteed below so a CONNECTION/TIMEOUT failure —
    # which never reaches a response and so never reaches finish(status) —
    # still records one attempt with zero credits. Counting only the
    # request paths that produced a response makes a lane that is failing
    # at the transport layer invisible in the attempt rate.
    await coingecko_limiter.acquire()
    try:
        async with session.get(url, params=params, headers=headers) as resp:
            _call.finish(resp.status)
            if resp.status == 429:
                logger.warning("cg_detail_rate_limited", coin_id=coin_id)
                await coingecko_limiter.report_429()
                return None
            if resp.status == 404:
                logger.info("cg_detail_not_found", coin_id=coin_id)
                return None
            if resp.status >= 400:
                logger.warning(
                    "cg_detail_http_error", coin_id=coin_id, status=resp.status
                )
                return None

            data: dict = await resp.json()
    except Exception as exc:
        logger.warning("cg_detail_request_error", coin_id=coin_id, error=str(exc))
        return None

    # Cache and return
    _detail_cache[coin_id] = (now, data)
    logger.debug("cg_detail_fetched", coin_id=coin_id)

    return data


def extract_counter_data(detail: dict) -> dict:
    """Extract counter-narrative-relevant fields from a CoinGecko detail response.

    Returns a flat dict with safe defaults for missing data.
    """
    developer_data: dict[str, Any] = detail.get("developer_data", {}) or {}
    community_data: dict[str, Any] = detail.get("community_data", {}) or {}
    market_data: dict[str, Any] = detail.get("market_data", {}) or {}

    return {
        "commits_4w": developer_data.get("commit_count_4_weeks", 0) or 0,
        "reddit_subscribers": community_data.get("reddit_subscribers", 0) or 0,
        "telegram_users": community_data.get("telegram_channel_user_count", 0) or 0,
        "sentiment_up_pct": detail.get("sentiment_votes_up_percentage", 50.0) or 50.0,
        "price_change_7d": market_data.get("price_change_percentage_7d", 0) or 0,
        "price_change_30d": market_data.get("price_change_percentage_30d", 0) or 0,
        "watchlist_portfolio_users": detail.get("watchlist_portfolio_users", 0) or 0,
    }
