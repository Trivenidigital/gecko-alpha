#!/usr/bin/env bash
# gecko-backup-create — produce a fresh online backup of scout.db.
#
# Round 11 finding: srilu has gecko-backup-rotate.sh running daily but
# `found=0` every day — no producer was ever installed. The rotation +
# watchdog stack has been monitoring an empty directory.
#
# This script fills the gap. It uses sqlite3's online .backup API which
# is lock-free against concurrent writes (BL-NEW-GECKO-BACKUP-CREATE).
# After writing, runs PRAGMA integrity_check to verify the new backup is
# readable. On any failure (sqlite3 missing, source unreadable, integrity
# check fail), exits non-zero so the systemd unit's OnFailure chain
# fires.
#
# Required env:
#   GECKO_DB_PATH        — absolute path to live scout.db (default
#                          /root/gecko-alpha/scout.db)
#   GECKO_BACKUP_DIR     — directory to write the .bak.<ts> file
#                          (matches the rotate script's source-of-truth)
# Optional env:
#   GECKO_BACKUP_CREATE_HEARTBEAT_FILE — override path (default
#                                        /var/lib/gecko-alpha/backup-rotation/
#                                        create-last-ok)
#   GECKO_BACKUP_CREATE_LOCK_FILE      — flock guard path (default
#                                        /var/lock/gecko-backup-create.lock)
#   GECKO_BACKUP_SQLITE_BIN            — override sqlite3 binary (test seam)
#
# Exit codes:
#   0 = success (backup created + integrity OK + heartbeat written)
#   2 = misconfiguration (missing dir / binary / unreadable source)
#   3 = lock contention (another invocation in flight)
#   4 = sqlite3 .backup command failed
#   5 = PRAGMA integrity_check did not return "ok"
#
# Naming convention: `scout.db.bak.YYYYMMDDTHHMMSSZ` matches the
# `scout.db.bak.*` glob used by gecko-backup-rotate.sh so the rotation
# step that runs immediately after this script discovers + prunes.

set -euo pipefail

DB_PATH="${GECKO_DB_PATH:-/root/gecko-alpha/scout.db}"
: "${GECKO_BACKUP_DIR:?ERROR: GECKO_BACKUP_DIR must be set}"
HEARTBEAT_FILE="${GECKO_BACKUP_CREATE_HEARTBEAT_FILE:-/var/lib/gecko-alpha/backup-rotation/create-last-ok}"
LOCK_FILE="${GECKO_BACKUP_CREATE_LOCK_FILE:-/var/lock/gecko-backup-create.lock}"
SQLITE_BIN="${GECKO_BACKUP_SQLITE_BIN:-sqlite3}"

if [[ ! -f "$DB_PATH" ]]; then
    echo "ERROR: GECKO_DB_PATH=$DB_PATH is not a regular file" >&2
    exit 2
fi
if [[ ! -d "$GECKO_BACKUP_DIR" ]]; then
    echo "ERROR: GECKO_BACKUP_DIR=$GECKO_BACKUP_DIR is not a directory" >&2
    exit 2
fi
if ! command -v "$SQLITE_BIN" >/dev/null 2>&1; then
    echo "ERROR: sqlite3 binary not found ($SQLITE_BIN). apt install sqlite3" >&2
    exit 2
fi

# Lock — prevent concurrent invocations from creating duplicate backups
# (e.g. timer fire overlapping with a manual operator run).
if ! exec 9>"$LOCK_FILE"; then
    echo "ERROR: cannot open $LOCK_FILE — check /var/lock writability" >&2
    exit 2
fi
if ! flock -n 9; then
    echo "gecko-backup-create: another invocation holds $LOCK_FILE; skipping" >&2
    exit 3
fi

if ! mkdir -p "$(dirname "$HEARTBEAT_FILE")"; then
    echo "ERROR: cannot create heartbeat parent dir for $HEARTBEAT_FILE" >&2
    exit 2
fi

TS="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="$GECKO_BACKUP_DIR/scout.db.bak.$TS"
DEST_TMP="$DEST.partial"

# Remove the temp backup AND every SQLite sidecar it can leave behind.
#
# `sqlite3 .backup` opens the destination as a database, so a failed or
# interrupted run can leave `-journal`, `-wal` and `-shm` companions next to
# the main file. Removing only "$DEST_TMP" leaves those behind forever: nothing
# else ever cleans them, and until the rotation glob was tightened they counted
# toward KEEP and could evict a completed backup.
#
# Observed 2026-08-08 on prod: five orphaned `*.partial-journal` files from five
# consecutive failed runs, which had to be removed by hand.
_cleanup_partial() {
    rm -f -- \
        "$DEST_TMP" \
        "$DEST_TMP-journal" \
        "$DEST_TMP-wal" \
        "$DEST_TMP-shm"
}

# Signal cleanup. Installed immediately after _cleanup_partial() and DEST_TMP
# exist, so there is no window in which the temp file can be created while
# uncovered.
#
# The ordinary error paths below call _cleanup_partial directly. They cannot
# help when the script is SIGNALLED rather than allowed to fail: systemd sends
# SIGTERM on TimeoutStartSec expiry, and without a trap the shell dies with the
# partial and every sidecar left on disk.
#
# 2026-08-08: TimeoutStartSec=600 expired mid-`.backup` on a 6.82 GB database.
# systemd terminated the unit (status=15/TERM) and orphaned a 6.8 GB `.partial`
# plus its -shm/-wal. Nothing else ever revisits a `.partial-*` name, so it sat
# there consuming capacity until removed by hand. Two earlier orphans of the
# same shape (2026-08-02, 2026-08-03) are strongly corroborated as the same
# mechanism — matching near-complete sizes under the same unchanged 600s limit
# — though their systemd records had already aged out of journal retention, so
# that attribution is inference rather than log evidence.
#
# Exit 143 = 128 + SIGTERM, the conventional shell encoding, so the unit still
# reports failure rather than a spurious success.
#
# SIGKILL is deliberately NOT handled: it is uncatchable. An orphan can still
# result from `kill -9`, OOM, or power loss. After PR #518 such an orphan can
# no longer masquerade as a completed backup or evict one, but reclaiming its
# disk space remains a separate, unsolved problem.
_on_signal() {
    local sig="$1"
    echo "gecko-backup-create: received SIG${sig} — removing partial artifacts" >&2
    _cleanup_partial
    trap - "$sig"
    exit $((128 + $(kill -l "$sig")))
}
trap '_on_signal TERM' TERM
trap '_on_signal INT' INT
trap '_on_signal HUP' HUP

echo "gecko-backup-create: source=$DB_PATH dest=$DEST"

# Online backup — copies pages incrementally; safe against concurrent
# writers. The .partial sentinel lets the next-run rotation skip a
# half-written file if this script is SIGKILLed mid-backup.
if ! "$SQLITE_BIN" "$DB_PATH" ".backup '$DEST_TMP'" 2>&1; then
    echo "ERROR: sqlite3 .backup failed" >&2
    _cleanup_partial
    exit 4
fi

# Verify the new file is structurally sound before we promote it.
# PRAGMA integrity_check returns "ok" on a single line for a clean DB;
# anything else (multi-line error report) indicates corruption.
INTEGRITY="$("$SQLITE_BIN" "$DEST_TMP" "PRAGMA integrity_check;" 2>&1 || true)"
if [[ "$INTEGRITY" != "ok" ]]; then
    echo "ERROR: integrity check failed for $DEST_TMP:" >&2
    printf '%s\n' "$INTEGRITY" >&2
    _cleanup_partial
    exit 5
fi

# Promote .partial → final name. atomic-rename within same filesystem.
mv -f "$DEST_TMP" "$DEST"

# Sidecars should not survive a clean backup, but sweep them anyway: `mv`
# promotes only the main file, so anything left beside it would linger under a
# `.partial-*` name that no later run ever revisits.
rm -f -- "$DEST_TMP-journal" "$DEST_TMP-wal" "$DEST_TMP-shm"

# Size sanity-check (visible in journal for postmortem).
SIZE="$(stat -c '%s' "$DEST" 2>/dev/null || echo unknown)"
echo "gecko-backup-create: created $DEST size=$SIZE integrity=ok"

# Atomic heartbeat write (matches gecko-backup-rotate.sh pattern).
HB_TMP="${HEARTBEAT_FILE}.tmp.$$"
date +%s > "$HB_TMP"
mv -f "$HB_TMP" "$HEARTBEAT_FILE"
echo "gecko-backup-create: heartbeat updated at $HEARTBEAT_FILE"
