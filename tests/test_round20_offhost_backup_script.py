"""Tests for scripts/gecko-backup-offhost.sh (Round 20).

Mirrors the existing tests/test_backup_create_script.py pattern: invoke
the bash script via subprocess with env overrides, assert on exit code +
filesystem side effects. Uses a local directory destination so no SSH or
external host is required.

Skipped on Windows: bash + flock + rsync semantics are Linux-specific.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash + flock + rsync semantics are Linux-specific",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "gecko-backup-offhost.sh"


def _run(env_overrides=None):
    env = os.environ.copy()
    env.update(env_overrides or {})
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )


def _make_bak(path: Path, age_seconds: int, payload: bytes = b"backup data") -> Path:
    path.write_bytes(payload)
    mtime = time.time() - age_seconds
    os.utime(path, (mtime, mtime))
    return path


# ---------------------------------------------------------------------
# Opt-in semantics
# ---------------------------------------------------------------------


def test_offhost_skips_when_dest_unset(tmp_path):
    """Empty GECKO_OFFHOST_BACKUP_DEST → exit 0, no heartbeat, no transfer."""
    backup_dir = tmp_path / "src"
    backup_dir.mkdir()
    heartbeat = tmp_path / "hb"
    lock = tmp_path / "lock"

    env = {
        "GECKO_OFFHOST_BACKUP_DEST": "",
        "GECKO_BACKUP_DIR": str(backup_dir),
        "GECKO_OFFHOST_BACKUP_HEARTBEAT_FILE": str(heartbeat),
        "GECKO_OFFHOST_BACKUP_LOCK_FILE": str(lock),
    }
    proc = _run(env)
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert "disabled" in proc.stdout.lower()
    assert not heartbeat.exists(), "heartbeat must not be written when disabled"


def test_offhost_skips_when_dest_whitespace_only(tmp_path):
    """Whitespace-only dest is still 'unset' — defensive trim."""
    backup_dir = tmp_path / "src"
    backup_dir.mkdir()
    heartbeat = tmp_path / "hb"

    proc = _run(
        {
            "GECKO_OFFHOST_BACKUP_DEST": "   ",
            "GECKO_BACKUP_DIR": str(backup_dir),
            "GECKO_OFFHOST_BACKUP_HEARTBEAT_FILE": str(heartbeat),
            "GECKO_OFFHOST_BACKUP_LOCK_FILE": str(tmp_path / "lock"),
        }
    )
    assert proc.returncode == 0
    assert not heartbeat.exists()


# ---------------------------------------------------------------------
# Misconfiguration
# ---------------------------------------------------------------------


def test_offhost_exit_2_when_no_backup_files(tmp_path):
    """Configured but no .bak files exist → exit 2 (caller alerts)."""
    backup_dir = tmp_path / "src"
    backup_dir.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    heartbeat = tmp_path / "hb"

    proc = _run(
        {
            "GECKO_OFFHOST_BACKUP_DEST": str(dest) + "/",
            "GECKO_BACKUP_DIR": str(backup_dir),
            "GECKO_OFFHOST_BACKUP_HEARTBEAT_FILE": str(heartbeat),
            "GECKO_OFFHOST_BACKUP_LOCK_FILE": str(tmp_path / "lock"),
        }
    )
    assert proc.returncode == 2, f"stderr: {proc.stderr}"
    assert "no scout.db.bak" in proc.stderr.lower()
    assert not heartbeat.exists()


def test_offhost_exit_2_when_backup_dir_missing(tmp_path):
    proc = _run(
        {
            "GECKO_OFFHOST_BACKUP_DEST": str(tmp_path / "dest") + "/",
            "GECKO_BACKUP_DIR": str(tmp_path / "does-not-exist"),
            "GECKO_OFFHOST_BACKUP_HEARTBEAT_FILE": str(tmp_path / "hb"),
            "GECKO_OFFHOST_BACKUP_LOCK_FILE": str(tmp_path / "lock"),
        }
    )
    assert proc.returncode == 2


# ---------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------


def test_offhost_ships_newest_bak_and_writes_heartbeat(tmp_path):
    """Newest .bak gets copied; heartbeat is a unix timestamp."""
    if shutil.which("rsync") is None:
        pytest.skip("rsync not installed")

    backup_dir = tmp_path / "src"
    backup_dir.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    heartbeat = tmp_path / "hb"

    _make_bak(backup_dir / "scout.db.bak.20260524T030000Z", age_seconds=7200)
    newest = _make_bak(
        backup_dir / "scout.db.bak.20260525T030000Z",
        age_seconds=3600,
        payload=b"NEWEST",
    )

    before = int(time.time())
    proc = _run(
        {
            "GECKO_OFFHOST_BACKUP_DEST": str(dest) + "/",
            "GECKO_BACKUP_DIR": str(backup_dir),
            "GECKO_OFFHOST_BACKUP_HEARTBEAT_FILE": str(heartbeat),
            "GECKO_OFFHOST_BACKUP_LOCK_FILE": str(tmp_path / "lock"),
        }
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"

    # Newest .bak landed at destination
    shipped = dest / newest.name
    assert shipped.exists(), f"expected {shipped} after rsync"
    assert shipped.read_bytes() == b"NEWEST"

    # Heartbeat is unix-timestamp-ish
    assert heartbeat.exists()
    hb_value = int(heartbeat.read_text().strip())
    assert before <= hb_value <= int(time.time()) + 5


def test_offhost_skips_partial_sentinel_files(tmp_path):
    """gecko-backup-create.sh writes scout.db.bak.<ts>.partial during
    transfer; the off-host script must NOT pick those — they're
    half-written."""
    if shutil.which("rsync") is None:
        pytest.skip("rsync not installed")

    backup_dir = tmp_path / "src"
    backup_dir.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    heartbeat = tmp_path / "hb"

    real = _make_bak(
        backup_dir / "scout.db.bak.20260524T030000Z",
        age_seconds=7200,
        payload=b"REAL",
    )
    # Even though .partial is newer (lower age), it must be skipped.
    _make_bak(
        backup_dir / "scout.db.bak.20260525T030000Z.partial",
        age_seconds=1800,
        payload=b"PARTIAL",
    )

    proc = _run(
        {
            "GECKO_OFFHOST_BACKUP_DEST": str(dest) + "/",
            "GECKO_BACKUP_DIR": str(backup_dir),
            "GECKO_OFFHOST_BACKUP_HEARTBEAT_FILE": str(heartbeat),
            "GECKO_OFFHOST_BACKUP_LOCK_FILE": str(tmp_path / "lock"),
        }
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert (dest / real.name).exists()
    # The .partial must NOT have been shipped
    partials = list(dest.glob("*.partial"))
    assert partials == [], f"shipped a .partial sentinel: {partials}"


def test_offhost_lock_serializes_concurrent_runs(tmp_path):
    """Second invocation while a hung first run holds the lock → exit 3."""
    if shutil.which("flock") is None:
        pytest.skip("flock not available")

    backup_dir = tmp_path / "src"
    backup_dir.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    lock = tmp_path / "lock"
    _make_bak(backup_dir / "scout.db.bak.20260525T030000Z", age_seconds=3600)

    # Open the lock from a parallel process and hold it.
    holder = subprocess.Popen(
        ["bash", "-c", f"exec 9>{lock}; flock -x 9; sleep 5"],
    )
    try:
        # Tiny pause to let the holder acquire.
        time.sleep(0.3)
        proc = _run(
            {
                "GECKO_OFFHOST_BACKUP_DEST": str(dest) + "/",
                "GECKO_BACKUP_DIR": str(backup_dir),
                "GECKO_OFFHOST_BACKUP_HEARTBEAT_FILE": str(tmp_path / "hb"),
                "GECKO_OFFHOST_BACKUP_LOCK_FILE": str(lock),
            }
        )
        assert proc.returncode == 3, f"stderr: {proc.stderr}"
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_offhost_rsync_binary_missing_exit_6(tmp_path):
    backup_dir = tmp_path / "src"
    backup_dir.mkdir()
    _make_bak(backup_dir / "scout.db.bak.20260525T030000Z", age_seconds=3600)

    proc = _run(
        {
            "GECKO_OFFHOST_BACKUP_DEST": str(tmp_path / "dest") + "/",
            "GECKO_BACKUP_DIR": str(backup_dir),
            "GECKO_OFFHOST_BACKUP_HEARTBEAT_FILE": str(tmp_path / "hb"),
            "GECKO_OFFHOST_BACKUP_LOCK_FILE": str(tmp_path / "lock"),
            "GECKO_OFFHOST_BACKUP_BIN": "rsync-does-not-exist-on-purpose",
        }
    )
    assert proc.returncode == 6, f"stderr: {proc.stderr}"
