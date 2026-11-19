"""Descriptive read-only counts of recorded Telegram outcomes, not receipts."""

from datetime import datetime, timedelta, timezone

import aiosqlite
import structlog

from dashboard.db import _ro_db

_log = structlog.get_logger()
_OUTCOMES = (
    "sent",
    "blocked_eligibility",
    "blocked_cooldown",
    "blocked_dedup_24h",
    "dispatch_failed",
    "announcement_sent",
    "m1_5c_announcement_sent",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def get_telegram_outcomes(db_path: str, days: int = 1) -> dict:
    """Count ledger events in an inclusive UTC window without invoking writers."""
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 30:
        raise ValueError("days must be an integer between 1 and 30")
    as_of = _utc_now()
    start = (as_of - timedelta(days=days)).isoformat()
    end = as_of.isoformat()
    meta = {
        "ok": True,
        "read_only": True,
        "historical_only": True,
        "generated_at": end,
        "window_start": start,
        "as_of": end,
        "days": days,
        "time_policy": "sqlite_julianday_inclusive_naive_utc",
    }
    # Only fixed local constants are interpolated; timestamps are SQL parameters.
    known = ",".join(f"'{name}'" for name in _OUTCOMES)
    buckets = (*_OUTCOMES, "other_recorded_outcomes")
    sums = ",\n".join(
        f"COALESCE(SUM(CASE WHEN in_window AND bucket='{name}' THEN 1 ELSE 0 END),0) AS {name}"
        for name in buckets
    )
    sql = f"""
        WITH parsed AS (
            SELECT outcome, detail, julianday(alerted_at) AS stamp,
                   CASE WHEN outcome IN ({known}) THEN outcome
                        ELSE 'other_recorded_outcomes' END AS bucket
            FROM tg_alert_log
        ), classified AS (
            SELECT *, stamp BETWEEN julianday(?) AND julianday(?) AS in_window,
                   CASE WHEN detail GLOB 'universe_filter:*' THEN 'paper_open_universe'
                        WHEN detail GLOB 'detection_lane:universe_filter:*' THEN 'detection_universe'
                        ELSE 'other_or_unspecified' END AS eligibility
            FROM parsed
        )
        SELECT COALESCE(SUM(CASE WHEN in_window THEN 1 ELSE 0 END),0) AS total_events,
               COALESCE(SUM(CASE WHEN stamp IS NULL THEN 1 ELSE 0 END),0) AS invalid_timestamps,
               COALESCE(SUM(CASE WHEN stamp > julianday(?) THEN 1 ELSE 0 END),0) AS future_timestamps,
               {sums},
               COALESCE(SUM(CASE WHEN in_window AND outcome='blocked_eligibility'
                   AND eligibility='paper_open_universe' THEN 1 ELSE 0 END),0) AS paper_open_universe,
               COALESCE(SUM(CASE WHEN in_window AND outcome='blocked_eligibility'
                   AND eligibility='detection_universe' THEN 1 ELSE 0 END),0) AS detection_universe,
               COALESCE(SUM(CASE WHEN in_window AND outcome='blocked_eligibility'
                   AND eligibility='other_or_unspecified' THEN 1 ELSE 0 END),0) AS other_or_unspecified
        FROM classified
    """
    try:
        async with _ro_db(db_path) as conn:
            await conn.execute("BEGIN")
            cursor = await conn.execute(sql, (start, end, end))
            row = await cursor.fetchone()
        meta.update(
            table_wide_invalid_timestamp_count=row["invalid_timestamps"],
            table_wide_future_timestamp_count=row["future_timestamps"],
        )
        return {
            "meta": meta,
            "total_events": row["total_events"],
            "outcomes": {name: row[name] for name in buckets},
            "blocked_eligibility": {
                name: row[name]
                for name in (
                    "paper_open_universe",
                    "detection_universe",
                    "other_or_unspecified",
                )
            },
        }
    except Exception as exc:
        reason = "query_failed"
        if isinstance(exc, FileNotFoundError):
            reason = "database_unavailable"
        elif isinstance(exc, aiosqlite.OperationalError) and (
            "no such table:" in str(exc) or "no such column:" in str(exc)
        ):
            reason = "schema_unavailable"
        _log.warning(
            "telegram_outcomes_unavailable",
            reason=reason,
            exception_type=type(exc).__name__,
            exc_info=reason == "query_failed",
        )
        return {
            "meta": {**meta, "ok": False, "data_missing_reason": reason},
            "total_events": None,
            "outcomes": None,
            "blocked_eligibility": None,
        }
