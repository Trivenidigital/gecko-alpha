#!/usr/bin/env bash
# C4 coverage / silent-failure watchdog cron entry (design #392).
# Runs scripts/source_call_coverage_watchdog.py, which evaluates the X
# price-snapshot watchdogs and alerts the operator on any 'alert' finding.
#
# DEPLOY-WITHOUT-ACTIVATE: gated on SOURCE_CALL_COVERAGE_WATCHDOG_ENABLED read
# from the CRON ENVIRONMENT (default false) — NOT a Settings/.env field, so C4
# adds no config. Until the operator sets it (e.g. in the crontab line), the
# watchdog exits 0 as a no-op. No deploy/activation during the DEX soak without
# separate approval.
#
# .env IS sourced so the enabled alert path can read the Telegram credentials
# (TELEGRAM_BOT_TOKEN / _CHAT_ID) via Settings; the enable flag itself is
# intentionally kept OUT of .env to avoid touching the Settings surface.
#
# Exit codes:
#   0  — ok (no alerts, or disabled no-op)
#   5  — one or more watchdog alerts fired
#   1  — DB missing / runtime / alert-dispatch error
#  64  — unknown argument

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
PYTHON="${GECKO_PYTHON:-${REPO_ROOT}/.venv/bin/python}"

ENV_FILE="${GECKO_ENV_FILE:-${REPO_ROOT}/.env}"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

DB_PATH="${REPO_ROOT}/scout.db"
# Read from the cron environment; default off. Deliberately not in .env.
ENABLED="${SOURCE_CALL_COVERAGE_WATCHDOG_ENABLED:-false}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --db)
      DB_PATH="${2:?--db requires a path}"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 64
      ;;
  esac
done

cd "$REPO_ROOT"

exec "${PYTHON}" "${SCRIPT_DIR}/source_call_coverage_watchdog.py" \
    --db "${DB_PATH}" --enabled "${ENABLED}"
