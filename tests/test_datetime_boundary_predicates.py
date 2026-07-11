"""Boundary behaviour of the INF-04 datetime predicates.

Two representative shapes are pinned here (the ones fixed across scout/,
dashboard/ and scripts/):

1. Rolling window ``ts >= <N-unit cutoff>`` — combo_refresh, bl060 audit,
   dashboard category/signal_events.
2. Calendar-day ``ts >= <today's UTC midnight>`` — the tg_social cashtag cap
   (dispatcher) and its dashboard mirror.

Columns are stored as Python ``.isoformat()`` ('T'-separated, ``+00:00``). The
tests use stdlib ``sqlite3`` so they exercise SQLite's actual TEXT comparison —
the exact engine behaviour the bug and fix depend on — without importing the
aiohttp-bearing production modules (Windows OpenSSL constraint).
"""

from datetime import datetime, time, timedelta

import sqlite3

from scout.timeutil import sql_utc_cutoff


def _table():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (opened_at TEXT)")
    return conn


def _count_ge(conn, bound: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM t WHERE opened_at >= ?", (bound,)
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# Predicate 1 — rolling N-unit window
# ---------------------------------------------------------------------------


def test_window_predicate_space_bound_artifact_vs_isoformat_fix():
    """Same-day rows straddling a non-midnight cutoff, both directions.

    A space-separated bound (the old ``datetime('now','-30 days')`` form) keeps
    a row from EARLIER the same day because ``'T'`` (0x54) > ``' '`` (0x20) at
    char 10 — the whole-day off-by-one. A like-for-like isoformat bound (the
    ``sql_utc_cutoff`` form) excludes it correctly.
    """
    conn = _table()
    early = "2026-06-10T09:00:00.000000+00:00"  # before 14:30 cutoff -> OUT
    late = "2026-06-10T20:00:00.000000+00:00"  # after 14:30 cutoff  -> IN
    conn.executemany("INSERT INTO t VALUES (?)", [(early,), (late,)])

    space_bound = "2026-06-10 14:30:00"  # OLD datetime('now', ...) rendering
    iso_bound = "2026-06-10T14:30:00+00:00"  # FIXED sql_utc_cutoff rendering

    # OLD form: the pre-cutoff 09:00 row is wrongly kept (day-granularity bug).
    assert _count_ge(conn, space_bound) == 2
    # FIXED form: only the genuinely in-window 20:00 row survives.
    assert _count_ge(conn, iso_bound) == 1
    # Direction check: the late row is IN under both; the early row flips.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM t WHERE opened_at = ? AND opened_at >= ?",
            (early, iso_bound),
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM t WHERE opened_at = ? AND opened_at >= ?",
            (late, iso_bound),
        ).fetchone()[0]
        == 1
    )


def test_window_predicate_helper_is_second_granular():
    """The shipped ``sql_utc_cutoff`` bound admits/excludes at SECOND
    granularity — the precision the old 'T'-vs-space predicate lost."""
    conn = _table()
    cutoff = sql_utc_cutoff(days=7)
    cutoff_dt = datetime.fromisoformat(cutoff)
    just_out = (cutoff_dt - timedelta(seconds=1)).isoformat()  # OUT
    at_bound = cutoff_dt.isoformat()  # == bound -> IN (>=)
    just_in = (cutoff_dt + timedelta(seconds=1)).isoformat()  # IN
    conn.executemany("INSERT INTO t VALUES (?)", [(just_out,), (at_bound,), (just_in,)])
    assert _count_ge(conn, cutoff) == 2  # at_bound + just_in, not just_out


# ---------------------------------------------------------------------------
# Predicate 2 — calendar-day (today's UTC midnight)
# ---------------------------------------------------------------------------


def test_start_of_day_predicate_midnight_boundary_both_directions():
    """A row at exactly today's 00:00:00 UTC counts as "today"; a row one
    second into yesterday does not. Fixed and old-space-form bounds agree here
    (midnight bounds are outcome-preserving — the 'T'>' ' bias coincides with
    correctness at 00:00:00), so this fix is structural, not behavioural."""
    conn = _table()
    cutoff = sql_utc_cutoff(start_of_day=True)
    midnight_dt = datetime.fromisoformat(cutoff)
    today_midnight = midnight_dt.isoformat()  # 00:00:00 today   -> IN
    today_noon = (midnight_dt + timedelta(hours=12)).isoformat()  # today -> IN
    yesterday_late = (midnight_dt - timedelta(seconds=1)).isoformat()  # -> OUT
    conn.executemany(
        "INSERT INTO t VALUES (?)",
        [(today_midnight,), (today_noon,), (yesterday_late,)],
    )

    # Fixed isoformat bound: both of today's rows, not yesterday's.
    assert _count_ge(conn, cutoff) == 2

    # Direction check.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM t WHERE opened_at = ? AND opened_at >= ?",
            (today_midnight, cutoff),
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM t WHERE opened_at = ? AND opened_at >= ?",
            (yesterday_late, cutoff),
        ).fetchone()[0]
        == 0
    )

    # Outcome-preservation: the old space-form midnight bound gives the same
    # count (documented: midnight is the one non-buggy case of the artifact).
    assert midnight_dt.time() == time(0, 0, 0, 0)
    space_midnight = midnight_dt.strftime("%Y-%m-%d %H:%M:%S")
    assert _count_ge(conn, space_midnight) == 2
