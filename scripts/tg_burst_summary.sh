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
    # Pull last N week-files; jq filters by event types.
    ARCHIVE_EVENTS=$(find "$ARCHIVE_DIR" -name '*.jsonl.gz' -mtime "-$((HOURS / 24 + 1))" 2>/dev/null \
        | xargs -r zcat 2>/dev/null \
        | jq -c 'select(.event | test("tg_dispatch_observed|tg_burst_observed|tg_dispatch_rejected_429"))' 2>/dev/null \
        || true)
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
