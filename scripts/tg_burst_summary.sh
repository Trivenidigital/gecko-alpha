#!/usr/bin/env bash
# tg_burst_summary.sh — summarize TG dispatch from journalctl + archive.
# V14 fold: time-of-day histogram + top-K callsites + 429 correlation.
# Note: `tg_dispatch_observed` is DEBUG-level (V13 fold) — requires
# either journald set to debug retention OR the archive script's output.
# Usage: ./scripts/tg_burst_summary.sh [hours-back]   (default: 168 = 1 week)
# Requires: jq.
set -euo pipefail

HOURS="${1:-168}"
SINCE="${HOURS} hours ago"
ARCHIVE_DIR="/var/log/gecko-alpha/tg-burst-archive"

echo "=== TG dispatch summary, last ${HOURS}h ==="
echo

# Build the combined event stream from journalctl + any archive files
# that fall within the window. Archive files are jsonl.gz, one event per line.
JOURNAL_EVENTS=$(journalctl -u gecko-pipeline --since "$SINCE" 2>/dev/null \
    | grep -E '"event": "(tg_dispatch_observed|tg_burst_observed|tg_dispatch_rejected_429)"' \
    || true)
ARCHIVE_EVENTS=""
if [[ -d "$ARCHIVE_DIR" ]]; then
    # V17 PR-review NICE-TO-HAVE #2: filter archives by FILENAME-DATE
    # (consistent with tg_burst_archive.sh rotation discipline).
    # mtime filtering is broken by rsync/backup mtime touching.
    cutoff_epoch=$(date -d "$HOURS hours ago" +%s 2>/dev/null || echo 0)
    selected_files=()
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
        if [[ "$file_epoch" -gt "$cutoff_epoch" ]]; then
            selected_files+=("$f")
        fi
    done
    if [[ ${#selected_files[@]} -gt 0 ]]; then
        ARCHIVE_EVENTS=$(zcat "${selected_files[@]}" 2>/dev/null \
            | jq -c 'select(.event | test("tg_dispatch_observed|tg_burst_observed|tg_dispatch_rejected_429"))' 2>/dev/null \
            || true)
    fi
fi
COMBINED=$(printf "%s\n%s" "$JOURNAL_EVENTS" "$ARCHIVE_EVENTS" | grep -v '^$' || true)

if [[ -z "$COMBINED" ]]; then
    echo "(no events in window)"
    exit 0
fi

OBSERVED=$(printf "%s\n" "$COMBINED" | grep -c '"event": "tg_dispatch_observed"' || true)
BURST=$(printf "%s\n" "$COMBINED" | grep -c '"event": "tg_burst_observed"' || true)
REJECTED=$(printf "%s\n" "$COMBINED" | grep -c '"event": "tg_dispatch_rejected_429"' || true)

echo "Dispatches: $OBSERVED"
echo "Bursts (threshold breach): $BURST"
echo "429s from Telegram (firm pacing trigger): $REJECTED"
echo

# V14 fold: time-of-day histogram (which hours cluster bursts?)
if [[ "$BURST" -gt 0 || "$REJECTED" -gt 0 ]]; then
    echo "--- Burst+429 events by hour-of-day ---"
    printf "%s\n" "$COMBINED" \
        | jq -r 'select(.event != "tg_dispatch_observed") | .timestamp' 2>/dev/null \
        | awk -F'T' '{print substr($2,1,2)}' \
        | sort | uniq -c | sort -k2n \
        | awk '{ printf "%s:00  %s\n", $2, $1 }'
    echo
fi

if [[ "$REJECTED" -gt 0 ]]; then
    echo "--- 429 events (firm pacing trigger) ---"
    printf "%s\n" "$COMBINED" \
        | grep '"event": "tg_dispatch_rejected_429"' \
        | jq -r '"\(.timestamp) chat=\(.chat_id) source=\(.source) retry_after=\(.retry_after // "null")"' 2>/dev/null \
        | head -20
    echo
fi

if [[ "$BURST" -gt 0 ]]; then
    echo "--- Top callsites (source) by burst contribution ---"
    printf "%s\n" "$COMBINED" \
        | grep '"event": "tg_burst_observed"' \
        | jq -r '.source // "unattributed"' 2>/dev/null \
        | sort | uniq -c | sort -rn | head -10
    echo
fi

echo "--- Top callsites by total dispatch count ---"
printf "%s\n" "$COMBINED" \
    | grep '"event": "tg_dispatch_observed"' \
    | jq -r '.source // "unattributed"' 2>/dev/null \
    | sort | uniq -c | sort -rn | head -10
