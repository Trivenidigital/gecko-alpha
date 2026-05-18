#!/usr/bin/env bash
# cron-drift-watchdog — detect drift between repo cron/gecko-alpha.crontab
# managed block and live `crontab -l` output. Alert via Telegram on drift;
# silent-suppress identical drift-sets via sha256 ack tombstone.
#
# BL-NEW-CRON-DRIFT-WATCHDOG (cycle 12). Mirrors cycle-10's
# scripts/systemd-drift-watchdog.sh structurally/conventionally, with
# 2-reviewer folds applied (see tasks/plan_cron_drift_watchdog.md v2):
#  - Tempfile-based diff (R1 #2/#3) to avoid command-substitution newline
#    asymmetry false positives
#  - mktemp for Telegram response file (R2 #4 — symlink-attack hardening)
#  - curl --max-time 30 (R2 #12 — bounds held-lock window)
#  - sentinel-count guard + sentinel-text-typo detector (R1 #1, R1 #8)
#  - explicit `command -v crontab` precheck (R1 #4)
#  - distinct exit codes 4=ENV vs 8=FRAGMENT (R1 #12)
#
# Exit codes:
#   0  CLEAN (no drift; heartbeat-file touched)
#   1  DRIFT alerted (or silently suppressed via tombstone match)
#   4  ENV_FILE missing
#   5  TELEGRAM_BOT_TOKEN / CHAT_ID missing or placeholder
#   6  required binary (python3 / crontab) missing
#   7  Telegram HTTP_STATUS != 200 (ACK NOT written — re-alerts next fire)
#   8  FRAGMENT_FILE missing
#   99 (test stub only) unexpected `crontab` subcommand

set -euo pipefail

REPO_DIR="${GECKO_REPO:-/root/gecko-alpha}"
ENV_FILE="${GECKO_ENV_FILE:-$REPO_DIR/.env}"
ACK_DIR="${CRON_DRIFT_ACK_DIR:-/var/lib/gecko-alpha/cron-drift-watchdog}"
ACK_FILE="$ACK_DIR/last_alerted_hash"
HEARTBEAT_FILE="${CRON_DRIFT_HEARTBEAT_FILE:-$ACK_DIR/heartbeat}"
LOCK_FILE="$ACK_DIR/.lock"
FRAGMENT_FILE="${CRON_FRAGMENT_FILE:-$REPO_DIR/cron/gecko-alpha.crontab}"
UV_BIN="${UV_BIN:-}"
CRONTAB_BIN="${CRONTAB_BIN:-crontab}"

# --- Bootstrap ----------------------------------------------------------

# R1 #4: crontab binary present (loud failure if not)
if ! command -v "$CRONTAB_BIN" >/dev/null 2>&1; then
    echo "ERROR: crontab binary not found: $CRONTAB_BIN" >&2
    exit 6
fi

if ! mkdir -p "$ACK_DIR" 2>/dev/null; then
    echo "WARN: failed to mkdir $ACK_DIR; ack-tombstone unavailable; alert may re-fire daily" >&2
fi
chmod 0700 "$ACK_DIR" 2>/dev/null || true

# flock prevents timer-race on ACK_FILE non-atomic read-modify-write.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "OK: skipping run — previous invocation still holds $LOCK_FILE"
    exit 0
fi

# --- R1 #12 + R1 #8: FRAGMENT_FILE presence (distinct exit 8) -----------

if [[ ! -f "$FRAGMENT_FILE" ]]; then
    echo "ERROR: repo fragment $FRAGMENT_FILE not found" >&2
    exit 8
fi

# --- Tempfile setup (R2 #4: symlink-attack-safe) ------------------------

EXPECTED_FILE="$(mktemp -t gecko-cron-expected.XXXXXX)"
LIVE_FILE="$(mktemp -t gecko-cron-live.XXXXXX)"
RESP_FILE=""  # set later, just before curl; trap cleans both if set
trap 'rm -f "$EXPECTED_FILE" "$LIVE_FILE" ${RESP_FILE:+"$RESP_FILE"}' EXIT

cat "$FRAGMENT_FILE" > "$EXPECTED_FILE"

# Same empty-crontab guard as cron/deploy.sh:30
LIVE_FULL="$( ( "$CRONTAB_BIN" -l 2>/dev/null || true ) )"

# --- Sentinel-count guard (R1 #1) ---------------------------------------

BEGIN_COUNT=$(printf '%s\n' "$LIVE_FULL" | grep -c '^# === BEGIN gecko-alpha managed block' || true)
END_COUNT=$(printf '%s\n' "$LIVE_FULL" | grep -c '^# === END gecko-alpha managed block' || true)

# Extract LIVE_BLOCK between sentinels (awk; emits nothing if sentinels absent).
LIVE_BLOCK="$(printf '%s\n' "$LIVE_FULL" | awk '
    /^# === BEGIN gecko-alpha managed block/ { capturing=1 }
    capturing { print }
    /^# === END gecko-alpha managed block/ { capturing=0 }
')"
printf '%s\n' "$LIVE_BLOCK" > "$LIVE_FILE"

DRIFT_LINES=()

if [[ "$BEGIN_COUNT" != "1" || "$END_COUNT" != "1" ]]; then
    DRIFT_LINES+=("DRIFT: malformed sentinel structure (begin=$BEGIN_COUNT end=$END_COUNT)")
fi

# --- Sentinel-text-typo detector (R1 #8) --------------------------------

if [[ "$BEGIN_COUNT" == "0" ]]; then
    LOOSE_BEGIN="$(printf '%s\n' "$LIVE_FULL" | grep -i 'BEGIN gecko-alpha' | head -1 || true)"
    if [[ -n "$LOOSE_BEGIN" ]]; then
        DRIFT_LINES+=("DRIFT: sentinel text does not match canonical form: $LOOSE_BEGIN")
    fi
fi

# --- Content diff via tempfiles (R1 #2/#3 — no newline asymmetry) -------

if [[ -z "$LIVE_BLOCK" ]]; then
    DRIFT_LINES+=("DRIFT: managed block missing from prod crontab")
elif ! diff -q "$EXPECTED_FILE" "$LIVE_FILE" >/dev/null 2>&1; then
    DRIFT_LINES+=("DRIFT: managed block content differs from repo fragment")
    # Use --label to give stable labels (default headers include tempfile
    # paths + mtimes which vary between runs and would break sha256 ack
    # tombstone dedup — same drift would re-alert every run).
    DIFF_BODY="$(diff -u --label "repo:cron/gecko-alpha.crontab" --label "live:crontab -l" "$EXPECTED_FILE" "$LIVE_FILE" 2>/dev/null || true)"
    if [[ -n "$DIFF_BODY" ]]; then
        DRIFT_LINES+=("$DIFF_BODY")
    fi
fi

# --- Hash + ack-tombstone -----------------------------------------------

DRIFT_REPORT_SORTED="$(printf '%s\n' "${DRIFT_LINES[@]:-}" | grep -v '^$' | sort || true)"

if [[ -z "$DRIFT_REPORT_SORTED" ]]; then
    touch "$HEARTBEAT_FILE" 2>/dev/null || true
    rm -f "$ACK_FILE" 2>/dev/null || true
    echo "OK: 0 drifts (managed block matches repo fragment)"
    exit 0
fi

DRIFT_HASH=$(printf '%s' "$DRIFT_REPORT_SORTED" | sha256sum | awk '{print $1}')

if [[ -f "$ACK_FILE" ]]; then
    PRIOR_HASH=$(cat "$ACK_FILE" 2>/dev/null || true)
    if [[ "$PRIOR_HASH" == "$DRIFT_HASH" ]]; then
        echo "SUPPRESS: drift set unchanged (hash $DRIFT_HASH); see prior alert"
        exit 1
    fi
fi

# --- Alert (R1 #11 / R2 #15: rename strings vs systemd-watchdog copy) ---

MAX_BODY=3500
TRUNC_FOOTER=""
if [[ "${#DRIFT_REPORT_SORTED}" -gt "$MAX_BODY" ]]; then
    DRIFT_REPORT_SORTED="${DRIFT_REPORT_SORTED:0:$MAX_BODY}"
    TRUNC_FOOTER=$'\n(more drifts truncated — see journalctl for cron-drift-watchdog)'
fi

ALERT_BODY="⚠️ cron-drift-watchdog: drift detected
$DRIFT_REPORT_SORTED$TRUNC_FOOTER"

# UV_BIN stub path (tests)
if [[ -n "$UV_BIN" ]]; then
    "$UV_BIN" stub-watchdog-alert "$ALERT_BODY" || true
    if ! echo "$DRIFT_HASH" > "$ACK_FILE" 2>/dev/null; then
        echo "WARN: cron_drift_ack_write_failed (stub path) — alert may re-fire next run" >&2
    fi
    exit 1
fi

# Prod path: curl-direct
if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: env file $ENV_FILE not found; alert NOT delivered" >&2
    exit 4
fi

TELEGRAM_BOT_TOKEN="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
TELEGRAM_CHAT_ID="$(grep -E '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"

if [[ -z "$TELEGRAM_BOT_TOKEN" || "$TELEGRAM_BOT_TOKEN" == "placeholder" ]]; then
    echo "ERROR: TELEGRAM_BOT_TOKEN missing/placeholder in $ENV_FILE" >&2
    exit 5
fi
if [[ -z "$TELEGRAM_CHAT_ID" || "$TELEGRAM_CHAT_ID" == "placeholder" ]]; then
    echo "ERROR: TELEGRAM_CHAT_ID missing/placeholder in $ENV_FILE" >&2
    exit 5
fi

PYTHON_BIN="$(command -v python3 || command -v python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
    echo "ERROR: no python available for JSON encoding" >&2
    exit 6
fi

PAYLOAD="$(GECKO_TG_TEXT="$ALERT_BODY" GECKO_TG_CHAT="$TELEGRAM_CHAT_ID" "$PYTHON_BIN" -c '
import json, os
print(json.dumps({"chat_id": os.environ["GECKO_TG_CHAT"], "text": os.environ["GECKO_TG_TEXT"]}))
')"

# R2 #4 + #12: mktemp-based response file + curl max-time
RESP_FILE="$(mktemp -t gecko-cron-drift-resp.XXXXXX)"

HTTP_STATUS="$(curl -s --max-time 30 -o "$RESP_FILE" -w '%{http_code}' \
    -X POST \
    -H 'Content-Type: application/json' \
    -d "$PAYLOAD" \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" || echo 000)"

if [[ "$HTTP_STATUS" != "200" ]]; then
    echo "ERROR: Telegram delivery failed (HTTP $HTTP_STATUS); ACK_FILE NOT written; next fire will re-alert" >&2
    if [[ -s "$RESP_FILE" ]]; then
        echo "RESPONSE: $(cat "$RESP_FILE" | head -c 500)" >&2
    fi
    exit 7
fi

# Alert delivered; write ack
if ! echo "$DRIFT_HASH" > "$ACK_FILE" 2>/dev/null; then
    echo "WARN: cron_drift_ack_write_failed — alert delivered but tombstone unwritable; alert WILL re-fire next run" >&2
fi

echo "ALERTED: HTTP 200; hash=$DRIFT_HASH; ${#DRIFT_LINES[@]} drift items"
exit 1
