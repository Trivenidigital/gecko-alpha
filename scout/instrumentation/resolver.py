"""I1 resolver — CoinGecko ``/coins/{id}.platforms`` -> ``contract_coin_map``.

Observe-only and best-effort: it never raises into the pipeline, never blocks
ingest or the gate, and feeds nothing into the scorer. Linkage is necessarily
*retroactive* (a DEX mint has no coin_id until CG lists it), so this only ever
maps DEX tokens that eventually CG-list (see the survivorship caveat in the
spec). Reuses the battle-tested ``fetch_coin_detail`` client.
"""

import aiohttp
import structlog

from scout.config import Settings
from scout.counter.detail import fetch_coin_detail
from scout.db import Database

logger = structlog.get_logger(__name__)


async def resolve_coin_platforms(
    coin_id: str,
    session: aiohttp.ClientSession,
    db: Database,
    settings: Settings,
) -> int | None:
    """Resolve one CG coin_id's platform contracts into ``contract_coin_map``.

    Returns the number of contracts recorded, or ``None`` on fetch failure.
    Best-effort: all errors are swallowed + logged.
    """
    try:
        detail = await fetch_coin_detail(
            session=session,
            coin_id=coin_id,
            api_key=getattr(settings, "COINGECKO_API_KEY", "") or "",
        )
    except Exception:
        logger.warning("dex_resolver_fetch_failed", coin_id=coin_id)
        return None
    if not detail:
        return None

    platforms = detail.get("platforms") or {}
    recorded = 0
    for chain, contract in platforms.items():
        if isinstance(contract, str) and contract.strip():
            try:
                await db.record_contract_coin_map(
                    contract.strip(), str(chain), coin_id, "platforms", "high"
                )
                recorded += 1
            except Exception:
                logger.exception(
                    "dex_resolver_record_failed", coin_id=coin_id, chain=chain
                )
    return recorded


async def run_resolver_pass(
    coin_ids: list[str],
    session: aiohttp.ClientSession,
    db: Database,
    settings: Settings,
) -> dict:
    """Resolve up to ``DEX_RESOLVER_BUDGET_PER_CYCLE`` not-yet-resolved coin_ids.

    Caps ``/coins/{id}`` calls per cycle so the shared 30 req/min limiter stays
    mostly free for ingestion. Already-resolved coin_ids are skipped (TTL).
    Observe-only; returns a summary for logging.
    """
    budget = int(getattr(settings, "DEX_RESOLVER_BUDGET_PER_CYCLE", 5))
    ttl = int(getattr(settings, "DEX_RESOLVER_NEGATIVE_TTL_SEC", 3600))
    attempted = 0
    recorded = 0
    failed = 0
    seen: set[str] = set()
    for coin_id in coin_ids:
        if attempted >= budget:
            break
        if not coin_id or coin_id in seen:
            continue
        seen.add(coin_id)
        try:
            # Skip already-resolved coin_ids AND ones that failed within the
            # negative-result TTL (so persistent 404s don't drain budget).
            if await db.coin_id_resolved(coin_id) or await db.coin_id_attempt_fresh(
                coin_id, ttl
            ):
                continue
        except Exception:
            logger.exception("dex_resolver_ttl_check_failed", coin_id=coin_id)
            continue
        n = await resolve_coin_platforms(coin_id, session, db, settings)
        attempted += 1
        if n is None:
            # Failed/unknown fetch -> record a negative-result marker so the TTL
            # skips it next cycle; failure is observable via this row + the log.
            failed += 1
            try:
                await db.record_resolver_attempt(coin_id)
            except Exception:
                logger.exception(
                    "dex_resolver_attempt_record_failed", coin_id=coin_id
                )
        elif n:
            recorded += n
    logger.info(
        "dex_resolver_pass", attempted=attempted, recorded=recorded, failed=failed
    )
    return {"attempted": attempted, "recorded": recorded, "failed": failed}
