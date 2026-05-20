#!/usr/bin/env python3
"""Check upstream TG/X rows that have not reached source_calls yet."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _count_unledgered(conn: sqlite3.Connection, rows, cutoff: datetime) -> int:
    total = 0
    for source_type, event_id, observed_at in rows:
        observed = _parse_utc(observed_at)
        if observed is None or observed > cutoff:
            continue
        exists = conn.execute(
            "SELECT 1 FROM source_calls WHERE source_type=? AND source_event_id=?",
            (source_type, event_id),
        ).fetchone()
        if exists is None:
            total += 1
    return total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="scout.db")
    parser.add_argument("--threshold-minutes", type=int, default=30)
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=args.threshold_minutes)
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "db_not_found",
                    "db": str(db_path),
                    "threshold_minutes": args.threshold_minutes,
                    "unledgered_tg": None,
                    "unledgered_x": None,
                    "checked_at": now.isoformat(),
                },
                sort_keys=True,
            )
        )
        return 3

    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        try:
            tg_rows = conn.execute(
                "SELECT 'tg', CAST(id AS TEXT), created_at FROM tg_social_signals"
            ).fetchall()
            x_rows = conn.execute(
                "SELECT 'x', event_id, received_at FROM narrative_alerts_inbound"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "schema_missing",
                        "detail": str(exc)[:120],
                        "db": str(db_path),
                        "threshold_minutes": args.threshold_minutes,
                        "unledgered_tg": None,
                        "unledgered_x": None,
                        "checked_at": now.isoformat(),
                    },
                    sort_keys=True,
                )
            )
            return 4
        unledgered_tg = _count_unledgered(conn, tg_rows, cutoff)
        unledgered_x = _count_unledgered(conn, x_rows, cutoff)
    finally:
        conn.close()

    result = {
        "ok": unledgered_tg == 0 and unledgered_x == 0,
        "threshold_minutes": args.threshold_minutes,
        "unledgered_tg": unledgered_tg,
        "unledgered_x": unledgered_x,
        "checked_at": now.isoformat(),
    }
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
