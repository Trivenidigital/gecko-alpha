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

echo "gecko-backup-create: source=$DB_PATH dest=$DEST"

# Online backup — copies pages incrementally; safe against concurrent
# writers. The .partial sentinel lets the next-run rotation skip a
# half-written file if this script is SIGKILLed mid-backup.
if ! "$SQLITE_BIN" "$DB_PATH" ".backup '$DEST_TMP'" 2>&1; then
    echo "ERROR: sqlite3 .backup failed" >&2
    rm -f "$DEST_TMP"
    exit 4
fi

# Verify the new file is structurally sound before we promote it.
# PRAGMA integrity_check returns "ok" on a single line for a clean DB;
# anything else (multi-line error report) indicates corruption.
INTEGRITY="$("$SQLITE_BIN" "$DEST_TMP" "PRAGMA integrity_check;" 2>&1 || true)"
if [[ "$INTEGRITY" != "ok" ]]; then
    echo "ERROR: integrity check failed for $DEST_TMP:" >&2
    printf '%s\n' "$INTEGRITY" >&2
    rm -f "$DEST_TMP"
    exit 5
fi

# Promote .partial → final name. atomic-rename within same filesystem.
mv -f "$DEST_TMP" "$DEST"

# Size sanity-check (visible in journal for postmortem).
SIZE="$(stat -c '%s' "$DEST" 2>/dev/null || echo unknown)"
echo "gecko-backup-create: created $DEST size=$SIZE integrity=ok"

# Atomic heartbeat write (matches gecko-backup-rotate.sh pattern).
HB_TMP="${HEARTBEAT_FILE}.tmp.$$"
date +%s > "$HB_TMP"
mv -f "$HB_TMP" "$HEARTBEAT_FILE"
echo "gecko-backup-create: heartbeat updated at $HEARTBEAT_FILE"
