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
--   3. scorer.py signal weights changed OR SCORER_MAX_RAW changed (per design v2 R2 #5 fold)
--   4. 2026-08-17 (90d calendar backstop)
--   5. operator explicit request

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
