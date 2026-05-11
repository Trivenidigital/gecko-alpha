import os
import stat
import subprocess
import time
from pathlib import Path

import pytest


WATCHDOG = Path(__file__).parent.parent.parent / "scripts" / "gecko-audit-snapshot-watchdog.sh"


def _make_uv_stub(tmp_path: Path) -> Path:
    """Create a stub UV_BIN that records its invocation args to a file."""
    stub = tmp_path / "uv-stub.sh"
    stub.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{tmp_path}/uv-stub.log"\n')
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


@pytest.mark.skipif(os.name == "nt", reason="bash watchdog runs on Linux/WSL only")
def test_watchdog_exits_0_when_heartbeat_fresh(tmp_path):
    """Fresh heartbeat (now) → exit 0 + no alert invocation."""
    hb = tmp_path / "hb"
    hb.write_text(str(int(time.time())))
    uv_stub = _make_uv_stub(tmp_path)

    env = {
        "GECKO_AUDIT_HEARTBEAT_FILE": str(hb),
        "GECKO_AUDIT_STALE_AFTER_SEC": "30",
        "UV_BIN": str(uv_stub),
        "PATH": os.environ.get("PATH", ""),
    }
    result = subprocess.run(
        ["bash", str(WATCHDOG)], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0
    assert not (tmp_path / "uv-stub.log").exists()  # no alert fired


@pytest.mark.skipif(os.name == "nt", reason="bash watchdog runs on Linux/WSL only")
def test_watchdog_exits_1_and_alerts_when_stale(tmp_path):
    """Stale heartbeat → exit 1 + UV_BIN stub invoked with age message."""
    hb = tmp_path / "hb"
    hb.write_text(str(int(time.time()) - 200))  # 200s old
    uv_stub = _make_uv_stub(tmp_path)

    env = {
        "GECKO_AUDIT_HEARTBEAT_FILE": str(hb),
        "GECKO_AUDIT_STALE_AFTER_SEC": "30",  # 30s threshold; 200s > 30s
        "UV_BIN": str(uv_stub),
        "PATH": os.environ.get("PATH", ""),
    }
    result = subprocess.run(
        ["bash", str(WATCHDOG)], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 1
    log = (tmp_path / "uv-stub.log").read_text()
    assert "stub-audit-snapshot-watchdog-alert" in log


@pytest.mark.skipif(os.name == "nt", reason="bash watchdog runs on Linux/WSL only")
def test_watchdog_exits_1_and_alerts_when_heartbeat_missing(tmp_path):
    """Missing heartbeat file → exit 1 + alert."""
    hb = tmp_path / "does-not-exist"
    uv_stub = _make_uv_stub(tmp_path)

    env = {
        "GECKO_AUDIT_HEARTBEAT_FILE": str(hb),
        "GECKO_AUDIT_STALE_AFTER_SEC": "30",
        "UV_BIN": str(uv_stub),
        "PATH": os.environ.get("PATH", ""),
    }
    result = subprocess.run(
        ["bash", str(WATCHDOG)], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 1
    log = (tmp_path / "uv-stub.log").read_text()
    assert "MISSING" in log or "stub-audit-snapshot-watchdog-alert" in log


@pytest.mark.skipif(os.name == "nt", reason="bash watchdog runs on Linux/WSL only")
def test_watchdog_exits_1_and_alerts_when_heartbeat_corrupt(tmp_path):
    """Corrupt heartbeat content (non-numeric) → exit 1 + alert."""
    hb = tmp_path / "hb"
    hb.write_text("not-a-number")
    uv_stub = _make_uv_stub(tmp_path)

    env = {
        "GECKO_AUDIT_HEARTBEAT_FILE": str(hb),
        "GECKO_AUDIT_STALE_AFTER_SEC": "30",
        "UV_BIN": str(uv_stub),
        "PATH": os.environ.get("PATH", ""),
    }
    result = subprocess.run(
        ["bash", str(WATCHDOG)], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 1
    log = (tmp_path / "uv-stub.log").read_text()
    assert "CORRUPT" in log or "stub-audit-snapshot-watchdog-alert" in log
