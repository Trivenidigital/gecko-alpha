#!/usr/bin/env python3
"""Check upstream TG/X rows that have not reached source_calls yet.

Also (optional) checks writer-side cron-tick liveness via a heartbeat
file touched by `source_calls_live_writer.py` on each successful run.
The writer-side branch detects writer-cron outages independently of
upstream traffic — needed because the ledger-lag branch's `MAX(upstream)
- MAX(source_calls)` comparison stays small when BOTH sides are quiet,
masking writer-cron failure (CLAUDE.md §12a Class-1 silent-failure).

Status enum on the writer-side branch:
  writer_stale             — heartbeat exists, mtime older than threshold
  writer_heartbeat_missing — heartbeat absent, ledger has rows (writer ran before, now broken)
  writer_heartbeat_pending — heartbeat absent, ledger empty (first-run; alert-suppressed)
  writer_never_fired       — pending persisted >24h (escalates to alert)

Exit codes:
  0 — ok (no lag, writer healthy or pending)
  2 — ledger lag found
  3 — db missing
  4 — schema missing
  5 — writer-side stale/missing/never-fired
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PENDING_ESCALATION_HOURS = 6


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


def _ledger_has_rows(conn: sqlite3.Connection) -> bool:
    """Defensive count — returns False if table doesn't exist yet (fresh DB)."""
    try:
        row = conn.execute("SELECT 1 FROM source_calls LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def _check_writer_heartbeat(
    heartbeat_path: Path | None,
    threshold_minutes: int,
    ledger_has_rows: bool,
    now: datetime,
) -> tuple[str, dict] | None:
    """Return (status, detail) tuple if a writer-side issue is detected.

    Returns None when:
      - heartbeat_path is None (branch disabled)
      - heartbeat file exists and mtime is within threshold (writer healthy)

    Pending-since state file:
      Stored as `<heartbeat_path>.pending-since` (sibling file). Created on
      first observation of `writer_heartbeat_pending`. mtime acts as the
      first-observation timestamp. Removed when writer becomes healthy
      (caller's responsibility — see main()).
    """
    if heartbeat_path is None:
        return None

    if heartbeat_path.exists():
        mtime = datetime.fromtimestamp(heartbeat_path.stat().st_mtime, tz=timezone.utc)
        age_minutes = (now - mtime).total_seconds() / 60.0
        if age_minutes > threshold_minutes:
            return (
                "writer_stale",
                {
                    "path": str(heartbeat_path),
                    "age_minutes": round(age_minutes, 1),
                    "threshold_minutes": threshold_minutes,
                    "last_writer_success_at": mtime.isoformat(),
                },
            )
        return None  # healthy

    # heartbeat file absent
    if ledger_has_rows:
        return (
            "writer_heartbeat_missing",
            {
                "path": str(heartbeat_path),
                "ledger_has_rows": True,
                "explanation": "writer ran before (ledger has rows) but heartbeat file is gone",
            },
        )

    # pending — empty ledger, no heartbeat. Check escalation.
    pending_since = heartbeat_path.with_name(heartbeat_path.name + ".pending-since")
    try:
        pending_since.parent.mkdir(parents=True, exist_ok=True)
        if not pending_since.exists():
            pending_since.touch()
    except OSError:
        pass  # observability best-effort; do not crash

    if pending_since.exists():
        first_seen = datetime.fromtimestamp(pending_since.stat().st_mtime, tz=timezone.utc)
        age_hours = (now - first_seen).total_seconds() / 3600.0
        if age_hours > PENDING_ESCALATION_HOURS:
            return (
                "writer_never_fired",
                {
                    "path": str(heartbeat_path),
                    "pending_since": first_seen.isoformat(),
                    "age_hours": round(age_hours, 1),
                    "escalation_hours": PENDING_ESCALATION_HOURS,
                },
            )

    return (
        "writer_heartbeat_pending",
        {
            "path": str(heartbeat_path),
            "ledger_has_rows": False,
            "alert_suppressed": True,
        },
    )


def _clear_pending_since(heartbeat_path: Path | None) -> None:
    """Remove the pending-since marker when writer recovers to healthy."""
    if heartbeat_path is None:
        return
    pending_since = heartbeat_path.with_name(heartbeat_path.name + ".pending-since")
    try:
        if pending_since.exists():
            pending_since.unlink()
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="scout.db")
    parser.add_argument("--threshold-minutes", type=int, default=30)
    parser.add_argument(
        "--writer-heartbeat-file",
        default=None,
        help="Optional. Path touched by source_calls_live_writer.py on success. "
        "When set, enables the writer-cron-tick liveness branch.",
    )
    parser.add_argument(
        "--writer-threshold-minutes",
        type=int,
        default=20,
        help="Alert if writer heartbeat older than this. Default 4x writer cadence (5min).",
    )
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=args.threshold_minutes)
    db_path = Path(args.db).expanduser()
    heartbeat_path = (
        Path(args.writer_heartbeat_file).expanduser()
        if args.writer_heartbeat_file
        else None
    )

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
        # Writer-side check runs FIRST so the more-actionable diagnosis wins
        # when both writer-staleness and ledger-lag would otherwise alert.
        ledger_rows = _ledger_has_rows(conn)
        writer_finding = _check_writer_heartbeat(
            heartbeat_path,
            args.writer_threshold_minutes,
            ledger_has_rows=ledger_rows,
            now=now,
        )
        if writer_finding is not None:
            status, detail = writer_finding
            # Clear pending-since marker when we're in any non-pending state:
            # the pending tracking is only meaningful while we're actively
            # observing a first-run-empty-ledger condition (Reviewer-B I1).
            if status != "writer_heartbeat_pending" and status != "writer_never_fired":
                _clear_pending_since(heartbeat_path)
            payload = {
                "ok": status == "writer_heartbeat_pending",
                "status": status,
                "detail": detail,
                "checked_at": now.isoformat(),
            }
            print(json.dumps(payload, sort_keys=True))
            return 0 if status == "writer_heartbeat_pending" else 5

        # Writer healthy (or branch disabled). If writer-branch active and healthy,
        # clear any stale pending-since marker.
        if heartbeat_path is not None and heartbeat_path.exists():
            _clear_pending_since(heartbeat_path)

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
