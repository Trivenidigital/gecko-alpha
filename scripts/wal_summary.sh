#!/usr/bin/env bash
# wal_summary.sh — summarize sqlite_wal_probe / sqlite_wal_bloat_observed
# events from journalctl + archive. Mirrors tg_burst_summary.sh pattern.
# Note: sqlite_wal_probe is DEBUG-level — requires journald debug retention
# OR the archive script's output.
# Usage: ./scripts/wal_summary.sh [hours-back]   (default: 168 = 1 week)
# Requires: jq.
#
# D5b reminder: shm_size_bytes is the -shm shared-memory sidecar, NOT WAL
# bloat. The bloat-trigger threshold reads wal_size_bytes only.
# D5 (V23 M2 fold): Week-1 baseline drops the first probe after each
# process start (gap >90min between consecutive probes).
set -euo pipefail

HOURS="${1:-168}"
SINCE="${HOURS} hours ago"
ARCHIVE_DIR="/var/log/gecko-alpha/wal-archive"

echo "=== SQLite WAL probe summary, last ${HOURS}h ==="
echo "NOTE: shm_size_bytes is informational (NOT bloat); wal_size_bytes drives the trigger."
echo

JOURNAL_EVENTS=$(journalctl -u gecko-pipeline --since "$SINCE" 2>/dev/null \
    | grep -E '"event": "(sqlite_wal_probe|sqlite_wal_bloat_observed|sqlite_wal_probe_failed)"' \
    || true)
ARCHIVE_EVENTS=""
if [[ -d "$ARCHIVE_DIR" ]]; then
    cutoff_epoch=$(date -d "$HOURS hours ago" +%s 2>/dev/null || echo 0)
    if [[ "$cutoff_epoch" -eq 0 ]]; then
        # V24 SHOULD-FIX: don't silently expand the archive window. A
        # cutoff_epoch=0 would include every archive file regardless of
        # the requested HOURS — the operator must see this degradation.
        echo "WARN: cutoff parse failed for '$HOURS hours ago'; archive window UNBOUNDED" >&2
    fi
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
            | jq -c 'select(.event | test("sqlite_wal_probe|sqlite_wal_bloat_observed|sqlite_wal_probe_failed"))' 2>/dev/null \
            || true)
    fi
fi
# Post-cycle-4 review fix: dedup journal + archive overlap. wal_archive.sh
# uses a rolling 2-week window for missed-run self-recovery, so events that
# are still in journald retention ALSO appear in the latest archive .jsonl.gz.
# Without dedup, downstream aggregators (PROBES/BLOATS counts, consecutive-
# bloat-run-length, p95 baseline) inflate by ~2x for the journal-AND-archive
# overlap region and produce false tuning recommendations. Dedup key is the
# JSON `"timestamp"` field — structlog writes ISO-8601 with microseconds so
# collisions across distinct events are negligible. awk first-seen filter
# avoids the jq slurp-into-memory cost on multi-week windows.
COMBINED=$(printf "%s\n%s" "$JOURNAL_EVENTS" "$ARCHIVE_EVENTS" | grep -v '^$' \
    | awk '
        match($0, /"timestamp": "[^"]+"/) {
            ts = substr($0, RSTART, RLENGTH)
            # Reviewer-non-blocking tightening: include event name in the
            # dedup key so two distinct event types emitted in the same
            # microsecond do not collapse. structlog writes both fields
            # in both journal and archive line shapes.
            ev = ""
            if (match($0, /"event": "[^"]+"/)) {
                ev = substr($0, RSTART, RLENGTH)
            }
            key = ts "|" ev
            if (!seen[key]++) print $0
            next
        }
        # Lines without a timestamp field (defensive — should not occur for
        # gecko-pipeline structlog) pass through unfiltered.
        { print $0 }
    ' || true)

if [[ -z "$COMBINED" ]]; then
    echo "(no events in window — is journald debug retention on? sqlite_wal_probe is DEBUG-level)"
    exit 0
fi

PROBES=$(printf "%s\n" "$COMBINED" | grep -c '"event": "sqlite_wal_probe"' || true)
BLOATS=$(printf "%s\n" "$COMBINED" | grep -c '"event": "sqlite_wal_bloat_observed"' || true)
FAILS=$(printf "%s\n" "$COMBINED" | grep -c '"event": "sqlite_wal_probe_failed"' || true)

echo "Probes: $PROBES"
echo "Bloat events (wal_size_bytes > SQLITE_WAL_BLOAT_BYTES): $BLOATS"
echo "Probe failures: $FAILS"
echo

# D6: longest STRICTLY-consecutive run of bloat events
# The hourly probe is the cohort. We extract per-probe wal_size_bytes
# and the bloat threshold, then walk the time-ordered stream computing
# max consecutive-run-length where wal_size_bytes > threshold.
if [[ "$PROBES" -gt 0 ]]; then
    echo "--- Consecutive-bloat-run aggregator (D6 strict criterion) ---"
    printf "%s\n" "$COMBINED" \
        | grep '"event": "sqlite_wal_probe"' \
        | jq -r '"\(.timestamp) \(.wal_size_bytes) \(.threshold_bytes // 50000000)"' 2>/dev/null \
        | sort \
        | awk '
            { ts=$1; wal=$2; thresh=$3;
              if (wal+0 > thresh+0) {
                run++;
                if (run > max_run) { max_run = run; max_end = ts; }
              } else {
                run = 0;
              }
            }
            END {
              if (max_run == "") max_run = 0;
              printf "max consecutive bloat-run length: %s probes\n", max_run+0;
              if (max_run+0 > 0) printf "  ending at: %s\n", max_end;
              if (max_run+0 >= 12) print "  >= 12: TUNE criterion MET";
              else print "  < 12: TUNE criterion NOT met";
            }'
    echo
fi

# Runaway-WAL single-event check
if [[ "$PROBES" -gt 0 ]]; then
    echo "--- Runaway-WAL check (any single probe > 500MB → TUNE-IMMEDIATELY) ---"
    printf "%s\n" "$COMBINED" \
        | grep '"event": "sqlite_wal_probe"' \
        | jq -r 'select(.wal_size_bytes > 524288000) | "\(.timestamp) wal=\(.wal_size_bytes) pages=\(.wal_pages)"' 2>/dev/null \
        | head -10 \
        | (read -r line && echo "RUNAWAY DETECTED:" && echo "$line" && cat) || echo "(none observed)"
    echo
fi

# DB fragmentation single-event check
if [[ "$PROBES" -gt 0 ]]; then
    echo "--- DB fragmentation (freelist_count > 0.10 × page_count on any single probe) ---"
    printf "%s\n" "$COMBINED" \
        | grep '"event": "sqlite_wal_probe"' \
        | jq -r 'select(.freelist_count > (.page_count * 0.10)) | "\(.timestamp) freelist=\(.freelist_count) pages=\(.page_count) ratio=\((.freelist_count / .page_count) * 100 | floor)%"' 2>/dev/null \
        | head -5 \
        | (read -r line && echo "FRAG DETECTED:" && echo "$line" && cat) || echo "(none observed)"
    echo
fi

# Bloat events (raw)
if [[ "$BLOATS" -gt 0 ]]; then
    echo "--- Bloat events (raw) ---"
    printf "%s\n" "$COMBINED" \
        | grep '"event": "sqlite_wal_bloat_observed"' \
        | jq -r '"\(.timestamp) wal=\(.wal_size_bytes) threshold=\(.threshold_bytes) pages=\(.wal_pages)"' 2>/dev/null \
        | head -20
    echo
fi

# Probe failures (raw)
if [[ "$FAILS" -gt 0 ]]; then
    echo "--- Probe failures (raw) ---"
    printf "%s\n" "$COMBINED" \
        | grep '"event": "sqlite_wal_probe_failed"' \
        | jq -r '"\(.timestamp) \(.exception // "unknown")"' 2>/dev/null \
        | head -10
    echo
fi

# Week-1 baseline calibration (D5 + D5 V23 M2 fold)
# Drop first probe-event after each process start (gap > 90 minutes
# between consecutive probes signals restart-bracket boundary).
echo "--- Week-1 baseline calibration (D5 V23 M2 fold) ---"
echo "Drops first probe after each process-restart boundary (gap >90min)."

BASELINE_LINE=$(printf "%s\n" "$COMBINED" \
    | grep '"event": "sqlite_wal_probe"' \
    | jq -r '"\(.timestamp) \(.wal_size_bytes)"' 2>/dev/null \
    | sort \
    | awk '
        function ts_to_epoch(ts,   cmd, out) {
          cmd = "date -d \"" ts "\" +%s 2>/dev/null"
          cmd | getline out
          close(cmd)
          return out+0
        }
        BEGIN { prev_epoch = 0; restart_count = 0; }
        { ts=$1; wal=$2; epoch=ts_to_epoch(ts);
          if (prev_epoch > 0 && (epoch - prev_epoch) > 5400) {
            # gap > 90 min: this row is restart-bracket; drop it
            restart_count++;
          } else if (prev_epoch > 0) {
            samples[++n] = wal+0;
          } else {
            # very first sample is also restart-bracket
            restart_count++;
          }
          prev_epoch = epoch;
        }
        END {
          if (n == 0) {
            printf "n=0 (need at least 2 probes after dropping restart-brackets; restart_count=%d)\n", restart_count;
            exit 0;
          }
          # sort samples ascending
          for (i = 1; i <= n; i++) {
            for (j = i+1; j <= n; j++) {
              if (samples[j] < samples[i]) {
                t = samples[i]; samples[i] = samples[j]; samples[j] = t;
              }
            }
          }
          p50_idx = int((n+1) * 0.50); if (p50_idx < 1) p50_idx = 1;
          p95_idx = int((n+1) * 0.95); if (p95_idx < 1) p95_idx = 1;
          if (p95_idx > n) p95_idx = n;
          p50 = samples[p50_idx];
          p95 = samples[p95_idx];
          # round 1.5×p95 up to nearest 5MB
          suggested = int((p95 * 1.5) / 5000000 + 1) * 5000000;
          if (suggested < 50000000) suggested = 50000000;  # floor at default
          printf "n=%d clean samples (dropped %d restart-bracket)\n", n, restart_count;
          printf "  p50 wal_size_bytes: %d (%.1f MB)\n", p50, p50/1048576.0;
          printf "  p95 wal_size_bytes: %d (%.1f MB)\n", p95, p95/1048576.0;
          printf "  suggested SQLITE_WAL_BLOAT_BYTES (~1.5×p95 rounded to 5MB, floor 50MB):\n";
          printf "    SQLITE_WAL_BLOAT_BYTES=%d  # %.1f MB\n", suggested, suggested/1048576.0;
          if (restart_count > 1 && n < 100) {
            printf "  WARN: %d process restarts in window; if any during Week 1, re-run after 168 clean probes (data-bound per CLAUDE.md §11)\n", restart_count;
          }
        }')
echo "$BASELINE_LINE"
