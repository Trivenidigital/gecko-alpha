**New primitives introduced:** Read-only suppression-cohort health endpoint and small Pipeline panel, as approved; no storage, writer, policy or pricing primitive.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Suppression population and recorded-label counts | No project-specific substitute found | Reuse existing analyzer semantics and ledger |
| Bounded SQLite reader and React health panel | No applicable replacement identified | Reuse application libraries and panel conventions |

The approved plan records the 2026-09-14 drift-first check, loading-only https://hermes-agent.nousresearch.com/docs/skills/ response and accessible https://github.com/0xNyk/awesome-hermes-agent directory. Verdict: narrow in-tree integration, no new dependency; not an exhaustive catalog absence claim.

## Approval and runtime prerequisite evidence

Root structural and ops provenance reviewers independently APPROVED plan37752f0b5cb48c4c1cd07f02a78fd4fa5ec6e4ec without required folds. This is DESIGN ONLY; two design approvals are required before build. Worktree remains isolated from PR579 and selective release07af.

Disposable stdin probe at **2026-09-14T02:46:47.061622+00:00** used mode=ro, query_only, BEGIN, unconditional close, 10s per-query progress deadline,45s outer alarm,512MiB process cap. No remote files, initializer, network analyzer/send or DB writes. Artifact: C:/Users/srini/.codex/automations/gecko-overnight-autonomous-closeout/runtime-dash11-health-prerequisites-20260914.txt. Runtime HEADd2f0d61e; dashboard3409677 and pipeline3032091 active at /root/gecko-alpha. Analyzer, signals and outcome-labeler source have no delta between that baseline and this worktree's d7a0e267. This snapshot is not ongoing health or deployment proof.

| Observation | Value |
|---|---:|
| 7d ledger suppression / decision suppression |18450 /13650 |
| Ledger losers / chain / first |13650 /4487 /313 |
| Decision losers; other signals |13650; absent |
| 30d cohort rows / distinct tokens |147195 /2670 |
| Recorded r24h / r7d |85780 /31261 |
| Earliest-anchor rows / tokens / r7d available |2670 /2670 /41 |
| Status complete / partial / pending / unlabelable |89573 /222 /18228 /39172 |
| Broader30d gated_out rows / malformed verdicts |183312 /0 |
| All retained ledger / decision rows |573210 /1122173 |
| Table-wide unparseable / future timestamps |0 /0 in both tables |

All query outputs completed. Longest individual observation975.823ms; cohort EXPLAIN is full ledger scan plus temporary GROUP BY tree. These are baseline queries, not the proposed reader benchmark or an SLO. Ledger oldest2026-07-03; decisions oldest2026-07-31. Source config defaults decision retention45d (`scout/config.py:882`), hourly prune `scout/main.py:1865`; no matching .env override observed, but that does not independently attest every process environment override. Retained rows are the only denominator claimed. Existing producer mismatch and PR579 price-provenance limitations remain unresolved; no historical-price re-audit.

## Reader, work ownership and concrete bounds

Add dashboard/suppression_health.py with synchronous standard-library sqlite3 worker and pure bounded accumulators; execute via asyncio.to_thread. Capture DB absolute path locally when create_app is built. Do not import the CLI analyzer, Database.initialize, pipeline settings or trading policy. Add a no-parameter GET /api/suppression_cohort/health. The request cannot select windows, DB paths or limits.

Each app owns one active-work slot and retains its worker task until terminal. Check/acquire the slot synchronously on the event-loop thread before any await; if occupied return503 busy immediately, with no queued query. A dedicated async owner awaits the thread and releases the slot in its finally. Await this owner through asyncio.shield; cancellation of a client does not cancel the owner or release capacity while its thread still runs. Keep a strong reference until completion, consume detached exceptions, and retain read ownership during app shutdown. Tests must prove repeated canceled requests cannot start overlapping workers or leak task exceptions. No global cache, shared connection or cross-app state.

The worker owns connect through close in one thread using mode=ro, timeout0.25 and contextlib.closing/finally. It enables query_only then BEGIN, performs all reads on that snapshot, and closes on every exit. Before connect record monotonic deadline5s; SQLite progress handler every1000 VM operations interrupts past that deadline, and Python checks deadline before/after every fetch and each processed row. Connect/lock wait has250ms SQLite timeout; arbitrary filesystem stalls remain outside that guarantee and must not be described as interruptible. Cancellation before scheduled worker execution may avoid opening; once scheduled the owner must wait for closure rather than abandon acquisition.

Set SQLITE_LIMIT_LENGTH=65536. No SQL ORDER BY/GROUP BY/window function, temporary table or index creation: stream unsorted rows in batches128. At most250000 returned ledger rows plus50000 returned suppressed-decision rows,20000 distinct valid token identities,256 distinct signal keys, and16384 characters per gate-verdict value. Token/signal keys are at most128 characters. Reject the whole observation on any budget exceedance, oversized field or unsupported SQLite schema; never expose partial counts as complete. The dictionaries hold only counters and one small earliest-anchor tuple per token, not entire rows or verdict objects. The SQL VM deadline also bounds scans of rejected rows. Record elapsed query observation in private validation evidence, not a speed promise in the UI.

## Snapshot SQL and semantics

Take UTC observed_at immediately before BEGIN; derive fixed7d and30d starts. Require columns id,kind,token_id,surface,gate_verdicts,emitted_at,r24h,r7d,label_status plus decision signal_type,reason,created_at. No schema initialization. Bind timestamps; use julianday for inclusive-start/exclusive-end window membership, supporting existing offset/legacy SQLite timestamps consistently:

```sql
SELECT id,token_id,surface,gate_verdicts,emitted_at,
       r24h IS NOT NULL,r7d IS NOT NULL,label_status
FROM signal_outcome_ledger
WHERE kind='gated_out_sample'
  AND julianday(emitted_at)>=julianday(:lookback_start)
  AND julianday(emitted_at)<julianday(:observed_at);

SELECT signal_type,created_at FROM trade_decision_events
WHERE reason='suppressed'
  AND julianday(created_at)>=julianday(:window_start)
  AND julianday(created_at)<julianday(:observed_at);
```

Parse returned timestamps to UTC (legacy naive timestamps mean UTC, aware offsets normalized with overflow guard); accept only valid full date/time. Recheck membership in Python to avoid SQLite fractional precision becoming an incorrect boundary count. Unsupported selected timestamps increment excluded_timestamp_rows, separately for each scanned population. SQL-unparseable timestamps and future/outside-window rows are not selected; their counts are **not measured by this endpoint**. Metadata explicitly says table-wide timestamp diagnostics were not run. Do not copy the dated probe's zero into live responses or represent unparseable rows as a selected-window total. A failed timestamp parse cannot enter counts or anchor selection.

Parse JSON object verdicts only. Missing/null/nonobject/malformed JSON increments malformed_verdict_rows for the broader selected gated_out population. Require both exact tags reason=suppressed/source_layer=dispatcher. This preserves analyzer cohort semantics without evaluating cost. Show broader scan counts separately from classified suppression cohort counts.

For classified cohort rows, count total and raw label_status distribution (known complete/partial/pending/unlabelable plus unknown count), r24h/r7d non-null availability and valid distinct token identities. Nonempty TEXT identifiers within length limits are valid; null/blank/wrong-type identities contribute invalid_token_rows and remain in row counts but not distinct/anchor counts. No normalization aliases or trimming into another identity. Invalid/blank/wrong-type signal identities count under an explicit unknown-signal bucket, not a real named lane. Length-budget violations fail the whole read.

Maintain each valid token's earliest normalized emission using (timestamp,id) ordering, before inspecting r7d availability. Equal-time anchors choose smallest integer id deterministically; document this tie policy rather than relying on analyzer fetch-order accident. Count earliest_anchor_tokens_with_recorded_r7d, never matured/price-verified/eligible tokens. No return values are selected. Status complete remains only a stored status; it cannot substitute for any non-null availability counter.

For7d cohort rows accumulate counts by surface, alongside decision counts by exact signal_type. Emit the sorted union of signal keys as raw ledger_rows/decision_rows and state ledger_only,decision_only,ledger_excess,ledger_fewer or equal_counts. Equal counts means exactly that, not matched events. Aggregate coverage is always unknown; count ratios and coverage percentages are omitted. Producer coverage is unverified for the whole population even if no current excess appears.

## Response and UI

HTTP200 Cache-Control:no-store returns meta with ok=true, observed_at, both explicit start/end windows, retained_rows_only=true, timestamp_diagnostics_scope='selected_parse_exclusions_only', table_wide_timestamp_counts=null, coverage='unknown', provenance='unverified', conclusions='insufficient_evidence_for_cost_or_ranking'. Return population rows, broader_selected_gated_out_rows/malformed_verdict_rows, selected timestamp exclusions, cohort row/distinct/anchor/label counters and invalid identity counters. Never emit token IDs, raw verdicts, prices, returns, dollars, notional, rankings or recommendation fields. Success with zero classified rows has state insufficient_data. Nonempty state recorded_counts_only never upgrades provenance.

HTTP503 has ok=false, fixed reason busy/read_unavailable/schema_unavailable/read_limit, observed_at=null and no count payload. No SQL/path/exception strings. Do not reuse previous success as current after error. All budget, malformed schema, acquisition, lock and query failures resolve unavailable; CancelledError propagates at the route boundary while the shielded owner completes cleanup.

Place SuppressionCohortHealthPanel next to DispatchFunnelPanel in Pipeline. Fetch once on mount and on explicit Refresh; **no interval polling**, focus/resume fetch or automatic retry, given observed full scans. Disable Refresh during its one in-flight request; abort and invalidate generation on unmount or replacement. Client timeout7s; server remains capacity-owned until its5s worker closes. Old callbacks cannot publish after timeout/unmount/new generation. Display Loading, Unavailable with Retry, Insufficient data or recorded counts. Always display 'EXPERIMENTAL — recorded labels do not verify prices; insufficient evidence for cost or ranking.' Show both window dates, exact as-of and 'Last observed; refresh to update', never a current-green health indicator. Manual refresh shows loading and clears current counts; previous data may be omitted entirely.

Render a compact per-signal count table, separate row/unique-token/earliest-anchor metrics, raw label-status counts and r24h/r7d availability. Explain unmatched producer populations and unknown coverage. Do not hide unknown signal bucket or unlabelable/invalid exclusions. Use existing panel typography, wrapping/scrolling at narrow widths and accessible refresh/status labels. No navigation, ordering, score, trading action or other panel mutation.

## Discriminating verification and acceptance

- Real SQLite fixtures assert tag filtering, per-signal mismatch in both directions, duplicate tokens, earliest unresolved/later resolved, deterministic tie IDs, stored complete with nullr7d, invalid identities and unknown statuses. Different row insertion order must produce the same documented counts.
- UTC offsets/legacy full timestamps, exact boundaries, malformed selected timestamps and SQL-unparseable out-of-window rows prove scope labeling; no window count for unassignable timestamps. Regression pins that table-wide diagnostics are not executed by normal requests.
- Missing DB cannot create a file; missing schema, malformed/oversized input, all budget boundaries, actual SQL VM timeout and lock errors produce503 without partial success. Authorizer/fixture hashes prove no writes or initializer. Two app instances retain their own path.
- Barrier-controlled acquisition/read, repeated client cancellation and concurrent requests prove worker connection eventually closes, slot stays occupied, busy requests start no worker, and new request succeeds after completion. Normal tests retain repository conftest; no production database access.
- Concurrent fixture writer plus transaction barriers prove ledger/decision/label aggregates share one pinned snapshot. Pure accumulator slow-row falsifier must hit the Python deadline. No sorted/fetchall population accumulation.
- Existing analyzer tests remain unchanged. Node/controller and rendered tests exercise initial/manual fetch only, late completion, timeout/unmount, missing/error/empty/mismatch payloads and forbidden output fields. Browser fixture QA covers normal/mismatch/empty/unavailable/narrow-width states. Existing frontend build and dist parity apply.
- Before acceptance, run the actual candidate reader against a private production copy, compare its counts to independent pinned SQL for identical windows, record full completion and query time/memory/resource behavior. A5s refusal under actual volume requires query/scope redesign and renewed review, not increasing limits silently or adding an index. One successful probe does not establish concurrent production affordability.

No code, tests, build or production actions in this design commit. Two independent design verdicts precede implementation; two PR reviews and exact CI follow build. Root owns merge and any later deployment. PR579 price findings and release baselines remain untouched.
