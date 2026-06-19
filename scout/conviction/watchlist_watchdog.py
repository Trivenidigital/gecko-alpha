"""Prospective-watchlist freshness watchdog (§12a, BL-NEW-CONVICTION-FORWARD-MEASUREMENT).

A new pipeline table ships with its freshness SLO + watchdog (CLAUDE.md §12a). This
keys off the run HEARTBEAT (``latest_conviction_watchlist_run_at`` — written every
build, incl. 0-row + fail-closed), NOT the latest snapshot row, so it can tell
"builder ran, found 0" from "builder never ran". On staleness it logs a WARNING and
fires a §12b operator alert (``parse_mode=None`` + dispatched/delivered logs).

Status: ``ok`` (run within SLO) | ``down`` (run older than SLO) | ``unknown``
(never run — transient on fresh deploy until the first hourly build; not alerted to
avoid a deploy false-alarm).
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

logger = structlog.get_logger()

_alerted = False  # in-memory per-episode dedup; resets on a healthy run / restart


def _reset_for_tests() -> None:
    global _alerted
    _alerted = False


def _parse_dt(value: str) -> datetime:
    s = str(value).strip().replace("Z", "+00:00")
    if "T" not in s and " " in s:
        s = s.replace(" ", "T", 1)
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def check_watchlist_freshness(
    db, session, settings, logger=logger, *, now: datetime | None = None
) -> str:
    """Return ``ok|down|unknown`` for the prospective-watchlist build freshness."""
    global _alerted
    if not getattr(settings, "CONVICTION_PROSPECTIVE_ENABLED", True):
        return "ok"
    now = now or datetime.now(timezone.utc)
    run = await db.latest_conviction_watchlist_run()
    if run is None:
        # Genuinely never ran (fresh deploy before the first hourly build). A
        # builder CRASH writes a 'failed' heartbeat (main.py), so None here is not
        # a hidden failure — don't alert (avoids a deploy false-alarm).
        logger.info(
            "conviction_watchlist_freshness", status="unknown", reason="never_run"
        )
        return "unknown"

    run_at = run["run_at"]
    age_min = (now - _parse_dt(run_at)).total_seconds() / 60.0
    slo = settings.CONVICTION_WATCHLIST_SNAPSHOT_SLO_MINUTES
    if age_min > slo:
        reason = "stale"
    elif run.get("status") != "ok":
        # Fresh heartbeat but the build did not produce a valid snapshot
        # (failed / skipped_exclusion_failed) — that IS a down state (P1 fold).
        reason = run.get("status") or "unknown_status"
    else:
        _alerted = False  # healthy → re-arm
        logger.info(
            "conviction_watchlist_freshness",
            status="ok",
            age_minutes=round(age_min, 1),
        )
        return "ok"

    logger.warning(
        "conviction_watchlist_snapshot_stale",
        status="down",
        reason=reason,
        age_minutes=round(age_min, 1),
        slo_minutes=slo,
        run_at=run_at,
    )
    if not _alerted:
        await _alert_stale(run_at, age_min, slo, reason, session, settings, logger)
    return "down"


async def _alert_stale(run_at, age_min, slo, reason, session, settings, logger) -> None:
    global _alerted
    from scout import alerter  # lazy: keep aiohttp out of the import path

    if reason == "stale":
        detail = f"no snapshot in {round(age_min / 60, 1)}h (SLO {slo}m)"
    else:
        detail = f"last build status={reason}"
    body = (
        f"WARNING prospective conviction watchlist DOWN — {detail}. "
        f"Last run: {run_at}. Check gecko-pipeline + the hourly maintenance loop."
    )
    logger.info("conviction_watchlist_alert_dispatched", run_at=run_at, reason=reason)
    try:
        await alerter.send_telegram_message(
            body,
            session,
            settings,
            parse_mode=None,
            raise_on_failure=True,
            source="conviction_watchlist_watchdog",
        )
    except Exception:
        logger.exception("conviction_watchlist_alert_failed")
        return
    _alerted = True
    logger.info("conviction_watchlist_alert_delivered", run_at=run_at)
