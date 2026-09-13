# Telegram recorded-outcome visibility design

**New primitives introduced:** a read-only ledger aggregate, GET route, and compact panel using the existing Telegram Dispatch Feedback section.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Gecko audit outcome and literal prefix taxonomy | None found in inspected ecosystem directory; Hub catalog unavailable | Implement the Gecko-specific read adapter |
| Read-only dashboard and UI lifecycle | Existing _ro_db / FastAPI / React | Reuse these primitives without new dependencies |

Checked https://hermes-agent.nousresearch.com/docs/skills/ and https://github.com/0xNyk/awesome-hermes-agent on 2026-09-13. The Hub loading shell prevents an exhaustive catalog search; the directory's generic surfaces do not supply this ledger's semantics. Verdict: no Hermes orchestration replacement.

## Approval, source and runtime

Both parallel plan reviewers approved 83f589b5. Scope is ALR-07 descriptive visibility, not changed alert eligibility. Source writes two literal prefixes to tg_alert_log while existing /api/tg_alerts/recent selects sent only. Runtime at23:10Z: 1,654 detection-universe / four paper-open universe / 328 other-or-unspecified eligibility events; 436/0/0 respectively in30days. Fresh schema and totals are in the parent automation's alert-outcome-runtime.txt and alert-prefix-runtime.txt. The ledger contains events even though open paper trades are zero; no forward-soak required to display historical counts. Runtime delivery health is not established by these rows.

## Query and API

New dashboard/telegram_outcomes.py imports only _ro_db from dashboard.db, datetime, aiosqlite and structlog. It never imports scout.db or initializes/migrates a DB. Public async get_telegram_outcomes(db_path:str,days:int=1)->dict validates1..30, freezes datetime.now(timezone.utc), computes window_start, and opens one readonly connection with BEGIN. A single aggregate SQL statement uses bound window_start/as_of parameters, julianday(alerted_at), and CASE SUMs with COALESCE(…,0). No raw string from a row reaches JSON; output strings are fixed constants and server-generated ISO timestamps.

Known outcome buckets: sent, blocked_eligibility, blocked_cooldown, blocked_dedup_24h, dispatch_failed, announcement_sent, m1_5c_announcement_sent. SQL CASE ELSE groups unfamiliar or NULL outcomes into other_recorded_outcomes; NULL must not fall through SQL NOT IN's three-valued logic. Eligibility uses GLOB 'universe_filter:*', GLOB 'detection_lane:universe_filter:*', ELSE other_or_unspecified; NULL/nontext/unfamiliar details stay in that residual. Restrict all three to blocked_eligibility. Every bucket is always returned; no result ordering depends on counts.

Window is inclusive [start,as_of] at SQLite julianday precision (approximately milliseconds). Timezone offsets normalize through SQLite. Parseable timezone-naive timestamps are interpreted as UTC by SQLite and documented as such; no stronger producer guarantee is implied. Unparseable timestamps are excluded and counted in table_wide_invalid_timestamp_count. Timestamps greater than as_of are excluded and counted in table_wide_future_timestamp_count. Those counters are table-wide, never represented as in-window exclusions. No lexical timestamp comparisons.

Envelope: meta {ok:true,read_only:true,historical_only:true,generated_at,window_start,as_of,days,time_policy:'sqlite_julianday_inclusive_naive_utc',table_wide_invalid_timestamp_count,table_wide_future_timestamp_count}; total_events:int; outcomes:{fixed bucket:int}; blocked_eligibility:{paper_open_universe:int,detection_universe:int,other_or_unspecified:int}. Outcome sum equals total_events; eligibility sum equals outcomes.blocked_eligibility. No ratios, unique-token estimates, raw reasons or causal interpretations. Empty table/window returns success zero.

GET /api/tg_alerts/outcomes?days=1 validates Query ge1 le30. Route calls the helper using create_app's captured db_path, returns JSONResponse with Cache-Control:no-store. Missing file ->503 meta.okfalse/data_missing_reason database_unavailable; missing table/required column ->schema_unavailable; other exceptions ->query_failed. Unavailable envelope has no numeric counts (null, not zeros), Retry-After:60. Server logs structured telegram_outcomes_unavailable with reason/type; unexpected errors use exception logging, never public SQL/path/exception text. Do not invoke _get_scout_db. Route location precedes dynamic tg_alerts routes, though existing dynamic route includes an additional operator-action suffix.

## UI lifecycle and semantics

New components/TelegramOutcomePanel.jsx is rendered within existing subTab==='dispatch' alongside TGDispatchFeedbackPanel, independent of the parent poll's data/error; no App.jsx or subtab-map changes. It is mounted only when that section is selected. Render heading Recorded Telegram outcomes,1/7/30 day buttons,Refresh,exact window and total event count, fixed-order outcome list and eligibility subtable. A visible caveat states counts are recorded events, not unique tokens; recorded sent is not independent delivery confirmation. Other/unspecified eligibility is not proven ineligible. Table-wide timestamp exclusion counts are explicitly labeled and visible when nonzero. Empty window says No recorded events in this window; an unavailable request says Counts unavailable with Retry. Do not reuse unknown counts as zero.

A small component-owned request helper in telegramOutcomeRequest.js takes fetch and state callback dependencies for executable tests. Each load aborts old request, increments identity and emits loading with prior payload cleared. Success, catch and finally emit only if identity still current and not disposed. dispose invalidates identity and aborts. Component creates helper in useEffect, calls load for days, and cleanup disposes; manual refresh triggers same load for current days. This is local to this consumer; no global fetch abstraction. Require HTTP success AND meta.ok===true before rendering counts. Server error details are a fixed generic user message, not raw response text. Buttons allow changing window to exercise cancellation; repeated refresh supersedes prior request. Existing parent polling stays unchanged.

## Verification

New tests/test_telegram_outcomes_endpoint.py: red404 before implementation; minimal SQLite fixtures, ASGITransport, no initialize call. Verify all outcome/eligibility buckets and sums, both exact prefixes vs similar/case-different prefixes, NULL/unfamiliar outcomes/details, offset/naive timestamps and exact inclusive boundaries, invalid/future timestamps,1/30 bounds and invalid inputs, empty existing vs missing DB/table/column/corrupt query errors, sanitized logged failures, no-store and retry headers, no raw payload, unchanged DB bytes/schema and mode-ro rejection. Freeze helper clock for deterministic tests. Add a same-snapshot probe if multiple SQL statements become necessary; design uses one aggregate statement.

Executable Node helper tests (called by pytest, skip only when node absent) simulate out-of-order success/error/finally, unavailable meta with HTTP200, HTTP503,network error,reset loading and dispose. Existing TG frontend guards verify section remains unchanged; build proves JSX integration. Browser visual smoke uses a local fixture with complete dashboard schema, or a standalone fixture rendering this component to avoid unrelated websocket missing-schema loops. Run targeted new tests, tests/test_dashboard_tg_tab_frontend.py, tests/test_dashboard_funnel_and_outcomes.py, tests/test_dashboard_api.py, npm run build and git diff --check.

## Integration and release

Root branch feat/telegram-outcome-visibility-20260913 owns these files. Child postmortem branch is separate; merges stay serial and api.py import/route conflicts must preserve both. Two parallel design approvals before tests/implementation; then PR and two parallel final reviews/folds, exact-head CI before authorized merge. Record reviewers at the code head and rerun base-integration checks when master advances. No production deployment while capture owner controls migration rollout. Later rollback is a normal viewer revert/rebuild, no DB restore. No outgoing messages, writer/schema/config/execution changes.

## Review

Awaiting two independent design reviews. No implementation started.
