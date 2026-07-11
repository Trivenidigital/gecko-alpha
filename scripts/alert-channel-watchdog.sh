#!/usr/bin/env bash
# Alert-channel + digest + narrative + tg-channel freshness watchdog cron entry
# (CLAUDE.md §12a). Runs scripts/alert_channel_watchdog.py, which monitors FOUR
# surfaces in ONE script per the operator amendment, and alerts on any breach:
#   1. tg_alert_log            — 'sent'-row freshness (ALERT_SENT_SLO_HOURS),
#                                qualified by dispatch activity (ALR-08): a
#                                stale/empty channel pages only when the pipeline
#                                opened > ALERT_DISPATCH_ACTIVITY_THRESHOLD trades
#                                in the window (else quiet-is-legitimate: exit 0).
#   2. paper_daily_summary     — daily-digest write-rate (DIGEST_SUMMARY_SLO_DAYS)
#   3. narrative_alerts_inbound — X/narrative inbound freshness (NARRATIVE_INBOUND_SLO_HOURS)
#   4. tg_social_health        — per-channel staleness scan (TG_CHANNEL_STALE_DAYS)
#
# Context: the Telegram alert channel went silent 2026-06-25 -> 07-08 (14
# days, zero 'sent' rows) AND the daily digest stopped writing after
# 2026-06-26 — neither was noticed because no watchdog read either table. The
# X/narrative inbound feed then went silently dead 2026-06-24 for 16 days
# (NAR-02) and tg_social channels drift silent one-by-one (@alohcooks 72d,
# NAR-07); a both-sides-quiet feed / a dead channel emits no lag signal, only
# absence — so these are set/freshness checks, not lag checks.
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
#   0  — ok (all fresh, disabled no-op, OR alert channel quiet-legitimate:
#          0 sent + 0 dispatch activity — logged, never paged; ALR-08)
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
NARRATIVE_INBOUND_SLO_HOURS="${NARRATIVE_INBOUND_SLO_HOURS:-72}"
TG_CHANNEL_STALE_DAYS="${TG_CHANNEL_STALE_DAYS:-14}"
# ALR-08 dispatch-activity qualifier for check 1 (default 0: any open with 0
# sent pages; raise to tolerate the 24h-dedup tail). From the cron env, not .env.
DISPATCH_ACTIVITY_THRESHOLD="${ALERT_DISPATCH_ACTIVITY_THRESHOLD:-0}"
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
    --narrative-inbound-slo-hours "${NARRATIVE_INBOUND_SLO_HOURS}" \
    --tg-channel-stale-days "${TG_CHANNEL_STALE_DAYS}" \
    --dispatch-activity-threshold "${DISPATCH_ACTIVITY_THRESHOLD}" \
    --cooldown-hours "${COOLDOWN_HOURS}" --state-dir "${STATE_DIR}"
