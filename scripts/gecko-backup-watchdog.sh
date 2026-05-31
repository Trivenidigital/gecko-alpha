#!/usr/bin/env bash
# gecko-backup-watchdog — alert if EITHER the rotation OR the create step
# hasn't run successfully in 48h.
#
# Round 13 extension: Round 11 added a second heartbeat for the create
# step (gecko-backup-create.sh writes /var/lib/.../create-last-ok after
# integrity_check passes). The original watchdog only checked the rotate
# heartbeat, leaving a blind spot: if create starts failing (sqlite3
# missing, integrity check fail, disk full), rotate may STILL run
# against an empty dir and update its own heartbeat — exactly the
# pre-R11 pathology that left srilu with zero backups for weeks while
# the watchdog stayed green. Checking BOTH heartbeats closes the gap.
#
# Telegram delivery path is direct via the bot HTTP API (not via
# scout.alerter.send_telegram_message). Rationale per R6 PR review CRITICAL:
# 1. send_telegram_message takes 3 positional args including
#    aiohttp.ClientSession — easy to misuse from a sidecar script.
# 2. send_telegram_message swallows aiohttp errors and returns None — the
#    watchdog cannot tell delivery succeeded vs silently failed.
# 3. Constructing an aiohttp.ClientSession inline doubles the surface area
#    for embedded-Python syntax errors against $age_msg interpolation.
#
# A direct Python urllib POST checks status code explicitly, so any HTTP
# failure (404, 401, network) is observable and propagates exit-1 cleanly.
# Keep the bot token out of process argv; curl URLs expose it via ps.
#
# UV_BIN exists for compat with earlier draft and as a testability seam:
# pytest's _make_uv_stub overrides it with a recorder script.

set -euo pipefail

ROTATE_HEARTBEAT_FILE="${GECKO_BACKUP_HEARTBEAT_FILE:-/var/lib/gecko-alpha/backup-rotation/backup-last-ok}"
# Round 13 — create-heartbeat is OPTIONAL (empty-default). The systemd
# unit sets it explicitly in production via
# GECKO_BACKUP_CREATE_HEARTBEAT_FILE; bare-CLI invocations and the
# existing test suite (which only override the rotate heartbeat) skip
# the create check via the empty-default below. To enable the
# dual-check, set GECKO_BACKUP_CREATE_HEARTBEAT_FILE in the env or
# systemd unit.
CREATE_HEARTBEAT_FILE="${GECKO_BACKUP_CREATE_HEARTBEAT_FILE:-}"
STALE_AFTER_SEC="${GECKO_BACKUP_STALE_AFTER_SEC:-172800}"  # 48h
GECKO_REPO="${GECKO_REPO:-/root/gecko-alpha}"
ENV_FILE="${GECKO_ENV_FILE:-$GECKO_REPO/.env}"
# UV_BIN retained as testability seam — when the pytest stub points UV_BIN
# at a stub script, the watchdog calls it instead of the inline curl path.
UV_BIN="${UV_BIN:-}"

now=$(date +%s)

# Returns "OK | STALE_MISSING | STALE_CORRUPT | STALE_AGE:<sec>" + age_sec
# via stdout (single line). Caller parses.
check_heartbeat() {
    local file="$1"
    if [[ ! -f "$file" ]]; then
        echo "STALE_MISSING"
        return
    fi
    local last_ok
    last_ok=$(cat "$file" 2>/dev/null || true)
    # R5 + R6 CRITICAL: validate heartbeat content. Empty / non-numeric
    # (corrupt mid-write, manual `: > heartbeat`, fs full) must NOT die in
    # bash arithmetic — instead treat as MISSING and alert.
    if [[ ! "$last_ok" =~ ^[0-9]+$ ]]; then
        echo "STALE_CORRUPT:${last_ok}"
        return
    fi
    local age_sec=$(( now - last_ok ))
    if (( age_sec > STALE_AFTER_SEC )); then
        echo "STALE_AGE:${age_sec}"
    else
        echo "OK:${age_sec}"
    fi
}

rotate_result="$(check_heartbeat "$ROTATE_HEARTBEAT_FILE")"
if [[ -n "$CREATE_HEARTBEAT_FILE" ]]; then
    create_result="$(check_heartbeat "$CREATE_HEARTBEAT_FILE")"
else
    create_result="SKIPPED"
fi

format_msg() {
    local label="$1"
    local file="$2"
    local result="$3"
    case "$result" in
        OK:*)            echo "${label} last_ok=${result#OK:}s ago" ;;
        STALE_MISSING)   echo "${label} heartbeat MISSING (${file})" ;;
        STALE_CORRUPT:*) echo "${label} heartbeat CORRUPT (${file}: ${result#STALE_CORRUPT:})" ;;
        STALE_AGE:*)     echo "${label} last_ok=${result#STALE_AGE:}s ago (STALE)" ;;
        SKIPPED)         echo "${label} check skipped (env unset)" ;;
        *)               echo "${label} UNKNOWN_RESULT=${result}" ;;
    esac
}

rotate_msg="$(format_msg "rotate" "$ROTATE_HEARTBEAT_FILE" "$rotate_result")"
create_msg="$(format_msg "create" "$CREATE_HEARTBEAT_FILE" "$create_result")"

is_stale=0
case "$rotate_result" in OK:*) ;; *) is_stale=1 ;; esac
# SKIPPED is intentionally NOT treated as stale — see CREATE_HEARTBEAT_FILE comment.
case "$create_result" in OK:*|SKIPPED) ;; *) is_stale=1 ;; esac

age_msg="${rotate_msg}; ${create_msg}"

if (( is_stale == 0 )); then
    echo "OK: gecko-backup heartbeats both fresh within ${STALE_AFTER_SEC}s — ${age_msg}"
    exit 0
fi

echo "STALE: gecko-backup heartbeat check FAILED — ${age_msg}"

# --- Alert delivery ---------------------------------------------------------

if [[ -n "$UV_BIN" ]]; then
    "$UV_BIN" stub-watchdog-alert "$age_msg" || true
    exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: env file $ENV_FILE not found; alert NOT delivered" >&2
    exit 4
fi

TELEGRAM_BOT_TOKEN="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
TELEGRAM_CHAT_ID="$(grep -E '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"

if [[ -z "$TELEGRAM_BOT_TOKEN" || "$TELEGRAM_BOT_TOKEN" == "placeholder" ]]; then
    echo "ERROR: TELEGRAM_BOT_TOKEN missing/placeholder in $ENV_FILE; alert NOT delivered" >&2
    exit 5
fi
if [[ -z "$TELEGRAM_CHAT_ID" || "$TELEGRAM_CHAT_ID" == "placeholder" ]]; then
    echo "ERROR: TELEGRAM_CHAT_ID missing/placeholder in $ENV_FILE; alert NOT delivered" >&2
    exit 5
fi

TEXT="⚠️ gecko-backup-watchdog: heartbeat stale — ${age_msg}. Check journalctl -u gecko-backup.service."

PYTHON_BIN="$(command -v python3 || command -v python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
    echo "ERROR: no python available for JSON encoding; alert NOT delivered" >&2
    exit 6
fi

TG_RESPONSE_FILE="/tmp/.gecko-tg-resp.$$"
HTTP_STATUS="$(GECKO_TG_TEXT="$TEXT" GECKO_TG_CHAT="$TELEGRAM_CHAT_ID" GECKO_TG_TOKEN="$TELEGRAM_BOT_TOKEN" GECKO_TG_RESPONSE_FILE="$TG_RESPONSE_FILE" "$PYTHON_BIN" -c '
import json
import os
import urllib.error
import urllib.request

response_file = os.environ["GECKO_TG_RESPONSE_FILE"]
payload = json.dumps({
    "chat_id": os.environ["GECKO_TG_CHAT"],
    "text": os.environ["GECKO_TG_TEXT"],
}).encode("utf-8")
request = urllib.request.Request(
    f"https://api.telegram.org/bot{os.environ['GECKO_TG_TOKEN']}/sendMessage",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read(500)
        if body:
            open(response_file, "wb").write(body)
        print(response.status)
except urllib.error.HTTPError as exc:
    body = exc.read(500)
    if body:
        open(response_file, "wb").write(body)
    print(exc.code)
except Exception as exc:
    open(response_file, "w", encoding="utf-8").write(repr(exc))
    print("000")
')"

if [[ "$HTTP_STATUS" != "200" ]]; then
    echo "ERROR: Telegram delivery failed (HTTP $HTTP_STATUS)" >&2
    if [[ -f "$TG_RESPONSE_FILE" ]]; then
        echo "RESPONSE: $(cat "$TG_RESPONSE_FILE" | head -c 500)" >&2
        rm -f "$TG_RESPONSE_FILE"
    fi
    exit 7
fi

rm -f "$TG_RESPONSE_FILE"
echo "ALERT DELIVERED: HTTP $HTTP_STATUS"
exit 1
