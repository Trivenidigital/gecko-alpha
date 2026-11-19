# Dashboard release retry plan

**New primitives introduced:** NONE. Amend the reviewed selective release with the independently reviewed existing WebSocket lifecycle correction.

## Hermes-first analysis

Drift: frozen candidate07af already implements selected dashboard viewers and complete release/compatibility proofs. The only new application residual is the unchanged WebSocket disconnect catch; its separate plan3e86326c/design7cc39a0a each have two approvals. Reuse all unchanged verification machinery.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Selective dashboard rollout | No Gecko-specific replacement found in prior same-run accessible catalog check | Reuse reviewed release artifacts and existing systemd service |
| WebSocket lifecycle | Existing FastAPI/Starlette/Uvicorn behavior | Use separately reviewed minimal correction after merge |

Same-run Hub and awesome-hermes-agent check is documented in the parent release plan; Hub catalog returned a loading shell. Verdict: this is release assembly, with no new primitive or dependency.

## Evidence and change boundary

Attempt1 never switched Git: systemd timed out after20seconds, killed dashboard processes and left failed/failed. Helper rejected failed twice. Existing remediator explicitly logged repaired02:11:44.861162Z. Baseline d2 remained installed; recovered dashboard3409677/3409683 and unchanged pipeline3032091 were verified, along with ten policy-table fingerprints/schema/config/deps. No exclusive cause is claimed for the task wait; disconnect swallowing is independently reproduced.

Create new candidate from frozen07af in this isolated worktree, leaving07af immutable. Overlay only the merged WebSocket PR dashboard/test delta after exact reviews and green CI. Exclude DASH12 CI files, unrelated master changes, core/config/schema/dependencies. New candidate must still equal productiond2 outside the exact selected dashboard/test union and explicit release metadata. Keep previously rebuilt frontend blobs unchanged and verify them. Extend manifest's exact selected PR set only to the merged WS PR; pin all immutable commit identities and show the07af-to-newcandidate delta.

## Verification and rollout gates

- [ ] Two independent plan reviews, then concrete design and two reviews before assembly.
- [ ] WS PR merged after two independent reviews and exact green CI; no unmerged application code deployed.
- [ ] New manifest verifies old core bytes/modes, operational consumer ASTs, exact final selected merged blobs and asset parity.
- [ ] Run old-core selected tests plus new WS actual-handler and natural-loopback shutdown tests on isolated Linux, matching production packages. No production DB access from tests.
- [ ] Reuse unchanged immutable SQLite baseline only after rechecking whole-file identity/hash and live critical policy equality; candidate initialization/API compatibility rerun on a fresh disposable copy. If live policy/schema assumptions drift, stop and reassess instead of overwriting state.
- [ ] Re-run new-candidate content/statistics/assets checks and two final independent candidate reviews.
- [ ] Update disposable helper candidate/manifest pins intentionally; reviewed stop amendment72173c6b has33local+33Linux tests, two code approvals. Re-review final exact helper after pins change.
- [ ] Fresh read-only runtime snapshot pins recovered dashboard identity, baseline cleanmasterd2, pipeline, env/deps/unit/lock; preflight passes before stop and again under lock.
- [ ] Root alone stops dashboard. Failed/failed may proceed only with MainPID=ControlPID=0, no Job, entire canonical cgroup empty or absent, repeated stable observations and held unchanged remediation flock. Recheck immediately before both Git switches. No forcekill/reset/config/pipeline action.
- [ ] Ordinary switch/start; actual lazy-initializer GET, endpoint/content/asset/policy/schema/process/journal smokes. On failure, same quiescence proof before ordinary switch back to masterd2/start/baseline smokes. Persist exact event outcomes; never label timeout termination graceful.

No new PR is required for disposable release assembly metadata: application changes must already be merged via the mandated PR reviews. No second rollout until every gate passes. If runtime refuses quiescence or recovery fails, stop further attempts, preserve evidence and report the unresolved condition.
