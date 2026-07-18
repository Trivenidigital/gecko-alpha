#!/usr/bin/env bash
# CoinGecko-ingestion freshness + persistent-outage watchdog cron entry
# (CLAUDE.md §12a). Runs scripts/cg_ingestion_watchdog.py, which reads OUTPUT
# rows (never heartbeats) and alerts on any breach:
#   1. trending_snapshots      — MAX(snapshot_at) freshness
#                                (TRENDING_SNAPSHOT_STALENESS_ALERT_HOURS)
#   2. coingecko ingestion     — persistent CG-outage: freshest snapshot_at
#                                across the CG snapshot writers (trending +
#                                gainers + losers) older than CG_OUTAGE_ALERT_HOURS
#                                => the PRIMARY source is dark (quota exhaustion).
#
# Context: 2026-07-14 the CG Demo key's quota exhausted; every cycle since logged
# cg_429_backoff -> coingecko_lanes_stopped_for_backoff and the CG-sourced
# trending_snapshots writer went dead 2026-07-13 16:12Z. 1,559 backoff events
# over 6 days, ZERO operator alerts. The in-process ingest watchdog was blind:
# the breaker trips on the FIRST CG lane and short-circuits the scanner lanes
# BEFORE they record a zero raw_count sample, so consecutive_misses never
# accumulates. This watchdog reads the writer OUTPUT directly instead.
#
# ACTIVATION AT DEPLOY: gated on CG_INGESTION_WATCHDOG_ENABLED read from the CRON
# ENVIRONMENT (default false) — NOT a Settings/.env field, so activation cannot
# happen by an accidental .env edit. The managed-block cron line sets it true;
# until cron/deploy.sh installs that line (the operator-approved deploy step) the
# watchdog is a no-op. A manual run of this wrapper without the env var stays a
# safe no-op.
#
# Per-check SEND cooldown (CG_INGESTION_WATCHDOG_COOLDOWN_HOURS, default 24; state
# under CG_INGESTION_WATCHDOG_STATE_DIR): at most one page per breached check per
# window so the hourly cron does not emit ~24 identical pages/day on a standing
# outage. Cooldown suppresses the SEND only — a breach still exits 5.
#
# .env IS sourced so (a) the enabled alert path can read the Telegram credentials
# (TELEGRAM_BOT_TOKEN / _CHAT_ID) via Settings and (b) the SLO knobs
# (TRENDING_SNAPSHOT_STALENESS_ALERT_HOURS / CG_OUTAGE_ALERT_HOURS, declared as
# Settings fields) flow through as env vars; the enable flag itself is
# intentionally kept OUT of .env to avoid an accidental activation.
#
# Exit codes:
#   0  — ok (both checks fresh, OR disabled no-op)
#   5  — one or more breaches (page dispatched and/or cooldown-suppressed)
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
ENABLED="${CG_INGESTION_WATCHDOG_ENABLED:-false}"
TRENDING_SLO_HOURS="${TRENDING_SNAPSHOT_STALENESS_ALERT_HOURS:-3}"
CG_OUTAGE_HOURS="${CG_OUTAGE_ALERT_HOURS:-2}"
COOLDOWN_HOURS="${CG_INGESTION_WATCHDOG_COOLDOWN_HOURS:-24}"
STATE_DIR="${CG_INGESTION_WATCHDOG_STATE_DIR:-/var/lib/gecko-alpha/cg-ingestion-watchdog}"

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

mkdir -p "$STATE_DIR"

cd "$REPO_ROOT"

exec "${PYTHON}" "${SCRIPT_DIR}/cg_ingestion_watchdog.py" \
    --db "${DB_PATH}" --enabled "${ENABLED}" \
    --trending-slo-hours "${TRENDING_SLO_HOURS}" \
    --cg-outage-hours "${CG_OUTAGE_HOURS}" \
    --cooldown-hours "${COOLDOWN_HOURS}" --state-dir "${STATE_DIR}"
