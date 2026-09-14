# Successor read-only dashboard release plan

**New primitives introduced:** NONE. Reuse the selective manifest, copy validator, content probes and guarded dashboard rollout. PLAN ONLY; no source assembly or production action.

## Hermes-first analysis

Drift first: frozen 5c43526c46a33b06e7ed18509c8a0ea43c7772d3 already carries merged PR575/577/578/580/583 and the reviewed old-core release machinery. tasks/release_dashboard_manifest.py:82-134 verifies selected squash ancestry, exact blobs/modes and operational consumer ASTs; its exact five-PR set at85 must narrowly expand to seven after PR585 merges. Existing external copy/rollout guards provide the required execution and recovery mechanisms. No new framework is justified.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Selective source and compatibility verification | None Gecko-specific in accessible prior same-run catalog | Reuse existing manifest and copy validators |
| Dashboard service recovery | Generic orchestration only | Reuse reviewed systemd/remediation-lock helper |

Actual prior checks on 2026-09-14: https://hermes-agent.nousresearch.com/docs/skills/ returned a catalog loading shell; https://github.com/0xNyk/awesome-hermes-agent was checked. Evidence is preserved in tasks/plan_dashboard_selective_release_2026_09_14.md:7-22. This plan does not claim a new exhaustive search. Verdict: use the already reviewed capabilities; no new dependency or custom architecture.

## Observed source and residual

New isolated worktree C:/projects/gecko-alpha-dashboard-health-release-20260914, branch docs/dashboard-health-release-plan-20260914 starts at frozen5c43526c. Frozen release worktree and external helper remain untouched. Fresh origin/master is1967cbfc42185fe4fb74d527bd0af9929375dae0 (PR584 merged); PR585 is pending, with reviewed integration661020e8e4b60716e37b3e0cab00e3ebdc30574c. These are observations, not authorization to deploy unmerged code.

Residual: the frozen release lacks the two read-only panels. PR584 adds lossless lane-state annotations to existing Focus/Inbox rows; PR585 adds manual-refresh experimental suppression health in Pipeline. In the current integration, dashboard/api.py:223 captures lane path;225 creates app-local HealthReader;236 and434 register the two routes. dashboard/lane_status.py:105-153 owns its bounded read-only worker; dashboard/suppression_health.py:53 and282 own the transactional health read and single-worker lifecycle. Retain these exact merged implementations, including shutdown drain, per-app isolation, abort/generation handling and both CSS sections. No scoring, sorting, row cardinality, execution eligibility, policy, index, schema, ingestion or dispatch changes.

Reviewed suppression reader SHA2560004d187920c40a7ebff98862f86aee341111afb310c34a987dd6401091234e5 is the current reference, to be rechecked against final merged585. Its7d population/14d recorded-label window and5s/caps are fixed. The prior exact-copy three-trial worst2.047s is evidence for that older population, not a live performance SLO. Preserve unknown coverage/provenance and window-relative anchor semantics. Lane labels remain observations, never trade eligibility.

## Source assembly contract after review gates

- [ ] Obtain two independent plan approvals, then write concrete design and obtain two design approvals before assembly.
- [ ] Require PR585 merged after exact CI and two reviews. Refresh origin/master and pin its full SHA plus actual squash584/585 identities; never substitute an intermediate candidate. If unrelated changes entered selected dashboard paths, inspect their provenance and return to review rather than silently importing them.
- [ ] Build a new candidate descending from frozen5c; preserve all1358 previously attested core files byte-for-byte and mode-for-mode against d2f0d61edc63cb55ae159ec952cce404991f21f5, including operational consumer AST identities. d2 is the immutable CORE reference, not an assumed runtime rollback target.
- [ ] Extend existing exact manifest set to575/577/578/580/583/584/585 with narrowly reviewed guard tests. Select only merged dashboard/tests union plus explicit release metadata. Exclude workflows, dependencies, core, config and unrelated master files. Preserve all current viewer and WS fixes. Verify exact final merged-master bytes for selected source and actually served frontend index/assets; retain allowed historical unreferenced assets. Do not deploy all master to obtain its frontend.
- [ ] Record exact5c-to-successor diff. Two new reader modules, their additive API/lifespan integration and approved UI/test changes are the application residual. Any unexpected source delta stops assembly for review. No implementation rewrites in the release branch.

## Compatibility, resource budget and evidence

Windows C: observed314251493376 bytes free; read-only host df observed14454181888 bytes free. These are planning snapshots only. No new SQLite copy or dependency environment is created now. Before later validation, measure free disk again and inventory only owned artifacts. Reserve capacity for the roughly2.83GB disposable database, bounded WAL/temp space and environment/logs, while retaining at least6GB free headroom; refuse insufficient capacity. Do not delete another owner's files or make repeated2.8GB copies to evade a failed gate.

- [ ] Reuse the existing immutable, full-hash-verified baseline if its supported schema/population remains appropriate. Explain retained-copy age; do not represent it as fresh live data. Existing sibling baseline.sqlite/candidate-init.sqlite path requirements remain explicit. Reuse one private disposable initializer copy when needed after design approvals; verify source hash and forbid symlinks/pending WAL. Any fresh backup requires separately justified scope and available disk.
- [ ] Reuse the pinned full-schema/all-observed-DML copy validator unchanged wherever possible. Extend only endpoint/content assertions for the two new readers under separate review; no weaker initializer or old-core checks. Compare new endpoint results against independent same-copy calculations at identical as-of/windows and lossless lane storage evidence. Existing history2234/postmortems31/Telegram1308/stops355 figures belong to the retained copy, not expected live counts.
- [ ] Run actual candidate imports/application, both app-local routes, lifespan drain, independent two-app fixtures, real WS shutdown, Focus/Inbox/Pipeline controller/render tests and referenced-asset checks on isolated Linux with production-compatible locked dependencies. No production test DB connections. Verify exact fresh frontend build parity against pinned merged blobs; report feature CI separately from release-candidate Linux evidence.
- [ ] Retain the health reader's bounded refusal behavior. Validate full-read completion/independent parity and ordinary-condition timing without changing5s/caps or masking a slow result. No cost or ranking claim follows from counts. Verify lane read-only raw-type/refusal and stale-observation semantics.
- [ ] Two independent exact candidate/helper reviews follow successful source/Linux/copy/content evidence. Preserve failed checks honestly; clean exports rather than loosening import/artifact guards.

## Runtime identity and rollback contract

Root reports the5c rollout succeeded at2026-09-14T04:01:39Z: deployed HEAD5c43526c46a33b06e7ed18509c8a0ea43c7772d3 on branch docs/dashboard-release-retry-20260914, dashboard PID3426800 and unchanged pipeline PID3032091. This is the planned successor BASE and rollback branch. Before successor rollout, root must freshly attest that clean HEAD/branch, source manifest, process/unit/lock, pipeline identity, schema, configuration and dependencies still match. Any other state stops for reconciliation. Never overwrite the completed retry's helper or baseline snapshot. Root also reports original immutable07af baseline retained, superseded initializer copies losslessly archived, and current5c copies intact; reuse remains subject to fresh hashes, not these reports alone.

The rollout helper must distinguish immutable core reference d2, verified deployed starting release, successor candidate, and actual rollback branch/ref. Rollback returns to the attested starting release on its verified branch/ref with its own smoke expectations and source manifest, NOT hard-coded masterd2. The concrete design must trace existing BASE-dependent assumptions individually (clean-tree/source pins, snapshots, recovery and entrypoint) before changing helper pins; do not globally replace d2 and accidentally weaken core proof. Reuse existing helper, with minimal explicit pin/contract adaptation under tests and two final reviews.

Initial copy/live compatibility may use only the separately reviewed exact combo_performance.last_refreshed exclusion. Full copy and full live fingerprints are captured separately; all later checks include timestamps. Do not extend that exception to health/lane values or any other column. Any additional drift requires read-only diagnosis and renewed review, never state overwrite. Copy initializer/content checks remain full.

- [ ] Fresh locked preflight verifies actual starting identity, candidate/manifest bytes, all core and operational consumers, full runtime policy/schema/config/dependencies and unchanged pipeline. Recheck before stop and immediately before Git switches.
- [ ] Dashboard-only ordinary stop; accept quiescence solely under reviewed PID/job/cgroup/lock evidence. No extra kill/reset/config changes. Ordinary switch/start, live lazy-initializer and old/new endpoint/asset/policy/schema/journal smokes. New endpoint unavailable/refusal cannot be presented as successful feature validation.
- [ ] Failure uses the same quiescence guard, restores actual attested starting branch/ref and verifies baseline smokes. Preserve logs and accurate graceful/timeout outcomes. Root alone authorizes/executes after all gates; this plan performs no rollout.

## Completion boundary

This task ends with plan commit and two-review handoff. Design, application assembly, private database copy, helper changes, source publication and production execution are later stages. No feature merge bypass or additional GitHub workflow requirement is introduced: merged-feature exact CI and honestly labeled exact-release Linux/copy proofs remain distinct gates.
