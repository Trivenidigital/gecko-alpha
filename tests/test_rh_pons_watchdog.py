"""Exercise the existing watchdog CLI against the RH heartbeat, without sends."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import pytest
from datetime import datetime, timedelta, timezone


def _run(
    tmp_path,
    age_hours=None,
    *,
    enabled=True,
    lane=True,
    dex_fresh=False,
    head=100,
    next_block=101,
    checkpoint=True,
    primary=True,
    checkpoint_age=0,
    max_lag=2000,
):
    db = tmp_path / "watchdog.db"
    now = datetime.now(timezone.utc)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE ingest_watchdog_state (source TEXT, updated_at TEXT)"
        )
        conn.execute("CREATE TABLE curve_launch_discoveries (first_seen_at TEXT)")
        conn.execute(
            "CREATE TABLE curve_scan_checkpoints (chain_id INTEGER, protocol TEXT, factory TEXT, next_block INTEGER, head_block INTEGER, updated_at TEXT)"
        )
        if checkpoint:
            from scout.ingestion.rh_pons import PONS_DEPLOYMENTS

            dep = next(d for d in PONS_DEPLOYMENTS if d.version == "pons_v2")
            conn.execute(
                "INSERT INTO curve_scan_checkpoints VALUES (?,?,?,?,?,?)",
                (
                    dep.chain_id,
                    dep.version,
                    dep.factory if primary else "0xwrong",
                    next_block,
                    head,
                    (now - timedelta(hours=checkpoint_age)).isoformat(),
                ),
            )
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
            "--max-head-lag-blocks",
            str(max_lag),
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


@pytest.mark.parametrize("max_lag,expected_code", [("2000", 0), ("-1", 1)])
def test_disabled_wrapper_does_not_require_writable_state(
    tmp_path, max_lag, expected_code
):
    bash = (
        Path("C:/Program Files/Git/bin/bash.exe")
        if os.name == "nt"
        else Path("/bin/bash")
    )
    if not bash.exists():
        pytest.skip("Bash is required for the deployment wrapper")
    env = dict(
        os.environ,
        RH_PONS_WATCHDOG_ENABLED="false",
        RH_PONS_MAX_HEAD_LAG_BLOCKS=max_lag,
        RH_PONS_WATCHDOG_STATE_DIR="/dev/null/rh-watchdog",
        GECKO_ENV_FILE=str(tmp_path / "absent.env"),
        GECKO_PYTHON=sys.executable.replace("\\", "/"),
    )
    result = subprocess.run(
        [str(bash), str(Path(__file__).parents[1] / "scripts/rh-pons-watchdog.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == expected_code, result.stderr
    expected_status = "disabled_noop" if expected_code == 0 else "invalid_configuration"
    assert json.loads(result.stdout)["status"] == expected_status


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"checkpoint": False}, "head_lag_unknown"),
        ({"head": None}, "head_lag_unknown"),
        ({"primary": False}, "head_lag_unknown"),
        ({"head": 5000, "next_block": 100}, "head_lag_exceeded"),
        ({"checkpoint_age": 2}, "checkpoint_stale"),
        ({"checkpoint_age": -2}, "checkpoint_future_invalid"),
        ({"head": "invalid"}, "head_lag_unknown"),
    ],
)
def test_fresh_heartbeat_cannot_hide_missing_or_lagged_head(tmp_path, kwargs, reason):
    result = _run(tmp_path, 0.01, **kwargs)
    assert result.returncode == 5, result.stderr
    check = json.loads(result.stdout.splitlines()[0])["check"]
    assert check["reason"] == reason
    assert "has stalled" not in result.stdout


def test_configured_head_lag_boundary(tmp_path):
    result = _run(tmp_path, 0.01, head=2000, next_block=1, max_lag=2000)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["check"]["head_lag_blocks"] == 2000
