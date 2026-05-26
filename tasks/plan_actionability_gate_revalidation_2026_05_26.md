**New primitives introduced:** NONE

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Read-only SQLite cohort audit | none found in Hermes bundled/optional skills catalog for gecko-alpha paper-trade cohort validation | Build from scratch as SQL in the findings doc; no runtime primitive. |
| Trading actionability / signal-quality validation | none found in Hermes skill hub for project-specific paper-trade stamps and PnL cohort attribution | Keep custom because the evidence lives in `scout.db.paper_trades` and the runbook is project-owned. |
| Backlog / session-memory reminder | Hermes can schedule reminders and preserve memory, but it does not replace the repo-owned gate record | Update repo docs/backlog; optional Hermes reminder remains out of scope. |

Awesome-hermes-agent ecosystem check: no drop-in skill for gecko-alpha actionability cohort validation or SQLite paper-trade PnL attribution. Verdict: this PR is a repo-owned read-only evidence pass, not a new Hermes or custom runtime feature.

## Goal

Revalidate the actionability data gate now that prod appears to have crossed the minimum asymmetric descriptive sample trigger:

- `n_actionable_closed >= 20`
- `n_exploratory_closed >= 5`

These are not power thresholds and do not support standalone statistical claims. They only justify a read-only descriptive audit with explicit uncertainty, outlier, malformed-row, and concentration checks.

The output is an evidence PR: plan, design, findings, and backlog/todo status updates. It does not change signal policy, suppress exploratory trades, alter live/capital behavior, rank sources, or enable Telegram alert qualification.

## Drift-check

Existing artifacts:

- `tasks/runbook_actionability_validation_2026_05_19.md` defines the observational runbook.
- `tasks/findings_actionability_gate_check_2026_05_22.md` recorded the prior thin state: actionable `21`, exploratory `3`.
- `backlog.md` still parks actionability v2, source-quality consumption, classifier changes, X/TG linkage, and no-peak risk handling behind the actionability re-check gate.
- `tasks/todo.md` line-level close-development note still reports the stale `21/3` state.

Residual gap: the gate has likely cleared in prod, but no current repo artifact records the full validation result or the branch decision. This PR closes that gap.

## Runtime assumptions to verify

1. The actionability schema and migration marker are still present in prod.
2. Closed rows must be selected with `status LIKE 'closed_%'`, not `status = 'closed'`.
3. Gate rows must satisfy `actionability_version IS NOT NULL`, `actionable IN (0,1)`, and closed-status semantics. Separately report any stamped closed rows with `actionable IS NULL` or missing `actionability_reason` as schema/stamping anomalies; do not count them toward either cohort.
4. The primary gate is a trigger for revalidation, not automatic authorization for v2/suppression/ranking/source-quality policy work.
5. If multiple early-fire clauses are true, they remain independent investigation tracks.

## Analysis plan

Run a read-only prod SQL packet via the required Windows SSH two-step. Use SQLite read-only mode, e.g. `sqlite3 -readonly scout.db` or `sqlite3 'file:scout.db?mode=ro'`, and include only `PRAGMA table_info`, `SELECT`, and read-only CTEs. No `UPDATE`, `INSERT`, `DELETE`, `CREATE`, `DROP`, `ALTER`, or `.save` commands.

1. Schema/migration marker, closed-status inventory, and anomaly counts for `status LIKE 'closed_%' AND closed_at IS NULL` plus stamped rows missing `actionable` / `actionability_reason`.
2. Cohort summary by `actionable`.
3. Cohort split by `signal_type`.
4. Cohort split by `actionability_reason`.
5. Cohort split by exit reason/status.
6. Exploratory winners list, interpreted only as false-negative leads for review; do not treat individual winners as evidence for suppression/classifier changes unless the pattern repeats by reason/signal bucket and survives the outlier checks.
7. Outlier dominance summary: totals with and without max win/loss per cohort, top-row and top-3 contribution share, median per-row PnL, and whether any branch conclusion changes after outlier removal.
8. Freshness/window summary: first stamped open, latest close, current open counts.
9. Version inventory by `actionability_version` and `actionable`. If multiple non-null versions are present, report cohorts by version and make `v1` the primary comparison unless the design explicitly justifies combining versions.

## Branch logic

The findings doc will classify the gate state:

- **CLEARED / no immediate implementation authorized:** primary n trigger is met, but the descriptive audit does not produce a robust, non-outlier-dominated, pre-specified implementation target; continue observation and document targeted follow-ups only.
- **CLEARED / candidate follow-up:** primary n trigger is met and a pre-specified reason/signal/exit bucket has adequate bucket-level n, survives max-win/max-loss removal, is not dominated by one row, and has a plausible mechanism. This branch authorizes only a separate investigation/design, not policy implementation.
- **NOT CLEARED:** prod query does not reproduce the preliminary `55/16` count; update the gate record and stop.

No branch directly authorizes implementation in this PR. Any policy/build item needs a separate plan/design after this evidence pass.

## Anti-scope

- No actionability classifier v2.
- No suppression of exploratory rows.
- No live trading, sizing, capital allocation, or dispatch changes.
- No source-quality/KOL/TG/X ranking consumption.
- No Telegram alert qualification work.
- No dashboard/API schema changes.
- No prod writes.

## Review plan

Two plan reviewers:

1. Statistical/data interpretation vector: sample size, outlier dominance, false-negative exploratory winners, and whether branch logic overclaims.
2. Runtime/structural vector: query predicates, status semantics, migration/schema assumptions, and anti-scope coverage.

After folding plan feedback, write a design doc with exact SQL and reviewer-specific corrections, then repeat two-vector design review before running the prod analysis.

## Verification

- `git diff --check`.
- Review produced SQL output against the plan's branch logic.
- PR diff should be Markdown evidence/status artifacts only: `tasks/*.md`, `backlog.md`, and `tasks/todo.md`. If `scout/`, `dashboard/`, `scripts/`, migrations, config, or runtime files change, stop and re-plan.
