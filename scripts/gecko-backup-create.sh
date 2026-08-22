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
#   GECKO_BACKUP_MIN_FREE_MARGIN_MB    — free space required BEYOND the size of
#                                        the database itself before REFUSING
#                                        (default 512)
#   GECKO_BACKUP_WARN_FREE_MARGIN_MB   — headroom below which the run warns but
#                                        still proceeds (default 2048)
#
# Exit codes:
#   0 = success (backup created + integrity OK + heartbeat written)
#   2 = misconfiguration (missing dir / binary / unreadable source)
#   3 = lock contention (another invocation in flight)
#   4 = sqlite3 .backup command failed
#   5 = PRAGMA integrity_check did not return "ok"
#   6 = insufficient free space (pre-flight refusal; nothing written, no
#       existing backup touched)
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

# --- PRE-FLIGHT FREE-SPACE GUARD -------------------------------------------
# This lane's own origin PR (#87) is titled "closes recurring 100%-disk
# incident", and yet nothing here checked free space before writing a file the
# size of the live database. `sqlite3 .backup` will happily fill the volume and
# then fail, and a full volume is far worse than a missing backup: the LIVE
# database shares it, so a failed write can take out writes to scout.db itself.
#
# Measured on srilu 2026-08-22: 7,012 MB database, 7.7 GB free, three retained
# backups (GECKO_BACKUP_KEEP=3) plus ~18.8 GB of ad-hoc one-off snapshots in
# /root that no rotation policy manages. Peak free DURING the nightly create was
# 973 MB. It fits — but nothing was watching, and nothing would have said so.
#
# Ordering note: create-then-rotate is deliberate and NOT inverted here.
# Rotating first would halve peak usage but deletes a known-good backup before
# the replacement is proven — trading a disk risk for a data risk. So this
# refuses loudly instead, leaving every existing backup intact.
# Two bands, so this DEGRADES instead of cliffing. A bare refuse-threshold set
# where the volume is already tight would have stopped tonight's backup on
# srilu (7,987 MB free, 7,012 MB database, 973 MB real headroom) — trading a
# silent disk risk for a silent backup gap, which is no better. The warn band
# reports thin headroom while still taking the backup; only genuine
# insufficiency refuses.
REQUIRED_MARGIN_MB="${GECKO_BACKUP_MIN_FREE_MARGIN_MB:-512}"
WARN_MARGIN_MB="${GECKO_BACKUP_WARN_FREE_MARGIN_MB:-2048}"
DB_SIZE_MB="$(( $(stat -c %s "$DB_PATH") / 1024 / 1024 ))"
FREE_MB="$(df -Pm "$GECKO_BACKUP_DIR" | awk 'NR==2 {print $4}')"
NEED_MB="$(( DB_SIZE_MB + REQUIRED_MARGIN_MB ))"
if [[ -z "$FREE_MB" ]]; then
    echo "ERROR: could not determine free space for $GECKO_BACKUP_DIR" >&2
    exit 2
fi
echo "gecko-backup-create: preflight db=${DB_SIZE_MB}MB free=${FREE_MB}MB need=${NEED_MB}MB (margin ${REQUIRED_MARGIN_MB}MB)"
WARN_MB="$(( DB_SIZE_MB + WARN_MARGIN_MB ))"
if (( FREE_MB >= NEED_MB && FREE_MB < WARN_MB )); then
    echo "WARNING: backup free space is thin — ${FREE_MB}MB free against a ${DB_SIZE_MB}MB database (comfortable would be >=${WARN_MB}MB)." >&2
    echo "  the backup WILL proceed; this is early notice, not a failure." >&2
fi
if (( FREE_MB < NEED_MB )); then
    echo "ERROR: insufficient free space for backup — refusing to start." >&2
    echo "  database ${DB_SIZE_MB}MB + margin ${REQUIRED_MARGIN_MB}MB = ${NEED_MB}MB needed, ${FREE_MB}MB free" >&2
    echo "  no backup was created and no existing backup was touched." >&2
    exit 6
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
#
# `immutable=1`, NOT a bare path, and not merely `mode=ro`. The backup inherits
# the source's WAL-mode header, so ANY ordinary open — read-only included —
# makes SQLite create `-shm` and `-wal` beside the file. Verified locally
# against sqlite 3.50.4: a `mode=ro` open of a freshly-`.backup`-ed WAL-header
# database produces both sidecars; the same open with `immutable=1` produces
# none and still returns `ok` from integrity_check and quick_check.
#
# On 2026-08-15 that mechanism cost prod all three of its backups: an operator
# quick_check over the two newest minted sidecars with fresh mtimes, and
# mtime-descending rotation retained the sidecars and deleted every real
# backup. Rotation now excludes those suffixes, but the durable fix is to not
# create them: `immutable=1` is correct here because a just-written `.backup`
# output is a complete, checkpointed database with nothing to replay.
#
# The URI is built with the path percent-escaped only where it must be. DEST is
# assembled from GECKO_BACKUP_DIR plus a timestamp, so `?` and `#` are the only
# realistic URI metacharacters; a literal `?` in an operator-set backup dir
# would otherwise truncate the path silently.
DEST_TMP_URI="file:$(printf '%s' "$DEST_TMP" | sed 's/?/%3f/g; s/#/%23/g')?mode=ro&immutable=1"
INTEGRITY="$("$SQLITE_BIN" "$DEST_TMP_URI" "PRAGMA integrity_check;" 2>&1 || true)"
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
