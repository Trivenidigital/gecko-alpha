#!/usr/bin/env bash
# investigation/ruling_response_queries.sh — READ-ONLY collector for the
# 2026-07-20 review ruling's operator-side evidence items. Every output is
# machine-captured (no transcription): run on the VPS as
#
#   bash investigation/ruling_response_queries.sh 2>&1 \
#     | tee /tmp/ruling_response_$(date -u +%Y%m%dT%H%M%SZ).log
#
# and attach the log verbatim.
#
# Covers:
#   A/B/C  cohort artifact repair — re-query from the EXACT revival audit
#          timestamp, with the 12:28:00 -> 12:28:52.954712 boundary window
#          reported separately (zero rows => the 43-trade result stands)
#   C      unique token/contract counts alongside trade-ID counts
#   D      time_death counterfactual adjudication (forward replay, evidence
#          classes separated) via investigation/time_death_counterfactual.py
set -uo pipefail
DB="${GECKO_DB:-/root/gecko-alpha/scout.db}"
# Exact revival audit timestamp per the ruling. DB rows store naive ISO
# strings (no trailing Z), so compare against the naive form.
TS="2026-07-17T12:28:52.954712"
WINDOW_START="2026-07-17T12:28:00"
Q() { echo; echo "=== $1 ==="; sqlite3 -readonly "$DB" "$2" 2>&1; }

echo "### ruling-response evidence snapshot $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "### db=$DB revival_ts=$TS"

Q "A. gainers_early cohort STRICTLY AFTER exact revival ts (closed + open)" "
SELECT status!='open' AS is_closed, COUNT(*) trades,
       COUNT(DISTINCT token_id) unique_tokens,
       ROUND(SUM(CASE WHEN status!='open' THEN pnl_usd END),2) realized_net
FROM paper_trades
WHERE signal_type='gainers_early' AND opened_at > '$TS'
GROUP BY is_closed;"

Q "B. boundary window $WINDOW_START -> $TS (rows here => recompute cohort)" "
SELECT rowid, token_id, symbol, opened_at, status, exit_reason, pnl_usd
FROM paper_trades
WHERE signal_type='gainers_early'
  AND opened_at > '$WINDOW_START' AND opened_at <= '$TS'
ORDER BY opened_at;"

Q "C. cohort trade-IDs vs unique token/contract exposure (never conflate)" "
SELECT COUNT(*) trade_rows, COUNT(DISTINCT token_id) unique_tokens,
       COUNT(DISTINCT symbol) unique_symbols
FROM paper_trades
WHERE signal_type='gainers_early' AND opened_at > '$TS';"

Q "C2. repeat-exposure tokens in the cohort (rows per token > 1)" "
SELECT token_id, symbol, COUNT(*) trades, ROUND(SUM(pnl_usd),2) net
FROM paper_trades
WHERE signal_type='gainers_early' AND opened_at > '$TS'
GROUP BY token_id HAVING COUNT(*) > 1 ORDER BY trades DESC;"

echo
echo "=== D. time_death counterfactual adjudication (per-trade CSV + summary) ==="
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CSV_OUT="${CSV_OUT:-/tmp/time_death_counterfactual.csv}"
python3 "$SCRIPT_DIR/time_death_counterfactual.py" \
  --db "$DB" --dry-run-cutoff-ts "$TS" --out "$CSV_OUT"
echo "--- per-trade CSV ($CSV_OUT) ---"
cat "$CSV_OUT"

echo
echo "### done — verdicts:"
echo "###   B empty            => 43-trade cohort result stands; else recompute"
echo "###   C trade_rows vs unique_tokens => report BOTH at next checkpoint"
echo "###   D summary line     => measured-live incremental_benefit only;"
echo "###                        dry_run_era / unresolved stay separate"
