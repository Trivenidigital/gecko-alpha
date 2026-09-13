# DASH-09 historical entry-stop display design

**New primitives introduced:** pure dashboard-only eligibility/calculation helper.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Paper attribution / Gecko history display | none found in accessible catalog; hub loading limited inspection | Extend existing repository read-only history, no third-party dependency |

Checked https://hermes-agent.nousresearch.com/docs/skills/ and
https://github.com/0xNyk/awesome-hermes-agent on 2026-09-13.
Ecosystem verdict: generic agent surfaces cannot replace Gecko-specific frozen
snapshot/provenance eligibility. Full catalog was not exhaustively searchable.

## Reviewed plan and runtime

Plan: tasks/plan_stop_shortfall_surface_2026_09_13.md.
Structural reviewer stop_gap_audit approved; attribution/ops stale_pr_audit
approved after explicit paper/experimental and exclusion-reason folds.
Runtime evidence and authorization are enumerated in the plan. No new writer,
table, flag, trade, message, ranking, aggregate or scheduler is introduced.

## Data contract

Each existing history row gains `stop_shortfall` with keys:
`state`, `exclusion_reason`, `entry_stop_pct`, `recorded_exit_return_pct`,
`shortfall_pp`, `basis` (constant `historical_paper_experimental`).
All three numbers are null unless state=available. States and precedence:

1. status != closed_sl: not_applicable/not_stop_exit.
2. exit_provenance=stop_gap_model: modeled/modeled_exit.
3. Missing enrichment schema/query: unavailable/evidence_schema_unavailable
   or evidence_query_failed (latter logged distinctly).
4. Explicit market required; missing or other provenance: exit_not_market.
5. Nonempty nonlegacy price source: price_source_unverified.
6. Snapshot version exactly v1: entry_snapshot_unverified.
7. Cutover and closed_at must parse as timezone-aware ISO times; compare in UTC;
   absent/naive/invalid: timestamp_unverified; close before cutover: pre_cutover.
8. Numeric type must be int/float, excluding bool, finite. entry>0, exit>=0,
   entry stop>0 and <=100, amount>0, quantity>0, remaining>=0;
   realized partial PnL must be finite. Otherwise invalid_numeric_evidence.
9. Non-null conviction_locked_at: conviction_modified; either ladder timestamp
   non-null, remaining not close to quantity (rel_tol=1e-9, abs_tol=0), or
   realized_pnl_usd !=0: partial_exit.
10. quantity*entry matches amount (rel_tol=1e-9, abs_tol=0); nonfinite product
    or inconsistent: inconsistent_notional. Compute exit return and shortfall
    without rounding; any nonfinite output: invalid_numeric_evidence.

Recorded exit return=100*(exit/entry-1); shortfall=max(0,-return-entry stop).
This is NOT terminal stop overshoot or execution slippage. Paper exit prices
already incorporate modeled slippage. Existing `outcome_integrity` is not used
for this eligibility and is unchanged.

## Query and failure behavior

dashboard/db.py retains original selected history, actionability, count and order.
Begin a read transaction on the existing mode=ro connection for consistent base
and enrichment reads. Enrich only selected IDs using one bounded parameterized
query (`p.*` plus named snapshot fields, keyed LEFT JOIN) and one cutover query.
No selected IDs => no enrichment query. Helper uses p evidence within the same
snapshot; no defaults of null to zero. Snapshot PK prevents row multiplication.

Missing table/column errors give evidence_schema_unavailable without dropping
history. Other sqlite enrichment errors give evidence_query_failed and a
structured warning without raw query/data. Unexpected exceptions must not be
mistaken for missing schema; guard enrichment independently from the legacy
history catch-all. Non-stop/modeled classification stays known even if evidence
is unavailable. Missing p fields are explicit invalid/unverified evidence via
the helper. No changes to history count, filters or writer code.

## Presentation

Add one nonsortable `Entry-stop shortfall` column after closed PnL %. Available:
shortfall to two decimals with `pp`; subordinate entry stop % and recorded
exit-price return %. Others: `Modeled`, `Not applicable` or `Unavailable` plus
human-readable exclusion reason, never a numeric zero placeholder.
Near history table show: `Historical paper / EXPERIMENTAL. Stored exits include
modeled paper slippage. Entry-stop comparison is not execution slippage or a
terminal-stop comparison; not for pruning, sizing or dispatch decisions.`
Missing object on older backend renders unavailable, so rolling deployment is
safe. No current-page mean or global aggregate; DASH-09 aggregate remains open.

## Files and verification

- dashboard/stop_shortfall.py: pure classifier.
- dashboard/db.py: independent read-only enrichment.
- dashboard/frontend/components/TradingTab.jsx: existing table addition.
- tests/test_stop_shortfall.py: unit boundary matrix plus small sqlite history
  fixtures testing fallback, parity, pagination, null and immutable DB behavior.
- Existing trading/actionability/dashboard tests, frontend Vite build and
  dashboard contracts; visual browser smoke using an isolated fixture DB.
- Tracked frontend dist built by existing npm build command.
- tasks docs and reviewer ownership record for this PR only.

## Delivery and rollback

Create feature PR, dispatch two independent reviewers for exact candidate,
fold all findings, require exact-head CI and clearance record before merge.
If deployed, verify no competing deploy owner, clean master revision and
allowed commit delta first; smoke GET history across available/modeled/missing
rows and unchanged counts, services and response latency. Rollback is reverting
the merged display commit through reviewed code, or restoring the prior deployed
revision while preserving unrelated operator changes. No schema rollback needed.
If capture owner controls deployment, leave rollout with that task and report
merged/not deployed explicitly; never claim production UI from local evidence.
