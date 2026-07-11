#!/usr/bin/env bash
# wal_archive.sh — dump SQLite WAL probe events to disk weekly.
# Insurance against journalctl rotation (sqlite_wal_probe is DEBUG-level
# and may be cleared aggressively under default retention).
# Install: weekly cron under ROOT crontab on srilu — service unit is
# root-owned, journalctl -u gecko-pipeline requires root or
# systemd-journal group membership.
#   45 3 * * 0 /root/gecko-alpha/scripts/wal_archive.sh
set -euo pipefail

# ARCHIVE_DIR is env-overridable for isolated testing; prod default unchanged.
ARCHIVE_DIR="${WAL_ARCHIVE_DIR:-/var/log/gecko-alpha/wal-archive}"
mkdir -p "$ARCHIVE_DIR"
chmod 0755 "$ARCHIVE_DIR"

# Same-day re-runs append a numeric suffix (mirrors tg_burst_archive.sh).
BASE="$ARCHIVE_DIR/$(date +%Y-%m-%d).jsonl.gz"
OUT="$BASE"
if [[ -e "$OUT" ]]; then
    i=2
    while [[ -e "$ARCHIVE_DIR/$(date +%Y-%m-%d).${i}.jsonl.gz" ]]; do
        i=$((i + 1))
    done
    OUT="$ARCHIVE_DIR/$(date +%Y-%m-%d).${i}.jsonl.gz"
fi

# 2-week window with overlap so a missed weekly run self-recovers next week
# (mirrors tg_burst_archive.sh V16 SHOULD-FIX #3).
#
# REC-05b: capture into a variable FIRST so an empty result fails LOUDLY
# instead of silently writing a valid-but-empty gzip. Root cause of the
# 0-byte-since-2026-05-31 regression: sqlite_wal_probe is DEBUG-level, so when
# journald debug retention has rotated it out (or the app log level is above
# DEBUG) `journalctl -p debug` returns nothing, grep matches nothing (exit 1),
# and under `set -o pipefail` the old inline `... | gzip > "$OUT"` STILL let
# gzip write an empty-gzip archive that decompressed to 0 bytes — a silent
# "captured nothing" masquerading as success. `|| true` keeps grep's no-match
# exit from aborting the capture mid-pipeline; the emptiness check below turns
# a no-capture into a non-zero exit with a stderr diagnosis.
EVENTS=$(journalctl -u gecko-pipeline -p debug --since "2 weeks ago" 2>/dev/null \
    | grep -E '"event": "(sqlite_wal_probe|sqlite_wal_bloat_observed|sqlite_wal_probe_failed)"' \
    || true)

if [[ -z "$EVENTS" ]]; then
    {
        echo "wal_archive.sh: FATAL — no sqlite_wal_probe events in the last 2 weeks."
        echo "  'journalctl -u gecko-pipeline -p debug' returned nothing matching."
        echo "  Likely causes:"
        echo "    1. sqlite_wal_probe is DEBUG-level and journald debug retention"
        echo "       rotated it out — widen retention or shorten archive cadence."
        echo "    2. the app structlog level is above DEBUG so the probe is never"
        echo "       emitted at all."
        echo "    3. gecko-pipeline is not running / not logging."
        echo "  Refusing to write an empty archive (would masquerade as success)."
    } >&2
    exit 1
fi

printf '%s\n' "$EVENTS" | gzip > "$OUT"
chmod 0644 "$OUT"

# Rotate by filename-date, not mtime (rsync/backup can touch mtimes).
# 8-week retention.
cutoff_epoch=$(date -d "56 days ago" +%s)
for f in "$ARCHIVE_DIR"/*.jsonl.gz; do
    [[ -f "$f" ]] || continue
    fname=$(basename "$f" .jsonl.gz)
    base="${fname%.*}"
    if [[ "$base" == "$fname" ]]; then
        date_str="$fname"
    else
        date_str="$base"
    fi
    file_epoch=$(date -d "$date_str" +%s 2>/dev/null || echo 0)
    if [[ "$file_epoch" -gt 0 && "$file_epoch" -lt "$cutoff_epoch" ]]; then
        rm -f "$f"
    fi
done
