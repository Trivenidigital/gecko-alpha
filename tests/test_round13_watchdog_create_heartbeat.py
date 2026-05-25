"""Round 13: backup-watchdog also monitors the create heartbeat.

Closes the blind spot identified after R11 deploy: the existing watchdog
only checked the rotate-step heartbeat. If R11's create-step starts
failing (sqlite3 missing, integrity_check fail, disk full, dest dir
gone), the rotate step may still run against an empty dir and update
its own heartbeat — exactly the pre-R11 pathology that left srilu with
zero recoverable backups for weeks while the watchdog stayed green.

The dual-check is gated on GECKO_BACKUP_CREATE_HEARTBEAT_FILE being
explicitly set in the environment. The systemd unit sets it; the
existing test suite + bare-CLI invocations don't, so backward compat
is preserved.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash watchdog tests are Linux-specific",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHDOG = REPO_ROOT / "scripts" / "gecko-backup-watchdog.sh"


def _make_uv_stub(tmp_path: Path) -> tuple[Path, Path]:
    marker = tmp_path / "uv-stub-fired"
    stub = tmp_path / "uv-stub"
    stub.write_text(
        f"#!/usr/bin/env bash\necho \"stub fired: $*\" > {marker}\nexit 0\n"
    )
    stub.chmod(0o755)
    return stub, marker


def _run(env_overrides):
    env = os.environ.copy()
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(WATCHDOG)],
        env=env,
        capture_output=True,
        text=True,
    )


def _fresh_heartbeat(p: Path, age_seconds: int = 3600) -> None:
    p.write_text(str(int(time.time() - age_seconds)))


def test_create_heartbeat_skipped_when_env_unset(tmp_path):
    """Backward compat — existing tests that only set the rotate
    heartbeat must still see exit 0."""
    rotate_hb = tmp_path / "rotate-hb"
    _fresh_heartbeat(rotate_hb)
    stub, marker = _make_uv_stub(tmp_path)
    env_overrides = {
        "GECKO_BACKUP_HEARTBEAT_FILE": str(rotate_hb),
        "GECKO_REPO": str(tmp_path),
        "GECKO_BACKUP_STALE_AFTER_SEC": "172800",
        "UV_BIN": str(stub),
    }
    # Explicitly REMOVE the env var so the watchdog sees it unset.
    env_overrides["GECKO_BACKUP_CREATE_HEARTBEAT_FILE"] = ""
    res = _run(env_overrides)
    assert res.returncode == 0, f"stderr: {res.stderr}\nstdout: {res.stdout}"
    assert "create check skipped" in res.stdout
    assert not marker.exists(), "stub should NOT fire when both checks pass"


def test_create_heartbeat_fresh_both_ok(tmp_path):
    """Both heartbeats fresh → exit 0."""
    rotate_hb = tmp_path / "rotate-hb"
    create_hb = tmp_path / "create-hb"
    _fresh_heartbeat(rotate_hb)
    _fresh_heartbeat(create_hb)
    stub, marker = _make_uv_stub(tmp_path)
    res = _run(
        {
            "GECKO_BACKUP_HEARTBEAT_FILE": str(rotate_hb),
            "GECKO_BACKUP_CREATE_HEARTBEAT_FILE": str(create_hb),
            "GECKO_REPO": str(tmp_path),
            "GECKO_BACKUP_STALE_AFTER_SEC": "172800",
            "UV_BIN": str(stub),
        }
    )
    assert res.returncode == 0, f"stderr: {res.stderr}\nstdout: {res.stdout}"
    assert "rotate last_ok=" in res.stdout
    assert "create last_ok=" in res.stdout
    assert not marker.exists()


def test_create_heartbeat_missing_alerts(tmp_path):
    """Rotate fresh + create heartbeat MISSING → alert."""
    rotate_hb = tmp_path / "rotate-hb"
    _fresh_heartbeat(rotate_hb)
    stub, marker = _make_uv_stub(tmp_path)
    res = _run(
        {
            "GECKO_BACKUP_HEARTBEAT_FILE": str(rotate_hb),
            "GECKO_BACKUP_CREATE_HEARTBEAT_FILE": str(tmp_path / "no-such"),
            "GECKO_REPO": str(tmp_path),
            "GECKO_BACKUP_STALE_AFTER_SEC": "172800",
            "UV_BIN": str(stub),
        }
    )
    assert res.returncode == 1, f"expected exit 1; got {res.returncode}"
    assert "create heartbeat MISSING" in res.stdout
    assert marker.exists()


def test_create_heartbeat_stale_alerts(tmp_path):
    """Rotate fresh + create heartbeat >48h old → alert."""
    rotate_hb = tmp_path / "rotate-hb"
    create_hb = tmp_path / "create-hb"
    _fresh_heartbeat(rotate_hb)
    _fresh_heartbeat(create_hb, age_seconds=49 * 3600)  # 49h old
    stub, marker = _make_uv_stub(tmp_path)
    res = _run(
        {
            "GECKO_BACKUP_HEARTBEAT_FILE": str(rotate_hb),
            "GECKO_BACKUP_CREATE_HEARTBEAT_FILE": str(create_hb),
            "GECKO_REPO": str(tmp_path),
            "GECKO_BACKUP_STALE_AFTER_SEC": "172800",
            "UV_BIN": str(stub),
        }
    )
    assert res.returncode == 1
    assert "(STALE)" in res.stdout
    assert "create" in res.stdout
    assert marker.exists()


def test_create_heartbeat_corrupt_alerts(tmp_path):
    """Rotate fresh + create heartbeat corrupt → alert."""
    rotate_hb = tmp_path / "rotate-hb"
    create_hb = tmp_path / "create-hb"
    _fresh_heartbeat(rotate_hb)
    create_hb.write_text("not-a-number")
    stub, marker = _make_uv_stub(tmp_path)
    res = _run(
        {
            "GECKO_BACKUP_HEARTBEAT_FILE": str(rotate_hb),
            "GECKO_BACKUP_CREATE_HEARTBEAT_FILE": str(create_hb),
            "GECKO_REPO": str(tmp_path),
            "GECKO_BACKUP_STALE_AFTER_SEC": "172800",
            "UV_BIN": str(stub),
        }
    )
    assert res.returncode == 1
    assert "create heartbeat CORRUPT" in res.stdout
    assert marker.exists()


def test_both_heartbeats_stale_alerts_with_both_in_message(tmp_path):
    """Both stale → message mentions both."""
    rotate_hb = tmp_path / "rotate-hb"
    create_hb = tmp_path / "create-hb"
    _fresh_heartbeat(rotate_hb, age_seconds=49 * 3600)
    _fresh_heartbeat(create_hb, age_seconds=72 * 3600)
    stub, marker = _make_uv_stub(tmp_path)
    res = _run(
        {
            "GECKO_BACKUP_HEARTBEAT_FILE": str(rotate_hb),
            "GECKO_BACKUP_CREATE_HEARTBEAT_FILE": str(create_hb),
            "GECKO_REPO": str(tmp_path),
            "GECKO_BACKUP_STALE_AFTER_SEC": "172800",
            "UV_BIN": str(stub),
        }
    )
    assert res.returncode == 1
    assert "rotate" in res.stdout
    assert "create" in res.stdout
    assert "STALE" in res.stdout
    assert marker.exists()


def test_create_stale_rotate_fresh_does_not_mask_create_failure(tmp_path):
    """The exact pre-R11 pathology: rotate runs fine against empty dir,
    its heartbeat is fresh, but create has been failing for days. The
    watchdog MUST still alert."""
    rotate_hb = tmp_path / "rotate-hb"
    create_hb = tmp_path / "create-hb"
    _fresh_heartbeat(rotate_hb, age_seconds=600)  # rotate ran 10min ago
    _fresh_heartbeat(create_hb, age_seconds=72 * 3600)  # create stuck 3d ago
    stub, marker = _make_uv_stub(tmp_path)
    res = _run(
        {
            "GECKO_BACKUP_HEARTBEAT_FILE": str(rotate_hb),
            "GECKO_BACKUP_CREATE_HEARTBEAT_FILE": str(create_hb),
            "GECKO_REPO": str(tmp_path),
            "GECKO_BACKUP_STALE_AFTER_SEC": "172800",
            "UV_BIN": str(stub),
        }
    )
    assert res.returncode == 1, (
        "watchdog must alert on stale create heartbeat even if rotate "
        "heartbeat is fresh — this is the pre-R11 silent-failure shape"
    )
    assert "rotate last_ok=" in res.stdout  # rotate IS fresh
    assert "create" in res.stdout and "STALE" in res.stdout
    assert marker.exists()
