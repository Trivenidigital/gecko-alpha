#!/usr/bin/env bash
# tg_burst_archive.sh — dump TG burst events to disk weekly.
# Insurance against journalctl rotation under burst load (V14 fold).
# Install: weekly cron under ROOT crontab on srilu — service unit is
# root-owned, journalctl -u gecko-pipeline requires root or
# systemd-journal group membership.
#   30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh
set -euo pipefail

ARCHIVE_DIR="/var/log/gecko-alpha/tg-burst-archive"
mkdir -p "$ARCHIVE_DIR"
chmod 0755 "$ARCHIVE_DIR"
OUT="$ARCHIVE_DIR/$(date +%Y-%m-%d).jsonl.gz"

# V16 SHOULD-FIX #3 fold: 2-week window with overlap so a missed weekly
# run self-recovers next week. Storage cost ~1 MB extra per week.
journalctl -u gecko-pipeline --since "2 weeks ago" 2>/dev/null \
    | grep -E '"event": "(tg_dispatch_observed|tg_burst_observed|tg_dispatch_rejected_429)"' \
    | gzip > "$OUT"
chmod 0644 "$OUT"

# V16 NICE-TO-HAVE #5 fold: rotate by filename-date, not mtime.
# rsync/backup tools can touch mtimes; filename is the authoritative cohort.
cutoff_epoch=$(date -d "56 days ago" +%s)
for f in "$ARCHIVE_DIR"/*.jsonl.gz; do
    [[ -f "$f" ]] || continue
    base=$(basename "$f" .jsonl.gz)
    file_epoch=$(date -d "$base" +%s 2>/dev/null || echo 0)
    if [[ "$file_epoch" -gt 0 && "$file_epoch" -lt "$cutoff_epoch" ]]; then
        rm -f "$f"
    fi
done
