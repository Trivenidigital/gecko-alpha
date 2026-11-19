# Historical postmortem viewer plan

**Goal:** expose existing moved-already captures as a bounded, descriptive history in the dashboard.
**New primitives introduced:** one read-only list query/API and one Performance tab; reuse SQLite read-only connections and existing React navigation. No evidence detail endpoint in V1.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Gecko historical postmortem list | none found in inspected ecosystem directory; Skills Hub catalog did not load | Build small repo-specific adapter over existing Gecko table |
| Dashboard navigation and read-only SQLite | Existing Gecko primitives; no applicable Hermes replacement identified | Reuse dashboard/db.py _ro_db and dashboard/frontend/App.jsx |

Checked https://hermes-agent.nousresearch.com/docs/skills and
https://github.com/0xNyk/awesome-hermes-agent on 2026-09-13. Hub returned a
loading shell, not a complete searchable catalog. The ecosystem directory offers
agent surfaces and orchestration rather than a Gecko table consumer. Verdict:
keep Hermes orchestration intact; implement only this domain-specific viewer.

## Drift and runtime evidence

Base eaceb8071f4d86ebe1cf00eb2137e3323f17e1d2 in isolated branch
feat/postmortem-history-20260913. Source recorder exists in
scout/postmortem/moved_already.py:152 and schema in scout/db.py:2150;
rg found no postmortem consumer in dashboard Python/JSX. Prior residual is
recorded in tasks/closeout_followthrough_2026_09_13_2114.md:46. This closes only
historical list visibility, not full DASH-05 missed-token capture/attribution.

Root runtime probe: production d2f0d61e, 31 stored rows, latest capture
2026-08-09T01:39:15.405190+00:00, zero open paper trades. Evidence strings maximum
15546 characters. Corrected root probe 2026-09-13T22:57:13.271140Z confirms malformed JSON=0; entry_mcap_snapshot is null in all31. List V1 never selects/parses
that evidence, so malformed/large JSON cannot block it. No inferred evidence
completeness from historical presence alone. Existing history can be viewed independent
of recorder flags or future trades; no forward-fire soak is needed. Parent
coordinates current runtime schema validation before implementation, reviewers,
PR575 base reconciliation, and any final merge. No deployment in this slice:
active capture/migration owner remains separate.

## Proposed contract for review

GET /api/postmortems/moved-already?limit=25&before_id=<positive integer>.
FastAPI validates limit 1..100; before_id is optional positive SQLite signed
64-bit integer (maximum 9223372036854775807). Keyset query uses bound parameters,
id DESC and LIMIT limit+1; next_before_id is the last returned id only when
has_more. Response next_before_id is a decimal string; the UI preserves it without numeric conversion. Ordering is explicitly newest recorded, not newest detection time.
No arbitrary sorting/filtering, OFFSET, evidence blobs, joins, or new dependencies.
Rows contain decimal-string id (preserves SQLite 64-bit precision in JavaScript), token_id, detected_at, run_pct, and
most_frequent_recorded_block_reason (alias of dropping_gate). Non-finite or
non-numeric run_pct becomes null; preserve valid negative/zero values rather
than making unsupported quality decisions. Token/block strings are ordinary
React text, not HTML. Numeric database fields must serialize as strict JSON.

Metadata contains ok, read_only, historical_only, generated_at, total_records,
latest_detected_at (the detected_at attached to newest recorded id; not lexical MAX), sort_policy, limit; envelope contains rows, has_more,
next_before_id. Read count/latest/page in one read transaction so aggregate
metadata and page agree. Missing DB/table/query failure is 503 with ok=false,
rows=[], explicit data_missing_reason, Cache-Control:no-store; an existing empty
table is 200 with total_records=0. Responses do not claim capture freshness health.
Latest timestamp is labeled Capture time of newest recorded row, never pipeline health.

Add Historical Postmortems under Performance through dashboard/frontend/App.jsx.
Display token id, capture time, Price change from paper entry, and Most frequent
recorded pre-detection block reason. run_pct means the recorded percentage price change from the selected most-recent open paper trade entry price to the cached price at capture; it is neither a 24-hour change nor realized return. Copy: These captures came from open paper
trades above the configured run threshold at recording time. Historical
observations; not comprehensive missed-token coverage. Block frequency is not
causal attribution. Null block reason means no recorded reason, not no blockage.

Use a fixed 25-row page, cursor stack for Previous, Next only when has_more,
Refresh resets to the first page. Fetch on tab mount/paging/manual refresh;
AbortController or request identity prevents older responses overwriting newer
pages. Loading, unavailable, empty and valid history are distinct. No links or
actions offering execution, dispatch, enable/disable, pruning or reclassification.

## Review and implementation sequence

- [x] Clean isolated worktree/branch; source drift and current Hermes check.
- [x] Two independent plan reviews coordinated by parent; structural approval and operations reapproval at527ce25c.
- [x] Write tasks/design_postmortem_history_2026_09_13.md; two independent design reviews and folds before build (final e7b5cc28).
- [x] Add failing tests in tests/test_postmortem_history_endpoint.py: populated/empty/missing DB/table; exact fields/no evidence; pagination with id gaps and intervening inserts; invalid limit/cursor; strict finite JSON; read-only database preservation.
- [x] Implement get_postmortem_history in dashboard/db.py and GET route in dashboard/api.py; typed response models in dashboard/models.py if consistent with reviewed design.
- [x] Add dashboard/frontend/components/PostmortemHistoryTab.jsx; wire App.jsx and minimal style.css reuse. Extend navigation guard; cover async error/stale response/page state through repo-supported frontend tests.
- [x] Run focused endpoint/navigation tests, existing API regression tests, frontend build, git diff --check. Use shared installed Python; document exact commands/results.
- [x] Commit meaningful verified changes; reconcile latest merged origin/master after PR575, rerun affected checks, push feature branch and create PR.
- [x] Two independent PR reviews approved exact 2cb1d4f9 (structural/read-only bounds and semantic/UI truth); per-app DB isolation fix reapproved. PR577 has codex and codex-automation labels.
- [ ] Exact final-head CI green before parent considers merge; deployment remains held.

## Files and release boundary

Modify dashboard/db.py, dashboard/api.py, dashboard/models.py (only if needed),
dashboard/frontend/App.jsx, dashboard/frontend/style.css (only if needed),
tasks/todo.md. Create panel, focused endpoint/UI tests, design and review evidence.
No recorder, writer, schema, config, dispatch, model threshold, production file,
service, or DB changes. Rollback after a later authorized deployment is reverting
the viewer commit and rebuilding prior UI; no data restore/migration needed.

## Review

Structural plan reviewer approved for design with required precision/timestamp semantics folded above. Operations reviewer requested the price-change definition and exact column label; both folded above, operations reapproval received. Design and build complete; 142 focused tests and independent visual QA passed. Current source
and row inventory support a display; broader capture coverage, causal analysis,
T-minus reconstruction and writer watchdog remain explicitly separate residuals.
