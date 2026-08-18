#!/usr/bin/env bash
# gecko-backup-offhost — ship the newest COMPLETED local scout.db backup to an
# operator-configured off-host destination.
#
# Round 20 motivation: R11/R13/R18/R19 produce, rotate, watchdog, and
# surface local backups well, but they all live on the same VPS
# filesystem as the live DB. A single filesystem failure (RAID die,
# accidental `rm -rf /`, mistyped fdisk) takes both the DB and every
# .bak with it. This script closes the gap by copying the newest .bak
# elsewhere — another mount, another host, an object store — on the same
# schedule.
#
# B2 amendment (2026-08-16): "another mount on the same provider" is still
# inside the provider's failure domain. The operator ruled Backblaze B2 the
# off-host destination precisely to leave it. B2 is S3-compatible OBJECT
# storage and rsync cannot speak to it at all — there is no shell on the far
# side, no rsync daemon, no filesystem. So the transfer step is now PLUGGABLE:
#
#   GECKO_OFFHOST_BACKUP_TRANSPORT=rsync  (default, unchanged behaviour)
#   GECKO_OFFHOST_BACKUP_TRANSPORT=s3     (rclone -> B2 / any S3-compatible)
#
# Everything ABOVE the transfer — the disabled-by-default gate, the flock
# guard, the completed-backup selection, the heartbeat — is shared, because
# those are the parts that are safety-critical and have already been paid for
# in incidents. Forking a parallel script would have duplicated exactly the
# selection logic that destroyed prod's backups twice.
#
# Required env (script no-ops if the destination is unset — opt-in by design):
#   rsync transport:
#     GECKO_OFFHOST_BACKUP_DEST  — rsync target spec. Anything rsync
#                                  accepts works: `user@host:/path/`,
#                                  `/mnt/external/backups/`, etc.
#                                  Empty/unset = disabled (exit 0).
#   s3 transport:
#     GECKO_OFFHOST_S3_BUCKET           — bucket name. Empty/unset = disabled
#                                         (exit 0). `GECKO_OFFHOST_BACKUP_DEST`
#                                         is IGNORED under this transport.
#     GECKO_OFFHOST_S3_ENDPOINT         — S3 endpoint URL, e.g.
#                                         https://s3.us-west-004.backblazeb2.com
#     GECKO_OFFHOST_S3_KEY_ID           — application key ID
#     GECKO_OFFHOST_S3_APPLICATION_KEY  — application key (SECRET)
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
#   GECKO_OFFHOST_S3_PREFIX             — key prefix inside the bucket
#                                         (default: bucket root)
#   GECKO_OFFHOST_S3_REGION             — S3 region, when the provider needs it
#   GECKO_OFFHOST_S3_PROVIDER           — rclone s3 `provider` value
#                                         (default "Other", correct for B2's
#                                         S3-compatible API)
#   GECKO_OFFHOST_S3_RCLONE_BIN         — override rclone binary (test seam)
#   GECKO_OFFHOST_MIN_BACKUP_BYTES      — plausibility floor for the selected
#                                         backup (default 512, the smallest a
#                                         valid SQLite database can be). Below
#                                         this, or missing the SQLite header,
#                                         the run aborts with exit 2 rather
#                                         than shipping a stub off-host.
#
# CREDENTIAL SCOPE: use a BUCKET-SCOPED application key, never an account-wide
# master key. On Backblaze that is "Add a New Application Key" restricted to
# this one bucket. The key this script holds can, by design, overwrite the
# off-host copies; it must not be able to touch anything else in the account.
#
# CREDENTIAL HANDLING: the key is passed to rclone through
# `RCLONE_CONFIG_<REMOTE>_*` environment variables exported inside a subshell,
# never on the command line. Command lines are world-readable via `ps`/`/proc`,
# environments are not. Any rclone output this script echoes is passed through
# a literal-substitution redactor first, so a future rclone that decides to
# echo its own configuration still cannot leak the key into the cron log.
#
# Exit codes:
#   0 = success (file shipped + VERIFIED + heartbeat written), OR the object
#       was already present and verified (idempotent re-run), OR disabled
#   2 = misconfiguration (no .bak found, the newest backup fails the
#       plausibility floor, can't create dirs, missing required env for the
#       selected transport, unknown transport)
#   3 = lock contention (another invocation in flight)
#   4 = transfer failed (rsync/rclone returned non-zero)
#   5 = POST-UPLOAD VERIFICATION FAILED (remote size and/or content hash does
#       not match the local file, or the remote hash could not be read at all)
#   6 = transport binary missing (rsync / rclone / md5sum)
#
# VERIFICATION IS NOT AN EXIT CODE. Under the s3 transport a zero exit from the
# upload command is NOT accepted as proof the bytes landed. After every upload
# the object's SIZE and CONTENT HASH are read back from the remote and compared
# to the local file, and a mismatch — or an unreadable remote hash — is a hard
# failure that does NOT write the heartbeat. This project has been burned
# repeatedly by treating "the command returned 0" as evidence; an off-host
# backup nobody has ever verified is indistinguishable from no backup at all,
# right up until the restore.
#
# The rsync transport keeps its historical behaviour (rsync does its own
# whole-file checksum on the wire); the read-back verification is specific to
# the object-storage path, where the upload is an HTTP request that can succeed
# while storing something else.
#
# Idempotency: rsync with --partial+--inplace makes re-runs cheap (only
# changed pages transfer). Under s3, a re-run first reads back the object and
# skips the upload entirely when size+hash already match, so the daily cron
# costs one metadata request once the day's backup is safe. Uploads land on a
# `.partial-upload` key and are promoted server-side only after verification,
# so an interrupted run can never leave a truncated object sitting under the
# real backup name.
#
# ---------------------------------------------------------------------------
# RESTORE PROCEDURE (s3/B2) — full detail in docs/runbook_offhost_backup_b2.md
# ---------------------------------------------------------------------------
#   1. List what is off-host:
#        rclone lsl "$REMOTE:$BUCKET/$PREFIX/"
#   2. Download the chosen object:
#        rclone copyto "$REMOTE:$BUCKET/$PREFIX/scout.db.bak.<ts>" ./restore.db
#   3. Verify it against the object store's own hash BEFORE trusting it:
#        md5sum ./restore.db
#        rclone lsjson --stat --hash "$REMOTE:$BUCKET/$PREFIX/scout.db.bak.<ts>"
#   4. Integrity-check the downloaded file:
#        sqlite3 "file:$PWD/restore.db?mode=ro&immutable=1" 'PRAGMA quick_check;'
#
#      *** `immutable=1` IS MANDATORY, NOT DECORATION. *** A backup carries the
#      live DB's WAL-mode header, so ANY ordinary open — read-only included —
#      makes SQLite mint `-wal` and `-shm` files beside it. On 2026-08-15 an
#      operator ran exactly this integrity check with a plain `mode=ro` URI;
#      the sidecars it created had fresh mtimes, mtime-descending rotation
#      handed them the retention slots, and all three real backups were
#      deleted. The integrity check destroyed the thing it was verifying.
#      Every read of a backup file uses `file:...?mode=ro&immutable=1`.
#   5. Only then promote: stop the pipeline, move the live scout.db aside,
#      move restore.db into place.

set -euo pipefail

_trim() {
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"  # ltrim
    s="${s%"${s##*[![:space:]]}"}"  # rtrim
    printf '%s' "$s"
}

TRANSPORT="$(_trim "${GECKO_OFFHOST_BACKUP_TRANSPORT:-rsync}")"
DEST="$(_trim "${GECKO_OFFHOST_BACKUP_DEST:-}")"

S3_BUCKET="$(_trim "${GECKO_OFFHOST_S3_BUCKET:-}")"
S3_ENDPOINT="$(_trim "${GECKO_OFFHOST_S3_ENDPOINT:-}")"
S3_KEY_ID="$(_trim "${GECKO_OFFHOST_S3_KEY_ID:-}")"
S3_APP_KEY="${GECKO_OFFHOST_S3_APPLICATION_KEY:-}"
S3_PREFIX="$(_trim "${GECKO_OFFHOST_S3_PREFIX:-}")"
S3_REGION="$(_trim "${GECKO_OFFHOST_S3_REGION:-}")"
S3_PROVIDER="$(_trim "${GECKO_OFFHOST_S3_PROVIDER:-Other}")"
RCLONE_BIN="${GECKO_OFFHOST_S3_RCLONE_BIN:-rclone}"

BACKUP_DIR="${GECKO_BACKUP_DIR:-/root/gecko-alpha}"
HEARTBEAT_FILE="${GECKO_OFFHOST_BACKUP_HEARTBEAT_FILE:-/var/lib/gecko-alpha/backup-rotation/offhost-last-ok}"
LOCK_FILE="${GECKO_OFFHOST_BACKUP_LOCK_FILE:-/var/lock/gecko-backup-offhost.lock}"
RSYNC_BIN="${GECKO_OFFHOST_BACKUP_BIN:-rsync}"
RSYNC_OPTS="${GECKO_OFFHOST_BACKUP_RSYNC_OPTS:---archive --partial --inplace}"

# rclone remote name. Defined ENTIRELY through RCLONE_CONFIG_GECKOOFFHOST_*
# environment variables (see _rclone below), which take precedence over any
# same-named remote in the operator's rclone.conf — so an unrelated ambient
# config cannot silently redirect the backups somewhere else.
S3_REMOTE="geckooffhost"

# Non-secret marker recording that this lane IS configured, for readers in
# OTHER PROCESSES.
#
# The dashboard renders `offhost_configured` to separate "the operator never
# enabled this" from "enabled but stale". It cannot read the shipper's
# configuration: the credentials live in a 0600 env file sourced only by the
# unit that runs THIS script, and putting that file on the dashboard's unit
# would hand a network-facing process the B2 application key to fix a display
# field. So the shipper publishes the one bit the dashboard needs — configured
# or not — and nothing else. The file holds the destination LABEL, never a
# credential.
#
# Written on every configured run and REMOVED on the disabled path, so an
# operator who un-configures the lane does not leave the dashboard asserting a
# destination that no longer exists.
CONFIGURED_MARKER="${GECKO_OFFHOST_CONFIGURED_MARKER:-/var/lib/gecko-alpha/backup-rotation/offhost-configured}"

_clear_configured_marker() {
    rm -f -- "$CONFIGURED_MARKER" 2>/dev/null || true
}

_write_configured_marker() {
    local tmp="${CONFIGURED_MARKER}.tmp.$$"
    if mkdir -p "$(dirname "$CONFIGURED_MARKER")" 2>/dev/null \
        && printf '%s\n' "$1" > "$tmp" 2>/dev/null; then
        mv -f "$tmp" "$CONFIGURED_MARKER" 2>/dev/null || rm -f "$tmp"
    fi
    # Best-effort by design: this marker drives a DISPLAY field. Failing the
    # backup because a cosmetic breadcrumb could not be written would trade a
    # wrong dashboard for a missing off-host copy.
    return 0
}

# Redact credential material out of any text before it reaches stdout/stderr.
# Pure bash parameter substitution: the secret is never an argument to another
# process (which `ps` would expose), unlike the obvious `sed "s/$KEY/.../"`.
_redact() {
    local text="$1"
    [[ -n "$S3_APP_KEY" ]] && text="${text//"$S3_APP_KEY"/[REDACTED]}"
    [[ -n "$S3_KEY_ID" ]] && text="${text//"$S3_KEY_ID"/[REDACTED]}"
    printf '%s' "$text"
}

# ---------------------------------------------------------------------------
# Transport selection + disabled-by-default gate
# ---------------------------------------------------------------------------
case "$TRANSPORT" in
    rsync)
        if [[ -z "$DEST" ]]; then
            echo "gecko-backup-offhost: GECKO_OFFHOST_BACKUP_DEST is empty — off-host backup disabled (set the env to enable)"
            _clear_configured_marker
            exit 0
        fi
        DEST_LABEL="$DEST"
        ;;
    s3)
        if [[ -z "$S3_BUCKET" ]]; then
            echo "gecko-backup-offhost: GECKO_OFFHOST_S3_BUCKET is empty — off-host backup disabled (set the env to enable)"
            _clear_configured_marker
            exit 0
        fi
        # Configured-but-incomplete is a MISCONFIGURATION, not a quiet skip:
        # once the operator has named a bucket they expect backups to leave the
        # box, and silently exiting 0 would satisfy the cron while shipping
        # nothing. Names only — never the values.
        MISSING=()
        [[ -z "$S3_ENDPOINT" ]] && MISSING+=("GECKO_OFFHOST_S3_ENDPOINT")
        [[ -z "$S3_KEY_ID" ]] && MISSING+=("GECKO_OFFHOST_S3_KEY_ID")
        [[ -z "$S3_APP_KEY" ]] && MISSING+=("GECKO_OFFHOST_S3_APPLICATION_KEY")
        if (( ${#MISSING[@]} > 0 )); then
            echo "ERROR: GECKO_OFFHOST_S3_BUCKET is set but required env is missing: ${MISSING[*]}" >&2
            exit 2
        fi
        S3_PREFIX="${S3_PREFIX#/}"
        S3_PREFIX="${S3_PREFIX%/}"
        if [[ -n "$S3_PREFIX" ]]; then
            DEST_LABEL="s3://${S3_BUCKET}/${S3_PREFIX}/"
        else
            DEST_LABEL="s3://${S3_BUCKET}/"
        fi
        ;;
    *)
        echo "ERROR: unknown GECKO_OFFHOST_BACKUP_TRANSPORT='$TRANSPORT' (expected 'rsync' or 's3')" >&2
        exit 2
        ;;
esac

# Configured. Publish the bit the dashboard needs BEFORE anything that can
# fail, so "the operator enabled this" is visible even on a run that then
# aborts — that is exactly the state the field must not render as "never
# enabled".
_write_configured_marker "$DEST_LABEL"

if [[ ! -d "$BACKUP_DIR" ]]; then
    echo "ERROR: GECKO_BACKUP_DIR=$BACKUP_DIR is not a directory" >&2
    exit 2
fi

if [[ "$TRANSPORT" == "rsync" ]]; then
    if ! command -v "$RSYNC_BIN" >/dev/null 2>&1; then
        echo "ERROR: rsync binary not found ($RSYNC_BIN). apt install rsync" >&2
        exit 6
    fi
else
    if ! command -v "$RCLONE_BIN" >/dev/null 2>&1; then
        echo "ERROR: rclone binary not found ($RCLONE_BIN). Install it with 'apt install rclone' or 'curl https://rclone.org/install.sh | sudo bash' — the s3 transport cannot run without it (rsync cannot speak to object storage, which is why this transport exists)." >&2
        exit 6
    fi
    if ! command -v md5sum >/dev/null 2>&1; then
        echo "ERROR: md5sum not found — post-upload verification is impossible without it, and an unverified off-host copy is not a backup. apt install coreutils" >&2
        exit 6
    fi
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

# ---------------------------------------------------------------------------
# Select the newest COMPLETED backup
# ---------------------------------------------------------------------------
# Shared by both transports on purpose. This is the single most incident-prone
# piece of the whole backup stack and it must not be reimplemented per
# destination.
NEWEST=""
NEWEST_MTIME=0
shopt -s nullglob
for pattern in 'scout.db.bak.*' 'scout.db.bak-*'; do
    for f in "$BACKUP_DIR"/$pattern; do
        [[ -f "$f" ]] || continue
        # Two distinct exclusion families, from two distinct incidents. This
        # loop selects NEWEST by mtime, and BOTH families are systematically
        # newer than the completed backup they sit next to — so either one can
        # win the selection and get shipped off-host as though it were a
        # backup, quietly replacing the real off-site copy with a stub.
        #
        # 1. `*.partial*` — the RESERVED IN-PROGRESS NAMESPACE owned by
        #    gecko-backup-create.sh: `$DEST.partial` plus every SQLite sidecar
        #    it can leave beside it. Five such files accumulated on prod
        #    (2026-08-08).
        #
        # 2. `*-wal` / `*-shm` / `*-journal` — sidecars minted beside a
        #    COMPLETED backup. A backup carries the live DB's WAL-mode header,
        #    so merely OPENING one creates them, read-only opens included. On
        #    2026-08-15 an operator's `PRAGMA quick_check` over the two newest
        #    backups minted sidecars with fresh mtimes and mtime-descending
        #    rotation deleted all three real backups. Their names contain no
        #    `.partial` at all, so family (1) did not cover them — which is
        #    exactly how this script was left exposed after that fix landed in
        #    gecko-backup-rotate.sh but not here.
        #
        # The sidecar patterns anchor to the END of the name, so an operator
        # tag that merely CONTAINS one of the words
        # (`scout.db.bak.before-wal-migration`) is still a backup and is still
        # shipped — the ad-hoc-tag workflow is deliberately not narrowed.
        case "$f" in
            *.partial*|*-wal|*-shm|*-journal) continue ;;
        esac
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

SIZE="$(stat -c '%s' "$NEWEST" 2>/dev/null || echo 0)"

# PLAUSIBILITY FLOOR — is the thing we selected actually a database?
#
# The sidecar exclusions above answer "is this file one of the artifact shapes
# we know about". They cannot answer "is this file a backup". A 0-byte
# `scout.db.bak.<tag>` newer than every real backup passes every exclusion, is
# selected, uploads fine, and VERIFIES — 0 == 0, and the md5 of nothing matches
# the md5 of nothing — then writes a green heartbeat. The lane would look
# healthy while the only copy outside the failure domain was a stub.
#
# gecko-backup-create.sh cannot produce that shape, but an interrupted ad-hoc
# `cp scout.db scout.db.bak.<tag>` can, and that hand-made workflow is one this
# script deliberately keeps supporting. So the file has to look like a SQLite
# database before it is allowed to become the off-site copy.
#
# KNOWN LIMIT, stated rather than implied: this catches empty and non-database
# stubs. It does NOT catch a `cp` interrupted late, which is already larger
# than the floor and still carries a valid header. Proving completeness would
# mean an integrity check over the whole multi-GB file on every run;
# gecko-backup-create.sh already integrity-checks what IT produces, and this
# floor is the cheap guard for the hand-made path.
# Trimmed like every other operator-set variable. An env file written by hand
# picks up trailing spaces easily, and `' 512 '` would otherwise fail the
# integer check and take the whole lane down with a misconfiguration error.
MIN_BACKUP_BYTES="$(_trim "${GECKO_OFFHOST_MIN_BACKUP_BYTES:-512}")"
if ! [[ "$MIN_BACKUP_BYTES" =~ ^[0-9]+$ ]]; then
    echo "ERROR: GECKO_OFFHOST_MIN_BACKUP_BYTES=$MIN_BACKUP_BYTES must be a non-negative integer" >&2
    exit 2
fi
# 15 bytes, not the full 16-byte `SQLite format 3\0` magic: bash command
# substitution strips NUL bytes, so the terminator cannot survive into a
# variable to be compared. The 15-character prefix is already unambiguous.
HEADER="$(head -c 15 "$NEWEST" 2>/dev/null || true)"
if (( SIZE < MIN_BACKUP_BYTES )) || [[ "$HEADER" != "SQLite format 3" ]]; then
    echo "ERROR: $NEWEST is the newest backup but does not look like a SQLite database (size=${SIZE}B, floor=${MIN_BACKUP_BYTES}B, header=$([[ "$HEADER" == "SQLite format 3" ]] && echo ok || echo 'not a SQLite header')). Refusing to ship it off-host: an empty or truncated stub would upload and VERIFY cleanly and then stand as the only copy outside this box. Check the backup directory by hand — an interrupted 'cp' leaves exactly this shape." >&2
    exit 2
fi

echo "gecko-backup-offhost: transport=$TRANSPORT source=$NEWEST size=$SIZE dest=$DEST_LABEL"

_write_heartbeat() {
    # Atomic heartbeat write (matches gecko-backup-create.sh pattern).
    # Reached ONLY after the copy is proven good — a heartbeat is the
    # watchdog's evidence that an off-host copy exists, so writing one for an
    # unverified upload would convert a real gap into a green dashboard.
    local hb_tmp="${HEARTBEAT_FILE}.tmp.$$"
    date +%s > "$hb_tmp"
    mv -f "$hb_tmp" "$HEARTBEAT_FILE"
    echo "gecko-backup-offhost: heartbeat updated at $HEARTBEAT_FILE"
}

# ---------------------------------------------------------------------------
# Transport: rsync (unchanged)
# ---------------------------------------------------------------------------
if [[ "$TRANSPORT" == "rsync" ]]; then
    # shellcheck disable=SC2086  # we intentionally word-split RSYNC_OPTS
    if ! "$RSYNC_BIN" $RSYNC_OPTS "$NEWEST" "$DEST" 2>&1; then
        echo "ERROR: rsync transfer failed (src=$NEWEST dest=$DEST)" >&2
        exit 4
    fi
    echo "gecko-backup-offhost: transfer ok ($NEWEST → $DEST)"
    # The local copy is NEVER removed. Off-host shipping is additive; local
    # keep-N retention belongs to gecko-backup-rotate.sh and stays independent.
    _write_heartbeat
    exit 0
fi

# ---------------------------------------------------------------------------
# Transport: s3 (rclone → Backblaze B2 or any S3-compatible endpoint)
# ---------------------------------------------------------------------------

# Credentials live only in this subshell's environment — not in argv, not in
# the parent's exported set.
_rclone() {
    (
        export RCLONE_CONFIG_GECKOOFFHOST_TYPE="s3"
        export RCLONE_CONFIG_GECKOOFFHOST_PROVIDER="$S3_PROVIDER"
        export RCLONE_CONFIG_GECKOOFFHOST_ACCESS_KEY_ID="$S3_KEY_ID"
        export RCLONE_CONFIG_GECKOOFFHOST_SECRET_ACCESS_KEY="$S3_APP_KEY"
        export RCLONE_CONFIG_GECKOOFFHOST_ENDPOINT="$S3_ENDPOINT"
        if [[ -n "$S3_REGION" ]]; then
            export RCLONE_CONFIG_GECKOOFFHOST_REGION="$S3_REGION"
        fi
        exec "$RCLONE_BIN" "$@"
    )
}

KEY_BASE="$(basename "$NEWEST")"
if [[ -n "$S3_PREFIX" ]]; then
    REMOTE_OBJ="${S3_REMOTE}:${S3_BUCKET}/${S3_PREFIX}/${KEY_BASE}"
else
    REMOTE_OBJ="${S3_REMOTE}:${S3_BUCKET}/${KEY_BASE}"
fi
# Uploads land here first. The suffix deliberately contains `.partial` so that
# if one of these objects is ever pulled back down into a backup directory the
# selection loop above skips it under the reserved-namespace rule.
REMOTE_TMP="${REMOTE_OBJ}.partial-upload"

LOCAL_SIZE="$(stat -c '%s' "$NEWEST")"
# Hash from STDIN, not by filename. `md5sum FILE` escapes its output line with
# a leading backslash whenever the path contains a backslash or a newline
# (GNU coreutils' quoting rule), which would silently prepend `\` to the digest
# and make every verification fail with a bogus mismatch. Feeding the file on
# stdin removes the filename from the output entirely.
LOCAL_MD5="$(md5sum < "$NEWEST" | awk '{print $1}')"
if [[ -z "$LOCAL_MD5" ]]; then
    echo "ERROR: could not compute local md5 for $NEWEST" >&2
    exit 2
fi

# Read back size + content hash for a remote object.
#   0 = present and MATCHES local
#   1 = absent, or its metadata could not be fetched at all
#   2 = present and PROVABLY WRONG (size or hash differs from local)
#   3 = present but UNPROVABLE (no readable size, or the remote reported no
#       hash) — we know nothing about the bytes either way
#
# 2 and 3 are separate return codes and not one "failed" bucket, because they
# license different ACTIONS, not just different messages. A provably-wrong
# object is garbage and deleting it loses nothing. An unprovable object may be
# a perfectly good backup that a backend simply would not checksum for us, and
# deleting it destroys a copy to punish our own ignorance. Neither is ever
# reported as verified.
_verify_remote() {
    local obj="$1" label="$2"
    local meta got_size got_md5
    # `--hash` without `--hash-type`: the runbook's stated rclone floor (1.59,
    # the version that introduced `--stat`) does not cover `--hash-type` on
    # lsjson, and an rclone that rejects the flag exits non-zero — which would
    # be read below as "cannot see it", discarding the real reason.
    #
    # Absence is decided by rclone's EXIT STATUS, and ONLY by the codes that
    # actually mean absence.
    #
    # A blanket "non-zero means absent" folds "the object is not there"
    # together with "the metadata request failed" — a network blip, a 5xx, an
    # expired key — and those demand opposite handling. The promoted-object
    # call site reports absence as "NO object exists … treat the off-host copy
    # as missing"; said about a temporary read failure that is a FALSE
    # STATEMENT about a present, byte-correct backup, and it sends the operator
    # away from an orphan object that nothing in the retention design will ever
    # prune.
    #
    # rclone documents 3 = directory not found and 4 = file not found. Those
    # two are absence. EVERY other non-zero code is UNPROVABLE (3), which keeps
    # the object and says so. The mapping fails in the safe direction: if some
    # rclone build reported not-found under a different code, a first-ever
    # upload would read "present but unverifiable" and re-upload — noisy, but
    # it never deletes, and it never claims something is missing that is not.
    #
    # VERIFY THE CODES AGAINST THE INSTALLED BINARY at enable time; the runbook
    # carries that step. Documentation is not the running program.
    local rc=0
    meta="$(_rclone lsjson --stat --hash "$obj" 2>/dev/null)" || rc=$?
    if (( rc != 0 )); then
        case "$rc" in
            3|4) return 1 ;;
            *)
                echo "ERROR: $label — the metadata request for $obj FAILED (rclone exit $rc). That is not evidence the object is absent; it is evidence we cannot see it." >&2
                return 3
                ;;
        esac
    fi
    meta="$(_trim "$meta")"
    if [[ -z "$meta" || "$meta" == "null" ]]; then
        return 1
    fi
    got_size="$(printf '%s' "$meta" | sed -n 's/.*"Size"[[:space:]]*:[[:space:]]*\(-\{0,1\}[0-9]\{1,\}\).*/\1/p' | head -n 1)"
    got_md5="$(printf '%s' "$meta" | sed -n 's/.*"md5"[[:space:]]*:[[:space:]]*"\([0-9a-fA-F]*\)".*/\1/p' | head -n 1)"

    if [[ -z "$got_size" ]]; then
        echo "ERROR: $label — the remote object exists but its size could not be read back; refusing to call it verified" >&2
        return 3
    fi
    if (( got_size != LOCAL_SIZE )); then
        echo "ERROR: $label — SIZE MISMATCH: local=$LOCAL_SIZE remote=$got_size ($obj)" >&2
        return 2
    fi
    if [[ -z "$got_md5" ]]; then
        # Not a pass, but not a mismatch either. A remote that cannot report a
        # whole-object hash gives us no way to distinguish a good copy from a
        # same-length corrupt one, and "the sizes matched" is the weakest
        # possible restore guarantee. Return 3, so callers that would delete a
        # provably-bad object leave this one alone.
        echo "ERROR: $label — the remote reported NO md5 for the object, so its contents cannot be verified (size matched at $got_size bytes, which is not sufficient)" >&2
        return 3
    fi
    # Case-fold before comparing. md5sum emits lowercase, but the hash comes
    # back from whatever the backend chose to send; an uppercase digest would
    # otherwise mismatch forever, under a message that falsely swears the
    # remote "stored different bytes".
    got_md5="${got_md5,,}"
    if [[ "$got_md5" != "${LOCAL_MD5,,}" ]]; then
        echo "ERROR: $label — CONTENT HASH MISMATCH: local md5=$LOCAL_MD5 remote md5=$got_md5 (size matched at $got_size bytes — the upload returned success and stored different bytes)" >&2
        return 2
    fi
    return 0
}

# Best-effort delete. Reports whether it actually worked rather than letting
# the caller's "Removing the staging key" read as an accomplished fact — a
# delete that failed leaves an orphan object accruing storage cost, and the
# operator can only chase it if the log says so.
_delete_remote() {
    local obj="$1" out="" rc=0
    out="$(_rclone deletefile "$obj" 2>&1)" || rc=$?
    if (( rc != 0 )); then
        echo "WARNING: could not delete $obj (rclone exited $rc) — an orphan object may remain off-host and will need removing by hand" >&2
        [[ -n "$out" ]] && printf '%s\n' "$(_redact "$out")" >&2
        return 0
    fi
    [[ -n "$out" ]] && printf 'gecko-backup-offhost: %s\n' "$(_redact "$out")"
    echo "gecko-backup-offhost: removed $obj"
    return 0
}

# --- idempotence: is the day's object already up there and provably correct?
set +e
_verify_remote "$REMOTE_OBJ" "pre-upload check"
PRE_RC=$?
set -e
if (( PRE_RC == 0 )); then
    echo "gecko-backup-offhost: $KEY_BASE already present off-host and verified (size=$LOCAL_SIZE md5=$LOCAL_MD5) — skipping upload"
    _write_heartbeat
    exit 0
fi
if (( PRE_RC == 2 )); then
    echo "gecko-backup-offhost: a remote object already exists at $REMOTE_OBJ but does NOT match the local backup — re-uploading over it" >&2
elif (( PRE_RC == 3 )); then
    echo "gecko-backup-offhost: a remote object already exists at $REMOTE_OBJ but could not be verified (see the reason above) — re-uploading to replace it with a copy we can prove" >&2
fi

# --- upload to the staging key
echo "gecko-backup-offhost: uploading $KEY_BASE → $DEST_LABEL (staging key .partial-upload)"
UPLOAD_OUT=""
if ! UPLOAD_OUT="$(_rclone copyto "$NEWEST" "$REMOTE_TMP" 2>&1)"; then
    echo "ERROR: rclone upload failed (src=$NEWEST dest=$DEST_LABEL)" >&2
    printf '%s
' "$(_redact "$UPLOAD_OUT")" >&2
    _delete_remote "$REMOTE_TMP"
    exit 4
fi
[[ -n "$UPLOAD_OUT" ]] && printf '%s
' "$(_redact "$UPLOAD_OUT")"

# --- verify the staged object BEFORE it is allowed to carry the backup's name
set +e
_verify_remote "$REMOTE_TMP" "staged upload"
STAGE_RC=$?
set -e
if (( STAGE_RC != 0 )); then
    # "could not be verified", NOT "does not match". The specific reason is
    # already on the line above, and one of the three reasons is that the
    # remote reported no hash — in which case the object may well be fine and
    # we simply cannot prove it. Summarising that as a mismatch would state as
    # fact something this script does not know.
    echo "ERROR: post-upload verification FAILED for the staged object — rclone exited 0 but the stored object could not be verified against the local backup (see the reason above). Removing the staging key; the real backup name was never written, so nothing unverified is being presented as good." >&2
    _delete_remote "$REMOTE_TMP"
    exit 5
fi
echo "gecko-backup-offhost: staged object verified (size=$LOCAL_SIZE md5=$LOCAL_MD5)"

# --- promote server-side
MOVE_OUT=""
if ! MOVE_OUT="$(_rclone moveto "$REMOTE_TMP" "$REMOTE_OBJ" 2>&1)"; then
    echo "ERROR: rclone could not promote the staged object to $REMOTE_OBJ" >&2
    printf '%s
' "$(_redact "$MOVE_OUT")" >&2
    _delete_remote "$REMOTE_TMP"
    exit 4
fi
[[ -n "$MOVE_OUT" ]] && printf '%s
' "$(_redact "$MOVE_OUT")"

# --- verify the object AS PRESENTED. The promotion is a server-side copy, so
# the bytes and the hash metadata it carries are the remote's word, not ours;
# the authoritative check is the one against the name a restore would fetch.
set +e
_verify_remote "$REMOTE_OBJ" "promoted object"
FINAL_RC=$?
set -e
if (( FINAL_RC == 2 )); then
    # PROVABLY wrong: the bytes up there are not the backup. Deleting loses
    # nothing and stops a known-bad object sitting under a name a restore
    # would trust.
    echo "ERROR: the promoted object at $REMOTE_OBJ is PROVABLY WRONG. Deleting it rather than leaving a bad copy under a good name, and NOT writing the heartbeat — the watchdog must see this as a missing off-host backup." >&2
    _delete_remote "$REMOTE_OBJ"
    exit 5
fi
if (( FINAL_RC == 1 )); then
    # Not "unprovable" — ABSENT. rclone reported a successful move and the
    # object is not there. Saying it is "being left in place" would describe an
    # object that does not exist, and would send the operator looking for
    # something to verify by hand.
    echo "ERROR: rclone reported a successful promotion but NO object exists at $REMOTE_OBJ. Nothing was left off-host by this run and the heartbeat is NOT written; treat the off-host copy as missing." >&2
    exit 5
fi
if (( FINAL_RC != 0 )); then
    # UNPROVABLE, not wrong — and the object is KEPT.
    #
    # These same bytes passed the staged verify seconds ago; what failed here
    # is the read-back, typically a promotion that did not carry the hash
    # metadata forward or a metadata request that errored. Deleting on that
    # basis destroys a copy that is probably fine in order to punish our own
    # ignorance, and it buys nothing: the heartbeat is withheld either way, so
    # the watchdog pages identically. On a >5 GiB object, where the promotion
    # is a multipart server-side copy that may legitimately drop the
    # Md5chksum metadata, the deleting version would also re-upload the whole
    # backup every single night.
    echo "ERROR: the promoted object at $REMOTE_OBJ could not be VERIFIED (see the reason above). It is being LEFT IN PLACE — it may well be a good copy that the remote would not checksum, and deleting an unproven object destroys a backup to punish our own ignorance. The heartbeat is NOT written, so the watchdog still reports this lane as broken; verify or remove the object by hand." >&2
    exit 5
fi

echo "gecko-backup-offhost: transfer ok and VERIFIED ($NEWEST → $DEST_LABEL$KEY_BASE, size=$LOCAL_SIZE md5=$LOCAL_MD5)"
# The local copy is NEVER removed on upload success. Off-host shipping is
# additive; local keep-N retention belongs to gecko-backup-rotate.sh and stays
# independent of whether this script ran at all.
_write_heartbeat
