# DASH-09 all-history entry-stop shortfall summary plan

**New primitives introduced:** one bounded read-only summary query/API and a small
summary panel in the existing Trading tab. Reuse classify_stop_shortfall unchanged;
no new eligibility, return formula, data writer, schema, dependency or trading rule.

## Hermes-first analysis

Drift first: dashboard/stop_shortfall.py:30 already provides the authoritative
classifier; dashboard/db.py:3203/3261 enriches only a history page. Existing
plan_stop_shortfall_surface_2026_09_13.md explicitly excludes aggregates, and
report_stop_shortfall_surface_2026_09_13.md records the all-history residual.
Source search found no all-history shortfall summary. Preserve the shipped row
surface instead of rebuilding it.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Gecko historical stop eligibility | none found in accessible directory; Skills Hub catalog did not load | Reuse existing Gecko classifier; no replacement math |
| Full-population read-only dashboard aggregate | none found for this repository contract | Build small adapter and use standard-library descriptive statistics |

Checked https://hermes-agent.nousresearch.com/docs/skills/ and
https://github.com/0xNyk/awesome-hermes-agent on 2026-09-14 after drift. Hub was a
loading shell; accessible ecosystem directory contains general skills/surfaces,
not this Gecko paper-evidence contract. Verdict: residual repository integration
only, with Hermes remaining orchestrator; this is not an exhaustive catalog claim.

## Status and authorization

PLAN ONLY. Root must obtain two parallel plan reviews and fold findings before
writing design; two parallel design reviews/folds must then precede build. The
operator's autonomous production-push request authorizes the read-only residual;
no operator-only gates are implicated. Root coordinates reviewer dispatch and any
later integration/deployment. Prior row-surface approvals do not approve this new
summary. No implementation, new design, PR, deployment or runtime mutation here.

Clean new worktree C:/projects/gecko-alpha-stop-summary-20260914, branch
docs/stop-shortfall-summary-plan-20260914, base origin/master
e6a55d7ad3f2029d3ef63fea0104d49a9e082fca. Reviewed CLAUDE.md, lessons, prior plan,
prior report and current classifier. Repository AGENTS.md is absent; supplied
operator rules apply. Using writing-plans, using-git-worktrees and brainstorming
skill guidance, subject to the operator's explicit plan-before-design sequence.

## Goal and chosen boundary

Expose mean and median recorded entry-stop shortfall in percentage points across
all stored closed_sl paper trades that the existing classifier marks available.
Show the full population and exclusions alongside those descriptives. This is
historical PAPER / EXPERIMENTAL, not execution slippage, terminal-stop overshoot,
expectancy, portfolio PnL, ranking or evidence for sizing/pruning/dispatch.

Recommended: standalone GET /api/trading/stop-shortfall-summary, rendered as an
independent summary block in TradingTab. It avoids changing legacy history/list
contracts and permits explicit error handling. Alternatives considered: adding
fields to trading stats risks ambiguous stats/filter scope; aggregating browser
pages is incorrect because pagination hides the relevant population.

The scope is ALL stored closed_sl rows, independent of history pagination,
actionability filter, eligible-only toggle, ordering and time filters. Label it
“All stored paper stop exits — independent of table filters.” Do not provide
summary filter parameters or imply filtered/current-page means. Other close
statuses and open positions are outside the declared population, not silently
counted as unavailable stops. No time-series, breakdown ranking or new scoring.

## Runtime evidence and remaining assumptions

Root reports production checkout d2f0d61e with actual pipeline/dashboard/Hermes
units active. This agent does not re-attest loaded code or deployment. Fresh
read-only, query_only, BEGIN snapshot at 2026-09-14T00:25:21.506293+00:00 executed
the actual merged classifier in memory with the full stop/snapshot join:

- 355 stored closed_sl rows: 23 available, 4 modeled, 328 unavailable.
- Exclusions: price_source_unverified324, modeled_exit4, conviction_modified4.
- Eligible descriptive mean1.3471808080779946pp; median1.1309679311149452pp.
- price_provenance_v1 cutover2026-07-03T00:32:14.634856+00:00.
- Zero closes during the preceding30days. No recent/forward inference or ongoing
  capture/health claim follows from this historical snapshot.

Artifact read: C:/Users/srini/.codex/automations/gecko-overnight-autonomous-closeout/runtime-stop-cohort-20260914.txt.
These values are verification fixtures for this observation, never hardcoded UI.

Before design approval, root must verify or provide the exact SQL/probe evidence:
1. Current schema includes every classifier input and snapshot uniqueness;
   source declares paper_trade_id PRIMARY KEY at scout/db.py:7736, but runtime
   must verify PRAGMA table_info and duplicate count. No join multiplication.
2. Full-population cardinality and query time under one read transaction; the
   observed355rows permits a bounded read, not an unbounded growth assumption.
3. Consuming path uses the app's explicit db_path, never mutable module-global
   redirection; verify two-app isolation in tests and production service path.
4. Existing classifier output for all355rows reconciles exactly to totals;
   verify source file hash used by runtime probe and cutover validity. Any
   unavailable evidence stays unavailable; do not widen eligibility for n.
5. Ownership/deployed revision before eventual deploy. No flag change is needed;
   historical query does not require new dispatch fires, so no forward soak.

Probe requirements: mode=ro, query_only=ON, explicit BEGIN, bounded wall timeout,
no ORM migration/import side effects, no writes/messages. Read the required
columns, snapshot PK/count, cutover, COUNT(*) WHERE status='closed_sl', classifier
state/reason histogram, eligible min/max/mean/median and close timestamp range.
Do not print token identities. SSH output must be redirected then read in a
separate tool invocation. Repeat a freshness probe if ownership/runtime changes
before design or deployment; do not rerun merely to satisfy calendar duration.

## Proposed contract for plan review

Success200, Cache-Control:no-store. Metadata identifies generated_at, read_only,
basis='historical_paper_experimental', scope='all_stored_closed_sl',
independent_of_table_filters=true and cutover timestamp. Summary fields:
state ('available', 'empty', 'no_eligible_rows'), total_stop_rows,
eligible_rows, modeled_rows, unavailable_rows, exclusions_by_reason,
mean_shortfall_pp, median_shortfall_pp. No token rows or ranking in this response.

Reuse classify_stop_shortfall for EVERY stop row; reduce only its available
shortfall_pp values. Never reimplement formula/eligibility in SQL/JS. Count modeled
separately from unavailable; exclusions_by_reason includes both, and
eligible_rows + modeled_rows + unavailable_rows == total_stop_rows. Mean/median
are unweighted per paper-trade observations, not distinct tokens; repeated tokens
are not independent evidence. No confidence or significance claim. n=1 remains a
labeled historical descriptive; there is no promotion/sample-size gate.

Existing empty table is state=empty, counts0 and mean/median=null. Nonempty stops
with no eligible evidence are state=no_eligible_rows, explicit exclusions and
mean/median=null. Eligible zero-valued shortfall contributes0 and is displayed as
0.00pp; never conflate it with unavailable. Keep full finite precision in JSON;
round only display. Arithmetic overflow/nonfinite output yields unavailable,
never permissive JSON NaN/Infinity or a misleading number.

Missing DB/table/required column, missing/invalid cutover, duplicate snapshot
identity, timeout or query failure: explicit503 unavailable, sanitized reason,
Retry-After:60, no-store; counts and descriptives null instead of zero. Log an
error category without exposing SQL, paths or raw evidence to the response. Do
not route through legacy get_trading_history catch-all returning[].

Bounded resource policy for review: COUNT and evidence scan in the same read
transaction. If count exceeds50000 stop rows, return503 population_too_large;
never compute on a truncated subset. Select only classifier fields, fetch in
batches, release connection promptly. Retain only eligible scalar shortfalls for
median and counters. Use existing SQLite/query timeout patterns; design must pin
a bounded total request timeout and prove cancellation closes the connection.
Do not add cache tables, indexes, migrations or background writers.

## Work units after plan/design approvals

- [ ] Complete two plan reviews/folds, including statistical meaning and query/
  isolation attack vectors. Record reviewed SHA and unresolved assumptions.
- [ ] Complete runtime assumptions above; write separate design document defining
  typed response, bounded query and independent UI state; obtain two reviews.
- [ ] Add failing tests in tests/test_stop_shortfall_summary.py using minimal DB
  fixtures and existing classifier evidence fixture shape. Then implement a
  read-only query helper in dashboard/stop_shortfall.py or dashboard/db.py (design
  chooses one), route in dashboard/api.py and schema in dashboard/models.py.
  Do not modify classify_stop_shortfall behavior or scout code. Commit meaningful
  API slice after focused checks.
- [ ] Add independent summary fetch/render to TradingTab.jsx, preferably a small
  StopShortfallSummary.jsx component. Explicit loading/error/empty/no-eligible/
  available states, manual retry, AbortController/request identity stale-result
  protection. Failure must not erase rows or affect legacy history requests.
  Refresh when tab mounts/manual retry; table paging/filtering cannot substitute
  a page statistic or change declared summary scope. Add executable UI tests and
  synthetic visual QA. Commit source plus regenerated distribution bundle.
- [ ] Create PR; root dispatches two parallel independent reviews/folds. Record
  real terminal clearances, exact final-candidate CI and focused verification.
  Merge/deploy only within production-push authorization with current ownership,
  smoke evidence and rollback. Root is deployment coordinator.

## Required falsifiers and verification

Tests must demonstrate full-population mean differs from first-page mean (more
than one page, eligible rows only on later pages); filter/page changes leave the
all-history result unchanged. Exact classifier parity on every fixture, including
legacy/unverified price, pre-cutover, modeled, partial, conviction, invalid numeric,
missing modification evidence, consistent quantities and entry notional. Assert
eligible+modeled+unavailable reconciliation and reason counts. Equal numeric
values from repeated tokens remain multiple trade observations, labeled as such.

Cover odd/even median, n0/n1, all-zero shortfall, mixed values and very large finite
values; strict JSON. Empty database/table versus missing schema/query/cutover must
be visibly different. Read-only bytes/schema preservation and denied write probe;
two-app DB-path isolation; duplicate snapshot row fails closed; same-snapshot count
and scan under an intervening insert; too-large population is unavailable with no
partial statistic; cancellation and failure do not poison subsequent requests.

Run C:/projects/gecko-alpha/.venv/Scripts/python.exe -m pytest
 tests/test_stop_shortfall_summary.py tests/test_stop_shortfall.py
 tests/test_trading_dashboard.py tests/test_dashboard_api.py -q --tb=short
Add the
executable frontend suite selected by design, npm --prefix dashboard/frontend run
build, touched Python formatting checks and git diff --check. Visual QA must show
PAPER/EXPERIMENTAL, filter independence, n/exclusions, pp units and all error/empty
states. No production testing via live trades or DB mutation.

## Release, rollback and plan-only review

Before deployment read the fresh production cohort and compare endpoint counts /
mean / median under an equivalent snapshot; differences require explanation, not
hardcoded expected355/23. Smoke only GET endpoint and existing history; check
explicit missing/unavailable response in local fixtures. Roll back dashboard source
and built assets to prior approved revision, preserving separately owned work;
no data restore or migrations. The summary does not close execution attribution,
recent performance, live sizing or the whole DASH backlog.

Plan self-review: only two documentation files are changed in this task; code,
DB/config and deploy remain untouched. Statistical and runtime assumptions are
explicit; new primitives are additive and eligibility remains existing authority.
Two independent plan reviews are pending; no implementation approval is claimed.
