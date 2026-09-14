# Suppression cohort health — build checkpoint, acceptance held

The health-only residual exists, but this implementation is **not ready for publication or deployment**. Semantic parity and fixed resource refusal work; practical runtime margin remains insufficient on the owned production copy. Frontend work is unfinished and paused. No PR, merge, schema/index changes, policy mutation or deployment has occurred.

## Review and build evidence

- Plan37752f0b received independent root structural and ops provenance approvals.
- Design259c6f28 received independent root semantic/structural and ops resource/provenance approvals.
- Initial reader f46fa5a2 passed22 focused tests including15 unchanged analyzer tests, but actual-copy execution hit the5s deadline during the decision query. Lightweight exception-stage diagnostics showed183073 broader rows/147018 cohort rows/2664 anchors processed and0 decision rows; SQLite OperationalError, approximately26MiB RSS. No bounds were raised.
- First resource amendment f6989b76 received root semantic and ops bounds approvals. Decision index correction f921e833 passed semantic parity; VDBE demonstrates predicate evaluation on index fields before main-row access. Full-reader observations3.785s,4.750s,1.372s,0.950s left only0.250s worst margin. Both reviewers held affordability.
- Second resource amendment b8e02a5b received root semantic and ops bounds approvals. Ledger index correction f2f50c4d preserves the same date/tag/status/anchor semantics; no status filter. VDBE index timestamp checks at5-24 precede main-table kind25 and row fields28 onward. Three complete-reader runs took **4.691s,1.007s,0.949s**, with peak RSS27892KiB. Worst margin remains0.309s/6.2%; this does not materially resolve the held gate. Source SHA2564886230c657a1966e3dd4fa664928a8894079e43cae67368af19a67f174aa8d7.

`C:/projects/gecko-alpha/.venv/Scripts/python.exe -m pytest tests/test_suppression_health.py -q --tb=short` — **21 passed** at f2f50c4d. Coverage includes missing/wrong/partial indexes, status-diverse cohort and earliest-anchor ordering, reason-only decisions across multiple decision values, actual VM interruption, slow Python deadline, population limits, real acquisition cancellation with eventual Windows handle closure, per-app path isolation and pinned snapshot under concurrent fixture writes. Test fixtures explicitly own and close sqlite3 connections; their transaction context manager alone is not a close guarantee. The anchor-ID malformed-schema test currently reaches schema refusal after replacing a table with a view; strengthening it to isolate ID validation remains a pre-publication test follow-up if build resumes.

Two production-controller Node tests passed during initial UI work. They are not complete frontend/render/visual QA. No frontend build or generated dist has been produced for this task. The worktree retains explicit uncommitted UI work; do not treat it as a candidate to merge.

## Copy and comparison scope

Parent authorized one private copy at /root/gecko-dash11-health-validation-20260914/baseline.sqlite,2830766080bytes. Source and destination SHA256 both a0b1c9a7238ff18480060968430f6786b29875fbd1babaae3e5ddaec7d0a54e4. The source was the already closed earlier selective-release backup; the release baseline was never connected, deleted or modified. Candidate and independent SQL connected only to the owned copy in mode=ro/query_only; source text was executed in memory from stdin. Before/after main-file SHA matched. The probe's512MiB process cap, no socket/subprocess audit guard and60s outer limit applied to validation; candidate retained its own5s/row/cardinality limits.

Comparison as-of2026-09-14T02:46:47.061622Z uses7d population and30d lookback. This is an **older backup population**, not live counts at that time. Same-copy independent SQL agrees on147018 cohort rows,2664 distinct tokens,85585 recorded r24h,31206 recorded r7d and41 earliest anchors with recorded r7d; labels89393 complete/193 partial/18301 pending/39131 unlabelable. Broad selected gated-out183073, malformed0, invalid token0; selected parse exclusions0, independently checked against the copy's valid timestamp population. Population rows: chain4452/0,first313/0,losers13508/13508 ledger/decision. Coverage and price provenance remain unknown/unverified regardless of these counts.

Artifacts under C:/Users/srini/.codex/automations/gecko-overnight-autonomous-closeout/:

- runtime-dash11-health-prerequisites-20260914.txt — separate live read-only snapshot.
- runtime-dash11-copy-20260914.txt — source/destination hashes and2.7GiB footprint.
- runtime-dash11-copy-refusal-20260914.txt — initial actual deadline/stage evidence.
- runtime-dash11-copy-full-validation-20260914.txt — first amendment parity/timing/VDBE.
- runtime-dash11-ledger-vdbe-20260914.txt — second amendment predicate ordering.
- runtime-dash11-ledger-full-validation-20260914.txt — all-field parity, three runs, source/copy hashes.

No cache flushing, host-load changes, new indexes, retention changes, suppressed decision-value filter, lexical timestamp restriction or limit widening was used. Faster subsequent observations are retained but cannot replace the worst run. A further scope/query redesign requires review before implementation; otherwise retain this as a findings-only engineering pause. Root owns that disposition and any later publication. PR579 and frozen release07af remain untouched.
