#!/usr/bin/env bash
# Weekly export of ledger_enrollment_evicted journal records to a durable
# append-only JSONL file — interim durability until BL-NEW-LEDGER-EVICTION-DB-MARKER
# lands (the journal is size-rotated; this file is the idempotent copy).
# Output format: journald envelope per line (-o json); the structlog event is in
# .MESSAGE; dedup at read time on (.__REALTIME_TIMESTAMP, .MESSAGE).
# Runbook: docs/runbook_deploy_ga_fixes_2026_07_02.md (deploy-#2 weekly steps).
set -euo pipefail
EXPORT_FILE="${LEDGER_EVICTION_EXPORT_FILE:-/var/lib/gecko-alpha/ledger_eviction_export.jsonl}"
mkdir -p "$(dirname "$EXPORT_FILE")"
matches="$(journalctl -u gecko-pipeline --since '-8 days' -o json 2>/dev/null | grep ledger_enrollment_evicted || true)"
n=0
if [ -n "$matches" ]; then
  printf '%s\n' "$matches" >> "$EXPORT_FILE"
  n="$(printf '%s\n' "$matches" | wc -l)"
fi
echo "ledger_eviction_export_run appended=$n file=$EXPORT_FILE"
