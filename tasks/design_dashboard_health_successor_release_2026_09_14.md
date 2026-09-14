# Successor selective dashboard release design

**New primitives introduced:** NONE. Extend the existing disposable manifest and rollout helper; retain the pinned copy validator and existing service. DESIGN ONLY after two plan approvals at5e249634a36218dca7ce3f91c1dd38e395e1576a.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Selective source identity | No Gecko-specific primitive in prior accessible same-run checks | Extend tasks/release_dashboard_manifest.py |
| Read-only copy verification and rollback | Generic orchestration only | Reuse existing pinned validator and guarded service helper |

Drift findings and actual prior Hub/awesome-hermes-agent checks are recorded in the approved successor plan and original release plan. Hub returned a loading shell; no exhaustive negative claim is made. Verdict: no new architecture, dependency or deployment platform.

## Fixed identities and roles

| Identity | Value and purpose |
|---|---|
| CORE_BASE | d2f0d61edc63cb55ae159ec952cce404991f21f5, immutable core/consumer reference |
| RUNTIME_BASE | 5c43526c46a33b06e7ed18509c8a0ea43c7772d3, deployed release and rollback HEAD |
| ROLLBACK_BRANCH | docs/dashboard-release-retry-20260914, must resolve exactly to RUNTIME_BASE |
| Candidate branch | docs/dashboard-health-release-plan-20260914, candidate SHA unknown until assembly |
| Final feature master | Fresh immutable origin/master after585 merges, not yet available |
| PR584 |1967cbfc42185fe4fb74d527bd0af9929375dae0 |
| PR585 | Actual squash SHA after merged exact CI; never661020e8 intermediate |

Root's04:01:39Z success and independent04:02:53Z production review establish the planned starting state. Fresh runtime preflight must still verify it. No PID or live policy state is inferred from source. Original07af baseline remains the immutable DB reference, separate from source CORE_BASE and RUNTIME_BASE.

## Manifest and assembly

Keep production_base_sha and baseline_files with their existing meaning: d2 core source and its full tree. Do NOT replace them with5c; ancestry checks d2→final master and selected squash→final master remain valid. Add explicit runtime_base_sha, rollback_branch, runtime_baseline_files fields. runtime_baseline_files is the full tree of pinned5c, including its release metadata; obtain it directly from its Git object. Validate that5c matches the previously verified five-PR manifest, including its core and existing viewer/WS source. The5c release branch is not presumed an ancestor of master.

Extend the manifest's exact selected PR set from five to seven575/577/578/580/583/584/585. Reject missing/extra IDs, non-squash/intermediate commits and non-ancestor feature commits. Use the same selected dashboard/tests path union and final master blobs/modes. Attest all1358 previously protected core files unchanged against d2 and all five operational consumer ASTs unchanged against d2,5c and successor. Newly selected files cannot silently remove an old protected core file from that explicit prior set.

Add only exact successor plan/design/report metadata filenames to the explicit metadata allowlist; the current helper accepts selective-release filename prefixes, not these health-successor names. Do not introduce a broad tasks wildcard. Keep current release metadata explicit. Selected frontend source and actually referenced index/assets must equal final merged master; rebuild to a private output directory and compare. Historical unreferenced artifacts may remain only as allowed by the exact manifest. If fresh master introduces unrelated selected-path changes, inspect and resolve before assembly; no automatic whole-master deploy.

Assemble only after585 merge and two design approvals, in this successor worktree descending5c. Overlay selected final merged blobs; preserve nonselected files and modes. Record5c→candidate diff and module hashes. Health reader hash0004d187920c40a7ebff98862f86aee341111afb310c34a987dd6401091234e5 and lane/controller/source identities are checked against their actual final merged versions. API integration remains exactly merged app-local paths, HealthReader lifetime/drain and two additive routes. No source adaptation to old core is allowed without renewed review.

## Helper audit and exact adaptation

Preserve the completed deployed helper as immutable evidence before making a successor copy. The currently reviewed timestamp-amended helper is the reuse point; hash it at implementation start and retain its49-test suite. Root owns the original automation helper; implementation creates a separately named successor helper until final approval, never overwrites the active release evidence.

| Existing dependency | Successor behavior |
|---|---|
| BASE constant | Split explicit CORE_BASE and RUNTIME_BASE; no global replacement |
| historical_preconditions (HEAD/master) | Require RUNTIME_BASE and exact ROLLBACK_BRANCH; keep config/pipeline hard pins unchanged |
| snapshot | Record runtime HEAD and branch alongside existing full policy/config/schema/process facts; reject missing/mismatched identities |
| preflight manifest identity | production_base_sha==CORE_BASE, runtime_base_sha==RUNTIME_BASE, rollback_branch exact, release_sha candidate, verified true |
| preflight checkout attestation | Attest runtime_baseline_files, NOT d2 baseline_files; require clean5c checkout and rollback ref fixed before stop |
| candidate Git object | Existing exact expected_files plus explicit metadata comparison remains; immutable core proof rechecked |
| refs | Candidate local and published remote refs equal candidate; rollback local and published refs equal5c, recheck local immediately before rollback |
| rollback switch | Switch exact ROLLBACK_BRANCH; require HEAD5c, clean tracked tree and runtime_baseline_files; log5c |
| smoke asset mapping | Candidate uses expected_files; rollback uses runtime_baseline_files |
| DB VERIFIED_COPY | Original immutable07af baseline path and pending-WAL/hash checks unchanged |
| policy snapshots | Same-transaction full+compatibility observations; only initial exact combo.last_refreshed exception; FULL separate copy/live pins thereafter |
| disk, quiescence, lock, dependencies, pipeline | Preserve existing12GiB gate and all reviewed checks unchanged |

Mock tests must fail for accidental master rollback, runtime tree attested against d2, core manifest repointed to5c, missing runtime fields, drifted rollback ref, candidate ref drift and skipped full timestamp checks. Exercise successful5c→successor and failure at stop/switch/start/smoke/terminal-log with5c restoration, without any real services/Git/network. Retain all existing stop PID/job/cgroup/lock, lazy initializer, signal and log failure tests.

Both candidate and rollback5c support existing viewers. Refactor the existing candidate-only viewer smoke body into shared checks for history enrichment, postmortems, Telegram and all-stored stop summary;5c rollback must exercise them too. Candidate alone adds lane/health checks. Preserve full history SQL identity and other existing invariants. No assumption that rollback lacks viewers.

## Copy reuse and disk accounting

At planning observation host free space was14454181888 bytes, about13.46GiB. A new pair costs roughly5661532160 bytes before WAL/temp/env/logs, and even one fresh copy would breach12GiB. No pair allocation is permitted on that budget. Original07af baseline stays intact. Root reports the now-superseded5c pair baseline.sqlite/candidate-init.sqlite remains present; reverify exact paths, private ownership/mode, no symlinks/open writers/pending WAL, complete file hashes and prior proof artifacts before reuse.

Preferred bounded path: losslessly archive each explicitly owned5c pair member and its original proof metadata, verify decompressed whole-file hash, then reuse that same reviewed pair IN PLACE for successor validation with recorded prehashes. This avoids allocating another5.66GB. New source export lives in a separate candidate directory; the unchanged validator only requires its copy and sibling baseline under the approved scratch root, not source and DB in the same directory. Keep baseline.sqlite immutable; only candidate-init.sqlite is the initializer target. The original5c source export and proof logs remain frozen. Archived initializer proof is preserved before any new candidate execution; record this intentional reuse rather than claiming a brand-new copy.

Archive staging also consumes space: measure it, account for temporary files, and preserve12GiB free before any validation/rollout work. If archival cannot fit, use root-approved recoverable lossless archival of another explicitly owned superseded copy or stop. Do not remove unknown files or lower the gate. Restore only from verified archives if required. No new backup/clone is justified just to produce a fresh directory name. Alternative new-pair allocation requires enough independently verified free space for5.66GB plus bounded overhead and12GiB remaining, and renewed concrete path review.

## Exact candidate validation

Use unchanged pinned release_dashboard_validate.py SHA256a47b1aa3c28cdd7942a32e395053cab2e1a4b3b3efe983fd0793bbe2d13b8ac8 for full initializer/schema/all observed DML/critical-row compatibility, candidate mode only. Its production_base_sha/baseline_files continue referring to d2, so no validator schema reinterpretation is needed. Existing5c proof is the runtime-baseline source evidence; do not rerun its baseline flag and mislabel d2 as5c. New candidate source is a clean Git export with no generated bytecode/test caches/envfiles; use python-B for content probes. Copy before/after full hashes and all-table fingerprints remain required.

Extend the existing private content probe with two route checks after normal app creation under identical import/authorizer/resource controls; keep the validator itself pinned. Lane endpoint: exact raw SQLite storage-driven expected evidence, every observed lane key once, correct disabled/suspended/conflict/unknown mapping and observed-at rules. Health endpoint: independent full7d/14d SQL/classifier aggregates at identical observed_at, all population/label/token/earliest-anchor/diagnostic fields, not page means or counts-only approximation. Earlier copy lacks post-backup events; preserve that limitation. Repeat the exact bounded full reader three ordinary-condition trials, retain first/worst and assess margin without changing5s/caps; no warm-run cherry-picking. If refusal/narrow margin occurs, stop and review rather than tune limits.

Run combined old-core regression suites plus both new app isolation/controller/React render tests, real WS lifecycle and source/dist existence/parity checks on private Linux. Reuse already verified runtime-compatible dependency environment if its frozen package identity matches; otherwise budget isolated installation. Rebuild final frontend with locked dependencies and compare actual served blobs to final merged master and manifest. Windows/local evidence and Linux exact-candidate evidence are labeled separately from feature GitHub CI.

## Live smoke semantics and recovery

Fresh readonly snapshot verifies actual5c starting tree/branch, dashboard PID and pipeline identity, all schema/config/dependencies, required columns/indexes for both new readers, and full policy compatibility before any stop. New health request is manual and bounded; no load loop or production population dump. Live smoke verifies HTTP200/meta.ok true, windows7/14 with parseable exact dates and observed_at, unknown coverage/unverified provenance, bounded nonnegative counts and internal row/label/population partitions. It does not require old-copy counts or claim independent live all-field SQL parity. Lane smoke verifies HTTP200/meta.ok/read_only, source/not_execution_eligibility, bounded unique exact keys and supported statuses; no trade-eligibility assertion. Copy tests provide independent semantic parity.

Existing get() limits every HTTP request to3s, while health reader's legitimate cap is5s. Add an explicit request-timeout parameter default3s and use7s ONLY for the new health request, still inside the existing60s total smoke deadline/body budget; preserve server5s and all other HTTP timeouts. Test a health response slower than3s but within7s and deadline/oversized/503 refusal. No retry loop on unavailable health; fail the smoke and recover5c. Lane uses normal3s budget.

Root alone performs ordinary dashboard stop/switch/start after two exact candidate/helper approvals, all proof gates and fresh locked preflight. Existing strict quiescence rechecks, initializer GET, full post-smoke policy/schema/config/process/journal checks, pipeline pin, event logging and rollback safeguards remain. Post-snapshot timestamp drift on either DB still fails. If rollback is needed, restore verified5c branch and complete its existing viewer smokes; absence of the two new endpoints is expected on5c and is not queried during rollback. No force kill/reset/config/migration/index/dispatch actions.

## Acceptance and checkpoint

- [ ] Root and ops approve this exact design before any code/assembly/copy/helper changes.
- [ ]585 merged exact CI; pin master and seven PRs; red/green manifest/helper residual tests.
- [ ] Reviewed recoverable copy reuse and12GiB budget; exact source/Linux/build/full-copy/new-content proofs.
- [ ] Two exact final candidate/helper reviews; root verifies fresh production baseline and conditionally executes.

This design commit changes only design/todo metadata. No implementation, archive, copy mutation, helper change or production action occurs now.
