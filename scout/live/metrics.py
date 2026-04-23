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
    d = date_utc or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    await db._conn.execute(
        "INSERT INTO live_metrics_daily (date, metric, value) VALUES (?, ?, ?) "
        "ON CONFLICT(date, metric) DO UPDATE SET value = value + excluded.value",
        (d, metric, by),
    )
    await db._conn.commit()
