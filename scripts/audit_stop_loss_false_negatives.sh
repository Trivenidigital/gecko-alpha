#!/usr/bin/env bash
set -euo pipefail

# Offline audit gate for post-held-refresh stop-loss false negatives.
# Intended to run from the gecko-alpha repo root on the VPS:
#   scripts/audit_stop_loss_false_negatives.sh [scout.db]
#
# Runner-board label is offline-only:
#   first post-open gainers_comparisons.peak_gain_pct >= 75 OR
#   first post-open momentum_7d.price_change_7d >= 75.
# Do not use this future-looking label for live ranking.
#
# Stop-loss false-negative gate requires first_runner_at > closed_at.
# The 2026-05-26 closeout found 15/42 historical gainers_early rows where
# runner_board_ts was before stop_close_ts; counting those as "stopped before
# runner" overstated the false-negative bucket by 35.7%.
#
# With --alert, the script sends one plain-text Telegram alert only when the
# gate leaves WAIT_MORE_MATURE_DATA. State files prevent repeated alerts for
# the same gate_status. Cron logs still show every run.

DB_PATH="${1:-scout.db}"
HELD_REFRESH_ENABLED_AT="${HELD_REFRESH_ENABLED_AT:-2026-05-18T16:16:07+00:00}"
MATURITY_HOURS="${MATURITY_HOURS:-131}"
ALL_FAMILY_THRESHOLD="${ALL_FAMILY_THRESHOLD:-30}"
GAINERS_EARLY_THRESHOLD="${GAINERS_EARLY_THRESHOLD:-15}"
CALENDAR_BACKSTOP="${CALENDAR_BACKSTOP:-2026-08-26}"
BROAD_PNL_GAINERS_EARLY_THRESHOLD="${BROAD_PNL_GAINERS_EARLY_THRESHOLD:-20}"
STATE_DIR="${STOP_LOSS_FN_AUDIT_STATE_DIR:-/var/lib/gecko-alpha/stop-loss-fn-audit}"
ENV_FILE="${GECKO_ENV_FILE:-.env}"
ALERT=0

if [[ "${1:-}" == "--alert" ]]; then
  ALERT=1
  shift
  DB_PATH="${1:-scout.db}"
fi

if [[ ! -f "$DB_PATH" ]]; then
  echo "ERROR db_not_found path=$DB_PATH" >&2
  exit 1
fi

SQL_QUERY="
WITH base AS (
  SELECT
    pt.id,
    pt.token_id,
    UPPER(pt.symbol) AS symbol,
    pt.signal_type,
    pt.exit_reason,
    pt.opened_at,
    pt.closed_at,
    pt.pnl_pct,
    pt.peak_pct,
    (
      SELECT MIN(gc.appeared_on_gainers_at)
      FROM gainers_comparisons gc
      WHERE gc.coin_id = pt.token_id
        AND gc.appeared_on_gainers_at >= pt.opened_at
        AND COALESCE(gc.peak_gain_pct, 0) >= 75
    ) AS first_gainer_runner_at,
    (
      SELECT MAX(gc.peak_gain_pct)
      FROM gainers_comparisons gc
      WHERE gc.coin_id = pt.token_id
        AND gc.appeared_on_gainers_at >= pt.opened_at
    ) AS later_gainer_peak,
    (
      SELECT MIN(m.detected_at)
      FROM momentum_7d m
      WHERE (m.coin_id = pt.token_id OR UPPER(m.symbol) = UPPER(pt.symbol))
        AND m.detected_at >= pt.opened_at
        AND COALESCE(m.price_change_7d, 0) >= 75
    ) AS first_momentum_runner_at,
    (
      SELECT MAX(m.price_change_7d)
      FROM momentum_7d m
      WHERE (m.coin_id = pt.token_id OR UPPER(m.symbol) = UPPER(pt.symbol))
        AND m.detected_at >= pt.opened_at
    ) AS later_7d_peak
  FROM paper_trades pt
  WHERE pt.signal_type IN ('gainers_early', 'losers_contrarian', 'trending_catch')
    AND pt.opened_at >= datetime('now', '-45 days')
),
labeled AS (
  SELECT
    *,
    CASE
      WHEN first_gainer_runner_at IS NOT NULL AND first_momentum_runner_at IS NOT NULL
        THEN MIN(first_gainer_runner_at, first_momentum_runner_at)
      WHEN first_gainer_runner_at IS NOT NULL THEN first_gainer_runner_at
      WHEN first_momentum_runner_at IS NOT NULL THEN first_momentum_runner_at
      ELSE NULL
    END AS first_runner_at
  FROM base
),
flags AS (
  SELECT
    *,
    CASE WHEN opened_at >= '$HELD_REFRESH_ENABLED_AT' THEN 1 ELSE 0 END AS post_enable,
    CASE WHEN datetime(opened_at, '+$MATURITY_HOURS hours') < datetime('now') THEN 1 ELSE 0 END AS mature_open,
    CASE
      WHEN COALESCE(pnl_pct, 0) <= 0
       AND (
         COALESCE(later_gainer_peak, 0) >= 75
         OR COALESCE(later_7d_peak, 0) >= 75
         OR COALESCE(peak_pct, 0) >= 75
       )
      THEN 1 ELSE 0
    END AS offline_false_negative
  FROM labeled
),
counts AS (
  SELECT
    SUM(CASE WHEN post_enable=1 AND mature_open=1 AND offline_false_negative=1 AND exit_reason='stop_loss' AND first_runner_at > closed_at THEN 1 ELSE 0 END) AS all_family_n,
    SUM(CASE WHEN post_enable=1 AND mature_open=1 AND offline_false_negative=1 AND exit_reason='stop_loss' AND first_runner_at > closed_at AND signal_type='gainers_early' THEN 1 ELSE 0 END) AS gainers_early_n,
    SUM(CASE WHEN post_enable=1 AND mature_open=1 AND signal_type='gainers_early' AND closed_at IS NOT NULL AND pnl_pct IS NOT NULL THEN 1 ELSE 0 END) AS broad_pnl_gainers_early_n
  FROM flags
)
SELECT
  datetime('now') AS audit_utc,
  '$HELD_REFRESH_ENABLED_AT' AS held_refresh_enabled_at,
  $MATURITY_HOURS AS maturity_hours,
  all_family_n,
  $ALL_FAMILY_THRESHOLD AS all_family_threshold,
  gainers_early_n,
  $GAINERS_EARLY_THRESHOLD AS gainers_early_threshold,
  broad_pnl_gainers_early_n,
  $BROAD_PNL_GAINERS_EARLY_THRESHOLD AS broad_pnl_gainers_early_threshold,
  '$CALENDAR_BACKSTOP' AS calendar_backstop,
  CASE
    WHEN all_family_n >= $ALL_FAMILY_THRESHOLD THEN 'READY_REAUDIT_ALL_FAMILIES'
    WHEN gainers_early_n >= $GAINERS_EARLY_THRESHOLD THEN 'READY_REAUDIT_GAINERS_EARLY'
    WHEN date('now') >= date('$CALENDAR_BACKSTOP') THEN 'CLOSE_NOISE_FLOOR_BACKSTOP'
    ELSE 'WAIT_MORE_MATURE_DATA'
  END AS stop_loss_gate_status,
  CASE
    WHEN broad_pnl_gainers_early_n >= $BROAD_PNL_GAINERS_EARLY_THRESHOLD THEN 'READY_BROAD_PNL_RECHECK'
    ELSE 'WAIT_BROAD_PNL_MATURE_DATA'
  END AS broad_pnl_gate_status
FROM counts;
"

sqlite3 -header -column "$DB_PATH" "$SQL_QUERY"

if [[ "$ALERT" != "1" ]]; then
  exit 0
fi

mkdir -p "$STATE_DIR"

TSV="$(sqlite3 -noheader -separator $'\t' "$DB_PATH" "$SQL_QUERY")"
IFS=$'\t' read -r audit_utc held_at maturity_h all_n all_threshold ge_n ge_threshold broad_n broad_threshold backstop stop_status broad_status <<< "$TSV"

if [[ "$stop_status" == "WAIT_MORE_MATURE_DATA" && "$broad_status" == "WAIT_BROAD_PNL_MATURE_DATA" ]]; then
  echo "stop_loss_false_negative_audit_wait all_family_n=$all_n gainers_early_n=$ge_n broad_pnl_gainers_early_n=$broad_n backstop=$backstop"
  exit 0
fi

status_key="${stop_status}_${broad_status}"
state_file="$STATE_DIR/last_alert_status"
if [[ -f "$state_file" ]] && [[ "$(cat "$state_file")" == "$status_key" ]]; then
  echo "stop_loss_false_negative_audit_alert_suppressed status=$status_key"
  exit 0
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR env_not_found path=$ENV_FILE" >&2
  exit 5
fi

TELEGRAM_BOT_TOKEN="$(grep -E '^[[:space:]]*TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | head -1 | sed -E 's/^[[:space:]]*TELEGRAM_BOT_TOKEN=//' | sed -E 's/^[\"'\'']//; s/[\"'\''][[:space:]]*$//; s/[[:space:]]*$//')"
TELEGRAM_CHAT_ID="$(grep -E '^[[:space:]]*TELEGRAM_CHAT_ID=' "$ENV_FILE" | head -1 | sed -E 's/^[[:space:]]*TELEGRAM_CHAT_ID=//' | sed -E 's/^[\"'\'']//; s/[\"'\''][[:space:]]*$//; s/[[:space:]]*$//')"

if [[ -z "$TELEGRAM_BOT_TOKEN" || "$TELEGRAM_BOT_TOKEN" == "placeholder" || -z "$TELEGRAM_CHAT_ID" || "$TELEGRAM_CHAT_ID" == "placeholder" ]]; then
  echo "ERROR telegram_credentials_missing_or_placeholder env=$ENV_FILE" >&2
  exit 5
fi

PYTHON_BIN="$(command -v python3 || command -v python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  echo "ERROR python_not_found_for_json_payload" >&2
  exit 6
fi

ALERT_BODY="stop_loss_false_negative_audit gate changed
stop_loss_gate_status=$stop_status
broad_pnl_gate_status=$broad_status
all_family_n=$all_n/$all_threshold
gainers_early_n=$ge_n/$ge_threshold
broad_pnl_gainers_early_n=$broad_n/$broad_threshold
maturity_hours=$maturity_h
held_refresh_enabled_at=$held_at
calendar_backstop=$backstop

Action: re-run the stop-loss attribution audit before changing trading policy."

PAYLOAD="$(GECKO_TG_TEXT="$ALERT_BODY" GECKO_TG_CHAT="$TELEGRAM_CHAT_ID" "$PYTHON_BIN" -c 'import json, os; print(json.dumps({"chat_id": os.environ["GECKO_TG_CHAT"], "text": os.environ["GECKO_TG_TEXT"]}))')"

echo "stop_loss_false_negative_audit_alert_dispatched status=$status_key"
HTTP_STATUS="$(curl -s -o /tmp/.stop-loss-fn-audit-tg-resp.$$ -w '%{http_code}' \
  -X POST \
  -H 'Content-Type: application/json' \
  -d "$PAYLOAD" \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" || echo 000)"

if [[ "$HTTP_STATUS" != "200" ]]; then
  echo "ERROR telegram_delivery_failed http_status=$HTTP_STATUS" >&2
  if [[ -f "/tmp/.stop-loss-fn-audit-tg-resp.$$" ]]; then
    echo "RESPONSE: $(head -c 500 /tmp/.stop-loss-fn-audit-tg-resp.$$)" >&2
    rm -f "/tmp/.stop-loss-fn-audit-tg-resp.$$"
  fi
  exit 7
fi
rm -f "/tmp/.stop-loss-fn-audit-tg-resp.$$"
printf '%s' "$status_key" > "$state_file"
echo "stop_loss_false_negative_audit_alert_delivered status=$status_key"
