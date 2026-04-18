"""asyncio.Task entry point for the LunarCrush social-velocity loop.

Runs independently of the main scan cycle. Lifecycle:

1. Startup: hydrate baselines + credit ledger from DB, prune old
   social_signals rows per ``LUNARCRUSH_RETENTION_DAYS``.
2. Each cycle: if credit budget not exhausted, fetch /coins/list/v2,
   run the detector, apply the buffered-commit pattern (design spec §8).
3. Every ``LUNARCRUSH_CHECKPOINT_EVERY_N_POLLS`` cycles or on graceful
   shutdown: flush dirty baselines + credit ledger in one transaction.
4. Shutdown via ``shutdown_event`` or ``asyncio.CancelledError`` -- the
   ``finally`` block flushes state before exiting.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

import aiohttp
import structlog

from scout.social.baselines import (
    BaselineCache,
    flush_baselines,
    hydrate_baselines,
)
from scout.social.lunarcrush.alerter import format_social_alert, send_social_alert
from scout.social.lunarcrush.client import LunarCrushClient
from scout.social.lunarcrush.credits import CreditLedger, flush_credit_ledger
from scout.social.lunarcrush.detector import detect_spikes
from scout.social.lunarcrush.price import get_price_change_1h
from scout.social.models import ResearchAlert

if TYPE_CHECKING:
    from scout.config import Settings
    from scout.db import Database

logger = structlog.get_logger(__name__)


class _AuthDisabled(Exception):
    """Raised internally to break out of the loop on 401/403."""


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _insert_alerts(
    db: "Database", alerts: list[ResearchAlert]
) -> None:
    """INSERT OR IGNORE each alert; commit once. Raises on DB errors.

    UNIQUE(coin_id, detected_at) provides TOCTOU-safe dedup even if the
    detector orchestrator somehow queued two detections for the same coin.

    Atomicity: if any INSERT raises mid-batch, rollback the open
    transaction so partial rows do not leak into a later commit from a
    different code path, then re-raise. Alerts stay un-persisted; the
    caller treats the batch as failed and re-enters detection next cycle.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    try:
        for a in alerts:
            kinds = {k.value for k in a.spike_kinds}
            await db._conn.execute(
                """INSERT OR IGNORE INTO social_signals (
                    coin_id, symbol, name,
                    fired_social_volume_24h, fired_galaxy_jump, fired_interactions_accel,
                    galaxy_score, social_volume_24h, social_volume_baseline,
                    social_spike_ratio, interactions_24h, sentiment,
                    social_dominance, price_change_1h, price_change_24h,
                    market_cap, current_price, detected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    a.coin_id,
                    a.symbol,
                    a.name,
                    1 if "social_volume_24h" in kinds else 0,
                    1 if "galaxy_jump" in kinds else 0,
                    1 if "interactions_accel" in kinds else 0,
                    a.galaxy_score,
                    a.social_volume_24h,
                    a.social_volume_baseline,
                    a.social_spike_ratio,
                    a.interactions_24h,
                    a.sentiment,
                    a.social_dominance,
                    a.price_change_1h,
                    a.price_change_24h,
                    a.market_cap,
                    a.current_price,
                    a.detected_at.isoformat(),
                ),
            )
        await db._conn.commit()
    except Exception:
        try:
            await db._conn.rollback()
        except Exception:
            logger.exception("social_insert_rollback_error")
        raise


async def _prune_old_rows(db: "Database", retention_days: int) -> int:
    """Delete ``social_signals`` rows older than ``retention_days``."""
    if db._conn is None or retention_days <= 0:
        return 0
    cursor = await db._conn.execute(
        """DELETE FROM social_signals
           WHERE datetime(detected_at) < datetime('now', '-' || ? || ' days')""",
        (int(retention_days),),
    )
    await db._conn.commit()
    return cursor.rowcount or 0


# ---------------------------------------------------------------------------
# Cycle body
# ---------------------------------------------------------------------------


SendFn = Callable[[list[ResearchAlert]], Awaitable[bool]]


async def _process_cycle(
    settings: "Settings",
    db: "Database",
    cache: BaselineCache,
    coins: list[dict],
    *,
    current_poll_interval: Optional[int] = None,
    send_fn: SendFn,
) -> int:
    """Run detector + transactional commit + Telegram for one cycle.

    Returns number of alerts dispatched. ``send_fn`` returns True if the
    Telegram call succeeded. DB insert failures cause the buffered-commit
    to be dropped (baseline stays in sync with the row that actually
    exists in the DB).
    """
    alerts, buffered_states = await detect_spikes(
        db,
        settings,
        cache,
        coins,
        current_poll_interval=current_poll_interval,
    )

    if not alerts:
        # Non-firing coins already had their baselines committed inline.
        return 0

    # Enrich price_change_1h from the CoinGecko raw-markets cache.
    enriched: list[ResearchAlert] = []
    for a in alerts:
        ch_1h, ch_24h = get_price_change_1h(a.symbol, a.coin_id)
        # Only override when we actually found something -- detector may
        # already have copied across values from the LC payload.
        updates: dict = {}
        if ch_1h is not None:
            updates["price_change_1h"] = ch_1h
        if ch_24h is not None and a.price_change_24h is None:
            updates["price_change_24h"] = ch_24h
        enriched.append(a.model_copy(update=updates) if updates else a)

    # Transactional commit: DB first, then Telegram, then cache.
    try:
        await _insert_alerts(db, enriched)
    except Exception:
        logger.exception("social_insert_failed", count=len(enriched))
        # Drop buffered baseline updates for firing coins -- keeps the
        # in-memory cache in sync with the DB row that was NOT inserted.
        return 0

    # DB succeeded -- commit buffered baseline updates ONLY for coins
    # whose alerts actually survived dedup + top-N truncation. A firing
    # coin that got dropped by either filter leaves its pre-state in the
    # cache (spec §8 step 10: cache-consistency invariant with DB rows).
    surviving_ids = {a.coin_id for a in enriched}
    for coin_id, state in buffered_states.items():
        if coin_id not in surviving_ids:
            continue
        cache.set(coin_id, state)
        cache.mark_dirty(coin_id)

    # Telegram dispatch (best-effort -- baseline stays committed even on fail).
    try:
        ok = await send_fn(enriched)
        if not ok:
            logger.warning("social_alert_send_returned_false", count=len(enriched))
    except Exception:
        logger.exception("social_alert_send_error", count=len(enriched))

    return len(enriched)


# ---------------------------------------------------------------------------
# Restart wiring
# ---------------------------------------------------------------------------


def _make_done_callback(
    *,
    restarter: Callable[[float], None],
    backoff_seconds: float = 30.0,
):
    """Return a done-callback that schedules a task restart after an uncaught crash."""

    def _cb(task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.exception("social_loop_task_crashed", exc_info=exc)
            restarter(backoff_seconds)

    return _cb


# ---------------------------------------------------------------------------
# Main loop entry point
# ---------------------------------------------------------------------------


async def run_social_loop(
    settings: "Settings",
    db: "Database",
    shutdown_event: asyncio.Event,
) -> None:
    """Run the LunarCrush social-velocity loop until ``shutdown_event`` fires.

    Double kill-switch at the top: if LUNARCRUSH_ENABLED is off or the API
    key is empty, returns immediately without starting the loop.
    """
    if not getattr(settings, "LUNARCRUSH_ENABLED", False):
        logger.info("social_loop_disabled_by_flag")
        return
    if not getattr(settings, "LUNARCRUSH_API_KEY", ""):
        logger.info("social_loop_disabled_no_api_key")
        return

    cache = BaselineCache()
    ledger = CreditLedger(settings)
    client = LunarCrushClient(settings)
    poll_counter = 0

    try:
        # Startup: hydrate + prune.
        await hydrate_baselines(db, cache)
        await ledger.hydrate(db)
        pruned = await _prune_old_rows(
            db, int(settings.LUNARCRUSH_RETENTION_DAYS)
        )
        if pruned:
            logger.info("social_retention_pruned", rows_deleted=pruned)
        logger.info(
            "social_loop_started",
            baseline_coins=len(cache),
            credits_used=ledger.credits_used,
        )

        while not shutdown_event.is_set():
            try:
                await _run_one_cycle(settings, db, client, cache, ledger)
            except asyncio.CancelledError:
                raise
            except _AuthDisabled:
                logger.warning("social_loop_auth_disabled_exiting")
                break
            except Exception:
                logger.exception("social_loop_cycle_error")

            poll_counter += 1
            # Periodic checkpoint.
            checkpoint_every = int(
                settings.LUNARCRUSH_CHECKPOINT_EVERY_N_POLLS
            )
            if checkpoint_every > 0 and poll_counter % checkpoint_every == 0:
                try:
                    await flush_baselines(db, cache)
                    await flush_credit_ledger(db, ledger)
                except Exception:
                    logger.exception("social_checkpoint_error")

            # Wait for next cycle or shutdown.
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=ledger.current_poll_interval(),
                )
            except asyncio.TimeoutError:
                pass  # normal -- interval elapsed

    except asyncio.CancelledError:
        pass
    finally:
        try:
            await flush_baselines(db, cache)
            await flush_credit_ledger(db, ledger)
        except Exception:
            logger.exception("social_final_flush_error")
        try:
            await client.close()
        except Exception:
            logger.exception("social_client_close_error")
        logger.info("social_loop_exited")


async def _run_one_cycle(
    settings: "Settings",
    db: "Database",
    client: LunarCrushClient,
    cache: BaselineCache,
    ledger: CreditLedger,
) -> None:
    """One detector + alert cycle. Respects credit-budget short-circuit."""
    ledger.maybe_rollover()
    if ledger.is_exhausted():
        logger.warning(
            "social_credit_budget_exhausted",
            credits_used=ledger.credits_used,
        )
        return
    if ledger.is_soft_budget_hit():
        logger.info(
            "social_credit_budget_near",
            credits_used=ledger.credits_used,
        )

    coins, credit_cost = await client.fetch_coins_list()
    ledger.consume(credit_cost)
    if client.disabled:
        # 401 / 403 path: shut the loop down cleanly.
        logger.warning("social_loop_auth_disabled_exiting")
        raise _AuthDisabled()
    if not coins:
        return

    # Use a per-cycle Telegram session for the alerter so errors don't
    # spill into the LunarCrush client session.
    async def _dispatch(alerts: list[ResearchAlert]) -> bool:
        async with aiohttp.ClientSession() as session:
            return await send_social_alert(alerts, session, settings)

    await _process_cycle(
        settings,
        db,
        cache,
        coins,
        current_poll_interval=ledger.current_poll_interval(),
        send_fn=_dispatch,
    )
