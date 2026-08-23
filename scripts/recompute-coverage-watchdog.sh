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

# PREFLIGHT, before the `cd` and before anything invokes the interpreter.
# Under `set -euo pipefail` a failed `cd` exits 1 --
# the SAME code the alarm path uses after a successful Telegram send. A dead
# watchdog and a firing watchdog were indistinguishable to anything
# downstream. Reachable straight from the runbook's own install step: this
# script is installed to /usr/local/bin while APP_DIR defaults to
# /root/gecko-alpha, and that split has already caused a deploy that shipped
# nothing on this box.
#
# EXIT CONTRACT (documented in docs/runbook_recompute_coverage.md):
#   0  healthy            5  APP_DIR invalid
#   1  ALARM, Telegram    6  interpreter missing
#   2  check could not run (WARN, no Telegram)
#   7  the app could not be imported to read the gate
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


# The gate must match what the READERS use. `evidence_status` is a frozen
# decision about the gate as it stood when the backfill ran; cross_surface
# re-tests the lead against CONVICTION_EARLY_LEAD_MINUTES at scoring time.
#
# ASK THE APPLICATION, do not parse .env. Two reasons, both found by running
# this rather than reading it:
#
#   1. The earlier `grep ... | tail -1 | cut` died on the REAL production
#      .env. That file has no CONVICTION_EARLY_LEAD_MINUTES -- the value is a
#      Python default -- so grep exited 1, pipefail propagated it, and `set -e`
#      killed the script before the check ran. It printed nothing and exited
#      1, which is this wrapper's own ALARM code: dead while looking like it
#      fired, in the alarm that exists because nothing here reads journald.
#
#   2. Even fixed, .env is the wrong source. The value lives in
#      scout/config.py, so a code change to that default would move the
#      in-process probe via Settings while this kept a stale literal -- the
#      same divergence the probe's gate re-check was added to remove, pushed
#      down into the shell.
#
# `check_recompute_coverage.py` stays stdlib-only (it must run when the app is
# broken); the WRAPPER does the asking, and falls back to the literal only if
# the interpreter cannot answer.
cd "$APP_DIR"
# Two-step, so an incomplete .env degrades to the CODE default rather than to
# a number written here. Building full Settings needs every required secret, so
# on a box mid-configuration the first form fails -- and falling back to a
# shell literal would silently reintroduce the fourth-literal problem this
# block exists to remove. The second form reads the field default without
# instantiating Settings, so it tracks scout/config.py even then.
GATE="$("$PYTHON" - <<'PYGATE' 2>/dev/null || true
from scout.config import Settings

try:
    from scout.config import get_settings

    print(get_settings().CONVICTION_EARLY_LEAD_MINUTES)
except Exception:
    print(Settings.model_fields["CONVICTION_EARLY_LEAD_MINUTES"].default)
PYGATE
)"
if [[ -z "${GATE:-}" ]]; then
    # Neither form worked: the app is unimportable. Say so loudly and refuse
    # rather than measuring against a guess -- a watchdog reporting healthy
    # off an assumed threshold is the failure this whole file exists to
    # prevent.
    echo "FATAL: could not read CONVICTION_EARLY_LEAD_MINUTES from $APP_DIR" >&2
    exit 7
fi

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
