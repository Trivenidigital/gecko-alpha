"""DexScreener API poller for trending tokens."""

import asyncio
from collections import defaultdict

import aiohttp
import structlog

from scout.config import Settings
from scout.heartbeat import IngestSourceSample
from scout.models import CandidateToken

logger = structlog.get_logger()

BOOST_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
TOKEN_URL = "https://api.dexscreener.com/tokens/v1"

MAX_RETRIES = 3
MAX_CONCURRENT = 5
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)
_last_watchdog_samples: dict[str, IngestSourceSample] = {}


def _set_watchdog_sample(sample: IngestSourceSample) -> None:
    _last_watchdog_samples[sample.source] = sample


def get_last_watchdog_samples() -> list[IngestSourceSample]:
    return list(_last_watchdog_samples.values())


def clear_watchdog_samples() -> None:
    _last_watchdog_samples.clear()


async def _get_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    retries: int = MAX_RETRIES,
) -> list | dict | None:
    """GET a URL with exponential backoff on 429 / 5xx."""
    for attempt in range(retries):
        try:
            async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status == 429 or resp.status >= 500:
                    wait = 2**attempt
                    logger.warning(
                        "DexScreener returned error, retrying",
                        url=url,
                        status=resp.status,
                        wait=wait,
                        attempt=attempt + 1,
                        retries=retries,
                    )
                    await asyncio.sleep(wait)
                    continue
                if resp.status != 200:
                    logger.warning(
                        "DexScreener returned error", url=url, status=resp.status
                    )
                    return None
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            wait = 2**attempt
            logger.warning(
                "DexScreener request failed, retrying",
                url=url,
                error=str(exc),
                wait=wait,
            )
            await asyncio.sleep(wait)
    logger.warning("DexScreener failed after retries", url=url, retries=retries)
    return None


async def fetch_trending(
    session: aiohttp.ClientSession,
    settings: Settings,
) -> list[CandidateToken]:
    """Fetch trending tokens from DexScreener.

    1. Get boosted/trending token addresses from the boosts endpoint.
    2. For each, fetch full pair data from the tokens endpoint.
    3. Filter by market cap range and token age.
    4. Return list of CandidateToken.
    """
    clear_watchdog_samples()
    _set_watchdog_sample(
        IngestSourceSample(source="dexscreener:boosts", raw_count=0, error="pending")
    )
    boosts = await _get_json(session, BOOST_URL)
    if not boosts:
        _set_watchdog_sample(
            IngestSourceSample(
                source="dexscreener:boosts",
                raw_count=0,
                usable_count=0,
                error="no_boosts_or_fetch_failed",
            )
        )
        return []
    _set_watchdog_sample(
        IngestSourceSample(
            source="dexscreener:boosts",
            raw_count=len(boosts),
            usable_count=len(boosts),
        )
    )
    _set_watchdog_sample(
        IngestSourceSample(source="dexscreener:tokens", raw_count=0, error="pending")
    )

    # Group token addresses by chain for batched lookups
    chain_tokens: dict[str, list[str]] = defaultdict(list)
    for entry in boosts:
        chain = entry.get("chainId", "")
        address = entry.get("tokenAddress", "")
        if chain and address and address not in chain_tokens[chain]:
            chain_tokens[chain].append(address)

    sem = asyncio.Semaphore(MAX_CONCURRENT)

    async def _fetch_one(chain: str, address: str) -> tuple[bool, list[CandidateToken]]:
        async with sem:
            url = f"{TOKEN_URL}/{chain}/{address}"
            pairs = await _get_json(session, url)
            if not pairs or not isinstance(pairs, list):
                return False, []

            results: list[CandidateToken] = []
            for pair_data in pairs:
                fdv = float(pair_data.get("fdv") or 0)
                if not (settings.MIN_MARKET_CAP <= fdv <= settings.MAX_MARKET_CAP):
                    continue

                try:
                    token = CandidateToken.from_dexscreener(pair_data)
                except Exception:
                    logger.exception("Failed to parse DexScreener pair data")
                    continue

                results.append(token)
            return True, results

    tasks = [
        _fetch_one(chain, addr)
        for chain, addrs in chain_tokens.items()
        for addr in addrs
    ]
    gather_results = await asyncio.gather(*tasks, return_exceptions=True)

    candidates: list[CandidateToken] = []
    successful_detail_payloads = 0
    for result in gather_results:
        if isinstance(result, Exception):
            logger.warning("Token fetch failed", error=str(result))
            continue
        detail_success, result_candidates = result
        if detail_success:
            successful_detail_payloads += 1
        candidates.extend(result_candidates)

    logger.info(
        "DexScreener: found candidates",
        candidate_count=len(candidates),
        boost_count=len(boosts),
    )
    _set_watchdog_sample(
        IngestSourceSample(
            source="dexscreener:tokens",
            raw_count=successful_detail_payloads,
            usable_count=len(candidates),
            error=None if successful_detail_payloads else "no_detail_payloads",
        )
    )
    return candidates
