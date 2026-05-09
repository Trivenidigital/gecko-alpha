"""Tests for scripts/gecko-backup-rotate.sh + gecko-backup-watchdog.sh.

Test methodology: each test creates an isolated tmp_path with fake backup
files at staggered mtimes (via os.utime), then invokes the bash script via
subprocess with environment overrides. Asserts on file survival + heartbeat
state + exit code.

Watchdog tests use a stub `uv` script on $PATH (also overrides UV_BIN env)
to observe the Telegram-via-uv-run-python alert path without making a real
network call.

Skipped on Windows: bash + flock + os.utime semantics differ. The scripts
are deployed to Linux only (Hetzner VPS via systemd).
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
    reason="bash + flock + symlink + chmod 000 semantics are Linux-specific",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "gecko-backup-rotate.sh"
WATCHDOG_SCRIPT = REPO_ROOT / "scripts" / "gecko-backup-watchdog.sh"


def _make_backup(dir: Path, name: str, mtime: float, size: int = 100) -> Path:
    p = dir / name
    p.write_bytes(b"x" * size)
    os.utime(p, (mtime, mtime))
    return p


def _run(env_overrides=None, cwd=None):
    env = os.environ.copy()
    env.update(env_overrides or {})
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        cwd=cwd,
    )


# ----------------------------------------------------------------------
# Rotation script — happy path + boundaries
# ----------------------------------------------------------------------


def test_keeps_top_n_by_mtime(tmp_path):
    """5 backups, KEEP=3 → top-3-by-mtime survive."""
    now = time.time()
    for i, age_hours in enumerate([10, 20, 30, 40, 50]):
        _make_backup(
            tmp_path,
            f"scout.db.bak.tag{i}.{int(now)}",
            now - age_hours * 3600,
        )
    hb = tmp_path / "hb"
    res = _run(
        {
            "GECKO_BACKUP_DIR": str(tmp_path),
            "GECKO_BACKUP_KEEP": "3",
            "GECKO_BACKUP_HEARTBEAT_FILE": str(hb),
            "GECKO_BACKUP_LOCK_FILE": str(tmp_path / "lock"),
        }
    )
    assert res.returncode == 0, res.stderr
    surviving = sorted(
        p.name
        for p in tmp_path.iterdir()
        if p.name not in {"hb", "lock"}
    )
    # Top-3 by recency: tag0 (10h), tag1 (20h), tag2 (30h) survive.
    assert any("tag0" in n for n in surviving)
    assert any("tag1" in n for n in surviving)
    assert any("tag2" in n for n in surviving)
    assert not any("tag3" in n for n in surviving)
    assert not any("tag4" in n for n in surviving)
    assert len(surviving) == 3


def test_idempotent_rerun_on_trimmed_dir(tmp_path):
    """Re-running on a 3-file dir with KEEP=3 is a no-op."""
    now = time.time()
    for i in range(3):
        _make_backup(tmp_path, f"scout.db.bak.tag{i}.x", now - i * 3600)
    hb = tmp_path / "hb"
    env = {
        "GECKO_BACKUP_DIR": str(tmp_path),
        "GECKO_BACKUP_KEEP": "3",
        "GECKO_BACKUP_HEARTBEAT_FILE": str(hb),
        "GECKO_BACKUP_LOCK_FILE": str(tmp_path / "lock"),
    }
    _run(env)
    res2 = _run(env)
    assert res2.returncode == 0
    surviving = [
        p for p in tmp_path.iterdir() if p.name not in {"hb", "lock"}
    ]
    assert len(surviving) == 3


def test_empty_dir_is_noop(tmp_path):
    """Empty backup dir — exit 0, no error, heartbeat written."""
    hb = tmp_path / "hb"
    res = _run(
        {
            "GECKO_BACKUP_DIR": str(tmp_path),
            "GECKO_BACKUP_KEEP": "3",
            "GECKO_BACKUP_HEARTBEAT_FILE": str(hb),
            "GECKO_BACKUP_LOCK_FILE": str(tmp_path / "lock"),
        }
    )
    assert res.returncode == 0
    assert "no rotation needed" in res.stdout
    assert hb.exists()


def test_keep_zero_deletes_everything(tmp_path):
    """KEEP=0 operator escape hatch — all backups deleted."""
    now = time.time()
    for i in range(3):
        _make_backup(tmp_path, f"scout.db.bak.tag{i}", now - i * 3600)
    hb = tmp_path / "hb"
    res = _run(
        {
            "GECKO_BACKUP_DIR": str(tmp_path),
            "GECKO_BACKUP_KEEP": "0",
            "GECKO_BACKUP_HEARTBEAT_FILE": str(hb),
            "GECKO_BACKUP_LOCK_FILE": str(tmp_path / "lock"),
        }
    )
    assert res.returncode == 0
    remaining = [
        p
        for p in tmp_path.iterdir()
        if p.name not in {"hb", "lock"}
    ]
    assert len(remaining) == 0


def test_unified_sort_across_both_name_patterns(tmp_path):
    """R2 MUST-FIX regression-lock: both naming patterns participate in
    a SINGLE mtime sort. 8 alternating files, KEEP=3 → exactly 3 survive
    (NOT 6 = 3-from-each-bucket)."""
    now = time.time()
    for i, hours_ago in enumerate([1, 2, 3, 4, 5, 6, 7, 8]):
        if i % 2 == 0:
            name = f"scout.db.bak.tag{i}.{int(now)}"
        else:
            name = f"scout.db.bak-2026010{i}T000000Z"
        _make_backup(tmp_path, name, now - hours_ago * 3600)
    hb = tmp_path / "hb"
    res = _run(
        {
            "GECKO_BACKUP_DIR": str(tmp_path),
            "GECKO_BACKUP_KEEP": "3",
            "GECKO_BACKUP_HEARTBEAT_FILE": str(hb),
            "GECKO_BACKUP_LOCK_FILE": str(tmp_path / "lock"),
        }
    )
    assert res.returncode == 0
    surviving = [
        p.name
        for p in tmp_path.iterdir()
        if p.name not in {"hb", "lock"}
    ]
    assert len(surviving) == 3, (
        f"Expected exactly 3 survivors (single unified sort); got "
        f"{len(surviving)}: {surviving}"
    )


# ----------------------------------------------------------------------
# Rotation script — error / abort paths
# ----------------------------------------------------------------------


def test_unset_dir_aborts(tmp_path):
    """R1 MUST-FIX: GECKO_BACKUP_DIR unset → script exits non-zero."""
    env = {k: v for k, v in os.environ.items() if k != "GECKO_BACKUP_DIR"}
    env["GECKO_BACKUP_HEARTBEAT_FILE"] = str(tmp_path / "hb")
    env["GECKO_BACKUP_LOCK_FILE"] = str(tmp_path / "lock")
    res = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert "GECKO_BACKUP_DIR" in res.stderr


def test_nonexistent_dir_aborts(tmp_path):
    """R1 MUST-FIX: GECKO_BACKUP_DIR set to nonexistent path → exit 2."""
    nonexistent = tmp_path / "does-not-exist"
    res = _run(
        {
            "GECKO_BACKUP_DIR": str(nonexistent),
            "GECKO_BACKUP_KEEP": "3",
            "GECKO_BACKUP_HEARTBEAT_FILE": str(tmp_path / "hb"),
            "GECKO_BACKUP_LOCK_FILE": str(tmp_path / "lock"),
        }
    )
    assert res.returncode == 2
    assert "is not a directory" in res.stderr


def test_negative_keep_aborts(tmp_path):
    """R3 NIT: GECKO_BACKUP_KEEP=-1 is rejected by `^[0-9]+$` regex → exit 2."""
    hb = tmp_path / "hb"
    _make_backup(tmp_path, "scout.db.bak.x", time.time())
    res = _run(
        {
            "GECKO_BACKUP_DIR": str(tmp_path),
            "GECKO_BACKUP_KEEP": "-1",
            "GECKO_BACKUP_HEARTBEAT_FILE": str(hb),
            "GECKO_BACKUP_LOCK_FILE": str(tmp_path / "lock"),
        }
    )
    assert res.returncode == 2
    assert "must be a non-negative integer" in res.stderr


def test_unwritable_heartbeat_path_aborts(tmp_path):
    """R3 MUST-FIX: heartbeat-write failure must surface, not silently succeed.

    Original draft used `chmod 000` on a parent dir, but root bypasses DAC
    permissions on Linux — the test passed silently when run as root on the
    VPS during pre-PR verification. Switched to path-blocked-by-regular-file:
    the script's `mkdir -p $(dirname $HB)` fails with ENOTDIR ("Not a
    directory") because the parent path component is a file. Root cannot
    bypass this kernel-level error.
    """
    blocker_file = tmp_path / "blocker_file"
    blocker_file.write_text("not a directory")
    # Heartbeat would-be-parent is a regular file → mkdir -p fails ENOTDIR.
    hb = blocker_file / "hb"
    _make_backup(tmp_path, "scout.db.bak.x", time.time())
    res = _run(
        {
            "GECKO_BACKUP_DIR": str(tmp_path),
            "GECKO_BACKUP_KEEP": "3",
            "GECKO_BACKUP_HEARTBEAT_FILE": str(hb),
            "GECKO_BACKUP_LOCK_FILE": str(tmp_path / "lock"),
        }
    )
    assert res.returncode != 0, (
        f"Expected non-zero exit for unwritable heartbeat; "
        f"got {res.returncode}, stderr={res.stderr}"
    )


def test_flock_concurrent_invocation_exits_3(tmp_path):
    """R3 MUST-FIX: flock guard against concurrent invocations."""
    hb = tmp_path / "hb"
    lock = tmp_path / "lock"
    _make_backup(tmp_path, "scout.db.bak.x", time.time())

    # Hold the lock with a separate flock invocation that sleeps so the
    # file desc stays open during our test invocation.
    holder = subprocess.Popen(
        ["bash", "-c", f"exec 9>{lock}; flock 9; sleep 5"],
    )
    try:
        time.sleep(0.5)  # give holder time to take the lock
        res = _run(
            {
                "GECKO_BACKUP_DIR": str(tmp_path),
                "GECKO_BACKUP_KEEP": "3",
                "GECKO_BACKUP_HEARTBEAT_FILE": str(hb),
                "GECKO_BACKUP_LOCK_FILE": str(lock),
            }
        )
        assert res.returncode == 3, (
            f"Expected exit 3 for lock contention; got {res.returncode} "
            f"stderr={res.stderr}"
        )
        # Heartbeat must NOT be written when locked out.
        assert not hb.exists()
    finally:
        holder.terminate()
        holder.wait()


# ----------------------------------------------------------------------
# Rotation script — heartbeat + symlink + space safety
# ----------------------------------------------------------------------


def test_filename_with_space_preserved(tmp_path):
    """R2 NIT: pathological filename with embedded space — `cut` preserves
    the path correctly. Locks the `cut -d' ' -f2-` choice over `awk`."""
    now = time.time()
    _make_backup(tmp_path, "scout.db.bak.tag.normal", now - 1)
    _make_backup(tmp_path, "scout.db.bak. extra-tag", now - 2)
    _make_backup(tmp_path, "scout.db.bak.older", now - 100)
    hb = tmp_path / "hb"
    res = _run(
        {
            "GECKO_BACKUP_DIR": str(tmp_path),
            "GECKO_BACKUP_KEEP": "2",
            "GECKO_BACKUP_HEARTBEAT_FILE": str(hb),
            "GECKO_BACKUP_LOCK_FILE": str(tmp_path / "lock"),
        }
    )
    assert res.returncode == 0, res.stderr
    surviving = sorted(
        p.name
        for p in tmp_path.iterdir()
        if p.name not in {"hb", "lock"}
    )
    assert "scout.db.bak.tag.normal" in surviving
    assert "scout.db.bak. extra-tag" in surviving
    assert "scout.db.bak.older" not in surviving


def test_heartbeat_written_on_success(tmp_path):
    """Heartbeat file is written with current epoch on success.

    R3 MUST-FIX: float bounds with 1s tolerance for slow CI / clock-skew.
    """
    hb = tmp_path / "hb"
    _make_backup(tmp_path, "scout.db.bak.x", time.time())
    before = time.time()
    res = _run(
        {
            "GECKO_BACKUP_DIR": str(tmp_path),
            "GECKO_BACKUP_KEEP": "3",
            "GECKO_BACKUP_HEARTBEAT_FILE": str(hb),
            "GECKO_BACKUP_LOCK_FILE": str(tmp_path / "lock"),
        }
    )
    after = time.time() + 1  # 1s tolerance for slow CI / int truncation
    assert res.returncode == 0
    assert hb.exists()
    written = int(hb.read_text().strip())
    assert before - 1 <= written <= after, (
        f"heartbeat={written} not within [{before-1}, {after}]"
    )


def test_heartbeat_NOT_written_on_failure(tmp_path):
    """Heartbeat NOT updated when script fails (e.g., bad dir → exit 2)."""
    hb = tmp_path / "hb"
    res = _run(
        {
            "GECKO_BACKUP_DIR": str(tmp_path / "nope"),
            "GECKO_BACKUP_KEEP": "3",
            "GECKO_BACKUP_HEARTBEAT_FILE": str(hb),
            "GECKO_BACKUP_LOCK_FILE": str(tmp_path / "lock"),
        }
    )
    assert res.returncode != 0
    assert not hb.exists()


def test_symlink_not_followed(tmp_path):
    """Defensive: symlink matching the glob is NOT rotated (-type f filter)."""
    real = tmp_path / "scout.db.bak.real"
    real.write_text("x")
    target_outside = tmp_path / "outside.db"
    target_outside.write_text("important — should not be touched")
    symlink = tmp_path / "scout.db.bak.symlink"
    symlink.symlink_to(target_outside)
    hb = tmp_path / "hb"
    res = _run(
        {
            "GECKO_BACKUP_DIR": str(tmp_path),
            "GECKO_BACKUP_KEEP": "0",
            "GECKO_BACKUP_HEARTBEAT_FILE": str(hb),
            "GECKO_BACKUP_LOCK_FILE": str(tmp_path / "lock"),
        }
    )
    assert res.returncode == 0
    # The real file got deleted (matches -type f).
    assert not real.exists()
    # The symlink was NOT followed; target_outside survives.
    assert target_outside.exists()
    assert target_outside.read_text() == "important — should not be touched"
    # The symlink inode itself was NOT in the deletion list.
    assert symlink.is_symlink(), (
        "symlink should survive — find -type f must exclude symlinks"
    )


# ----------------------------------------------------------------------
# Watchdog tests — R3 CRITICAL gap closed.
# Stub `uv` to observe the Telegram-via-uv-run-python alert path without
# making a real network call.
# ----------------------------------------------------------------------


def _make_uv_stub(tmp_path: Path) -> tuple[Path, Path]:
    """Create a stub `uv` script that records its invocation to a marker."""
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    stub = stub_dir / "uv"
    marker = tmp_path / "alert_marker"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "uv called: $@" >> {marker}\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    return stub, marker


def _run_watchdog(tmp_path: Path, env_overrides):
    stub, marker = _make_uv_stub(tmp_path)
    env = os.environ.copy()
    env["PATH"] = f"{stub.parent}:" + env.get("PATH", "")
    env["UV_BIN"] = str(stub)
    env.update(env_overrides)
    res = subprocess.run(
        ["bash", str(WATCHDOG_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )
    return res, marker


def test_watchdog_missing_heartbeat_alerts(tmp_path):
    """R3 CRITICAL: heartbeat file missing → watchdog fires alert (exit 1)."""
    res, marker = _run_watchdog(
        tmp_path,
        {
            "GECKO_BACKUP_HEARTBEAT_FILE": str(tmp_path / "does-not-exist"),
            "GECKO_REPO": str(tmp_path),
            "GECKO_BACKUP_STALE_AFTER_SEC": "172800",
        },
    )
    assert res.returncode == 1, res.stderr
    assert "MISSING" in res.stdout or "MISSING" in res.stderr
    assert marker.exists(), "uv stub was not called — alert path skipped"


def test_watchdog_stale_heartbeat_alerts(tmp_path):
    """R3 CRITICAL: heartbeat older than threshold → watchdog fires alert."""
    hb = tmp_path / "hb"
    hb.write_text(str(int(time.time() - 49 * 3600)))  # 49h ago > 48h
    res, marker = _run_watchdog(
        tmp_path,
        {
            "GECKO_BACKUP_HEARTBEAT_FILE": str(hb),
            "GECKO_REPO": str(tmp_path),
            "GECKO_BACKUP_STALE_AFTER_SEC": "172800",
        },
    )
    assert res.returncode == 1
    assert marker.exists(), "stale-path alert was not delivered"


def test_watchdog_fresh_heartbeat_ok(tmp_path):
    """R3 CRITICAL: heartbeat fresh → exit 0, NO alert delivered."""
    hb = tmp_path / "hb"
    hb.write_text(str(int(time.time() - 3600)))  # 1h ago
    res, marker = _run_watchdog(
        tmp_path,
        {
            "GECKO_BACKUP_HEARTBEAT_FILE": str(hb),
            "GECKO_REPO": str(tmp_path),
            "GECKO_BACKUP_STALE_AFTER_SEC": "172800",
        },
    )
    assert res.returncode == 0
    assert "OK:" in res.stdout
    assert not marker.exists(), "uv stub was unexpectedly called on fresh path"
