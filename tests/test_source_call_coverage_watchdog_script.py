"""Tests for the C4 coverage-watchdog cron entrypoint (#392).

The disabled path and the enabled-but-no-alert evaluation path are both
aiohttp-free by design, so they run on Windows via subprocess. The alert-send
path (aiohttp + alerter) is exercised on CI/VPS; the watchdog LOGIC is unit-
tested in test_source_call_c4_watchdogs.py.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scout.db import Database

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "source_call_coverage_watchdog.py"


def test_watchdog_script_disabled_is_inert(tmp_path):
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(tmp_path / "absent.db")],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    body = json.loads(res.stdout)
    assert body["ok"] is True
    assert body["skipped"] == "watchdog_disabled"


async def test_watchdog_script_enabled_empty_db_no_alerts(tmp_path):
    # Enabled against a fresh empty DB: every check suppresses/ok (writer never
    # ran, no eligible calls) -> zero alerts -> exit 0, no aiohttp/network.
    dbp = tmp_path / "wd.db"
    d = Database(dbp)
    await d.initialize()
    await d.close()

    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(dbp), "--enabled", "true"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    body = json.loads(res.stdout)
    assert body["ok"] is True
    assert body["alerts"] == 0
    checks = {f["check"] for f in body["findings"]}
    assert "writer_freshness" in checks
    assert "provider_error_spike" in checks
    # writer never ran -> freshness + provider-error are suppressed, not alerting
    statuses = {f["check"]: f["status"] for f in body["findings"]}
    assert statuses["writer_freshness"] == "suppressed"
    assert statuses["provider_error_spike"] == "suppressed"
