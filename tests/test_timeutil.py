"""Tests for scout.timeutil — INF-04 / BL-DATETIME-NORMALIZATION.

The helper's contract is that its output shares the exact serialization format
of columns stored via ``datetime.now(timezone.utc).isoformat()``, so a bound
value can be compared like-for-like against such a column without the
``'T'``-vs-space day-boundary artifact.
"""

from datetime import datetime, timedelta, timezone

import pytest

from scout.timeutil import sql_utc_cutoff


def test_cutoff_is_isoformat_tz_aware():
    """Output must be a ``T``-separated, ``+00:00`` ISO-8601 string — the same
    format columns are stored in (paper_trades.opened_at, signal_events.created_at,
    category_snapshots.snapshot_at)."""
    cutoff = sql_utc_cutoff(hours=24)
    assert "T" in cutoff
    assert cutoff.endswith("+00:00")
    # Round-trips through fromisoformat and is tz-aware UTC.
    parsed = datetime.fromisoformat(cutoff)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_cutoff_matches_stored_column_format():
    """A stored column value and a cutoff produced the same way must be
    byte-for-byte comparable (same prefix structure)."""
    stored = datetime.now(timezone.utc).isoformat()
    cutoff = sql_utc_cutoff(days=30)
    # Same separator, same offset suffix, same field widths up to seconds.
    assert stored[10] == cutoff[10] == "T"
    assert stored.endswith("+00:00") and cutoff.endswith("+00:00")


def test_days_hours_minutes_look_backward():
    before = datetime.now(timezone.utc)
    cutoff = sql_utc_cutoff(days=1, hours=2, minutes=30)
    after = datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(cutoff)
    lo = before - timedelta(days=1, hours=2, minutes=30)
    hi = after - timedelta(days=1, hours=2, minutes=30)
    assert lo <= parsed <= hi


def test_zero_offset_is_now():
    before = datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(sql_utc_cutoff())
    after = datetime.now(timezone.utc)
    assert before <= parsed <= after


def test_start_of_day_is_utc_midnight():
    cutoff = sql_utc_cutoff(start_of_day=True)
    parsed = datetime.fromisoformat(cutoff)
    assert (parsed.hour, parsed.minute, parsed.second, parsed.microsecond) == (
        0,
        0,
        0,
        0,
    )
    assert parsed.date() == datetime.now(timezone.utc).date()
    assert parsed.tzinfo is not None


def test_start_of_day_rejects_offsets():
    """Guard against silent misuse — combining start_of_day with an offset is
    a programming error, not a silently-ignored argument."""
    with pytest.raises(ValueError):
        sql_utc_cutoff(days=7, start_of_day=True)
    with pytest.raises(ValueError):
        sql_utc_cutoff(hours=1, start_of_day=True)
