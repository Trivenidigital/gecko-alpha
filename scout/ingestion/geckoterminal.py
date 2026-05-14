"""GeckoTerminal API poller for trending pools."""

import asyncio

import aiohttp
import structlog

from scout.config import Settings
from scout.heartbeat import IngestSourceSample
from scout.models import CandidateToken

logger = structlog.get_logger()

GECKO_BASE = "https://api.geckoterminal.com/api/v2"
MAX_ATTEMPTS = 3
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)
_last_watchdog_samples: list[IngestSourceSample] = []
_last_error_by_chain: dict[str, str] = {}


def get_last_watchdog_samples() -> list[IngestSourceSample]:
    return list(_last_watchdog_samples)


def clear_watchdog_samples() -> None:
    _last_watchdog_samples.clear()
    _last_error_by_chain.clear()


async def _get_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    chain: str,
    max_attempts: int = MAX_ATTEMPTS,
) -> list | dict | None:
    """GET GeckoTerminal JSON with bounded retries on HTTP 429 / 5xx."""
    _last_error_by_chain.pop(chain, None)
    for attempt in range(1, max_attempts + 1):
        try:
            async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status == 429 or resp.status >= 500:
                    if attempt < max_attempts:
                        wait = 2 ** (attempt - 1)
                        logger.warning(
                            "geckoterminal_retrying",
                            chain=chain,
                            url=url,
                            status=resp.status,
                            wait=wait,
                            attempt=attempt,
                            max_attempts=max_attempts,
                        )
                        await asyncio.sleep(wait)
                        continue
                    logger.warning(
                        "geckoterminal_retries_exhausted",
                        chain=chain,
                        url=url,
                        status=resp.status,
                        max_attempts=max_attempts,
                    )
                    _last_error_by_chain[chain] = f"http_{resp.status}"
                    return None
                if resp.status != 200:
                    logger.warning(
                        "geckoterminal_non_retryable_status",
                        chain=chain,
                        url=url,
                        status=resp.status,
                    )
                    _last_error_by_chain[chain] = f"http_{resp.status}"
                    return None
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning(
                "geckoterminal_request_error",
                chain=chain,
                url=url,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            _last_error_by_chain[chain] = type(exc).__name__
            return None
    return None


async def fetch_trending_pools(
    session: aiohttp.ClientSession, settings: Settings
) -> list[CandidateToken]:
    """Fetch trending pools from GeckoTerminal for all configured chains."""
    candidates: list[CandidateToken] = []
    _last_watchdog_samples.clear()

    for chain in settings.CHAINS:
        url = f"{GECKO_BASE}/networks/{chain}/trending_pools"
        data = await _get_json(session, url, chain=chain)
        if not isinstance(data, dict):
            _last_watchdog_samples.append(
                IngestSourceSample(
                    source=f"geckoterminal:{chain}",
                    raw_count=0,
                    usable_count=0,
                    error=_last_error_by_chain.get(chain, "no_raw_data"),
                )
            )
            continue

        # NB: GT returns trending_pools in rank order; idx 0 = most-traded.
        raw_pools = data.get("data", [])
        chain_usable_count = 0
        for idx, pool in enumerate(raw_pools):
            try:
                token = CandidateToken.from_geckoterminal(pool, chain=chain)
                token = token.model_copy(update={"gt_trending_rank": idx + 1})
                if (
                    settings.MIN_MARKET_CAP
                    <= token.market_cap_usd
                    <= settings.MAX_MARKET_CAP
                ):
                    candidates.append(token)
                    chain_usable_count += 1
            except Exception as e:
                logger.warning("Failed to parse GeckoTerminal pool", error=str(e))
                continue
        _last_watchdog_samples.append(
            IngestSourceSample(
                source=f"geckoterminal:{chain}",
                raw_count=len(raw_pools),
                usable_count=chain_usable_count,
            )
        )

    return candidates
