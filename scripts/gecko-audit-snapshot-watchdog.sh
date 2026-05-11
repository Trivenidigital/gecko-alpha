#!/usr/bin/env bash
# gecko-audit-snapshot-watchdog — alert if snapshot hasn't run successfully in 30h.
#
# 30h window = daily timer at 04:00 UTC + 6h grace for late fires. Watchdog
# itself runs at 10:00 UTC, so worst case is heartbeat is 30h stale (yesterday
# 04:00 → today 10:00). If heartbeat is older than 30h, snapshot has missed
# at least one cycle.
#
# Telegram delivery is direct curl, NOT via scout.alerter, per R6 CRITICAL
# in gecko-backup-watchdog: alerter swallows aiohttp errors silently. Direct
# curl checks HTTP status and propagates non-200 as exit 7.

set -euo pipefail

HEARTBEAT_FILE="${GECKO_AUDIT_HEARTBEAT_FILE:-/var/lib/gecko-alpha/audit-snapshot/snapshot-last-ok}"
STALE_AFTER_SEC="${GECKO_AUDIT_STALE_AFTER_SEC:-108000}"  # 30h
GECKO_REPO="${GECKO_REPO:-/root/gecko-alpha}"
ENV_FILE="${GECKO_ENV_FILE:-$GECKO_REPO/.env}"
UV_BIN="${UV_BIN:-}"  # test seam

now=$(date +%s)
is_stale=0

if [[ ! -f "$HEARTBEAT_FILE" ]]; then
    age_msg="heartbeat file MISSING ($HEARTBEAT_FILE)"
    is_stale=1
else
    last_ok=$(cat "$HEARTBEAT_FILE" 2>/dev/null || true)
    if [[ ! "$last_ok" =~ ^[0-9]+$ ]]; then
        age_msg="heartbeat file CORRUPT ($HEARTBEAT_FILE: $(printf '%q' "$last_ok"))"
        is_stale=1
    else
        age_sec=$(( now - last_ok ))
        age_msg="last_ok=${age_sec}s ago"
        if (( age_sec > STALE_AFTER_SEC )); then
            is_stale=1
        fi
    fi
fi

if (( is_stale == 0 )); then
    echo "OK: gecko-audit-snapshot ran within ${STALE_AFTER_SEC}s ($age_msg)"
    exit 0
fi

echo "STALE: gecko-audit-snapshot has not run successfully — $age_msg"

if [[ -n "$UV_BIN" ]]; then
    "$UV_BIN" stub-audit-snapshot-watchdog-alert "$age_msg" || true
    exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: env file $ENV_FILE not found; alert NOT delivered" >&2
    exit 4
fi

TELEGRAM_BOT_TOKEN="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
TELEGRAM_CHAT_ID="$(grep -E '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"

if [[ -z "$TELEGRAM_BOT_TOKEN" || "$TELEGRAM_BOT_TOKEN" == "placeholder" ]]; then
    echo "ERROR: TELEGRAM_BOT_TOKEN missing/placeholder in $ENV_FILE; alert NOT delivered" >&2
    exit 5
fi
if [[ -z "$TELEGRAM_CHAT_ID" || "$TELEGRAM_CHAT_ID" == "placeholder" ]]; then
    echo "ERROR: TELEGRAM_CHAT_ID missing/placeholder in $ENV_FILE; alert NOT delivered" >&2
    exit 5
fi

TEXT="⚠️ gecko-audit-snapshot-watchdog: snapshot stale — ${age_msg}. Check journalctl -u gecko-audit-snapshot.service."

PYTHON_BIN="$(command -v python3 || command -v python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
    echo "ERROR: no python available for JSON encoding; alert NOT delivered" >&2
    exit 6
fi

PAYLOAD="$(GECKO_TG_TEXT="$TEXT" GECKO_TG_CHAT="$TELEGRAM_CHAT_ID" "$PYTHON_BIN" -c '
import json, os
print(json.dumps({"chat_id": os.environ["GECKO_TG_CHAT"], "text": os.environ["GECKO_TG_TEXT"]}))
')"

HTTP_STATUS="$(curl -s -o /tmp/.gecko-audit-tg-resp.$$ -w '%{http_code}' \
    -X POST \
    -H 'Content-Type: application/json' \
    -d "$PAYLOAD" \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" || echo 000)"

if [[ "$HTTP_STATUS" != "200" ]]; then
    echo "ERROR: Telegram delivery failed (HTTP $HTTP_STATUS)" >&2
    if [[ -f "/tmp/.gecko-audit-tg-resp.$$" ]]; then
        echo "RESPONSE: $(cat /tmp/.gecko-audit-tg-resp.$$ | head -c 500)" >&2
        rm -f "/tmp/.gecko-audit-tg-resp.$$"
    fi
    exit 7
fi

rm -f "/tmp/.gecko-audit-tg-resp.$$"
echo "ALERT DELIVERED: HTTP $HTTP_STATUS"
exit 1
