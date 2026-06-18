"""Durable SQLite maintenance orchestration (P0 Part B).

Imported by ``scout.main`` and called once per hourly maintenance pass. Kept
free of aiohttp imports so it is unit-testable in isolation — ``scout.alerter``
is imported lazily only on the stale-reader alert path.

Order: probe -> incremental_vacuum (if freelist high) -> wal_checkpoint
(if WAL large OR a vacuum ran) -> stale-reader watchdog. Each step is wrapped
so one failure cannot crash the cycle, and a ``busy`` checkpoint is logged at
WARNING (never as success) — that was the silent failure behind the
2026-06-18 WAL bloat.
"""

from __future__ import annotations

import os

from scout.observability.sqlite_holder_watchdog import (
    find_stale_readers,
    scan_db_holders,
)

# In-memory per-pid alert dedup; resets on restart (re-alerting after a restart
# is acceptable/desirable). See project memory on in-memory telemetry.
_ALERTED_PIDS: set[int] = set()


def _reset_alert_dedup_for_tests() -> None:
    _ALERTED_PIDS.clear()


async def run_sqlite_maintenance(db, session, settings, logger) -> None:
    try:
        state = await db.probe_wal_state()
    except Exception:
        logger.exception("sqlite_maintenance_probe_failed")
        return
    freelist = int(state.get("freelist_count", 0))
    wal_bytes = int(state.get("wal_size_bytes", 0))

    ran_iv = False
    if (
        settings.SQLITE_INCREMENTAL_VACUUM_ENABLED
        and freelist > settings.SQLITE_INCREMENTAL_VACUUM_FREELIST_THRESHOLD
    ):
        try:
            logger.info("sqlite_incremental_vacuum_attempted", freelist=freelist)
            res = await db.run_incremental_vacuum(
                max_pages=settings.SQLITE_INCREMENTAL_VACUUM_MAX_PAGES
            )
            ran_iv = res.get("pages_reclaimed", 0) > 0
            logger.info("sqlite_incremental_vacuum_completed", **res)
        except Exception:
            logger.exception("sqlite_incremental_vacuum_failed")

    if settings.SQLITE_WAL_CHECKPOINT_ENABLED and (
        wal_bytes > settings.SQLITE_WAL_CHECKPOINT_THRESHOLD_BYTES or ran_iv
    ):
        try:
            logger.info("sqlite_wal_checkpoint_attempted", wal_bytes=wal_bytes)
            ck = await db.checkpoint_wal_truncate()
            if ck.get("busy", 1) == 0:
                logger.info("sqlite_wal_checkpoint_succeeded", **ck)
            else:
                # busy != 0 is NOT success — a reader is pinning the WAL.
                logger.warning("sqlite_wal_checkpoint_busy", **ck)
        except Exception:
            logger.exception("sqlite_wal_checkpoint_failed")

    if settings.SQLITE_STALE_READER_WATCHDOG_ENABLED:
        try:
            own = os.getpid()
            holders = scan_db_holders(
                [str(settings.DB_PATH)],
                own_pid=own,
                expected_units=settings.SQLITE_EXPECTED_SERVICE_UNITS,
            )
            logger.info("sqlite_stale_reader_scan", holders=len(holders))
            stale = find_stale_readers(
                holders,
                max_age_hours=settings.SQLITE_STALE_READER_MAX_AGE_HOURS,
                own_pid=own,
            )
            for h in stale:
                logger.warning(
                    "sqlite_stale_reader_detected",
                    pid=h.pid,
                    cmdline=h.cmdline[:200],
                    age_hours=round(h.age_seconds / 3600, 1),
                    cgroup=h.cgroup[:120],
                )
            if stale and settings.SQLITE_STALE_READER_ALERT_ENABLED:
                await _alert_stale_readers(stale, session, settings, logger)
        except Exception:
            logger.exception("sqlite_stale_reader_watchdog_failed")


async def _alert_stale_readers(stale, session, settings, logger) -> None:
    new = [h for h in stale if h.pid not in _ALERTED_PIDS]
    if not new:
        return
    from scout import alerter  # lazy: keep aiohttp out of the import path

    lines = ["WARNING stale scout.db reader(s) pinning the WAL:"]
    for h in new:
        lines.append(
            f"pid {h.pid} age {round(h.age_seconds / 3600, 1)}h :: {h.cmdline[:120]}"
        )
    lines.append("Kill the orphan(s) so wal_checkpoint can truncate.")
    logger.info("sqlite_stale_reader_alert_dispatched", pids=[h.pid for h in new])
    try:
        # Fold 2: send_telegram_message only raises on non-200 when
        # raise_on_failure=True — otherwise a failed send would falsely log
        # "delivered". Mark delivered/dedup ONLY after a non-raising call.
        await alerter.send_telegram_message(
            "\n".join(lines),
            session,
            settings,
            parse_mode=None,
            raise_on_failure=True,
            source="sqlite_stale_reader_watchdog",
        )
    except Exception:
        logger.exception("sqlite_stale_reader_alert_failed")
        return
    for h in new:
        _ALERTED_PIDS.add(h.pid)
    logger.info("sqlite_stale_reader_alert_delivered", pids=[h.pid for h in new])
