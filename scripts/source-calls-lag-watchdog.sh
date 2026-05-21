#!/usr/bin/env bash
# Alert if upstream tg_social_signals / narrative_alerts_inbound rows have not
# reached source_calls within the freshness SLO; OR if the source_calls
# writer cron has stopped firing (when WRITER_HEARTBEAT_FILE is set).
#
# Single Telegram alerter surface per CLAUDE.md §12a. Mirrors the curl
# dispatch pattern in scripts/chain-anchor-health-watchdog.sh.
#
# Status differentiation (writer-side branch active only when
# WRITER_HEARTBEAT_FILE / --writer-heartbeat-file is set):
#   ledger_lag                — upstream lag (existing behavior, default)
#   writer_stale              — writer heartbeat older than threshold
#   writer_heartbeat_missing  — heartbeat absent, ledger has rows
#   writer_never_fired        — heartbeat absent + ledger empty > 6h
#
# Sibling state file: when the writer is in `writer_heartbeat_pending`
# (heartbeat absent + ledger empty), the Python check creates a
# `<heartbeat>.pending-since` sibling file. mtime tracks first
# observation; if it persists >6h the status escalates to
# writer_never_fired. Cleared automatically on writer recovery.
#
# §12b alert hygiene: parse_mode= (plain text), structured-log triplet
# (alert_dispatched/delivered/failed) around the curl call.
#
# Exit codes (wrapper):
#   0  — ok (no lag, writer healthy)
#   1  — alert dispatched via Telegram (HTTP 200)
#   2  — non-zero check, env file missing (alert deferred to stdout)
#   3  — non-zero check, Telegram credentials missing (alert deferred to stdout)
#   7  — alert path entered but Telegram delivery failed (ALERT_FAILED_DELIVERY)
#  64  — unknown argument

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
DB_PATH="${REPO_ROOT}/scout.db"
ENV_FILE="${GECKO_ENV_FILE:-${REPO_ROOT}/.env}"
PYTHON="${GECKO_PYTHON:-${REPO_ROOT}/.venv/bin/python}"
THRESHOLD_MINUTES="30"

# Source .env EARLY so cron invocations pick up WRITER_HEARTBEAT_FILE,
# WRITER_THRESHOLD_MINUTES, and Telegram credentials. Cron's default env
# is sparse — without this early source the writer-heartbeat branch is
# skipped entirely (Python check runs without --writer-heartbeat-file
# args, so cron-tick detection is dead under cron). Discovered
# 2026-05-21 post-deploy. The existing late-stage source for Telegram
# creds is preserved as a fallback when --env-file is overridden at
# the CLI after the early source.
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

WRITER_HEARTBEAT_FILE="${WRITER_HEARTBEAT_FILE:-}"
WRITER_THRESHOLD_MINUTES="${WRITER_THRESHOLD_MINUTES:-20}"

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
    --writer-heartbeat-file)
      WRITER_HEARTBEAT_FILE="${2:?--writer-heartbeat-file requires a path}"
      shift 2
      ;;
    --writer-threshold-minutes)
      WRITER_THRESHOLD_MINUTES="${2:?--writer-threshold-minutes requires a value}"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 64
      ;;
  esac
done

py_args=(--db "${DB_PATH}" --threshold-minutes "${THRESHOLD_MINUTES}")
if [[ -n "$WRITER_HEARTBEAT_FILE" ]]; then
    py_args+=(--writer-heartbeat-file "$WRITER_HEARTBEAT_FILE")
    py_args+=(--writer-threshold-minutes "$WRITER_THRESHOLD_MINUTES")
fi

# Capture stdout only — stderr goes to journal where it belongs. This
# protects the JSON parse below from any future stderr noise.
set +e
result="$("${PYTHON}" "${SCRIPT_DIR}/check_source_calls_lag.py" "${py_args[@]}" 2>/dev/null)"
status=$?
set -e

if [[ "$status" -eq 0 ]]; then
    echo "OK: $result"
    exit 0
fi

# Extract structured status + detail.path from JSON's body (last non-empty
# line is parsed defensively). The path is used in remediation hints so
# the operator sees the actual configured path, not a fallback default.
# Falls through to "unknown" / empty path -> ledger_lag text.
parse_output="$(echo "$result" | python3 -c '
import json, sys
lines = [l for l in sys.stdin.read().splitlines() if l.strip()]
if not lines:
    print("unknown\t")
    sys.exit(0)
try:
    d = json.loads(lines[-1])
    status = d.get("status", "unknown")
    path = ""
    detail = d.get("detail")
    if isinstance(detail, dict):
        path = detail.get("path", "") or ""
    print(f"{status}\t{path}")
except Exception:
    print("unknown\t")
' 2>/dev/null || echo "unknown	")"

parsed_status="${parse_output%%	*}"
parsed_path="${parse_output#*	}"
remediation_path="${parsed_path:-${WRITER_HEARTBEAT_FILE:-/var/lib/gecko-alpha/source-calls/writer-heartbeat}}"

# Build operator-facing alert text — plain prose with extracted fields.
# Body is bounded to 3500 chars to stay under Telegram's 4096 limit.
case "$parsed_status" in
    writer_stale)
        text="source-calls-lag-watchdog: writer cron stale — last SUCCEEDED >${WRITER_THRESHOLD_MINUTES}min ago (threshold ${WRITER_THRESHOLD_MINUTES}min). path=${remediation_path}. Likely cause: source-calls-live-writer cron stopped or writer is failing on every tick. Check: systemctl status cron && journalctl --since '1h ago' | grep source_calls_live_writer | tail -20. status=${status} detail=${result}"
        ;;
    writer_heartbeat_missing)
        text="source-calls-lag-watchdog: writer heartbeat missing — ledger has rows but heartbeat file is gone. path=${remediation_path}. Likely cause: state-dir wiped, permission change, or WRITER_HEARTBEAT_FILE env var dropped. Check: ls -la \$(dirname ${remediation_path}). status=${status} detail=${result}"
        ;;
    writer_never_fired)
        text="source-calls-lag-watchdog: writer never fired — heartbeat absent + ledger empty for >6h. path=${remediation_path}. Likely cause: writer cron line missing, .env unset, or wrapper exit on every run. Check: crontab -l | grep source-calls-live-writer && tail -50 /var/log/syslog | grep source_calls. status=${status} detail=${result}"
        ;;
    *)
        text="source-calls-lag-watchdog: source_calls ledger lagging or unreachable. status=${status} result=${result}"
        ;;
esac

# Bound text length defensively (Telegram limit 4096 chars).
if [[ "${#text}" -gt 3500 ]]; then
    text="${text:0:3500}... [truncated]"
fi

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

# §12b structured log triplet — emit dispatched line BEFORE curl, then
# delivered (200) or failed (non-200 / curl error) AFTER. Stderr is
# captured by systemd-journal.
echo "source_calls_lag_alert_dispatched status=${parsed_status} python_exit=${status}" >&2

set +e
http_status="$(curl -fsS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${text}" \
    -d "parse_mode=" \
    -o /dev/null -w "%{http_code}" 2>/dev/null)"
curl_rc=$?
set -e

if [[ "$curl_rc" -eq 0 && "$http_status" == "200" ]]; then
    echo "source_calls_lag_alert_delivered status=${parsed_status} http_status=${http_status}" >&2
    echo "ALERT_SENT: $text"
    exit 1
else
    echo "source_calls_lag_alert_failed status=${parsed_status} curl_rc=${curl_rc} http_status=${http_status}" >&2
    echo "ALERT_FAILED_DELIVERY: $text (http_status=${http_status})" >&2
    exit 7
fi
