#!/usr/bin/env bash
# Alert if recent minara_alert_command_emitted logs are not represented in DB.

set -euo pipefail

APP_DIR="${GECKO_APP_DIR:-/root/gecko-alpha}"
DB_PATH="${GECKO_DB_PATH:-$APP_DIR/scout.db}"
ENV_FILE="${GECKO_ENV_FILE:-$APP_DIR/.env}"
PYTHON="${GECKO_PYTHON:-$APP_DIR/.venv/bin/python}"
SINCE="${MINARA_EMISSION_WATCHDOG_SINCE:-24 hours ago}"
SERVICE="${MINARA_EMISSION_WATCHDOG_SERVICE:-gecko-pipeline}"
TOLERANCE="${MINARA_EMISSION_WATCHDOG_TOLERANCE:-0}"

tmp_all="$(mktemp)"
tmp_journal="$(mktemp)"
tmp_err="$(mktemp)"
trap 'rm -f "$tmp_all" "$tmp_journal" "$tmp_err"' EXIT

if ! journalctl -u "$SERVICE" --since "$SINCE" --no-pager -o cat > "$tmp_all" 2> "$tmp_err"; then
    err="$(head -c 500 "$tmp_err")"
    result="{\"ok\":false,\"source_error\":\"journalctl_failed\",\"service\":\"$SERVICE\",\"err\":\"$err\"}"
    status=2
else
    grep 'minara_alert_command_emitted' "$tmp_all" > "$tmp_journal" || true
    cd "$APP_DIR"
    set +e
    result="$("$PYTHON" scripts/check_minara_emission_persistence.py \
        --db "$DB_PATH" --journal "$tmp_journal" --tolerance "$TOLERANCE" 2>&1)"
    status=$?
    set -e
fi

if [[ "$status" -eq 0 ]]; then
    echo "OK: $result"
    exit 0
fi

text="minara-emission-persistence-watchdog: DB rows lag journalctl emissions since='$SINCE'. result=$result"

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
