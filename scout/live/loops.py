"""Scheduled loops for the live subsystem (BL-055 Task 18, spec §10).

Three independently-cancellable loops:

* :func:`shadow_evaluator_loop` - periodic close-check every
  ``TRADE_EVAL_INTERVAL_SEC`` seconds. Wraps
  :func:`scout.live.shadow_evaluator.evaluate_open_shadow_trades`.
* :func:`override_staleness_loop` - daily audit of ``venue_overrides`` at
  UTC 12:00. Probes Binance via
  :meth:`scout.live.adapter_base.ExchangeAdapter.fetch_exchange_info_row`
  and logs WARN for any active override whose pair is no longer listed.
* :func:`live_metrics_rollup_loop` - daily summary of today's
  ``live_metrics_daily`` rows at UTC 00:30. Logs an INFO summary and -
  best-effort - posts it to Telegram.

Each loop matches the :func:`scout.main.briefing_loop` idiom: inner work is
wrapped in try/except (log and continue), ``asyncio.CancelledError`` is
re-raised so graceful shutdown is observable.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import aiohttp
import structlog

from scout.alerter import send_telegram_message
from scout.config import Settings
from scout.db import Database
from scout.live.shadow_evaluator import evaluate_open_shadow_trades

if TYPE_CHECKING:  # pragma: no cover
    from scout.live.adapter_base import ExchangeAdapter
    from scout.live.config import LiveConfig
    from scout.live.kill_switch import KillSwitch

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Scheduling helpers
# ---------------------------------------------------------------------------


def compute_next_run_utc(
    now: datetime, target_hour: int, target_minute: int
) -> datetime:
    """Return the next UTC datetime at ``target_hour:target_minute``.

    If the target has already passed today (or equals ``now``), wraps to
    tomorrow. ``now`` must be tz-aware UTC.
    """
    target = now.replace(
        hour=target_hour, minute=target_minute, second=0, microsecond=0
    )
    if target <= now:
        target += timedelta(days=1)
    return target


async def _sleep_until(target: datetime) -> None:
    """Sleep until ``target`` (UTC). Separate helper so tests can patch it."""
    delta = (target - datetime.now(timezone.utc)).total_seconds()
    if delta > 0:
        await asyncio.sleep(delta)


# ---------------------------------------------------------------------------
# shadow_evaluator_loop
# ---------------------------------------------------------------------------


async def shadow_evaluator_loop(
    *,
    db: Database,
    adapter: "ExchangeAdapter",
    config: "LiveConfig",
    ks: "KillSwitch",
    settings: Settings,
    interval_sec: float | None = None,
) -> None:
    """Periodic shadow-trade scanner (spec §6.2 + §10.5).

    Calls :func:`evaluate_open_shadow_trades` every
    ``TRADE_EVAL_INTERVAL_SEC`` seconds (default 60s). Unhandled exceptions
    inside a single iteration are logged at ERROR and the loop continues;
    only ``asyncio.CancelledError`` terminates the loop.
    """
    sleep_for = (
        interval_sec
        if interval_sec is not None
        else float(getattr(settings, "TRADE_EVAL_INTERVAL_SEC", 60.0))
    )
    log.info("shadow_evaluator_loop_started", interval_sec=sleep_for)
    while True:
        try:
            await evaluate_open_shadow_trades(
                db=db,
                adapter=adapter,
                config=config,
                ks=ks,
                settings=settings,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("shadow_evaluator_loop_iteration_failed", error=str(exc))
        await asyncio.sleep(sleep_for)


# ---------------------------------------------------------------------------
# override_staleness_loop
# ---------------------------------------------------------------------------


async def _run_override_staleness_audit(
    adapter: "ExchangeAdapter", db: Database
) -> list[str]:
    """Probe every non-disabled ``venue_overrides`` row. Returns the list of
    symbols whose pair is no longer listed on the venue (stale)."""
    assert db._conn is not None
    cur = await db._conn.execute(
        "SELECT symbol, venue, pair FROM venue_overrides WHERE disabled = 0"
    )
    rows = await cur.fetchall()

    stale: list[str] = []
    for symbol, venue, pair in rows:
        try:
            row = await adapter.fetch_exchange_info_row(pair)
        except Exception as exc:
            log.warning(
                "override_staleness_probe_failed",
                symbol=symbol,
                pair=pair,
                venue=venue,
                error=str(exc),
            )
            continue
        if row is None:
            stale.append(symbol)
            log.warning(
                "live_override_stale_detected",
                symbol=symbol,
                pair=pair,
                venue=venue,
            )
    log.info(
        "override_staleness_audit",
        active=len(rows),
        stale=len(stale),
    )
    return stale


async def override_staleness_loop(
    *,
    adapter: "ExchangeAdapter",
    db: Database,
    settings: Settings,
) -> None:
    """Daily 12:00 UTC audit of ``venue_overrides`` (spec §10).

    Walks every active override and probes the adapter. Stale entries
    (pair no longer listed) are logged at WARN - operators are expected to
    investigate and either update or disable the override.
    """
    # The settings arg is unused today but kept in the signature so a future
    # WARN-alert batch can pull from settings.TELEGRAM_* without a re-wiring
    # pass. Keeps the call-site in scout.main stable across §10 follow-ups.
    del settings

    log.info("override_staleness_loop_started")
    while True:
        now = datetime.now(timezone.utc)
        target = compute_next_run_utc(now, target_hour=12, target_minute=0)
        await _sleep_until(target)
        try:
            await _run_override_staleness_audit(adapter, db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("override_staleness_loop_failed", error=str(exc))


# ---------------------------------------------------------------------------
# live_metrics_rollup_loop
# ---------------------------------------------------------------------------


async def _collect_daily_metrics(db: Database, date_utc: str) -> list[tuple[str, int]]:
    """Fetch all ``(metric, value)`` rows for the given UTC date."""
    assert db._conn is not None
    cur = await db._conn.execute(
        "SELECT metric, value FROM live_metrics_daily WHERE date = ? "
        "ORDER BY metric ASC",
        (date_utc,),
    )
    return [(m, int(v)) for (m, v) in await cur.fetchall()]


def _format_rollup(date_utc: str, rows: list[tuple[str, int]]) -> str:
    """Render the daily metric rollup as a short Telegram-friendly string."""
    if not rows:
        return f"live metrics {date_utc}: (no counters recorded)"
    lines = [f"live metrics {date_utc}:"]
    for metric, value in rows:
        lines.append(f"  {metric} = {value}")
    return "\n".join(lines)


async def live_metrics_rollup_loop(
    *,
    db: Database,
    session: aiohttp.ClientSession,
    settings: Settings,
) -> None:
    """Daily 00:30 UTC summary of ``live_metrics_daily`` (spec §10).

    Logs an INFO summary every day and - best-effort - posts it to
    Telegram. Send failures are logged at WARN but never crash the loop.
    """
    log.info("live_metrics_rollup_loop_started")
    while True:
        now = datetime.now(timezone.utc)
        target = compute_next_run_utc(now, target_hour=0, target_minute=30)
        await _sleep_until(target)
        try:
            # Per plan: "reads today's live_metrics_daily rows". At 00:30 UTC
            # the bucket for "today" is mostly empty - the interesting data
            # is the just-ended day. Summarise yesterday so the digest covers
            # a full 24h window; fall back to today if yesterday is empty
            # (e.g. fresh install, test harness seeding today's bucket).
            yday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            rows = await _collect_daily_metrics(db, yday)
            summary_date = yday
            if not rows:
                rows = await _collect_daily_metrics(db, today)
                summary_date = today
            body = _format_rollup(summary_date, rows)
            log.info(
                "live_metrics_daily_summary",
                date=summary_date,
                metric_count=len(rows),
            )
            try:
                await send_telegram_message(body, session, settings)
            except Exception as send_exc:
                log.warning("live_metrics_rollup_send_failed", error=str(send_exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("live_metrics_rollup_loop_failed", error=str(exc))
