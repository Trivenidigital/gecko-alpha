#!/usr/bin/env python3
"""Read-only replay of the `refresh_all` enumeration repair.

WHY THIS EXISTS. `fix/refresh-enumeration-closed-window` widens the set of
combos `refresh_all` selects. Widening a selection is exactly the kind of change
whose blast radius cannot be read off the diff: the new arms admit rows that
have not been recomputed in months, and recomputing them can move `suppressed`.
This script answers, against the real production database and BEFORE the merge,
the only question that matters: *which rows change, and does any of them cross a
suppression boundary?*

WHAT IT REPORTS, per key in the proposed enumeration:
  - both rows as they stand today (trades / wins / losses / WR / suppressed /
    last_refreshed), for the 7d window and the 30d window
  - the canonical 7d + 30d economics recomputed at `--at`
  - whether EITHER persisted row would change, decided per window
  - which combos NEWLY enter membership relative to the deployed enumeration
  - predicted D4/D5 suppression transitions
  - the full GLOBAL terminal-incomplete candidate set

PER-WINDOW COMPARISON. `_refresh_combo_locked` writes TWO rows per combo, one
per window, from two independent aggregates. A comparison that fetches only the
`window = '30d'` row therefore attests to half the write: a stale persisted 7d
row sitting behind an exact 30d row reported "no change". Each window is now
compared against its own canonical recomputation and reported separately as
`7d_match` / `30d_match` and `would_change_7d` / `would_change_30d`, with
`would_change_any` as the aggregate. A window with NO persisted row is a change
for that window (`row_would_be_created`), never a silent skip — the treatment
the 30d window already gave a missing row.

  The legacy field names are kept so reports either side of this change stay
  diffable, and both now carry ANY-window semantics: `would_change` equals
  `would_change_any`, and `rows_that_would_change` counts combos where either
  window moves (`rows_that_would_change_7d` / `_30d` break that count down).
  `change_reason` reports the 30d reason when the 30d window changes — it is the
  window that drives suppression — and otherwise the 7d reason; the unambiguous
  per-window fields are `change_reason_7d` and `change_reason_30d`.

  Enumeration itself stays keyed on `window = '30d'`, mirroring `refresh_all`.
  A combo carrying a 7d row but no 30d row is outside both the production
  enumeration and this replay of it.

THRESHOLD PROVENANCE. `_classify_retest` and `_retest_accounting` are IMPORTED
from `scout.trading.combo_refresh` and `VALID_TERMINAL_OUTCOME_SQL` from
`scout.trading.paper`; every threshold comes from `Settings`. Nothing is
re-typed here. The one thing that is necessarily MIRRORED rather than called is
the final transition branch, which lives inline inside `_refresh_combo_locked`
and cannot be executed without performing the write it is part of. That mirror
is marked `MIRRORED` in `_predict_transition` and its report field is named
`predicted_transition_MIRRORED_LOGIC` so no reader mistakes it for a call into
the real decision path.

SAFETY POSTURE.
  - Opens SQLite `mode=ro`. Writes are refused by SQLite, not merely avoided.
  - NEVER constructs `Database` and NEVER calls `.initialize()`. A read-only
    observer that enters the migration machinery is the failure class removed in
    PR #520 (~40 migrations per tick against a live 6.8 GB database).
  - Emits no alerts and installs nothing.

  One honest caveat, pinned by a test. The database runs `journal_mode=wal`, and
  opening a WAL database read-only makes SQLite materialize `-wal`/`-shm`
  sidecars — its own read-side index machinery. That happens for ANY reader,
  including a bare stdlib connection executing nothing, and it is not a write to
  the data: the database file itself is byte-identical across a run.
  `immutable=1` would suppress the sidecars and is deliberately NOT used, since
  it asserts the file cannot change while open — false for a live database, and
  a licence for SQLite to serve stale or torn reads.

Output is JSON on stdout: sorted keys, sorted lists, deterministic across runs
for a fixed `--db` and `--at`.

Exit codes:
  0 — replay completed
  1 — DB missing, unreadable, bad `--at`, or config unresolvable
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite

from scout.config import Settings
from scout.trading.combo_refresh import _classify_retest, _retest_accounting
from scout.trading.paper import VALID_TERMINAL_OUTCOME_SQL


class _RoDb:
    """Minimal read-only stand-in exposing the single attribute the reused
    accounting helper touches.

    `_retest_accounting` needs `db._conn.execute`, nothing else. Passing this
    instead of a real `Database` is what lets the replay reuse the production
    accounting code without importing the migration machinery.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn


def _iso(at: datetime, *, days: float) -> str:
    """Cutoff `days` before `at`, in the stored `.isoformat()` format.

    Mirrors `scout.timeutil.sql_utc_cutoff`, which is anchored to `now()` and so
    cannot express the `--at` replay timestamp.
    """
    return (at - timedelta(days=days)).isoformat()


async def _scalar_set(conn: aiosqlite.Connection, sql: str, params: tuple) -> set[str]:
    cur = await conn.execute(sql, params)
    return {r[0] for r in await cur.fetchall() if r[0]}


async def _proposed_membership(
    conn: aiosqlite.Connection, at: datetime, window_days: int
) -> set[str]:
    """The three-arm enumeration this PR introduces."""
    cutoff = _iso(at, days=window_days)
    opens = await _scalar_set(
        conn,
        "SELECT DISTINCT signal_combo FROM paper_trades "
        "WHERE signal_combo IS NOT NULL AND opened_at >= ?",
        (cutoff,),
    )
    closes = await _scalar_set(
        conn,
        "SELECT DISTINCT signal_combo FROM paper_trades "
        "WHERE signal_combo IS NOT NULL "
        f"  AND {VALID_TERMINAL_OUTCOME_SQL} "
        "  AND closed_at >= ?",
        (cutoff,),
    )
    existing = await _scalar_set(
        conn,
        "SELECT combo_key FROM combo_performance WHERE window = '30d'",
        (),
    )
    return opens | closes | existing


async def _deployed_membership(
    conn: aiosqlite.Connection, at: datetime, window_days: int
) -> set[str]:
    """The enumeration currently running in production (opens ∪ suppressed)."""
    cutoff = _iso(at, days=window_days)
    opens = await _scalar_set(
        conn,
        "SELECT DISTINCT signal_combo FROM paper_trades "
        "WHERE signal_combo IS NOT NULL AND opened_at >= ?",
        (cutoff,),
    )
    suppressed = await _scalar_set(
        conn,
        "SELECT combo_key FROM combo_performance "
        "WHERE window = '30d' AND suppressed = 1",
        (),
    )
    return opens | suppressed


async def _economics(
    conn: aiosqlite.Connection, combo: str, at: datetime, days: int
) -> dict:
    """Canonical economics for `combo` over the `days`-day window ending at `at`.

    Same aggregate and same validity predicate as `_refresh_combo_locked`.
    """
    cur = await conn.execute(
        f"""SELECT
              COUNT(*) AS trades,
              SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
              SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) AS losses,
              COALESCE(SUM(pnl_usd), 0) AS total_pnl_usd,
              COALESCE(AVG(pnl_pct), 0) AS avg_pnl_pct
            FROM paper_trades
            WHERE signal_combo = ?
              AND {VALID_TERMINAL_OUTCOME_SQL}
              AND closed_at >= ?""",
        (combo, _iso(at, days=days)),
    )
    row = await cur.fetchone()
    trades = (row[0] if row else 0) or 0
    wins = (row[1] if row else 0) or 0
    losses = (row[2] if row else 0) or 0
    return {
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "total_pnl_usd": round(float((row[3] if row else 0) or 0), 6),
        "avg_pnl_pct": round(float((row[4] if row else 0) or 0), 6),
        "win_rate_pct": round((100.0 * wins / trades) if trades else 0.0, 6),
    }


async def _existing_row(
    conn: aiosqlite.Connection, combo: str, window: str
) -> dict | None:
    """The persisted row for `combo` in `window`, or None if there is none.

    Same shape for both windows so the comparison has one code path. The
    suppression and parole fields are meaningful only on the 30d row —
    `_refresh_combo_locked` pins the 7d row's `suppressed` to 0 and never writes
    its parole columns.
    """
    cur = await conn.execute(
        "SELECT trades, wins, losses, win_rate_pct, suppressed, suppressed_at, "
        "       parole_at, parole_trades_remaining, last_refreshed "
        "FROM combo_performance WHERE combo_key = ? AND window = ?",
        (combo, window),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return {
        "trades": row[0],
        "wins": row[1],
        "losses": row[2],
        "win_rate_pct": round(float(row[3] or 0), 6),
        "suppressed": int(row[4] or 0),
        "suppressed_at": row[5],
        "parole_at": row[6],
        "parole_trades_remaining": row[7],
        "last_refreshed": row[8],
    }


def _matches(existing: dict | None, recomputed: dict) -> bool:
    """Does the persisted row already carry the canonical economics?

    A missing row is NOT a match — the refresh would create it, which is a
    change for that window.
    """
    if existing is None:
        return False
    return (
        existing["trades"] == recomputed["trades"]
        and existing["wins"] == recomputed["wins"]
        and existing["losses"] == recomputed["losses"]
        and abs(existing["win_rate_pct"] - recomputed["win_rate_pct"]) < 1e-6
    )


def _change_reason(existing: dict | None, matched: bool) -> str:
    if existing is None:
        return "row_would_be_created"
    return "no_change" if matched else "economics_differ"


def _predict_transition(
    existing: dict | None, w30: dict, retest_state, settings
) -> str:
    """MIRRORED from the transition branch of `_refresh_combo_locked`.

    That branch is inline inside the function that performs the write, so it
    cannot be executed read-only. Thresholds are read from `Settings` and the
    retest classification is the imported `_classify_retest` — only the branch
    STRUCTURE is reproduced here. Kept in the same order as the original so a
    reviewer can diff them side by side.
    """
    min_trades = settings.FEEDBACK_SUPPRESSION_MIN_TRADES
    wr_thresh = settings.FEEDBACK_SUPPRESSION_WR_THRESHOLD_PCT
    crosses = w30["trades"] >= min_trades and w30["win_rate_pct"] < wr_thresh

    if existing is None or not existing["suppressed"]:
        return "new_suppression" if crosses else "none"
    if retest_state == "complete":
        if w30["win_rate_pct"] >= wr_thresh:
            return "clear_suppression"
        return "resuppress_fresh_parole"
    return f"preserve:{retest_state}"


async def _stuck_retest_candidates(
    conn: aiosqlite.Connection, settings, at: datetime
) -> dict[str, list[dict]]:
    """The GLOBAL stuck-retest sets, via the exact predicate in
    `_process_retest_terminal_incomplete`, split by class.

    Global, not scoped to the refreshed combos — the 2026-08-13 review under-
    counted the expected pages precisely because it reasoned from the refreshed
    list instead of this predicate.

    TWO classes since F2, and the split is the point: `terminal_incomplete` is
    a cohort that produced too little usable evidence, `parole_stalled` is a
    cohort that never existed. Reporting them in one bucket would reproduce, in
    the diagnostic, the exact conflation the pager was fixed for — the harness
    has to mirror production or it is not a replay.
    """
    target = settings.FEEDBACK_PAROLE_RETEST_TRADES
    grace_cutoff = (at - timedelta(days=1)).isoformat()
    cur = await conn.execute(
        "SELECT combo_key, suppressed_at, parole_at, parole_trades_remaining "
        "FROM combo_performance "
        "WHERE window = '30d' AND suppressed = 1 "
        "  AND retest_incomplete_alerted_at IS NULL "
        "  AND parole_at IS NOT NULL "
        "  AND (COALESCE(parole_trades_remaining, 1) <= 0 OR parole_at <= ?)",
        (grace_cutoff,),
    )
    rows = await cur.fetchall()
    db = _RoDb(conn)
    buckets: dict[str, list[dict]] = {"terminal_incomplete": [], "parole_stalled": []}
    for combo_key, _suppressed_at, parole_at, remaining in rows:
        acct = await _retest_accounting(
            db, combo_key, parole_at, remaining, target, now=at
        )
        state = _classify_retest(acct, remaining, target)
        if state not in buckets:
            continue
        buckets[state].append(
            {
                "combo_key": combo_key,
                "valid_closed": acct["valid_closed"],
                "invalid_closed": acct["invalid_closed"],
                "open_now": acct["open_now"],
                "cohort_total": acct["cohort_total"],
                "slots_spent": acct["spent"],
                "required": target,
            }
        )
    return {k: sorted(v, key=lambda d: d["combo_key"]) for k, v in buckets.items()}


async def replay(db_path: Path, at: datetime, settings) -> dict:
    """Run the whole replay. Opens the DB read-only and never writes."""
    uri = f"file:{db_path.as_posix()}?mode=ro"
    async with aiosqlite.connect(uri, uri=True) as conn:
        window_days = settings.FEEDBACK_REFRESH_WINDOW_DAYS
        proposed = await _proposed_membership(conn, at, window_days)
        deployed = await _deployed_membership(conn, at, window_days)
        target = settings.FEEDBACK_PAROLE_RETEST_TRADES
        ro = _RoDb(conn)

        combos: list[dict] = []
        changed = 0
        changed_7d = 0
        changed_30d = 0
        for combo in sorted(proposed):
            existing_7d = await _existing_row(conn, combo, "7d")
            existing_30d = await _existing_row(conn, combo, "30d")
            w7 = await _economics(conn, combo, at, 7)
            w30 = await _economics(conn, combo, at, 30)

            retest_state = None
            if existing_30d is not None and existing_30d["suppressed"]:
                acct = await _retest_accounting(
                    ro,
                    combo,
                    existing_30d["parole_at"],
                    existing_30d["parole_trades_remaining"],
                    target,
                    now=at,
                )
                retest_state = _classify_retest(
                    acct, existing_30d["parole_trades_remaining"], target
                )

            # Each window against its OWN canonical recomputation. The refresh
            # writes both rows; comparing only one attests to half the write.
            match_7d = _matches(existing_7d, w7)
            match_30d = _matches(existing_30d, w30)
            would_change_7d = not match_7d
            would_change_30d = not match_30d
            would_change_any = would_change_7d or would_change_30d

            reason_7d = _change_reason(existing_7d, match_7d)
            reason_30d = _change_reason(existing_30d, match_30d)
            if would_change_30d:
                change_reason = reason_30d
            elif would_change_7d:
                change_reason = reason_7d
            else:
                change_reason = "no_change"

            if would_change_any:
                changed += 1
            if would_change_7d:
                changed_7d += 1
            if would_change_30d:
                changed_30d += 1

            combos.append(
                {
                    "combo_key": combo,
                    "in_deployed_enumeration": combo in deployed,
                    "newly_enumerated": combo not in deployed,
                    "existing_row_7d": existing_7d,
                    "existing_row_30d": existing_30d,
                    "recomputed_7d": w7,
                    "recomputed_30d": w30,
                    "7d_match": match_7d,
                    "30d_match": match_30d,
                    "would_change_7d": would_change_7d,
                    "would_change_30d": would_change_30d,
                    "would_change_any": would_change_any,
                    # Legacy field, now ANY-window (see module docstring).
                    "would_change": would_change_any,
                    "change_reason_7d": reason_7d,
                    "change_reason_30d": reason_30d,
                    "change_reason": change_reason,
                    "retest_state": retest_state,
                    "predicted_transition_MIRRORED_LOGIC": _predict_transition(
                        existing_30d, w30, retest_state, settings
                    ),
                }
            )

        transitions = sorted(
            c["combo_key"]
            for c in combos
            if c["predicted_transition_MIRRORED_LOGIC"]
            in ("new_suppression", "clear_suppression", "resuppress_fresh_parole")
        )
        stuck = await _stuck_retest_candidates(conn, settings, at)

    return {
        "at": at.isoformat(),
        "db": str(db_path),
        "read_only": True,
        "window_days": window_days,
        "thresholds": {
            "FEEDBACK_SUPPRESSION_MIN_TRADES": settings.FEEDBACK_SUPPRESSION_MIN_TRADES,
            "FEEDBACK_SUPPRESSION_WR_THRESHOLD_PCT": (
                settings.FEEDBACK_SUPPRESSION_WR_THRESHOLD_PCT
            ),
            "FEEDBACK_PAROLE_DAYS": settings.FEEDBACK_PAROLE_DAYS,
            "FEEDBACK_PAROLE_RETEST_TRADES": settings.FEEDBACK_PAROLE_RETEST_TRADES,
            "FEEDBACK_REFRESH_WINDOW_DAYS": settings.FEEDBACK_REFRESH_WINDOW_DAYS,
        },
        "membership": {
            "proposed_count": len(proposed),
            "deployed_count": len(deployed),
            "newly_enumerated": sorted(proposed - deployed),
            "dropped_vs_deployed": sorted(deployed - proposed),
        },
        # ANY-window count, with the per-window breakdown beside it.
        "rows_that_would_change": changed,
        "rows_that_would_change_7d": changed_7d,
        "rows_that_would_change_30d": changed_30d,
        "predicted_suppression_transitions": transitions,
        "terminal_incomplete_candidates_GLOBAL": stuck["terminal_incomplete"],
        "parole_stalled_candidates_GLOBAL": stuck["parole_stalled"],
        "combos": combos,
    }


def _parse_at(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only replay of the refresh_all enumeration repair."
    )
    parser.add_argument("--db", default="scout.db")
    parser.add_argument(
        "--at",
        default=None,
        help="ISO-8601 replay timestamp (default: now, UTC).",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(json.dumps({"ok": False, "error": f"db not found: {db_path}"}))
        return 1
    try:
        at = _parse_at(args.at)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": f"bad --at: {exc}"[:300]}))
        return 1
    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"config: {exc}"[:300]}))
        return 1

    try:
        report = asyncio.run(replay(db_path, at, settings))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"replay: {exc}"[:300]}))
        return 1

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
