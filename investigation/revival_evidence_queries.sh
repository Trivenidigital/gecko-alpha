#!/usr/bin/env bash
# investigation/revival_evidence_queries.sh — READ-ONLY revival-evidence collector.
# The decisive counterfactual the suspension audit found nobody has run:
# recompute per-signal stats with broken-era STALE closes ALSO excluded.
#
#   - entry_fallback / expired_stale_no_price ($0 fabricated closes) are
#     already excluded by prod stats (GA-01) — excluding them makes signals
#     look WORSE (removes $0 dilution), never better.
#   - stale_snapshot / expired_stale_price (forced exits at stale prices in
#     the broken era, pre 2026-05-18 lane-order fix) carry REAL fake losses
#     and are NOT excluded anywhere. If a suspension flips sign without them,
#     the suspension rests on contaminated evidence.
#
# Run on the VPS: bash investigation/revival_evidence_queries.sh
set -uo pipefail
DB="${GECKO_DB:-/root/gecko-alpha/scout.db}"
Q() { echo; echo "=== $1 ==="; sqlite3 -readonly "$DB" "$2" 2>&1; }

echo "### revival evidence snapshot $(date -u +%Y-%m-%dT%H:%M:%SZ)"

Q "A. per-signal stats — CURRENT prod predicate (fabricated excluded)" "
SELECT signal_type, COUNT(*) n, SUM(pnl_usd<0) losses,
       ROUND(100.0*SUM(pnl_usd>0)/COUNT(*),1) win_pct,
       ROUND(SUM(pnl_usd),2) net, ROUND(MIN(pnl_usd),2) worst
FROM paper_trades
WHERE status!='open'
  AND COALESCE(exit_provenance,'')!='entry_fallback'
  AND COALESCE(exit_reason,'')!='expired_stale_no_price'
GROUP BY signal_type ORDER BY net;"

Q "B. COUNTERFACTUAL — stale closes ALSO excluded (the un-run test)" "
SELECT signal_type, COUNT(*) n, SUM(pnl_usd<0) losses,
       ROUND(100.0*SUM(pnl_usd>0)/COUNT(*),1) win_pct,
       ROUND(SUM(pnl_usd),2) net, ROUND(MIN(pnl_usd),2) worst
FROM paper_trades
WHERE status!='open'
  AND COALESCE(exit_provenance,'')!='entry_fallback'
  AND COALESCE(exit_reason,'')!='expired_stale_no_price'
  AND COALESCE(exit_provenance,'')!='stale_snapshot'
  AND COALESCE(exit_reason,'')!='expired_stale_price'
GROUP BY signal_type ORDER BY net;"

Q "C. contamination mass per signal (how much of each record is stale/fabricated)" "
SELECT signal_type,
       COUNT(*) closed,
       SUM(COALESCE(exit_provenance,'')='entry_fallback'
           OR COALESCE(exit_reason,'')='expired_stale_no_price') fabricated,
       SUM(COALESCE(exit_provenance,'')='stale_snapshot'
           OR COALESCE(exit_reason,'')='expired_stale_price') stale,
       ROUND(SUM(CASE WHEN COALESCE(exit_provenance,'')='stale_snapshot'
                        OR COALESCE(exit_reason,'')='expired_stale_price'
                      THEN pnl_usd END),2) stale_pnl
FROM paper_trades WHERE status!='open'
GROUP BY signal_type ORDER BY stale DESC;"

Q "D. suspension records for the undocumented cases (trending_catch, volume_spike, first_signal re-suspend)" "
SELECT signal_type, enabled, suspended_at, suspended_reason
FROM signal_params
WHERE signal_type IN ('trending_catch','volume_spike','first_signal',
                      'chain_completed','losers_contrarian','tg_social','slow_burn');"

Q "D2. last 15 signal_params audit rows" "
SELECT * FROM signal_params_audit ORDER BY id DESC LIMIT 15;"

Q "E. combo suppression + parole state (post-#424 check)" "
SELECT combo_key, window, trades, wins, win_rate_pct, suppressed, suppressed_at,
       parole_at, parole_trades_remaining, last_refreshed, perm_suppression_alerted_at
FROM combo_performance ORDER BY suppressed DESC, combo_key;"

Q "F. outcome-ledger ripeness (forward revival evidence per signal)" "
SELECT surface, label_status, COUNT(*) FROM signal_outcome_ledger
GROUP BY surface, label_status ORDER BY surface;"

echo; echo "### done — section B vs A per signal is the verdict:"
echo "###   sign flips or hard_loss gate un-fires => suspension contaminated, revival case exists"
echo "###   still negative => suspension stands on clean evidence"
