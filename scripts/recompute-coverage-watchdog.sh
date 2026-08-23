#!/usr/bin/env bash
# §12a watchdog for the legacy-provenance overlay.
#
# The overlay has no runtime writer -- an offline backfill fills it -- so a
# deploy that skips that step silently strips chains credit from every
# pre-cutover detection and collapses tier_high under green logs. The in-process
# probe logs the condition hourly, but nothing on this box reads journald, so
# the log line alone is operationally silence.
#
# parse_mode= (empty) is deliberate: status names contain underscores
# (verified_canonical, indeterminate_history) which Telegram MarkdownV1 eats as
# italics markers, returning HTTP 200 with the body mangled.

set -euo pipefail

APP_DIR="${GECKO_APP_DIR:-/root/gecko-alpha}"
DB_PATH="${GECKO_DB_PATH:-$APP_DIR/scout.db}"
ENV_FILE="${GECKO_ENV_FILE:-$APP_DIR/.env}"
PYTHON="${GECKO_PYTHON:-$APP_DIR/.venv/bin/python}"

cd "$APP_DIR"
set +e
result="$("$PYTHON" scripts/check_recompute_coverage.py --db "$DB_PATH" 2>&1)"
status=$?
set -e

if [[ "$status" -eq 0 ]]; then
    echo "OK: $result"
    exit 0
fi

if [[ "$status" -eq 2 ]]; then
    echo "WARN: check could not run: $result" >&2
    exit 2
fi

text="recompute-coverage-watchdog: legacy chains provenance overlay is recovering no credit. result=$result"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ALERT: $text"
    echo "WARN: env file missing, cannot send Telegram: $ENV_FILE" >&2
    exit 3
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
    echo "ALERT: $text"
    echo "WARN: Telegram env missing, cannot send alert" >&2
    exit 4
fi

echo "recompute_coverage_alert_dispatched: $text"
curl -fsS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${text}" \
    -d "parse_mode=" >/dev/null
echo "recompute_coverage_alert_delivered: $text"
exit 1
