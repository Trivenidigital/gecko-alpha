"""Tests for scripts/gecko-backup-rotate.sh + gecko-backup-watchdog.sh.

Test methodology: each test creates an isolated tmp_path with fake backup
files at staggered mtimes (via os.utime), then invokes the bash script via
subprocess with environment overrides.

Watchdog has two alert-delivery paths:
- UV_BIN set: invoke stub recorder (tested via _run_watchdog).
- UV_BIN unset: read $ENV_FILE for Telegram creds + curl direct (tested via
  _run_watchdog_real_path with ENV_FILE pointing at fixture-controlled paths).

Skipped on Windows: bash + flock + os.utime semantics differ. The scripts
are deployed to Linux only (Hetzner VPS via systemd).
"""

from __future__ import annotations

import os
import shlex
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
    now = time.time()
    for i, age_hours in enumerate([10, 20, 30, 40, 50]):
        _make_backup(
            tmp_path, f"scout.db.bak.tag{i}.{int(now)}", now - age_hours * 3600
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
        p.name for p in tmp_path.iterdir() if p.name not in {"hb", "lock"}
    )
    assert any("tag0" in n for n in surviving)
    assert any("tag1" in n for n in surviving)
    assert any("tag2" in n for n in surviving)
    assert not any("tag3" in n for n in surviving)
    assert not any("tag4" in n for n in surviving)
    assert len(surviving) == 3


def test_idempotent_rerun_on_trimmed_dir(tmp_path):
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
    surviving = [p for p in tmp_path.iterdir() if p.name not in {"hb", "lock"}]
    assert len(surviving) == 3


def test_empty_dir_is_noop(tmp_path):
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
    remaining = [p for p in tmp_path.iterdir() if p.name not in {"hb", "lock"}]
    assert len(remaining) == 0


def test_unified_sort_across_both_name_patterns(tmp_path):
    """R2 MUST-FIX regression-lock: both naming patterns participate in
    a SINGLE mtime sort. 8 alternating files, KEEP=3 → exactly 3 survive."""
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
    surviving = [p.name for p in tmp_path.iterdir() if p.name not in {"hb", "lock"}]
    assert len(surviving) == 3


# ----------------------------------------------------------------------
# Rotation script — error / abort paths
# ----------------------------------------------------------------------


def test_unset_dir_aborts(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "GECKO_BACKUP_DIR"}
    env["GECKO_BACKUP_HEARTBEAT_FILE"] = str(tmp_path / "hb")
    env["GECKO_BACKUP_LOCK_FILE"] = str(tmp_path / "lock")
    res = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert res.returncode != 0
    assert "GECKO_BACKUP_DIR" in res.stderr


def test_nonexistent_dir_aborts(tmp_path):
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
    """R3 MUST-FIX: heartbeat-write failure must surface (ENOTDIR — root cannot bypass)."""
    blocker_file = tmp_path / "blocker_file"
    blocker_file.write_text("not a directory")
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
    assert res.returncode != 0


def test_flock_concurrent_invocation_exits_3(tmp_path):
    """R3 MUST-FIX: flock guard against concurrent invocations."""
    hb = tmp_path / "hb"
    lock = tmp_path / "lock"
    _make_backup(tmp_path, "scout.db.bak.x", time.time())
    quoted_lock = shlex.quote(str(lock))
    holder = subprocess.Popen(
        ["bash", "-c", f"exec 9>{quoted_lock}; flock 9; sleep 5"],
    )
    try:
        time.sleep(0.5)
        res = _run(
            {
                "GECKO_BACKUP_DIR": str(tmp_path),
                "GECKO_BACKUP_KEEP": "3",
                "GECKO_BACKUP_HEARTBEAT_FILE": str(hb),
                "GECKO_BACKUP_LOCK_FILE": str(lock),
            }
        )
        assert res.returncode == 3
        assert not hb.exists()
    finally:
        holder.terminate()
        holder.wait()


# ----------------------------------------------------------------------
# Heartbeat + symlink + space safety
# ----------------------------------------------------------------------


def test_filename_with_space_preserved(tmp_path):
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
        p.name for p in tmp_path.iterdir() if p.name not in {"hb", "lock"}
    )
    assert "scout.db.bak.tag.normal" in surviving
    assert "scout.db.bak. extra-tag" in surviving
    assert "scout.db.bak.older" not in surviving


def test_heartbeat_written_on_success(tmp_path):
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
    after = time.time() + 1
    assert res.returncode == 0
    assert hb.exists()
    written = int(hb.read_text().strip())
    assert before - 1 <= written <= after


def test_heartbeat_NOT_written_on_failure(tmp_path):
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


def test_heartbeat_atomic_write_via_rename(tmp_path):
    """R6 MUST-FIX regression-lock: heartbeat must be written via .tmp + mv-f rename."""
    hb = tmp_path / "hb"
    _make_backup(tmp_path, "scout.db.bak.x", time.time())
    res = _run(
        {
            "GECKO_BACKUP_DIR": str(tmp_path),
            "GECKO_BACKUP_KEEP": "3",
            "GECKO_BACKUP_HEARTBEAT_FILE": str(hb),
            "GECKO_BACKUP_LOCK_FILE": str(tmp_path / "lock"),
        }
    )
    assert res.returncode == 0
    tmp_files = [p for p in tmp_path.iterdir() if p.name.startswith("hb.tmp.")]
    assert tmp_files == [], f"orphan tmp files: {tmp_files}"


def test_symlink_not_followed(tmp_path):
    real = tmp_path / "scout.db.bak.real"
    real.write_text("x")
    target_outside = tmp_path / "outside.db"
    target_outside.write_text("important")
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
    assert not real.exists()
    assert target_outside.exists()
    assert symlink.is_symlink()


# ----------------------------------------------------------------------
# Watchdog tests — UV_BIN stub path
# ----------------------------------------------------------------------


def _make_uv_stub(tmp_path: Path) -> tuple[Path, Path]:
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    stub = stub_dir / "uv"
    marker = tmp_path / "alert_marker"
    quoted_marker = shlex.quote(str(marker))
    stub.write_text(
        "#!/usr/bin/env bash\n" f'echo "uv called: $@" >> {quoted_marker}\n' "exit 0\n"
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
    assert marker.exists()


def test_watchdog_stale_heartbeat_alerts(tmp_path):
    hb = tmp_path / "hb"
    hb.write_text(str(int(time.time() - 49 * 3600)))
    res, marker = _run_watchdog(
        tmp_path,
        {
            "GECKO_BACKUP_HEARTBEAT_FILE": str(hb),
            "GECKO_REPO": str(tmp_path),
            "GECKO_BACKUP_STALE_AFTER_SEC": "172800",
        },
    )
    assert res.returncode == 1
    assert marker.exists()


def test_watchdog_fresh_heartbeat_ok(tmp_path):
    hb = tmp_path / "hb"
    hb.write_text(str(int(time.time() - 3600)))
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
    assert not marker.exists()


@pytest.mark.parametrize(
    "corrupt_content, label",
    [
        ("", "empty"),
        ("not-a-number", "non-numeric"),
        ("1234abc", "mixed"),
        ("\n", "newline-only"),
        ("   ", "whitespace-only"),
    ],
)
def test_watchdog_corrupt_heartbeat_alerts(tmp_path, corrupt_content, label):
    """R5 + R6 CRITICAL: corrupt heartbeat must NOT die in bash arithmetic."""
    hb = tmp_path / "hb"
    hb.write_text(corrupt_content)
    res, marker = _run_watchdog(
        tmp_path,
        {
            "GECKO_BACKUP_HEARTBEAT_FILE": str(hb),
            "GECKO_REPO": str(tmp_path),
            "GECKO_BACKUP_STALE_AFTER_SEC": "172800",
        },
    )
    assert (
        res.returncode == 1
    ), f"Corrupt heartbeat ({label!r}) should fire alert; got {res.returncode} stderr={res.stderr}"
    assert "CORRUPT" in res.stdout
    assert marker.exists()


# ----------------------------------------------------------------------
# Watchdog — real (non-stub) Telegram delivery path
# ----------------------------------------------------------------------


def _run_watchdog_real_path(tmp_path: Path, env_overrides):
    env = os.environ.copy()
    env.pop("UV_BIN", None)
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(WATCHDOG_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )


def test_watchdog_no_env_file_exits_4(tmp_path):
    res = _run_watchdog_real_path(
        tmp_path,
        {
            "GECKO_BACKUP_HEARTBEAT_FILE": str(tmp_path / "missing-hb"),
            "GECKO_REPO": str(tmp_path),
            "GECKO_ENV_FILE": str(tmp_path / "no-such-env"),
            "GECKO_BACKUP_STALE_AFTER_SEC": "172800",
        },
    )
    assert res.returncode == 4
    assert "env file" in res.stderr
    assert "alert NOT delivered" in res.stderr


def test_watchdog_placeholder_token_exits_5(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=placeholder\nTELEGRAM_CHAT_ID=12345\n")
    res = _run_watchdog_real_path(
        tmp_path,
        {
            "GECKO_BACKUP_HEARTBEAT_FILE": str(tmp_path / "missing-hb"),
            "GECKO_REPO": str(tmp_path),
            "GECKO_ENV_FILE": str(env_file),
            "GECKO_BACKUP_STALE_AFTER_SEC": "172800",
        },
    )
    assert res.returncode == 5
    assert "TELEGRAM_BOT_TOKEN" in res.stderr


def test_watchdog_placeholder_chat_id_exits_5(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=real-looking-token\nTELEGRAM_CHAT_ID=\n")
    res = _run_watchdog_real_path(
        tmp_path,
        {
            "GECKO_BACKUP_HEARTBEAT_FILE": str(tmp_path / "missing-hb"),
            "GECKO_REPO": str(tmp_path),
            "GECKO_ENV_FILE": str(env_file),
            "GECKO_BACKUP_STALE_AFTER_SEC": "172800",
        },
    )
    assert res.returncode == 5
    assert "TELEGRAM_CHAT_ID" in res.stderr


def test_watchdog_fresh_heartbeat_skips_alert_path(tmp_path):
    hb = tmp_path / "hb"
    hb.write_text(str(int(time.time() - 3600)))
    res = _run_watchdog_real_path(
        tmp_path,
        {
            "GECKO_BACKUP_HEARTBEAT_FILE": str(hb),
            "GECKO_REPO": str(tmp_path),
            "GECKO_ENV_FILE": str(tmp_path / "would-have-errored"),
            "GECKO_BACKUP_STALE_AFTER_SEC": "172800",
        },
    )
    assert res.returncode == 0
    assert "OK:" in res.stdout


def test_watchdog_path_with_apostrophe_does_not_break(tmp_path):
    """R5/R6 CRITICAL regression-lock: HEARTBEAT path with apostrophe."""
    weird_dir = tmp_path / "it's-broken"
    weird_dir.mkdir()
    weird_hb = weird_dir / "hb"
    res, marker = _run_watchdog(
        tmp_path,
        {
            "GECKO_BACKUP_HEARTBEAT_FILE": str(weird_hb),
            "GECKO_REPO": str(tmp_path),
            "GECKO_BACKUP_STALE_AFTER_SEC": "172800",
        },
    )
    assert res.returncode == 1
    assert marker.exists()
