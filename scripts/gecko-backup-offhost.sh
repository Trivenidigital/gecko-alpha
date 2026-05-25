#!/usr/bin/env bash
# gecko-backup-offhost — ship the newest local scout.db backup to an
# operator-configured off-host destination.
#
# Round 20 motivation: R11/R13/R18/R19 produce, rotate, watchdog, and
# surface local backups well, but they all live on the same VPS
# filesystem as the live DB. A single filesystem failure (RAID die,
# accidental `rm -rf /`, mistyped fdisk) takes both the DB and every
# .bak with it. This script closes the gap by copying the newest .bak
# elsewhere — another mount, another host, an object store mount — on
# the same schedule.
#
# Required env (script no-ops if unset — opt-in by design):
#   GECKO_OFFHOST_BACKUP_DEST  — rsync target spec. Anything rsync
#                                accepts works: `user@host:/path/`,
#                                `/mnt/external/backups/`, etc.
#                                Empty/unset = disabled (exit 0).
#
# Optional env:
#   GECKO_DB_PATH                       — live scout.db (informational
#                                         only; not transferred)
#   GECKO_BACKUP_DIR                    — source dir of .bak files
#                                         (default /root/gecko-alpha)
#   GECKO_OFFHOST_BACKUP_HEARTBEAT_FILE — heartbeat path (default
#                                         /var/lib/gecko-alpha/
#                                         backup-rotation/offhost-last-ok)
#   GECKO_OFFHOST_BACKUP_LOCK_FILE      — flock guard (default
#                                         /var/lock/gecko-backup-offhost.lock)
#   GECKO_OFFHOST_BACKUP_BIN            — override rsync binary
#                                         (testability seam)
#   GECKO_OFFHOST_BACKUP_RSYNC_OPTS     — extra rsync flags (default
#                                         "--archive --partial --inplace")
#
# Exit codes:
#   0 = success (file shipped + heartbeat written) OR disabled (env unset)
#   2 = misconfiguration (no .bak found, can't create dirs)
#   3 = lock contention (another invocation in flight)
#   4 = rsync transfer failed
#   6 = rsync binary missing
#
# Idempotency: rsync with --partial+--inplace makes re-runs cheap (only
# changed pages transfer). Safe to schedule alongside the existing
# rotate timer.

set -euo pipefail

DEST="${GECKO_OFFHOST_BACKUP_DEST:-}"
DEST="${DEST#"${DEST%%[![:space:]]*}"}"  # ltrim
DEST="${DEST%"${DEST##*[![:space:]]}"}"  # rtrim

if [[ -z "$DEST" ]]; then
    echo "gecko-backup-offhost: GECKO_OFFHOST_BACKUP_DEST is empty — off-host backup disabled (set the env to enable)"
    exit 0
fi

BACKUP_DIR="${GECKO_BACKUP_DIR:-/root/gecko-alpha}"
HEARTBEAT_FILE="${GECKO_OFFHOST_BACKUP_HEARTBEAT_FILE:-/var/lib/gecko-alpha/backup-rotation/offhost-last-ok}"
LOCK_FILE="${GECKO_OFFHOST_BACKUP_LOCK_FILE:-/var/lock/gecko-backup-offhost.lock}"
RSYNC_BIN="${GECKO_OFFHOST_BACKUP_BIN:-rsync}"
RSYNC_OPTS="${GECKO_OFFHOST_BACKUP_RSYNC_OPTS:---archive --partial --inplace}"

if [[ ! -d "$BACKUP_DIR" ]]; then
    echo "ERROR: GECKO_BACKUP_DIR=$BACKUP_DIR is not a directory" >&2
    exit 2
fi
if ! command -v "$RSYNC_BIN" >/dev/null 2>&1; then
    echo "ERROR: rsync binary not found ($RSYNC_BIN). apt install rsync" >&2
    exit 6
fi

# Lock — prevent concurrent ships from racing on the heartbeat write
# and over-saturating the destination link.
if ! exec 9>"$LOCK_FILE"; then
    echo "ERROR: cannot open $LOCK_FILE — check /var/lock writability" >&2
    exit 2
fi
if ! flock -n 9; then
    echo "gecko-backup-offhost: another invocation holds $LOCK_FILE; skipping" >&2
    exit 3
fi

if ! mkdir -p "$(dirname "$HEARTBEAT_FILE")"; then
    echo "ERROR: cannot create heartbeat parent dir for $HEARTBEAT_FILE" >&2
    exit 2
fi

# Find newest .bak — match both naming conventions the rotate script
# accepts (scout.db.bak.* and scout.db.bak-*). `find -printf` so we sort
# numerically by mtime without depending on stat-output formatting.
NEWEST=""
NEWEST_MTIME=0
shopt -s nullglob
for pattern in 'scout.db.bak.*' 'scout.db.bak-*'; do
    for f in "$BACKUP_DIR"/$pattern; do
        [[ -f "$f" ]] || continue
        # Skip .partial sentinels left by gecko-backup-create.sh
        [[ "$f" == *.partial ]] && continue
        mtime=$(stat -c '%Y' "$f" 2>/dev/null || echo 0)
        if (( mtime > NEWEST_MTIME )); then
            NEWEST="$f"
            NEWEST_MTIME=$mtime
        fi
    done
done
shopt -u nullglob

if [[ -z "$NEWEST" ]]; then
    echo "ERROR: no scout.db.bak.* found in $BACKUP_DIR (run gecko-backup-create.sh first)" >&2
    exit 2
fi

SIZE="$(stat -c '%s' "$NEWEST" 2>/dev/null || echo unknown)"
echo "gecko-backup-offhost: source=$NEWEST size=$SIZE dest=$DEST"

# shellcheck disable=SC2086  # we intentionally word-split RSYNC_OPTS
if ! "$RSYNC_BIN" $RSYNC_OPTS "$NEWEST" "$DEST" 2>&1; then
    echo "ERROR: rsync transfer failed (src=$NEWEST dest=$DEST)" >&2
    exit 4
fi

echo "gecko-backup-offhost: transfer ok ($NEWEST → $DEST)"

# Atomic heartbeat write (matches gecko-backup-create.sh pattern).
HB_TMP="${HEARTBEAT_FILE}.tmp.$$"
date +%s > "$HB_TMP"
mv -f "$HB_TMP" "$HEARTBEAT_FILE"
echo "gecko-backup-offhost: heartbeat updated at $HEARTBEAT_FILE"
