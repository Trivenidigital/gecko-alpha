#!/usr/bin/env bash
# gecko-backup-rotate — keep top-N most-recent scout.db backups, delete rest.
#
# Required env:
#   GECKO_BACKUP_DIR    — absolute path containing scout.db.bak.* files
# Optional env:
#   GECKO_BACKUP_KEEP            — count to retain (default 3)
#   GECKO_BACKUP_HEARTBEAT_FILE  — override path
#                                  (default /var/lib/gecko-alpha/backup-rotation/backup-last-ok)
#   GECKO_BACKUP_LOCK_FILE       — flock guard path
#                                  (default /var/lock/gecko-backup-rotate.lock)
#
# Exit codes:
#   0 = success (including no-op on empty dir)
#   2 = misconfiguration (unset/missing dir, bad keep, lock open failed)
#   3 = lock contention (another invocation in flight)
#   4 = find/sort/cut pipeline failed
#   non-zero = unexpected failure (set -e propagation, e.g. unwritable HB)
#
# Heartbeat file lives in /var/lib (persistent across reboots). DO NOT use
# /var/run — that's tmpfs on Ubuntu and clears at boot, which would cause
# the watchdog to fire a false-positive STALE alert after every reboot.

set -euo pipefail

: "${GECKO_BACKUP_DIR:?ERROR: GECKO_BACKUP_DIR must be set (no baked-in default)}"
KEEP="${GECKO_BACKUP_KEEP:-3}"
HEARTBEAT_FILE="${GECKO_BACKUP_HEARTBEAT_FILE:-/var/lib/gecko-alpha/backup-rotation/backup-last-ok}"
LOCK_FILE="${GECKO_BACKUP_LOCK_FILE:-/var/lock/gecko-backup-rotate.lock}"

if [[ ! -d "$GECKO_BACKUP_DIR" ]]; then
    echo "ERROR: GECKO_BACKUP_DIR=$GECKO_BACKUP_DIR is not a directory" >&2
    exit 2
fi

if ! [[ "$KEEP" =~ ^[0-9]+$ ]]; then
    echo "ERROR: GECKO_BACKUP_KEEP=$KEEP must be a non-negative integer" >&2
    exit 2
fi

# Acquire flock AFTER validation so we don't leave orphan empty lock files
# from misconfigured invocations (R5/R6 NIT). On `exec` redirect failure
# (/var/lock not writable, ProtectSystem=strict, EPERM), bash's set -e
# propagates with a generic message — guard with explicit context for journal.
if ! exec 9>"$LOCK_FILE"; then
    echo "ERROR: cannot open $LOCK_FILE — check /var/lock writability + ProtectSystem=" >&2
    exit 2
fi
if ! flock -n 9; then
    echo "gecko-backup-rotate: another invocation holds $LOCK_FILE; skipping" >&2
    exit 3
fi

# Ensure the heartbeat parent dir exists (idempotent). StateDirectory= in
# the systemd unit also ensures this on first start; the mkdir is for
# direct-CLI invocation paths.
if ! mkdir -p "$(dirname "$HEARTBEAT_FILE")"; then
    echo "ERROR: cannot create heartbeat parent dir for $HEARTBEAT_FILE" >&2
    exit 2
fi

# R6 MUST-FIX: pipefail does not cover process substitution `< <(...)` cleanly
# in older bashes. Capture the find pipeline output to a tempfile via explicit
# redirect + status check so a partial / errored pipeline is observable
# (e.g., find permission-denied → empty mapfile → silent green-when-broken).
TMP_LIST="$(mktemp)"
trap 'rm -f "$TMP_LIST" "$TMP_LIST.err"' EXIT
if ! find "$GECKO_BACKUP_DIR" -maxdepth 1 -type f \
        \( -name 'scout.db.bak.*' -o -name 'scout.db.bak-*' \) \
        -printf '%T@ %p\n' 2>"$TMP_LIST.err" \
    | sort -rn \
    | cut -d' ' -f2- > "$TMP_LIST"; then
    echo "ERROR: find/sort/cut pipeline failed; stderr:" >&2
    cat "$TMP_LIST.err" >&2 || true
    exit 4
fi
mapfile -t files < "$TMP_LIST"

total="${#files[@]}"
echo "gecko-backup-rotate: dir=$GECKO_BACKUP_DIR found=$total keep=$KEEP"

if (( total > KEEP )); then
    to_delete=("${files[@]:$KEEP}")
    # R6 MUST-FIX: log targets BEFORE rm so post-mortem on partial failure
    # can reconstruct what was meant to delete vs what actually leaked.
    echo "gecko-backup-rotate: rotating ${#to_delete[@]} files:"
    for f in "${to_delete[@]}"; do echo "  -> rm $f"; done
    rm -v -- "${to_delete[@]}"
    deleted="${#to_delete[@]}"
    echo "gecko-backup-rotate: deleted=$deleted retained=$KEEP"
else
    echo "gecko-backup-rotate: no rotation needed (total <= keep)"
fi

# R6 MUST-FIX: atomic heartbeat write. Truncate-then-write would expose
# a 0-byte file to any concurrent watchdog read. .tmp + mv-f is a
# kernel-atomic rename within the same filesystem.
HB_TMP="${HEARTBEAT_FILE}.tmp.$$"
date +%s > "$HB_TMP"
mv -f "$HB_TMP" "$HEARTBEAT_FILE"
echo "gecko-backup-rotate: heartbeat updated at $HEARTBEAT_FILE"
