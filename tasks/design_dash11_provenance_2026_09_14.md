# DASH-11 provenance audit design — 2026-09-14

**New primitives introduced:** none; local scratch query and durable findings.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Ledger provenance / retained-price replay | none verified in accessible catalog | Existing Python sqlite3 plus source trace; no new runtime dependency |

September 14 hub https://hermes-agent.nousresearch.com/docs/skills/ remained client-rendered/loading. Ecosystem https://github.com/0xNyk/awesome-hermes-agent checked; generic orchestration does not reconstruct missing original price observations. Verdict: use repository evidence; discovery is not exhaustive.

## Method

Read production using Python stdlib via SSH stdin, outer timeout 45 seconds and SQLite progress deadline 30 seconds. Open file:/root/gecko-alpha/scout.db?mode=ro, set query_only, BEGIN before first data read; close in finally. No imports of application settings or write-capable labeler. Print only aggregate numbers, schema field names, timestamps and source hashes. Do not print token IDs, environment or credentials.

Pin now once; use explicit 7d health / 120d maturation windows. Query gated_out_sample ordered emitted_at DESC, filter parsed object reason=suppressed and source_layer=dispatcher, then preserve first encountered row for equal timestamps and replace only when strictly earlier (matching analyzer). Report equal-earliest ties separately: SQLite equal-key ordering is not a stable identity guarantee. Select earliest BEFORE checking r7d. Report matured n and cohort emission/horizon bounds.

Obtain schema, global price observation min/max and each matured anchor's first positive historical observation at/after emission+7d using the labeler's exact query. Classify absent, <=2h, >2h, horizon earlier than oldest retained observation, and numerical match (finite arithmetic, tolerance 1e-9 absolute + 1e-9 relative). Two hours is a diagnostic benchmark from cache fallback defaults, not an approved historical-label threshold. Record cache schema but do not claim a current cache row recovers the original labeling input. labeled_at is row finalization time, not per-horizon observation time.

Report seven-day populations by producer plus last emitted/labeled timestamps and one-day/one-hour counts. These establish observed rates only, not future guarantees. Global min/max timestamps describe surviving observations, not configured retention or proof pruning executed. Compare production labeler/analyzer/prune-function source hashes to audited checkout; report mismatches rather than attributing checkout code to production.

## Interpretation and output

Absence today is non-replayability, not proven historical corruption. A matching reconstructed return is numerical consistency, not original source/time proof. Do not recompute headline dollars, prescribe pruning or change label policy. Health-only UI can proceed with counts and warnings; any monetary surface must distinguish unverified provenance and avoid implying exact-seven-day/executable returns.

Write tasks/findings_dash11_provenance_2026_09_14.md, update only active todo/plan review evidence. Include reproducible query/method, source-line references, exact runtime times, limitations and next engineering gate. No backlog status closure beyond this audit. Revert documentation commit for rollback; no deployment required.

## Plan folds

Structural reviewer approved: exact analyzer cohort/tie semantics and replayability-versus-validity distinction adopted above. Safety review recorded after response; design cannot proceed to execution before both plan and both design reviews complete.

Safety plan review approved with folds adopted: pinned BEGIN, UTC/canonical selection, hard query deadlines; inspect EXPLAIN QUERY PLAN for the per-token historical lookup before running the cohort. No raw DB copy.

Both design reviewers approved. Execution folds: original stored anchor; invalid anchors counted; timeouts mean incomplete evidence. Runtime results and the EXPLAIN inspection timing deviation are recorded in findings.
