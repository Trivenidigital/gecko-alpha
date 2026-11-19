# DASH-09 all-history summary verification

Implemented read-only GET /api/trading/stop-shortfall-summary and an independent
Trading-tab panel. All stored closed_sl paper rows use the unchanged existing
classifier. Mean/median pp are descriptive, unweighted per trade; PAPER /
EXPERIMENTAL, independent of table filters, not execution slippage or expectancy.
No trading, DB/schema/config changes, external messages or deployment.

## Review sequence

Two independent plan reviewers approved4085bf88 without required folds.
Two independent design reviewers approvede4f8ecbc without required folds.
Root authorized build only after both design terminal approvals. New PR reviews
and exact-candidate CI remain pending; these earlier approvals do not replace them.

## Verification and falsifiers

C:/projects/gecko-alpha/.venv/Scripts/python.exe -m pytest tests/test_stop_shortfall_summary.py tests/test_stop_shortfall_summary_frontend.py tests/test_stop_shortfall.py tests/test_trading_dashboard.py tests/test_dashboard_api.py tests/test_dashboard_frontend_layout.py -q --tb=short

Result190passed in15.13seconds. Initial endpoint tests failed404 before code;
frontend executable controller test failed missing module before code.

Coverage includes full-population parity across63rows with eligible rows beyond
first page, repeated-token observation grain, zero/n1/odd/even descriptive values,
empty versus unavailable and excluded evidence, immutable per-app DB targets,
read-only bytes preservation and actual write refusal, same-snapshot result across
concurrent mutation by a separate fixture connection, schema/cutover/oversized
inputs, population guard, actual long SQLite VM timeout, slow Python classifier
deadline, outer async timeout, repeated cancellation awaiting cleanup, and
successful fresh requests after errors. Existing classifier tests retain strict
numeric/provenance/partial/conviction exclusion rules. Classifier file has no diff.

A cancellation falsifier initially FAILED: cursor.close queued while a long SQL
fetch remained active, delaying interruption until the3second deadline. Fixed by
letting error/cancellation unwind immediately to outer interrupt+awaited connection
cleanup. The test now demonstrates canceled fetch completes within0.5seconds and
repeated cancellation cannot abandon held cleanup. SQLite and Python guards are
independently tested; no assumption that an asyncio timeout kills worker SQL.

Node executes actual frontend request controller: stale success/error/finally
rejected; aborted/unmounted requests do not publish; refresh/error clear numbers;
empty/no-eligible/zero display distinctions and fixed filter-independent URL.

npm --prefix dashboard/frontend ci --ignore-scripts and npm --prefix
dashboard/frontend run build succeeded (Vite6.4.1,76modules). Generated distribution
included. Black formatted touched Python; final --check and diff check required
before publication. No dependency changes.

Synthetic live browser QA of actual component at localhost8919 confirmed readable
headers,1.35pp mean,1.13pp median,23/355counts, modeled/unavailable/exclusion reasons,
PAPER/EXPERIMENTAL and scope/caveat copy. Empty and no-eligible display null statistics
as Unavailable; error displays Retry without counts/old statistics. This is
component/fixture QA, not full deployed application evidence. Temporary fixture,
server and browser tab removed/stopped. Fixture-only import path error corrected;
no product defect inferred from that preview setup issue.

## Runtime basis and deployment boundary

See design for fresh root-provided00:28:58UTC pinned read-only cohort/schema/hash
probe.355stops,23eligible,mean1.3471808080779946pp,median1.1309679311149452pp;
classifier hash matches local; no missing columns/duplicate snapshot keys.
Historical eligible closesJuly10-Aug9; zero preceding30day closes.17.931ms is one
probe, not an SLO. Root subsequently verified dashboard WorkingDirectory
/root/gecko-alpha and checkoutd2f0d61e at00:36UTC. This candidate not deployed.

Root owns serial base integration after other viewer PRs; generated bundle must be
rebuilt if those merges affect frontend. Root must obtain two PRreviews, clear real
findings, exact-head CI, then coordinate any authorized merge/deploy. No merge or
deploy performed here. Smoke after eventual authorized deployment: GET summary,
compare complete cohort with equivalent read-only snapshot, GET existing history.
Rollback new source/component/dist to prior approved revision; no DB restore.

Bounds are cooperative under a functioning scheduler/local filesystem:3second work
budget,5second outer async timeout,100ms SQLite busy timeout,VM progress callback,
per-row checks/yield every128rows,50000row fail-closed cap and4096byte-cell guard.
They do not promise preemption of an OS filesystem stall. Oversized/missing evidence
and timeout return explicit503, never partial/zero success. No ranking, pruning,
capital, dispatch, source or strategy changes. Next action: independent PRreviews.

## PR580 review fixes

Logic review found acquisition cancellation could abandon sqlite3.connect's newly opened native connection before _ro_db received it. Two real connector-barrier regressions failed (repeated cancellation and outer timeout); the adapter now owns/shields acquisition and awaits its eventual result before interrupt/close. Both tests prove the request waits for connector release and TrackedConnection.close is called, then a fresh request works. Shared _ro_db and classifier unchanged. Another failing fixture proved an aware cutover whose UTC conversion overflows returned200; validation now normalizes to UTC and returns503 cutover_unavailable. Summary focused tests20passed after fixes. Root's prior actual-candidate00:46:18UTC read-only probe matched355/23/4/328 and mean/median (9.454ms single observation); it predates these fixes and is not re-attested here. Renewed two-vector review required.

After both review fixes, the same complete focused command above passed193tests in20.52seconds. Black and diff checks passed; frontend source/dist unchanged, so no rebuild needed for these Python-only fixes.

## Integration 2026-09-14
Root integrated master f55271fc after PR578 merged with7964tests12skipped118contracts. API insertion conflict resolved by retaining both unchanged summary and postmortem handlers; Black wraps one inherited Telegram JSONResponse call only. Generated frontend rebuilt from combined source,80modules. Combined summary/classifier/trading/API/layout/postmortem/Telegram/navigation suite229passed. No classifier/shared_ro_db/core change versus master. Stale575/577/578 checklist gates reconciled as merged, not deployed. Both new review fixes atd78b2ba1 were independently re-approved after193tests; fresh integrated-candidate reviews and final CI still required.

Both integration reviewers approved ddf5d6cb3ba956d45d30109a4ce46d9769406f4f: logic_review (logic/concurrency) and ops_review (ops-safety/silent-failure). Each independently reran 98 combined tests; prior 193-test substantive reviews retained only after confirming unchanged summary logic. No outstanding findings. Clearance record anchors this reviewed integration; final metadata-head CI remains required. The separate selective-release plan/design approvals do not establish deployment.
