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

# The gate must match what the READERS use. `evidence_status` is a frozen
# decision about the gate as it stood when the backfill ran; cross_surface
# re-tests the lead against CONVICTION_EARLY_LEAD_MINUTES at scoring time.
# Sourced from .env when present so raising the setting cannot leave the
# alarm measuring the old threshold.
GATE=1440
if [[ -f "$ENV_FILE" ]]; then
    env_gate="$(grep -E '^CONVICTION_EARLY_LEAD_MINUTES=' "$ENV_FILE" | tail -1 | cut -d= -f2)"
    [[ -n "${env_gate:-}" ]] && GATE="$env_gate"
fi

# Validate before `cd`. Under `set -euo pipefail` a failed `cd` exits 1 --
# the SAME code the alarm path uses after a successful Telegram send. A dead
# watchdog and a firing watchdog were indistinguishable to anything
# downstream. Reachable straight from the runbook's own install step: this
# script is installed to /usr/local/bin while APP_DIR defaults to
# /root/gecko-alpha, and that split has already caused a deploy that shipped
# nothing on this box.
#
# EXIT CONTRACT (documented in docs/runbook_recompute_coverage.md):
#   0  healthy            5  APP_DIR invalid
#   1  ALARM, Telegram    6  python interpreter missing
#   2  check could not run (WARN, no Telegram)
#   3  .env missing       4  Telegram credentials missing
# Only 1 notifies. 2-6 are operator-visible failures of the watchdog itself.
if [[ ! -d "$APP_DIR" ]]; then
    echo "FATAL: APP_DIR does not exist: $APP_DIR" >&2
    exit 5
fi
if [[ ! -x "$PYTHON" ]]; then
    echo "FATAL: python interpreter not executable: $PYTHON" >&2
    exit 6
fi

cd "$APP_DIR"
set +e
result="$("$PYTHON" scripts/check_recompute_coverage.py --db "$DB_PATH"     --gate-minutes "$GATE" 2>&1)"
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
