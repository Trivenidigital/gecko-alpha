#!/usr/bin/env bash
# revival-verdict-watchdog — alert when signal_params_audit soak_verdict
# rows of the form keep_on_provisional_until_<ISO> have passed their
# embedded expiry without a fresh operator verdict.
#
# Design: tasks/plan_revival_verdict_watchdog_2026_05_19.md (PR #185).
# Read-only: never mutates signal_params_audit. Alert-only; auto-revoke
# explicitly rejected per CLAUDE.md §12b discipline.
#
# Telegram delivery is curl-direct (NOT scout.alerter.send_telegram_message)
# matching the documented choice in scripts/gecko-backup-watchdog.sh.
# parse_mode is omitted entirely — message contains signal_type names
# with underscores that Markdown would consume.
#
# Per-signal alert idempotency under STATE_DIR avoids daily spam; the
# default re-alert window is 168h (weekly) per signal.
#
# Exit codes:
#   0  No expired provisional verdicts OR alert suppressed by idempotency
#   1  Alert delivered
#   4  DB / SQL / malformed-ISO error
#   5  Telegram token/chat missing or placeholder
#   6  python missing (JSON encoding)
#   7  Telegram HTTP delivery failed

set -euo pipefail

GECKO_REPO="${GECKO_REPO:-/root/gecko-alpha}"
DB_PATH="${GECKO_DB_PATH:-$GECKO_REPO/scout.db}"
ENV_FILE="${GECKO_ENV_FILE:-$GECKO_REPO/.env}"
STATE_DIR="${REVIVAL_VERDICT_WATCHDOG_STATE_DIR:-/var/lib/gecko-alpha/revival-verdict-watchdog}"
REALERT_HOURS="${REVIVAL_VERDICT_WATCHDOG_REALERT_HOURS:-168}"

mkdir -p "$STATE_DIR"

if [[ ! -f "$DB_PATH" ]]; then
    echo "ERROR: DB not found at $DB_PATH" >&2
    exit 4
fi

# Now: override for deterministic tests; default is system UTC.
if [[ -n "${REVIVAL_VERDICT_WATCHDOG_NOW_OVERRIDE:-}" ]]; then
    NOW_ISO="$REVIVAL_VERDICT_WATCHDOG_NOW_OVERRIDE"
else
    NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi

# Strip trailing Z / +00:00 from $NOW_ISO so the parser path is uniform.
# (We always treat NOW as UTC; the strip is purely about epoch conversion
# via sqlite's strftime which accepts naive UTC.)
NOW_PARSE="${NOW_ISO%Z}"
NOW_PARSE="${NOW_PARSE%+00:00}"

# ---------------------------------------------------------------------------
# SQL: most-recent soak_verdict row per signal_type.
# ---------------------------------------------------------------------------
LATEST_ROWS="$(sqlite3 -separator $'\t' "$DB_PATH" "
WITH latest AS (
  SELECT signal_type, MAX(applied_at) AS max_at
  FROM signal_params_audit
  WHERE field_name = 'soak_verdict'
  GROUP BY signal_type
)
SELECT a.signal_type, a.new_value, a.applied_at
FROM signal_params_audit a
JOIN latest l
  ON l.signal_type = a.signal_type
 AND l.max_at      = a.applied_at
WHERE a.field_name = 'soak_verdict'
ORDER BY a.signal_type;
" 2>&1)" || {
    echo "ERROR: sqlite query failed: $LATEST_ROWS" >&2
    exit 4
}

# ---------------------------------------------------------------------------
# Helper: convert iso string to "naive UTC" epoch seconds via sqlite.
# Accepts the 5 shapes from criterion 7a + rejects everything else (return
# empty string).
# ---------------------------------------------------------------------------
iso_to_epoch() {
    local iso="$1"
    local stripped="$iso"
    # Strip a single trailing Z (criterion 7a shape ii / v)
    stripped="${stripped%Z}"
    # Strip a single trailing +00:00 (shape iii)
    stripped="${stripped%+00:00}"
    # Reject anything that still contains a + or another - timezone marker
    # past position 10 (date is YYYY-MM-DD = 10 chars; offsets like
    # +05:30 are at position 19+).
    local tail="${stripped:10}"
    case "$tail" in
        *+*|*Z*) echo ""; return ;;
    esac
    # tail may contain a `-` for malformed input; reject if it does
    case "$tail" in
        *-*) echo ""; return ;;
    esac
    # Strip microseconds if present (shapes iv / v)
    stripped="${stripped%.*}"
    # Validate the surface shape: YYYY-MM-DDTHH:MM:SS exactly.
    if ! [[ "$stripped" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}$ ]]; then
        echo ""
        return
    fi
    # Convert via sqlite strftime — empty on parse failure.
    local epoch
    epoch="$(sqlite3 ":memory:" "SELECT CAST(strftime('%s', '$stripped') AS INTEGER);" 2>/dev/null || echo "")"
    if [[ -z "$epoch" || "$epoch" == "" ]]; then
        echo ""
        return
    fi
    echo "$epoch"
}

NOW_EPOCH="$(iso_to_epoch "$NOW_PARSE")"
if [[ -z "$NOW_EPOCH" ]]; then
    echo "ERROR: cannot parse NOW=$NOW_ISO" >&2
    exit 4
fi

# ---------------------------------------------------------------------------
# Iterate rows, classify each as: skip / future / expired / malformed.
# ---------------------------------------------------------------------------
EXPIRED_SIGNALS=()
EXPIRED_DETAILS=()
EXPIRED_COUNT=0
declare -A APPLIED_AT_BY_SIGNAL=()

PROV_PREFIX="keep_on_provisional_until_"

while IFS=$'\t' read -r signal_type new_value applied_at; do
    [[ -z "$signal_type" ]] && continue
    # Skip legacy verdicts
    if [[ "$new_value" != "${PROV_PREFIX}"* ]]; then
        continue
    fi
    iso_suffix="${new_value#$PROV_PREFIX}"
    expiry_epoch="$(iso_to_epoch "$iso_suffix")"
    if [[ -z "$expiry_epoch" ]]; then
        echo "ERROR: malformed ISO in signal_params_audit for signal_type=$signal_type new_value=$new_value applied_at=$applied_at" >&2
        echo "revival_verdict_watchdog_malformed signal_type=$signal_type applied_at=$applied_at"
        exit 4
    fi
    if (( expiry_epoch > NOW_EPOCH )); then
        # Future expiry; nothing to do.
        continue
    fi
    EXPIRED_COUNT=$(( EXPIRED_COUNT + 1 ))
    EXPIRED_SIGNALS+=("$signal_type")
    age_sec=$(( NOW_EPOCH - expiry_epoch ))
    age_hours=$(( age_sec / 3600 ))
    EXPIRED_DETAILS+=("$signal_type: applied $applied_at, expired $iso_suffix (${age_hours}h ago)")
    APPLIED_AT_BY_SIGNAL["$signal_type"]="$applied_at"
done <<< "$LATEST_ROWS"

echo "revival_verdict_watchdog_run expired_count=$EXPIRED_COUNT now=$NOW_ISO realert_hours=$REALERT_HOURS"

if (( EXPIRED_COUNT == 0 )); then
    exit 0
fi

# ---------------------------------------------------------------------------
# Idempotency: filter expired list against per-signal state files.
# Re-alert when (a) no prior alert, (b) prior alert is older than the
# re-alert window, or (c) prior alert pre-dates the current row's
# applied_at (operator emitted a fresh verdict since the last alert).
# ---------------------------------------------------------------------------
ALERT_SIGNALS=()
ALERT_DETAILS=()

for i in "${!EXPIRED_SIGNALS[@]}"; do
    sig="${EXPIRED_SIGNALS[$i]}"
    detail="${EXPIRED_DETAILS[$i]}"
    state_file="$STATE_DIR/last_alert_$sig"
    should_alert=1
    if [[ -f "$state_file" ]]; then
        prior_iso="$(head -n1 "$state_file" | tr -d '[:space:]')"
        prior_epoch="$(iso_to_epoch "${prior_iso%Z}")"
        if [[ -n "$prior_epoch" ]]; then
            applied_iso="${APPLIED_AT_BY_SIGNAL[$sig]}"
            applied_epoch="$(iso_to_epoch "${applied_iso%Z}")"
            # If applied_at > prior_alert, this is a fresh verdict event
            # → bypass re-alert window (criterion 8).
            if [[ -n "$applied_epoch" ]] && (( applied_epoch > prior_epoch )); then
                should_alert=1
            else
                # Within re-alert window?
                age_h=$(( (NOW_EPOCH - prior_epoch) / 3600 ))
                if (( age_h < REALERT_HOURS )); then
                    should_alert=0
                fi
            fi
        fi
    fi
    if (( should_alert == 1 )); then
        ALERT_SIGNALS+=("$sig")
        ALERT_DETAILS+=("$detail")
    fi
done

if (( ${#ALERT_SIGNALS[@]} == 0 )); then
    echo "revival_verdict_watchdog_realert_skipped expired_count=$EXPIRED_COUNT (all within re-alert window)"
    exit 0
fi

# ---------------------------------------------------------------------------
# Compose summary alert body (criterion 2a — single summary, multi-row).
# ---------------------------------------------------------------------------
ALERT_BODY="⚠ revival-verdict-watchdog: ${#ALERT_SIGNALS[@]} signal(s) have expired provisional verdicts."$'\n\n'"EXPIRED:"
for d in "${ALERT_DETAILS[@]}"; do
    ALERT_BODY+=$'\n'"- $d"
done
ALERT_BODY+=$'\n\n'"Action: re-run the revival-criteria evaluator. If PASS, emit a fresh keep_on_provisional_until_<iso> audit row. If FAIL, follow runbook for the affected signal."

# ---------------------------------------------------------------------------
# Telegram delivery (plain text, no parse_mode).
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: env file $ENV_FILE not found; alert NOT delivered" >&2
    exit 5
fi

# Tolerate leading whitespace in .env per PR #161 pattern.
TELEGRAM_BOT_TOKEN="$(grep -E '^[[:space:]]*TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | head -1 | sed -E 's/^[[:space:]]*TELEGRAM_BOT_TOKEN=//' | tr -d '"' | tr -d "'" | sed 's/[[:space:]]*$//')"
TELEGRAM_CHAT_ID="$(grep -E '^[[:space:]]*TELEGRAM_CHAT_ID=' "$ENV_FILE" | head -1 | sed -E 's/^[[:space:]]*TELEGRAM_CHAT_ID=//' | tr -d '"' | tr -d "'" | sed 's/[[:space:]]*$//')"

if [[ -z "$TELEGRAM_BOT_TOKEN" || "$TELEGRAM_BOT_TOKEN" == "placeholder" ]]; then
    echo "ERROR: TELEGRAM_BOT_TOKEN missing/placeholder in $ENV_FILE; alert NOT delivered" >&2
    exit 5
fi
if [[ -z "$TELEGRAM_CHAT_ID" || "$TELEGRAM_CHAT_ID" == "placeholder" ]]; then
    echo "ERROR: TELEGRAM_CHAT_ID missing/placeholder in $ENV_FILE; alert NOT delivered" >&2
    exit 5
fi

PYTHON_BIN="$(command -v python3 || command -v python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
    echo "ERROR: no python available for JSON encoding; alert NOT delivered" >&2
    exit 6
fi

PAYLOAD="$(GECKO_TG_TEXT="$ALERT_BODY" GECKO_TG_CHAT="$TELEGRAM_CHAT_ID" "$PYTHON_BIN" -c '
import json, os
print(json.dumps({"chat_id": os.environ["GECKO_TG_CHAT"], "text": os.environ["GECKO_TG_TEXT"]}))
')"

# §12b log-pair: dispatched + delivered.
echo "revival_verdict_watchdog_alert_dispatched signals=${#ALERT_SIGNALS[@]}"

HTTP_STATUS="$(curl -s -o /tmp/.revival-verdict-tg-resp.$$ -w '%{http_code}' \
    -X POST \
    -H 'Content-Type: application/json' \
    -d "$PAYLOAD" \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" || echo 000)"

if [[ "$HTTP_STATUS" != "200" ]]; then
    echo "ERROR: Telegram delivery failed (HTTP $HTTP_STATUS)" >&2
    if [[ -f "/tmp/.revival-verdict-tg-resp.$$" ]]; then
        echo "RESPONSE: $(head -c 500 /tmp/.revival-verdict-tg-resp.$$)" >&2
        rm -f "/tmp/.revival-verdict-tg-resp.$$"
    fi
    exit 7
fi

rm -f "/tmp/.revival-verdict-tg-resp.$$"
echo "revival_verdict_watchdog_alert_delivered http_status=$HTTP_STATUS signals=${#ALERT_SIGNALS[@]}"

# Write state files NOW (after successful delivery).
for sig in "${ALERT_SIGNALS[@]}"; do
    echo "$NOW_ISO" > "$STATE_DIR/last_alert_$sig"
done

exit 1
