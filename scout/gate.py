"""Conviction gate: combines quant and narrative scores to decide alerts."""

import structlog

import aiohttp

from scout.config import Settings
from scout.db import Database
from scout.exceptions import MiroFishConnectionError, MiroFishTimeoutError
from scout.mirofish.client import simulate
from scout.mirofish.fallback import score_narrative_fallback
from scout.mirofish.seed_builder import build_seed
from scout.models import CandidateToken

logger = structlog.get_logger()


async def evaluate(
    token: CandidateToken,
    db: Database,
    session: aiohttp.ClientSession,
    settings: Settings,
) -> tuple[bool, float, CandidateToken]:
    """Evaluate a candidate token through the conviction gate.

    Returns:
        (should_alert, conviction_score, updated_token)
    """
    quant_score = token.quant_score or 0
    narrative_score = None

    # Only run MiroFish if quant_score passes MIN_SCORE and daily cap not reached
    if quant_score >= settings.MIN_SCORE:
        daily_count = await db.get_daily_mirofish_count()
        if daily_count < settings.MAX_MIROFISH_JOBS_PER_DAY:
            narrative_score = await _get_narrative_score(token, session, db, settings)

    # Compute conviction score
    if narrative_score is not None:
        conviction = (quant_score * settings.QUANT_WEIGHT) + (narrative_score * settings.NARRATIVE_WEIGHT)
    else:
        conviction = float(quant_score)

    should_alert = conviction >= settings.CONVICTION_THRESHOLD

    # Update token with scores
    updated = token.model_copy(update={
        "narrative_score": narrative_score,
        "conviction_score": conviction,
    })

    return (should_alert, conviction, updated)


async def _get_narrative_score(
    token: CandidateToken,
    session: aiohttp.ClientSession,
    db: Database,
    settings: Settings,
) -> int | None:
    """Run MiroFish simulation with Claude fallback."""
    seed = build_seed(token)

    try:
        result = await simulate(seed, session, settings)
        await db.log_mirofish_job(token.contract_address)
        return result.narrative_score
    except (MiroFishTimeoutError, MiroFishConnectionError) as e:
        logger.warning("MiroFish failed, falling back to Claude", contract_address=token.contract_address, error=str(e))
        try:
            result = await score_narrative_fallback(seed, settings.ANTHROPIC_API_KEY)
            await db.log_mirofish_job(token.contract_address)
            return result.narrative_score
        except Exception as e:
            logger.error("Claude fallback also failed", contract_address=token.contract_address, error=str(e))
            return None
