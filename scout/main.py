"""CoinPump Scout -- main pipeline entry point."""

import argparse
import asyncio
import json
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
from scout.db import Database
from scout.gate import evaluate
from scout.ingestion.coingecko import fetch_top_movers as cg_fetch_top_movers
from scout.ingestion.coingecko import fetch_trending as cg_fetch_trending
from scout.ingestion.dexscreener import fetch_trending
from scout.ingestion.geckoterminal import fetch_trending_pools
from scout.ingestion.holder_enricher import enrich_holders
from scout.narrative.digest import format_heating_alert
from scout.narrative.evaluator import evaluate_pending
from scout.narrative.learner import daily_learn, weekly_consolidate
from scout.narrative.models import NarrativePrediction
from scout.narrative.observer import (
    compute_acceleration,
    detect_market_regime,
    fetch_categories,
    load_snapshots_at,
    parse_category_response,
    prune_old_snapshots,
    store_snapshot,
)
from scout.narrative.predictor import (
    fetch_laggards,
    filter_laggards,
    is_cooling_down,
    partition_and_select,
    record_signal,
    score_token,
    store_predictions,
)
from scout.narrative.strategy import Strategy
from scout.trending.tracker import (
    compare_with_signals as trending_compare,
    fetch_and_store_trending,
)
from scout.counter.detail import fetch_coin_detail, extract_counter_data
from scout.counter.flags import compute_narrative_flags, compute_memecoin_flags
from scout.counter.scorer import score_counter_narrative, score_counter_memecoin
from scout.safety import is_safe
from scout.scorer import score

logger = structlog.get_logger()


# BL-033: Module-level heartbeat state. Tracks cumulative pipeline stats
# across cycles and emits a structured "heartbeat" log every
# HEARTBEAT_INTERVAL_SECONDS so operators can see the pipeline is alive.
_heartbeat_stats: dict = {
    "started_at": None,
    "tokens_scanned": 0,
    "candidates_promoted": 0,
    "alerts_fired": 0,
    "narrative_predictions": 0,
    "counter_scores_memecoin": 0,
    "counter_scores_narrative": 0,
    "last_heartbeat_at": None,
}


def _reset_heartbeat_stats() -> None:
    """Reset module-level heartbeat state (test helper)."""
    _heartbeat_stats.update(
        started_at=None,
        tokens_scanned=0,
        candidates_promoted=0,
        alerts_fired=0,
        narrative_predictions=0,
        counter_scores_memecoin=0,
        counter_scores_narrative=0,
        last_heartbeat_at=None,
    )


def _maybe_emit_heartbeat(settings) -> bool:
    """Log heartbeat every HEARTBEAT_INTERVAL_SECONDS.

    On first call, seeds started_at/last_heartbeat_at without logging.
    Returns True if a heartbeat log was emitted.
    """
    now = datetime.now(timezone.utc)
    if _heartbeat_stats["last_heartbeat_at"] is None:
        _heartbeat_stats["last_heartbeat_at"] = now
        _heartbeat_stats["started_at"] = now
        return False
    elapsed = (now - _heartbeat_stats["last_heartbeat_at"]).total_seconds()
    if elapsed < settings.HEARTBEAT_INTERVAL_SECONDS:
        return False
    uptime_minutes = (now - _heartbeat_stats["started_at"]).total_seconds() / 60
    logger.info(
        "heartbeat",
        uptime_minutes=round(uptime_minutes, 1),
        tokens_scanned=_heartbeat_stats["tokens_scanned"],
        candidates_promoted=_heartbeat_stats["candidates_promoted"],
        alerts_fired=_heartbeat_stats["alerts_fired"],
        narrative_predictions=_heartbeat_stats["narrative_predictions"],
        counter_scores_memecoin=_heartbeat_stats["counter_scores_memecoin"],
        counter_scores_narrative=_heartbeat_stats["counter_scores_narrative"],
        last_heartbeat_at=_heartbeat_stats["last_heartbeat_at"].isoformat(),
    )
    _heartbeat_stats["last_heartbeat_at"] = now
    return True


async def _safe_counter_followup(token, session, settings, db=None):
    """Run counter-score and send follow-up Telegram message. Never raises."""
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
    for token in enriched:
        historical_scores = await db.get_recent_scores(token.contract_address, limit=3)
        points, signals = score(token, settings, historical_scores=historical_scores)
        updated = token.model_copy(
            update={"quant_score": points, "signals_fired": signals}
        )
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
            async with session.get(url) as resp:
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


async def narrative_agent_loop(
    session: aiohttp.ClientSession,
    settings: Settings,
    db: Database,
) -> None:
    """Run the narrative rotation agent as a long-lived background loop.

    Phases per cycle: OBSERVE -> PREDICT -> EVALUATE -> LEARN (daily/weekly).
    """
    strategy = Strategy(db)
    await strategy.load_or_init()

    # Load scheduling timestamps from strategy
    _epoch = datetime.min.replace(tzinfo=timezone.utc)
    last_eval_at = strategy.get_timestamp("last_eval_at", default=_epoch)
    last_daily_learn_at = strategy.get_timestamp("last_daily_learn_at", default=_epoch)
    last_weekly_learn_at = strategy.get_timestamp("last_weekly_learn_at", default=_epoch)

    while True:
        try:
            now = datetime.now(timezone.utc)

            # ----------------------------------------------------------
            # OBSERVE
            # ----------------------------------------------------------
            raw_categories = await fetch_categories(
                session, api_key=settings.COINGECKO_API_KEY
            )
            if not raw_categories:
                logger.warning("narrative.observe_empty")
                await asyncio.sleep(settings.NARRATIVE_POLL_INTERVAL)
                continue

            # Compute weighted 24h change for regime detection
            total_mcap = sum(float(c.get("market_cap") or 0) for c in raw_categories)
            if total_mcap > 0:
                weighted_change = (
                    sum(
                        float(c.get("market_cap_change_24h") or 0)
                        * float(c.get("market_cap") or 0)
                        for c in raw_categories
                    )
                    / total_mcap
                )
            else:
                weighted_change = 0.0

            market_regime = detect_market_regime(weighted_change)
            snapshots = parse_category_response(raw_categories, market_regime)
            await store_snapshot(db, snapshots)

            # Trending snapshot (gated by TRENDING_SNAPSHOT_ENABLED)
            if settings.TRENDING_SNAPSHOT_ENABLED:
                try:
                    await fetch_and_store_trending(
                        session, db, api_key=settings.COINGECKO_API_KEY
                    )
                except Exception:
                    logger.exception("trending_tracker.snapshot_error")

            # Load 6-hour-ago snapshots for acceleration comparison
            six_hours_ago = now - timedelta(hours=6)
            prev_snapshots = await load_snapshots_at(db, six_hours_ago)

            accel_threshold = float(strategy.get("category_accel_threshold"))  # type: ignore[arg-type]
            vol_growth_min = float(strategy.get("category_volume_growth_min"))  # type: ignore[arg-type]
            accelerations = compute_acceleration(
                snapshots, prev_snapshots, accel_threshold, vol_growth_min
            )

            # ----------------------------------------------------------
            # PREDICT
            # ----------------------------------------------------------
            heating = [a for a in accelerations if a.is_heating]
            heating.sort(key=lambda a: a.acceleration, reverse=True)
            max_heating = int(strategy.get("max_heating_per_cycle"))  # type: ignore[arg-type]
            heating = heating[:max_heating]

            cooldown_hours = int(strategy.get("signal_cooldown_hours"))  # type: ignore[arg-type]
            min_trigger = int(strategy.get("min_trigger_count"))  # type: ignore[arg-type]
            max_picks = int(strategy.get("max_picks_per_category"))  # type: ignore[arg-type]
            narrative_alert_enabled = bool(strategy.get("narrative_alert_enabled"))
            lessons = str(strategy.get("lessons_learned"))

            for accel in heating:
                try:
                    await safe_emit(
                        db,
                        token_id=accel.category_id,
                        pipeline="narrative",
                        event_type="category_heating",
                        event_data={
                            "category_id": accel.category_id,
                            "name": accel.name,
                            "acceleration": accel.acceleration,
                            "volume_growth_pct": accel.volume_growth_pct,
                            "market_regime": market_regime,
                        },
                        source_module="narrative.observer",
                    )
                    if await is_cooling_down(db, accel.category_id):
                        logger.info(
                            "narrative.category_cooling_down", category=accel.name
                        )
                        continue

                    trigger_count = await record_signal(
                        db,
                        category_id=accel.category_id,
                        category_name=accel.name,
                        acceleration=accel.acceleration,
                        volume_growth_pct=accel.volume_growth_pct,
                        coin_count_change=accel.coin_count_change,
                        cooldown_hours=cooldown_hours,
                    )

                    if trigger_count < min_trigger:
                        logger.info(
                            "narrative.below_min_trigger",
                            category=accel.name,
                            trigger_count=trigger_count,
                            min_trigger=min_trigger,
                        )
                        continue

                    # Fetch and filter laggards
                    raw_laggards = await fetch_laggards(
                        session, accel.category_id, api_key=settings.COINGECKO_API_KEY
                    )
                    laggards = filter_laggards(
                        raw_laggards,
                        category_id=accel.category_id,
                        category_name=accel.name,
                        max_mcap=float(strategy.get("laggard_max_mcap")),  # type: ignore[arg-type]
                        max_change=float(strategy.get("laggard_max_change")),  # type: ignore[arg-type]
                        min_change=float(strategy.get("laggard_min_change")),  # type: ignore[arg-type]
                        min_volume=float(strategy.get("laggard_min_volume")),  # type: ignore[arg-type]
                    )

                    if not laggards:
                        logger.info("narrative.no_laggards", category=accel.name)
                        continue

                    scored_laggards, control_laggards = partition_and_select(
                        laggards, max_picks
                    )

                    # Build top-3 coins string for prompt context
                    top_3 = raw_laggards[:3]
                    top_3_coins = ", ".join(
                        f"{c.get('name', '?')} ({c.get('symbol', '?').upper()})"
                        for c in top_3
                    )

                    strategy_snap = strategy.get_all()

                    # Score each scored laggard with Claude
                    prediction_rows: list[dict] = []
                    prediction_models: list[NarrativePrediction] = []

                    consecutive_failures = 0
                    for token in scored_laggards:
                        if consecutive_failures >= 3:
                            logger.warning(
                                "narrative_scoring_3_failures",
                                category=accel.category_id,
                            )
                            break

                        # Fetch detail up-front to get watchlist_portfolio_users
                        # for the narrative scoring prompt. Reused below for
                        # counter-narrative scoring (cache makes this cheap).
                        token_detail = await fetch_coin_detail(
                            session, token.coin_id, settings.COINGECKO_API_KEY
                        )
                        token_cdata = (
                            extract_counter_data(token_detail) if token_detail else None
                        )
                        watchlist_users = (
                            int(token_cdata["watchlist_portfolio_users"])
                            if token_cdata else 0
                        )

                        result = await score_token(
                            token=token,
                            accel=accel,
                            market_regime=market_regime,
                            top_3_coins=top_3_coins,
                            lessons=lessons,
                            api_key=settings.ANTHROPIC_API_KEY,
                            model=settings.NARRATIVE_SCORING_MODEL,
                            watchlist_users=watchlist_users,
                        )
                        if result is None:
                            consecutive_failures += 1
                            continue
                        consecutive_failures = 0  # reset on success

                        # Chain events: laggard was selected and narrative scored
                        await safe_emit(
                            db,
                            token_id=token.coin_id,
                            pipeline="narrative",
                            event_type="laggard_picked",
                            event_data={
                                "category_id": accel.category_id,
                                "category_name": accel.name,
                                "narrative_fit_score": int(result.get("narrative_fit_score", 0)),
                                "confidence": result.get("confidence", ""),
                                "trigger_count": trigger_count,
                            },
                            source_module="narrative.predictor",
                        )
                        await safe_emit(
                            db,
                            token_id=token.coin_id,
                            pipeline="narrative",
                            event_type="narrative_scored",
                            event_data={
                                "narrative_fit_score": int(result.get("narrative_fit_score", 0)),
                                "staying_power": result.get("staying_power", ""),
                                "confidence": result.get("confidence", ""),
                            },
                            source_module="narrative.predictor",
                        )

                        # Counter-score for narrative picks
                        counter_risk = None
                        counter_flags_json = None
                        counter_arg = None
                        counter_completeness = None
                        counter_scored = None

                        if settings.COUNTER_ENABLED:
                            if token_cdata is not None:
                                cdata = token_cdata
                                data_comp = "full"
                            else:
                                cdata = {
                                    "commits_4w": 0,
                                    "reddit_subscribers": 0,
                                    "telegram_users": 0,
                                    "sentiment_up_pct": 50.0,
                                    "price_change_7d": 0,
                                    "price_change_30d": 0,
                                    "watchlist_portfolio_users": 0,
                                }
                                data_comp = "partial"

                            narrative_flags = compute_narrative_flags(
                                price_change_30d=cdata["price_change_30d"],
                                commits_4w=cdata["commits_4w"],
                                reddit_subs=cdata["reddit_subscribers"],
                                sentiment_up_pct=cdata["sentiment_up_pct"],
                                narrative_fit_score=result.get("narrative_fit", 50),
                                token_vol_change_24h=0.0,
                                category_vol_growth_pct=accel.volume_growth_pct,
                            )

                            counter_result = await score_counter_narrative(
                                token_name=token.name,
                                symbol=token.symbol,
                                market_cap=token.market_cap,
                                price_change_24h=token.price_change_24h,
                                category_name=accel.name,
                                acceleration=accel.acceleration,
                                narrative_fit_score=result.get("narrative_fit", 50),
                                flags=narrative_flags,
                                data_completeness=data_comp,
                                api_key=settings.ANTHROPIC_API_KEY,
                                model=settings.COUNTER_MODEL,
                            )

                            counter_risk = counter_result.risk_score
                            counter_flags_json = (
                                json.dumps(
                                    [f.model_dump() for f in counter_result.red_flags]
                                )
                                if counter_result.red_flags
                                else None
                            )
                            counter_arg = counter_result.counter_argument
                            counter_completeness = counter_result.data_completeness
                            counter_scored = (
                                counter_result.counter_scored_at.isoformat()
                            )

                            await safe_emit(
                                db,
                                token_id=token.coin_id,
                                pipeline="narrative",
                                event_type="counter_scored",
                                event_data={
                                    "risk_score": counter_risk if counter_risk is not None else 0,
                                    "flag_count": len(counter_result.red_flags or []),
                                    "high_severity_count": sum(
                                        1 for f in (counter_result.red_flags or [])
                                        if f.severity == "high"
                                    ),
                                    "data_completeness": counter_completeness,
                                },
                                source_module="counter.scorer",
                            )

                        pred_row = {
                            "category_id": accel.category_id,
                            "category_name": accel.name,
                            "coin_id": token.coin_id,
                            "symbol": token.symbol,
                            "name": token.name,
                            "market_cap_at_prediction": token.market_cap,
                            "price_at_prediction": token.price,
                            "narrative_fit_score": result.get("narrative_fit_score", 0),
                            "staying_power": result.get("staying_power", "unknown"),
                            "confidence": result.get("confidence", "low"),
                            "reasoning": result.get("reasoning", ""),
                            "market_regime": market_regime,
                            "trigger_count": trigger_count,
                            "is_control": False,
                            "is_holdout": False,
                            "strategy_snapshot": strategy_snap,
                            "strategy_snapshot_ab": None,
                            "predicted_at": now.isoformat(),
                        }
                        pred_row["counter_risk_score"] = counter_risk
                        pred_row["counter_flags"] = counter_flags_json
                        pred_row["counter_argument"] = counter_arg
                        pred_row["counter_data_completeness"] = counter_completeness
                        pred_row["counter_scored_at"] = counter_scored
                        pred_row["watchlist_users"] = watchlist_users
                        prediction_rows.append(pred_row)
                        prediction_models.append(
                            NarrativePrediction(
                                category_id=accel.category_id,
                                category_name=accel.name,
                                coin_id=token.coin_id,
                                symbol=token.symbol,
                                name=token.name,
                                market_cap_at_prediction=token.market_cap,
                                price_at_prediction=token.price,
                                narrative_fit_score=result.get(
                                    "narrative_fit_score", 0
                                ),
                                staying_power=result.get("staying_power", "unknown"),
                                confidence=result.get("confidence", "low"),
                                reasoning=result.get("reasoning", ""),
                                market_regime=market_regime,
                                trigger_count=trigger_count,
                                is_control=False,
                                strategy_snapshot=strategy_snap,
                                predicted_at=now,
                                watchlist_users=watchlist_users,
                            )
                        )

                    # Add control predictions (no Claude scoring).
                    # Fetch watchlist the same way as agent picks so that
                    # backtest comparisons between agent and control are fair
                    # — both groups have the same data captured at prediction
                    # time. The detail fetcher has a 30-min cache so this is
                    # essentially free when a token also appeared in scoring.
                    for token in control_laggards:
                        control_detail = await fetch_coin_detail(
                            session, token.coin_id, settings.COINGECKO_API_KEY
                        )
                        control_watchlist = 0
                        if control_detail:
                            ccdata = extract_counter_data(control_detail)
                            control_watchlist = int(
                                ccdata.get("watchlist_portfolio_users", 0) or 0
                            )
                        prediction_rows.append(
                            {
                                "category_id": accel.category_id,
                                "category_name": accel.name,
                                "coin_id": token.coin_id,
                                "symbol": token.symbol,
                                "name": token.name,
                                "market_cap_at_prediction": token.market_cap,
                                "price_at_prediction": token.price,
                                "narrative_fit_score": 0,
                                "staying_power": "unknown",
                                "confidence": "low",
                                "reasoning": "control pick — no Claude scoring",
                                "market_regime": market_regime,
                                "trigger_count": trigger_count,
                                "is_control": True,
                                "is_holdout": False,
                                "strategy_snapshot": strategy_snap,
                                "strategy_snapshot_ab": None,
                                "predicted_at": now.isoformat(),
                                "watchlist_users": control_watchlist,
                            }
                        )

                    if prediction_rows:
                        await store_predictions(db, prediction_rows)
                        _heartbeat_stats["narrative_predictions"] += len(prediction_models)
                        if settings.COUNTER_ENABLED:
                            _heartbeat_stats["counter_scores_narrative"] += len(prediction_models)
                        logger.info(
                            "narrative.predictions_stored",
                            category=accel.name,
                            scored=len(prediction_models),
                            control=len(control_laggards),
                        )

                    # Send alert if enabled
                    if narrative_alert_enabled and prediction_models:
                        try:
                            alert_text = format_heating_alert(
                                accel, prediction_models, top_3_coins
                            )
                            await send_telegram_message(alert_text, session, settings)
                            logger.info("narrative.alert_sent", category=accel.name)
                        except Exception:
                            logger.exception(
                                "narrative.alert_error", category=accel.name
                            )

                except Exception:
                    logger.exception(
                        "narrative.predict_category_error", category=accel.name
                    )

            # ----------------------------------------------------------
            # EVALUATE (gated by NARRATIVE_EVAL_INTERVAL)
            # ----------------------------------------------------------
            if (now - last_eval_at).total_seconds() >= settings.NARRATIVE_EVAL_INTERVAL:
                try:
                    await evaluate_pending(
                        session, db, strategy, api_key=settings.COINGECKO_API_KEY
                    )
                    last_eval_at = now
                    await strategy.set_timestamp("last_eval_at", now)
                    logger.info("narrative.eval_complete")
                except Exception:
                    logger.exception("narrative.eval_error")

                # Trending comparison (piggybacks on EVALUATE interval)
                if settings.TRENDING_SNAPSHOT_ENABLED:
                    try:
                        await trending_compare(db)
                        logger.info("trending_tracker.compare_complete")
                    except Exception:
                        logger.exception("trending_tracker.compare_error")

            # ----------------------------------------------------------
            # LEARN daily (gated by hour + 23h gap)
            # ----------------------------------------------------------
            if (
                now.hour == settings.NARRATIVE_LEARN_HOUR_UTC
                and (now - last_daily_learn_at).total_seconds() >= 23 * 3600
            ):
                try:
                    await daily_learn(
                        db,
                        strategy,
                        api_key=settings.ANTHROPIC_API_KEY,
                        model=settings.NARRATIVE_LEARN_MODEL,
                    )
                    await prune_old_snapshots(
                        db, settings.NARRATIVE_SNAPSHOT_RETENTION_DAYS
                    )
                    last_daily_learn_at = now
                    await strategy.set_timestamp("last_daily_learn_at", now)
                    logger.info("narrative.daily_learn_complete")
                except Exception:
                    logger.exception("narrative.daily_learn_error")

            # ----------------------------------------------------------
            # LEARN weekly (gated by weekday + hour + 6.9-day gap)
            # ----------------------------------------------------------
            if (
                now.weekday() == settings.NARRATIVE_WEEKLY_LEARN_DAY
                and now.hour == (settings.NARRATIVE_LEARN_HOUR_UTC + 1) % 24
                and (now - last_weekly_learn_at).total_seconds() >= 6.9 * 86400
            ):
                try:
                    await weekly_consolidate(
                        db,
                        strategy,
                        api_key=settings.ANTHROPIC_API_KEY,
                        model=settings.NARRATIVE_LEARN_MODEL,
                    )
                    last_weekly_learn_at = now
                    await strategy.set_timestamp("last_weekly_learn_at", now)
                    logger.info("narrative.weekly_learn_complete")
                except Exception:
                    logger.exception("narrative.weekly_learn_error")

        except Exception:
            import traceback
            traceback.print_exc()
            logger.exception("narrative.loop_error")

        await asyncio.sleep(settings.NARRATIVE_POLL_INTERVAL)


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
                            settings, db, session, dry_run=args.dry_run
                        )
                        logger.info("Cycle complete", **stats)
                        _heartbeat_stats["tokens_scanned"] += stats.get("tokens_scanned", 0)
                        _heartbeat_stats["candidates_promoted"] += stats.get("candidates_promoted", 0)
                        _heartbeat_stats["alerts_fired"] += stats.get("alerts_fired", 0)
                    except Exception as e:
                        logger.error("Cycle failed", error=str(e))

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
                    asyncio.create_task(narrative_agent_loop(session, settings, db))
                )
            if settings.SECONDWAVE_ENABLED:
                from scout.secondwave.detector import secondwave_loop
                tasks.append(
                    asyncio.create_task(secondwave_loop(session, settings))
                )
            if settings.CHAINS_ENABLED:
                tasks.append(
                    asyncio.create_task(run_chain_tracker(db, settings))
                )

            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await db.close()
        logger.info("Scanner stopped", cycles_completed=cycle_count)


if __name__ == "__main__":
    asyncio.run(main())
