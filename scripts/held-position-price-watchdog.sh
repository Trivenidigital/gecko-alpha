#!/usr/bin/env bash
# held-position-price-watchdog — alert if any open paper_trade has a
# price_cache row older than STALE_AFTER_MIN or missing entirely.
#
# Co-shipped with the held-position price-refresh lane per §12a + §12c-narrow
# discipline. The lane itself is the fix; this watchdog catches the case where
# the lane silently breaks (e.g., CG returns 429 repeatedly, network blip,
# regression). See tasks/plan_held_position_price_freshness.md.
#
# Telegram delivery is direct curl-style (NOT scout.alerter.send_telegram_message)
# matching the documented choice in scripts/gecko-backup-watchdog.sh: the
# alerter requires aiohttp.ClientSession, swallows errors silently, and can't
# propagate HTTP failures cleanly. Curl-direct lets bash itself check status.
#
# Hysteresis: alerts only when count > 0 across 3 consecutive runs, to avoid
# spurious single-cycle blips during transient CG failures. Hysteresis state
# lives in a counter file under /var/lib/gecko-alpha/held-position-watchdog/.

set -euo pipefail

GECKO_REPO="${GECKO_REPO:-/root/gecko-alpha}"
DB_PATH="${GECKO_DB_PATH:-$GECKO_REPO/scout.db}"
ENV_FILE="${GECKO_ENV_FILE:-$GECKO_REPO/.env}"
STALE_AFTER_MIN="${HELD_POSITION_STALE_AFTER_MIN:-30}"
HYSTERESIS_CONSECUTIVE="${HELD_POSITION_WATCHDOG_HYSTERESIS:-3}"
STATE_DIR="${HELD_POSITION_WATCHDOG_STATE_DIR:-/var/lib/gecko-alpha/held-position-watchdog}"
COUNTER_FILE="$STATE_DIR/consecutive_stale_count"

mkdir -p "$STATE_DIR"

if [[ ! -f "$DB_PATH" ]]; then
    echo "ERROR: DB not found at $DB_PATH" >&2
    exit 4
fi

# Query: count open paper_trades whose price_cache row is missing OR
# older than STALE_AFTER_MIN minutes.
STALE_COUNT="$(sqlite3 "$DB_PATH" "
SELECT COUNT(*) FROM paper_trades pt
LEFT JOIN price_cache pc ON pt.token_id = pc.coin_id
WHERE pt.status = 'open'
  AND (pc.updated_at IS NULL
       OR (julianday('now') - julianday(pc.updated_at)) * 24.0 * 60.0 > $STALE_AFTER_MIN);
" 2>&1)"

if ! [[ "$STALE_COUNT" =~ ^[0-9]+$ ]]; then
    echo "ERROR: sqlite query failed: $STALE_COUNT" >&2
    exit 4
fi

echo "stale_count=$STALE_COUNT threshold=${STALE_AFTER_MIN}min"

# Update hysteresis counter
if (( STALE_COUNT > 0 )); then
    if [[ -f "$COUNTER_FILE" ]]; then
        prev=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)
        [[ "$prev" =~ ^[0-9]+$ ]] || prev=0
    else
        prev=0
    fi
    consecutive=$(( prev + 1 ))
    echo "$consecutive" > "$COUNTER_FILE"
else
    # Reset counter on clean run
    consecutive=0
    echo "0" > "$COUNTER_FILE"
fi

if (( STALE_COUNT == 0 )); then
    echo "OK: 0 held positions with stale price_cache"
    exit 0
fi

if (( consecutive < HYSTERESIS_CONSECUTIVE )); then
    echo "PENDING: stale_count=$STALE_COUNT consecutive=$consecutive/${HYSTERESIS_CONSECUTIVE} (no alert yet)"
    exit 0
fi

# Worst-offender details for alert body (best-effort; if it fails, still alert
# with just the count rather than block delivery).
WORST="$(sqlite3 "$DB_PATH" "
SELECT pt.symbol || ' (' || pt.token_id || '): ' ||
       CASE WHEN pc.updated_at IS NULL THEN 'NO CACHE ROW'
            ELSE ROUND((julianday('now') - julianday(pc.updated_at)) * 24.0, 1) || 'h stale'
       END
FROM paper_trades pt
LEFT JOIN price_cache pc ON pt.token_id = pc.coin_id
WHERE pt.status = 'open'
  AND (pc.updated_at IS NULL
       OR (julianday('now') - julianday(pc.updated_at)) * 24.0 * 60.0 > $STALE_AFTER_MIN)
ORDER BY (CASE WHEN pc.updated_at IS NULL THEN 999999 ELSE julianday('now') - julianday(pc.updated_at) END) DESC
LIMIT 1;
" 2>/dev/null || echo "(detail-query failed)")"

echo "STALE THRESHOLD CROSSED: $STALE_COUNT held positions stale > ${STALE_AFTER_MIN}min for $consecutive consecutive runs"
echo "WORST: $WORST"

# --- Alert delivery -------------------------------------------------------

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

TEXT="⚠️ held-position-price-watchdog: $STALE_COUNT open paper trades have price_cache rows stale > ${STALE_AFTER_MIN}min for $consecutive consecutive runs. Worst: $WORST. Check held_position_refresh_summary in journalctl -u gecko-pipeline."

PYTHON_BIN="$(command -v python3 || command -v python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
    echo "ERROR: no python available for JSON encoding; alert NOT delivered" >&2
    exit 6
fi

PAYLOAD="$(GECKO_TG_TEXT="$TEXT" GECKO_TG_CHAT="$TELEGRAM_CHAT_ID" "$PYTHON_BIN" -c '
import json, os
print(json.dumps({"chat_id": os.environ["GECKO_TG_CHAT"], "text": os.environ["GECKO_TG_TEXT"]}))
')"

HTTP_STATUS="$(curl -s -o /tmp/.gecko-held-tg-resp.$$ -w '%{http_code}' \
    -X POST \
    -H 'Content-Type: application/json' \
    -d "$PAYLOAD" \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" || echo 000)"

if [[ "$HTTP_STATUS" != "200" ]]; then
    echo "ERROR: Telegram delivery failed (HTTP $HTTP_STATUS)" >&2
    if [[ -f "/tmp/.gecko-held-tg-resp.$$" ]]; then
        echo "RESPONSE: $(cat /tmp/.gecko-held-tg-resp.$$ | head -c 500)" >&2
        rm -f "/tmp/.gecko-held-tg-resp.$$"
    fi
    exit 7
fi

rm -f "/tmp/.gecko-held-tg-resp.$$"
echo "ALERT DELIVERED: HTTP $HTTP_STATUS (stale_count=$STALE_COUNT consecutive=$consecutive)"
exit 1
