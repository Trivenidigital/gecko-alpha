"""Durable SQLite maintenance orchestration (P0 Part B).

Imported by ``scout.main`` and called once per hourly maintenance pass. Kept
free of aiohttp imports so it is unit-testable in isolation — ``scout.alerter``
is imported lazily only on the alert paths.

Order: incremental_vacuum (if freelist high) -> wal_checkpoint (if WAL large OR
a vacuum ran) -> stale-reader watchdog. Each step is wrapped so one failure
cannot crash the cycle, and a ``busy`` checkpoint is logged at WARNING (never as
success) — that was the silent failure behind the 2026-06-18 WAL bloat.

Two operator-alert paths (gate-3 failure-mode review):
- stale-reader watchdog: a non-allowlisted holder older than the age gate.
- consecutive-busy checkpoint: covers a WAL pinned by a holder younger than the
  age gate OR by an expected service (e.g. a long dashboard read), which the
  watchdog alone would not surface. Alerts after N consecutive busy checkpoints.
"""

from __future__ import annotations

import os

from scout.observability.sqlite_holder_watchdog import (
    find_stale_readers,
    proc_available,
    scan_db_holders,
)

# In-memory state; resets on restart (re-alerting after a restart is
# acceptable/desirable). See project memory on in-memory telemetry.
_ALERTED_PIDS: set[int] = set()
_consecutive_busy: int = 0
_busy_alerted: bool = False


def _reset_alert_dedup_for_tests() -> None:
    global _consecutive_busy, _busy_alerted
    _ALERTED_PIDS.clear()
    _consecutive_busy = 0
    _busy_alerted = False


async def run_sqlite_maintenance(db, session, settings, logger, *, state) -> None:
    """``state`` is the already-probed ``probe_wal_state()`` dict — probed ONCE
    by the caller and shared with the WAL-profile observability block, so the
    hourly loop never double-probes."""
    global _consecutive_busy, _busy_alerted
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

    if settings.SQLITE_WAL_CHECKPOINT_ENABLED:
        if wal_bytes > settings.SQLITE_WAL_CHECKPOINT_THRESHOLD_BYTES or ran_iv:
            try:
                logger.info("sqlite_wal_checkpoint_attempted", wal_bytes=wal_bytes)
                ck = await db.checkpoint_wal_truncate()
                if ck.get("busy", 1) == 0:
                    _consecutive_busy = 0
                    _busy_alerted = False
                    logger.info("sqlite_wal_checkpoint_succeeded", **ck)
                else:
                    # busy != 0 is NOT success — a reader is pinning the WAL.
                    _consecutive_busy += 1
                    logger.warning(
                        "sqlite_wal_checkpoint_busy",
                        consecutive=_consecutive_busy,
                        **ck,
                    )
                    if (
                        _consecutive_busy
                        >= settings.SQLITE_WAL_CHECKPOINT_BUSY_ALERT_THRESHOLD
                        and not _busy_alerted
                        and settings.SQLITE_STALE_READER_ALERT_ENABLED
                    ):
                        delivered = await _alert_persistent_busy(
                            str(settings.DB_PATH),
                            session,
                            settings,
                            logger,
                            consecutive=_consecutive_busy,
                        )
                        _busy_alerted = delivered
            except Exception:
                logger.exception("sqlite_wal_checkpoint_failed")
        else:
            # WAL below threshold and no vacuum this pass -> healthy; clear the
            # busy episode so a future pin re-alerts.
            _consecutive_busy = 0
            _busy_alerted = False

    if settings.SQLITE_STALE_READER_WATCHDOG_ENABLED:
        try:
            own = os.getpid()
            if not proc_available():
                # Distinguish a blind watchdog from a genuine zero-holder scan.
                logger.warning("sqlite_stale_reader_scan_unavailable")
            else:
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


async def _alert_persistent_busy(
    db_path, session, settings, logger, *, consecutive
) -> bool:
    """Alert on a persistently-busy checkpoint, listing ALL current holders
    (no age gate) so a sub-age-gate or expected-service WAL pin is attributable.
    Returns True only on a non-raising send."""
    from scout import alerter  # lazy

    try:
        holders = scan_db_holders(
            [db_path],
            own_pid=os.getpid(),
            expected_units=settings.SQLITE_EXPECTED_SERVICE_UNITS,
        )
    except Exception:
        logger.exception("sqlite_wal_checkpoint_busy_alert_scan_failed")
        holders = []

    lines = [
        f"WARNING scout.db wal_checkpoint busy x{consecutive} — WAL not truncating."
    ]
    if holders:
        lines.append("Current scout.db holders:")
        for h in holders:
            tag = "service" if h.is_expected_service else "NON-SERVICE"
            lines.append(
                f"pid {h.pid} {tag} age {round(h.age_seconds / 3600, 1)}h "
                f":: {h.cmdline[:100]}"
            )
    else:
        lines.append("No non-pipeline holders visible via /proc.")
    lines.append("A pinned WAL bloats unbounded — investigate the holder(s).")
    logger.info("sqlite_wal_checkpoint_busy_alert_dispatched", consecutive=consecutive)
    try:
        await alerter.send_telegram_message(
            "\n".join(lines),
            session,
            settings,
            parse_mode=None,
            raise_on_failure=True,
            source="sqlite_wal_checkpoint_busy",
        )
    except Exception:
        logger.exception("sqlite_wal_checkpoint_busy_alert_failed")
        return False
    logger.info("sqlite_wal_checkpoint_busy_alert_delivered", consecutive=consecutive)
    return True
