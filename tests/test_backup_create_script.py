"""Tests for scripts/gecko-backup-create.sh (Round 11).

Mirrors the existing tests/test_backup_rotate_script.py pattern — each
test creates an isolated tmp_path, invokes the bash script via subprocess
with environment overrides, and asserts on exit code + filesystem side
effects.

Skipped on Windows: bash + flock semantics are Linux-specific.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash + flock + sqlite3 .backup semantics are Linux-specific",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "gecko-backup-create.sh"


def _make_seed_db(path: Path, row_count: int = 10) -> None:
    """Create a small valid SQLite database at path."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    for i in range(row_count):
        conn.execute("INSERT INTO t (val) VALUES (?)", (f"row-{i}",))
    conn.commit()
    conn.close()


def _run(env_overrides=None):
    env = os.environ.copy()
    env.update(env_overrides or {})
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------


def test_create_writes_dated_bak_and_heartbeat(tmp_path):
    """Creates a .bak.<ts> file and writes the heartbeat."""
    db = tmp_path / "scout.db"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    heartbeat = tmp_path / "heartbeat"
    lock = tmp_path / "lock"
    _make_seed_db(db)

    proc = _run(
        {
            "GECKO_DB_PATH": str(db),
            "GECKO_BACKUP_DIR": str(backup_dir),
            "GECKO_BACKUP_CREATE_HEARTBEAT_FILE": str(heartbeat),
            "GECKO_BACKUP_CREATE_LOCK_FILE": str(lock),
        }
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    backups = sorted(backup_dir.glob("scout.db.bak.*"))
    assert len(backups) == 1, f"expected 1 backup, found {len(backups)}"
    assert backups[0].stat().st_size > 0
    assert heartbeat.exists()
    # heartbeat is a unix timestamp
    ts = int(heartbeat.read_text().strip())
    assert abs(ts - int(time.time())) < 10


def test_create_produces_readable_sqlite(tmp_path):
    """The created backup file must be queryable as a SQLite DB."""
    db = tmp_path / "scout.db"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    _make_seed_db(db, row_count=42)

    proc = _run(
        {
            "GECKO_DB_PATH": str(db),
            "GECKO_BACKUP_DIR": str(backup_dir),
            "GECKO_BACKUP_CREATE_HEARTBEAT_FILE": str(tmp_path / "hb"),
            "GECKO_BACKUP_CREATE_LOCK_FILE": str(tmp_path / "lock"),
        }
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    bak = next(backup_dir.glob("scout.db.bak.*"))
    conn = sqlite3.connect(bak)
    count = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    conn.close()
    assert count == 42, "backup row count must match source"


# ---------------------------------------------------------------------
# Misconfiguration paths — exit 2
# ---------------------------------------------------------------------


def test_create_exits_2_when_db_missing(tmp_path):
    proc = _run(
        {
            "GECKO_DB_PATH": str(tmp_path / "nonexistent.db"),
            "GECKO_BACKUP_DIR": str(tmp_path),
            "GECKO_BACKUP_CREATE_HEARTBEAT_FILE": str(tmp_path / "hb"),
            "GECKO_BACKUP_CREATE_LOCK_FILE": str(tmp_path / "lock"),
        }
    )
    assert proc.returncode == 2
    assert "not a regular file" in proc.stderr


def test_create_exits_2_when_backup_dir_missing(tmp_path):
    db = tmp_path / "scout.db"
    _make_seed_db(db)
    proc = _run(
        {
            "GECKO_DB_PATH": str(db),
            "GECKO_BACKUP_DIR": str(tmp_path / "no-such-dir"),
            "GECKO_BACKUP_CREATE_HEARTBEAT_FILE": str(tmp_path / "hb"),
            "GECKO_BACKUP_CREATE_LOCK_FILE": str(tmp_path / "lock"),
        }
    )
    assert proc.returncode == 2
    assert "not a directory" in proc.stderr


def test_create_exits_2_when_sqlite3_missing(tmp_path):
    db = tmp_path / "scout.db"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    _make_seed_db(db)
    proc = _run(
        {
            "GECKO_DB_PATH": str(db),
            "GECKO_BACKUP_DIR": str(backup_dir),
            "GECKO_BACKUP_CREATE_HEARTBEAT_FILE": str(tmp_path / "hb"),
            "GECKO_BACKUP_CREATE_LOCK_FILE": str(tmp_path / "lock"),
            "GECKO_BACKUP_SQLITE_BIN": "/nonexistent/sqlite3",
        }
    )
    assert proc.returncode == 2
    assert "sqlite3 binary not found" in proc.stderr


# ---------------------------------------------------------------------
# Lock contention — exit 3
# ---------------------------------------------------------------------


def test_create_exits_3_when_lock_held(tmp_path):
    db = tmp_path / "scout.db"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    _make_seed_db(db)
    lock = tmp_path / "lock"
    lock.touch()

    # Hold lock in a separate flock subprocess, then run the script.
    holder = subprocess.Popen(
        ["flock", "-n", str(lock), "sleep", "5"],
    )
    time.sleep(0.5)  # ensure holder grabbed first
    try:
        proc = _run(
            {
                "GECKO_DB_PATH": str(db),
                "GECKO_BACKUP_DIR": str(backup_dir),
                "GECKO_BACKUP_CREATE_HEARTBEAT_FILE": str(tmp_path / "hb"),
                "GECKO_BACKUP_CREATE_LOCK_FILE": str(lock),
            }
        )
        assert proc.returncode == 3
        assert "another invocation holds" in proc.stderr
    finally:
        holder.kill()
        holder.wait()


# ---------------------------------------------------------------------
# Integrity check failure — exit 5 (with a stub sqlite3)
# ---------------------------------------------------------------------


def test_create_exits_5_on_integrity_failure(tmp_path):
    """Stub sqlite3 that returns 'malformed' instead of 'ok' on
    PRAGMA integrity_check triggers exit 5."""
    db = tmp_path / "scout.db"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    _make_seed_db(db)

    # Stub sqlite3 binary that handles two invocations:
    #   1. ".backup ..." → succeed (write a file at the .partial path)
    #   2. "PRAGMA integrity_check;" → print "***corruption***"
    stub = tmp_path / "sqlite3-stub"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$2" == .backup* ]]; then\n'
        '  # extract dest from `.backup \'path\'`\n'
        '  dest=$(echo "$2" | sed "s/^.backup '"'"'\\(.*\\)'"'"'$/\\1/")\n'
        '  echo "stub-backup-data" > "$dest"\n'
        "  exit 0\n"
        'elif [[ "$2" == "PRAGMA integrity_check;" ]]; then\n'
        '  echo "***corruption detected by stub***"\n'
        "  exit 0\n"
        "fi\n"
        "exit 99\n"
    )
    stub.chmod(0o755)

    proc = _run(
        {
            "GECKO_DB_PATH": str(db),
            "GECKO_BACKUP_DIR": str(backup_dir),
            "GECKO_BACKUP_CREATE_HEARTBEAT_FILE": str(tmp_path / "hb"),
            "GECKO_BACKUP_CREATE_LOCK_FILE": str(tmp_path / "lock"),
            "GECKO_BACKUP_SQLITE_BIN": str(stub),
        }
    )
    assert proc.returncode == 5
    assert "integrity check failed" in proc.stderr
    # The .partial file must have been cleaned up.
    partials = list(backup_dir.glob("*.partial"))
    assert partials == []
