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
from scout.config import Settings, configure_cache, load_settings
from scout.counter.detail import fetch_coin_detail, extract_counter_data
from scout.counter.flags import compute_memecoin_flags
from scout.counter.scorer import score_counter_memecoin
from scout.db import Database
from scout.gate import evaluate
from scout.heartbeat import (
    IngestSourceSample,
    IngestWatchdogEvent,
    _heartbeat_stats,
    _maybe_emit_heartbeat,
    _reset_heartbeat_stats,
    observe_ingest_sources,
)
from scout.ingestion.coingecko import fetch_top_movers as cg_fetch_top_movers
from scout.ingestion.coingecko import fetch_trending as cg_fetch_trending
from scout.ingestion.coingecko import fetch_by_volume as cg_fetch_by_volume
from scout.ingestion.coingecko import fetch_midcap_gainers as cg_fetch_midcap_gainers
from scout.ingestion import coingecko as _cg_module
from scout.ingestion import dexscreener as _dex_module
from scout.ingestion import geckoterminal as _gt_module
from scout.ingestion.dexscreener import fetch_trending
from scout.ingestion.geckoterminal import fetch_trending_pools
from scout.ingestion.held_position_prices import fetch_held_position_prices
from scout.ingestion.holder_enricher import enrich_holders
from scout.narrative.agent import narrative_agent_loop
from scout.news.cryptopanic import (
    enrich_candidates_with_news,
    fetch_cryptopanic_posts,
)
from scout.news.schemas import classify_macro, classify_sentiment
from scout.ratelimit import coingecko_limiter
from scout.safety import is_safe
from scout.scorer import score
from scout.spikes.detector import (
    record_volume,
    detect_spikes,
    detect_7d_momentum,
    detect_slow_burn_7d,
)
from scout.gainers.tracker import store_top_gainers
from scout.losers.tracker import store_top_losers
from scout.velocity.detector import alert_velocity_detections, detect_velocity
from scout.trading.signals import (
    trade_chain_completions,
    trade_first_signals,
    trade_gainers,
    trade_losers,
    trade_trending,
    trade_volume_spikes,
)
from scout.briefing.collector import collect_briefing_data
from scout.briefing.synthesizer import split_message, synthesize_briefing
from scout import alerter
from scout.perp.enrichment import enrich_candidates_with_perp_anomalies
from scout.perp.watcher import run_perp_watcher
from scout.trading import combo_refresh as _combo_refresh
from scout.trading import weekly_digest as _weekly_digest

# BL-055 live-trading subsystem — wired into scout/main.py per spec §10.
from scout.live.binance_adapter import BinanceSpotAdapter
from scout.live.config import LiveConfig
from scout.live.engine import LiveEngine
from scout.live.kill_switch import KillSwitch
from scout.live.loops import (
    live_metrics_rollup_loop,
    override_staleness_loop,
    shadow_evaluator_loop,
)
from scout.live.reconciliation import (
    emit_live_startup_status,
    reconcile_open_shadow_trades,
)
from scout.live.resolver import OverrideStore, VenueResolver

logger = structlog.get_logger()

# Module-level tracking of pending social-loop restart tasks so the shutdown
# path can cancel them (preventing a fresh loop from spawning against a
# closed DB). Also prevents detached tasks from being GC'd mid-flight.
_social_restart_tasks: set[asyncio.Task] = set()
# Shared counter for consecutive social-loop restarts; resets on any success
# signal. Used by the done-callback to enforce LUNARCRUSH_MAX_CONSECUTIVE_RESTARTS.
_social_consecutive_restarts = [0]

# Consecutive combo_refresh failure counter; incremented on failure, reset to 0 on success.
# Used to trigger streak-alert when >= 3 consecutive failures occur.
_combo_refresh_failure_streak = 0
# Last streak value for which we sent an alert; prevents duplicate alerts on every loop
# iteration once the streak stays at or above 3. Reset to 0 when streak clears.
_combo_refresh_streak_last_alerted = 0

# Last YYYY-MM-DD that maybe_suspend_signals fired. Same idempotency pattern
# as last_refresh_date / last_digest_date — once per local day per process.
_last_suspension_date = ""

# Last YYYY-MM-DD that the weekly calibration dry-run scheduler fired.
# Same module-level-sentinel idempotency as _last_suspension_date.
# Test-only reset helper exposed below per project convention
# (mirrors `scout.trading.params.clear_cache_for_tests` shape).
_last_calibration_dryrun_date = ""


def _clear_calibration_dryrun_date_for_tests() -> None:
    """Reset module-level sentinel (test isolation per arch-D5 + adv-M1).
    Conftest registers an autouse fixture that calls this so T6 (sets
    sentinel to today) doesn't poison T8/T0."""
    global _last_calibration_dryrun_date
    _last_calibration_dryrun_date = ""


def _format_ingest_watchdog_event(event: IngestWatchdogEvent) -> str:
    if event.kind == "recovered":
        return (
            "Gecko-Alpha ingestion recovered\n"
            f"source={event.source}\n"
            f"last_success_at={event.last_success_at or '-'}"
        )
    return (
        "Gecko-Alpha ingestion starvation\n"
        f"source={event.source}\n"
        f"empty_cycles={event.consecutive_empty_cycles}\n"
        f"threshold={event.threshold}\n"
        f"last_success_at={event.last_success_at or '-'}\n"
        f"error={event.error or '-'}"
    )


async def _dispatch_ingest_watchdog_events(
    events: list[IngestWatchdogEvent],
    session: aiohttp.ClientSession,
    settings: Settings,
    *,
    dry_run: bool,
) -> None:
    for event in events:
        body = _format_ingest_watchdog_event(event)
        if dry_run:
            logger.info(
                "ingest_watchdog_alert_dry_run",
                kind=event.kind,
                source=event.source,
                consecutive_empty_cycles=event.consecutive_empty_cycles,
                threshold=event.threshold,
                last_success_at=event.last_success_at,
                error=event.error,
            )
            continue
        logger.info(
            "ingest_watchdog_alert_dispatched",
            kind=event.kind,
            source=event.source,
        )
        try:
            await alerter.send_telegram_message(
                body,
                session,
                settings,
                parse_mode=None,
                raise_on_failure=True,
            )
            logger.info(
                "ingest_watchdog_alert_delivered",
                kind=event.kind,
                source=event.source,
            )
        except Exception as exc:
            logger.warning(
                "ingest_watchdog_alert_failed",
                kind=event.kind,
                source=event.source,
                error=str(exc),
                error_type=type(exc).__name__,
            )


def _ingest_watchdog_samples_from_cycle(
    *,
    settings: Settings,
    dex_error: Exception | None,
    gecko_error: Exception | None,
    cg_movers_error: Exception | None,
    cg_trending_error: Exception | None,
    cg_by_volume_error: Exception | None,
    cg_midcap_error: Exception | None,
) -> list[IngestSourceSample]:
    samples: list[IngestSourceSample] = []
    if dex_error is not None:
        samples.append(
            IngestSourceSample(
                source="dexscreener:boosts",
                raw_count=0,
                error=str(dex_error),
            )
        )
    else:
        samples.extend(_dex_module.get_last_watchdog_samples())

    if gecko_error is not None:
        for chain in getattr(settings, "CHAINS", []):
            samples.append(
                IngestSourceSample(
                    source=f"geckoterminal:{chain}",
                    raw_count=0,
                    error=str(gecko_error),
                )
            )
    else:
        samples.extend(_gt_module.get_last_watchdog_samples())

    cg_errors = [
        ("coingecko:markets", cg_movers_error),
        ("coingecko:trending", cg_trending_error),
        ("coingecko:volume", cg_by_volume_error),
        ("coingecko:midcap", cg_midcap_error),
    ]
    error_sources = {source for source, exc in cg_errors if exc is not None}
    for source, exc in cg_errors:
        if exc is not None:
            samples.append(
                IngestSourceSample(source=source, raw_count=0, error=str(exc))
            )
    samples.extend(
        sample
        for sample in _cg_module.get_last_watchdog_samples()
        if sample.source not in error_sources
    )
    return samples


async def _run_feedback_schedulers(
    db,
    settings,
    last_refresh_date: str,
    last_digest_date: str,
    now_local: datetime,
) -> tuple[str, str]:
    """Run the nightly combo refresh and weekly digest if their windows fire.

    Pure side-effecting helper (no loop state) — the caller passes
    last-run sentinels + a clock, and gets the updated sentinels back.
    Takes a naive-local datetime (server wall-clock) deliberately so operators
    can set FEEDBACK_COMBO_REFRESH_HOUR / FEEDBACK_WEEKLY_DIGEST_HOUR in
    familiar local time, not UTC. Cron drift across DST is an accepted constraint.
    """
    global _combo_refresh_failure_streak, _combo_refresh_streak_last_alerted
    today_iso = now_local.strftime("%Y-%m-%d")

    # Nightly combo refresh (FEEDBACK_COMBO_REFRESH_HOUR, local)
    if (
        now_local.hour == settings.FEEDBACK_COMBO_REFRESH_HOUR
        and last_refresh_date != today_iso
    ):
        try:
            summary = await _combo_refresh.refresh_all(db, settings)
            logger.info("combo_refresh_done", **summary)
            _combo_refresh_failure_streak = 0
            _combo_refresh_streak_last_alerted = 0
            last_refresh_date = today_iso
        except Exception:
            _combo_refresh_failure_streak += 1
            logger.exception(
                "combo_refresh_loop_error",
                consecutive_failures=_combo_refresh_failure_streak,
            )
            if (
                _combo_refresh_failure_streak >= 3
                and _combo_refresh_streak_last_alerted == 0
            ):
                _combo_refresh_streak_last_alerted = _combo_refresh_failure_streak
                # Fire once per streak (reset when refresh succeeds).
                try:
                    async with aiohttp.ClientSession() as session:
                        await alerter.send_telegram_message(
                            f"⚠ combo_refresh failed {_combo_refresh_failure_streak}× "
                            f"in a row — check logs.",
                            session,
                            settings,
                        )
                except Exception:
                    logger.exception("combo_refresh_streak_alert_dispatch_error")

    # Tier 1b auto-suspension — daily hour-gate (SUSPENSION_CHECK_HOUR local).
    # No-op when SIGNAL_PARAMS_ENABLED=False (handled inside the helper).
    global _last_suspension_date
    if (
        now_local.hour == settings.SUSPENSION_CHECK_HOUR
        and _last_suspension_date != today_iso
    ):
        try:
            from scout.trading.auto_suspend import maybe_suspend_signals

            async with aiohttp.ClientSession() as session:
                suspended = await maybe_suspend_signals(db, settings, session=session)
            if suspended:
                logger.info("auto_suspend_pass", count=len(suspended))
            _last_suspension_date = today_iso
        except Exception:
            logger.exception("auto_suspend_loop_error")

    # Weekly digest (FEEDBACK_WEEKLY_DIGEST_WEEKDAY, _HOUR local)
    if (
        now_local.weekday() == settings.FEEDBACK_WEEKLY_DIGEST_WEEKDAY
        and now_local.hour == settings.FEEDBACK_WEEKLY_DIGEST_HOUR
        and last_digest_date != today_iso
    ):
        try:
            await _weekly_digest.send_weekly_digest(db, settings)
            last_digest_date = today_iso
        except Exception:
            logger.exception("weekly_digest_loop_error")

    # Weekly calibration dry-run alert
    # (CALIBRATION_DRY_RUN_WEEKDAY/_HOUR local; gated on
    # CALIBRATION_DRY_RUN_ENABLED kill-switch per adv-S1).
    # NEVER auto-applies — operator manually re-runs with --apply
    # after reviewing the Telegram message.
    global _last_calibration_dryrun_date
    if (
        settings.CALIBRATION_DRY_RUN_ENABLED
        and now_local.weekday() == settings.CALIBRATION_DRY_RUN_WEEKDAY
        and now_local.hour == settings.CALIBRATION_DRY_RUN_HOUR
        and _last_calibration_dryrun_date != today_iso
    ):
        try:
            from scout.trading.calibrate import (
                build_diffs,
                format_dryrun_telegram_message,
                telegram_token_looks_real,
            )

            diffs = await build_diffs(
                db,
                settings,
                window_days=settings.CALIBRATION_WINDOW_DAYS,
                min_trades=settings.CALIBRATION_MIN_TRADES,
                step=settings.CALIBRATION_STEP_SIZE_PCT,
                signal_filter=None,
                since_deploy=True,  # adv-M3: skip pre-apply contamination
            )
            actionable = sum(1 for d in diffs if d.changes)
            msg = format_dryrun_telegram_message(
                diffs,
                actionable,
                window_days=settings.CALIBRATION_WINDOW_DAYS,
            )
            # adv-S2 / silent-failure-C1: pass parse_mode=None — message body
            # contains [reason] brackets that Telegram's Markdown parser would
            # mis-handle as link anchors and reject the entire message with
            # HTTP 400. send_telegram_message accepts parse_mode kwarg as of
            # this PR (default "Markdown" preserves back-compat for other
            # callers).
            if telegram_token_looks_real(settings):
                # M3 fix: explicit 10s total timeout so a hanging Telegram API
                # doesn't block the entire feedback-scheduler callback.
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    await alerter.send_telegram_message(
                        msg, session, settings, parse_mode=None
                    )
                logger.info(
                    "calibration_dryrun_pass",
                    actionable=actionable,
                    total=len(diffs),
                )
            else:
                # M2 fix: warning-level (was info) so the placeholder-token
                # condition surfaces in operator dashboards filtered for
                # `level >= warning`. The project-wide token has been
                # placeholder for weeks; without warning surface, the
                # operator won't know calibration alerts are silently
                # log-skipping every Monday.
                logger.warning(
                    "calibration_dryrun_telegram_skipped",
                    reason="placeholder_token",
                    actionable=actionable,
                    total=len(diffs),
                )
            _last_calibration_dryrun_date = today_iso
        except (ImportError, AttributeError):
            # H3 fix: deterministic-class errors (typo on rename, missing
            # symbol) won't fix themselves on retry. Advance sentinel so
            # we don't burn 60 retries/hour. Log with distinct event so
            # the operator can grep separately.
            logger.exception("calibration_dryrun_terminal_error")
            _last_calibration_dryrun_date = today_iso
        except Exception:
            # Transient errors (DB, network) — don't advance sentinel so
            # we retry next minute within the hour window.
            logger.exception("calibration_dryrun_loop_error")

    return last_refresh_date, last_digest_date


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
            last_briefing_at = datetime.fromisoformat(last_str.replace("Z", "+00:00"))
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
            should_run = now.hour in briefing_hours and (
                last_briefing_at is None
                or (now - last_briefing_at).total_seconds() > 39600  # >11h
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
                    "risk_score": (
                        counter.risk_score if counter.risk_score is not None else 0
                    ),
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


async def _maybe_start_perp_watcher(settings, *, db, session) -> asyncio.Task | None:
    """Launch the perp watcher iff PERP_ENABLED. Returns the task or None."""
    if not settings.PERP_ENABLED:
        return None
    if not (settings.PERP_BINANCE_ENABLED or settings.PERP_BYBIT_ENABLED):
        logger.warning("perp_watcher_no_exchanges_enabled_noop")
        return None
    return asyncio.create_task(
        run_perp_watcher(session, db, settings),
        name="perp-watcher",
    )


async def _maybe_enrich_perp(tokens, *, db, settings):
    """Run perp enrichment iff PERP_ENABLED. Return tokens unchanged otherwise."""
    if not settings.PERP_ENABLED or db is None:
        return tokens
    return await enrich_candidates_with_perp_anomalies(tokens, db, settings)


def _combine_coin_market_rows(*raw_lists: list[dict]) -> list[dict]:
    """Combine /coins/markets-shaped rows with first-seen dedupe by CG id."""
    combined: list[dict] = []
    seen_ids: set[str] = set()
    for raw_list in raw_lists:
        for coin in raw_list:
            cid = coin.get("id", "")
            if cid and cid not in seen_ids:
                seen_ids.add(cid)
                combined.append(coin)
    return combined


async def _fetch_coingecko_lanes(
    session: aiohttp.ClientSession,
    settings: Settings,
    db: Database,
) -> tuple[list, list, list, list, list]:
    """Run CoinGecko lanes sequentially so 429 cooldown stops lower-priority fan-out."""

    async def _call(name: str, fn, *args):
        try:
            return await fn(*args)
        except Exception as exc:
            logger.warning("coingecko_lane_failed", lane=name, error=str(exc))
            return exc

    cg_movers = await _call("top_movers", cg_fetch_top_movers, session, settings)
    if coingecko_limiter.is_backing_off():
        logger.warning("coingecko_lanes_stopped_for_backoff", after="top_movers")
        return cg_movers, [], [], [], []

    cg_trending = await _call("trending", cg_fetch_trending, session, settings)
    if coingecko_limiter.is_backing_off():
        logger.warning("coingecko_lanes_stopped_for_backoff", after="trending")
        return cg_movers, cg_trending, [], [], []

    cg_by_volume = await _call("by_volume", cg_fetch_by_volume, session, settings)
    if coingecko_limiter.is_backing_off():
        logger.warning("coingecko_lanes_stopped_for_backoff", after="by_volume")
        return cg_movers, cg_trending, cg_by_volume, [], []

    cg_midcap_gainers = await _call(
        "midcap_gainers", cg_fetch_midcap_gainers, session, settings
    )
    if coingecko_limiter.is_backing_off():
        logger.warning("coingecko_lanes_stopped_for_backoff", after="midcap_gainers")
        return cg_movers, cg_trending, cg_by_volume, cg_midcap_gainers, []

    held_position_raw = await _call(
        "held_position_prices", fetch_held_position_prices, session, settings, db
    )
    return cg_movers, cg_trending, cg_by_volume, cg_midcap_gainers, held_position_raw


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
    # Held-position refresh lane (§12c-narrow remediation, BL-NEW-HELD-POSITION-REFRESH).
    # Refreshes price_cache for tokens currently in open paper_trades regardless
    # of whether they appear in any other ingestion lane. See
    # tasks/plan_held_position_price_freshness.md.
    _dex_module.clear_watchdog_samples()
    _gt_module.clear_watchdog_samples()
    _cg_module.clear_watchdog_samples()
    dex_tokens, gecko_tokens, cg_results = await asyncio.gather(
        fetch_trending(session, settings),
        fetch_trending_pools(session, settings),
        _fetch_coingecko_lanes(session, settings, db),
        return_exceptions=True,
    )
    if isinstance(cg_results, Exception):
        cg_movers = cg_results
        cg_trending = cg_results
        cg_by_volume = cg_results
        cg_midcap_gainers = cg_results
        held_position_raw = cg_results
    else:
        (
            cg_movers,
            cg_trending,
            cg_by_volume,
            cg_midcap_gainers,
            held_position_raw,
        ) = cg_results
    # Handle exceptions from gather
    dex_error = dex_tokens if isinstance(dex_tokens, Exception) else None
    gecko_error = gecko_tokens if isinstance(gecko_tokens, Exception) else None
    cg_movers_error = cg_movers if isinstance(cg_movers, Exception) else None
    cg_trending_error = cg_trending if isinstance(cg_trending, Exception) else None
    cg_by_volume_error = cg_by_volume if isinstance(cg_by_volume, Exception) else None
    cg_midcap_error = (
        cg_midcap_gainers if isinstance(cg_midcap_gainers, Exception) else None
    )
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
    if isinstance(cg_midcap_gainers, Exception):
        logger.warning(
            "CoinGecko midcap gainer scan failed", error=str(cg_midcap_gainers)
        )
        cg_midcap_gainers = []
    if isinstance(held_position_raw, Exception):
        logger.warning("held_position_refresh failed", error=str(held_position_raw))
        held_position_raw = []

    ingest_watchdog_events = observe_ingest_sources(
        _ingest_watchdog_samples_from_cycle(
            settings=settings,
            dex_error=dex_error,
            gecko_error=gecko_error,
            cg_movers_error=cg_movers_error,
            cg_trending_error=cg_trending_error,
            cg_by_volume_error=cg_by_volume_error,
            cg_midcap_error=cg_midcap_error,
        ),
        settings,
    )
    if ingest_watchdog_events:
        await _dispatch_ingest_watchdog_events(
            ingest_watchdog_events,
            session,
            settings,
            dry_run=dry_run,
        )

    # Cache raw CoinGecko prices for dashboard (zero extra API calls)
    all_raw = list(_cg_module.last_raw_markets)
    if _cg_module.last_raw_trending:
        all_raw.extend(_cg_module.last_raw_trending)
    if _cg_module.last_raw_by_volume:
        all_raw.extend(_cg_module.last_raw_by_volume)
    if _cg_module.last_raw_midcap_gainers:
        all_raw.extend(_cg_module.last_raw_midcap_gainers)
    if held_position_raw:
        all_raw.extend(held_position_raw)
    if all_raw:
        try:
            cached = await db.cache_prices(all_raw)
            logger.info("price_cache_updated", count=cached)
        except Exception:
            logger.exception("price_cache_error")

    # Combine raw market data from movers, hydrated trending, and volume scans.
    # Held-position rows refresh price_cache only; they should not create new signals.
    _raw_markets_combined = _combine_coin_market_rows(
        _cg_module.last_raw_markets,
        _cg_module.last_raw_trending,
        _cg_module.last_raw_by_volume,
        _cg_module.last_raw_midcap_gainers,
    )

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
                    await trade_volume_spikes(
                        trading_engine, db, spikes, settings=settings
                    )
        except Exception:
            logger.exception("volume_spike_error")

    # Top Gainers Tracker (zero extra API calls -- uses cached data)
    # Store + dispatch are split so dispatch errors don't hide storage errors.
    if settings.GAINERS_TRACKER_ENABLED and _raw_markets_combined:
        try:
            await store_top_gainers(
                db,
                _raw_markets_combined,
                min_change=settings.GAINERS_MIN_CHANGE,
                max_mcap=settings.GAINERS_MAX_MCAP,
            )
        except Exception:
            logger.exception("gainers_tracker_error")
        if trading_engine:
            try:
                await trade_gainers(
                    trading_engine,
                    db,
                    min_mcap=settings.PAPER_MIN_MCAP,
                    max_mcap=settings.PAPER_MAX_MCAP,
                    settings=settings,
                )
            except Exception:
                logger.exception("gainers_trade_dispatch_error")

    # Top Losers Tracker (contrarian dip-catch paper trades)
    if settings.LOSERS_TRACKER_ENABLED and _raw_markets_combined:
        try:
            await store_top_losers(
                db,
                _raw_markets_combined,
                max_drop=settings.LOSERS_MIN_DROP,
                max_mcap=settings.LOSERS_MAX_MCAP,
            )
        except Exception:
            logger.exception("losers_tracker_error")
        # losers_contrarian historically net-loses (-$581 / 109 trades);
        # disabled by default in prod via PAPER_SIGNAL_LOSERS_CONTRARIAN_ENABLED=False.
        if trading_engine and settings.PAPER_SIGNAL_LOSERS_CONTRARIAN_ENABLED:
            try:
                await trade_losers(
                    trading_engine,
                    db,
                    min_mcap=settings.PAPER_MIN_MCAP,
                    max_mcap=settings.PAPER_MAX_MCAP,
                    settings=settings,
                )
            except Exception:
                logger.exception("losers_trade_dispatch_error")

    # Chain pattern completions (BL-068 wiring 2026-05-01) — fires when a
    # chain_pattern triggers a chain_matches row. Long-hold defaults belong
    # in signal_params for signal_type='chain_completed' (max_duration=720h,
    # trail_pct=35, sl_pct=30) — these multi-day plays don't fit the
    # default 168h/20% trail. Function existed in signals.py:760 since
    # BL-064 but was never imported here, so 0 chain_completed paper trades
    # had ever opened. dispatcher self-bounds to last 5 min — no-op when
    # chain_patterns are deactivated or no recent matches.
    if trading_engine:
        try:
            await trade_chain_completions(trading_engine, db, settings=settings)
        except Exception:
            logger.exception("chain_completions_trade_dispatch_error")

    # 7-Day Momentum Scanner (zero extra API calls -- filters existing data)
    if settings.MOMENTUM_7D_ENABLED and _raw_markets_combined:
        try:
            momentum_7d = await detect_7d_momentum(
                db,
                _raw_markets_combined,
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
    # BL-075 Phase B: Slow-Burn Watcher (RIV-shape detector, research-only).
    if settings.SLOW_BURN_ENABLED and _raw_markets_combined:
        try:
            slow_burn = await detect_slow_burn_7d(
                db,
                _raw_markets_combined,
                min_7d_change=settings.SLOW_BURN_MIN_7D_CHANGE,
                max_1h_change=settings.SLOW_BURN_MAX_1H_CHANGE,
                max_mcap=settings.SLOW_BURN_MAX_MCAP,
                min_volume_24h=settings.SLOW_BURN_MIN_VOLUME,
                dedup_days=settings.SLOW_BURN_DEDUP_DAYS,
            )
            if slow_burn:
                logger.info(
                    "slow_burn_tokens",
                    count=len(slow_burn),
                    tokens=[s["symbol"] for s in slow_burn],
                    mcap_unknown_count=sum(
                        1 for s in slow_burn if not s.get("market_cap")
                    ),
                    also_in_momentum_count=sum(
                        1 for s in slow_burn if s.get("also_in_momentum_7d")
                    ),
                )
        except Exception:
            logger.exception("slow_burn_error")

    # Velocity Alerter (1h extreme-pump research alert, no paper trade)
    if settings.VELOCITY_ALERTS_ENABLED and _raw_markets_combined:
        try:
            velocity = await detect_velocity(db, _raw_markets_combined, settings)
            if velocity:
                await alert_velocity_detections(velocity, session, settings)
        except Exception:
            logger.exception("velocity_alert_error")

    # Stage 2: Aggregate
    all_candidates = aggregate(
        list(dex_tokens)
        + list(gecko_tokens)
        + list(cg_movers)
        + list(cg_trending)
        + list(cg_by_volume)
        + list(cg_midcap_gainers)
    )
    stats["tokens_scanned"] = len(all_candidates)

    # Kick off CryptoPanic fetch concurrently with enrichment (if enabled).
    # Never raises — short-circuits to [] on any failure.
    cryptopanic_task = None
    if settings.CRYPTOPANIC_ENABLED:
        cryptopanic_task = asyncio.create_task(
            fetch_cryptopanic_posts(session, settings)
        )

    try:
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
                await db.log_volume_snapshot(
                    token.contract_address, token.volume_24h_usd
                )

        # Stage 2.5: Perp enrichment (OI/funding anomalies from perp watcher)
        enriched = await _maybe_enrich_perp(enriched, db=db, settings=settings)

        # Await CryptoPanic fetch (launched before enrichment) with a 10s cap
        # so a stalled third-party call cannot extend the cycle indefinitely.
        if cryptopanic_task is not None:
            try:
                cp_posts = await asyncio.wait_for(cryptopanic_task, timeout=10.0)
            except Exception as e:
                logger.warning("cryptopanic_fetch_failed", error=str(e))
                cp_posts = []
            if cp_posts:
                # Persist posts (idempotent INSERT OR IGNORE).
                for post in cp_posts:
                    try:
                        sentiment = classify_sentiment(
                            post.votes_positive, post.votes_negative
                        )
                        is_macro = classify_macro(
                            post.currencies,
                            threshold=settings.CRYPTOPANIC_MACRO_MIN_CURRENCIES,
                        )
                        await db.insert_cryptopanic_post(
                            post, is_macro=is_macro, sentiment=sentiment
                        )
                    except Exception:
                        logger.exception(
                            "cryptopanic_persist_error", post_id=post.post_id
                        )
                # Tag candidates
                enriched = enrich_candidates_with_news(enriched, cp_posts, settings)
    finally:
        # Guarantee the task is not left pending if any exception in the
        # enrichment block unwinds run_cycle before wait_for is reached.
        # On the happy path the task is already .done() and cancel() is a no-op.
        # Fire-and-forget — do NOT await here; we don't care about the value
        # at this point and we don't want to block cleanup.
        if cryptopanic_task is not None and not cryptopanic_task.done():
            cryptopanic_task.cancel()

    # Stage 3: Score
    scored = []
    all_scored_tokens = []  # All tokens with updated quant_score/signals_fired
    for token in enriched:
        try:
            historical_scores = await db.get_recent_scores(
                token.contract_address, limit=3
            )
            points, signals = score(
                token, settings, historical_scores=historical_scores
            )
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
            logger.exception(
                "scoring_error", token=getattr(token, "contract_address", "?")
            )

    # Paper trade on first meaningful signal (earliest detection point)
    if trading_engine:
        scored_for_trading = [
            (t, t.quant_score, t.signals_fired)
            for t in all_scored_tokens
            if (t.quant_score or 0) > 0 and t.signals_fired
        ]
        if scored_for_trading:
            await trade_first_signals(
                trading_engine,
                db,
                scored_for_trading,
                min_mcap=settings.PAPER_MIN_MCAP,
                max_mcap=settings.PAPER_MAX_MCAP,
                settings=settings,
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


async def _maybe_announce_tg_alerts(db, session, settings) -> None:
    """BL-NEW-TG-ALERT-ALLOWLIST: first-deploy operator announcement.

    R2-C1 + R1-I4 design folds. Gated on 'announcement_sent' sentinel
    row in tg_alert_log so it fires exactly once per database lifetime
    regardless of engine restart count.
    """
    if db._conn is None:
        return
    try:
        cur = await db._conn.execute(
            "SELECT 1 FROM tg_alert_log WHERE outcome='announcement_sent' " "LIMIT 1"
        )
        if await cur.fetchone():
            return  # already announced
    except Exception:
        # Table may not exist on older schema during deploy transition;
        # don't crash — let the migration apply, then announce on next start.
        logger.exception("tg_alert_announcement_table_check_failed")
        return

    body = (
        "📢 TG alert allowlist active\n"
        "Allowed signals (paper-trade open): gainers_early, "
        "narrative_prediction, losers_contrarian, volume_spike\n"
        "Open-only — check dashboard for closes\n"
        "chain_completed via existing chain alerter\n"
        f"Per-token cooldown: {settings.TG_ALERT_PER_TOKEN_COOLDOWN_HOURS}h "
        "(reduce via .env TG_ALERT_PER_TOKEN_COOLDOWN_HOURS=2 for "
        "second-leg signals)\n"
        "To silence per-signal: UPDATE signal_params SET "
        "tg_alert_eligible=0 WHERE signal_type='...';"
    )
    try:
        await alerter.send_telegram_message(body, session, settings, parse_mode=None)
        async with db._txn_lock:
            await db._conn.execute(
                "INSERT INTO tg_alert_log "
                "(paper_trade_id, signal_type, token_id, alerted_at, outcome) "
                "VALUES (NULL, 'announcement', '_system', ?, "
                "'announcement_sent')",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await db._conn.commit()
        logger.info("tg_alert_announcement_sent")
    except Exception:
        # Don't block startup on announcement failure; will retry next start
        # because no sentinel row was written.
        logger.exception("tg_alert_announcement_failed")


async def _maybe_announce_m1_5c(db, session, settings) -> None:
    """BL-NEW-M1.5C: Minara DEX-eligibility onboarding announcement.

    Fires ONCE per database lifetime, gated on:
    - MINARA_ALERT_ENABLED=True (default; honors the feature flag)
    - Separate sentinel 'm1_5c_announcement_sent' in tg_alert_log
      (independent from M1.5b's 'announcement_sent')

    R2-C2 design-stage fold. R1-I1 fold: separate from M1.5b function to
    avoid refactor regression.
    """
    if db._conn is None:
        return
    if not getattr(settings, "MINARA_ALERT_ENABLED", True):
        return
    try:
        cur = await db._conn.execute(
            "SELECT 1 FROM tg_alert_log "
            "WHERE outcome='m1_5c_announcement_sent' LIMIT 1"
        )
        if await cur.fetchone():
            return  # already announced
    except Exception:
        logger.exception("tg_alert_m1_5c_announcement_check_failed")
        return

    body = (
        "📢 M1.5c — Minara DEX-eligibility extension active\n"
        "Solana-listed tokens now include a copy-pasteable command:\n"
        "  Run: minara swap --from USDC --to <addr> --amount-usd N\n"
        "\n"
        "First-time setup (one-time, on your local terminal):\n"
        "1. npm install -g minara@latest\n"
        "2. minara login --device  (browser device-code OAuth)\n"
        "3. minara deposit  (fund USDC + SOL gas on Solana)\n"
        "Docs: https://github.com/Minara-AI/skills\n"
        "\n"
        "Default size: $10. Override via .env MINARA_ALERT_AMOUNT_USD=N.\n"
        "Disable: MINARA_ALERT_ENABLED=False + restart.\n"
        "Tip: long-press the `Run:` line to copy only that line.\n"
        "Note: gecko-alpha does NOT execute — Minara prompts before swap."
    )
    try:
        await alerter.send_telegram_message(body, session, settings, parse_mode=None)
        async with db._txn_lock:
            await db._conn.execute(
                "INSERT INTO tg_alert_log "
                "(paper_trade_id, signal_type, token_id, alerted_at, outcome) "
                "VALUES (NULL, 'announcement', '_system', ?, "
                "'m1_5c_announcement_sent')",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await db._conn.commit()
        logger.info("tg_alert_m1_5c_announcement_sent")
    except Exception:
        logger.exception("tg_alert_m1_5c_announcement_failed")


async def _drain_pending_live_tasks(paper_trader, timeout_sec: float = 5.0) -> None:
    """Drain any in-flight PaperTrader live-handoff tasks before DB close.

    Spec §10.3 — orphaned shadow-row writes are a data-loss risk, so we wait
    for outstanding ``_pending_live_tasks`` to complete (or time out) before
    the DB connection is torn down. Never re-raises; a timeout logs WARN.
    """
    pending = getattr(paper_trader, "_pending_live_tasks", None)
    if not pending:
        return
    logger.info("live_shutdown_drain_begin", pending=len(pending))
    try:
        await asyncio.wait_for(
            asyncio.gather(*list(pending), return_exceptions=True),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "live_shutdown_drain_timeout",
            remaining=len(pending),
        )
    logger.info("live_shutdown_drain_done")


async def main(argv: list[str] | None = None) -> int:
    """Main entry point with CLI arg parsing and graceful shutdown.

    ``argv`` defaults to ``None`` so the CLI (``python -m scout.main``) reads
    ``sys.argv``. Tests pass an explicit list to drive the startup guard (spec
    §1.3) without invoking the shell.
    """
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
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Print resolved config and exit (no DB or HTTP init)",
    )
    args = parser.parse_args(argv)

    # --check-config runs BEFORE any DB / HTTP / live subsystem wiring so
    # operators can introspect resolved live-trading knobs on a stopped host.
    if args.check_config:
        s = load_settings()
        lc = LiveConfig(s)
        print(f"LIVE_MODE={lc.mode}")
        print(f"live_signal_allowlist_set={sorted(s.live_signal_allowlist_set)}")
        print(f"live_signal_sizes_map={dict(s.live_signal_sizes_map)}")
        print(f"resolve_tp_pct={lc.resolve_tp_pct()}")
        print(f"resolve_sl_pct={lc.resolve_sl_pct()}")
        print(f"resolve_max_duration_hours={lc.resolve_max_duration_hours()}")
        print(f"LIVE_DAILY_LOSS_CAP_USD={s.LIVE_DAILY_LOSS_CAP_USD}")
        print(f"LIVE_MAX_EXPOSURE_USD={s.LIVE_MAX_EXPOSURE_USD}")
        print(f"LIVE_MAX_OPEN_POSITIONS={s.LIVE_MAX_OPEN_POSITIONS}")
        return 0

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

    settings = load_settings()
    # Pre-populate the module-level settings cache to avoid async race
    # on first lazy get_settings() call during startup.
    configure_cache(settings)
    if args.min_score_override is not None:
        settings.MIN_SCORE = args.min_score_override
        logger.info("MIN_SCORE overridden", min_score=settings.MIN_SCORE)

    # Honour CoinGecko rolling-cap and burst-profile settings from config.
    from scout.ratelimit import configure_from_settings as _cg_ratelimit_configure

    _cg_ratelimit_configure(settings)

    db = Database(settings.DB_PATH)
    await db.initialize()

    # --- BL-055 live-trading wiring (spec §10) --------------------------------
    # Built BEFORE the TradingEngine so we can inject the LiveEngine into
    # PaperTrader via the engine constructor. In paper mode this is a no-op.
    live_config = LiveConfig(settings)
    live_engine: LiveEngine | None = None
    live_adapter: BinanceSpotAdapter | None = None
    live_kill_switch: KillSwitch | None = None
    _live_owned: list = []  # adapters to close() on graceful shutdown

    # BL-NEW-LIVE-HYBRID M1 v2.1 startup notification (Task 14):
    # post a Telegram alert ONCE when LIVE_TRADING_ENABLED=True at
    # process startup so the operator confirms the master kill state.
    # PR #86 V3-M1: only fire when mode='live' too — a paper-mode boot
    # with LIVE_TRADING_ENABLED=True is a sensible safety step (not
    # alarming) and shouldn't generate a "master kill OFF" alert.
    if getattr(settings, "LIVE_TRADING_ENABLED", False) and live_config.mode == "live":
        try:
            async with aiohttp.ClientSession() as _startup_session:
                await alerter.send_telegram_message(
                    (
                        "[live-trading] master kill OFF — "
                        f"LIVE_TRADING_ENABLED=True; LIVE_MODE={live_config.mode}; "
                        f"per-trade-cap=${settings.LIVE_TRADE_AMOUNT_USD}; "
                        f"agg-cap=${settings.LIVE_MAX_EXPOSURE_USD}; "
                        f"per-token-cap={settings.LIVE_MAX_OPEN_POSITIONS_PER_TOKEN}"
                    ),
                    _startup_session,
                    settings,
                    parse_mode=None,
                )
        except Exception:
            logger.exception("live_startup_notification_failed")

    if live_config.mode in ("shadow", "live"):
        if live_config.mode == "live":
            # M1.5a Layer 1 master-kill enforcement (replaces M1's
            # NotImplementedError): operator MUST flip LIVE_TRADING_ENABLED
            # to enter live mode.
            if not getattr(settings, "LIVE_TRADING_ENABLED", False):
                await db.close()
                raise RuntimeError(
                    "LIVE_MODE=live requires LIVE_TRADING_ENABLED=True "
                    "(Layer 1 master kill). Operator must set "
                    "LIVE_TRADING_ENABLED=True in .env after answering "
                    "the 4 design open questions + funding the venue."
                )
            if not settings.BINANCE_API_KEY or not settings.BINANCE_API_SECRET:
                await db.close()
                raise RuntimeError("LIVE_MODE=live requires BINANCE_API_KEY/SECRET")

            # M1.5a balance smoke check (replaces M1's NotImplementedError).
            # PR #86 V3-I2 fold: smoke check should also gate on
            # LIVE_USE_REAL_SIGNED_REQUESTS=True. Otherwise operator who
            # forgets that flag boots cleanly + every signal fires
            # 'live_signed_disabled' indefinitely.
            if not getattr(settings, "LIVE_USE_REAL_SIGNED_REQUESTS", False):
                await db.close()
                raise RuntimeError(
                    "LIVE_MODE=live requires LIVE_USE_REAL_SIGNED_REQUESTS=True. "
                    "Default False is the M1.5a emergency-revert posture; "
                    "operator must explicitly opt in to live execution by "
                    "setting LIVE_USE_REAL_SIGNED_REQUESTS=True in .env "
                    "after balance smoke check passes on testnet."
                )

            # R2-C1: verify SPOT permission, not just balance fetch
            # R2-I6: 10s timeout (was 5s), absorbs EU-VPS jitter
            # NOTE: BinanceSpotAdapter is already imported at module top
            # (line 66). Local import would shadow it as a function-local
            # name, breaking shadow-mode boot under Python scoping rules.
            from scout.live.binance_adapter import BinanceAuthError

            smoke_adapter = BinanceSpotAdapter(settings, db=db)
            smoke_start = time.monotonic()
            try:
                account = await asyncio.wait_for(
                    smoke_adapter._signed_get("/api/v3/account", params={}),
                    timeout=10.0,
                )
                permissions = account.get("permissions", []) or []
                if "SPOT" not in permissions:
                    await smoke_adapter.close()
                    await db.close()
                    raise RuntimeError(
                        f"LIVE_MODE=live boot blocked: API key lacks SPOT trade "
                        f"permission. Got permissions={permissions!r}. Verify in "
                        f"Binance API console (Edit Restrictions → Enable Spot "
                        f"& Margin Trading)."
                    )
                balances = account.get("balances", []) or []
                usdt_free = next(
                    (
                        float(b.get("free", "0"))
                        for b in balances
                        if b.get("asset") == "USDT"
                    ),
                    0.0,
                )
                smoke_dur_ms = int((time.monotonic() - smoke_start) * 1000)
                logger.info(
                    "live_smoke_check_passed",
                    usdt_free=usdt_free,
                    permissions=permissions,
                    duration_ms=smoke_dur_ms,
                )
            except BinanceAuthError as exc:
                await smoke_adapter.close()
                await db.close()
                raise RuntimeError(
                    f"LIVE_MODE=live smoke check auth-fail: {exc}. "
                    "Verify BINANCE_API_KEY/SECRET correctness + IP whitelist + "
                    "key not revoked + key has TRADE permission scope."
                ) from exc
            except Exception as exc:  # R1-I4: single Exception clause
                await smoke_adapter.close()
                await db.close()
                raise RuntimeError(
                    f"LIVE_MODE=live smoke check failed: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            await smoke_adapter.close()
        live_adapter = BinanceSpotAdapter(settings, db=db)
        _live_owned.append(live_adapter)
        resolver = VenueResolver(
            binance_adapter=live_adapter,
            override_store=OverrideStore(db),
            positive_ttl=timedelta(hours=1),
            negative_ttl=timedelta(seconds=60),
            db=db,
        )
        live_kill_switch = KillSwitch(db)

        # M1.5b: construct RoutingLayer when LIVE_USE_ROUTING_LAYER=True.
        # When the flag is False (default), routing=None is passed and the
        # engine's _dispatch_live branch is bypassed (M1.5a single-venue
        # behavior preserved). When True, the engine's __init__ misconfig
        # CRASH validates that LIVE_USE_REAL_SIGNED_REQUESTS is also True
        # (design §2.2).
        live_routing = None
        if getattr(settings, "LIVE_USE_ROUTING_LAYER", False):
            from scout.live.routing import RoutingLayer

            live_routing = RoutingLayer(
                db=db,
                settings=settings,
                adapters={"binance": live_adapter},
            )
            logger.info("routing_layer_constructed", venues=["binance"])

        live_engine = LiveEngine(
            config=live_config,
            resolver=resolver,
            adapter=live_adapter,
            db=db,
            kill_switch=live_kill_switch,
            routing=live_routing,
        )
        # Boot-time drift reconciliation + startup status (Task 16).
        await reconcile_open_shadow_trades(
            db=db,
            adapter=live_adapter,
            config=live_config,
            ks=live_kill_switch,
            settings=settings,
        )
        await emit_live_startup_status(
            db=db,
            adapter=live_adapter,
            config=live_config,
            ks=live_kill_switch,
        )
    # --------------------------------------------------------------------------

    # Paper trading engine
    from scout.trading.engine import TradingEngine

    trading_engine = None
    if settings.TRADING_ENABLED:
        trading_engine = TradingEngine(
            mode=settings.TRADING_MODE,
            db=db,
            settings=settings,
            live_engine=live_engine,
        )
        logger.info(
            "trading_engine_initialized",
            mode=settings.TRADING_MODE,
        )
        # Audit log of resolved paper-trading knobs. Settings uses extra="ignore"
        # so an env-var typo (e.g. PAPER_TRAILING_ACTIVATION_PC missing the T)
        # silently falls back to the default. Logging the resolved values once
        # at boot lets the operator spot typos by diffing expected vs. actual.
        logger.info(
            "paper_trading_config_resolved",
            trade_amount_usd=settings.PAPER_TRADE_AMOUNT_USD,
            max_open_trades=settings.PAPER_MAX_OPEN_TRADES,
            max_exposure_usd=settings.PAPER_MAX_EXPOSURE_USD,
            tp_pct=settings.PAPER_TP_PCT,
            sl_pct=settings.PAPER_SL_PCT,
            tp_sell_pct=settings.PAPER_TP_SELL_PCT,
            max_duration_hours=settings.PAPER_MAX_DURATION_HOURS,
            slippage_bps=settings.PAPER_SLIPPAGE_BPS,
            trailing_enabled=settings.PAPER_TRAILING_ENABLED,
            trailing_activation_pct=settings.PAPER_TRAILING_ACTIVATION_PCT,
            trailing_drawdown_pct=settings.PAPER_TRAILING_DRAWDOWN_PCT,
            trailing_floor_pct=settings.PAPER_TRAILING_FLOOR_PCT,
            gainers_max_24h_pct=settings.PAPER_GAINERS_MAX_24H_PCT,
            min_mcap=settings.PAPER_MIN_MCAP,
            max_mcap=settings.PAPER_MAX_MCAP,
            max_mcap_rank=settings.PAPER_MAX_MCAP_RANK,
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
    last_combo_refresh_date = ""  # empty so the first eligible hour fires
    last_weekly_digest_date = ""
    outcome_check_interval = 3600  # 1 hour
    _reset_heartbeat_stats()

    try:
        async with aiohttp.ClientSession() as session:
            # BL-NEW-TG-ALERT-ALLOWLIST (R1-I3+I5 design fold): wire the
            # session into trading_engine for TG alert dispatch. Engine was
            # constructed pre-session-context above; set_tg_session is the
            # post-construction hook.
            if trading_engine is not None:
                trading_engine.set_tg_session(session)

            # BL-NEW-TG-ALERT-ALLOWLIST (R2-C1 + R1-I4 design folds):
            # one-time first-deploy operator announcement gated on
            # 'announcement_sent' sentinel row in tg_alert_log. Fires
            # exactly once per database lifetime regardless of restart
            # count.
            await _maybe_announce_tg_alerts(db, session, settings)
            # M1.5c addition (R1-I1 fold: separate function, separate sentinel)
            await _maybe_announce_m1_5c(db, session, settings)

            async def _pipeline_loop() -> None:
                nonlocal cycle_count
                nonlocal last_outcome_check, last_summary_date
                nonlocal last_combo_refresh_date, last_weekly_digest_date

                while not shutdown_event.is_set():
                    try:
                        stats = await run_cycle(
                            settings,
                            db,
                            session,
                            dry_run=args.dry_run,
                            trading_engine=trading_engine,
                        )
                        logger.info("Cycle complete", **stats)
                        _heartbeat_stats["tokens_scanned"] += stats.get(
                            "tokens_scanned", 0
                        )
                        _heartbeat_stats["candidates_promoted"] += stats.get(
                            "candidates_promoted", 0
                        )
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

                        try:
                            await db.prune_perp_anomalies(
                                keep_days=settings.PERP_ANOMALY_RETENTION_DAYS
                            )
                        except Exception as e:
                            logger.warning("perp_anomaly_prune_error", error=str(e))

                        # BL-053: prune CryptoPanic posts older than retention cap
                        if settings.CRYPTOPANIC_ENABLED:
                            try:
                                pruned_cp = await db.prune_cryptopanic_posts(
                                    keep_days=settings.CRYPTOPANIC_RETENTION_DAYS
                                )
                                if pruned_cp:
                                    logger.info(
                                        "cryptopanic_pruned",
                                        rows_deleted=pruned_cp,
                                    )
                            except Exception:
                                logger.exception("cryptopanic_prune_failed")

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

                    # Nightly combo refresh + weekly digest scheduling
                    last_combo_refresh_date, last_weekly_digest_date = (
                        await _run_feedback_schedulers(
                            db,
                            settings,
                            last_combo_refresh_date,
                            last_weekly_digest_date,
                            datetime.now(),
                        )
                    )

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

            perp_task = await _maybe_start_perp_watcher(
                settings, db=db, session=session
            )

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

                tasks.append(asyncio.create_task(secondwave_loop(session, settings)))
            if settings.BRIEFING_ENABLED:
                tasks.append(asyncio.create_task(briefing_loop(session, settings, db)))
            if settings.CHAINS_ENABLED:
                tasks.append(asyncio.create_task(run_chain_tracker(db, settings)))

            # BL-064: TG social signals listener (default OFF). Launches a
            # Telethon user-session subscriber to curated channels.
            if settings.TG_SOCIAL_ENABLED:
                from scout.social.telegram.listener import (
                    run_listener as run_tg_social_listener,
                )

                tasks.append(
                    asyncio.create_task(
                        run_tg_social_listener(
                            db=db,
                            settings=settings,
                            engine=trading_engine,
                            http_session=session,
                        )
                    )
                )

            # BL-055 live-subsystem loops (spec §10) — only spawned when a
            # LiveEngine was constructed above. Each loop is independently
            # cancellable; failures inside iterations are logged and swallowed.
            if live_engine is not None:
                assert live_adapter is not None and live_kill_switch is not None
                tasks.append(
                    asyncio.create_task(
                        shadow_evaluator_loop(
                            db=db,
                            adapter=live_adapter,
                            config=live_config,
                            ks=live_kill_switch,
                            settings=settings,
                        )
                    )
                )
                tasks.append(
                    asyncio.create_task(
                        override_staleness_loop(
                            adapter=live_adapter,
                            db=db,
                            settings=settings,
                        )
                    )
                )
                tasks.append(
                    asyncio.create_task(
                        live_metrics_rollup_loop(
                            db=db,
                            session=session,
                            settings=settings,
                        )
                    )
                )

            # LunarCrush social-velocity loop runs OUTSIDE asyncio.gather --
            # a social crash must never take down the main pipeline. The
            # done-callback re-creates the task with a 30s back-off.
            social_task: asyncio.Task | None = None
            if getattr(settings, "LUNARCRUSH_ENABLED", False) and getattr(
                settings, "LUNARCRUSH_API_KEY", ""
            ):
                from scout.social.lunarcrush.loop import (
                    _make_done_callback,
                    run_social_loop,
                )

                def _spawn_social_task() -> asyncio.Task:
                    t = asyncio.create_task(
                        run_social_loop(settings, db, shutdown_event)
                    )
                    t.add_done_callback(
                        _make_done_callback(
                            restarter=_schedule_social_restart,
                            backoff_seconds=30.0,
                        )
                    )
                    return t

                def _schedule_social_restart(delay: float) -> None:
                    # Cap consecutive restarts -- if the loop keeps crashing
                    # right back up, leave the social tier down rather than
                    # cycling forever.
                    max_restarts = int(
                        getattr(
                            settings,
                            "LUNARCRUSH_MAX_CONSECUTIVE_RESTARTS",
                            5,
                        )
                    )
                    _social_consecutive_restarts[0] += 1
                    if _social_consecutive_restarts[0] > max_restarts:
                        logger.critical(
                            "social_loop_restart_cap_reached",
                            consecutive=_social_consecutive_restarts[0],
                            max=max_restarts,
                        )
                        return
                    # Pre-sleep shutdown guard.
                    if shutdown_event.is_set():
                        return

                    async def _restart() -> None:
                        try:
                            await asyncio.sleep(delay)
                        except asyncio.CancelledError:
                            return
                        # Post-sleep shutdown guard: the event may have
                        # been set while we were asleep.
                        if shutdown_event.is_set():
                            return
                        logger.info("social_loop_restarting", after_seconds=delay)
                        _spawn_social_task()

                    t = asyncio.create_task(_restart())
                    _social_restart_tasks.add(t)

                    def _cleanup(task: asyncio.Task) -> None:
                        _social_restart_tasks.discard(task)
                        if task.cancelled():
                            return
                        exc = task.exception()
                        if exc is not None:
                            logger.error(
                                "social_restart_task_crashed",
                                exc_info=exc,
                            )

                    t.add_done_callback(_cleanup)

                social_task = _spawn_social_task()
                logger.info("social_loop_task_spawned")

            # Both loops share the same session and rate limiter intentionally.
            # The coingecko_limiter (25 req/min) coordinates access; that IS
            # the back-pressure mechanism.
            await asyncio.gather(*tasks, return_exceptions=True)

            # Cancel any pending restart-task so it cannot spin up a fresh
            # social loop against the DB we're about to close.
            for t in list(_social_restart_tasks):
                t.cancel()

            # Cancel the perp watcher task (if running) on graceful shutdown.
            if perp_task is not None:
                perp_task.cancel()
                try:
                    await asyncio.wait_for(perp_task, timeout=5.0)
                except asyncio.CancelledError:
                    pass
                except asyncio.TimeoutError:
                    logger.warning("perp_task_shutdown_timeout_first_pass")
                    perp_task.cancel()
                    try:
                        await asyncio.wait_for(perp_task, timeout=2.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        logger.error("perp_task_shutdown_hard_timeout")
                except Exception:
                    logger.exception("perp_loop_shutdown_error")

            # Ensure the social task winds down cleanly after the main loops exit.
            if social_task is not None and not social_task.done():
                try:
                    await asyncio.wait_for(social_task, timeout=5.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    social_task.cancel()
                except Exception:
                    logger.exception("social_loop_shutdown_error")
    finally:
        # BL-055 §10.3 — drain any in-flight PaperTrader → LiveEngine handoff
        # tasks before tearing down the DB connection. Orphaned writes against
        # a closed connection corrupt shadow_trades accounting.
        if trading_engine is not None:
            try:
                await _drain_pending_live_tasks(trading_engine._paper_trader)
            except Exception:
                logger.exception("live_shutdown_drain_error")
        # Close any live adapters we own (Binance HTTP session, etc.).
        for adapter in _live_owned:
            if hasattr(adapter, "close"):
                try:
                    await adapter.close()
                except Exception:
                    logger.exception("live_adapter_close_error")
        await db.close()
        logger.info("Scanner stopped", cycles_completed=cycle_count)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
