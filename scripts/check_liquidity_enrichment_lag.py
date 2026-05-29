#!/usr/bin/env python3
"""Check liquidity-enrichment writer health + row-rate SLO.

Two independent branches (mirrors ``check_source_calls_lag.py``):

1. **Writer-side liveness** — heartbeat file touched by the cron writer
   on each successful tick. Distinguishes:
     - ``writer_stale``             heartbeat exists, mtime > threshold
     - ``writer_heartbeat_missing`` heartbeat absent, candidates have
                                    enrichment rows (writer ran before)
     - ``writer_heartbeat_pending`` heartbeat absent, no enrichment rows
                                    yet (first-run; alert-suppressed)
     - ``writer_never_fired``       pending persisted > escalation window

2. **Row-rate SLO** — % of recent ``candidates`` rows whose
   ``liquidity_enriched_at`` is within the staleness threshold. Default
   SLO: 80% of rows opened in the last 7 days were enriched within the
   last 30 minutes. Used to detect "writer running but failing to update
   the right rows" (CLAUDE.md §12a Class-1 silent-failure).

Killswitch awareness (operator guardrail #5 + design failure-mode
table): when ``--killswitch-disabled`` is passed, the watchdog
suppresses BOTH staleness branches and exits 0 with status
``killswitch_disabled``. The cron writer is intentionally off; pager
fatigue is prevented. Operator MUST manually verify the cron is off;
the watchdog will not signal it.

Exit codes:
  0 — ok (writer healthy, row SLO met, OR killswitch disabled,
      OR first-run pending)
  2 — row-rate SLO breached
  3 — db missing
  4 — schema missing (Phase 1a-i migration not applied)
  5 — writer-side stale / heartbeat missing / never fired
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PENDING_ESCALATION_HOURS = 6
ENRICHMENT_MIGRATION_NAME = "bl_new_liquidity_enrichment_v1"


def _parse_utc(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string into a UTC datetime. ``None``-safe."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _schema_present(conn: sqlite3.Connection) -> bool:
    """Return True iff Phase 1a-i enrichment columns exist on ``candidates``."""
    try:
        rows = conn.execute("PRAGMA table_info(candidates)").fetchall()
    except sqlite3.OperationalError:
        return False
    cols = {row[1] for row in rows}
    required = {
        "liquidity_usd_enriched",
        "liquidity_enriched_source",
        "liquidity_enriched_at",
        "liquidity_enriched_confidence",
    }
    return required.issubset(cols)


def _enrichment_table_has_rows(conn: sqlite3.Connection) -> bool:
    """Return True iff at least one candidates row carries an enrichment
    timestamp. Defensive against missing schema."""
    try:
        row = conn.execute(
            "SELECT 1 FROM candidates "
            "WHERE liquidity_enriched_at IS NOT NULL LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def _check_writer_heartbeat(
    heartbeat_path: Path | None,
    threshold_minutes: int,
    has_enrichment_rows: bool,
    now: datetime,
) -> tuple[str, dict] | None:
    """Return (status, detail) if a writer-side issue is detected.

    Returns ``None`` when:
      - heartbeat_path is None (branch disabled by caller)
      - heartbeat file exists and mtime within threshold (writer healthy)

    Pending-since state file (sibling of heartbeat path with
    ``.pending-since`` suffix) acts as the first-observation timestamp
    for the writer_never_fired escalation. Created on first observation
    of ``writer_heartbeat_pending``; cleared on healthy recovery.
    """
    if heartbeat_path is None:
        return None

    if heartbeat_path.exists():
        mtime = datetime.fromtimestamp(
            heartbeat_path.stat().st_mtime, tz=timezone.utc
        )
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
    if has_enrichment_rows:
        return (
            "writer_heartbeat_missing",
            {
                "path": str(heartbeat_path),
                "has_enrichment_rows": True,
                "explanation": (
                    "writer ran before (enrichment rows exist) but "
                    "heartbeat file is gone"
                ),
            },
        )

    # pending — empty enrichment, no heartbeat. Check escalation window.
    pending_since = heartbeat_path.with_name(
        heartbeat_path.name + ".pending-since"
    )
    try:
        pending_since.parent.mkdir(parents=True, exist_ok=True)
        if not pending_since.exists():
            pending_since.touch()
    except OSError:
        # observability best-effort; do not crash the watchdog
        pass

    if pending_since.exists():
        first_seen = datetime.fromtimestamp(
            pending_since.stat().st_mtime, tz=timezone.utc
        )
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
            "has_enrichment_rows": False,
            "alert_suppressed": True,
        },
    )


def _clear_pending_since(heartbeat_path: Path | None) -> None:
    """Remove the pending-since marker on writer recovery."""
    if heartbeat_path is None:
        return
    pending_since = heartbeat_path.with_name(
        heartbeat_path.name + ".pending-since"
    )
    try:
        if pending_since.exists():
            pending_since.unlink()
    except OSError:
        pass


def _row_coverage(
    conn: sqlite3.Connection,
    *,
    recent_window_hours: int,
    staleness_threshold_minutes: int,
    now: datetime,
) -> dict:
    """Compute % of recent ``candidates`` rows enriched within threshold.

    Denominator: candidates first_seen_at within recent_window_hours.
    Numerator: same set WHERE liquidity_enriched_at within
    staleness_threshold_minutes of now.

    Returns dict with denominator, numerator, ratio, threshold, plus
    ``ok`` boolean. Returns ``denominator=0`` when no recent rows exist;
    caller should treat that as "no signal" not "SLO breach".
    """
    recent_cutoff = (
        now - timedelta(hours=recent_window_hours)
    ).isoformat()
    fresh_cutoff = (
        now - timedelta(minutes=staleness_threshold_minutes)
    ).isoformat()
    denom_row = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE first_seen_at >= ?",
        (recent_cutoff,),
    ).fetchone()
    denominator = int(denom_row[0]) if denom_row else 0
    if denominator == 0:
        return {
            "denominator": 0,
            "numerator": 0,
            "ratio": None,
            "recent_window_hours": recent_window_hours,
            "staleness_threshold_minutes": staleness_threshold_minutes,
        }
    numer_row = conn.execute(
        "SELECT COUNT(*) FROM candidates "
        "WHERE first_seen_at >= ? "
        "AND liquidity_enriched_at IS NOT NULL "
        "AND liquidity_enriched_at >= ?",
        (recent_cutoff, fresh_cutoff),
    ).fetchone()
    numerator = int(numer_row[0]) if numer_row else 0
    ratio = numerator / denominator
    return {
        "denominator": denominator,
        "numerator": numerator,
        "ratio": round(ratio, 4),
        "recent_window_hours": recent_window_hours,
        "staleness_threshold_minutes": staleness_threshold_minutes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Watchdog for liquidity-enrichment writer (Phase 1a-i)."
    )
    parser.add_argument("--db", default="scout.db")
    parser.add_argument(
        "--writer-heartbeat-file",
        default=None,
        help=(
            "Path touched by the liquidity-enrichment cron on success. "
            "When set, enables the writer-cron-tick liveness branch."
        ),
    )
    parser.add_argument(
        "--writer-threshold-minutes",
        type=int,
        default=20,
        help="Alert if writer heartbeat older than this. Default 4x writer "
        "cadence (cron at 5min would alert at 20min).",
    )
    parser.add_argument(
        "--row-coverage-threshold",
        type=float,
        default=0.80,
        help="Row-rate SLO floor. Default 0.80 = 80%% of recent rows must "
        "be enriched within staleness threshold.",
    )
    parser.add_argument(
        "--recent-window-hours",
        type=int,
        default=168,
        help="Window over which row-coverage is measured. Default 168h (7d).",
    )
    parser.add_argument(
        "--staleness-threshold-minutes",
        type=int,
        default=30,
        help="A row counts as 'fresh' if liquidity_enriched_at is within "
        "this many minutes of now. Default 30m.",
    )
    parser.add_argument(
        "--killswitch-disabled",
        action="store_true",
        help="Pass when LIQUIDITY_ENRICHMENT_ENABLED=False in operator's "
        ".env. Suppresses all staleness alerts; reports status only.",
    )
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
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
                    "checked_at": now.isoformat(),
                },
                sort_keys=True,
            )
        )
        return 3

    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        # Killswitch branch — short-circuits all staleness checks.
        # Per design: cron is intentionally off; no alert needed.
        if args.killswitch_disabled:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "status": "killswitch_disabled",
                        "detail": {
                            "explanation": (
                                "LIQUIDITY_ENRICHMENT_ENABLED=False per "
                                "operator. Writer intentionally off; "
                                "staleness alerts suppressed."
                            ),
                        },
                        "checked_at": now.isoformat(),
                    },
                    sort_keys=True,
                )
            )
            return 0

        # Schema check — Phase 1a-i migration must have applied.
        if not _schema_present(conn):
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "schema_missing",
                        "detail": (
                            "candidates table missing one or more of "
                            "liquidity_usd_enriched / "
                            "liquidity_enriched_source / "
                            "liquidity_enriched_at / "
                            "liquidity_enriched_confidence"
                        ),
                        "db": str(db_path),
                        "checked_at": now.isoformat(),
                    },
                    sort_keys=True,
                )
            )
            return 4

        # Writer-side branch runs FIRST so the more-actionable diagnosis
        # wins when both writer-staleness and row-SLO breach would
        # otherwise alert (matches check_source_calls_lag.py ordering).
        has_rows = _enrichment_table_has_rows(conn)
        writer_finding = _check_writer_heartbeat(
            heartbeat_path,
            args.writer_threshold_minutes,
            has_enrichment_rows=has_rows,
            now=now,
        )
        if writer_finding is not None:
            status, detail = writer_finding
            # Clear pending-since when leaving the pending state.
            if status not in (
                "writer_heartbeat_pending",
                "writer_never_fired",
            ):
                _clear_pending_since(heartbeat_path)
            payload = {
                "ok": status == "writer_heartbeat_pending",
                "status": status,
                "detail": detail,
                "checked_at": now.isoformat(),
            }
            print(json.dumps(payload, sort_keys=True))
            return 0 if status == "writer_heartbeat_pending" else 5

        # Writer healthy (or branch disabled). If branch active and
        # healthy, clear any stale pending-since marker.
        if heartbeat_path is not None and heartbeat_path.exists():
            _clear_pending_since(heartbeat_path)

        # Row-rate SLO branch.
        coverage = _row_coverage(
            conn,
            recent_window_hours=args.recent_window_hours,
            staleness_threshold_minutes=args.staleness_threshold_minutes,
            now=now,
        )
    finally:
        conn.close()

    if coverage["denominator"] == 0:
        # No signal yet — first-run / fresh deploy. Report OK with status
        # so the operator can see "watchdog is wired up but waiting for
        # data" rather than treating no-recent-rows as a breach.
        result = {
            "ok": True,
            "status": "no_recent_candidates",
            "coverage": coverage,
            "checked_at": now.isoformat(),
        }
        print(json.dumps(result, sort_keys=True))
        return 0

    ok = coverage["ratio"] >= args.row_coverage_threshold
    result = {
        "ok": ok,
        "status": "row_slo_ok" if ok else "row_slo_breach",
        "coverage": coverage,
        "row_coverage_threshold": args.row_coverage_threshold,
        "checked_at": now.isoformat(),
    }
    print(json.dumps(result, sort_keys=True))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
