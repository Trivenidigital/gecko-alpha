"""UTC time helpers for SQL datetime predicates.

INF-04 / BL-DATETIME-NORMALIZATION. The recurring ``'T'``-vs-space day-boundary
bug (off-by-one #4, ``tasks/lessons.md``) comes from comparing a stored
ISO-8601 timestamp column — written by ``datetime.now(timezone.utc).isoformat()``
with a ``T`` separator and ``+00:00`` suffix — directly against a SQLite
``datetime('now', ...)`` bound, which renders space-separated and tz-less.
SQLite compares the two as TEXT; at character 10 ``'T'`` (0x54) > ``' '``
(0x20), so on the boundary day the predicate silently behaves as a whole-day
``DATE()`` comparison — an off-by-up-to-one-day error.

``sql_utc_cutoff`` returns a bound in the SAME ``.isoformat()`` format the
columns are stored in, so a predicate can compare like-for-like
(``WHERE opened_at >= ?``) without wrapping either side and without the
artifact. Bind the result as a query parameter; because both operands are
fixed-width UTC ISO-8601 strings with an identical ``+00:00`` suffix, their
lexicographic order matches chronological order and the column's index is
still usable (no function wrap on the column).

This mirrors the in-tree precedent in ``dashboard.db.get_dispatch_funnel``,
which already binds ``(datetime.now(timezone.utc) - timedelta(...)).isoformat()``
for exactly this reason.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

__all__ = ["sql_utc_cutoff"]


def sql_utc_cutoff(
    *,
    days: float = 0,
    hours: float = 0,
    minutes: float = 0,
    start_of_day: bool = False,
) -> str:
    """Return a UTC cutoff as an ISO-8601 string matching stored ``.isoformat()`` values.

    The cutoff is ``now - (days, hours, minutes)``; positive arguments look
    BACK in time, so a 30-day lookback lower bound is ``sql_utc_cutoff(days=30)``
    and a rolling 24h bound is ``sql_utc_cutoff(hours=24)``.

    Pass ``start_of_day=True`` for today's UTC midnight — the lower bound of a
    calendar-day window. It is mutually exclusive with the offset arguments.

    Bind the result as a query parameter and compare it directly against a
    column stored via ``datetime.now(timezone.utc).isoformat()`` (tz-aware
    ``+00:00``) — e.g. ``WHERE opened_at >= ?`` — so both operands share the
    ``T``-separated ISO-8601 format and the TEXT comparison is chronological.

    Raises:
        ValueError: if ``start_of_day`` is combined with any nonzero offset.
    """
    now = datetime.now(timezone.utc)
    if start_of_day:
        if days or hours or minutes:
            raise ValueError(
                "sql_utc_cutoff: start_of_day is mutually exclusive with "
                "days/hours/minutes offsets"
            )
        return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return (now - timedelta(days=days, hours=hours, minutes=minutes)).isoformat()
