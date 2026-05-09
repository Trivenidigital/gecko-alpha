#!/usr/bin/env bash
# gecko-backup-rotate — keep top-N most-recent scout.db backups, delete rest.
#
# Required env:
#   GECKO_BACKUP_DIR    — absolute path containing scout.db.bak.* files
# Optional env:
#   GECKO_BACKUP_KEEP            — count to retain (default 3)
#   GECKO_BACKUP_HEARTBEAT_FILE  — override path
#                                  (default /var/lib/gecko-alpha/backup-last-ok)
#   GECKO_BACKUP_LOCK_FILE       — flock guard path
#                                  (default /var/lock/gecko-backup-rotate.lock)
#
# Exit codes:
#   0 = success (including no-op on empty dir)
#   2 = misconfiguration (unset/missing dir, bad keep)
#   3 = lock contention (another invocation in flight)
#   non-zero = unexpected failure (set -e propagation, e.g. unwritable HB)
#
# Heartbeat file lives in /var/lib (persistent across reboots). DO NOT use
# /var/run — that's tmpfs on Ubuntu and clears at boot, which would cause
# the watchdog to fire a false-positive STALE alert after every reboot.

set -euo pipefail

: "${GECKO_BACKUP_DIR:?ERROR: GECKO_BACKUP_DIR must be set (no baked-in default)}"
KEEP="${GECKO_BACKUP_KEEP:-3}"
HEARTBEAT_FILE="${GECKO_BACKUP_HEARTBEAT_FILE:-/var/lib/gecko-alpha/backup-last-ok}"
LOCK_FILE="${GECKO_BACKUP_LOCK_FILE:-/var/lock/gecko-backup-rotate.lock}"

# Concurrent invocations (Persistent=true catch-up + manual systemctl start)
# would race and double-delete. Acquire an exclusive non-blocking lock; exit
# 3 cleanly on contention.
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
# -type f excludes symlinks (defensive). -maxdepth 1 anchors scope to the
# explicit dir. -printf '%T@ %p\n' = epoch-seconds + space + path; consumed
# by `cut -d' ' -f2-` so filenames containing spaces are preserved.
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
