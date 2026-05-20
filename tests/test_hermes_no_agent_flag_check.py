"""Tests for scripts/hermes-no-agent-flag-check.sh.

BL-NEW-HERMES-CRON-NO-AGENT-FLAG-WATCHDOG (2026-05-20).

The script is a single-shot validator for the gecko-x-narrative-scanner
Hermes cron job's `no_agent: true` flag (and adjacent invariants).
These tests stub jobs.json with various failure shapes and assert the
correct exit code + structured stderr output.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash + jq semantics are Linux-specific (mirrors tests/test_cron_drift_watchdog.py)",
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "hermes-no-agent-flag-check.sh"


def _good_jobs_json(tmp_path: Path) -> Path:
    """Standard jobs.json shape — the gecko-x-narrative-scanner job present
    with no_agent=true, enabled=true, script set."""
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps({
        "jobs": [{
            "id": "c849fffec986",
            "name": "gecko-x-narrative-scanner",
            "script": "gecko_x_narrative_scanner.sh",
            "no_agent": True,
            "schedule": {"kind": "cron", "expr": "0 * * * *"},
            "enabled": True,
            "last_status": "success",
        }],
    }))
    return path


def _run(jobs_json: Path, *args: str) -> subprocess.CompletedProcess:
    """Invoke the script with HERMES_CRON_JOBS_JSON pointing to the
    fixture. Returns CompletedProcess."""
    env = os.environ.copy()
    env["HERMES_CRON_JOBS_JSON"] = str(jobs_json)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_happy_path_exits_zero(tmp_path):
    result = _run(_good_jobs_json(tmp_path))
    assert result.returncode == 0
    assert result.stderr == ""


def test_verbose_emits_stdout_summary(tmp_path):
    result = _run(_good_jobs_json(tmp_path), "--verbose")
    assert result.returncode == 0
    assert "HERMES-NO-AGENT-CHECK-OK" in result.stdout
    assert "no_agent=true" in result.stdout


def test_missing_jobs_json_exits_1(tmp_path):
    missing = tmp_path / "no-such-file.json"
    result = _run(missing)
    assert result.returncode == 1
    assert "jobs-json-not-readable" in result.stderr


def test_job_not_in_jobs_json_exits_2(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps({"jobs": []}))
    result = _run(path)
    assert result.returncode == 2
    assert "job-not-found" in result.stderr


def test_no_agent_false_exits_3(tmp_path):
    """The load-bearing check — if no_agent is flipped to false, the
    May 15 prompt-injection failure mode returns. Must alert."""
    data = json.loads(_good_jobs_json(tmp_path).read_text())
    data["jobs"][0]["no_agent"] = False
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(data))
    result = _run(path)
    assert result.returncode == 3
    assert "no-agent-flag-flipped" in result.stderr
    assert "prompt-injection-scanner-may-block" in result.stderr


def test_enabled_false_exits_4(tmp_path):
    data = json.loads(_good_jobs_json(tmp_path).read_text())
    data["jobs"][0]["enabled"] = False
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(data))
    result = _run(path)
    assert result.returncode == 4
    assert "cron-disabled" in result.stderr


def test_script_path_missing_exits_5(tmp_path):
    data = json.loads(_good_jobs_json(tmp_path).read_text())
    data["jobs"][0]["script"] = ""
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(data))
    result = _run(path)
    assert result.returncode == 5
    assert "script-path-missing" in result.stderr


def test_script_path_null_exits_5(tmp_path):
    data = json.loads(_good_jobs_json(tmp_path).read_text())
    data["jobs"][0]["script"] = None
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(data))
    result = _run(path)
    assert result.returncode == 5
    assert "script-path-missing" in result.stderr


def test_stderr_is_structured_json_on_failure(tmp_path):
    data = json.loads(_good_jobs_json(tmp_path).read_text())
    data["jobs"][0]["no_agent"] = False
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(data))
    result = _run(path)
    assert result.returncode == 3
    parsed = json.loads(result.stderr.strip())
    assert parsed["event"] == "HERMES-NO-AGENT-CHECK-FAIL"
    assert parsed["reason"] == "no-agent-flag-flipped"
    assert parsed["expected"] == "true"
    assert parsed["actual"] == "false"
    assert parsed["job-id"] == "c849fffec986"


def test_custom_job_id_via_env(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps({
        "jobs": [{
            "id": "custom-id",
            "no_agent": True,
            "enabled": True,
            "script": "x.sh",
        }],
    }))
    env = os.environ.copy()
    env["HERMES_CRON_JOBS_JSON"] = str(path)
    env["HERMES_NARRATIVE_SCANNER_JOB_ID"] = "custom-id"
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0


def test_quiet_flag_suppresses_stdout_on_ok(tmp_path):
    """--quiet means no stdout. Stderr still emits on failure."""
    result = _run(_good_jobs_json(tmp_path), "--quiet")
    assert result.returncode == 0
    assert result.stdout == ""
