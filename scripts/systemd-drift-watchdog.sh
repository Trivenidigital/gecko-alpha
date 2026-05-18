#!/usr/bin/env bash
# systemd-drift-watchdog — detect drift between repo systemd/*.{service,timer}
# and prod /etc/systemd/system/<name>, plus enumerate unexpected drop-ins
# (<unit>.d/*.conf). Alert via Telegram on drift; silent-suppress identical
# drift-sets via sha256 ack tombstone.
#
# BL-NEW-SYSTEMD-DRIFT-PRECOMMIT-HOOK (cycle 10). Modeled on
# scripts/gecko-backup-watchdog.sh (curl-direct Telegram path; UV_BIN stub
# testability seam at line 27 there).
#
# BL-NEW-WATCHDOG-SYMLINK-AND-MAXTIME-BACKPORT (cycle 14) — backport of
# PR-#156 cron-drift-watchdog hardening for parity:
#  - mktemp response file (R2 #4 — symlink-attack hardening)
#  - curl --max-time 30 (R2 #12 — bounds held-lock window if Telegram hangs)
#  - ACK_DIR unwritable → exit 9 (cron-PR review-2 P2 — replaces silent WARN
#    that would then bash-crash under set -e on `exec 9>"$LOCK_FILE"`)
#  - UV_BIN refuse-in-prod guard (cron-PR R1 #15 — accidental UV_BIN in
#    prod env would silently absorb alerts; opt-in via env)
#  - leading-whitespace tolerance in .env parsing (false-negative exit 5
#    when an operator indents the keys)
#
# Exit codes:
#   0  CLEAN (no drift; heartbeat-file touched)
#   1  DRIFT alerted (or silently suppressed via tombstone match)
#   4  ENV_FILE missing (UV_BIN empty path only)
#   5  TELEGRAM_BOT_TOKEN / CHAT_ID missing or placeholder
#   6  no python3 for JSON encoding OR UV_BIN set without opt-in
#   7  Telegram HTTP_STATUS != 200 (ACK NOT written — re-alerts next fire)
#   9  ACK_DIR cannot be created (state-dir unwritable; cannot operate)
#
# V47/V48 design folds applied:
# - Process substitution `while ... done < <(find ... -print0 | sort -z)`
#   instead of pipe (subshell would lose DRIFT_REPORT accumulation).
# - Pre-hash sort of DRIFT_REPORT for filesystem-order-independent hash.
# - HTTP-failure path writes NO ack (next fire intentionally re-alerts).

set -euo pipefail

REPO_DIR="${GECKO_REPO:-/root/gecko-alpha}"
PROD_SYSTEMD_DIR="${PROD_SYSTEMD_DIR:-/etc/systemd/system}"
ENV_FILE="${GECKO_ENV_FILE:-$REPO_DIR/.env}"
ACK_DIR="${SYSTEMD_DRIFT_ACK_DIR:-/var/lib/gecko-alpha/systemd-drift-watchdog}"
ACK_FILE="$ACK_DIR/last_alerted_hash"
HEARTBEAT_FILE="${SYSTEMD_DRIFT_HEARTBEAT_FILE:-$ACK_DIR/heartbeat}"
LOCK_FILE="$ACK_DIR/.lock"
UV_BIN="${UV_BIN:-}"

# Direction-B prefix filter — units we KNOW belong to gecko-alpha. A future
# unit added prod-side outside these prefixes would be silently invisible.
# Risk-register row in design D5 flags this if naming conventions change.
DIRB_PATTERNS=(
    "gecko-*.service"
    "gecko-*.timer"
    "minara-*.service"
    "minara-*.timer"
    "systemd-drift-watchdog.*"
)

# --- Bootstrap ----------------------------------------------------------

# BL-NEW-WATCHDOG-SYMLINK-AND-MAXTIME-BACKPORT (cron-PR R1 #15 fold):
# refuse stub path in prod. UV_BIN accidentally set in operator env would
# silently absorb alert delivery, write ACK, never notify Telegram.
# Tests opt-in via GECKO_WATCHDOG_ALLOW_UV_STUB=1.
if [[ -n "$UV_BIN" && -z "${GECKO_WATCHDOG_ALLOW_UV_STUB:-}" ]]; then
    echo "ERROR: UV_BIN set ($UV_BIN) but GECKO_WATCHDOG_ALLOW_UV_STUB not opted-in; refusing stub path to prevent silent alert suppression in prod" >&2
    exit 6
fi

# BL-NEW-WATCHDOG-SYMLINK-AND-MAXTIME-BACKPORT (cron-PR review-2 P2 fold):
# previously `mkdir -p` failure only warned, but the subsequent
# `exec 9>"$LOCK_FILE"` would fail abruptly under set -e with a bash error
# message harder to grep than a controlled exit. Now exit 9 with a clear
# stderr message.
if ! mkdir -p "$ACK_DIR" 2>/dev/null; then
    echo "ERROR: failed to mkdir $ACK_DIR; cannot proceed without ack-tombstone state" >&2
    exit 9
fi
chmod 0700 "$ACK_DIR" 2>/dev/null || true

# V48 SHOULD-FIX — flock prevents timer-race on ACK_FILE non-atomic
# read-modify-write. -n = non-blocking; if held, exit 0 silently
# (next fire will run). Use FD 9 wrapped around the entire run.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "OK: skipping run — previous invocation still holds $LOCK_FILE"
    exit 0
fi

# --- Direction-A: repo → prod -------------------------------------------

DRIFT_LINES=()

# V47 MUST-FIX — process substitution preserves outer-scope DRIFT_LINES
# (piped `while` would run in subshell and lose appends at loop exit).
# V48 MUST-FIX — sort -z makes filesystem-order irrelevant.
while IFS= read -r -d '' f; do
    name=$(basename "$f")
    if [[ ! -f "$PROD_SYSTEMD_DIR/$name" ]]; then
        DRIFT_LINES+=("DRIFT: $name (missing in prod)")
        continue
    fi
    # V45 MUST-FIX #2 — `if ! diff ...` guard so set -e doesn't kill the loop
    # on the FIRST drift detection (bare `diff -q` returns 1 = "differ" which
    # would trip set -e mid-loop).
    if ! diff -q "$f" "$PROD_SYSTEMD_DIR/$name" >/dev/null 2>&1; then
        DRIFT_LINES+=("DRIFT: $name")
    fi
    # V45 SHOULD-FIX — drop-in detection via compgen -G
    if compgen -G "$PROD_SYSTEMD_DIR/${name}.d/*.conf" >/dev/null 2>&1; then
        DRIFT_LINES+=("DROP-IN PRESENT: ${name}.d/")
    fi
done < <(find "$REPO_DIR/systemd" -maxdepth 1 -type f \( -name "*.service" -o -name "*.timer" \) -print0 2>/dev/null | sort -z)

# --- Direction-B: prod → repo (V45 MUST-FIX #3) -------------------------

# Build the find -name expressions from DIRB_PATTERNS.
DIRB_FIND_ARGS=()
for pat in "${DIRB_PATTERNS[@]}"; do
    DIRB_FIND_ARGS+=(-o -name "$pat")
done
# Drop the leading -o so `find` accepts the args.
DIRB_FIND_ARGS=("${DIRB_FIND_ARGS[@]:1}")

while IFS= read -r -d '' p; do
    name=$(basename "$p")
    if [[ ! -f "$REPO_DIR/systemd/$name" ]]; then
        DRIFT_LINES+=("UNTRACKED PROD UNIT: $name")
    fi
done < <(find "$PROD_SYSTEMD_DIR" -maxdepth 1 -type f \( "${DIRB_FIND_ARGS[@]}" \) -print0 2>/dev/null | sort -z)

# --- Hash + ack-tombstone (V46 MUST-FIX) --------------------------------

# Stable serialization: sort the accumulated lines BEFORE hashing so any
# order perturbation from the two find walks doesn't churn the hash.
DRIFT_REPORT_SORTED="$(printf '%s\n' "${DRIFT_LINES[@]:-}" | grep -v '^$' | sort || true)"

if [[ -z "$DRIFT_REPORT_SORTED" ]]; then
    # CLEAN — touch heartbeat, clear ack-tombstone for next regression
    touch "$HEARTBEAT_FILE" 2>/dev/null || true
    rm -f "$ACK_FILE" 2>/dev/null || true
    echo "OK: 0 drifts, 0 drop-ins, 0 untracked prod units"
    exit 0
fi

DRIFT_HASH=$(printf '%s' "$DRIFT_REPORT_SORTED" | sha256sum | awk '{print $1}')

# Silent suppress if hash unchanged
if [[ -f "$ACK_FILE" ]]; then
    PRIOR_HASH=$(cat "$ACK_FILE" 2>/dev/null || true)
    if [[ "$PRIOR_HASH" == "$DRIFT_HASH" ]]; then
        echo "SUPPRESS: drift set unchanged (hash $DRIFT_HASH); see prior alert"
        exit 1
    fi
fi

# --- Alert -------------------------------------------------------------

# V45 SHOULD-FIX — truncate body to keep Telegram payload under 4096
# (cap content at 3500, add footer if truncated).
MAX_BODY=3500
TRUNC_FOOTER=""
if [[ "${#DRIFT_REPORT_SORTED}" -gt "$MAX_BODY" ]]; then
    DRIFT_REPORT_SORTED="${DRIFT_REPORT_SORTED:0:$MAX_BODY}"
    TRUNC_FOOTER=$'\n(N more drifts truncated — see journalctl -u systemd-drift-watchdog)'
fi

ALERT_BODY="⚠️ systemd-drift-watchdog: drift detected
$DRIFT_REPORT_SORTED$TRUNC_FOOTER"

# UV_BIN stub path (tests)
if [[ -n "$UV_BIN" ]]; then
    "$UV_BIN" stub-watchdog-alert "$ALERT_BODY" || true
    # Stub path treats invocation as success; write ack.
    if ! echo "$DRIFT_HASH" > "$ACK_FILE" 2>/dev/null; then
        echo "WARN: ack write failed; alert may re-fire next run" >&2
    fi
    exit 1
fi

# Prod path: curl-direct (mirrors gecko-backup-watchdog.sh:71-113)
if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: env file $ENV_FILE not found; alert NOT delivered" >&2
    exit 4
fi

# BL-NEW-WATCHDOG-SYMLINK-AND-MAXTIME-BACKPORT: tolerate leading whitespace
# on .env lines (previously a stray indent caused a silent exit-5 false negative).
TELEGRAM_BOT_TOKEN="$(grep -E '^[[:space:]]*TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | head -1 | sed -E 's/^[[:space:]]*TELEGRAM_BOT_TOKEN=//' | tr -d '"' | tr -d "'")"
TELEGRAM_CHAT_ID="$(grep -E '^[[:space:]]*TELEGRAM_CHAT_ID=' "$ENV_FILE" | head -1 | sed -E 's/^[[:space:]]*TELEGRAM_CHAT_ID=//' | tr -d '"' | tr -d "'")"

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

# BL-NEW-WATCHDOG-SYMLINK-AND-MAXTIME-BACKPORT (R2 #4 + #12):
# mktemp-based response file (symlink-attack-safe; replaces predictable PID path)
# + curl --max-time 30 (bounds held-lock window if Telegram hangs).
# PR-#159 R2 M1 fold: trap registered BEFORE mktemp via ${RESP_FILE:+...} idiom
# (matches cron-drift-watchdog.sh pattern); eliminates the microsecond
# trap-not-yet-set leak window if SIGTERM arrives mid-mktemp.
RESP_FILE=""
trap 'rm -f ${RESP_FILE:+"$RESP_FILE"}' EXIT
RESP_FILE="$(mktemp -t gecko-systemd-drift-resp.XXXXXX)"

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
    # V48 MUST-FIX — DO NOT write ACK_FILE on HTTP failure
    exit 7
fi

# Alert succeeded — write ack. V48 SHOULD-FIX: warn-but-don't-kill if
# ack-write fails (e.g. disk full); the alert was delivered, so the
# operator already knows.
if ! echo "$DRIFT_HASH" > "$ACK_FILE" 2>/dev/null; then
    echo "WARN: systemd_drift_ack_write_failed — alert delivered but tombstone unwritable; alert WILL re-fire next run" >&2
fi

echo "ALERTED: HTTP 200; hash=$DRIFT_HASH; ${#DRIFT_LINES[@]} drift items"
exit 1
