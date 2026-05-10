"""BL-033: Module-level heartbeat state and periodic heartbeat logging.

Tracks cumulative pipeline stats across cycles and emits a structured
"heartbeat" log every HEARTBEAT_INTERVAL_SECONDS so operators can see
the pipeline is alive.

BL-075 Phase A (2026-05-03) adds `mcap_null_with_price_count` —
increments on each CoinGecko ingestion parse where market_cap was
null/0 but current_price was positive. Surfaces the silent-rejection
rate at the mcap=0 floor that caused the RIV (riv-coin) miss.
"""

from datetime import datetime, timezone

import structlog

logger = structlog.get_logger()

_heartbeat_stats: dict = {
    "started_at": None,
    "tokens_scanned": 0,
    "candidates_promoted": 0,
    "alerts_fired": 0,
    "narrative_predictions": 0,
    "counter_scores_memecoin": 0,
    "counter_scores_narrative": 0,
    "mcap_null_with_price_count": 0,
    "slow_burn_detected_today": 0,
    "last_heartbeat_at": None,
}


def _reset_heartbeat_stats() -> None:
    """Reset module-level heartbeat state (test helper).

    Note: _heartbeat_stats is a module-level global. Tests MUST call this
    function (or the pipeline's own reset path) to avoid cross-test pollution.
    """
    _heartbeat_stats.update(
        started_at=None,
        tokens_scanned=0,
        candidates_promoted=0,
        alerts_fired=0,
        narrative_predictions=0,
        counter_scores_memecoin=0,
        counter_scores_narrative=0,
        mcap_null_with_price_count=0,
        slow_burn_detected_today=0,
        last_heartbeat_at=None,
    )


def increment_mcap_null_with_price() -> None:
    """BL-075 Phase A: bump null-mcap-with-price counter (called from ingestion).

    Fires when CoinGecko returns a token with market_cap=null/0 but
    current_price>0 — the silent-rejection shape that caused the RIV
    (riv-coin) 100x miss on 2026-05-03.
    """
    _heartbeat_stats["mcap_null_with_price_count"] += 1


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
        mcap_null_with_price_count=_heartbeat_stats["mcap_null_with_price_count"],
        slow_burn_detected_today=_heartbeat_stats["slow_burn_detected_today"],
        last_heartbeat_at=_heartbeat_stats["last_heartbeat_at"].isoformat(),
    )
    _heartbeat_stats["last_heartbeat_at"] = now
    return True


def increment_slow_burn_detected(count: int = 1) -> None:
    """BL-075 Phase B: bump slow-burn detection counter (called from detector)."""
    _heartbeat_stats["slow_burn_detected_today"] += count
