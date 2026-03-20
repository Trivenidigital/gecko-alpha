"""Async HTTP client for MiroFish REST API."""

import asyncio
import structlog

import aiohttp

from scout.config import Settings
from scout.exceptions import MiroFishConnectionError, MiroFishTimeoutError
from scout.models import MiroFishResult

logger = structlog.get_logger()


async def simulate(
    seed: dict,
    session: aiohttp.ClientSession,
    settings: Settings,
) -> MiroFishResult:
    """Run a MiroFish narrative simulation.

    Posts the seed payload to MiroFish's /simulate endpoint and
    returns a MiroFishResult.

    Raises:
        MiroFishTimeoutError: if the request exceeds MIROFISH_TIMEOUT_SEC
        MiroFishConnectionError: on connection failure or malformed response
    """
    url = f"{settings.MIROFISH_URL}/simulate"
    timeout = aiohttp.ClientTimeout(total=settings.MIROFISH_TIMEOUT_SEC)

    try:
        async with session.post(url, json=seed, timeout=timeout) as resp:
            if resp.status != 200:
                raise MiroFishConnectionError(
                    f"MiroFish returned HTTP {resp.status}"
                )
            data = await resp.json()
    except asyncio.TimeoutError:
        raise MiroFishTimeoutError(
            f"MiroFish simulation timed out after {settings.MIROFISH_TIMEOUT_SEC}s"
        )
    except aiohttp.ClientError as e:
        raise MiroFishConnectionError(f"MiroFish connection error: {e}") from e

    try:
        return MiroFishResult(
            narrative_score=data["narrative_score"],
            virality_class=data["virality_class"],
            summary=data["summary"],
        )
    except (KeyError, ValueError) as e:
        raise MiroFishConnectionError(
            f"MiroFish returned malformed response: {e}"
        ) from e
