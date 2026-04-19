"""Suppression entry-gate (spec §5.2).

Must be imported only from `signals.py` dispatchers. The module-level state
(`_fallback_timestamps`, `_last_alerted_ts`) is process-local, which is safe
because gecko-alpha runs a single event-loop process.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone

import aiohttp
import aiosqlite
import structlog

from scout import alerter
from scout.db import Database

log = structlog.get_logger()

_FALLBACK_WINDOW_SEC = 3600
_fallback_timestamps: "deque[float]" = deque()
_last_alerted_ts: float = 0.0


def get_fallback_count() -> int:
    """Return current fallback-counter size. Public accessor for weekly digest."""
    return len(_fallback_timestamps)


async def should_open(db: Database, combo_key: str, *, settings) -> tuple[bool, str]:
    """Entry-gate: returns (allow, reason). Fail-open on DB error.

    `settings` is required so the fail-open alert can (a) respect
    `FEEDBACK_FALLBACK_ALERT_THRESHOLD` / `_COOLDOWN_SEC` and (b) build the
    real alerter.send_telegram_message(text, session, settings) payload.
    """
    try:
        cursor = await db._conn.execute(
            "SELECT suppressed, parole_at, parole_trades_remaining "
            "FROM combo_performance WHERE combo_key = ? AND window = '30d'",
            (combo_key,),
        )
        row = await cursor.fetchone()
    except aiosqlite.Error as e:
        await _record_fallback(combo_key, str(e), settings)
        return (True, "db_error_fallback_allow")

    if row is None:
        return (True, "cold_start")

    suppressed, parole_at, _ = row[0], row[1], row[2]

    if not suppressed:
        return (True, "ok")

    if parole_at is None:
        return (False, "suppressed")

    try:
        parole_dt = datetime.fromisoformat(parole_at)
    except (ValueError, TypeError) as e:
        await _record_fallback(combo_key, f"parole_at parse: {e}", settings)
        return (True, "db_error_fallback_allow")
    if parole_dt.tzinfo is None:
        parole_dt = parole_dt.replace(tzinfo=timezone.utc)
    if parole_dt > datetime.now(timezone.utc):
        return (False, "suppressed")

    # Parole window open — atomic decrement via BEGIN IMMEDIATE.
    # Note: aiosqlite serializes statements against a single Connection object.
    # BEGIN IMMEDIATE acquires a RESERVED lock at the SQLite file level, so
    # when two separate Connection objects (e.g. two Database instances at
    # the same file) race, the second BEGIN IMMEDIATE blocks until the first
    # commits — SQLite's per-file locking enforces the invariant. Same-conn
    # "nested BEGIN" is NOT a concurrency case in an asyncio single-loop
    # process; see test_concurrent_decrement_grants_only_one.
    try:
        await db._conn.execute("BEGIN IMMEDIATE")
        cur = await db._conn.execute(
            "SELECT parole_trades_remaining FROM combo_performance "
            "WHERE combo_key = ? AND window = '30d'",
            (combo_key,),
        )
        reread = await cur.fetchone()
        remaining = reread[0] if reread else 0
        if remaining is None or remaining <= 0:
            await db._conn.execute("COMMIT")
            return (False, "parole_exhausted")
        await db._conn.execute(
            "UPDATE combo_performance SET parole_trades_remaining = ? "
            "WHERE combo_key = ? AND window = '30d'",
            (remaining - 1, combo_key),
        )
        await db._conn.commit()
        return (True, "parole_retest")
    except aiosqlite.Error as e:
        try:
            await db._conn.execute("ROLLBACK")
        except aiosqlite.Error as rb_err:
            log.warning(
                "suppression_rollback_failed",
                combo_key=combo_key,
                err=str(rb_err),
                err_id="SUPP_ROLLBACK",
            )
        await _record_fallback(combo_key, f"parole_decrement: {e}", settings)
        return (True, "db_error_fallback_allow")


async def _record_fallback(combo_key: str, err: str, settings) -> None:
    """Log + maintain the fail-open counter; fire Telegram alert with cooldown."""
    global _last_alerted_ts
    log.error(
        "suppression_db_error",
        combo_key=combo_key,
        err=err,
        err_id="SUPP_DB_FAIL",
    )
    now_ts = time.monotonic()
    _fallback_timestamps.append(now_ts)
    while (
        _fallback_timestamps and now_ts - _fallback_timestamps[0] > _FALLBACK_WINDOW_SEC
    ):
        _fallback_timestamps.popleft()

    threshold = settings.FEEDBACK_FALLBACK_ALERT_THRESHOLD
    cooldown = settings.FEEDBACK_FALLBACK_ALERT_COOLDOWN_SEC
    if len(_fallback_timestamps) >= threshold and now_ts - _last_alerted_ts >= cooldown:
        _last_alerted_ts = now_ts
        msg = (
            f"⚠ Suppression fail-open fired {len(_fallback_timestamps)}x "
            f"in last hour. DB may be degraded — combos are currently ungated."
        )
        try:
            # One-shot aiohttp session — fallbacks are rare (DB-degraded),
            # so the overhead of opening+closing a connection pool once per
            # alert is acceptable vs. threading a long-lived session through
            # every dispatcher.
            async with aiohttp.ClientSession() as session:
                await alerter.send_telegram_message(msg, session, settings)
        except Exception:
            log.exception("suppression_fallback_alert_dispatch_error")
