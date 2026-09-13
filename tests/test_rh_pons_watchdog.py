"""Exercise the existing watchdog CLI against the RH heartbeat, without sends."""

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone


def _run(tmp_path, age_hours=None, *, enabled=True, lane=True, dex_fresh=False):
    db = tmp_path / "watchdog.db"
    now = datetime.now(timezone.utc)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE ingest_watchdog_state (source TEXT, updated_at TEXT)"
        )
        conn.execute("CREATE TABLE curve_launch_discoveries (first_seen_at TEXT)")
        if age_hours is not None:
            conn.execute(
                "INSERT INTO ingest_watchdog_state VALUES (?, ?)",
                ("rh_pons", (now - timedelta(hours=age_hours)).isoformat()),
            )
        if dex_fresh:
            conn.execute(
                "INSERT INTO ingest_watchdog_state VALUES (?, ?)",
                ("dex_discovery", now.isoformat()),
            )
    return subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "scripts/dex_discovery_watchdog.py"),
            "--source",
            "rh_pons",
            "--db",
            str(db),
            "--enabled",
            str(enabled),
            "--discovery-enabled",
            str(lane),
            "--dry-run",
            "--staleness-hours",
            "1",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_rh_fresh_empty_market_is_healthy(tmp_path):
    result = _run(tmp_path, 0.01)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["check"]["status"] == "ok"
    assert report["check"]["last_new_discovery_at"] is None


def test_other_lane_heartbeat_cannot_hide_rh_outage(tmp_path):
    result = _run(tmp_path, dex_fresh=True)
    assert result.returncode == 5, result.stderr
    report = json.loads(result.stdout.splitlines()[0])
    assert report["check"]["reason"] == "heartbeat_absent"
    assert "rh_pons heartbeat" in result.stdout


def test_rh_stale_heartbeat_is_breach(tmp_path):
    result = _run(tmp_path, 2)
    assert result.returncode == 5, result.stderr
    assert json.loads(result.stdout.splitlines()[0])["check"]["reason"] == "stale"


def test_rh_disabled_lane_does_not_page(tmp_path):
    result = _run(tmp_path, lane=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "not_armed_discovery_disabled"


def test_rh_future_heartbeat_does_not_hide_outage(tmp_path):
    result = _run(tmp_path, -2)
    assert result.returncode == 5, result.stderr
    assert (
        json.loads(result.stdout.splitlines()[0])["check"]["reason"] == "future_invalid"
    )
