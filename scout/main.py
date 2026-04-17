"""CoinPump Scout -- main pipeline entry point."""

import argparse
import asyncio
import signal
import sys
import time

import aiohttp
import structlog

from datetime import datetime, timedelta, timezone

from scout.aggregator import aggregate
from scout.alerter import format_daily_summary, send_alert, send_telegram_message
from scout.chains.events import safe_emit
from scout.chains.patterns import seed_built_in_patterns
from scout.chains.tracker import run_chain_tracker
from scout.config import Settings, configure_cache
from scout.counter.detail import fetch_coin_detail, extract_counter_data
from scout.counter.flags import compute_memecoin_flags
from scout.counter.scorer import score_counter_memecoin
from scout.db import Database
from scout.gate import evaluate
from scout.heartbeat import (
    _heartbeat_stats,
    _maybe_emit_heartbeat,
    _reset_heartbeat_stats,
)
from scout.ingestion.coingecko import fetch_top_movers as cg_fetch_top_movers
from scout.ingestion.coingecko import fetch_trending as cg_fetch_trending
from scout.ingestion.coingecko import fetch_by_volume as cg_fetch_by_volume
from scout.ingestion import coingecko as _cg_module
from scout.ingestion.dexscreener import fetch_trending
from scout.ingestion.geckoterminal import fetch_trending_pools
from scout.ingestion.holder_enricher import enrich_holders
from scout.narrative.agent import narrative_agent_loop
from scout.safety import is_safe
from scout.scorer import score
from scout.spikes.detector import record_volume, detect_spikes, detect_7d_momentum
from scout.gainers.tracker import store_top_gainers
from scout.losers.tracker import store_top_losers
from scout.trading.signals import (
    trade_first_signals,
    trade_volume_spikes,
)
from scout.briefing.collector import collect_briefing_data
from scout.briefing.synthesizer import split_message, synthesize_briefing

logger = structlog.get_logger()


async def briefing_loop(
    session: aiohttp.ClientSession,
    settings: Settings,
    db: Database,
) -> None:
    """Time-gated briefing generation loop.

    Runs every 60s, checks if a briefing is due based on BRIEFING_HOURS_UTC
    and an 11-hour minimum gap. Persists last_briefing_at to DB so it
    survives restarts.
    """
    import json as _json

    briefing_hours = [int(h.strip()) for h in settings.BRIEFING_HOURS_UTC.split(",")]

    # Load last briefing time from DB (persist across restarts)
    last_briefing_at: datetime | None = None
    last_str = await db.get_last_briefing_time()
    if last_str:
        try:
            last_briefing_at = datetime.fromisoformat(
                last_str.replace("Z", "+00:00")
            )
            if last_briefing_at.tzinfo is None:
                last_briefing_at = last_briefing_at.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass

    logger.info(
        "briefing_loop_started",
        hours=briefing_hours,
        last_briefing_at=str(last_briefing_at),
    )

    while True:
        try:
            now = datetime.now(timezone.utc)
            should_run = (
                now.hour in briefing_hours
                and (
                    last_briefing_at is None
                    or (now - last_briefing_at).total_seconds() > 39600  # >11h
                )
            )

            if should_run:
                briefing_type = "morning" if now.hour < 12 else "evening"
                logger.info("briefing_starting", type=briefing_type)

                try:
                    raw = await collect_briefing_data(session, db, settings)
                    synthesis = await synthesize_briefing(
                        raw,
                        settings.ANTHROPIC_API_KEY,
                        settings.BRIEFING_MODEL,
                    )

                    # Store in DB
                    await db.store_briefing(
                        briefing_type=briefing_type,
                        raw_data=_json.dumps(raw, default=str),
                        synthesis=synthesis,
                        model_used=settings.BRIEFING_MODEL,
                        created_at=now.isoformat(),
                    )

                    # Send to Telegram
                    if settings.BRIEFING_TELEGRAM_ENABLED:
                        for chunk in split_message(synthesis, 4096):
                            await send_telegram_message(chunk, session, settings)

                    last_briefing_at = now
                    logger.info("briefing_delivered", type=briefing_type)
                except Exception:
                    logger.exception("briefing_error")

        except Exception:
            logger.exception("briefing_loop_tick_error")

        await asyncio.sleep(60)


async def _safe_counter_followup(token, session, settings, db=None):
    """Run counter-score and send follow-up Telegram message. Never raises."""
    if session.closed:
        return
    try:
        buy_pressure = 0.5
        if getattr(token, "txns_h1_buys", None) and getattr(
            token, "txns_h1_sells", None
        ):
            total = token.txns_h1_buys + token.txns_h1_sells
            if total > 0:
                buy_pressure = token.txns_h1_buys / total

        vol_liq = token.volume_24h_usd / max(token.liquidity_usd, 1)

        flags = compute_memecoin_flags(
            buy_pressure=buy_pressure,
            liquidity_usd=token.liquidity_usd,
            token_age_days=token.token_age_days,
            vol_liq_ratio=vol_liq,
            holder_count=token.holder_count,
            goplus_creator_pct=0.0,
            goplus_is_honeypot=False,
        )

        counter = await score_counter_memecoin(
            token_name=token.token_name,
            symbol=token.ticker,
            chain=token.chain,
            token_age_days=token.token_age_days,
            liquidity_usd=token.liquidity_usd,
            vol_liq_ratio=vol_liq,
            buy_pressure=buy_pressure,
            holder_count=token.holder_count,
            flags=flags,
            data_completeness="pipeline_only",
            api_key=settings.ANTHROPIC_API_KEY,
            model=settings.COUNTER_MODEL,
        )

        if db is not None:
            await safe_emit(
                db,
                token_id=token.contract_address,
                pipeline="memecoin",
                event_type="counter_scored",
                event_data={
                    "risk_score": counter.risk_score if counter.risk_score is not None else 0,
                    "flag_count": len(counter.red_flags or []),
                    "high_severity_count": sum(
                        1 for f in (counter.red_flags or []) if f.severity == "high"
                    ),
                    "data_completeness": counter.data_completeness,
                },
                source_module="counter.scorer",
            )

        if counter.risk_score is not None:
            flag_lines = "\n".join(
                f"- [{f.severity.upper()}] {f.flag}: {f.detail}"
                for f in counter.red_flags
            )
            msg = (
                f"Risk assessment for {token.ticker}:\n"
                f"Risk: {counter.risk_score}/100 | {counter.data_completeness} data\n"
                f"{flag_lines}\n"
                f'"{counter.counter_argument}"'
            )
            await send_telegram_message(msg, session, settings)

        _heartbeat_stats["counter_scores_memecoin"] += 1
        logger.info(
            "counter_followup_sent", symbol=token.ticker, risk_score=counter.risk_score
        )
    except Exception as e:
        logger.error(
            "counter_followup_error", symbol=getattr(token, "ticker", "?"), error=str(e)
        )


async def run_cycle(
    settings: Settings,
    db: Database,
    session: aiohttp.ClientSession,
    dry_run: bool = False,
    trading_engine=None,
) -> dict:
    """Run one full pipeline cycle.

    Returns stats dict with tokens_scanned, candidates_promoted, alerts_fired, etc.
    """
    stats = {"tokens_scanned": 0, "candidates_promoted": 0, "alerts_fired": 0}

    # Stage 1: Parallel ingestion
    dex_tokens, gecko_tokens, cg_movers, cg_trending, cg_by_volume = await asyncio.gather(
        fetch_trending(session, settings),
        fetch_trending_pools(session, settings),
        cg_fetch_top_movers(session, settings),
        cg_fetch_trending(session, settings),
        cg_fetch_by_volume(session, settings),
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
    if isinstance(cg_by_volume, Exception):
        logger.warning("CoinGecko volume scan failed", error=str(cg_by_volume))
        cg_by_volume = []

    # Cache raw CoinGecko prices for dashboard (zero extra API calls)
    all_raw = list(_cg_module.last_raw_markets)
    if _cg_module.last_raw_trending:
        all_raw.extend(_cg_module.last_raw_trending)
    if _cg_module.last_raw_by_volume:
        all_raw.extend(_cg_module.last_raw_by_volume)
    if all_raw:
        try:
            cached = await db.cache_prices(all_raw)
            logger.info("price_cache_updated", count=cached)
        except Exception:
            logger.exception("price_cache_error")

    # Combine raw market data from both movers and volume scans (dedup by id)
    _raw_markets_combined: list[dict] = []
    _seen_ids: set[str] = set()
    for raw_list in [_cg_module.last_raw_markets, _cg_module.last_raw_by_volume]:
        for coin in raw_list:
            cid = coin.get("id", "")
            if cid and cid not in _seen_ids:
                _seen_ids.add(cid)
                _raw_markets_combined.append(coin)

    # Volume Spike Detection (zero extra API calls -- uses cached data)
    if settings.VOLUME_SPIKE_ENABLED and _raw_markets_combined:
        try:
            await record_volume(db, _raw_markets_combined)
            spikes = await detect_spikes(
                db, settings.VOLUME_SPIKE_RATIO, settings.VOLUME_SPIKE_MAX_MCAP
            )
            if spikes:
                logger.info("volume_spikes_detected", count=len(spikes))
                if trading_engine:
                    await trade_volume_spikes(trading_engine, db, spikes)
        except Exception:
            logger.exception("volume_spike_error")

    # Top Gainers Tracker (zero extra API calls -- uses cached data)
    if settings.GAINERS_TRACKER_ENABLED and _raw_markets_combined:
        try:
            await store_top_gainers(
                db, _raw_markets_combined,
                min_change=settings.GAINERS_MIN_CHANGE,
                max_mcap=settings.GAINERS_MAX_MCAP,
            )
        except Exception:
            logger.exception("gainers_tracker_error")

    # Top Losers Tracker -- validation-only (no paper trades, data collection for comparison)
    if settings.LOSERS_TRACKER_ENABLED and _raw_markets_combined:
        try:
            await store_top_losers(
                db, _raw_markets_combined,
                max_drop=settings.LOSERS_MIN_DROP,
                max_mcap=settings.LOSERS_MAX_MCAP,
            )
        except Exception:
            logger.exception("losers_tracker_error")

    # 7-Day Momentum Scanner (zero extra API calls -- filters existing data)
    if settings.MOMENTUM_7D_ENABLED and _raw_markets_combined:
        try:
            momentum_7d = await detect_7d_momentum(
                db, _raw_markets_combined,
                min_7d_change=settings.MOMENTUM_7D_MIN_CHANGE,
                max_mcap=settings.MOMENTUM_7D_MAX_MCAP,
                min_volume_24h=settings.MOMENTUM_7D_MIN_VOLUME,
            )
            if momentum_7d:
                logger.info(
                    "momentum_7d_tokens",
                    count=len(momentum_7d),
                    tokens=[m["symbol"] for m in momentum_7d],
                )
        except Exception:
            logger.exception("momentum_7d_error")

    # Stage 2: Aggregate
    all_candidates = aggregate(
        list(dex_tokens) + list(gecko_tokens) + list(cg_movers) + list(cg_trending) + list(cg_by_volume)
    )
    stats["tokens_scanned"] = len(all_candidates)

    # Enrich holders (concurrently)
    enriched = list(
        await asyncio.gather(
            *[enrich_holders(token, session, settings) for token in all_candidates]
        )
    )

    # Compute holder_growth_1h from previous snapshots
    for i, token in enumerate(enriched):
        if token.holder_count > 0:
            prev = await db.get_previous_holder_count(token.contract_address)
            if prev is not None:
                growth = token.holder_count - prev
                enriched[i] = token.model_copy(
                    update={"holder_growth_1h": max(0, growth)}
                )
            await db.log_holder_snapshot(token.contract_address, token.holder_count)

    # Compute vol_7d_avg from historical volume snapshots + log current volume
    for i, token in enumerate(enriched):
        if token.volume_24h_usd > 0:
            vol_avg = await db.get_vol_7d_avg(token.contract_address)
            if vol_avg is not None:
                enriched[i] = enriched[i].model_copy(update={"vol_7d_avg": vol_avg})
            await db.log_volume_snapshot(token.contract_address, token.volume_24h_usd)

    # Stage 3: Score
    scored = []
    all_scored_tokens = []  # All tokens with updated quant_score/signals_fired
    for token in enriched:
        try:
            historical_scores = await db.get_recent_scores(token.contract_address, limit=3)
            points, signals = score(token, settings, historical_scores=historical_scores)
            updated = token.model_copy(
                update={"quant_score": points, "signals_fired": signals}
            )
            all_scored_tokens.append(updated)
            await db.upsert_candidate(updated)
            await db.log_score(token.contract_address, points)
            await safe_emit(
                db,
                token_id=token.contract_address,
                pipeline="memecoin",
                event_type="candidate_scored",
                event_data={
                    "quant_score": int(points),
                    "signals_fired": list(signals),
                    "signal_count": len(signals),
                },
                source_module="scorer",
            )
            if points >= settings.MIN_SCORE:
                scored.append((updated, signals))
                stats["candidates_promoted"] += 1
        except Exception:
            logger.exception("scoring_error", token=getattr(token, "contract_address", "?"))

    # Paper trade on first meaningful signal (earliest detection point)
    if trading_engine:
        scored_for_trading = [
            (t, t.quant_score, t.signals_fired)
            for t in all_scored_tokens
            if (t.quant_score or 0) > 0 and t.signals_fired
        ]
        if scored_for_trading:
            await trade_first_signals(
                trading_engine, db, scored_for_trading,
                min_mcap=settings.PAPER_MIN_MCAP,
            )

    # Stages 4-5: Gate (MiroFish + conviction)
    for token, signals in scored:
        should_alert, conviction, gated_token = await evaluate(
            token,
            db,
            session,
            settings,
            signals_fired=signals,
        )

        # Persist narrative + conviction scores back to DB
        await db.upsert_candidate(gated_token)

        logger.info(
            "gate_decision",
            token=gated_token.token_name,
            should_alert=should_alert,
            conviction_score=round(conviction, 1),
            threshold=settings.CONVICTION_THRESHOLD,
        )

        if not should_alert:
            continue

        # Stage 6: Safety check + alert
        if not await is_safe(gated_token.contract_address, gated_token.chain, session):
            logger.warning(
                "Token failed safety check", token=gated_token.contract_address
            )
            continue

        # Duplicate suppression: skip if alerted in last 4 hours
        if await db.was_recently_alerted(gated_token.contract_address):
            logger.info(
                "alert_suppressed_duplicate",
                token=gated_token.token_name,
                contract_address=gated_token.contract_address,
            )
            continue

        if dry_run:
            logger.info(
                "DRY RUN: would alert",
                token=gated_token.token_name,
                conviction=conviction,
            )
            continue

        logger.info(
            "alert_attempted", token=gated_token.token_name, platform="telegram"
        )
        try:
            await send_alert(gated_token, signals, session, settings)
            logger.info(
                "alert_delivered", token=gated_token.token_name, status="success"
            )
            await safe_emit(
                db,
                token_id=gated_token.contract_address,
                pipeline="memecoin",
                event_type="alert_fired",
                event_data={
                    "conviction_score": float(gated_token.conviction_score or 0),
                    "alert_type": "telegram",
                },
                source_module="alerter",
            )
        except Exception as e:
            logger.error(
                "alert_delivery_failed", token=gated_token.token_name, error=str(e)
            )

        await db.log_alert(
            contract_address=gated_token.contract_address,
            chain=gated_token.chain,
            conviction_score=conviction,
            alert_market_cap=gated_token.market_cap_usd,
            price_usd=getattr(gated_token, "price_usd", None),
            token_name=getattr(gated_token, "token_name", None),
            ticker=getattr(gated_token, "ticker", None),
        )
        stats["alerts_fired"] += 1

        # Counter-score follow-up (async, non-blocking)
        if settings.COUNTER_ENABLED:
            task = asyncio.create_task(
                _safe_counter_followup(gated_token, session, settings, db=db)
            )
            task.add_done_callback(
                lambda t: t.exception() if not t.cancelled() else None
            )

    return stats


async def check_outcomes(
    db: Database,
    session: aiohttp.ClientSession,
) -> int:
    """Check current prices for alerted tokens and record outcomes.

    Uses DexScreener tokens API to fetch current market cap.
    Returns count of outcomes recorded.
    """
    unchecked = await db.get_unchecked_alerts()
    if not unchecked:
        return 0

    recorded = 0
    for alert in unchecked:
        contract = alert["contract_address"]
        chain = alert["chain"]
        alert_mcap = alert["alert_market_cap"]
        if not alert_mcap or alert_mcap <= 0:
            continue

        try:
            url = f"https://api.dexscreener.com/tokens/v1/{chain}/{contract}"
            timeout = aiohttp.ClientTimeout(total=15)
            async with session.get(url, timeout=timeout) as resp:
                if resp.status != 200:
                    continue
                pairs = await resp.json()

            if not pairs or not isinstance(pairs, list):
                continue

            # Use first pair's FDV as current market cap
            current_mcap = float(pairs[0].get("fdv") or 0)
            if current_mcap <= 0:
                continue

            pct_change = ((current_mcap - alert_mcap) / alert_mcap) * 100
            await db.log_outcome(
                alert_id=alert["id"],
                contract_address=contract,
                alert_price=alert_mcap,
                check_price=current_mcap,
                price_change_pct=pct_change,
            )
            logger.info(
                "Outcome recorded",
                token=contract,
                alert_mcap=alert_mcap,
                current_mcap=current_mcap,
                pct_change=round(pct_change, 1),
            )
            recorded += 1
        except Exception as e:
            logger.warning("Outcome check failed", token=contract, error=str(e))

    return recorded


async def main() -> None:
    """Main entry point with CLI arg parsing and graceful shutdown."""
    parser = argparse.ArgumentParser(description="CoinPump Scout scanner")
    parser.add_argument(
        "--dry-run", action="store_true", help="Run without sending alerts"
    )
    parser.add_argument(
        "--cycles", type=int, default=0, help="Number of cycles (0=infinite)"
    )
    parser.add_argument(
        "--min-score-override",
        type=int,
        default=None,
        help="Override MIN_SCORE threshold (for testing)",
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
    # Pre-populate the module-level settings cache to avoid async race
    # on first lazy get_settings() call during startup.
    configure_cache(settings)
    if args.min_score_override is not None:
        settings.MIN_SCORE = args.min_score_override
        logger.info("MIN_SCORE overridden", min_score=settings.MIN_SCORE)

    # Honour COINGECKO_RATE_LIMIT_PER_MIN from config.
    from scout.ratelimit import configure_from_settings as _cg_ratelimit_configure

    _cg_ratelimit_configure(settings)

    db = Database(settings.DB_PATH)
    await db.initialize()

    # Paper trading engine
    from scout.trading.engine import TradingEngine

    trading_engine = None
    if settings.TRADING_ENABLED:
        trading_engine = TradingEngine(
            mode=settings.TRADING_MODE, db=db, settings=settings
        )
        logger.info(
            "trading_engine_initialized",
            mode=settings.TRADING_MODE,
        )

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
    last_outcome_check = time.monotonic()
    last_summary_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    outcome_check_interval = 3600  # 1 hour
    _reset_heartbeat_stats()

    try:
        async with aiohttp.ClientSession() as session:

            async def _pipeline_loop() -> None:
                nonlocal cycle_count
                nonlocal last_outcome_check, last_summary_date

                while not shutdown_event.is_set():
                    try:
                        stats = await run_cycle(
                            settings, db, session, dry_run=args.dry_run,
                            trading_engine=trading_engine,
                        )
                        logger.info("Cycle complete", **stats)
                        _heartbeat_stats["tokens_scanned"] += stats.get("tokens_scanned", 0)
                        _heartbeat_stats["candidates_promoted"] += stats.get("candidates_promoted", 0)
                        _heartbeat_stats["alerts_fired"] += stats.get("alerts_fired", 0)
                    except Exception as e:
                        logger.error("Cycle failed", error=str(e))

                    # Evaluate paper trades EVERY cycle (TP/SL/checkpoints)
                    # Must run frequently so TP triggers within minutes, not hours.
                    if trading_engine:
                        try:
                            from scout.trading.evaluator import evaluate_paper_trades
                            await evaluate_paper_trades(db, settings)
                        except Exception:
                            logger.exception("trading.pipeline_eval_error")

                    # Update peak prices for Early Catches + Top Gainers (every cycle)
                    try:
                        from scout.trending.tracker import update_trending_peaks
                        from scout.gainers.tracker import update_gainers_peaks
                        await update_trending_peaks(db)
                        await update_gainers_peaks(db)
                    except Exception:
                        logger.exception("peak_update_error")

                    cycle_count += 1

                    # BL-033: periodic heartbeat summary
                    _maybe_emit_heartbeat(settings)
                    now = time.monotonic()

                    # Hourly tasks: outcome check + DB prune
                    if now - last_outcome_check >= outcome_check_interval:
                        try:
                            outcomes_recorded = await check_outcomes(db, session)
                            if outcomes_recorded:
                                logger.info(
                                    "Outcomes checked", recorded=outcomes_recorded
                                )
                        except Exception as e:
                            logger.warning("Outcome check error", error=str(e))

                        # Prune old candidates if DB > 500MB
                        try:
                            db_size = (
                                settings.DB_PATH.stat().st_size
                                if settings.DB_PATH.exists()
                                else 0
                            )
                            if db_size > 500_000_000:
                                pruned = await db.prune_old_candidates(keep_days=7)
                                logger.info(
                                    "db_pruned",
                                    rows_deleted=pruned,
                                    db_size_mb=round(db_size / 1e6, 1),
                                )
                        except Exception as e:
                            logger.warning("DB prune error", error=str(e))

                        last_outcome_check = now

                    # Daily summary at midnight UTC
                    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    if current_date != last_summary_date:
                        try:
                            summary_data = await db.get_daily_summary_data()
                            summary_text = format_daily_summary(summary_data)
                            if not args.dry_run:
                                await send_telegram_message(
                                    summary_text, session, settings
                                )
                            logger.info(
                                "Daily summary sent",
                                alerts=summary_data["alerts_today"],
                                win_rate=summary_data["win_rate_pct"],
                            )
                        except Exception as e:
                            logger.warning("Daily summary failed", error=str(e))
                        last_summary_date = current_date

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

            # Seed chain patterns once at startup (idempotent)
            if settings.CHAINS_ENABLED:
                await seed_built_in_patterns(db)

            tasks: list[asyncio.Task] = [
                asyncio.create_task(_pipeline_loop()),
            ]
            if settings.NARRATIVE_ENABLED:
                tasks.append(
                    asyncio.create_task(
                        narrative_agent_loop(session, settings, db, trading_engine)
                    )
                )
            if settings.SECONDWAVE_ENABLED:
                from scout.secondwave.detector import secondwave_loop
                tasks.append(
                    asyncio.create_task(secondwave_loop(session, settings))
                )
            if settings.BRIEFING_ENABLED:
                tasks.append(
                    asyncio.create_task(briefing_loop(session, settings, db))
                )
            if settings.CHAINS_ENABLED:
                tasks.append(
                    asyncio.create_task(run_chain_tracker(db, settings))
                )

            # Both loops share the same session and rate limiter intentionally.
            # The coingecko_limiter (25 req/min) coordinates access; that IS
            # the back-pressure mechanism.
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await db.close()
        logger.info("Scanner stopped", cycles_completed=cycle_count)


if __name__ == "__main__":
    asyncio.run(main())
