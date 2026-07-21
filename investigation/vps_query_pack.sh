#!/usr/bin/env bash
# investigation/vps_query_pack.sh — READ-ONLY forensic data collection.
# Run on the prod VPS:  bash investigation/vps_query_pack.sh > /tmp/gecko_forensics_$(date -u +%Y%m%d).txt
# Every query is a SELECT / PRAGMA / journalctl read. Nothing writes.
set -uo pipefail
DB="${GECKO_DB:-/root/gecko-alpha/scout.db}"
ENV_FILE="${GECKO_ENV:-/root/gecko-alpha/.env}"
Q() { echo; echo "=== $1 ==="; sqlite3 -readonly "$DB" "$2" 2>&1; }

# Threshold values are printed by EXACT-NAME allowlist only — never prefix
# matches (a prefix like NARRATIVE would also match NARRATIVE_API_KEY, and
# the recommended invocation redirects output into /tmp). As a second layer,
# any name containing KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL is REFUSED even
# if it appears in the allowlist (or in EXTRA_THRESHOLD_NAMES, a
# space-separated operator extension for ad-hoc non-secret knobs).
THRESHOLD_ALLOWLIST=(
  MIN_SCORE
  CONVICTION_THRESHOLD
  QUANT_WEIGHT
  NARRATIVE_WEIGHT
  CONVICTION_GATE_ENABLED
)
env_report() {
  echo "=== P0.env gate+flags (exact-name allowlist for threshold values; flags shown as set/unset) ==="
  local name line
  # Split EXTRA_THRESHOLD_NAMES without pathname expansion (a value like
  # '.*' must reach the grammar check as-is, never glob against the cwd).
  local -a extra=()
  read -ra extra <<< "${EXTRA_THRESHOLD_NAMES:-}"
  for name in "${THRESHOLD_ALLOWLIST[@]}" "${extra[@]}"; do
    # 1) Strict shell-variable-name grammar: a requested name may never be
    #    a pattern. This closes the regex bypass (NARRATIVE_API_.* passes a
    #    substring denylist yet would MATCH NARRATIVE_API_KEY under grep -E).
    if ! [[ "$name" =~ ^[A-Z_][A-Z0-9_]*$ ]]; then
      echo "$name: REJECTED (not a plain variable name — patterns never accepted)"
      continue
    fi
    # 2) Secret-word denylist on the validated identifier.
    case "$name" in
      *KEY*|*TOKEN*|*SECRET*|*PASSWORD*|*CREDENTIAL*)
        echo "$name: REFUSED (secret-like name never printed)"
        continue
        ;;
    esac
    # 3) LITERAL key comparison (no regex engine on the lookup path).
    line=$(awk -F= -v wanted="$name" '$1 == wanted { print; exit }' "$ENV_FILE" 2>/dev/null)
    if [[ -n "$line" ]]; then echo "$line"; else echo "$name unset"; fi
  done
  local f
  for f in DETECTION_ALERT_LANE_ENABLED MOVED_ALREADY_POSTMORTEM_ENABLED LIQUIDITY_ENRICHMENT_ENABLED RETIRE_DEAD_TABLES_ENABLED; do
    grep -q "^$f=" "$ENV_FILE" 2>/dev/null && echo "$f set: $(grep "^$f=" "$ENV_FILE")" || echo "$f unset"
  done
}

# Test seam: report only the (secret-safe) env section, touching nothing else.
if [[ "${1:-}" == "--env-report-only" ]]; then
  env_report
  exit 0
fi

echo "### gecko-alpha forensic snapshot $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "### DB: $DB ($(stat -c%s "$DB" 2>/dev/null) bytes)"

# ---------- Phase 0: what is actually running ----------
echo; echo "=== P0.processes ==="
systemctl is-active gecko-pipeline 2>&1
systemctl show gecko-pipeline -p ActiveEnterTimestamp -p MainPID 2>&1
echo; echo "=== P0.cron (managed block) ==="
crontab -l 2>/dev/null | sed -n '/BEGIN gecko-alpha/,/END gecko-alpha/p'
echo
env_report

Q "P0.last_writes_per_stage (freshness of each pipeline output)" "
SELECT 'candidates.first_seen_at'  AS stage, MAX(first_seen_at) FROM candidates
UNION ALL SELECT 'score_history',           MAX(scanned_at)    FROM score_history
UNION ALL SELECT 'trending_snapshots',      MAX(snapshot_at)   FROM trending_snapshots
UNION ALL SELECT 'gainers_snapshots',       MAX(snapshot_at)   FROM gainers_snapshots
UNION ALL SELECT 'alerts (conviction lane)',MAX(alerted_at)    FROM alerts
UNION ALL SELECT 'candidates.alerted_at',   MAX(alerted_at)    FROM candidates
UNION ALL SELECT 'tg_alert_log sent',       MAX(created_at)    FROM tg_alert_log WHERE outcome='sent'
UNION ALL SELECT 'paper_trades.opened_at',  MAX(opened_at)     FROM paper_trades
UNION ALL SELECT 'mirofish_jobs',           MAX(created_at)    FROM mirofish_jobs;"

Q "P0.last_alert_ever_by_lane" "
SELECT signal_type, MAX(created_at) AS last_sent, COUNT(*) AS total_sent
FROM tg_alert_log WHERE outcome='sent' GROUP BY signal_type ORDER BY last_sent DESC;"

Q "P0.score_distribution_60d (conviction + quant, vs gate)" "
SELECT COUNT(*) n,
       MAX(quant_score) max_q, MAX(conviction_score) max_conv,
       (SELECT quant_score FROM candidates WHERE first_seen_at>=datetime('now','-60 days') AND quant_score IS NOT NULL ORDER BY quant_score DESC LIMIT 1 OFFSET (SELECT COUNT(*)/100 FROM candidates WHERE first_seen_at>=datetime('now','-60 days') AND quant_score IS NOT NULL)) p99_q,
       (SELECT quant_score FROM candidates WHERE first_seen_at>=datetime('now','-60 days') AND quant_score IS NOT NULL ORDER BY quant_score DESC LIMIT 1 OFFSET (SELECT COUNT(*)/20  FROM candidates WHERE first_seen_at>=datetime('now','-60 days') AND quant_score IS NOT NULL)) p95_q,
       (SELECT conviction_score FROM candidates WHERE first_seen_at>=datetime('now','-60 days') AND conviction_score IS NOT NULL ORDER BY conviction_score DESC LIMIT 1 OFFSET (SELECT COUNT(*)/100 FROM candidates WHERE first_seen_at>=datetime('now','-60 days') AND conviction_score IS NOT NULL)) p99_conv,
       (SELECT conviction_score FROM candidates WHERE first_seen_at>=datetime('now','-60 days') AND conviction_score IS NOT NULL ORDER BY conviction_score DESC LIMIT 1 OFFSET (SELECT COUNT(*)/20  FROM candidates WHERE first_seen_at>=datetime('now','-60 days') AND conviction_score IS NOT NULL)) p95_conv
FROM candidates WHERE first_seen_at>=datetime('now','-60 days');"

Q "P0.score_history_distribution_60d" "
SELECT COUNT(*) n, MAX(score) max_s, AVG(score) avg_s FROM score_history WHERE scanned_at>=datetime('now','-60 days');"

# ---------- Phase 1: funnel attrition, trailing 60d ----------
Q "P1.funnel_60d" "
SELECT 'candidates(first_seen 60d)' stage, COUNT(*) FROM candidates WHERE first_seen_at>=datetime('now','-60 days')
UNION ALL SELECT 'signals_fired non-empty', COUNT(*) FROM candidates WHERE first_seen_at>=datetime('now','-60 days') AND signals_fired IS NOT NULL AND signals_fired NOT IN ('','[]')
UNION ALL SELECT 'quant>=1',  COUNT(*) FROM candidates WHERE first_seen_at>=datetime('now','-60 days') AND quant_score>=1
UNION ALL SELECT 'conviction scored', COUNT(*) FROM candidates WHERE first_seen_at>=datetime('now','-60 days') AND conviction_score IS NOT NULL
UNION ALL SELECT 'candidates.alerted_at set', COUNT(*) FROM candidates WHERE first_seen_at>=datetime('now','-60 days') AND alerted_at IS NOT NULL
UNION ALL SELECT 'alerts rows 60d', COUNT(*) FROM alerts WHERE alerted_at>=datetime('now','-60 days')
UNION ALL SELECT 'tg sent 60d (all lanes)', COUNT(*) FROM tg_alert_log WHERE outcome='sent' AND created_at>=datetime('now','-60 days')
UNION ALL SELECT 'paper_trades opened 60d', COUNT(*) FROM paper_trades WHERE opened_at>=datetime('now','-60 days');"

Q "P1.tg_outcome_mix_60d (dispatch rejection profile)" "
SELECT signal_type, outcome, COUNT(*) FROM tg_alert_log
WHERE created_at>=datetime('now','-60 days')
GROUP BY signal_type, outcome ORDER BY signal_type, COUNT(*) DESC;"

Q "P1.alerts_by_week (when did the conviction lane die)" "
SELECT strftime('%Y-%W', alerted_at) wk, COUNT(*) FROM alerts GROUP BY wk ORDER BY wk;"

Q "P1.suppression_state (frozen-lock check — PR #424 fix effect)" "
SELECT combo_key, window, trades, suppressed, suppressed_at, parole_at,
       parole_trades_remaining, last_refreshed, perm_suppression_alerted_at
FROM combo_performance WHERE suppressed=1 ORDER BY suppressed_at;"

Q "P1.signal_suspensions" "
SELECT signal_type, suspended_at, suspended_reason FROM signal_performance
WHERE suspended_at IS NOT NULL;" 2>/dev/null

Q "P1.kill_events_60d" "
SELECT * FROM kill_events WHERE created_at>=datetime('now','-60 days') ORDER BY created_at DESC LIMIT 40;"

# ---------- Phase 2/3 raw material ----------
Q "P2.exit_machinery_realized (by exit_reason, 90d closed)" "
SELECT exit_reason, COUNT(*) n, ROUND(AVG(pnl_pct),2) avg_pnl_pct, ROUND(SUM(pnl_usd),2) sum_pnl,
       ROUND(AVG(peak_pct),2) avg_peak_pct
FROM paper_trades WHERE status!='open' AND closed_at>=datetime('now','-90 days')
GROUP BY exit_reason ORDER BY n DESC;"

Q "P2.fixed24h_vs_exit (checkpoint_24h as fixed-hold proxy, 90d)" "
SELECT COUNT(*) n, ROUND(AVG(checkpoint_24h_pct),2) avg_24h_pct, ROUND(AVG(pnl_pct),2) avg_realized_pct,
       ROUND(AVG(peak_pct),2) avg_peak_pct
FROM paper_trades WHERE closed_at>=datetime('now','-90 days') AND checkpoint_24h_pct IS NOT NULL;"

Q "P3.detected_never_alerted_5x_90d (gainers_comparisons lens)" "
SELECT gc.coin_id, gc.symbol, MAX(gc.price_change_24h) best_24h,
       MAX(gc.detected_by_pipeline) det_pipeline, MAX(gc.pipeline_lead_minutes) lead_min,
       c.quant_score, c.conviction_score, c.alerted_at
FROM gainers_comparisons gc LEFT JOIN candidates c ON c.contract_address=gc.coin_id
WHERE gc.appeared_on_gainers_at>=datetime('now','-90 days')
GROUP BY gc.coin_id HAVING best_24h>=100 ORDER BY best_24h DESC LIMIT 60;"

Q "P3.moved_already_postmortems (if table populated)" "
SELECT COUNT(*) FROM moved_already_postmortems;" 2>/dev/null

echo; echo "=== P0.journal last-30d event counts ==="
for ev in cg_429_backoff coingecko_lanes_stopped_for_backoff alert_dispatched alert_delivered detection_alert_funnel; do
  n=$(journalctl -u gecko-pipeline --since "30 days ago" --no-pager 2>/dev/null | grep -c "$ev")
  echo "$ev: $n"
done
echo; echo "### done"
