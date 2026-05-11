#!/usr/bin/env bash
# gecko-audit-snapshot - bash entrypoint for systemd ExecStart=
#
# Wraps `uv run python scripts/gecko_audit_snapshot.py` with env-driven
# config the systemd unit provides. Matches scripts/gecko-backup-rotate.sh
# pattern for consistency.
#
# Required env:
#   GECKO_REPO              - absolute path to gecko-alpha repo
#   GECKO_DB_PATH           - absolute path to scout.db
#   GECKO_AUDIT_SOAK_START  - ISO-8601 UTC timestamp (soak start)
#   GECKO_AUDIT_SOAK_END    - ISO-8601 UTC timestamp (soak end)
#   GECKO_AUDIT_HEARTBEAT_FILE - absolute path to heartbeat file
# Optional env:
#   GECKO_DISK_GATE_PATH       - path for df check (default /root)
#   GECKO_DISK_GATE_THRESHOLD_GB - minimum free GB (default 10)
#
# Exit codes:
#   0 = success
#   2 = misconfiguration (env vars missing or invalid)
#   3 = runtime error (propagates from Python CLI)
#   8 = disk gate failure (insufficient free space)
#   9 = disk gate parse failure (df output malformed)

set -euo pipefail

: "${GECKO_REPO:?ERROR: GECKO_REPO must be set}"
: "${GECKO_DB_PATH:?ERROR: GECKO_DB_PATH must be set}"
: "${GECKO_AUDIT_SOAK_START:?ERROR: GECKO_AUDIT_SOAK_START must be set}"
: "${GECKO_AUDIT_SOAK_END:?ERROR: GECKO_AUDIT_SOAK_END must be set}"
: "${GECKO_AUDIT_HEARTBEAT_FILE:?ERROR: GECKO_AUDIT_HEARTBEAT_FILE must be set}"

# R2-C1 hard gate: pre-run disk check.
# Threshold = 10G free at $GECKO_DISK_GATE_PATH (default /root).
# Locked 2026-05-11 after R2 design review found cohort generates ~177K rows
# day-1. On gate failure: abort + Telegram alert via direct-curl so operator
# finds out at gate time, not 6 hours later at watchdog cycle.

DISK_GATE_PATH="${GECKO_DISK_GATE_PATH:-/root}"
DISK_GATE_THRESHOLD_GB="${GECKO_DISK_GATE_THRESHOLD_GB:-10}"

free_gb=$(df -BG "$DISK_GATE_PATH" 2>/dev/null | tail -1 | awk '{print $4}' | sed 's/G//')
if [[ ! "$free_gb" =~ ^[0-9]+$ ]]; then
    echo "ERROR: disk gate could not parse df output for $DISK_GATE_PATH (got: $free_gb)" >&2
    exit 9
fi

if (( free_gb < DISK_GATE_THRESHOLD_GB )); then
    echo "DISK GATE FAILED: only ${free_gb}G free at $DISK_GATE_PATH, need ${DISK_GATE_THRESHOLD_GB}G" >&2

    # Direct-curl Telegram alert. Mirrors gecko-audit-snapshot-watchdog.sh alert path.
    # Skipped if env is missing creds; the watchdog path also fires at 10:00 UTC
    # and catches heartbeat-not-updated separately.
    ENV_FILE="${GECKO_ENV_FILE:-$GECKO_REPO/.env}"
    if [[ -f "$ENV_FILE" ]]; then
        TELEGRAM_BOT_TOKEN="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
        TELEGRAM_CHAT_ID="$(grep -E '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
        if [[ -n "$TELEGRAM_BOT_TOKEN" && "$TELEGRAM_BOT_TOKEN" != "placeholder" && -n "$TELEGRAM_CHAT_ID" && "$TELEGRAM_CHAT_ID" != "placeholder" ]]; then
            TEXT="⚠️ gecko-audit-snapshot: DISK GATE FAILED — only ${free_gb}G free at ${DISK_GATE_PATH}, need ${DISK_GATE_THRESHOLD_GB}G. Snapshot aborted; investigate disk pressure before next 04:00 UTC fire."
            PYTHON_BIN="$(command -v python3 || command -v python || true)"
            if [[ -n "$PYTHON_BIN" ]]; then
                PAYLOAD="$(GECKO_TG_TEXT="$TEXT" GECKO_TG_CHAT="$TELEGRAM_CHAT_ID" "$PYTHON_BIN" -c '
import json, os
print(json.dumps({"chat_id": os.environ["GECKO_TG_CHAT"], "text": os.environ["GECKO_TG_TEXT"]}))
')"
                curl -s -o /dev/null -w '' -X POST -H 'Content-Type: application/json' -d "$PAYLOAD" \
                    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" || true
            fi
        fi
    fi
    exit 8
fi

if [[ ! -d "$GECKO_REPO" ]]; then
    echo "ERROR: GECKO_REPO=$GECKO_REPO is not a directory" >&2
    exit 2
fi

cd "$GECKO_REPO"
exec uv run python scripts/gecko_audit_snapshot.py \
    --db-path "$GECKO_DB_PATH" \
    --soak-start "$GECKO_AUDIT_SOAK_START" \
    --soak-end "$GECKO_AUDIT_SOAK_END" \
    --heartbeat-file "$GECKO_AUDIT_HEARTBEAT_FILE"
