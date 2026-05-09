#!/usr/bin/env bash
# gecko-backup-watchdog — alert if rotation hasn't run successfully in 48h.
#
# Telegram delivery path is direct via the bot HTTP API (not via
# scout.alerter.send_telegram_message). Rationale per R6 PR review CRITICAL:
# 1. send_telegram_message takes 3 positional args including
#    aiohttp.ClientSession — easy to misuse from a sidecar script.
# 2. send_telegram_message swallows aiohttp errors and returns None — the
#    watchdog cannot tell delivery succeeded vs silently failed.
# 3. Constructing an aiohttp.ClientSession inline doubles the surface area
#    for embedded-Python syntax errors against $age_msg interpolation.
#
# A direct curl-style POST checks status code in bash itself, so any HTTP
# failure (404, 401, network) is observable and propagates exit-1 cleanly.
#
# UV_BIN exists for compat with earlier draft and as a testability seam:
# pytest's _make_uv_stub overrides it with a recorder script.

set -euo pipefail

HEARTBEAT_FILE="${GECKO_BACKUP_HEARTBEAT_FILE:-/var/lib/gecko-alpha/backup-rotation/backup-last-ok}"
STALE_AFTER_SEC="${GECKO_BACKUP_STALE_AFTER_SEC:-172800}"  # 48h
GECKO_REPO="${GECKO_REPO:-/root/gecko-alpha}"
ENV_FILE="${GECKO_ENV_FILE:-$GECKO_REPO/.env}"
# UV_BIN retained as testability seam — when the pytest stub points UV_BIN
# at a stub script, the watchdog calls it instead of the inline curl path.
UV_BIN="${UV_BIN:-}"

now=$(date +%s)
is_stale=0  # R6 NIT: initialize before branches to avoid set -u trap.

if [[ ! -f "$HEARTBEAT_FILE" ]]; then
    age_msg="heartbeat file MISSING ($HEARTBEAT_FILE)"
    is_stale=1
else
    last_ok=$(cat "$HEARTBEAT_FILE" 2>/dev/null || true)
    # R5 + R6 CRITICAL: validate heartbeat content. Empty / non-numeric
    # (corrupt mid-write, manual `: > heartbeat`, fs full) must NOT die in
    # bash arithmetic — instead treat as MISSING and alert.
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
    echo "OK: gecko-backup-rotate ran within ${STALE_AFTER_SEC}s ($age_msg)"
    exit 0
fi

echo "STALE: gecko-backup-rotate has not run successfully — $age_msg"

# --- Alert delivery ---------------------------------------------------------

if [[ -n "$UV_BIN" ]]; then
    "$UV_BIN" stub-watchdog-alert "$age_msg" || true
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

TEXT="⚠️ gecko-backup-watchdog: rotation stale — ${age_msg}. Check journalctl -u gecko-backup.service."

PYTHON_BIN="$(command -v python3 || command -v python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
    echo "ERROR: no python available for JSON encoding; alert NOT delivered" >&2
    exit 6
fi

PAYLOAD="$(GECKO_TG_TEXT="$TEXT" GECKO_TG_CHAT="$TELEGRAM_CHAT_ID" "$PYTHON_BIN" -c '
import json, os
print(json.dumps({"chat_id": os.environ["GECKO_TG_CHAT"], "text": os.environ["GECKO_TG_TEXT"]}))
')"

HTTP_STATUS="$(curl -s -o /tmp/.gecko-tg-resp.$$ -w '%{http_code}' \
    -X POST \
    -H 'Content-Type: application/json' \
    -d "$PAYLOAD" \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" || echo 000)"

if [[ "$HTTP_STATUS" != "200" ]]; then
    echo "ERROR: Telegram delivery failed (HTTP $HTTP_STATUS)" >&2
    if [[ -f "/tmp/.gecko-tg-resp.$$" ]]; then
        echo "RESPONSE: $(cat /tmp/.gecko-tg-resp.$$ | head -c 500)" >&2
        rm -f "/tmp/.gecko-tg-resp.$$"
    fi
    exit 7
fi

rm -f "/tmp/.gecko-tg-resp.$$"
echo "ALERT DELIVERED: HTTP $HTTP_STATUS"
exit 1
