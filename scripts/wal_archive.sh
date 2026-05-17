#!/usr/bin/env bash
# wal_archive.sh — dump SQLite WAL probe events to disk weekly.
# Insurance against journalctl rotation (sqlite_wal_probe is DEBUG-level
# and may be cleared aggressively under default retention).
# Install: weekly cron under ROOT crontab on srilu — service unit is
# root-owned, journalctl -u gecko-pipeline requires root or
# systemd-journal group membership.
#   45 3 * * 0 /root/gecko-alpha/scripts/wal_archive.sh
set -euo pipefail

ARCHIVE_DIR="/var/log/gecko-alpha/wal-archive"
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
journalctl -u gecko-pipeline -p debug --since "2 weeks ago" 2>/dev/null \
    | grep -E '"event": "(sqlite_wal_probe|sqlite_wal_bloat_observed|sqlite_wal_probe_failed)"' \
    | gzip > "$OUT"
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
