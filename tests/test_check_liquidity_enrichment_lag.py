"""Tests for scripts/check_liquidity_enrichment_lag.py — writer + row-SLO branches.

Mirrors tests/test_check_source_calls_lag.py shape. Pure stdlib
(sqlite3 / Path / subprocess) — runs on Windows too.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check_liquidity_enrichment_lag.py"


def _build_db(
    db_path: Path,
    *,
    with_enrichment_schema: bool = True,
    recent_rows: int = 0,
    recent_enriched_rows: int = 0,
    recent_stale_rows: int = 0,
    very_old_enriched_rows: int = 0,
) -> None:
    """Build a minimal scout.db `candidates` table for the watchdog.

    - ``with_enrichment_schema``: include the 4 Phase 1a-i columns.
    - ``recent_rows``: rows with ``first_seen_at = now`` (unenriched).
    - ``recent_enriched_rows``: recent rows with ``liquidity_enriched_at``
      set to now (fresh per default 30-min threshold).
    - ``recent_stale_rows``: recent rows with ``liquidity_enriched_at``
      set 2h ago (stale per default 30-min threshold).
    - ``very_old_enriched_rows``: rows opened 10d ago (outside default
      7d recent window). Used to verify the row-SLO denominator excludes
      old rows.
    """
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    base_cols = [
        "contract_address TEXT PRIMARY KEY",
        "chain TEXT NOT NULL",
        "token_name TEXT NOT NULL",
        "ticker TEXT NOT NULL",
        "first_seen_at TEXT NOT NULL",
    ]
    if with_enrichment_schema:
        base_cols.extend(
            [
                "liquidity_usd_enriched REAL",
                "liquidity_enriched_source TEXT",
                "liquidity_enriched_at TEXT",
                "liquidity_enriched_confidence TEXT",
            ]
        )
    cur.execute(f"CREATE TABLE candidates ({', '.join(base_cols)})")

    counter = 0

    def _insert(first_seen: datetime, enriched_at: datetime | None) -> None:
        nonlocal counter
        counter += 1
        if with_enrichment_schema:
            cur.execute(
                "INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"contract_{counter}",
                    "coingecko",
                    f"tok_{counter}",
                    f"T{counter}",
                    first_seen.isoformat(),
                    None,
                    "dexscreener_v1" if enriched_at else None,
                    enriched_at.isoformat() if enriched_at else None,
                    "definite" if enriched_at else None,
                ),
            )
        else:
            cur.execute(
                "INSERT INTO candidates VALUES (?, ?, ?, ?, ?)",
                (
                    f"contract_{counter}",
                    "coingecko",
                    f"tok_{counter}",
                    f"T{counter}",
                    first_seen.isoformat(),
                ),
            )

    for _ in range(recent_rows):
        _insert(now - timedelta(hours=1), None)
    for _ in range(recent_enriched_rows):
        _insert(now - timedelta(hours=1), now - timedelta(minutes=5))
    for _ in range(recent_stale_rows):
        _insert(now - timedelta(hours=1), now - timedelta(hours=2))
    for _ in range(very_old_enriched_rows):
        _insert(now - timedelta(days=10), now - timedelta(minutes=5))

    conn.commit()
    conn.close()


def _run_check(
    db: Path,
    *,
    heartbeat: Path | None = None,
    writer_threshold: int = 20,
    killswitch_disabled: bool = False,
    row_coverage_threshold: float = 0.80,
    recent_window_hours: int = 168,
    staleness_threshold_minutes: int = 30,
) -> subprocess.CompletedProcess:
    args = [sys.executable, str(CHECK_SCRIPT), "--db", str(db)]
    if heartbeat is not None:
        args.extend(["--writer-heartbeat-file", str(heartbeat)])
        args.extend(["--writer-threshold-minutes", str(writer_threshold)])
    if killswitch_disabled:
        args.append("--killswitch-disabled")
    args.extend(["--row-coverage-threshold", str(row_coverage_threshold)])
    args.extend(["--recent-window-hours", str(recent_window_hours)])
    args.extend(
        ["--staleness-threshold-minutes", str(staleness_threshold_minutes)]
    )
    return subprocess.run(args, capture_output=True, text=True)


# ----- DB / schema sanity -----


def test_db_missing_exits_3(tmp_path):
    """Missing DB file → exit 3, error=db_not_found."""
    res = _run_check(tmp_path / "nonexistent.db")
    assert res.returncode == 3, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["error"] == "db_not_found"
    assert body["ok"] is False


def test_schema_missing_exits_4(tmp_path):
    """`candidates` table missing the 4 enrichment columns → exit 4."""
    db = tmp_path / "scout.db"
    _build_db(db, with_enrichment_schema=False)
    res = _run_check(db)
    assert res.returncode == 4, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["error"] == "schema_missing"


# ----- Killswitch branch (operator guardrail #5) -----


def test_killswitch_disabled_short_circuits_all_checks(tmp_path):
    """--killswitch-disabled exits 0 even when writer would otherwise be stale."""
    db = tmp_path / "scout.db"
    _build_db(db, recent_rows=10, recent_stale_rows=0)
    heartbeat = tmp_path / "heartbeat"
    heartbeat.touch()
    past = time.time() - 60 * 60  # 60 min ago, well past threshold
    os.utime(heartbeat, (past, past))

    res = _run_check(
        db,
        heartbeat=heartbeat,
        writer_threshold=20,
        killswitch_disabled=True,
    )
    assert res.returncode == 0, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["ok"] is True
    assert body["status"] == "killswitch_disabled"


def test_killswitch_disabled_with_missing_schema_still_exits_0(tmp_path):
    """Killswitch short-circuits BEFORE schema check — operator may be
    running the watchdog before migration applies."""
    db = tmp_path / "scout.db"
    _build_db(db, with_enrichment_schema=False)
    res = _run_check(db, killswitch_disabled=True)
    assert res.returncode == 0, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["status"] == "killswitch_disabled"


# ----- Writer-side branch (mirrors check_source_calls_lag.py shape) -----


def test_writer_branch_omitted_falls_through_to_row_slo(tmp_path):
    """No --writer-heartbeat-file → writer branch returns None →
    row-rate SLO branch runs."""
    db = tmp_path / "scout.db"
    _build_db(db, recent_rows=10, recent_enriched_rows=8)
    res = _run_check(db, heartbeat=None)
    assert res.returncode == 0, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["status"] == "row_slo_ok"
    assert body["coverage"]["ratio"] >= 0.80


def test_writer_heartbeat_fresh_falls_through_to_row_slo(tmp_path):
    """Heartbeat present + fresh → writer branch returns None → row SLO runs."""
    db = tmp_path / "scout.db"
    _build_db(db, recent_rows=10, recent_enriched_rows=8)
    heartbeat = tmp_path / "heartbeat"
    heartbeat.touch()

    res = _run_check(db, heartbeat=heartbeat, writer_threshold=20)
    assert res.returncode == 0, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["status"] == "row_slo_ok"


def test_writer_stale_returns_exit_5(tmp_path):
    """Heartbeat older than threshold → exit 5, status=writer_stale."""
    db = tmp_path / "scout.db"
    _build_db(db, recent_enriched_rows=3)
    heartbeat = tmp_path / "heartbeat"
    heartbeat.touch()
    past = time.time() - 30 * 60
    os.utime(heartbeat, (past, past))

    res = _run_check(db, heartbeat=heartbeat, writer_threshold=20)
    assert res.returncode == 5, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["status"] == "writer_stale"
    assert body["ok"] is False
    assert body["detail"]["age_minutes"] >= 20
    assert body["detail"]["threshold_minutes"] == 20
    assert body["detail"]["last_writer_success_at"]


def test_writer_heartbeat_missing_with_enrichment_rows_exits_5(tmp_path):
    """Heartbeat absent + enrichment rows exist → exit 5,
    status=writer_heartbeat_missing."""
    db = tmp_path / "scout.db"
    _build_db(db, recent_enriched_rows=5)
    heartbeat = tmp_path / "heartbeat"  # never touched

    res = _run_check(db, heartbeat=heartbeat, writer_threshold=20)
    assert res.returncode == 5, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["status"] == "writer_heartbeat_missing"
    assert body["ok"] is False
    assert body["detail"]["has_enrichment_rows"] is True


def test_writer_heartbeat_missing_empty_table_exits_0_pending(tmp_path):
    """Heartbeat absent + no enrichment rows + first observation →
    exit 0, status=writer_heartbeat_pending (alert suppressed)."""
    db = tmp_path / "scout.db"
    _build_db(db, recent_rows=5)  # rows exist but none enriched
    heartbeat = tmp_path / "heartbeat"  # never touched

    res = _run_check(db, heartbeat=heartbeat, writer_threshold=20)
    assert res.returncode == 0, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["status"] == "writer_heartbeat_pending"
    assert body["ok"] is True
    assert body["detail"]["alert_suppressed"] is True

    pending_since = heartbeat.with_name(heartbeat.name + ".pending-since")
    assert pending_since.exists()


def test_pending_past_escalation_threshold_escalates_to_never_fired(tmp_path):
    """Pending-since older than 6h → writer_never_fired alert."""
    db = tmp_path / "scout.db"
    _build_db(db, recent_rows=5)
    heartbeat = tmp_path / "heartbeat"

    pending_since = heartbeat.with_name(heartbeat.name + ".pending-since")
    pending_since.touch()
    past = time.time() - 7 * 3600  # 7h, just past 6h threshold
    os.utime(pending_since, (past, past))

    res = _run_check(db, heartbeat=heartbeat, writer_threshold=20)
    assert res.returncode == 5, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["status"] == "writer_never_fired"
    assert body["ok"] is False
    assert body["detail"]["age_hours"] >= 6
    assert body["detail"]["escalation_hours"] == 6


def test_writer_recovery_clears_pending_since(tmp_path):
    """When writer recovers (heartbeat fresh) the pending-since marker
    is removed so the next genuine pending observation gets a fresh
    timer."""
    db = tmp_path / "scout.db"
    _build_db(db, recent_rows=5)
    heartbeat = tmp_path / "heartbeat"
    pending_since = heartbeat.with_name(heartbeat.name + ".pending-since")

    heartbeat.touch()  # fresh
    pending_since.touch()  # leftover from prior pending period

    res = _run_check(db, heartbeat=heartbeat, writer_threshold=20)
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert not pending_since.exists()


def test_writer_branch_runs_before_row_slo(tmp_path):
    """Writer-stale should win over row-SLO breach (more actionable
    diagnosis wins). Mirrors check_source_calls_lag.py ordering."""
    db = tmp_path / "scout.db"
    # Build state where BOTH would fire: row coverage is 0% AND heartbeat
    # is stale. Watchdog must report writer_stale (exit 5), not row breach.
    _build_db(db, recent_rows=10, recent_enriched_rows=0)
    heartbeat = tmp_path / "heartbeat"
    heartbeat.touch()
    past = time.time() - 60 * 60
    os.utime(heartbeat, (past, past))

    res = _run_check(db, heartbeat=heartbeat, writer_threshold=20)
    assert res.returncode == 5, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["status"] == "writer_stale"


# ----- Row-rate SLO branch (operator guardrail #5 deeper check) -----


def test_row_slo_met_exits_0(tmp_path):
    """≥80% of recent rows have liquidity_enriched_at within threshold → ok."""
    db = tmp_path / "scout.db"
    _build_db(db, recent_rows=2, recent_enriched_rows=8)  # 8/10 = 80%
    res = _run_check(db, row_coverage_threshold=0.80)
    assert res.returncode == 0, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["status"] == "row_slo_ok"
    assert body["coverage"]["denominator"] == 10
    assert body["coverage"]["numerator"] == 8


def test_row_slo_breached_exits_2(tmp_path):
    """<80% of recent rows fresh → exit 2, status=row_slo_breach."""
    db = tmp_path / "scout.db"
    _build_db(db, recent_rows=7, recent_enriched_rows=3)  # 3/10 = 30%
    res = _run_check(db, row_coverage_threshold=0.80)
    assert res.returncode == 2, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["status"] == "row_slo_breach"
    assert body["coverage"]["ratio"] < 0.80


def test_row_slo_stale_rows_count_as_unfresh(tmp_path):
    """Rows enriched 2h ago count as unfresh under default 30-min threshold."""
    db = tmp_path / "scout.db"
    _build_db(db, recent_enriched_rows=2, recent_stale_rows=8)
    res = _run_check(db, row_coverage_threshold=0.80)
    assert res.returncode == 2, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["status"] == "row_slo_breach"
    assert body["coverage"]["numerator"] == 2
    assert body["coverage"]["denominator"] == 10


def test_no_recent_rows_exits_0_no_signal(tmp_path):
    """Empty candidates / no rows in recent window → exit 0,
    status=no_recent_candidates (not a breach)."""
    db = tmp_path / "scout.db"
    _build_db(db, very_old_enriched_rows=5)  # all 10d old, outside 7d window
    res = _run_check(db, row_coverage_threshold=0.80)
    assert res.returncode == 0, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["status"] == "no_recent_candidates"
    assert body["coverage"]["denominator"] == 0


def test_row_slo_denominator_excludes_old_rows(tmp_path):
    """Old rows (outside recent_window_hours) don't dilute the denominator."""
    db = tmp_path / "scout.db"
    # 10 recent rows, 8 enriched fresh. 100 ancient rows, all stale.
    # Denominator should be 10 (recent only).
    _build_db(
        db,
        recent_rows=2,
        recent_enriched_rows=8,
        very_old_enriched_rows=100,
    )
    res = _run_check(db, row_coverage_threshold=0.80, recent_window_hours=168)
    assert res.returncode == 0, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["coverage"]["denominator"] == 10
    assert body["coverage"]["numerator"] == 8


# ----- Output shape contract -----


def test_output_includes_checked_at_iso8601_in_all_paths(tmp_path):
    """Every code path emits a `checked_at` ISO-8601 timestamp."""
    db = tmp_path / "scout.db"
    _build_db(db, recent_rows=5, recent_enriched_rows=5)
    res = _run_check(db)
    body = json.loads(res.stdout)
    assert "checked_at" in body
    # parseable
    parsed = datetime.fromisoformat(body["checked_at"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
