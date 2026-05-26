**New primitives introduced:** NONE

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Paper-trade cohort validation | none found in Hermes bundled/optional skills catalog for project-local SQLite actionability stamps | Use read-only SQL against prod `scout.db`; no new runtime primitive. |
| Statistical actionability interpretation | none found for gecko-alpha-specific `paper_trades` PnL attribution | Keep as repo findings with explicit uncertainty and outlier checks. |
| Backlog gate status | Hermes can remember/schedule, but the canonical gate state lives in repo artifacts | Update `backlog.md` and `tasks/todo.md`; no Hermes job in this PR. |

Awesome-hermes-agent ecosystem check: no drop-in actionability audit skill or trading-signal cohort validator for this SQLite schema. Verdict: custom read-only evidence pass is justified.

## Design goal

Produce a read-only actionability gate revalidation packet for 2026-05-26. The packet answers:

1. Did prod clear the minimum descriptive gate (`n_actionable_closed >= 20` and `n_exploratory_closed >= 5`)?
2. Are there malformed stamped rows or status/close-time anomalies that make the gate count unsafe?
3. Does the descriptive evidence point to any separate follow-up investigation, without shipping policy changes in this PR?

## Runtime query discipline

All prod access uses the Windows SSH two-step:

1. Run SSH and redirect stdout/stderr to a local file.
2. Read that local file separately.

SQLite is opened read-only with `sqlite3 -readonly scout.db`. The script contains only `PRAGMA table_info`, `SELECT`, and CTE statements. No write statement or SQLite meta-command that can write files is allowed.

## Query packet

The prod packet will emit section markers through shell stdout only so the copied evidence is reviewable. It will not use `.output`, `.once`, temp tables, or any SQLite write/meta side effect.

```sql
SELECT sqlite_version() AS sqlite_version;

PRAGMA table_info(paper_trades);

SELECT name, cutover_ts
FROM paper_migrations
WHERE name = 'bl_new_actionability_gate_v1';

SELECT actionability_version,
       actionable,
       COUNT(*) AS rows,
       MIN(opened_at) AS first_opened_at,
       MAX(closed_at) AS latest_closed_at
FROM paper_trades
WHERE actionability_version IS NOT NULL
GROUP BY actionability_version, actionable
ORDER BY actionability_version, actionable;

SELECT status, COUNT(*) AS rows
FROM paper_trades
WHERE actionability_version IS NOT NULL
GROUP BY status
ORDER BY status;

SELECT
  SUM(CASE WHEN actionability_version IS NOT NULL
            AND status GLOB 'closed_*'
            AND closed_at IS NULL THEN 1 ELSE 0 END) AS closed_missing_closed_at,
  SUM(CASE WHEN actionability_version IS NOT NULL
            AND status NOT GLOB 'closed_*'
            AND status != 'open'
            AND closed_at IS NOT NULL THEN 1 ELSE 0 END) AS nonclosed_with_closed_at,
  SUM(CASE WHEN actionability_version IS NOT NULL
            AND status GLOB 'closed_*'
            AND actionable IS NULL THEN 1 ELSE 0 END) AS closed_missing_actionable,
  SUM(CASE WHEN actionability_version IS NOT NULL
            AND status GLOB 'closed_*'
            AND (actionability_reason IS NULL OR actionability_reason = '') THEN 1 ELSE 0 END) AS closed_missing_reason,
  SUM(CASE WHEN actionability_version IS NOT NULL
            AND status GLOB 'closed_*'
            AND pnl_usd IS NULL THEN 1 ELSE 0 END) AS closed_missing_pnl_usd,
  SUM(CASE WHEN actionability_version IS NOT NULL
            AND status GLOB 'closed_*'
            AND pnl_pct IS NULL THEN 1 ELSE 0 END) AS closed_missing_pnl_pct,
  SUM(CASE WHEN actionability_version IS NOT NULL
            AND opened_at < (
              SELECT cutover_ts
              FROM paper_migrations
              WHERE name = 'bl_new_actionability_gate_v1'
            ) THEN 1 ELSE 0 END) AS stamped_opened_before_cutover
FROM paper_trades;

WITH gate_eligible AS (
  SELECT *
  FROM paper_trades
  WHERE actionability_version = 'v1'
    AND actionable IN (0, 1)
    AND status GLOB 'closed_*'
    AND closed_at IS NOT NULL
)
SELECT actionable,
       COUNT(*) AS n_closed,
       SUM(CASE WHEN pnl_usd IS NOT NULL THEN 1 ELSE 0 END) AS n_with_pnl,
       ROUND(SUM(pnl_usd), 2) AS total_pnl,
       ROUND(AVG(pnl_usd), 2) AS avg_pnl,
       ROUND(MIN(pnl_usd), 2) AS max_loss,
       ROUND(MAX(pnl_usd), 2) AS max_win,
       SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
       SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) AS losses,
       ROUND(100.0 * SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) / NULLIF(SUM(CASE WHEN pnl_usd IS NOT NULL THEN 1 ELSE 0 END), 0), 1) AS win_rate_pct,
       MIN(opened_at) AS first_opened_at,
       MAX(closed_at) AS latest_closed_at
FROM gate_eligible
GROUP BY actionable
ORDER BY actionable;

WITH eligible_with_pnl AS (
  SELECT *
  FROM paper_trades
  WHERE actionability_version = 'v1'
    AND actionable IN (0, 1)
    AND status GLOB 'closed_*'
    AND closed_at IS NOT NULL
    AND pnl_usd IS NOT NULL
)
SELECT signal_type,
       actionable,
       COUNT(*) AS n,
       ROUND(SUM(pnl_usd), 2) AS total_pnl,
       SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
       SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) AS losses,
       ROUND(MIN(pnl_usd), 2) AS max_loss,
       ROUND(MAX(pnl_usd), 2) AS max_win
FROM eligible_with_pnl
GROUP BY signal_type, actionable
ORDER BY signal_type, actionable;

WITH eligible_with_pnl AS (
  SELECT *
  FROM paper_trades
  WHERE actionability_version = 'v1'
    AND actionable IN (0, 1)
    AND status GLOB 'closed_*'
    AND closed_at IS NOT NULL
    AND pnl_usd IS NOT NULL
)
SELECT actionable,
       actionability_reason,
       COUNT(*) AS n,
       ROUND(SUM(pnl_usd), 2) AS total_pnl,
       SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
       SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) AS losses,
       ROUND(MIN(pnl_usd), 2) AS max_loss,
       ROUND(MAX(pnl_usd), 2) AS max_win
FROM eligible_with_pnl
GROUP BY actionable, actionability_reason
ORDER BY actionable, n DESC, actionability_reason;

WITH eligible_with_pnl AS (
  SELECT *
  FROM paper_trades
  WHERE actionability_version = 'v1'
    AND actionable IN (0, 1)
    AND status GLOB 'closed_*'
    AND closed_at IS NOT NULL
    AND pnl_usd IS NOT NULL
)
SELECT actionable,
       status,
       COALESCE(exit_reason, '') AS exit_reason,
       COUNT(*) AS n,
       ROUND(SUM(pnl_usd), 2) AS total_pnl,
       SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
       SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) AS losses
FROM eligible_with_pnl
GROUP BY actionable, status, COALESCE(exit_reason, '')
ORDER BY actionable, n DESC, status;

WITH eligible_with_pnl AS (
  SELECT *
  FROM paper_trades
  WHERE actionability_version = 'v1'
    AND actionable IN (0, 1)
    AND status GLOB 'closed_*'
    AND closed_at IS NOT NULL
    AND pnl_usd IS NOT NULL
)
SELECT id, opened_at, closed_at, signal_type, symbol,
       ROUND(pnl_usd, 2) AS pnl_usd,
       ROUND(pnl_pct, 2) AS pnl_pct,
       status,
       COALESCE(exit_reason, '') AS exit_reason,
       actionability_reason
FROM eligible_with_pnl
WHERE actionable = 0
  AND pnl_usd > 0
ORDER BY pnl_usd DESC
LIMIT 50;

WITH eligible_with_pnl AS (
  SELECT *
  FROM paper_trades
  WHERE actionability_version = 'v1'
    AND actionable IN (0, 1)
    AND status GLOB 'closed_*'
    AND closed_at IS NOT NULL
    AND pnl_usd IS NOT NULL
),
ranked AS (
  SELECT actionable,
         pnl_usd,
         ABS(pnl_usd) AS abs_pnl,
         ROW_NUMBER() OVER (PARTITION BY actionable ORDER BY pnl_usd DESC) AS win_rank,
         ROW_NUMBER() OVER (PARTITION BY actionable ORDER BY pnl_usd ASC) AS loss_rank,
         ROW_NUMBER() OVER (PARTITION BY actionable ORDER BY ABS(pnl_usd) DESC) AS abs_rank
  FROM eligible_with_pnl
)
SELECT actionable,
       COUNT(*) AS n,
       ROUND(SUM(pnl_usd), 2) AS total_pnl,
       ROUND(SUM(CASE WHEN win_rank > 1 THEN pnl_usd ELSE 0 END), 2) AS total_without_max_win,
       ROUND(SUM(CASE WHEN loss_rank > 1 THEN pnl_usd ELSE 0 END), 2) AS total_without_max_loss,
       ROUND(SUM(CASE WHEN abs_rank > 1 THEN pnl_usd ELSE 0 END), 2) AS total_without_largest_abs,
       ROUND(SUM(CASE WHEN abs_rank > 2 THEN pnl_usd ELSE 0 END), 2) AS total_without_top2_abs,
       ROUND(SUM(CASE WHEN abs_rank <= 3 THEN abs_pnl ELSE 0 END), 2) AS top3_abs_pnl,
       ROUND(SUM(abs_pnl), 2) AS total_abs_pnl,
       ROUND(100.0 * SUM(CASE WHEN abs_rank <= 3 THEN abs_pnl ELSE 0 END) / NULLIF(SUM(abs_pnl), 0), 1) AS top3_abs_share_pct
FROM ranked
GROUP BY actionable
ORDER BY actionable;

WITH eligible_with_pnl AS (
  SELECT *
  FROM paper_trades
  WHERE actionability_version = 'v1'
    AND actionable IN (0, 1)
    AND status GLOB 'closed_*'
    AND closed_at IS NOT NULL
    AND pnl_usd IS NOT NULL
)
SELECT actionable, id, opened_at, closed_at, signal_type, symbol,
       ROUND(pnl_usd, 2) AS pnl_usd,
       ROUND(pnl_pct, 2) AS pnl_pct,
       actionability_reason,
       status,
       COALESCE(exit_reason, '') AS exit_reason
FROM eligible_with_pnl
ORDER BY actionable, pnl_usd DESC, id
LIMIT 500;
```

Median PnL is not computed in SQL because SQLite's percentile support is not portable across deployed versions. The findings doc will compute/report median manually from the row-level listing for both cohorts. The window-function outlier query requires SQLite 3.25+; if prod CLI is older, skip only that query and compute leave-one/two-out from the row-level listing.

## Interpretation rules

- Primary gate count uses only v1 eligible closed rows: `actionability_version = 'v1'`, `actionable IN (0,1)`, `status GLOB 'closed_*'`, and `closed_at IS NOT NULL`.
- PnL summaries use the stricter `eligible_with_pnl` cohort. If any gate-eligible row lacks `pnl_usd`, the findings doc separates gate count from performance count and does not use that missing-PnL row in performance claims.
- If anomaly counts are non-zero, the findings doc reports them before any gate verdict.
- The 20/5 gate is a descriptive trigger only. It is not proof that v1 is good, v2 is bad, or suppression should ship. For exploratory `n < 20`, the only permitted conclusions are "minimum descriptive sample reached" and "hypothesis leads"; no directional claim about classifier quality, suppression, or expected PnL is allowed.
- Exploratory winners are false-negative leads. They do not become classifier-change evidence unless they repeat by reason/signal bucket and survive outlier checks.
- Single-token exploratory winners cannot justify classifier changes, even if large. Each winner must be interpreted with same-signal, same-reason, and same signal+reason denominator context if promoted to a follow-up.
- Post-hoc buckets are hypothesis generation only. Do not call a bucket "repeated" unless bucket `n >= 5`; even then, a follow-up must pre-register the bucket rule and validate it on future or held-out data before policy changes.
- If a cohort's sign or practical conclusion changes after removing the largest absolute row or largest two absolute rows, the findings must label it outlier-dominated and avoid directional interpretation.
- If any stamped row opened before the migration cutover, label it separately as backfill/historical and keep the primary gate verdict on post-cutover prod evidence.
- Any implementation candidate must be framed as a separate follow-up plan/design item.

## Artifacts

- Add `tasks/findings_actionability_gate_revalidation_2026_05_26.md`.
- Update `backlog.md` actionability entries from stale `21/3` to current revalidation state.
- Update `tasks/todo.md` close-development note to point at the new findings.

## Anti-scope

- No Python, dashboard, SQL migration, shell script, or systemd changes.
- No prod writes.
- No source-quality ranking, TG alert qualification, actionability v2, suppression, live dispatch, sizing, or capital-allocation change.
- No "qualified alert" or urgency-tier design.

## Verification

- `git diff --check`.
- `git status --short` shows only Markdown evidence/status artifacts.
- Two PR reviewers check the findings for statistical overclaiming and query/runtime correctness.
