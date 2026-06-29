"""C6 — instrumentation watchdogs (freshness + data-quality) and metric emit.

Freshness alone is insufficient (B3): a table can be *fresh but semantically
empty*. This emits the two coverage metrics and raises alarms on the
fresh-but-empty silent-failure signature. All alerts are system-health only and
route to the operator/health channel (C3) — never the trading/signal channel.
Gated by ``DEX_INSTRUMENTATION_ENABLED``: a no-op when disabled.

No top-level aiohttp import (``alerter`` is imported lazily) so this is unit-
testable without the network stack.
"""

import structlog

logger = structlog.get_logger(__name__)

# In-memory dedup: re-alert only when the failure reason changes.
_last_alert_reason: str | None = None


def _reset_dedup() -> None:
    """Test hook to clear the module-level dedup state."""
    global _last_alert_reason
    _last_alert_reason = None


async def check_dex_instrumentation_health(db, session, settings, log=None) -> list[str]:
    """Emit coverage metrics and return the list of fired quality alarms.

    Observe-only. Returns ``[]`` when disabled or healthy.
    """
    log = log or logger
    if not getattr(settings, "DEX_INSTRUMENTATION_ENABLED", False):
        return []

    stats = await db.dex_quality_stats()
    cov = await db.compute_dex_coverage_metrics()

    # Metric emit (acceptance bar #4/#5) — observable every maintenance tick.
    log.info(
        "dex_instrumentation_metrics",
        dex_resolution_health=cov["dex_resolution_health"],
        dex_measurable_cohort_size=cov["dex_measurable_cohort_size"],
        listed_dex=cov["listed_dex"],
        entry_total=stats["entry_total"],
        entry_nonzero_rate=stats["entry_nonzero_rate"],
        txns_total=stats["txns_total"],
        txns_nonnull_rate=stats["txns_nonnull_rate"],
        map_total=stats["map_total"],
        map_resolved=stats["map_resolved"],
    )

    alarms = _compute_alarms(stats, cov, settings)
    if alarms:
        await _alert(alarms, session, settings, log)
    return alarms


def _compute_alarms(stats: dict, cov: dict, settings) -> list[str]:
    """Pure quality-alarm logic (no I/O) — the fresh-but-empty detector (B3).

    A rate is only checked when its table has rows: a present-but-near-zero rate
    is the silent-failure signature; an empty table is just "no data yet".
    """
    alarms: list[str] = []
    if stats["entry_total"] > 0 and (stats["entry_nonzero_rate"] or 0.0) < settings.DEX_NONZERO_MCAP_FLOOR:
        alarms.append(
            f"entry_mcap fresh-but-empty: {stats['entry_nonzero_rate']:.2f} finalized "
            f"of {stats['entry_total']} rows"
        )
    if stats["txns_total"] > 0 and (stats["txns_nonnull_rate"] or 0.0) < settings.DEX_NONNULL_TXNS_FLOOR:
        alarms.append(
            f"txns_h1_buys fresh-but-empty: {stats['txns_nonnull_rate']:.2f} non-null "
            f"of {stats['txns_total']} rows"
        )
    if stats["map_total"] > 0 and stats["map_resolved"] == 0:
        alarms.append(
            f"resolver fresh-but-empty: {stats['map_total']} map rows, 0 resolved"
        )
    if cov["listed_dex"] > 0 and cov["dex_resolution_health"] < settings.DEX_RESOLUTION_HEALTH_FLOOR:
        alarms.append(
            f"dex_resolution_health below floor: {cov['dex_resolution_health']:.2f} "
            f"< {settings.DEX_RESOLUTION_HEALTH_FLOOR}"
        )
    return alarms


async def _alert(alarms, session, settings, log) -> None:
    """Send a health alert to the operator/health channel. Deduped by reason."""
    global _last_alert_reason
    from scout import alerter  # lazy: keep aiohttp out of the import path

    reason = " | ".join(sorted(alarms))
    if reason == _last_alert_reason:
        return
    health_chat = getattr(settings, "TELEGRAM_HEALTH_CHAT_ID", "") or None
    body = "WARNING dex-instrumentation health: " + reason
    log.info("dex_instrumentation_alert_dispatched", reason=reason)
    try:
        await alerter.send_telegram_message(
            body,
            session,
            settings,
            parse_mode=None,
            raise_on_failure=True,
            source="dex_instrumentation_watchdog",
            chat_id=health_chat,
        )
    except Exception:
        log.exception("dex_instrumentation_alert_failed")
        return
    _last_alert_reason = reason
    log.info("dex_instrumentation_alert_delivered", reason=reason)
