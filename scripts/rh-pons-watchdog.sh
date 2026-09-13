#!/usr/bin/env bash
# RH/Pons discovery poll-liveness watchdog cron entry (CLAUDE.md §12a) — PR-C.
# Runs scripts/rh_pons_watchdog.py against the durable poll heartbeat
# (ingest_watchdog_state source='rh_pons'); MAX(first_seen_at) is
# diagnostic context only, never the paging signal.
#
# ACTIVATION AT DEPLOY: gated on RH_PONS_WATCHDOG_ENABLED read from the
# CRON ENVIRONMENT (default false) — NOT a Settings/.env field, so activation
# cannot happen by an accidental .env edit. Additionally armed only while the
# lane itself is on: RH_PONS_COLLECTOR_ENABLED is read from .env and passed
# through; when the lane is intentionally OFF the watchdog exits cleanly
# without paging (disablement is never represented as failure).
#
# .env IS sourced so (a) the alert path can read Telegram credentials via
# Settings and (b) the knobs (RH_PONS_POLL_STALENESS_ALERT_HOURS /
# RH_PONS_WATCHDOG_CLOCK_SKEW_SECONDS) flow through; the enable flag
# itself is intentionally kept OUT of .env — it is captured from the cron
# environment BEFORE .env is sourced and the variable is unset afterwards,
# so a stray RH_PONS_WATCHDOG_ENABLED line in .env can neither arm
# nor disarm the watchdog.
#
# Exit codes: 0 ok/disabled/lock-held · 5 breach · 1 error · 64 unknown arg

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
PYTHON="${GECKO_PYTHON:-${REPO_ROOT}/.venv/bin/python}"

# Capture the watchdog gate from the CRON ENVIRONMENT before .env is
# sourced, then unset it so no .env value can override the cron decision.
WATCHDOG_ENABLED_FROM_CRON="${RH_PONS_WATCHDOG_ENABLED:-false}"

ENV_FILE="${GECKO_ENV_FILE:-${REPO_ROOT}/.env}"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi
unset RH_PONS_WATCHDOG_ENABLED

DB_PATH="${REPO_ROOT}/scout.db"
# Watchdog gate: cron environment only (captured above); never .env.
ENABLED="${WATCHDOG_ENABLED_FROM_CRON}"
# Lane gate: from .env (the lane's own operator flag).
DISCOVERY_ENABLED="${RH_PONS_COLLECTOR_ENABLED:-false}"
STALENESS_HOURS="${RH_PONS_POLL_STALENESS_ALERT_HOURS:-2}"
CLOCK_SKEW_SECONDS="${RH_PONS_WATCHDOG_CLOCK_SKEW_SECONDS:-300}"
COOLDOWN_HOURS="${RH_PONS_WATCHDOG_COOLDOWN_HOURS:-24}"
STATE_DIR="${RH_PONS_WATCHDOG_STATE_DIR:-/var/lib/gecko-alpha/rh-pons-watchdog}"

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

exec "${PYTHON}" "${SCRIPT_DIR}/dex_discovery_watchdog.py" --source rh_pons \
    --db "${DB_PATH}" --enabled "${ENABLED}" \
    --discovery-enabled "${DISCOVERY_ENABLED}" \
    --staleness-hours "${STALENESS_HOURS}" \
    --clock-skew-seconds "${CLOCK_SKEW_SECONDS}" \
    --cooldown-hours "${COOLDOWN_HOURS}" --state-dir "${STATE_DIR}"
