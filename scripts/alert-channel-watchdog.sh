#!/usr/bin/env bash
# Alert-channel + daily-digest freshness watchdog cron entry (CLAUDE.md §12a).
# Runs scripts/alert_channel_watchdog.py, which monitors BOTH tg_alert_log
# ('sent'-row freshness) and paper_daily_summary (daily-digest write-rate) in
# ONE script per the operator amendment, and alerts on any breach.
#
# Context: the Telegram alert channel went silent 2026-06-25 -> 07-08 (14
# days, zero 'sent' rows) AND the daily digest stopped writing after
# 2026-06-26 — neither was noticed because no watchdog read either table.
#
# ACTIVATION AT DEPLOY: gated on ALERT_CHANNEL_WATCHDOG_ENABLED read from the
# CRON ENVIRONMENT (default false) — NOT a Settings/.env field, so this adds
# no config. The managed-block cron line sets it true; until cron/deploy.sh
# installs that line (the operator-approved deploy step) the watchdog is a
# no-op. A manual run of this wrapper without the env var stays a safe no-op.
#
# ACTIVATION PREREQUISITE (S2-3): activate only AFTER PR #429 (daily-digest
# yesterday-fix) is deployed and has written >=1 fresh paper_daily_summary row,
# else the first digest pages are for a known-broken-being-fixed writer. The
# per-table cooldown bounds this to one page/table/window, but the ordering is
# still the correct sequence.
#
# Per-table SEND cooldown (ALERT_CHANNEL_WATCHDOG_COOLDOWN_HOURS, default 24;
# state under ALERT_CHANNEL_WATCHDOG_STATE_DIR): at most one page per breached
# table per window so the hourly cron does not emit ~24 identical pages/day on
# a standing breach. Cooldown suppresses the SEND only — a breach still exits 5.
#
# .env IS sourced so the enabled alert path can read the Telegram credentials
# (TELEGRAM_BOT_TOKEN / _CHAT_ID) via Settings; the enable flag itself is
# intentionally kept OUT of .env to avoid touching the Settings surface.
#
# Exit codes:
#   0  — ok (both fresh, or disabled no-op)
#   5  — one or more freshness breaches (page dispatched and/or cooldown-suppressed)
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
ENABLED="${ALERT_CHANNEL_WATCHDOG_ENABLED:-false}"
SENT_SLO_HOURS="${ALERT_SENT_SLO_HOURS:-48}"
DIGEST_SLO_DAYS="${DIGEST_SUMMARY_SLO_DAYS:-2}"
COOLDOWN_HOURS="${ALERT_CHANNEL_WATCHDOG_COOLDOWN_HOURS:-24}"
STATE_DIR="${ALERT_CHANNEL_WATCHDOG_STATE_DIR:-/var/lib/gecko-alpha/alert-channel-watchdog}"

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

exec "${PYTHON}" "${SCRIPT_DIR}/alert_channel_watchdog.py" \
    --db "${DB_PATH}" --enabled "${ENABLED}" \
    --sent-slo-hours "${SENT_SLO_HOURS}" --digest-slo-days "${DIGEST_SLO_DAYS}" \
    --cooldown-hours "${COOLDOWN_HOURS}" --state-dir "${STATE_DIR}"
