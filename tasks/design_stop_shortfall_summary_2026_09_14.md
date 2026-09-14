# DASH-09 all-history entry-stop shortfall summary design

**New primitives introduced:** bounded read-only summary adapter/API, typed envelope
and small Trading-tab summary component. Existing classifier remains unchanged;
no new evidence eligibility, return formula, dependencies, writers or schema.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Gecko frozen entry-stop classification | none found in accessible ecosystem; Hub catalog loading | Reuse dashboard.stop_shortfall.classify_stop_shortfall unchanged |
| Full-history summary transport/rendering | none found for Gecko contract | Small local adapter over existing read-only DB and React tab |

2026-09-14 drift and checks recorded in the approved plan: Skills Hub
https://hermes-agent.nousresearch.com/docs/skills/ returned a loading shell;
https://github.com/0xNyk/awesome-hermes-agent directory inspected. Verdict: generic
Hermes surfaces do not replace this existing paper-evidence contract; preserve
Hermes orchestration and build only the residual adapter. Catalog limit disclosed.

## Approved plan and runtime prerequisites

Both independent plan reviewers APPROVE4085bf88252018f20799f8d24fef1080c7535962;
no required folds. This is DESIGN ONLY, awaiting two design approvals before build.
Root owns integration/deployment and other viewer branches. Isolated branch and
worktree remain docs/stop-shortfall-summary-plan-20260914 at
C:/projects/gecko-alpha-stop-summary-20260914; basee6a55d7a.

Fresh root read-only/query_only/BEGIN probe2026-09-14T00:28:58.412684+00:00:
classifier SHA256e129e23383e9ef9b8da12a726e3eab423705f112e3928c628154d1011c725778
matches local; all required trade/snapshot columns exist; snapshot PKpaper_trade_id,
duplicate keys0.355stop rows,23eligible; min0.39388136086476777pp,
max3.7647225708680523pp, mean1.3471808080779946pp, median1.1309679311149452pp.
Eligible close range2026-07-10T21:31:00.389644+00:00 through
2026-08-09T01:57:22.562792+00:00. Probe17.931ms with10s SQLite progress guard is
one timing observation, not an SLO. Earlier00:25:21 snapshot reconciles23eligible,
4modeled,328unavailable and zero30d closes. No forward-health inference.

Evidence file read:
C:/Users/srini/.codex/automations/gecko-overnight-autonomous-closeout/runtime-stop-design-20260914.txt.
Production checkoutd2f0d61e/service state is root-provided evidence, not a claim
this summary or all current master changes are deployed. Eligibility does not
depend on dispatch flags or new events; no calendar soak needed.

## Files and interfaces

- Create dashboard/stop_shortfall_summary.py: async get_stop_shortfall_summary(db_path:
  str) -> dict. Imports existing _ro_db from dashboard.db and unchanged
  classify_stop_shortfall from dashboard.stop_shortfall. Owns bounded query,
  reduction and explicit unavailable exception categories; no scout imports.
- dashboard/api.py: register GET /api/trading/stop-shortfall-summary. Capture an
  immutable local summary_db_path when create_app runs; never resolve the mutable
  module-global path on requests. This route-only isolation avoids unrelated API
  refactoring. Calls adapter, returns response model JSON and status/headers.
- dashboard/models.py: StopShortfallSummaryMeta, StopShortfallSummaryData and
  StopShortfallSummaryResponse with finite-float fields and Literal state/scope.
- Create dashboard/frontend/components/StopShortfallSummary.jsx, import it into
  TradingTab.jsx immediately above the closed-history table and its filters.
- Create tests/test_stop_shortfall_summary.py and
  tests/test_stop_shortfall_summary_frontend.py. Existing classifier/history tests
  remain intact. Regenerate dashboard/frontend/dist using existing Vite build.

No edit to classify_stop_shortfall; helpers shared elsewhere remain unchanged.
The new adapter imports db._ro_db one-way; dashboard.db does not import the new
module, avoiding a circular import. api imports the adapter directly.

## Request and response

The endpoint takes no filter/pagination options. Its scope is all stored rows
WHERE status='closed_sl', including every actionability group. No date filter.
Metadata: ok, generated_at(UTC), read_only=true,
basis='historical_paper_experimental', scope='all_stored_closed_sl',
independent_of_table_filters=true, cutover_ts:string|null,
data_missing_reason:string|null. Successful data fields:
state:available|empty|no_eligible_rows; total_stop_rows:int; eligible_rows:int;
modeled_rows:int; unavailable_rows:int; exclusions_by_reason:dict[str,int];
mean_shortfall_pp:finitefloat|null; median_shortfall_pp:finitefloat|null.

Success200/no-store. Empty is total0/all counts0/empty histogram/null statistics.
Nonempty without eligible rows is no_eligible_rows, actual counts/histogram and
null statistics. Available includes genuine0values. API retains full precision,
UI displays two decimals. Per-trade equal weighting, including repeated tokens;
no inference of independence, significance, expectancy or profitability.

Unavailable503/no-store/Retry-After60: meta.ok=false and sanitized reason;
data.state=unavailable, EVERY count/statistic=null and exclusions_by_reason=null.
Never mix partial numbers into a failed response. Reasons:
database_unavailable, evidence_schema_unavailable, cutover_unavailable,
duplicate_snapshot_identity, input_bounds_exceeded, population_too_large,
query_timeout, query_failed, invalid_aggregate. No internal exception text/SQL/path
in response; structured stop_shortfall_summary_unavailable log records category
and exception type. Unexpected failures additionally log exception server-side.
Request cancellation is propagated after cleanup, not rendered as successful JSON.

## Read path, classifier fidelity and bounds

1. Set start=monotonic(), deadline=start+3seconds and a threading.Event stop flag.
   Use existing _ro_db(mode=ro), set PRAGMA query_only=ON and busy_timeout=100ms,
   then install SQLite progress handler every1000VM operations. It returns1 if
   stop flag set OR monotonic>=deadline. Begin explicit read transaction before
   any schema/count/cutover/evidence queries. Connection setup and cleanup also
   sit inside the outer5second async timeout described below.
2. Verify required schema via PRAGMA table_info. All classifier evidence fields
   must exist; missing tables/columns fail503. Snapshot key must be uniquely
   declared paper_trade_id PRIMARY KEY (otherwise fail503 duplicate_snapshot_identity
   even if current rows happen unique). This is read-only validation, no migration.
3. Read price_provenance_v1 cutover with a fixed-name bound parameter and require
   exactly one nonempty timezone-aware parseable timestamp. Missing/invalid/multiple
   markers fail503 cutover_unavailable. Do not replace it with a default date.
4. COUNT(*) stops within the same transaction. Count>50000 returns unavailable,
   never a sample/partial summary. Count0 returns empty after prerequisites pass.
5. Select ONLY classifier fields from paper_trades LEFT JOIN snapshots by PK:
   p.id,status,exit_provenance,price_source,closed_at,entry_price,exit_price,
   quantity,remaining_qty,amount_usd,realized_pnl_usd,conviction_locked_at,
   leg_1_filled_at,leg_2_filled_at,s.entry_snapshot_version,s.sl_pct_at_entry.
   No tokens, symbols, signal_data, blobs of trade metadata or PnL calculation.
6. Bound each selected TEXT/BLOB cell before crossing the SQLite/Python boundary:
   CASE on typeof/length(CAST(field AS BLOB)), maximum4096bytes per field. If
   oversized, project NULL plus a row input_oversized flag and fail the WHOLE
   summary input_bounds_exceeded. For bounded cells retain SQLite original type
   and value; classifier sees the exact evidence (invalid types still exclude).
   This applies to every selected field, including columns with numeric affinity,
   and cutover; no truncation, decoding/coercion or weakened eligibility. Invalid
   UTF-8 decoding fails query_failed. Bound payload ensures a malformed single
   row cannot allocate unbounded memory or dominate Python classification.
7. fetchmany(128). Before every row check deadline and stop flag; call unchanged
   classifier(row,cutover). Only available.shortfall_pp enters numeric values.
   Count modeled/unavailable and exact exclusion reasons. Yield await sleep(0)
   after each batch so outer cancellation can run. Check count reconciliation
   against COUNT; unexpected state/row mismatch fails whole result.
8. Mean uses statistics.fmean(values), median statistics.median(values), with
   finite guards and OverflowError handled as invalid_aggregate. This is reduction
   of existing classifier outputs, not a second price formula. Check deadline
   before/after each reduction and serialization. At most50000 primitive floats
   bound the synchronous sort; no arbitrary user comparator/callback can run.
9. Close cursor/connection, ending read transaction before returning. No cache,
   materialized table, watcher, scheduled work, writes or persistent settings.

## Cancellation and concurrency

Wrap adapter work in asyncio.timeout(5). The3second monotonic budget is checked
by SQLite progress handler AND Python row/reduction checks; a SQL-only timeout
would leave Python work unbounded. At cancellation/TimeoutError set stop event,
await connection.interrupt(), then await connection.close() in cleanup before
propagating cancellation or reporting query_timeout. Keep the progress handler
installed until SQL has stopped; do not enqueue its removal ahead of interrupt.

SQLite interruption is direct; busy locks are capped100ms. The progress callback
makes a still-running VM stop even if the asyncio request timed out. _ro_db's
finally still closes the connection; cleanup must remain awaited rather than a
fire-and-forget task. No success returned after deadline. These are cooperative
application bounds under a functioning scheduler/local filesystem, not a promise
to preempt an OS-level filesystem stall. Test real SQL cancellation and injected
slow Python classification independently. Repeated cancellation cleanup should be
shielded until close completes, then propagate the original cancellation.

All connections/state are per request. No mutable cache/global result and no
shared DB connection. Two concurrent requests may read independent snapshots;
each individual count/classification/statistics set is internally consistent.

## UI behavior and wording

Component fetches once on mount and explicit Refresh/Retry only. Its request state
is independent of TradingTab's fetch Promise.all and pagination/filter state.
AbortController plus monotonically increasing request identity rejects stale
success/error/finally and unmounted updates. Refresh clears prior numeric values
while loading; error clears old values and shows Retry. A failed summary never
hides/changes the existing trade rows, filters, counts, sorting or PnL.

Title: Historical entry-stop shortfall. Persistent badges PAPER / EXPERIMENTAL.
Scope: All stored paper stop exits — independent of table filters.
Mean/median labels include pp. Display Eligible paper trades n / total stop exits,
modeled count, unavailable count and readable exclusion counts. Every nonempty
cohort displays “Historical sample; repeated tokens may appear. Not for pruning,
sizing or dispatch.” Caveat: “Recorded exits already include modeled paper
slippage. This compares recorded exit prices with the frozen entry stop; it is
not execution slippage, terminal-stop overshoot or expectancy.” No colors implying
profitable/healthy ranking and no threshold recommendations/actions.

Empty: “No stored paper stop exits.” No eligible: “No eligible stop-exit evidence,”
with exclusions and unavailable statistics. Error: “Summary unavailable” with
sanitized reason/Retry, no zero counts. Loading is explicit. Show generated time
as snapshot generation, never capture freshness or pipeline health.

## Tests and falsifiers

API/query fixtures: classifier parity;355-like exclusions; n0/n1, zero, even/odd
median; more than one history page with eligible values only beyond page1; page/
actionability/eligible-only changes do not alter summary. Repeated-token trades
remain distinct observations. Missing DB/schema/cutover versus empty; duplicate
key declaration; malformed/oversized text and numeric-affinity blobs; overflow;
strict JSON and no partial results on timeout/50001rows. Counts reconcile.

SQLite authorizer denies writes; preserve schema/data; use deterministic barrier
between COUNT and scan plus second-connection insertion to demonstrate snapshot
isolation. Two apps with different databases stay isolated. One test induces a
long SQL VM and verifies progress interruption/connection closure; another injects
slow Python classifier work and proves deadline rather than SQL hook stops it.
Instrument both successful/error/cancel paths to verify connection close and that
a fresh subsequent request succeeds. Polling/deadline tests use generous test
ceilings and deterministic injected clocks/events where possible, not fragile
production-millisecond assertions. Verify actual SQL progress behavior at least
once rather than mocking away every cancellation path.

Frontend executable tests prove stale success/error/finally refusal, independent
summary failure, empty/no-eligible/zero distinctions, filter-independent labeling
and refresh. Synthetic visual QA covers all states and pp/n/exclusion readability.

Run focused tests/test_stop_shortfall_summary.py, test_stop_shortfall.py,
test_trading_dashboard.py, test_dashboard_api.py and summary frontend suite;
existing related contracts; touched Python formatting; Vite build and diff check.
Do not broaden tests without a changed integration concern. Commit source/dist,
then root obtains2PR reviews and exact-candidate CI before release.

## Release and rollback

Root refreshes production ownership/revision and repeats bounded read-only cohort
comparison before deploying any merged artifact. Exact API cohort parity and
existing history GET smoke required; classifier must remain byte/behavior stable.
If evidence changed, explain using current rows rather than expecting355/23 forever.
Revert new dashboard source/component/dist to previous approved version; no data
restore/migration. No source suppression, flag changes, live orders, capital,
paid APIs or external messages. Design review pending; implementation remains held.

## Design clearance

Both independent design reviewers APPROVE e4f8ecbc0895fd69f8dc2a98edb4d180805391b9, no folds. Root explicitly authorized build after those terminal approvals. Fresh dashboard WorkingDirectory=/root/gecko-alpha and checkout d2f0d61e at00:36Z are root-provided runtime evidence, not deployment of this candidate.

## PR review cleanup and cutover fold

Acquire the read-only context in an owned asyncio task and await it through shield. The cleanup try begins before that await. On cancellation/timeout, retain and await eventual acquisition before interrupting and closing its connection, including repeated cancellation. An OS connector stall can therefore outlive the request budget while cleanup waits; the request must not falsely complete and abandon ownership. No shared _ro_db refactor. Validate cutover UTC normalization, including overflow, consistently with the unchanged classifier.
