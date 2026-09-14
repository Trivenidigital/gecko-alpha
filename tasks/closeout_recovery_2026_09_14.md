# Overnight closeout recovery — 2026-09-14

## Verified recovery and completed merge

The prior owner stopped on a usage-limit error, confirmed through a fresh task
snapshot. This recovery started from clean origin/master1967cbfc, then refreshed
to829d12b1 after merging [PR585](https://github.com/Trivenidigital/gecko-alpha/pull/585).
The final feature head f64bf7c2 had all three CI checks green:8055 tests passed,
14 skipped,118 dashboard contracts, frontend rebuild parity and reviewer
clearances. Recovery reviewers independently approved structural/read-only
correctness (92 tests,1 platform skip) and operations/lifecycle safety (29 tests).
Merge:829d12b191ef2a1122dc18755492a5dd4fc106fc,12:32:18Z.

PR582's stale todo checkbox was reconciled against its actual merged state:
2b084e27a3cbe53869a7d946562bcaab9bd93e75,02:53:28Z. Its CI-only change needs no
runtime deployment. PR585's merged state is separate from deployment.

## Scope drift and Hermes-first verdict

The six requested templates already exist under docs/superpowers/templates;
docs/runbooks/gecko-autonomous-operating-model.md already defines Hermes,
Codex, reviewers, operator gates and runtime truth sources. The existing
scripts/report_autonomous_status.mjs ran successfully in this recovery. No
duplicate templates, role framework or status UI were built.

The historical cockpit and Signal Trust parent items remain superseded by
their child work. PR584 supplies recorded lane-status annotations; PR585
supplies manual-refresh suppression-cohort health with unknown coverage and
unverified provenance. Neither changes ranking, dispatch eligibility or policy.

The [Hermes Skills Hub](https://hermes-agent.nousresearch.com/docs/skills/)
was re-opened during recovery and returned its loading shell. The
[independent ecosystem directory](https://github.com/0xNyk/awesome-hermes-agent)
was also checked. This is not an exhaustive negative catalog result.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Gecko selective source attestation | No directly applicable replacement established | Reuse existing Gecko manifest |
| Copy validation and service rollback | Generic orchestration; no Gecko-specific replacement established | Extend reviewed existing guards |

Verdict: retain existing Gecko release mechanisms; introduce no orchestration
framework, dependency, paid API or new service.

## Runtime assumptions checked before release work

Fresh SSH evidence12:31–12:39Z established:

- Production tracked tree clean at5c43526c46a33b06e7ed18509c8a0ea43c7772d3 on
  docs/dashboard-release-retry-20260914; dashboard3426800 active.
- Actual pipeline unit is gecko-pipeline.service, active with PID3032091 and
  start-monotonic15403988077140. gecko-alpha.service was an incorrect lookup,
  not evidence that the pipeline was down. Hermes gateway active.
- Codex worker failed before ExecStart: auth guard exit21, OAuth login inactive.
  This is an operator account gate; no auth, secret or guard changes were made.
- Required new reader columns/indexes passed the successor snapshot checks.
- Pinned policy guard found retained-copy/live compatibility; its only existing
  initial exception remains combo_performance.last_refreshed. Full post-snapshot
  fingerprints are retained, without adding another exception.
- Free disk14393053184 bytes left about1.4GiB above the mandatory12GiB floor.
  A new2.83GB validation copy was therefore prohibited.

At12:36Z, streamed complete SHA256 checks proved both retained5c validation
copies match existing immutable07af gzip archives (a0b1c9a7238ff18480060968430f6786b29875fbd1babaae3e5ddaec7d0a54e4).
Ownership, identity, no-open-FD and zero-WAL checks passed. Both reviewers
accepted reusing those identical archives. Six prior proof files and an explicit
archive-to-copy provenance mapping were preserved at12:43Z before any copy reuse.
The original baseline, source exports, archives and rollout evidence remain intact.

## Successor release review and current gate

Original plan5e249634 had two approvals; successor designf1a9250c received two
fresh reviews. Assembly correctly refused the blanket1358-file preservation
requirement because PR584 changes two UI components in that set. Amendment
ed680902 names their exact original/replacement blobs and modes, preserving all
remaining1356 files and all five operational consumer ASTs. Root and ops reviewed
the actual diffs and approved before assembly resumed.

The separate rollout helper retains core d2 and runtime rollback5c identities,
full policy guards, lock/quiescence checks and all old viewer smokes. Its new
health request alone has a7s HTTP timeout; server5s and total60s smoke limits
remain.87 mock tests passed locally and on Linux. Two independent implementation
reviews approved; candidate pinning and final release proofs remain separate.

The content probe independently compares every new lane and health response
field against SQL/storage evidence. Five fixture tests and two reviews passed.
Private Linux validation requires all three health trials below4s, inherited
512MiB/120s SQL bounds and an external180s timeout. No performance conclusion
about current live data follows from an older retained copy.

**Current checkpoint:** release assembly/private-copy validation in progress.
No new deployment has occurred in this recovery. Completion evidence will be
recorded separately; a merged PR is not treated as a deployed surface.

## Operator gates and permanent loop

Restore the intended OAuth login for codex-autonomous-dev-srilu on srilu; do not
bypass its guard. Publish/reconcile the current successor queue: the July Fable
tracker remains dated, untracked evidence in the main checkout, not a current
shared queue. Paid vendors, live execution, sizing, pruning, dispatch changes,
destructive writes and account/secret changes remain gated.

The saved hourly automation still requests a six-hour block. Its prompt needs
explicit owner/overlap checks, failed-owner recovery, current child-queue
selection, and separate configured/invoked/completed/deployed evidence. No
automation configuration change was made. This checkpoint does not claim six
hours elapsed or that every remaining backlog item is exhausted.
