#!/usr/bin/env python3
"""Freshness SLO check for trade_decision_events.

The table is only expected to advance when recent top-gainer tracker rows exist.
If gainers_snapshots has fresh rows but no fresh gainers_early decision events,
the dispatcher instrumentation is likely disconnected.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _iso_cutoff(minutes: float) -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .replace(tzinfo=None)
        .isoformat(sep=" ")
    )


def check(db_path: Path, lookback_minutes: float) -> tuple[int, dict]:
    if not db_path.exists():
        return 4, {"ok": False, "status": "db_missing", "db": str(db_path)}

    cutoff = _iso_cutoff(lookback_minutes)
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        tracker_count = conn.execute(
            """SELECT COUNT(*) AS n
               FROM gainers_snapshots
               WHERE datetime(snapshot_at) >= datetime(?)""",
            (cutoff,),
        ).fetchone()["n"]
        decision_count = conn.execute(
            """SELECT COUNT(*) AS n
               FROM trade_decision_events
               WHERE signal_type = 'gainers_early'
                 AND datetime(created_at) >= datetime(?)""",
            (cutoff,),
        ).fetchone()["n"]
    except sqlite3.Error as exc:
        return 3, {"ok": False, "status": "sqlite_error", "error": str(exc)}
    finally:
        try:
            conn.close()
        except Exception:
            pass

    body = {
        "ok": True,
        "status": "ok",
        "lookback_minutes": lookback_minutes,
        "recent_gainers_snapshots": tracker_count,
        "recent_gainers_early_decisions": decision_count,
    }
    if tracker_count == 0:
        body["status"] = "idle_no_recent_tracker_rows"
        return 0, body
    if decision_count == 0:
        body["ok"] = False
        body["status"] = "missing_recent_decisions"
        return 2, body
    return 0, body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="scout.db", type=Path)
    parser.add_argument("--lookback-minutes", default=15.0, type=float)
    args = parser.parse_args(argv)

    code, body = check(args.db, args.lookback_minutes)
    print(json.dumps(body, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
