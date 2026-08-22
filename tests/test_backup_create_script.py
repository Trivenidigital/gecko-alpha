"""Tests for scripts/gecko-backup-create.sh (Round 11).

Mirrors the existing tests/test_backup_rotate_script.py pattern — each
test creates an isolated tmp_path, invokes the bash script via subprocess
with environment overrides, and asserts on exit code + filesystem side
effects.

Skipped on Windows: bash + flock semantics are Linux-specific.
"""

from __future__ import annotations

import os
import shlex
import signal
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
        "  # extract dest from `.backup 'path'`\n"
        # Raw triple-quoted: `\(` and `\1` must reach sed literally. In a plain
        # string `"\1"` is chr(1), which silently made `dest` garbage — no
        # .partial was ever written and `assert partials == []` passed for the
        # wrong reason.
        r"""  dest=$(echo "$2" | sed "s/^.backup '\(.*\)'$/\1/")""" + "\n"
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


# ---------------------------------------------------------------------
# Sidecar cleanup — the artifacts that actually accumulated on prod
# ---------------------------------------------------------------------


def _sidecar_stub(path: Path, *, mode: str, witness: Path | None = None) -> Path:
    """A stub sqlite3 that writes the .partial AND its SQLite sidecars.

    `sqlite3 .backup` opens the destination as a database, so an interrupted or
    failed run can leave `-journal` / `-wal` / `-shm` companions beside it.
    Every prior test stubbed only the main file, which is exactly why five
    orphaned `*.partial-journal` files survived on prod undetected.

    mode="backup_fail"    -> create all four, then fail the .backup   (exit 4)
    mode="integrity_fail" -> create all four, report corruption       (exit 5)

    *witness*, when given, receives one line per artifact the stub actually
    created, written OUTSIDE the backup directory so cleanup cannot erase it.
    Without it, `leftovers == []` passes just as happily when the stub created
    nothing at all — which is precisely the vacuous-negative shape this file
    exists to eliminate. The witness turns "nothing left behind" into "these
    four existed, and then they were removed".
    """
    body = [
        "#!/usr/bin/env bash",
        'if [[ "$2" == .backup* ]]; then',
        r"""  dest=$(echo "$2" | sed "s/^.backup '\(.*\)'$/\1/")""",
        '  echo "stub-backup-data" > "$dest"',
        '  echo "j" > "$dest-journal"',
        '  echo "w" > "$dest-wal"',
        '  echo "s" > "$dest-shm"',
    ]
    if witness is not None:
        body += [
            f'  for a in "$dest" "$dest-journal" "$dest-wal" "$dest-shm"; do',
            f'    [[ -f "$a" ]] && echo "$a" >> {shlex.quote(str(witness))}',
            "  done",
        ]
    if mode == "backup_fail":
        body += ['  echo "stub: disk full" >&2', "  exit 1"]
    else:
        body += ["  exit 0"]
    body += [
        'elif [[ "$2" == "PRAGMA integrity_check;" ]]; then',
        (
            '  echo "***corruption detected by stub***"'
            if mode == "integrity_fail"
            else '  echo "ok"'
        ),
        "  exit 0",
        "fi",
        "exit 99",
    ]
    path.write_text("\n".join(body) + "\n")
    path.chmod(0o755)
    return path


@pytest.mark.parametrize(
    "mode,expected_rc", [("backup_fail", 4), ("integrity_fail", 5)]
)
def test_failure_removes_partial_AND_every_sidecar(tmp_path, mode, expected_rc):
    """*** THE PROD LEAK. ***

    Both failure paths removed only `$DEST_TMP`. The `-journal`, `-wal` and
    `-shm` companions were left behind permanently — nothing else ever cleaned
    them, and while the rotation matcher was a broad glob they counted toward
    KEEP and could evict a completed backup.

    The pre-existing regression asserted only `glob("*.partial") == []`, which
    passes with every sidecar still on disk. That is why five of them
    accumulated on prod across five consecutive failed runs before anyone
    noticed.
    """
    db = tmp_path / "scout.db"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    _make_seed_db(db)
    # Witness lives OUTSIDE backup_dir so cleanup cannot erase the evidence.
    witness = tmp_path / "created-artifacts.txt"
    stub = _sidecar_stub(tmp_path / "sqlite3-stub", mode=mode, witness=witness)

    proc = _run(
        {
            "GECKO_DB_PATH": str(db),
            "GECKO_BACKUP_DIR": str(backup_dir),
            "GECKO_BACKUP_CREATE_HEARTBEAT_FILE": str(tmp_path / "hb"),
            "GECKO_BACKUP_CREATE_LOCK_FILE": str(tmp_path / "lock"),
            "GECKO_BACKUP_SQLITE_BIN": str(stub),
        }
    )
    assert proc.returncode == expected_rc, proc.stderr

    # POSITIVE WITNESS FIRST. Without this, `leftovers == []` passes just as
    # happily when the stub created nothing — the vacuous negative this file
    # exists to eliminate. Assert existence AND count, not `if witness:`.
    assert witness.exists(), (
        "stub never ran or never created artifacts — the cleanup assertion "
        "below would pass for the wrong reason"
    )
    created = [ln for ln in witness.read_text().splitlines() if ln.strip()]
    assert len(created) == 4, f"expected all 4 artifacts created, got {created}"
    assert sorted(Path(c).name.split(".partial")[1] for c in created) == [
        "",
        "-journal",
        "-shm",
        "-wal",
    ], created

    # ...and only now is "nothing left behind" meaningful.
    leftovers = sorted(p.name for p in backup_dir.iterdir())
    assert leftovers == [], (
        f"failure path left artifacts behind: {leftovers}. Every one of these "
        "persists forever — no later run revisits a .partial-* name."
    )


def test_success_leaves_no_sidecars_beside_the_promoted_backup(tmp_path):
    """`mv` promotes only the main file; anything beside it would linger."""
    db = tmp_path / "scout.db"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    _make_seed_db(db)
    witness = tmp_path / "created-artifacts.txt"
    stub = _sidecar_stub(tmp_path / "sqlite3-stub", mode="ok", witness=witness)

    proc = _run(
        {
            "GECKO_DB_PATH": str(db),
            "GECKO_BACKUP_DIR": str(backup_dir),
            "GECKO_BACKUP_CREATE_HEARTBEAT_FILE": str(tmp_path / "hb"),
            "GECKO_BACKUP_CREATE_LOCK_FILE": str(tmp_path / "lock"),
            "GECKO_BACKUP_SQLITE_BIN": str(stub),
        }
    )
    assert proc.returncode == 0, proc.stderr

    assert witness.exists(), "stub never created the sidecars it was asked to"
    assert len([ln for ln in witness.read_text().splitlines() if ln.strip()]) == 4

    names = sorted(p.name for p in backup_dir.iterdir())
    assert len(names) == 1, f"expected exactly the promoted backup, got {names}"
    assert names[0].startswith("scout.db.bak."), names
    assert ".partial" not in names[0], names


# ---------------------------------------------------------------------
# Signal termination — the systemd TimeoutStartSec path
# ---------------------------------------------------------------------


def test_sigterm_removes_partial_and_every_sidecar(tmp_path):
    """*** THE 2026-08-08 TIMEOUT ORPHAN, LOCKED DOWN. ***

    systemd sends SIGTERM when TimeoutStartSec expires. Before the trap, the
    shell died with the partial and all sidecars on disk: a 6.8 GB orphan that
    no later run ever revisits.

    Signals the PROCESS GROUP, not just the direct child — that is the shape
    systemd uses when terminating a unit's control group, and a trap that only
    survives a direct `kill <pid>` would not prove the real case.

    Not a `grep for trap` test: it runs the real script against a stub that
    creates all four artifacts and then blocks, so the artifacts genuinely exist
    at signal time.
    """
    db = tmp_path / "scout.db"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    _make_seed_db(db)
    witness = tmp_path / "created-artifacts.txt"

    # Stub: create all four artifacts, record them, then block forever.
    stub = tmp_path / "sqlite3-stub"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$2" == .backup* ]]; then\n'
        r"""  dest=$(echo "$2" | sed "s/^.backup '\(.*\)'$/\1/")""" + "\n"
        '  echo d > "$dest"\n'
        '  echo j > "$dest-journal"\n'
        '  echo w > "$dest-wal"\n'
        '  echo s > "$dest-shm"\n'
        '  for a in "$dest" "$dest-journal" "$dest-wal" "$dest-shm"; do\n'
        f'    [[ -f "$a" ]] && echo "$a" >> {shlex.quote(str(witness))}\n'
        "  done\n"
        "  sleep 300\n"  # block, as a real .backup on a large DB would
        "fi\n"
        "exit 99\n"
    )
    stub.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "GECKO_DB_PATH": str(db),
            "GECKO_BACKUP_DIR": str(backup_dir),
            "GECKO_BACKUP_CREATE_HEARTBEAT_FILE": str(tmp_path / "hb"),
            "GECKO_BACKUP_CREATE_LOCK_FILE": str(tmp_path / "lock"),
            "GECKO_BACKUP_SQLITE_BIN": str(stub),
        }
    )
    proc = subprocess.Popen(
        ["bash", str(SCRIPT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,  # own process group, like a unit's cgroup
    )
    try:
        # Wait until the artifacts genuinely exist, so the signal lands during
        # the backup rather than before it started.
        deadline = time.time() + 30
        while time.time() < deadline:
            if witness.exists() and len(witness.read_text().split()) >= 4:
                break
            time.sleep(0.2)
        assert witness.exists(), "stub never created artifacts — nothing to clean"
        created = [ln for ln in witness.read_text().splitlines() if ln.strip()]
        assert len(created) == 4, created

        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        _, stderr = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=10)

    assert proc.returncode != 0, "a signalled backup must not report success"
    leftovers = sorted(p.name for p in backup_dir.iterdir())
    assert leftovers == [], (
        f"SIGTERM left artifacts behind: {leftovers} — this is exactly the "
        "6.8 GB orphan from 2026-08-08"
    )
    assert not (tmp_path / "hb").exists(), (
        "heartbeat must NOT advance on a signalled run — a moved heartbeat "
        "would tell the watchdog a backup succeeded"
    )
    assert "SIGTERM" in stderr or "removing partial" in stderr, stderr


def test_sigint_and_sighup_also_clean_up(tmp_path):
    """INT/HUP take the same path — an operator Ctrl-C or a closed session
    must not orphan a multi-GB file either."""
    for sig in (signal.SIGINT, signal.SIGHUP):
        db = tmp_path / f"scout-{sig}.db"
        backup_dir = tmp_path / f"backups-{sig}"
        backup_dir.mkdir()
        _make_seed_db(db)
        witness = tmp_path / f"w-{sig}.txt"

        stub = tmp_path / f"stub-{sig}"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$2" == .backup* ]]; then\n'
            r"""  dest=$(echo "$2" | sed "s/^.backup '\(.*\)'$/\1/")""" + "\n"
            '  echo d > "$dest"; echo j > "$dest-journal"\n'
            '  echo w > "$dest-wal"; echo s > "$dest-shm"\n'
            f'  echo "$dest" >> {shlex.quote(str(witness))}\n'
            "  sleep 300\n"
            "fi\n"
            "exit 99\n"
        )
        stub.chmod(0o755)

        env = os.environ.copy()
        env.update(
            {
                "GECKO_DB_PATH": str(db),
                "GECKO_BACKUP_DIR": str(backup_dir),
                "GECKO_BACKUP_CREATE_HEARTBEAT_FILE": str(tmp_path / f"hb-{sig}"),
                "GECKO_BACKUP_CREATE_LOCK_FILE": str(tmp_path / f"lock-{sig}"),
                "GECKO_BACKUP_SQLITE_BIN": str(stub),
            }
        )
        proc = subprocess.Popen(
            ["bash", str(SCRIPT)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            deadline = time.time() + 30
            while time.time() < deadline and not witness.exists():
                time.sleep(0.2)
            assert witness.exists(), f"{sig}: stub never ran"
            os.killpg(os.getpgid(proc.pid), sig)
            proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait(timeout=10)

        assert (
            sorted(p.name for p in backup_dir.iterdir()) == []
        ), f"{sig.name} left artifacts behind"


# ---------------------------------------------------------------------
# Unit-file contract — the timeout that produced the orphan
# ---------------------------------------------------------------------


def test_unit_timeout_is_pinned_at_30_minutes():
    """*** THE VALUE THAT CAUSED THE INCIDENT. ***

    `TimeoutStartSec=600` expired mid-`.backup` on a 6.82 GB database on
    2026-08-08, and systemd's SIGTERM orphaned a 6.8 GB `.partial`.

    Pinned rather than merely raised: this number is easy to lower "back to
    something reasonable" by someone who has not seen the failure, and the cost
    of getting it wrong is a multi-GB orphan plus a missed backup, not a slow
    job. The trap now cleans up on TERM, so a future expiry is survivable — but
    the backup still would not exist.
    """
    unit = (REPO_ROOT / "systemd" / "gecko-backup.service").read_text("utf-8")
    values = [
        ln.split("=", 1)[1].strip()
        for ln in unit.splitlines()
        if ln.strip().startswith("TimeoutStartSec=")
    ]
    assert values == [
        "1800"
    ], f"expected exactly one TimeoutStartSec=1800, got {values}"
    # The stale RATIONALE must be gone, not every mention of the old number:
    # the replacement comment cites 600s when explaining the incident, which is
    # exactly the context a future reader needs.
    assert (
        "4× headroom" not in unit and "4x headroom" not in unit
    ), "the superseded sizing rationale must not survive alongside the new value"
    assert "~2-3 minutes" not in unit, "stale duration estimate still present"


def test_create_script_traps_termination_signals():
    """Static companion to the behavioural SIGTERM test.

    The behavioural test proves cleanup happens; this proves the trap is
    installed AFTER `_cleanup_partial` and `DEST_TMP` exist. A trap registered
    before them would reference an unset variable and silently clean nothing —
    a failure the behavioural test could not distinguish from success if the
    stub happened to create no artifacts.
    """
    src = (REPO_ROOT / "scripts" / "gecko-backup-create.sh").read_text("utf-8")
    i_tmp = src.index("DEST_TMP=")
    i_fn = src.index("_cleanup_partial() {")
    for sig in ("TERM", "INT", "HUP"):
        marker = f"trap '_on_signal {sig}' {sig}"
        assert marker in src, f"missing trap for SIG{sig}"
        assert src.index(marker) > i_fn > i_tmp, (
            f"SIG{sig} trap must be installed after DEST_TMP and "
            "_cleanup_partial are defined"
        )
    # No `trap ... KILL` REGISTRATION. Asserted against trap lines only, not
    # prose: the script legitimately explains that SIGKILL is uncatchable and
    # out of scope, so grepping the file for "KILL" would fail on its own
    # correct disclaimer.
    trap_lines = [ln for ln in src.splitlines() if ln.strip().startswith("trap ")]
    assert trap_lines, "no traps registered at all"
    assert not any(
        "KILL" in ln for ln in trap_lines
    ), f"SIGKILL is uncatchable and must not be registered: {trap_lines}"


def test_integrity_check_opens_the_backup_immutable(tmp_path):
    """*** THE 2026-08-15 PROD INCIDENT, READER SIDE. ***

    The backup inherits the source's WAL-mode header, so ANY ordinary open —
    `mode=ro` included — makes SQLite create `-shm` and `-wal` beside it.
    Verified against sqlite 3.50.4: a `mode=ro` open of a freshly-`.backup`-ed
    WAL-header database produces both sidecars; the same open with
    `immutable=1` produces none and still returns `ok`.

    On prod that mechanism cost all three backups: sidecars with fresh mtimes
    took every mtime-descending retention slot. Rotation now excludes those
    suffixes, but not creating them is the durable half of the fix.

    Asserted on the ARGUMENT the script hands sqlite3, captured by a stub, so
    the test pins the contract rather than the absence of files on a platform
    whose sqlite may behave differently.
    """
    db = tmp_path / "scout.db"
    db.write_bytes(b"x" * 64)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    argfile = tmp_path / "args"

    stub = tmp_path / "sqlite3-stub"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\n" "$1" >> "$ARGFILE"\n'
        'if [[ "$2" == .backup* ]]; then\n'
        r"""  dest=$(echo "$2" | sed "s/^.backup '\(.*\)'$/\1/")""" + "\n"
        '  echo "stub-backup-data" > "$dest"\n'
        "  exit 0\n"
        'elif [[ "$2" == "PRAGMA integrity_check;" ]]; then\n'
        '  echo "ok"\n'
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
            "ARGFILE": str(argfile),
        }
    )
    assert proc.returncode == 0, proc.stderr

    args = argfile.read_text().splitlines()
    # Second invocation is the integrity check; the first is `.backup`.
    assert len(args) == 2, args
    verify_arg = args[1]
    assert verify_arg.startswith("file:"), (
        f"the integrity check opened a bare path, which creates sidecars on a "
        f"WAL-header backup: {verify_arg}"
    )
    assert "immutable=1" in verify_arg, (
        f"`mode=ro` alone still creates -shm/-wal; immutable=1 is what "
        f"prevents them: {verify_arg}"
    )


# ---------------------------------------------------------------------
# Pre-flight free-space guard (2026-08-22)
# ---------------------------------------------------------------------
#
# This lane's origin PR (#87) is titled "closes recurring 100%-disk incident",
# yet nothing checked free space before writing a file the size of the live
# database. On srilu 2026-08-22 the peak free space DURING the nightly create
# was 973 MB against a 7,012 MB database — it fit, but nothing was watching and
# nothing would have said so. A full volume is worse than a missing backup,
# because the LIVE database shares it.


def test_refuses_when_free_space_is_below_db_size_plus_margin(tmp_path):
    """THE guard: refuse rather than fill the volume.

    Margin is forced absurdly high so the requirement exceeds real free space
    without needing to actually fill a disk — the arithmetic under test is
    `free < db_size + margin`, and that is exercised either way.
    """
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
            # Require an impossible amount of headroom.
            "GECKO_BACKUP_MIN_FREE_MARGIN_MB": str(1024 * 1024 * 64),  # 64 TB
        }
    )
    assert proc.returncode == 6, (
        f"expected the distinct preflight exit code 6; got {proc.returncode}: "
        f"{proc.stderr}"
    )
    assert "insufficient free space" in proc.stderr.lower()


def test_refusal_writes_nothing_and_touches_no_existing_backup(tmp_path):
    """A refusal must be inert.

    create-then-rotate ordering is deliberately NOT inverted: rotating first
    would halve peak usage but deletes a known-good backup before its
    replacement is proven, trading a disk risk for a data risk. So the refusal
    path must leave every existing backup exactly as it found it.
    """
    db = tmp_path / "scout.db"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    _make_seed_db(db)

    existing = backup_dir / "scout.db.bak.20260101T000000Z"
    existing.write_bytes(b"pre-existing backup")
    before = existing.read_bytes()
    heartbeat = tmp_path / "hb"

    proc = _run(
        {
            "GECKO_DB_PATH": str(db),
            "GECKO_BACKUP_DIR": str(backup_dir),
            "GECKO_BACKUP_CREATE_HEARTBEAT_FILE": str(heartbeat),
            "GECKO_BACKUP_CREATE_LOCK_FILE": str(tmp_path / "lock"),
            "GECKO_BACKUP_MIN_FREE_MARGIN_MB": str(1024 * 1024 * 64),
        }
    )
    assert proc.returncode == 6
    assert existing.read_bytes() == before, "an existing backup was modified"
    assert list(backup_dir.iterdir()) == [existing], (
        "the refusal path must create no files at all, not even a .partial"
    )
    assert not heartbeat.exists(), (
        "a refused run must NOT write the create heartbeat — a fresh heartbeat "
        "would tell the freshness watchdog a backup succeeded"
    )


def test_sufficient_space_still_creates_normally(tmp_path):
    """The guard must not block ordinary operation.

    Without this, a guard that always refused would pass the test above and
    silently end nightly backups.
    """
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
            "GECKO_BACKUP_MIN_FREE_MARGIN_MB": "1",
        }
    )
    assert proc.returncode == 0, proc.stderr
    created = list(backup_dir.glob("scout.db.bak.*"))
    assert len(created) == 1
    assert not str(created[0]).endswith(".partial")


def test_preflight_reports_its_arithmetic(tmp_path):
    """The decision must be auditable from the journal, not inferred.

    "No backup last night" and "backup refused for space" look identical in a
    directory listing; only the log separates them.
    """
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
            "GECKO_BACKUP_MIN_FREE_MARGIN_MB": "1",
        }
    )
    combined = proc.stdout + proc.stderr
    assert "preflight" in combined
    for token in ("db=", "free=", "need="):
        assert token in combined, f"preflight log missing {token}"


def test_thin_but_sufficient_space_warns_and_still_backs_up(tmp_path):
    """Degrade, do not cliff.

    A bare refuse-threshold set where the volume is already tight would have
    stopped the nightly backup on srilu (7,987 MB free, 7,012 MB database,
    973 MB real headroom) — trading a silent disk risk for a silent backup gap,
    which is no improvement. The warn band reports thin headroom while still
    taking the backup.
    """
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
            "GECKO_BACKUP_MIN_FREE_MARGIN_MB": "1",           # never refuse
            "GECKO_BACKUP_WARN_FREE_MARGIN_MB": str(1024 * 1024 * 64),  # always warn
        }
    )
    assert proc.returncode == 0, proc.stderr
    assert "thin" in proc.stderr.lower(), "the thin-headroom warning must fire"
    assert len(list(backup_dir.glob("scout.db.bak.*"))) == 1, (
        "a WARNING must not block the backup — that is the difference between "
        "early notice and an outage"
    )


def test_comfortable_space_does_not_warn(tmp_path):
    """No crying wolf: a healthy volume must stay quiet.

    A warning that fires every night is one an operator learns to ignore, which
    is how the alarm stops working before the disk does.
    """
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
            "GECKO_BACKUP_MIN_FREE_MARGIN_MB": "1",
            "GECKO_BACKUP_WARN_FREE_MARGIN_MB": "1",
        }
    )
    assert proc.returncode == 0, proc.stderr
    assert "thin" not in proc.stderr.lower()
