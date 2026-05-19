# Actionability Visibility Validation Runbook

Purpose: validate PR #181 actionability stamps and the follow-up dashboard
visibility slice after deployment. This runbook is observational only.

Do not suppress exploratory paper trades or change live/capital allocation from
these checks. The classifier is collecting evidence first.

## 1. Confirm Deployed Code And Schema

Use the Windows SSH two-step pattern: redirect SSH output to a file, then read
the file locally.

```powershell
$script = @'
cd /root/gecko-alpha
echo HEAD_START
git rev-parse --short HEAD
git log -1 --oneline
echo HEAD_END
echo COLUMNS_START
sqlite3 scout.db 'PRAGMA table_info(paper_trades);' | grep -E 'actionable|actionability_reason|actionability_version'
echo COLUMNS_END
echo MARKER_START
sqlite3 -header -column scout.db "SELECT name, cutover_ts FROM paper_migrations WHERE name='bl_new_actionability_gate_v1';"
echo MARKER_END
'@
$script | ssh root@srilu-vps bash -s > .ssh_actionability_schema.txt 2>&1
Get-Content .ssh_actionability_schema.txt
```

Expected:
- `paper_trades` has `actionable`, `actionability_reason`, and
  `actionability_version`.
- `paper_migrations.name='bl_new_actionability_gate_v1'` exists.

## 2. Verify One Fresh Paper-Trade Open Is Stamped

Use the actionability migration cutover as the lower bound. On the 2026-05-19
deploy, the cutover was `2026-05-19T11:39:09.121422+00:00`; if re-deployed,
read the current cutover from step 1.

```powershell
$script = @'
cd /root/gecko-alpha
echo FRESH_ROWS_START
sqlite3 -header -column scout.db "
SELECT id, opened_at, signal_type, symbol,
       actionable, actionability_reason, actionability_version
FROM paper_trades
WHERE opened_at >= (
  SELECT cutover_ts FROM paper_migrations
  WHERE name='bl_new_actionability_gate_v1'
)
ORDER BY id DESC
LIMIT 20;
"
echo FRESH_ROWS_END
'@
$script | ssh root@srilu-vps bash -s > .ssh_actionability_fresh_rows.txt 2>&1
Get-Content .ssh_actionability_fresh_rows.txt
```

Expected once at least one post-deploy paper trade opens:
- `actionable` is `0` or `1`.
- `actionability_reason` is non-empty.
- `actionability_version` is `v1`.

If zero rows return, do not treat it as a failure. No qualifying paper-trade
open has occurred yet; rerun after the next paper-trade open.

## 3. Check 24h Actionable Vs Exploratory PnL

Run after at least 24 hours of stamped rows have accumulated.

```powershell
$script = @'
cd /root/gecko-alpha
echo COHORT_PNL_START
sqlite3 -header -column scout.db "
WITH stamped AS (
  SELECT CASE
           WHEN actionable = 1 THEN 'actionable'
           WHEN actionable = 0 THEN 'exploratory'
           ELSE 'unknown'
         END AS state,
         pnl_usd,
         pnl_pct
  FROM paper_trades
  WHERE status != 'open'
    AND closed_at >= datetime('now', '-24 hours')
)
SELECT state,
       COUNT(*) AS trades,
       SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
       ROUND(COALESCE(SUM(pnl_usd), 0), 2) AS pnl_usd,
       ROUND(COALESCE(AVG(pnl_pct), 0), 2) AS avg_pnl_pct,
       ROUND(
         CASE WHEN COUNT(*) > 0
              THEN 100.0 * SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) / COUNT(*)
              ELSE 0 END,
         1
       ) AS win_rate_pct
FROM stamped
GROUP BY state
ORDER BY state;
"
echo COHORT_PNL_END
'@
$script | ssh root@srilu-vps bash -s > .ssh_actionability_24h_pnl.txt 2>&1
Get-Content .ssh_actionability_24h_pnl.txt
```

Interpretation:
- Treat results as descriptive until enough stamped closes accumulate.
- Do not promote suppression/capital-allocation changes from a 24h sample.

## 4. List False-Negative Exploratory Winners

These are exploratory rows that won anyway. They are candidates for classifier
tuning, not immediate suppression-policy evidence.

```powershell
$script = @'
cd /root/gecko-alpha
echo FALSE_NEGATIVE_START
sqlite3 -header -column scout.db "
SELECT id, opened_at, closed_at, signal_type, symbol,
       pnl_usd, pnl_pct, actionability_reason
FROM paper_trades
WHERE actionable = 0
  AND status != 'open'
  AND pnl_usd > 0
  AND closed_at >= datetime('now', '-24 hours')
ORDER BY pnl_usd DESC
LIMIT 50;
"
echo FALSE_NEGATIVE_END
'@
$script | ssh root@srilu-vps bash -s > .ssh_actionability_false_negatives.txt 2>&1
Get-Content .ssh_actionability_false_negatives.txt
```

Review false negatives by reason. Useful follow-up questions:
- Is one reason repeatedly producing winners?
- Are winners concentrated in one signal type or market-cap bucket?
- Did winners have high `peak_pct` but poor exits, indicating an exit-policy
  issue rather than an entry-actionability issue?

## 5. Dashboard Smoke Check

After deploying this visibility PR and restarting `gecko-dashboard`, open the
Trading tab and verify:

- Actionability summary cards render.
- Reason table renders when stamped rows exist.
- Closed Trades filter changes row counts for `all`, `actionable`,
  `exploratory`, and `unknown`.
- Open and closed rows show actionability badges with reason hover text.
- Live-eligible indicators still render separately.

No behavioral policy change is approved by this runbook.
