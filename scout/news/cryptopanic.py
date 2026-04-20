"""Async CryptoPanic fetcher + candidate enricher (BL-053)."""

from __future__ import annotations

import asyncio

import aiohttp
import structlog

from scout.config import Settings
from scout.news.schemas import CryptoPanicPost, parse_post

logger = structlog.get_logger(__name__)

BASE_URL = "https://cryptopanic.com/api/v1/posts/"
MAX_RETRIES = 3
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)


async def fetch_cryptopanic_posts(
    session: aiohttp.ClientSession,
    settings: Settings,
) -> list[CryptoPanicPost]:
    """Fetch hot/rising/etc posts from CryptoPanic v1.

    Short-circuits to [] when the feature is disabled or the token is empty.
    Never raises - all network / parse / auth errors return [].
    """
    if not settings.CRYPTOPANIC_ENABLED:
        return []
    if not settings.CRYPTOPANIC_API_TOKEN:
        logger.warning("cryptopanic_auth_missing")
        return []

    params = {
        "auth_token": settings.CRYPTOPANIC_API_TOKEN,
        "filter": settings.CRYPTOPANIC_FETCH_FILTER,
        "public": "true",
    }

    logger.info("cryptopanic_fetch_started", filter=settings.CRYPTOPANIC_FETCH_FILTER)

    raw_results: list[dict] = []
    for attempt in range(MAX_RETRIES):
        try:
            async with session.get(
                BASE_URL, params=params, timeout=REQUEST_TIMEOUT
            ) as resp:
                if resp.status in (401, 403):
                    logger.warning(
                        "cryptopanic_fetch_failed",
                        status=resp.status,
                        error="auth",
                    )
                    return []
                if resp.status == 429 or resp.status >= 500:
                    wait = 2 ** (attempt + 1)
                    logger.warning(
                        "cryptopanic_retry",
                        status=resp.status,
                        wait=wait,
                        attempt=attempt + 1,
                    )
                    await asyncio.sleep(wait)
                    continue
                if resp.status != 200:
                    logger.warning("cryptopanic_fetch_failed", status=resp.status)
                    return []
                try:
                    data = await resp.json()
                except Exception as e:
                    logger.warning("cryptopanic_fetch_failed", error=f"json:{e!s}")
                    return []
                raw_results = data.get("results") or []
                break
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            wait = 2 ** (attempt + 1)
            logger.warning(
                "cryptopanic_retry",
                error=str(e),
                wait=wait,
                attempt=attempt + 1,
            )
            await asyncio.sleep(wait)
    else:
        logger.warning("cryptopanic_fetch_failed", error="retries_exhausted")
        return []

    posts: list[CryptoPanicPost] = []
    seen: set[int] = set()
    for raw in raw_results:
        post = parse_post(raw)
        if post is None:
            continue
        if post.post_id in seen:
            continue
        seen.add(post.post_id)
        posts.append(post)

    logger.info("cryptopanic_fetch_completed", count=len(posts))
    return posts
