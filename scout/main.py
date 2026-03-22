"""CoinPump Scout -- main pipeline entry point."""

import argparse
import asyncio
import logging
import signal
import sys
import time

import aiohttp
import structlog

from scout.aggregator import aggregate
from scout.alerter import send_alert
from scout.config import Settings
from scout.db import Database
from scout.gate import evaluate
from scout.ingestion.coingecko import fetch_top_movers as cg_fetch_top_movers
from scout.ingestion.coingecko import fetch_trending as cg_fetch_trending
from scout.ingestion.dexscreener import fetch_trending
from scout.ingestion.geckoterminal import fetch_trending_pools
from scout.ingestion.holder_enricher import enrich_holders
from scout.safety import is_safe
from scout.scorer import score

logger = structlog.get_logger()


async def run_cycle(
    settings: Settings,
    db: Database,
    session: aiohttp.ClientSession,
    dry_run: bool = False,
) -> dict:
    """Run one full pipeline cycle.

    Returns stats dict with tokens_scanned, candidates_promoted, alerts_fired, etc.
    """
    stats = {"tokens_scanned": 0, "candidates_promoted": 0, "alerts_fired": 0}

    # Stage 1: Parallel ingestion
    dex_tokens, gecko_tokens, cg_movers, cg_trending = await asyncio.gather(
        fetch_trending(session, settings),
        fetch_trending_pools(session, settings),
        cg_fetch_top_movers(session, settings),
        cg_fetch_trending(session, settings),
        return_exceptions=True,
    )
    # Handle exceptions from gather
    if isinstance(dex_tokens, Exception):
        logger.warning("DexScreener ingestion failed", error=str(dex_tokens))
        dex_tokens = []
    if isinstance(gecko_tokens, Exception):
        logger.warning("GeckoTerminal ingestion failed", error=str(gecko_tokens))
        gecko_tokens = []
    if isinstance(cg_movers, Exception):
        logger.warning("CoinGecko markets ingestion failed", error=str(cg_movers))
        cg_movers = []
    if isinstance(cg_trending, Exception):
        logger.warning("CoinGecko trending ingestion failed", error=str(cg_trending))
        cg_trending = []

    # Stage 2: Aggregate
    all_candidates = aggregate(
        list(dex_tokens) + list(gecko_tokens) + list(cg_movers) + list(cg_trending)
    )
    stats["tokens_scanned"] = len(all_candidates)

    # Enrich holders (concurrently)
    enriched = list(await asyncio.gather(
        *[enrich_holders(token, session, settings) for token in all_candidates]
    ))

    # Compute holder_growth_1h from previous snapshots
    for i, token in enumerate(enriched):
        if token.holder_count > 0:
            prev = await db.get_previous_holder_count(token.contract_address)
            if prev is not None:
                growth = token.holder_count - prev
                enriched[i] = token.model_copy(update={"holder_growth_1h": max(0, growth)})
            await db.log_holder_snapshot(token.contract_address, token.holder_count)

    # Stage 3: Score
    scored = []
    for token in enriched:
        historical_scores = await db.get_recent_scores(token.contract_address, limit=3)
        points, signals = score(token, settings, historical_scores=historical_scores)
        updated = token.model_copy(update={"quant_score": points})
        await db.upsert_candidate(updated)
        await db.log_score(token.contract_address, points)
        if points >= settings.MIN_SCORE:
            scored.append((updated, signals))
            stats["candidates_promoted"] += 1

    # Stages 4-5: Gate (MiroFish + conviction)
    for token, signals in scored:
        should_alert, conviction, gated_token = await evaluate(
            token, db, session, settings, signals_fired=signals,
        )

        if not should_alert:
            continue

        # Stage 6: Safety check + alert
        if not await is_safe(
            gated_token.contract_address, gated_token.chain, session
        ):
            logger.warning(
                "Token failed safety check", token=gated_token.contract_address
            )
            continue

        if dry_run:
            logger.info(
                "DRY RUN: would alert",
                token=gated_token.token_name,
                conviction=conviction,
            )
            continue

        await send_alert(gated_token, signals, session, settings)
        await db.log_alert(
            gated_token.contract_address, gated_token.chain, conviction
        )
        stats["alerts_fired"] += 1

    return stats


async def main() -> None:
    """Main entry point with CLI arg parsing and graceful shutdown."""
    parser = argparse.ArgumentParser(description="CoinPump Scout scanner")
    parser.add_argument(
        "--dry-run", action="store_true", help="Run without sending alerts"
    )
    parser.add_argument(
        "--cycles", type=int, default=0, help="Number of cycles (0=infinite)"
    )
    args = parser.parse_args()

    # Configure structlog
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )

    settings = Settings()
    db = Database(settings.DB_PATH)
    await db.initialize()

    shutdown_event = asyncio.Event()

    def _shutdown(sig, frame):
        logger.info("Shutdown signal received", signal=sig)
        shutdown_event.set()

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, _shutdown)
    try:
        signal.signal(signal.SIGTERM, _shutdown)
    except (OSError, ValueError):
        pass  # SIGTERM not supported on Windows

    cycle_count = 0
    cumulative = {"tokens_scanned": 0, "candidates_promoted": 0, "alerts_fired": 0}
    last_heartbeat = time.monotonic()
    heartbeat_interval = 300  # 5 minutes

    try:
        async with aiohttp.ClientSession() as session:
            while not shutdown_event.is_set():
                try:
                    stats = await run_cycle(
                        settings, db, session, dry_run=args.dry_run
                    )
                    logger.info("Cycle complete", **stats)
                    for k in cumulative:
                        cumulative[k] += stats.get(k, 0)
                except Exception as e:
                    logger.error("Cycle failed", error=str(e))

                cycle_count += 1

                # Heartbeat logging every 5 minutes
                now = time.monotonic()
                if now - last_heartbeat >= heartbeat_interval:
                    mirofish_today = await db.get_daily_mirofish_count()
                    logger.info(
                        "Heartbeat",
                        cycles=cycle_count,
                        cumulative_tokens_scanned=cumulative["tokens_scanned"],
                        cumulative_candidates_promoted=cumulative["candidates_promoted"],
                        cumulative_alerts_fired=cumulative["alerts_fired"],
                        mirofish_jobs_today=mirofish_today,
                    )
                    last_heartbeat = now

                if args.cycles > 0 and cycle_count >= args.cycles:
                    break

                # Wait for next cycle or shutdown
                try:
                    await asyncio.wait_for(
                        shutdown_event.wait(),
                        timeout=settings.SCAN_INTERVAL_SECONDS,
                    )
                except asyncio.TimeoutError:
                    pass  # Normal -- interval elapsed
    finally:
        await db.close()
        logger.info("Scanner stopped", cycles_completed=cycle_count)


if __name__ == "__main__":
    asyncio.run(main())
