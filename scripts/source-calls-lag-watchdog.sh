#!/usr/bin/env bash
# Alert if upstream tg_social_signals / narrative_alerts_inbound rows have not
# reached source_calls within the freshness SLO. Mirrors the Telegram-curl
# pattern in scripts/chain-anchor-health-watchdog.sh.
#
# Exit codes (wrapper, not python):
#   0 — ok (no lag)
#   1 — alert dispatched via Telegram
#   2 — non-zero check, env file missing (alert deferred to stdout)
#   3 — non-zero check, Telegram credentials missing (alert deferred to stdout)
#  64 — unknown argument

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
DB_PATH="${REPO_ROOT}/scout.db"
ENV_FILE="${GECKO_ENV_FILE:-${REPO_ROOT}/.env}"
PYTHON="${GECKO_PYTHON:-${REPO_ROOT}/.venv/bin/python}"
THRESHOLD_MINUTES="30"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --db)
      DB_PATH="${2:?--db requires a path}"
      shift 2
      ;;
    --threshold-minutes)
      THRESHOLD_MINUTES="${2:?--threshold-minutes requires a value}"
      shift 2
      ;;
    --env-file)
      ENV_FILE="${2:?--env-file requires a path}"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 64
      ;;
  esac
done

set +e
result="$("${PYTHON}" "${SCRIPT_DIR}/check_source_calls_lag.py" \
  --db "${DB_PATH}" \
  --threshold-minutes "${THRESHOLD_MINUTES}" 2>&1)"
status=$?
set -e

if [[ "$status" -eq 0 ]]; then
    echo "OK: $result"
    exit 0
fi

text="source-calls-lag-watchdog: source_calls ledger lagging or unreachable. status=${status} result=${result}"

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
