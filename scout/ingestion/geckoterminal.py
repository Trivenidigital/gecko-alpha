"""GeckoTerminal API poller for trending pools."""

import asyncio

import aiohttp
import structlog

from scout.config import Settings
from scout.models import CandidateToken

logger = structlog.get_logger()

GECKO_BASE = "https://api.geckoterminal.com/api/v2"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)


async def fetch_trending_pools(
    session: aiohttp.ClientSession, settings: Settings
) -> list[CandidateToken]:
    """Fetch trending pools from GeckoTerminal for all configured chains."""
    candidates: list[CandidateToken] = []

    for chain in settings.CHAINS:
        url = f"{GECKO_BASE}/networks/{chain}/trending_pools"
        try:
            async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status != 200:
                    logger.warning(
                        "GeckoTerminal returned error", chain=chain, status=resp.status
                    )
                    continue
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("GeckoTerminal request error", chain=chain, error=str(e))
            continue

        # NB: GT returns trending_pools in rank order; idx 0 = most-traded.
        for idx, pool in enumerate(data.get("data", [])):
            try:
                token = CandidateToken.from_geckoterminal(pool, chain=chain)
                token = token.model_copy(update={"gt_trending_rank": idx + 1})
                if (
                    settings.MIN_MARKET_CAP
                    <= token.market_cap_usd
                    <= settings.MAX_MARKET_CAP
                ):
                    candidates.append(token)
            except Exception as e:
                logger.warning("Failed to parse GeckoTerminal pool", error=str(e))
                continue

    return candidates
