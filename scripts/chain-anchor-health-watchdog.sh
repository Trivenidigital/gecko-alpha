#!/usr/bin/env bash
# Alert if chain-pattern anchors are live but active_chains is stale.

set -euo pipefail

APP_DIR="${GECKO_APP_DIR:-/root/gecko-alpha}"
DB_PATH="${GECKO_DB_PATH:-$APP_DIR/scout.db}"
ENV_FILE="${GECKO_ENV_FILE:-$APP_DIR/.env}"
PYTHON="${GECKO_PYTHON:-$APP_DIR/.venv/bin/python}"
ANCHOR_WINDOW_HOURS="${CHAIN_ANCHOR_WATCHDOG_ANCHOR_WINDOW_HOURS:-24}"
ACTIVE_STALE_HOURS="${CHAIN_ANCHOR_WATCHDOG_ACTIVE_STALE_HOURS:-24}"

cd "$APP_DIR"
set +e
result="$("$PYTHON" scripts/check_chain_anchor_health.py \
    --db "$DB_PATH" \
    --env "$ENV_FILE" \
    --anchor-window-hours "$ANCHOR_WINDOW_HOURS" \
    --active-stale-hours "$ACTIVE_STALE_HOURS" 2>&1)"
status=$?
set -e

if [[ "$status" -eq 0 ]]; then
    echo "OK: $result"
    exit 0
fi

text="chain-anchor-health-watchdog: chain anchor pipeline unhealthy. result=$result"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ALERT: $text"
    echo "WARN: env file missing, cannot send Telegram: $ENV_FILE" >&2
    exit 2
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
    echo "ALERT: $text"
    echo "WARN: Telegram env missing, cannot send alert" >&2
    exit 3
fi

curl -fsS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${text}" \
    -d "parse_mode=" >/dev/null

echo "ALERT_SENT: $text"
exit 1
