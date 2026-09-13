# Historical postmortem viewer design

**New primitives introduced:** one read-only history endpoint and one existing-dashboard tab; no new writer, schema, capture path or package.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Gecko postmortem history | none found in inspected ecosystem directory; Hub catalog loading shell | Reuse stored Gecko captures via small read-only adapter |
| SQLite and dashboard navigation | Existing Gecko primitives | Reuse _ro_db, FastAPI, App.jsx Performance navigation, table styling |

Current 2026-09-13 checks: https://hermes-agent.nousresearch.com/docs/skills
(catalog unavailable in fetched page), https://github.com/0xNyk/awesome-hermes-agent
(directory inspected; no Gecko postmortem consumer identified). Verdict: custom
code only for existing-table visibility; no Hermes orchestration replacement.

## Approved scope and runtime basis

Plan tasks/plan_postmortem_history_2026_09_13.md at527ce25c received structural
approval and operations reapproval. No source dashboard consumer exists; recorder
and schema already exist. Runtime schema confirmed by parent at
2026-09-13T23:02:09Z:31rows IDs1..31; id INTEGER PK, token_id/detected_at TEXT NOT
NULL, run_pct nullable REAL, evidence TEXT NOT NULL, dropping_gate nullable TEXT.
Newest ID31 capture2026-08-09T01:39:15.405190+00:00. All current run_pct values REAL;
max token/reason lengths23/20. Evidence malformed0/max15546chars, not read by V1.
Evidence source: parent automation postmortem-schema-runtime.txt. Zero open paper
trades does not block historical display. No claims of fresh capture operation.

## API and read isolation

GET /api/postmortems/moved-already with limit:int=Query(25,ge=1,le=100),
before_id:int|None=Query(None,ge=1,le=9223372036854775807). Standard invalid query
response422. Server converts cursor to SQL int; response row ids and
next_before_id are decimal strings to preserve 64-bit JavaScript precision.

Add async get_postmortem_history(db_path,limit=25,before_id=None) to dashboard/db.py.
Use existing _ro_db, BEGIN read transaction, fetch newest id/detected_at and COUNT,
then fetch the page in the same snapshot. Fixed SQL SELECT only
id,token_id,detected_at,run_pct,dropping_gate. Optional WHERE id < ?;
ORDER BY id DESC LIMIT ? bound to limit+1. No evidence selection, JSON functions,
imports from scout.db, writes, joins, migrations or feature-flag dependency.
Read transaction ends with connection close. A bounded page plus one COUNT over
existing tiny history is sufficient; no new indexes or cached counters.

Row shape: {id:string,token_id:string|null,detected_at:string|null,run_pct:number|null,field_unavailable_reasons:object,
most_frequent_recorded_block_reason:string|null}. run_pct accepts finite int/float
only, excluding bool; text/blob/nonfinite values become null. No recalculation or
joins to current prices. Return ids exactly with str(row['id']). Nullable reason
passes through; no reason is not a claim no blockage occurred. Text columns are
read through bounded SQL CASE expressions: only typeof(value)=text with length within token_id256, detected_at128, reason512 is returned; anomalous values return null plus a per-field unavailable reason (non_text or too_long). A four-bytes-per-character byte bound also prevents embedded NUL from bypassing SQLite character length limits. The same CASE and reason apply to latest_detected_at metadata. Never silently substring identity, reason, or timestamp. Normal values remain unchanged.

Envelope: {meta:{ok:true,read_only:true,historical_only:true,generated_at:string,
total_records:int,latest_detected_at:string|null,latest_detected_at_unavailable_reason:string|null,sort_policy:'id_desc',limit:int},
rows:[...],has_more:boolean,next_before_id:string|null}. latest_detected_at comes
from ORDER BY id DESC LIMIT1, never lexical MAX. Empty table returns200,total0,
latestnull,rows[],has_morefalse,cursornull. Page past end has total_records intact.
Pydantic response row/meta/envelope classes in dashboard/models.py provide schema;
JSONResponse or standard route response applies Cache-Control:no-store.

Error envelope503 with meta.ok=false, meta.read_only=true,
meta.historical_only=true, generated_at, data_missing_reason; rows[],has_morefalse,
next_before_idnull. FileNotFoundError maps database_unavailable; absent table or
required columns maps schema_unavailable; unexpected exceptions map query_failed.
Catch aiosqlite.OperationalError and recognize only SQLite missing-table/column
messages as schema absence; locked/corrupt/other errors remain query_failed.
Structured log postmortem_history_unavailable includes reason and exception type,
with server-side exception logging for unexpected failures. Public response never
contains exception text, paths or SQL. No missing table converted to successful
empty history. All outcomes no-store; unavailable includes Retry-After:60.

## UI

Create components/PostmortemHistoryTab.jsx; import/register/render in App.jsx's
Performance group as postmortem_history / Historical Postmortems. Do not edit
main.jsx. Reuse existing table classes; add scoped styles only if necessary.
No token links (chain is not known here), no actions on recorder/dispatch.

Persistent intro: Historical captures from open paper trades above the configured
run threshold at recording time. Not comprehensive missed-token coverage.
Recorded block frequency is not causal attribution.

Columns: Token; Captured at; Price change from paper entry; Most frequent recorded
pre-detection block reason. Price help text: Recorded change from the selected
most-recent open paper trade entry price to cached price at capture; not a24-hour
change or realized return. Null number displays Unavailable; null reason displays
No recorded reason only when the stored value is SQL NULL. A field unavailable reason renders Unavailable (too long/non-text), including anomalous token/capture/reason fields and latest capture metadata. Valid negative/zero values remain visible. Capture strings
render as stored timestamps; React escapes all text. Metadata: total stored
records and Capture time of newest recorded row, not a pipeline-health badge.

Page size25. Keep current cursor as null/string and previous cursor stack.
Previous/Next disabled during loading; Next disabled unless has_more. Refresh
resets stack/cursor to first page. Each load increments request sequence and uses
AbortController; commit success/error/finally state only for current request.
Cleanup aborts old request on unmount/page change. Loading clears old rows; failed
load displays unavailable and retry, never an empty-success message. Navigation
cursor state belongs to the request that initiated it, not response ordering.

## Verification and exact files

Endpoint tests in tests/test_postmortem_history_endpoint.py use minimal SQLite
fixtures and ASGITransport; no migrated full DB required. Assert:
-31 seeded records/page25 then6; descending ids, last cursor, final nullcursor;
-ids beyond2^53 retain exact decimal strings; signed64-bit cursor bounds422;
-insert a new row between pages; second-page old rows neither repeat nor skip;
-out-of-order detected_at proves latest metadata follows newest id;
-malformed/large evidence irrelevant; no evidence key; SQL trace proves column not read;
-empty200 distinct from missingDB/missingtable/missingcolumn503; unexpected query
failure sanitized and logged; Cache-Control on success/error;
-null/nonfinite/text run_pct normalize to null; negative/zero finite retained;
-DB content/schema preserved after request; direct _ro_db UPDATE attempt fails;
-limit1/100 boundaries, invalid0/101, invalidcursor0/negative/above64-bit bound;
-text containing markup appears as text; overlong/non-text token, reason, row timestamp and newest metadata timestamp return null plus correct unavailable reasons, never silent truncation. Malformed text decoding failures return sanitized query_failed503.

Frontend tests: add focused Python-driven Node tests using existing repo pattern,
extract only request/paging helper if required for executable async race tests;
otherwise use installed browser tooling for response-delay race proof. Require
loading/empty/error/pagination/reset behavior and stale request rejection, not
source substring checks alone. Extend existing tests/test_dashboard_nav_map.py
only where exact expected tab count requires it. Existing node/React packages
are sufficient; no new dependencies. Run focused API tests plus
 tests/test_dashboard_api.py and navigation tests, frontend npm run build,
git diff --check; record exact commands and actual results in final review doc.

## Release and rollback

No build starts until two design approvals and folds. Commit meaningful verified
changes; parent coordinates origin/master reconciliation afterPR575 before PR.
Two independent PR reviews/folds plus exact-head CI before merge. No deployment
while separate capture/migration owner is active. Later deployment rollback:
revert viewer commit, rebuild previous frontend; no data restoration required.
Broader capture coverage, recorder watchdog, T-minus guarantees and causality
remain open; list visibility cannot close those residuals.

## Design review

Structural design review approved. Operations requested explicit anomalous-field handling rather than silent truncation; folded above and operations reapproval received; build authorized by parent. Tests must reject stale success, error, and finally updates. No implementation files changed.


