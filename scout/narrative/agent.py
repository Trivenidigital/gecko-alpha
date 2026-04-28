"""Narrative rotation agent -- long-lived background loop.

Phases per cycle: OBSERVE -> PREDICT -> EVALUATE -> LEARN (daily/weekly).
Extracted from scout.main to keep the main module focused on pipeline
orchestration.
"""

import asyncio
import json

from datetime import datetime, timedelta, timezone

import aiohttp
import structlog

from scout.alerter import send_telegram_message
from scout.chains.events import safe_emit
from scout.config import Settings
from scout.counter.detail import fetch_coin_detail, extract_counter_data
from scout.counter.flags import compute_narrative_flags
from scout.counter.scorer import score_counter_narrative
from scout.db import Database
from scout.gainers.tracker import (
    compare_gainers_with_signals,
    update_gainers_peaks,
)
from scout.heartbeat import _heartbeat_stats
from scout.losers.tracker import compare_losers_with_signals
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
from scout.narrative.prompts import parse_fit_score
from scout.narrative.strategy import Strategy
from scout.preferences.matcher import should_alert_category, should_alert_token
from scout.trading.signals import (
    trade_chain_completions,
    trade_predictions,
    trade_trending,
)
from scout.trending.tracker import (
    compare_with_signals as trending_compare,
    fetch_and_store_trending,
    update_trending_peaks,
)

logger = structlog.get_logger()


async def narrative_agent_loop(
    session: aiohttp.ClientSession,
    settings: Settings,
    db: Database,
    trading_engine=None,
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
    last_weekly_learn_at = strategy.get_timestamp(
        "last_weekly_learn_at", default=_epoch
    )

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
            # narrative_agent_loop does NOT have access to cg_trending
            # (which is local to run_cycle).  Use fetch_and_store_trending
            # which fetches fresh data from /search/trending.
            if settings.TRENDING_SNAPSHOT_ENABLED:
                try:
                    await fetch_and_store_trending(
                        session, db, settings.COINGECKO_API_KEY
                    )
                    # trending_catch historically net-loses (-$339 / 86 trades);
                    # disabled in prod via PAPER_SIGNAL_TRENDING_CATCH_ENABLED=False.
                    if (
                        trading_engine
                        and settings.PAPER_SIGNAL_TRENDING_CATCH_ENABLED
                    ):
                        await trade_trending(
                            trading_engine,
                            db,
                            max_mcap_rank=settings.PAPER_MAX_MCAP_RANK,
                            min_mcap=settings.PAPER_MIN_MCAP,
                            max_mcap=settings.PAPER_MAX_MCAP,
                            settings=settings,
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

                    # Cache laggard prices for dashboard (zero extra API calls)
                    if raw_laggards:
                        try:
                            await db.cache_prices(raw_laggards)
                        except Exception:
                            logger.exception("price_cache_laggard_error")

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
                            if token_cdata
                            else 0
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

                        fit_score = parse_fit_score(result, default=0)

                        # Chain events: laggard was selected and narrative scored
                        await safe_emit(
                            db,
                            token_id=token.coin_id,
                            pipeline="narrative",
                            event_type="laggard_picked",
                            event_data={
                                "category_id": accel.category_id,
                                "category_name": accel.name,
                                "narrative_fit_score": fit_score,
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
                                "narrative_fit_score": fit_score,
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

                            if data_comp == "partial":
                                # Don't compute flags from missing data -- they'd all be HIGH severity
                                narrative_flags = []
                            else:
                                narrative_flags = compute_narrative_flags(
                                    price_change_30d=cdata["price_change_30d"],
                                    commits_4w=cdata["commits_4w"],
                                    reddit_subs=cdata["reddit_subscribers"],
                                    sentiment_up_pct=cdata["sentiment_up_pct"],
                                    narrative_fit_score=parse_fit_score(
                                        result, default=50
                                    ),
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
                                narrative_fit_score=parse_fit_score(result, default=50),
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
                                    "risk_score": (
                                        counter_risk if counter_risk is not None else 0
                                    ),
                                    "flag_count": len(counter_result.red_flags or []),
                                    "high_severity_count": sum(
                                        1
                                        for f in (counter_result.red_flags or [])
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
                            "narrative_fit_score": fit_score,
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
                                narrative_fit_score=fit_score,
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
                    # -- both groups have the same data captured at prediction
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
                        _heartbeat_stats["narrative_predictions"] += len(
                            prediction_models
                        )
                        if settings.COUNTER_ENABLED:
                            _heartbeat_stats["counter_scores_narrative"] += len(
                                prediction_models
                            )
                        logger.info(
                            "narrative.predictions_stored",
                            category=accel.name,
                            scored=len(prediction_models),
                            control=len(control_laggards),
                        )

                        # Open paper trades for narrative predictions
                        if trading_engine and prediction_models:
                            await trade_predictions(
                                trading_engine,
                                db,
                                prediction_models,
                                min_mcap=settings.PAPER_MIN_MCAP,
                                max_mcap=settings.PAPER_MAX_MCAP,
                                settings=settings,
                            )

                    # Send alert if enabled and matches user preferences
                    if narrative_alert_enabled and prediction_models:
                        if should_alert_category(accel.category_id, strategy):
                            alertable = [
                                p
                                for p in prediction_models
                                if should_alert_token(
                                    p.market_cap_at_prediction, strategy
                                )
                            ]
                            if alertable:
                                try:
                                    alert_text = format_heating_alert(
                                        accel, alertable, top_3_coins
                                    )
                                    await send_telegram_message(
                                        alert_text, session, settings
                                    )
                                    logger.info(
                                        "narrative.alert_sent", category=accel.name
                                    )
                                except Exception:
                                    logger.exception(
                                        "narrative.alert_error", category=accel.name
                                    )
                            else:
                                logger.info(
                                    "narrative.alert_skipped_mcap_filter",
                                    category=accel.name,
                                    total_predictions=len(prediction_models),
                                )
                        else:
                            logger.info(
                                "narrative.alert_skipped_preference",
                                category=accel.name,
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

                # Evaluate paper trades (TP/SL/checkpoints)
                if trading_engine:
                    try:
                        from scout.trading.evaluator import evaluate_paper_trades

                        await evaluate_paper_trades(db, settings)
                        logger.info("trading.eval_complete")
                    except Exception:
                        logger.exception("trading.eval_error")

                # Trending comparison (piggybacks on EVALUATE interval)
                if settings.TRENDING_SNAPSHOT_ENABLED:
                    try:
                        await trending_compare(db)
                        logger.info("trending_tracker.compare_complete")
                    except Exception:
                        logger.exception("trending_tracker.compare_error")

                # Gainers comparison (piggybacks on EVALUATE interval)
                if settings.GAINERS_TRACKER_ENABLED:
                    try:
                        await compare_gainers_with_signals(db)
                        logger.info("gainers_tracker.compare_complete")
                    except Exception:
                        logger.exception("gainers_tracker.compare_error")

                # Update peak prices for trending + gainers comparisons
                try:
                    await update_trending_peaks(db)
                    await update_gainers_peaks(db)
                except Exception:
                    logger.exception("peak_tracker.update_error")

                # Losers comparison (piggybacks on EVALUATE interval)
                if settings.LOSERS_TRACKER_ENABLED:
                    try:
                        await compare_losers_with_signals(db)
                        logger.info("losers_tracker.compare_complete")
                    except Exception:
                        logger.exception("losers_tracker.compare_error")

                # Paper trade completed chains
                if trading_engine and settings.CHAINS_ENABLED:
                    await trade_chain_completions(trading_engine, db, settings)

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
                    # Prune gainers/losers snapshots (M9 -- unbounded growth)
                    try:
                        from scout.gainers.tracker import (
                            prune_old_snapshots as prune_gainers,
                        )
                        from scout.losers.tracker import (
                            prune_old_snapshots as prune_losers,
                        )

                        await prune_gainers(
                            db,
                            retention_days=settings.NARRATIVE_SNAPSHOT_RETENTION_DAYS,
                        )
                        await prune_losers(
                            db,
                            retention_days=settings.NARRATIVE_SNAPSHOT_RETENTION_DAYS,
                        )
                    except Exception:
                        logger.exception("tracker_prune_error")
                    # Prune old data from tables that had no pruning (H6)
                    try:
                        for table, col, days in [
                            ("volume_spikes", "detected_at", 30),
                            ("momentum_7d", "detected_at", 30),
                            ("trending_snapshots", "snapshot_at", 7),
                            ("learn_logs", "created_at", 90),
                            ("chain_matches", "completed_at", 30),
                            ("holder_snapshots", "scanned_at", 14),
                            ("volume_snapshots", "scanned_at", 14),
                            ("score_history", "scanned_at", 14),
                        ]:
                            try:
                                await db._conn.execute(
                                    f"DELETE FROM {table} WHERE datetime({col}) < datetime('now', '-{days} days')"
                                )
                            except Exception:
                                pass
                        await db._conn.commit()
                    except Exception:
                        logger.exception("extra_prune_error")
                    last_daily_learn_at = now
                    await strategy.set_timestamp("last_daily_learn_at", now)
                    logger.info("narrative.daily_learn_complete")
                except Exception:
                    logger.exception("narrative.daily_learn_error")

                # Paper trading daily digest
                if trading_engine:
                    try:
                        from scout.trading.digest import build_paper_digest

                        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                        digest_text = await build_paper_digest(db, today)
                        if digest_text:
                            try:
                                await send_telegram_message(
                                    digest_text, session, settings
                                )
                            except Exception:
                                logger.exception("trading_digest_send_error")
                        logger.info("trading.digest_complete", date=today)
                    except Exception:
                        logger.exception("trading_digest_error")

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
            logger.exception("narrative.loop_error")

        await asyncio.sleep(settings.NARRATIVE_POLL_INTERVAL)
