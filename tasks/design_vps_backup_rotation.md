**New primitives introduced:** Same as `plan_vps_backup_rotation.md` — 2 bash scripts (`scripts/gecko-backup-rotate.sh`, `scripts/gecko-backup-watchdog.sh`), 4 systemd units (`gecko-backup.{service,timer}` + `gecko-backup-watchdog.{service,timer}`), heartbeat file `/var/lib/gecko-alpha/backup-last-ok`, runbook `docs/runbook_backup_rotation.md`. No code changes to `scout/` package. Tests in `tests/test_backup_rotate_script.py`.

# Design — VPS backup rotation

Plan: `plan_vps_backup_rotation.md` (R1 + R2 reviewer fixes folded).

## Hermes-first analysis

Inherited from plan. No skill match in 18 Hermes domains for SQLite backup rotation. Build from scratch using sister-project `shift-agent` systemd-timer architecture pattern (NOT borrowing the heavy GPG/S3/YAML script body).

## File-level layout

```
scripts/
  gecko-backup-rotate.sh        # primary rotation logic
  gecko-backup-watchdog.sh      # heartbeat-staleness alerter
systemd/
  gecko-backup.service
  gecko-backup.timer
  gecko-backup-watchdog.service
  gecko-backup-watchdog.timer
tests/
  test_backup_rotate_script.py  # pytest harness, ~10 cases
docs/
  runbook_backup_rotation.md    # install + ops + revert
```

## Concrete script bodies

### `scripts/gecko-backup-rotate.sh`

```bash
#!/usr/bin/env bash
# gecko-backup-rotate — keep top-N most-recent scout.db backups, delete rest.
#
# Required env:
#   GECKO_BACKUP_DIR    — absolute path containing scout.db.bak.* files
# Optional env:
#   GECKO_BACKUP_KEEP   — count to retain (default 3)
#   GECKO_BACKUP_HEARTBEAT_FILE — override path
#                                 (default /var/lib/gecko-alpha/backup-last-ok)
#   GECKO_BACKUP_LOCK_FILE       — flock guard path
#                                 (default /var/lock/gecko-backup-rotate.lock)
#
# Exit codes:
#   0 = success (including no-op on empty dir)
#   2 = misconfiguration (unset/missing dir, bad keep, bad heartbeat path)
#   3 = lock contention (another invocation in flight)
#   non-zero = unexpected failure (set -e propagation)
#
# Heartbeat file lives in /var/lib (persistent across reboots). DO NOT use
# /var/run — that's tmpfs on Ubuntu and clears at boot, which would cause
# the watchdog to fire a false-positive STALE alert after every reboot.

set -euo pipefail

: "${GECKO_BACKUP_DIR:?ERROR: GECKO_BACKUP_DIR must be set (no baked-in default)}"
KEEP="${GECKO_BACKUP_KEEP:-3}"
HEARTBEAT_FILE="${GECKO_BACKUP_HEARTBEAT_FILE:-/var/lib/gecko-alpha/backup-last-ok}"
LOCK_FILE="${GECKO_BACKUP_LOCK_FILE:-/var/lock/gecko-backup-rotate.lock}"

# R3 MUST-FIX flock guard — concurrent invocations (Persistent=true catch-up
# fire + manual `systemctl start`) would race and double-delete. Acquire an
# exclusive non-blocking lock; exit 3 cleanly on contention.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "gecko-backup-rotate: another invocation holds $LOCK_FILE; skipping" >&2
    exit 3
fi

if [[ ! -d "$GECKO_BACKUP_DIR" ]]; then
    echo "ERROR: GECKO_BACKUP_DIR=$GECKO_BACKUP_DIR is not a directory" >&2
    exit 2
fi

if ! [[ "$KEEP" =~ ^[0-9]+$ ]]; then
    echo "ERROR: GECKO_BACKUP_KEEP=$KEEP must be a non-negative integer" >&2
    exit 2
fi

# Ensure the heartbeat parent dir exists (idempotent — safe to mkdir each run).
# StateDirectory= in the systemd unit also ensures this on first start; the
# mkdir is for direct-CLI invocation paths.
mkdir -p "$(dirname "$HEARTBEAT_FILE")"

# Single unified find — both naming patterns participate in one mtime sort.
# -type f excludes symlinks (defensive). -maxdepth 1 anchors scope.
# -printf '%T@ %p\n' = epoch-seconds + space + path (handles space-in-filename
# when consumed by `cut -d' ' -f2-`).
mapfile -t files < <(
    find "$GECKO_BACKUP_DIR" -maxdepth 1 -type f \
        \( -name 'scout.db.bak.*' -o -name 'scout.db.bak-*' \) \
        -printf '%T@ %p\n' \
    | sort -rn \
    | cut -d' ' -f2-
)

total="${#files[@]}"
echo "gecko-backup-rotate: dir=$GECKO_BACKUP_DIR found=$total keep=$KEEP"

if (( total > KEEP )); then
    to_delete=("${files[@]:$KEEP}")
    rm -v -- "${to_delete[@]}"
    deleted="${#to_delete[@]}"
    echo "gecko-backup-rotate: deleted=$deleted retained=$KEEP"
else
    echo "gecko-backup-rotate: no rotation needed (total <= keep)"
fi

# Heartbeat last — only on full success path.
date +%s > "$HEARTBEAT_FILE"
echo "gecko-backup-rotate: heartbeat updated at $HEARTBEAT_FILE"
```

### `scripts/gecko-backup-watchdog.sh`

```bash
#!/usr/bin/env bash
# gecko-backup-watchdog — alert if rotation hasn't run successfully in 48h.
#
# Use absolute /root/.local/bin/uv (not bare `uv`) because systemd Type=oneshot
# units have a stripped PATH — verified against gecko-pipeline.service which
# also uses absolute uv. This is also the testability seam: the pytest harness
# overrides UV_BIN to a stub bash script for end-to-end watchdog tests.

set -euo pipefail

HEARTBEAT_FILE="${GECKO_BACKUP_HEARTBEAT_FILE:-/var/lib/gecko-alpha/backup-last-ok}"
STALE_AFTER_SEC="${GECKO_BACKUP_STALE_AFTER_SEC:-172800}"  # 48h
GECKO_REPO="${GECKO_REPO:-/root/gecko-alpha}"
UV_BIN="${UV_BIN:-/root/.local/bin/uv}"

now=$(date +%s)

if [[ ! -f "$HEARTBEAT_FILE" ]]; then
    age_msg="heartbeat file MISSING ($HEARTBEAT_FILE)"
    is_stale=1
else
    last_ok=$(cat "$HEARTBEAT_FILE")
    age_sec=$(( now - last_ok ))
    age_msg="last_ok=${age_sec}s ago"
    if (( age_sec > STALE_AFTER_SEC )); then
        is_stale=1
    else
        is_stale=0
    fi
fi

if (( is_stale == 1 )); then
    echo "STALE: gecko-backup-rotate has not run successfully — $age_msg"
    cd "$GECKO_REPO"
    # Use the existing project Telegram alerter so credentials + chat_id come
    # from .env and don't need duplicating into a sidecar.
    "$UV_BIN" run python -c "
import asyncio
from scout.alerter import send_telegram_message
from scout.config import Settings
async def go():
    s = Settings()
    await send_telegram_message(
        s,
        f'⚠️ gecko-backup-watchdog: rotation stale — $age_msg. '
        f'Check journalctl -u gecko-backup.service.'
    )
asyncio.run(go())
"
    exit 1
fi

echo "OK: gecko-backup-rotate ran within ${STALE_AFTER_SEC}s ($age_msg)"
```

## Concrete systemd units

### `systemd/gecko-backup.service`

```ini
[Unit]
Description=Gecko-Alpha — rotate scout.db backups, keep top-N most-recent
# R4 NIT: After=network.target dropped — script does no network I/O.

[Service]
Type=oneshot
User=root
Group=root
# StateDirectory=gecko-alpha → systemd creates /var/lib/gecko-alpha/ owned by
# the unit's User=. Persistent across reboots (R4 MUST-FIX).
StateDirectory=gecko-alpha
StateDirectoryMode=0750
Environment=GECKO_BACKUP_DIR=/root/gecko-alpha
Environment=GECKO_BACKUP_KEEP=3
Environment=GECKO_BACKUP_HEARTBEAT_FILE=/var/lib/gecko-alpha/backup-last-ok
# ExecStartPre fails-fast if the script went missing (R4 NIT closed). Without
# it, a missing script surfaces only as `Exec format error` in journalctl.
ExecStartPre=/usr/bin/test -x /usr/local/bin/gecko-backup-rotate.sh
ExecStart=/usr/local/bin/gecko-backup-rotate.sh
TimeoutStartSec=120
StandardOutput=journal
StandardError=journal
```

### `systemd/gecko-backup.timer`

```ini
[Unit]
Description=Gecko-Alpha — nightly backup rotation timer

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true
AccuracySec=1h
Unit=gecko-backup.service

[Install]
WantedBy=timers.target
```

### `systemd/gecko-backup-watchdog.service`

```ini
[Unit]
Description=Gecko-Alpha — alert if backup rotation is stale
# R4 NIT: watchdog DOES need network (Telegram API call via uv-run python).
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
Group=root
Environment=GECKO_BACKUP_STALE_AFTER_SEC=172800
Environment=GECKO_BACKUP_HEARTBEAT_FILE=/var/lib/gecko-alpha/backup-last-ok
Environment=GECKO_REPO=/root/gecko-alpha
Environment=UV_BIN=/root/.local/bin/uv
# R4 MUST-FIX: explicit PATH + HOME mirroring gecko-pipeline.service. Without
# these, `uv run` cannot locate uv (systemd strips PATH) and the watchdog
# silently exits non-zero with no Telegram delivery.
Environment=HOME=/root
Environment=PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStartPre=/usr/bin/test -x /usr/local/bin/gecko-backup-watchdog.sh
ExecStartPre=/usr/bin/test -x /root/.local/bin/uv
ExecStart=/usr/local/bin/gecko-backup-watchdog.sh
TimeoutStartSec=60
StandardOutput=journal
StandardError=journal
```

### `systemd/gecko-backup-watchdog.timer`

```ini
[Unit]
Description=Gecko-Alpha — daily watchdog: backup-rotation freshness check

[Timer]
OnCalendar=*-*-* 09:00:00
Persistent=true
AccuracySec=30m
Unit=gecko-backup-watchdog.service

[Install]
WantedBy=timers.target
```

## Test plan (pytest, `tests/test_backup_rotate_script.py`)

```python
"""Tests for scripts/gecko-backup-rotate.sh — invoked via subprocess.

Test methodology: each test creates an isolated tmp_path with fake backup files
at staggered mtimes (via os.utime), then invokes the bash script with
GECKO_BACKUP_DIR pointing at tmp_path and GECKO_BACKUP_HEARTBEAT_FILE pointing
at a tmp heartbeat path. Asserts on which files survive + heartbeat state +
exit code.
"""

import os
import stat
import subprocess
from pathlib import Path
import time
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gecko-backup-rotate.sh"


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


def test_keeps_top_n_by_mtime(tmp_path):
    """5 backups, KEEP=3 → top-3-by-mtime survive."""
    now = time.time()
    files = []
    for i, age_hours in enumerate([10, 20, 30, 40, 50]):
        files.append(_make_backup(
            tmp_path, f"scout.db.bak.tag{i}.{int(now)}", now - age_hours * 3600
        ))
    hb = tmp_path / "hb"
    res = _run({"GECKO_BACKUP_DIR": str(tmp_path),
                "GECKO_BACKUP_KEEP": "3",
                "GECKO_BACKUP_HEARTBEAT_FILE": str(hb)})
    assert res.returncode == 0, res.stderr
    surviving = sorted([p.name for p in tmp_path.iterdir() if p.name != "hb"])
    # files[0..2] are the 3 newest (10/20/30h ago); files[3..4] should be deleted
    assert "scout.db.bak.tag0." + str(int(now)) in surviving
    assert "scout.db.bak.tag1." + str(int(now)) in surviving
    assert "scout.db.bak.tag2." + str(int(now)) in surviving
    assert "scout.db.bak.tag3." + str(int(now)) not in surviving
    assert "scout.db.bak.tag4." + str(int(now)) not in surviving


def test_idempotent_rerun_on_trimmed_dir(tmp_path):
    """Re-running on a 3-file dir with KEEP=3 is a no-op."""
    now = time.time()
    for i in range(3):
        _make_backup(tmp_path, f"scout.db.bak.tag{i}.x", now - i * 3600)
    hb = tmp_path / "hb"
    _run({"GECKO_BACKUP_DIR": str(tmp_path),
          "GECKO_BACKUP_KEEP": "3",
          "GECKO_BACKUP_HEARTBEAT_FILE": str(hb)})
    res2 = _run({"GECKO_BACKUP_DIR": str(tmp_path),
                 "GECKO_BACKUP_KEEP": "3",
                 "GECKO_BACKUP_HEARTBEAT_FILE": str(hb)})
    assert res2.returncode == 0
    assert len([p for p in tmp_path.iterdir() if p.name != "hb"]) == 3


def test_empty_dir_is_noop(tmp_path):
    """Empty backup dir — exit 0, no error."""
    hb = tmp_path / "hb"
    res = _run({"GECKO_BACKUP_DIR": str(tmp_path),
                "GECKO_BACKUP_KEEP": "3",
                "GECKO_BACKUP_HEARTBEAT_FILE": str(hb)})
    assert res.returncode == 0
    assert "no rotation needed" in res.stdout


def test_keep_zero_deletes_everything(tmp_path):
    """KEEP=0 operator escape hatch — all backups deleted."""
    now = time.time()
    for i in range(3):
        _make_backup(tmp_path, f"scout.db.bak.tag{i}", now - i * 3600)
    hb = tmp_path / "hb"
    res = _run({"GECKO_BACKUP_DIR": str(tmp_path),
                "GECKO_BACKUP_KEEP": "0",
                "GECKO_BACKUP_HEARTBEAT_FILE": str(hb)})
    assert res.returncode == 0
    remaining = [p for p in tmp_path.iterdir() if p.name != "hb"]
    assert len(remaining) == 0


def test_unified_sort_across_both_name_patterns(tmp_path):
    """R2 MUST-FIX regression-lock: both `bak.<tag>` and `bak-<iso>` must
    participate in a SINGLE mtime sort. 4 of each at staggered mtimes,
    KEEP=3 → exactly 3 survive total (not 6 = 3-from-each-bucket)."""
    now = time.time()
    # 8 files alternating patterns, mtimes spread
    for i, hours_ago in enumerate([1, 2, 3, 4, 5, 6, 7, 8]):
        if i % 2 == 0:
            name = f"scout.db.bak.tag{i}.{int(now)}"
        else:
            name = f"scout.db.bak-2026010{i}T000000Z"
        _make_backup(tmp_path, name, now - hours_ago * 3600)
    hb = tmp_path / "hb"
    res = _run({"GECKO_BACKUP_DIR": str(tmp_path),
                "GECKO_BACKUP_KEEP": "3",
                "GECKO_BACKUP_HEARTBEAT_FILE": str(hb)})
    assert res.returncode == 0
    surviving = [p.name for p in tmp_path.iterdir() if p.name != "hb"]
    assert len(surviving) == 3, (
        f"Expected exactly 3 survivors (single unified sort); got {len(surviving)}: "
        f"{surviving}"
    )


def test_unset_dir_aborts(tmp_path):
    """R1 MUST-FIX: GECKO_BACKUP_DIR unset → script exits non-zero."""
    env = {k: v for k, v in os.environ.items() if k != "GECKO_BACKUP_DIR"}
    env["GECKO_BACKUP_HEARTBEAT_FILE"] = str(tmp_path / "hb")
    res = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True
    )
    assert res.returncode != 0
    assert "GECKO_BACKUP_DIR" in res.stderr


def test_nonexistent_dir_aborts(tmp_path):
    """R1 MUST-FIX: GECKO_BACKUP_DIR set to nonexistent path → exit 2."""
    nonexistent = tmp_path / "does-not-exist"
    res = _run({"GECKO_BACKUP_DIR": str(nonexistent),
                "GECKO_BACKUP_KEEP": "3",
                "GECKO_BACKUP_HEARTBEAT_FILE": str(tmp_path / "hb")})
    assert res.returncode == 2
    assert "is not a directory" in res.stderr


def test_filename_with_space_preserved(tmp_path):
    """R2 NIT: pathological filename with embedded space — `cut` must
    preserve the path correctly across the mtime sort. Locks the
    `cut -d' ' -f2-` choice over `awk '{print $2}'`."""
    now = time.time()
    _make_backup(tmp_path, "scout.db.bak.tag.normal", now - 1)
    _make_backup(tmp_path, "scout.db.bak. extra-tag", now - 2)  # space in name
    _make_backup(tmp_path, "scout.db.bak.older", now - 100)
    hb = tmp_path / "hb"
    res = _run({"GECKO_BACKUP_DIR": str(tmp_path),
                "GECKO_BACKUP_KEEP": "2",
                "GECKO_BACKUP_HEARTBEAT_FILE": str(hb)})
    assert res.returncode == 0, res.stderr
    surviving = sorted([p.name for p in tmp_path.iterdir() if p.name != "hb"])
    assert "scout.db.bak.tag.normal" in surviving
    assert "scout.db.bak. extra-tag" in surviving
    assert "scout.db.bak.older" not in surviving


def test_heartbeat_written_on_success(tmp_path):
    """R1 MUST-FIX: heartbeat file is written with timestamp on success.

    R3 MUST-FIX: float bounds with 1s tolerance for slow CI / clock-skew.
    """
    hb = tmp_path / "hb"
    _make_backup(tmp_path, "scout.db.bak.x", time.time())
    before = time.time()
    res = _run({"GECKO_BACKUP_DIR": str(tmp_path),
                "GECKO_BACKUP_KEEP": "3",
                "GECKO_BACKUP_HEARTBEAT_FILE": str(hb)})
    after = time.time() + 1  # 1s tolerance for slow CI / int truncation
    assert res.returncode == 0
    assert hb.exists()
    written = int(hb.read_text().strip())
    assert before - 1 <= written <= after, (
        f"heartbeat={written} not within [{before-1}, {after}]"
    )


def test_heartbeat_NOT_written_on_failure(tmp_path):
    """R1 MUST-FIX: heartbeat NOT updated when script fails (e.g., bad dir)."""
    hb = tmp_path / "hb"
    res = _run({"GECKO_BACKUP_DIR": str(tmp_path / "nope"),
                "GECKO_BACKUP_KEEP": "3",
                "GECKO_BACKUP_HEARTBEAT_FILE": str(hb)})
    assert res.returncode != 0
    assert not hb.exists()


def test_symlink_not_followed(tmp_path):
    """Defensive: symlink matching the glob is NOT rotated (-type f filter).

    R3 MUST-FIX: lock symlink.is_symlink() — `find -type f` definitively
    excludes symlinks, so the symlink itself MUST survive.
    """
    real = tmp_path / "scout.db.bak.real"
    real.write_text("x")
    target_outside = tmp_path / "outside.db"
    target_outside.write_text("important — should not be touched")
    symlink = tmp_path / "scout.db.bak.symlink"
    symlink.symlink_to(target_outside)
    hb = tmp_path / "hb"
    res = _run({"GECKO_BACKUP_DIR": str(tmp_path),
                "GECKO_BACKUP_KEEP": "0",  # delete-everything mode
                "GECKO_BACKUP_HEARTBEAT_FILE": str(hb)})
    assert res.returncode == 0
    # The real file got deleted (matches -type f).
    assert not real.exists()
    # The symlink was NOT followed; target_outside survives.
    assert target_outside.exists()
    assert target_outside.read_text() == "important — should not be touched"
    # The symlink inode itself was NOT in the deletion list (find -type f).
    assert symlink.is_symlink(), (
        "symlink should survive — find -type f must exclude symlinks"
    )


def test_negative_keep_aborts(tmp_path):
    """R3 NIT: GECKO_BACKUP_KEEP=-1 is rejected by `^[0-9]+$` regex → exit 2."""
    hb = tmp_path / "hb"
    _make_backup(tmp_path, "scout.db.bak.x", time.time())
    res = _run({"GECKO_BACKUP_DIR": str(tmp_path),
                "GECKO_BACKUP_KEEP": "-1",
                "GECKO_BACKUP_HEARTBEAT_FILE": str(hb)})
    assert res.returncode == 2
    assert "must be a non-negative integer" in res.stderr


def test_unwritable_heartbeat_path_aborts(tmp_path):
    """R3 MUST-FIX: heartbeat-write failure must surface, not silently succeed.

    Construct an unwritable heartbeat path (parent dir is `chmod 000`) and
    confirm the script exits non-zero so the systemd unit goes into
    `failed` state and the watchdog fires within 24h.
    """
    locked_parent = tmp_path / "locked"
    locked_parent.mkdir(mode=0o000)
    try:
        hb = locked_parent / "hb"
        _make_backup(tmp_path, "scout.db.bak.x", time.time())
        res = _run({"GECKO_BACKUP_DIR": str(tmp_path),
                    "GECKO_BACKUP_KEEP": "3",
                    "GECKO_BACKUP_HEARTBEAT_FILE": str(hb)})
        assert res.returncode != 0, (
            f"Expected non-zero exit for unwritable heartbeat; got {res.returncode}"
        )
    finally:
        # Restore perms so tmp_path teardown can clean up.
        locked_parent.chmod(0o755)


def test_flock_concurrent_invocation_exits_3(tmp_path):
    """R3 MUST-FIX: flock guard against concurrent invocations.

    Hold the lock with a separate flock invocation, then try to run the
    script — it must exit 3 cleanly (not corrupt the rotation).
    """
    import threading
    hb = tmp_path / "hb"
    lock = tmp_path / "lock"
    _make_backup(tmp_path, "scout.db.bak.x", time.time())

    # Acquire the lock in a held-flock subprocess (sleeps so the file desc
    # stays open for our test invocation).
    holder = subprocess.Popen(
        ["bash", "-c", f"exec 9>{lock}; flock 9; sleep 5"],
    )
    try:
        time.sleep(0.5)  # give holder time to take the lock
        res = _run({"GECKO_BACKUP_DIR": str(tmp_path),
                    "GECKO_BACKUP_KEEP": "3",
                    "GECKO_BACKUP_HEARTBEAT_FILE": str(hb),
                    "GECKO_BACKUP_LOCK_FILE": str(lock)})
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
# Watchdog tests — R3 CRITICAL gap closed.
# Stub `uv` on $PATH so the watchdog's Telegram-via-uv-run-python path is
# observable without the real network call. The stub writes a marker file
# we can assert on.
# ----------------------------------------------------------------------

WATCHDOG_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "gecko-backup-watchdog.sh"
)


def _make_uv_stub(tmp_path: Path) -> Path:
    """Create a stub `uv` script that records its invocation to a marker file."""
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    stub = stub_dir / "uv"
    marker = tmp_path / "alert_marker"
    stub.write_text(
        f"#!/usr/bin/env bash\n"
        f"echo \"uv called: $@\" >> {marker}\n"
        f"# Pretend the python invocation succeeded:\n"
        f"exit 0\n"
    )
    stub.chmod(0o755)
    return stub


def _run_watchdog(tmp_path: Path, env_overrides):
    """Invoke the watchdog with stub uv on PATH."""
    stub = _make_uv_stub(tmp_path)
    env = os.environ.copy()
    env["PATH"] = f"{stub.parent}:" + env.get("PATH", "")
    env["UV_BIN"] = str(stub)  # explicit override
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(WATCHDOG_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )


def test_watchdog_missing_heartbeat_alerts(tmp_path):
    """R3 CRITICAL: heartbeat file missing → watchdog fires alert (exit 1)."""
    res = _run_watchdog(tmp_path, {
        "GECKO_BACKUP_HEARTBEAT_FILE": str(tmp_path / "does-not-exist"),
        "GECKO_REPO": str(tmp_path),
        "GECKO_BACKUP_STALE_AFTER_SEC": "172800",
    })
    assert res.returncode == 1, res.stderr
    assert "MISSING" in res.stdout or "MISSING" in res.stderr
    # The uv stub was invoked → alert path was taken.
    marker = tmp_path / "alert_marker"
    assert marker.exists(), "uv stub was not called — alert path skipped"


def test_watchdog_stale_heartbeat_alerts(tmp_path):
    """R3 CRITICAL: heartbeat older than threshold → watchdog fires alert."""
    hb = tmp_path / "hb"
    # 49h ago > 48h threshold
    hb.write_text(str(int(time.time() - 49 * 3600)))
    res = _run_watchdog(tmp_path, {
        "GECKO_BACKUP_HEARTBEAT_FILE": str(hb),
        "GECKO_REPO": str(tmp_path),
        "GECKO_BACKUP_STALE_AFTER_SEC": "172800",
    })
    assert res.returncode == 1
    marker = tmp_path / "alert_marker"
    assert marker.exists(), "stale-path alert was not delivered"


def test_watchdog_fresh_heartbeat_ok(tmp_path):
    """R3 CRITICAL: heartbeat fresh → watchdog exits 0, NO alert delivered."""
    hb = tmp_path / "hb"
    # 1h ago < 48h threshold
    hb.write_text(str(int(time.time() - 3600)))
    res = _run_watchdog(tmp_path, {
        "GECKO_BACKUP_HEARTBEAT_FILE": str(hb),
        "GECKO_REPO": str(tmp_path),
        "GECKO_BACKUP_STALE_AFTER_SEC": "172800",
    })
    assert res.returncode == 0
    assert "OK:" in res.stdout
    marker = tmp_path / "alert_marker"
    assert not marker.exists(), "uv stub was unexpectedly called on fresh path"
```

11 test cases (was 5). All match concrete reviewer findings.

## Runbook (`docs/runbook_backup_rotation.md`)

```markdown
# VPS backup rotation runbook

## One-time install (operator)

```bash
# 1. Pull latest code on VPS
ssh srilu-vps 'cd /root/gecko-alpha && git pull'

# 2. Install bash scripts under /usr/local/bin
ssh srilu-vps '
  install -m 0755 /root/gecko-alpha/scripts/gecko-backup-rotate.sh \
                  /usr/local/bin/gecko-backup-rotate.sh
  install -m 0755 /root/gecko-alpha/scripts/gecko-backup-watchdog.sh \
                  /usr/local/bin/gecko-backup-watchdog.sh
'

# 3. Install systemd units
ssh srilu-vps '
  install -m 0644 /root/gecko-alpha/systemd/gecko-backup.service \
                  /etc/systemd/system/gecko-backup.service
  install -m 0644 /root/gecko-alpha/systemd/gecko-backup.timer \
                  /etc/systemd/system/gecko-backup.timer
  install -m 0644 /root/gecko-alpha/systemd/gecko-backup-watchdog.service \
                  /etc/systemd/system/gecko-backup-watchdog.service
  install -m 0644 /root/gecko-alpha/systemd/gecko-backup-watchdog.timer \
                  /etc/systemd/system/gecko-backup-watchdog.timer
  systemctl daemon-reload
'

# 4. Enable + start timers
ssh srilu-vps '
  systemctl enable --now gecko-backup.timer
  systemctl enable --now gecko-backup-watchdog.timer
  systemctl list-timers gecko-backup gecko-backup-watchdog
'
```

## Verify install

```bash
ssh srilu-vps 'systemctl status gecko-backup.timer gecko-backup-watchdog.timer'
# Expect: both active (waiting), enabled
```

## Manual rotation (if disk pressure before next 03:00)

```bash
ssh srilu-vps 'systemctl start gecko-backup.service && \
  journalctl -u gecko-backup.service -n 30 --no-pager'
```

## Notes on `Persistent=true` (operator warning — R4 MUST-FIX)

**At install time:** `systemctl enable --now gecko-backup.timer` will see that
today's 03:00 window has been missed (assuming install happens after 03:00
local), and per `Persistent=true` will fire `gecko-backup.service` IMMEDIATELY,
within the `AccuracySec=1h` smear window. Same for the watchdog timer at 09:00.

**This is benign but surprising.** If the operator just created a fresh manual
backup minutes before install, that backup is the NEWEST and is preserved as
#1; older backups rotate normally. No data loss. To avoid the surprise:

- Install during the 03:00–04:00 UTC window (no immediate fire).
- OR run `systemctl enable gecko-backup.timer gecko-backup-watchdog.timer`
  WITHOUT `--now`; then `systemctl start` only when ready for the first cycle.

**At reboot time:** Same behavior. Heartbeat file is in `/var/lib/gecko-alpha/`
(persistent) so the watchdog will NOT false-positive after a reboot — this is
a deliberate choice to use `/var/lib` instead of `/var/run`.

**Race with operator manual backup:** If the operator runs `cp scout.db
scout.db.bak.X` while the timer fires concurrently, the rotation script's
flock guard exits 3 cleanly without rotating; next 03:00 fire processes
both files together by mtime. No corruption, no data loss.

## Watchdog alert

If `gecko-backup.service` fails or is silently disabled, the watchdog timer
fires daily at 09:00 UTC and sends a Telegram alert via the existing
`scout.alerter.send_telegram_message` path. Operator should:

1. Check `journalctl -u gecko-backup.service` for the last successful run.
2. Check `cat /var/lib/gecko-alpha/backup-last-ok` for the heartbeat timestamp.
3. Manually trigger via `systemctl start gecko-backup.service` once root cause is fixed.

## Disable / revert

```bash
ssh srilu-vps '
  systemctl disable --now gecko-backup.timer gecko-backup-watchdog.timer
  systemctl stop gecko-backup.service gecko-backup-watchdog.service
  rm -f /usr/local/bin/gecko-backup-rotate.sh \
        /usr/local/bin/gecko-backup-watchdog.sh \
        /etc/systemd/system/gecko-backup.{service,timer} \
        /etc/systemd/system/gecko-backup-watchdog.{service,timer}
  systemctl daemon-reload
  rm -rf /var/lib/gecko-alpha
  rm -f /var/lock/gecko-backup-rotate.lock
'
```

## Future work (out of v1 scope)

- GPG encryption of backups (Phase 2).
- Offsite upload to S3/Backblaze (Phase 2).
- Backup integrity verification (`PRAGMA integrity_check`).
- Pre-deploy backup hook (auto-create backup before each `git pull`).
```

## Reviewer dispatch — design stage (2 parallel)

- **R3 (test rigor / pytest discipline):** Are the 11 pytest cases sufficient? Particularly: do they exercise the watchdog script end-to-end (currently the test plan covers only the rotation script)? Is the heartbeat-timestamp assertion robust to clock skew? Does the symlink test cleanly exercise `-type f` semantics?
- **R4 (systemd / install-time correctness):** Are the unit files structurally correct (After= / Wants= / RequiresMountsFor=)? Will `systemctl enable --now` cause an immediate fire (problematic for the rotation timer; benign for watchdog)? Should there be an `ExecStartPre=` to fail-loud if the script file is missing? Are the `Environment=` paths absolute and correct? Permissions on `/var/run/` writable by root?
