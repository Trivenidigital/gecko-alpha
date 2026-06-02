#!/usr/bin/env bash
# Execution-heartbeat watchdog for the gainer-acceleration detector
# (scout/gainers/acceleration.py, gap-fill 2026-06-02).
#
# Zero acceleration ROWS can be perfectly healthy (no token qualified this
# window), so a row-rate watchdog would false-alarm constantly. This instead
# verifies the detector RAN -- it greps the journal for the structured
# `acceleration_scan_complete` line the detector emits every cycle. Per global
# CLAUDE.md §12a (watchdog = execution heartbeat, NOT row-rate) and §12b
# (plain-text TG via curl-direct, dispatched/delivered/failed triplet).
#
# Healthy => exit 0. Stale (no heartbeat in window) => Telegram alert.
# Inert when ACCELERATION_ENABLED is falsey (operator turned the detector off).
#
# Exit codes:
#   0  — ok (heartbeat seen in window, or detector disabled)
#   1  — alert dispatched via Telegram (HTTP 200)
#   2  — stale, env file missing (alert deferred to stdout)
#   3  — stale, Telegram credentials missing (alert deferred to stdout)
#   7  — alert path entered but Telegram delivery failed
#  64  — unknown argument

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${GECKO_ENV_FILE:-${REPO_ROOT}/.env}"
SERVICE="${ACCEL_SERVICE:-gecko-pipeline}"
THRESHOLD_MINUTES="${ACCEL_THRESHOLD_MINUTES:-60}"
LOG_MARKER="acceleration_scan_complete"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --threshold-minutes) THRESHOLD_MINUTES="${2:?--threshold-minutes requires a value}"; shift 2 ;;
    --service) SERVICE="${2:?--service requires a value}"; shift 2 ;;
    --env-file) ENV_FILE="${2:?--env-file requires a path}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done

# Source .env early so cron picks up ACCELERATION_ENABLED + Telegram creds
# (cron's env is sparse). Mirrors scripts/source-calls-lag-watchdog.sh.
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

# Inert when the operator has disabled the detector (default is enabled).
accel_enabled="${ACCELERATION_ENABLED:-True}"
case "$accel_enabled" in
    False|false|0|no|No|NO|off|Off)
        echo "OK: acceleration detector disabled (ACCELERATION_ENABLED=${accel_enabled})"
        exit 0
        ;;
esac

# Count heartbeat lines in the window. grep -c over `--since` avoids fragile
# journal-timestamp parsing: zero matches => the detector has not run.
set +e
count="$(journalctl -u "$SERVICE" --since "${THRESHOLD_MINUTES} min ago" --no-pager 2>/dev/null \
    | grep -c "$LOG_MARKER")"
set -e
count="${count:-0}"

if [[ "$count" -gt 0 ]]; then
    echo "OK: ${count}x ${LOG_MARKER} on ${SERVICE} in last ${THRESHOLD_MINUTES}min"
    exit 0
fi

text="acceleration-heartbeat-watchdog: detector heartbeat STALE — no ${LOG_MARKER} on ${SERVICE} in last ${THRESHOLD_MINUTES}min. Likely cause: gecko-pipeline stopped, run_cycle no longer reaches detect_acceleration, or markets ingestion dry. Check: systemctl status ${SERVICE} && journalctl -u ${SERVICE} --since '2h ago' | grep -E 'acceleration_scan|gainer_acceleration_error' | tail -20"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ALERT: $text"
    echo "WARN: env file missing, cannot send Telegram: $ENV_FILE" >&2
    exit 2
fi

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
    echo "ALERT: $text"
    echo "WARN: Telegram env missing, cannot send alert" >&2
    exit 3
fi

# §12b structured triplet around the curl; parse_mode= (plain text) so the
# underscores in acceleration_scan_complete / gainer_acceleration_error are not
# eaten by Telegram MarkdownV1.
echo "acceleration_heartbeat_alert_dispatched service=${SERVICE} threshold=${THRESHOLD_MINUTES}" >&2

set +e
http_status="$(curl -fsS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${text}" \
    -d "parse_mode=" \
    -o /dev/null -w "%{http_code}" 2>/dev/null)"
curl_rc=$?
set -e

if [[ "$curl_rc" -eq 0 && "$http_status" == "200" ]]; then
    echo "acceleration_heartbeat_alert_delivered http_status=${http_status}" >&2
    echo "ALERT_SENT: $text"
    exit 1
else
    echo "acceleration_heartbeat_alert_failed curl_rc=${curl_rc} http_status=${http_status}" >&2
    echo "ALERT_FAILED_DELIVERY: $text (http_status=${http_status})" >&2
    exit 7
fi
