"""Tests for scripts/check_source_calls_lag.py — writer-heartbeat branch.

The ledger-lag branch (the script's original behavior) is exercised
indirectly via tests/test_source_calls_lag_watchdog.py through the
bash wrapper. These tests focus on the new writer-side branch.

Pure stdlib (sqlite3 / Path / subprocess) — runs on Windows too.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check_source_calls_lag.py"


def _build_db(db_path: Path, with_source_calls_rows: int = 0) -> None:
    """Build minimal scout.db schema for the check script.

    By default creates the source_calls table with N rows; also creates
    empty tg_social_signals + narrative_alerts_inbound so the ledger-lag
    branch doesn't trip schema_missing.
    """
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE tg_social_signals (id INTEGER PRIMARY KEY, created_at TEXT);
        CREATE TABLE narrative_alerts_inbound (id INTEGER PRIMARY KEY, event_id TEXT, received_at TEXT);
        CREATE TABLE source_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            UNIQUE (source_type, source_event_id)
        );
        """
    )
    for i in range(with_source_calls_rows):
        cur.execute(
            "INSERT INTO source_calls (source_type, source_event_id) VALUES (?, ?)",
            ("tg", str(i)),
        )
    conn.commit()
    conn.close()


def _run_check(
    db: Path,
    heartbeat: Path | None = None,
    writer_threshold: int = 20,
) -> subprocess.CompletedProcess:
    args = [sys.executable, str(CHECK_SCRIPT), "--db", str(db)]
    if heartbeat is not None:
        args.extend(["--writer-heartbeat-file", str(heartbeat)])
        args.extend(["--writer-threshold-minutes", str(writer_threshold)])
    return subprocess.run(args, capture_output=True, text=True)


# ----- Writer-side branch ON, heartbeat present -----


def test_writer_branch_omitted_keeps_existing_behavior(tmp_path):
    """No --writer-heartbeat-file arg → existing ledger-lag branch runs."""
    db = tmp_path / "scout.db"
    _build_db(db)
    res = _run_check(db, heartbeat=None)
    assert res.returncode == 0, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    # Existing JSON shape has no "status" field — back-compat.
    assert "status" not in body
    assert "unledgered_tg" in body


def test_writer_heartbeat_fresh_falls_through_to_ledger_check(tmp_path):
    """Heartbeat exists and is fresh → writer branch returns None → ledger check runs."""
    db = tmp_path / "scout.db"
    _build_db(db)
    heartbeat = tmp_path / "heartbeat"
    heartbeat.touch()  # mtime = now

    res = _run_check(db, heartbeat=heartbeat, writer_threshold=20)
    assert res.returncode == 0, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    # Ledger-lag JSON shape (no status field, has unledgered_*)
    assert "unledgered_tg" in body
    assert body["unledgered_tg"] == 0
    assert body["unledgered_x"] == 0


def test_writer_stale_returns_exit_5_with_status(tmp_path):
    """Heartbeat older than threshold → exit 5, status=writer_stale."""
    db = tmp_path / "scout.db"
    _build_db(db, with_source_calls_rows=3)
    heartbeat = tmp_path / "heartbeat"
    heartbeat.touch()
    # Set mtime to 30 minutes ago
    past = time.time() - 30 * 60
    os.utime(heartbeat, (past, past))

    res = _run_check(db, heartbeat=heartbeat, writer_threshold=20)
    assert res.returncode == 5, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["status"] == "writer_stale"
    assert body["ok"] is False
    assert body["detail"]["age_minutes"] >= 20
    assert body["detail"]["threshold_minutes"] == 20
    assert body["detail"]["last_writer_success_at"]  # ISO timestamp present


def test_writer_heartbeat_missing_with_ledger_rows_exits_5(tmp_path):
    """Heartbeat absent + ledger has rows → exit 5, status=writer_heartbeat_missing."""
    db = tmp_path / "scout.db"
    _build_db(db, with_source_calls_rows=5)
    heartbeat = tmp_path / "heartbeat"  # never touched

    res = _run_check(db, heartbeat=heartbeat, writer_threshold=20)
    assert res.returncode == 5, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["status"] == "writer_heartbeat_missing"
    assert body["ok"] is False
    assert body["detail"]["ledger_has_rows"] is True


def test_writer_heartbeat_missing_with_empty_ledger_exits_0_pending(tmp_path):
    """Heartbeat absent + ledger empty + first observation → exit 0 (alert suppressed)."""
    db = tmp_path / "scout.db"
    _build_db(db, with_source_calls_rows=0)
    heartbeat = tmp_path / "heartbeat"

    res = _run_check(db, heartbeat=heartbeat, writer_threshold=20)
    assert res.returncode == 0, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["status"] == "writer_heartbeat_pending"
    assert body["ok"] is True  # alert suppressed
    assert body["detail"]["alert_suppressed"] is True

    # The pending-since marker should have been created.
    pending_since = heartbeat.with_name(heartbeat.name + ".pending-since")
    assert pending_since.exists()


def test_pending_after_escalation_threshold_escalates_to_writer_never_fired(tmp_path):
    """Pending-since older than 6h escalation threshold → writer_never_fired alert.

    Threshold chosen as 6h (not 24h) per PR review fold: fresh activation
    where operator forgot to set the .env line should surface within a
    half-business-day, not the next morning. Plan kill criterion +
    activation runbook are now consistent with this threshold.
    """
    db = tmp_path / "scout.db"
    _build_db(db, with_source_calls_rows=0)
    heartbeat = tmp_path / "heartbeat"

    # Pre-create the pending-since marker 7h old (just past 6h threshold).
    pending_since = heartbeat.with_name(heartbeat.name + ".pending-since")
    pending_since.touch()
    past = time.time() - 7 * 3600
    os.utime(pending_since, (past, past))

    res = _run_check(db, heartbeat=heartbeat, writer_threshold=20)
    assert res.returncode == 5, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["status"] == "writer_never_fired"
    assert body["ok"] is False
    assert body["detail"]["age_hours"] >= 6
    assert body["detail"]["escalation_hours"] == 6


def test_writer_recovery_clears_pending_since(tmp_path):
    """When writer recovers (heartbeat fresh), the pending-since marker is removed."""
    db = tmp_path / "scout.db"
    _build_db(db)
    heartbeat = tmp_path / "heartbeat"
    pending_since = heartbeat.with_name(heartbeat.name + ".pending-since")

    # Pre-create both (writer was pending, now recovered)
    heartbeat.touch()  # fresh
    pending_since.touch()  # leftover from pending period

    res = _run_check(db, heartbeat=heartbeat, writer_threshold=20)
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert not pending_since.exists(), "pending-since marker must be removed on recovery"


def test_db_not_found_with_writer_branch_still_exits_3(tmp_path):
    """When DB missing, db_not_found path runs BEFORE writer check (preserves existing behavior)."""
    heartbeat = tmp_path / "heartbeat"
    heartbeat.touch()
    res = _run_check(tmp_path / "nonexistent.db", heartbeat=heartbeat)
    assert res.returncode == 3, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["error"] == "db_not_found"


def test_source_calls_table_missing_treated_as_empty_ledger(tmp_path):
    """Fresh DB where source_calls table doesn't yet exist → ledger_has_rows=False →
    writer-pending path (alert-suppressed), NOT a crash. Per Reviewer-B C3."""
    db = tmp_path / "scout.db"
    # Empty DB with tg_social_signals + narrative_alerts_inbound but NO source_calls
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE tg_social_signals (id INTEGER PRIMARY KEY, created_at TEXT);
        CREATE TABLE narrative_alerts_inbound (id INTEGER PRIMARY KEY, event_id TEXT, received_at TEXT);
        """
    )
    conn.commit()
    conn.close()

    heartbeat = tmp_path / "heartbeat"  # never touched
    res = _run_check(db, heartbeat=heartbeat, writer_threshold=20)
    assert res.returncode == 0, (res.stdout, res.stderr)
    body = json.loads(res.stdout)
    assert body["status"] == "writer_heartbeat_pending"


def test_status_field_present_only_when_writer_branch_active(tmp_path):
    """Back-compat: when writer branch is off, JSON has NO 'status' field
    (existing tests/wrapper depend on parsed_status==unknown fallback)."""
    db = tmp_path / "scout.db"
    _build_db(db)
    res = _run_check(db, heartbeat=None)
    body = json.loads(res.stdout)
    assert "status" not in body
