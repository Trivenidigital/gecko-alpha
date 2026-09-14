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
  AND julianday(emitted_at)>=julianday(:lookback_start)-1.0/86400
  AND julianday(emitted_at)<julianday(:observed_at)+1.0/86400;

SELECT signal_type,created_at FROM trade_decision_events
WHERE reason='suppressed'
  AND julianday(created_at)>=julianday(:window_start)-1.0/86400
  AND julianday(created_at)<julianday(:observed_at)+1.0/86400;
```

Parse returned timestamps to UTC (legacy naive timestamps mean UTC, aware offsets normalized with overflow guard); accept only valid full date/time. SQL deliberately overfetches one second at each edge to cover SQLite fractional precision rounding; enforce the exact inclusive-start/exclusive-end membership in Python before any count, so rounding cannot exclude an otherwise valid boundary row. Overfetched valid rows outside the exact interval are discarded, not counted as malformed. Unsupported selected timestamps increment excluded_timestamp_rows, separately for each scanned population. SQL-unparseable timestamps and future/outside-window rows are not selected; their counts are **not measured by this endpoint**. Metadata explicitly says table-wide timestamp diagnostics were not run. Do not copy the dated probe's zero into live responses or represent unparseable rows as a selected-window total. A failed timestamp parse cannot enter counts or anchor selection.

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

## Resource refusal amendment — requires two renewed design reviews

The initial build slice f46fa5a2 passed22 focused tests but **failed** the approved actual-copy affordability gate. Build is paused; the implementation query is unchanged pending this amendment's two reviews. In the private copy at /root/gecko-dash11-health-validation-20260914/baseline.sqlite (one2.7GiB copy, source and destination SHA256 a0b1c9a7238ff18480060968430f6786b29875fbd1babaae3e5ddaec7d0a54e4), the candidate repeatedly returned unavailable/read_limit at5.001s with26MiB RSS. This is the closed earlier backup, not a fresh live population: exact comparison as-of remains2026-09-14T02:46:47.061622Z, but the copy cannot contain events after its earlier backup snapshot. The release baseline was never connected or modified.

A disposable wrapper inspected only exception type and numeric accumulator sizes at refusal, with no per-row tracing: SQLite OperationalError at5.00082s,183073 broader rows processed,147018 cohort rows,2664 token anchors,3 sampled lanes,0 decision rows. This establishes deadline interruption during decision-query work, not a cardinality failure. Current EXPLAIN reports SCAN trade_decision_events. Independent same-copy SQL at identical windows returned13508 losers decisions and147018 cohort rows/2664 distinct tokens/85585 r24h/31206 r7d. Individual independent observations were2.337s and5.244s; those SQLs are different from the streaming reader and are not its performance proof. Artifacts: runtime-dash11-copy-refusal-20260914.txt and runtime-dash11-copy-sql-20260914.txt in the automation directory.

Proposed narrow change after renewed approvals: add **INDEXED BY idx_tde_decision_reason_created** to the existing decision SELECT. Preserve reason-only cohort semantics, the same timestamp overfetch/exact checks, row limits and5s total reader deadline; never add a decision-value predicate to make the index fit. The existing index contains decision,reason,created_at and implicit rowid. Its narrower pages can evaluate reason/timestamp predicates before looking up signal_type in the main table. Confirm that exact evaluation order with EXPLAIN/VDBE; a mere index name in EXPLAIN is insufficient. No new index, schema/retention change, materialized CTE, sort or temporary table is proposed.

Before executing that query, require PRAGMA index_list/index_info evidence that the named existing index is nonpartial and has exactly the ordered columns decision,reason,created_at. Absent or wrong definition yields schema_unavailable, not a silent fallback to the full table scan. This is a newly explicit supported-schema precondition; test missing, reordered and partial same-name indexes. Existing runtime schema proves this index exists in the production snapshot. Fixture schemas must model that existing production index, without creating it in application code.

After approvals, use TDD for wrong-index refusal and cohort equality with suppressed rows across multiple decision values. Inspect real VDBE for predicates preceding main-table column access, then execute the exact revised complete reader against this same owned copy. Independent same-copy SQL must agree on every cohort/label/anchor/population/diagnostic field, with retained-copy age stated. Record total elapsed time and margin below5s on bounded repeated trials; if it still refuses or has no practical margin, stop again for redesign rather than widening a limit. No release/production action follows from a successful benchmark. Resume remaining frontend verification only after this resource gate is resolved.

## Second resource amendment — ledger access path, DESIGN ONLY

Root semantic and ops bounds reviewers approved amendment f6989b76 before implementation. Decision-index correction f921e833 preserves the complete cohort and source hash c21016816662b24a9ebbd01d89726d1afbf5c259b15e8c94b6f1332093a0f791. Its actual-copy parity succeeded, and VDBE confirms index-only reason comparison at5/6 and timestamp comparisons through26 before main-table signal_type Column27 (DeferredSeek4 does not fetch the table row yet). However full-reader observations were3.785s followed by4.750s,1.372s,0.950s. Worst margin is only0.250s/5%; root and ops explicitly HOLD practical affordability. Faster subsequent reads do not erase the worst observation. No cache drop, workload manipulation or limit change was performed. See runtime-dash11-copy-index-validation-20260914.txt and runtime-dash11-copy-full-validation-20260914.txt.17 reader tests currently pass, including actual acquired-connection cancellation/Windows handle closure and concurrent snapshot writer coverage; tests do not resolve runtime headroom.

Propose a second access-path-only change after **two renewed approvals**: ledger SELECT uses existing **INDEXED BY idx_sol_status_emitted**, with the unchanged julianday one-second overfetch predicates written before kind='gated_out_sample'. The index columns are label_status,emitted_at. No label_status filter is added: pending/partial/complete/unlabelable and unexpected statuses remain equally in scope. No lexical timestamp envelope, source-layer shortcut, omitted malformed JSON, changed returned column set, order, group, temp table, materialization or anchor policy. Parse and exact-filter in Python exactly as before.

Attest idx_sol_status_emitted via index_list/index_info as a nonpartial index with exactly ordered columns label_status,emitted_at, using the same bounded schema-inspection pattern as the decision index. Missing, reversed or partial same-name index returns schema_unavailable with no fallback or index creation. Test all three and status-diverse cohorts. Budget checks continue to cover schema inspection, both SQL scans and all Python processing; the5s total/250000 ledger/50000 decision/20000 token/256 signal limits remain fixed.

The hypothesis is that narrower index pages can evaluate timestamp predicates before fetching wide ledger rows outside the lookback. It is **unproven**: eligible-row locality and page-cache state may erase any advantage. Verify real SQLite VDBE actually checks both date bounds before a main-table kind or other column read; textual WHERE order and an index-named EXPLAIN alone are not proof. If compilation violates that order, return to review rather than silently rewriting the query again.

After approvals, rerun the exact full candidate reader on the same owned copy and windows, with independent all-field SQL parity and before/after file identity. Record each complete run, peak process memory and worst margin below5s, including the first observation; no cherry-picking warmed runs or host cache changes. Plan a bounded sequence of three full reads under ordinary host conditions. Affordability remains held until root/ops judge the complete evidence sufficient; timeout or narrow margin requires another redesign or findings-only stop. Do not increase deadlines or row bounds to manufacture success. Resume frontend only after that gate passes; no merge or deploy in this task.

## Final scope amendment — fourteen-day recorded-label window, DESIGN ONLY

The second index amendment passed semantic parity but did not resolve practical margin: three complete-reader observations4.691s/1.007s/0.949s; worst0.309s below the5s limit. Preserve those results as a failed acceptance gate. Parent authorized one distinct scope revision for review, not repeated window tuning until green: retain the **7-day population** window and reduce the **recorded-label lookback to14 days**. If the reviewed14d complete-reader gate fails or retains narrow margin, stop this run with findings only. No deadline/cap/index/schema changes and no further window reduction.

Bounded independent read-only same-copy SQL, as-of2026-09-14T02:46:47.061622Z and lookback start2026-08-31T02:46:47.061622Z, returned48027 broader selected gated-out rows,30977 classified suppression rows,1517 distinct tokens,8235 recorded r24h and4804 recorded r7d. Stored statuses:8641 complete/193 partial/18301 pending/3842 unlabelable. Earliest14d anchors1517, with50 recorded r7d. Individual aggregate observations0.056–0.132s; these are not candidate performance proof. Artifact runtime-dash11-14d-scope-counts-20260914.txt. Probe used the already owned earlier copy only, mode=ro/query_only/BEGIN,7s query progress deadline/30s outer alarm/512MiB limit and unconditional close; no initializer or writes.

Compared with the30d copy cohort, broader returned volume falls183073→48027 (about74% fewer), and classified rows147018→30977. The endpoint still reports only retained rows; it loses visibility into the previous16 days of the former lookback, including older stored labels and unresolved records. It cannot answer30d or lifetime health. The14d span permits some observations to have seven elapsed days, but elapsed time neither guarantees labels nor validates provenance. Recorded r7d availability and raw status remain separate;8641 complete is not4804 r7d. Earliest-anchor selection is recomputed within14d before checking availability, so50 versus the prior30d41 is **not an improvement trend** or nested-cohort comparison: window-relative anchors can change for the same token. Do not display cross-window comparisons or claim maturity improvement.

After two approvals, change only the fixed label-lookback constant from30 to14 days and its explicit response metadata/UI wording, fixtures and documentation. Include numeric window_days=7 and lookback_days=14 alongside start/end timestamps so consumer tests pin the contract. The7d population, two-tag cohort, malformed/unknown diagnostics, index attestations, supported timestamps,1s SQL overfetch/exact Python boundary checks, earliest(timestamp,id) policy, non-null availability definitions and every resource limit stay unchanged. All SQL grouping/sampling prohibitions and no-cost/no-ranking/provenance-unverified rules remain in force. The earlier plan's30d assumption is superseded only by this approved scope section; preserve the original runtime evidence rather than rewrite history.

Discriminating tests must show a row15 days old is outside label counts while a row exactly14 days old is included, rows in the first7 days of the14d lookback contribute to labels but not7d population, and choosing an earlier unresolved row outside14d does not replace the earliest in-window anchor. Add explicit offset/boundary cases under14d and pin both numeric windows and UI dates. Re-run all reader tests and the unchanged analyzer suite; the CLI's120d default and behavior remain untouched.

Before any frontend continuation, execute the exact revised complete reader on the same owned copy/windows, verify all aggregate fields against independent SQL, and record three ordinary-condition runs including first/worst with the existing5s cap. Root and ops must judge practical margin; no warmed-only selection, cache drop, load manipulation, copy replacement or limit widening. The volume reduction is a rationale for trying this bounded alternative, not an acceptance result. No code change is authorized by writing this amendment; two renewed design approvals come first.
