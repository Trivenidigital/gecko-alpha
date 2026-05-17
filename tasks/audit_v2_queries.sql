-- BL-NEW-SOCIAL-MENTIONS-DENOMINATOR-AUDIT — re-run queries
-- Ship with PR per design v2 fold (Reviewer 1 #9 + Reviewer 2 #9).
--
-- Usage:
--   ssh srilu-vps 'sqlite3 /root/gecko-alpha/scout.db' < tasks/audit_v2_queries.sql
-- OR for results capture:
--   ssh srilu-vps 'sqlite3 /root/gecko-alpha/scout.db < /tmp/audit_v2_queries.sql' > audit_out.txt
--
-- Re-evaluation triggers (operator runs this when any fires):
--   1. narrative_alerts_inbound.resolved_coin_id ≥ 20 in 30d
--   2. tg_social_messages distinct-contract 24h rollup ≥ 50
--   3. scorer.py signal weights changed OR SCORER_MAX_RAW changed (invalidates Variant B 0-flip math)
--   4. 2026-08-17 (90d calendar backstop)
--   5. operator explicit request
--   6. any 30d window with top-10 score_history scores ≥ 60 (forward-stability detector per PR-review fold R3 #5)
--
-- COUPLING (per PR-review fold R2 #4): these queries hardcode config values.
-- Re-derive after ANY change to:
--   - scout/scorer.py:37 (SCORER_MAX_RAW = 208 — used in Q3/Q4 as 208.0 / 193.0)
--   - scout/scorer.py:121 (Signal 5 threshold > 50 — used in Q1)
--   - scout/config.py:27 (MIN_SCORE = 60 — used in Q2 >= 60, Q3 >= 60 <-> >= 65, Q4 >= 60)
--   - scout/config.py:28 (CONVICTION_THRESHOLD = 70 — used in Q2 >= 70, Q3 >= 70 <-> >= 75, Q4 >= 70)
--
-- SCHEMA dependencies (per PR-review fold R2 #11): these queries hardcode
-- table/column references. Re-derive on schema migration touching:
--   - candidates.social_mentions_24h (Q1)
--   - score_history.score, score_history.scanned_at (Q2/Q3/Q4)
--   - narrative_alerts_inbound.received_at, .resolved_coin_id (Q5)
--   - tg_social_messages.parsed_at, .contracts (Q6)
--   - social_signals / social_baselines / social_credit_ledger (Q7)

.headers on
.mode column

.print "=== Q1: Signal 5 fire-rate (Signal 5 threshold > 50) ==="
SELECT
  COUNT(*) AS total_candidates,
  SUM(CASE WHEN social_mentions_24h > 50 THEN 1 ELSE 0 END) AS would_fire_signal_5,
  SUM(CASE WHEN social_mentions_24h > 0 THEN 1 ELSE 0 END) AS nonzero,
  MAX(social_mentions_24h) AS max_value
FROM candidates;

.print ""
.print "=== Q2: full score_history max + distribution ==="
SELECT COUNT(*) AS rows, MAX(score) AS max_score,
  SUM(CASE WHEN score >= 60 THEN 1 ELSE 0 END) AS gte60_min_score,
  SUM(CASE WHEN score >= 70 THEN 1 ELSE 0 END) AS gte70_conviction
FROM score_history;

.print ""
.print "=== Q2b: score_history retention window (per PR-review fold R1 #6) ==="
SELECT MIN(scanned_at) AS oldest_row, MAX(scanned_at) AS newest_row, COUNT(*) AS n_rows
FROM score_history;

.print ""
.print "=== Q3: Variant B (remove + recalibrate gates 60→65 / 70→75) flip-count ==="
WITH recalc AS (
  SELECT score AS current_score,
    MIN(100, CAST(score * 208.0 / 100.0 / 193.0 * 100 AS INTEGER)) AS new_score
  FROM score_history
)
SELECT
  COUNT(*) AS total,
  SUM(CASE WHEN current_score >= 60 AND new_score < 65 THEN 1 ELSE 0 END) AS demoted_min_60_to_65,
  SUM(CASE WHEN current_score < 60 AND new_score >= 65 THEN 1 ELSE 0 END) AS promoted_min_60_to_65,
  SUM(CASE WHEN current_score >= 70 AND new_score < 75 THEN 1 ELSE 0 END) AS demoted_conviction_70_to_75,
  SUM(CASE WHEN current_score < 70 AND new_score >= 75 THEN 1 ELSE 0 END) AS promoted_conviction_70_to_75
FROM recalc;

.print ""
.print "=== Q4: Variant C (remove without recalibrating) MIN_SCORE promotions ==="
WITH recalc AS (
  SELECT score AS current_score,
    MIN(100, CAST(score * 208.0 / 100.0 / 193.0 * 100 AS INTEGER)) AS new_score
  FROM score_history
)
SELECT
  SUM(CASE WHEN current_score < 60 AND new_score >= 60 THEN 1 ELSE 0 END) AS newly_passes_min_60,
  SUM(CASE WHEN current_score < 70 AND new_score >= 70 THEN 1 ELSE 0 END) AS newly_passes_conv_70
FROM recalc;

.print ""
.print "=== Q3b: Variant B/C sensitivity to closed-form rounding (per PR-review fold R1 #1) ==="
-- The Q3/Q4 cast-to-int truncates; some raw values produce new_score off-by-one vs
-- recomputing the actual `int(raw * 100 / 193)` if raw were known. Below is the
-- "+0.5 rounded" version of Q4 — if newly_passes counts differ from Q4, the closed-form
-- is rounding-mode-fragile and the operator should add a manual raw-recomputation pass
-- against fresh prod data before relying on the 0-flip claim.
WITH recalc_rounded AS (
  SELECT score AS current_score,
    MIN(100, CAST(score * 208.0 / 100.0 / 193.0 * 100 + 0.5 AS INTEGER)) AS new_score_rounded
  FROM score_history
)
SELECT
  SUM(CASE WHEN current_score < 60 AND new_score_rounded >= 60 THEN 1 ELSE 0 END) AS rounded_newly_passes_min_60,
  SUM(CASE WHEN current_score < 70 AND new_score_rounded >= 70 THEN 1 ELSE 0 END) AS rounded_newly_passes_conv_70,
  SUM(CASE WHEN current_score >= 60 AND new_score_rounded < 65 THEN 1 ELSE 0 END) AS rounded_demoted_min_60_to_65,
  SUM(CASE WHEN current_score >= 70 AND new_score_rounded < 75 THEN 1 ELSE 0 END) AS rounded_demoted_conviction_70_to_75
FROM recalc_rounded;

.print ""
.print "=== Q4b: Variant C 35-candidate paper-trade outcome cross-check (per PR-review fold R1 #7) ==="
-- Join the 35 Variant-C-promoted candidates to paper_trades to test funnel-widening
-- value. If 0 of 35 had paper_trade outcomes, the promotion is cosmetic.
WITH promoted AS (
  SELECT DISTINCT sh.contract_address
  FROM score_history sh
  WHERE sh.score < 60
    AND CAST(sh.score * 208.0 / 100.0 / 193.0 * 100 AS INTEGER) >= 60
)
SELECT
  (SELECT COUNT(*) FROM promoted) AS n_promoted_candidates,
  (SELECT COUNT(DISTINCT pt.token_id) FROM paper_trades pt INNER JOIN promoted p ON pt.token_id = p.contract_address) AS n_with_paper_trades,
  (SELECT ROUND(SUM(pt.pnl_usd), 2) FROM paper_trades pt INNER JOIN promoted p ON pt.token_id = p.contract_address WHERE pt.pnl_usd IS NOT NULL) AS total_pnl_usd,
  (SELECT ROUND(100.0 * SUM(CASE WHEN pt.pnl_usd > 0 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) FROM paper_trades pt INNER JOIN promoted p ON pt.token_id = p.contract_address WHERE pt.pnl_usd IS NOT NULL) AS win_pct;

.print ""
.print "=== Q5: Hermes X resolution rate (narrative_alerts_inbound) ==="
SELECT
  COUNT(*) AS total_7d,
  SUM(CASE WHEN resolved_coin_id IS NOT NULL AND resolved_coin_id != '' THEN 1 ELSE 0 END) AS resolved_7d
FROM narrative_alerts_inbound
WHERE datetime(received_at) >= datetime('now', '-7 days');

.print ""
.print "=== Q6: TG per-token rollup feasibility (24h distinct contracts) ==="
SELECT
  COUNT(DISTINCT contracts) AS distinct_contract_groups_24h,
  COUNT(*) AS total_msgs_with_contracts_24h
FROM tg_social_messages
WHERE datetime(parsed_at) >= datetime('now', '-1 day')
  AND contracts IS NOT NULL AND contracts != '' AND contracts != '[]';

.print ""
.print "=== Q7: social_signals / social_baselines / social_credit_ledger row counts ==="
SELECT 'social_signals' AS t, COUNT(*) AS n FROM social_signals
UNION ALL SELECT 'social_baselines', COUNT(*) FROM social_baselines
UNION ALL SELECT 'social_credit_ledger', COUNT(*) FROM social_credit_ledger;
