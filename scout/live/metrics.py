"""UPSERT-increment helper for ``live_metrics_daily`` (BL-055 Task 11).

Used by :mod:`scout.live.binance_adapter` and :mod:`scout.live.resolver` to
record daily operational counters (rate-limit hits, resolver cache hit/miss,
shadow order outcomes). Imported optionally by those modules so the live
pipeline degrades gracefully when metrics are unavailable.
"""

from __future__ import annotations

from datetime import datetime, timezone

from scout.db import Database


async def inc(
    db: Database,
    metric: str,
    *,
    date_utc: str | None = None,
    by: int = 1,
) -> None:
    """UPSERT-increment a daily counter.

    Parameters
    ----------
    db:
        Open :class:`scout.db.Database` instance.
    metric:
        Counter name (e.g. ``"binance_rate_limit_hits"``).
    date_utc:
        Bucket date in ``YYYY-MM-DD``. Defaults to today UTC.
    by:
        Increment amount (default 1).
    """
    assert db._conn is not None, "Database must be initialized before inc()"
    assert db._txn_lock is not None, "Database must be initialized before inc()"
    d = date_utc or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Reviewer 2: every commit on the shared connection must hold _txn_lock so
    # concurrent writers cannot interleave executes and a half-open
    # transaction. Callers MUST NOT already hold _txn_lock (asyncio.Lock is
    # non-reentrant on the same task); the four callsites (engine.py L143/
    # L180/L216, kill_switch.py L285) all release the lock before calling
    # inc(), verified.
    async with db._txn_lock:
        await db._conn.execute(
            "INSERT INTO live_metrics_daily (date, metric, value) VALUES (?, ?, ?) "
            "ON CONFLICT(date, metric) DO UPDATE SET value = value + excluded.value",
            (d, metric, by),
        )
        await db._conn.commit()
