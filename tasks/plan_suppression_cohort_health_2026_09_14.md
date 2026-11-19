**New primitives introduced:** One read-only suppression-cohort health endpoint and one small dashboard panel; no new storage, writer, scoring, pricing, or operational primitive.

## Hermes-first analysis

Drift was checked first on origin/master d7a0e2672c8d67ba33dc47cfd1a818c2b2bad5b3 on 2026-09-14. The residual is presentation of existing ledger diagnostics, not a new analyzer.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Gecko suppression-cohort semantics | None found in accessible catalog/directory | Reuse in-tree analyzer predicates and count definitions; integrate a bounded reader |
| SQLite read-only aggregate dashboard | No project-specific replacement found | Reuse dashboard connection/error patterns and existing ledger tables |
| React health presentation | No applicable project component found | Reuse existing health-panel conventions within the current dashboard |

Checked https://hermes-agent.nousresearch.com/docs/skills/ on 2026-09-14; the fetched page returned a loading catalog, so this is not an exhaustive absence claim. Checked the accessible https://github.com/0xNyk/awesome-hermes-agent directory (the guessed NousResearch directory URL was absent). Its agent surfaces and tooling do not supply this project's cohort contract. Verdict: a small in-tree integration is appropriate; add no dependency or Hermes runtime.

## Drift and bounded objective

`scripts/suppression_cost_rollup.py:118` already reads the recall lane. Lines 170-206 isolate dispatcher suppression and count recorded labels; lines 208-219 retain the earliest emission per token before checking r7d; lines 224-279 report per-signal population diagnostics. PR571 shipped those diagnostics. Do not redo them or invoke the CLI cost/send path.

`dashboard/db.py:910` exposes generic ledger freshness, not suppression-specific counts or maturation. `dashboard/frontend/components/SourceCallsHealthPanel.jsx:4` describes a different source_calls population. Searches for suppression/recall/gated_out across dashboard API, DB and components found no matching cohort-health endpoint/panel. The residual is real, while the producer, ledger and analyzer primitives already exist.

Add a small **Suppression cohort health — EXPERIMENTAL** panel under the existing Pipeline view next to DispatchFunnelPanel (`dashboard/frontend/App.jsx:275`). It answers how many dispatcher-suppression records and recorded labels are visible in bounded windows, which signal populations cannot be reconciled, and why these records cannot establish cost or ranking. No new navigation group.

PR579 remains separately owned. Its dated findings report 189/214 matured horizons preceding retained historical observations and 25 numerically matching surviving candidates. Those observations neither prove historical mislabeling nor verify provenance. They must not become live dashboard counters or readiness thresholds. No repeat historical-price reconstruction is part of this work.

## Proposed contract and boundaries

- Fixed 7-day population window and 30-day maturation lookback, both ending at one explicit UTC as-of time in one read transaction. These intentionally differ from the CLI's 120-day default; show both dates. Count only retained rows in these intervals, never claim lifetime totals or complete suppression-event coverage.
- Reuse the existing two-tag cohort: kind gated_out_sample, gate verdict reason suppressed, source_layer dispatcher. Compare ledger surface counts with trade_decision_events reason suppressed by signal. Display raw counts; omit sampling percentages entirely because count agreement cannot prove event matching. Ledger-only, event-only and excess counts remain visible with population-mismatch/unknown-coverage language, not a green coverage badge.
- Show lookback row count, distinct tokens, recorded r24h/r7d non-null counts, pending/partial and unlabelable counts. Recorded-label availability is not validated price provenance. If distinct earliest-anchor r7d availability is shown, select the earliest lookback emission before inspecting resolution, matching the existing analyzer; design must explicitly handle equal timestamps deterministically without implying a verified trading opportunity.
- Show malformed/unattributable gate-verdict counts for the broader gated_out_sample lookback separately; they are not known suppression records. Define missing/null/invalid token identities and timestamps explicitly in design, report exclusions/unknowns, and never silently count them as valid distinct assets. Upper-bound windows exclude future rows. Any intentional departure from CLI timestamp assumptions must be named and fixture-tested.
- Provenance remains **unverified; insufficient evidence for cost or ranking** for every successful response, including large mature cohorts. No n threshold unlocks conclusions. Empty valid cohorts show **insufficient data**; missing DB/schema, timeout, scan/memory limit, or malformed unsupported schema show **unavailable**, never zero-success. Unknown label states remain visible rather than silently joining known states.
- No returns, dollars, win rates, top movers, token ranking, causal attribution, suppression recommendations, dispatch controls or policy mutation. No raw token identifiers or verdict JSON in the response. No tables, migrations, writers, alerts, flags, dependencies or scheduled jobs.

## Runtime prerequisites before design/build

Parent authorized bounded aggregate-only probes on 2026-09-14. Fresh runtime measurement is pending; historical reports are context, not current health evidence. Before design, capture one timestamped mode=ro + query_only + BEGIN snapshot with a hard deadline and guaranteed close, without Database.initialize or analyzer cost/send paths.

| Assumption | Required evidence and response to failure |
|---|---|
| Correct runtime source and DB | Checkout SHA, service working directory and relevant source hashes; actual DB schema columns/indexes, no secrets. Reconcile differences before selecting query path. |
| Complete selected query population within bounds | 7d ledger and decision counts by signal; 30d cohort/label-status counts and distinct/earliest-anchor availability. Include malformed verdict, invalid/future timestamp and identity diagnostics. No LIMIT-based sample may be labeled complete; timeout or scan limit is unavailable/partial evidence and cannot justify a completed endpoint. |
| Producer populations and retention | Reuse PR579's dated source trace; verify current suppression call sites and prune settings/code, oldest/newest retained timestamps per table. In particular chain/first ledger-only producers prevent full denominator inference even when current counts happen to match. |
| Recorded labels do not certify provenance | Verify current label/schema capability only; reuse PR579 limitations. No repeated historical-price audit, API purchase or forward-price probe. |
| Reader affordable on actual table volume | Capture query plan, relevant row volumes and one elapsed observation; benchmark a private production copy before build acceptance. One observation is not an SLO. |

If bounded diagnostics cannot finish, preserve that result and revise the scope/query before design rather than silently using a current page or arbitrary sample. A runtime population mismatch is expected displayable evidence, not authorization to fix producers.

## Implementation and verification plan after approvals

1. Obtain two independent plan reviews: cohort/interpretation and operational/read-only bounds. Fold required findings, then write design with exact response schema, SQL, parameter-free route, supported unavailable states, UI location and cancellation/resource limits. Obtain two independent design approvals before code.
2. TDD a narrowly scoped dashboard reader. Reuse reviewed health semantics without importing script CLI/sys.path side effects or computing then discarding cost. Prefer SQL aggregation over materializing all ledger rows; design must bound SQLite work, Python aggregation, acquisition/cancellation cleanup and simultaneous reads. No shared DB broad refactor.
3. Test mismatched signal populations, event-only/ledger-only lanes, duplicate emissions, earliest-unresolved versus later-resolved token, empty data, malformed verdict/identity/timestamp, future rows, unknown statuses and window boundaries. Prove read-only with real SQLite fixtures; fail closed on missing tables, actual SQL timeout and repeated cancellation including acquisition. A pinned transaction must keep component counts consistent during concurrent fixture writes.
4. Add API and panel tests that prevent cost/ranking fields, stale-success or zero-on-error presentation. Show independent window labels, mismatch, unavailable and insufficient-data states. Verify fetch abort/unmount behavior. Run existing analyzer regressions without changing its output, relevant dashboard tests, and frontend build/dist parity. Use browser fixture visual QA for normal, mismatch, empty, unavailable and narrow-screen views; no live DB controls.
5. Report exact tests and limitations, two independent PR reviews and final-head CI. Publication/merge/deployment remain coordinated separately; this plan grants no production action. No soak is needed for a purely descriptive reader, but no outcome/performance claim follows from its release.

## Status and authorization

PLAN ONLY. Parent's 2026-09-14 autonomous health-only assignment authorizes this isolated plan/docs commit and bounded read-only prerequisite probes. No design, feature implementation, build, production writes or deployment performed by this plan. PR579 ownership and frozen selective release07af remain untouched.
