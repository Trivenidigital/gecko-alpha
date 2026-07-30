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
# Initialize to -inf so the very first alert always passes the cooldown check.
# Using 0.0 is wrong on Linux where time.monotonic() can be small near boot
# (CI runners) — `now_ts - 0.0 >= cooldown` fails until the process has been
# alive for `cooldown` seconds. -inf removes that OS-dependence.
_last_alerted_ts: float = float("-inf")


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
    except aiosqlite.OperationalError as e:
        msg = str(e).lower()
        if "locked" in msg or "busy" in msg:
            await _record_fallback(combo_key, str(e), settings)
            return (True, "db_error_fallback_allow")
        log.exception(
            "suppression_db_operational_error",
            err_id="SUPP_DB_OP",
            combo_key=combo_key,
        )
        return (False, "error")
    except aiosqlite.Error:
        log.exception(
            "suppression_db_error",
            err_id="SUPP_DB_CORRUPT",
            combo_key=combo_key,
        )
        return (False, "error")

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

    # Parole window open — atomic decrement via the shared transaction-lock
    # discipline (db.transaction() = BEGIN IMMEDIATE + the process-wide
    # asyncio.Lock). The lock ensures two coroutines within the same event loop
    # (e.g. suppression.should_open, combo_refresh.refresh_combo, the chain
    # tracker, or the DEX discovery writer) can never hold overlapping
    # transactions on the one shared connection — the F2 collision that raised
    # "cannot start a transaction within a transaction" and led should_open's
    # old except-path to roll back a FOREIGN writer's uncommitted transaction.
    # SQLite's per-file locking still protects against separate Connection
    # objects (see test_concurrent_decrement_grants_only_one).
    #
    # ROLLBACK safety (F2 required change #2): db.transaction() only rolls back
    # a transaction it successfully opened, so a failed BEGIN here can never
    # destroy another path's transaction.
    try:
        async with db.transaction() as conn:
            cur = await conn.execute(
                "SELECT parole_trades_remaining FROM combo_performance "
                "WHERE combo_key = ? AND window = '30d'",
                (combo_key,),
            )
            reread = await cur.fetchone()
            remaining = reread[0] if reread else 0
            if remaining is None or remaining <= 0:
                # No decrement — the enclosing transaction commits a no-op.
                return (False, "parole_exhausted")
            await conn.execute(
                "UPDATE combo_performance SET parole_trades_remaining = ? "
                "WHERE combo_key = ? AND window = '30d'",
                (remaining - 1, combo_key),
            )
        return (True, "parole_retest")
    except aiosqlite.OperationalError as e:
        msg = str(e).lower()
        if "locked" in msg or "busy" in msg:
            await _record_fallback(combo_key, f"parole_decrement: {e}", settings)
            return (True, "db_error_fallback_allow")
        log.exception(
            "suppression_db_operational_error",
            err_id="SUPP_DB_OP",
            combo_key=combo_key,
        )
        return (False, "error")
    except aiosqlite.Error:
        log.exception(
            "suppression_db_error",
            err_id="SUPP_DB_CORRUPT",
            combo_key=combo_key,
        )
        return (False, "error")


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
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                # parse_mode=None — plain-text fail-open alert, no
                # formatting; passing explicit keeps §12b discipline even
                # when the message body looks safe today.
                await alerter.send_telegram_message(
                    msg, session, settings, parse_mode=None, source="suppression"
                )
        except Exception:
            log.exception("suppression_fallback_alert_dispatch_error")
