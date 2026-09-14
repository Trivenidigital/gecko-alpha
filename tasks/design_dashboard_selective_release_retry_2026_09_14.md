# Dashboard release retry design

**New primitives introduced:** NONE. Update existing manifest pins, candidate application blobs and disposable rollout evidence.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Selective application release | No specific replacement in same-run parent-plan ecosystem check | Existing reviewed Git manifest, copy validator and systemd helper |
| WebSocket shutdown proof | Existing installed ASGI tools | Reuse merged regression tests |

The parent plan records Hub loading limitation and awesome-hermes-agent check. Verdict: reuse, no custom framework. Plan2f16606f has two independent approvals.

## Assembly

Keep07af frozen. This retry branch starts there. After WS PR exact checks/reviews/merge, fetch master and pin its full squash SHA and first parent. Inspect WS diff and permit only reviewed dashboard/api.py and WebSocket tests as application delta. Apply that dashboard/test patch to07af; refuse unexpected overlaps or files. Do not merge master or import CI/provider/core changes.

Update release_dashboard_manifest.py exact PR key set from575/577/578/580 to those four plus the verified WS PR number. The manifest still derives the union only from merged squash dashboard/tests changes, compares selected paths with pinned WS merge tree and everything else with d2, preserving an explicit release metadata allowlist. If intervening master changes affect selected blobs, stop and reconcile before assembly rather than silently including them. Extend harness expectations for the fifth immutable merged PR; rerun all harness tests. Metadata names use existing release-plan/design/report prefixes. Compare07af-to-newcandidate diff and all1358 prior core files; no frontend/deps/config changes expected.

## Exact compatibility proof

Generate candidate archive from final committed SHA. New private host child under /root/gecko-dashboard-release-20260914/<newSHA>, mode0700, refuse collisions. Keep original baseline.sqlite at07af path immutable. Verify SHA256a0b1c9a7238ff18480060968430f6786b29875fbd1babaae3e5ddaec7d0a54e4 plus zero-WAL/file identity. Query current live critical policy fingerprints and schema via reviewed read-only helper; drift fails closed. Reuse recorded baseline initialization result only if helper/source/schema/package pins unchanged. Create an ordinary private closed-file copy at newSHA/baseline.sqlite and verify its full SHA256 equals the original; no symlink is permitted. This preserves the pinned validator contract that baseline.sqlite is beside candidate-init.sqlite. Copy this verified new-child baseline into a fresh candidate-init.sqlite; rerun candidate initialization/API validation using the reviewed a47b1aa3 validator and supplemental actual-content validator with new candidate source path. No live DB initialization is used as a substitute for copy proof.

Use unchanged private production-matching testvenv at07af (all80 runtime versions matched). Run selected269 tests plus merged WS tests against new candidate source, isolated fixtures and no production files. Existing fixture network isolation remains; the WS natural-shutdown loopback test requires networking only inside its own network namespace with loopback enabled, or an independently reviewed owned loopback scope if namespace loopback cannot be enabled. No external endpoints are required. Record exact test counts/warnings and natural completion; baseline old-code falsifier belongs to WS PR evidence. Recheck immutable baseline hash and all served assets afterward.

## Helper and runtime

Preserve priorattempt8b51 and amendment72173c6b files/evidence. Final helper changes only CANDIDATE and explicit VERIFIED_COPY location (retained07af immutable copy), plus any review-required fixes. Manifest file carries new candidate identity; other base/env/deps/pipeline/unit pins remain unchanged unless fresh evidence requires replanning. Run33mocktests locally/Linux, compare helper delta and obtain two exact helper approvals.

Fresh snapshot uses recovered dashboard identity, unchanged pipeline and source d2. Read-only preflight first, then repeat under existing lock. Stop contract is exactly the twice-reviewed amendment: inactive/dead or failed/failed, bothPIDs0, Jobempty, canonical cgroup absent or populated0 over full subtree, stable second observation, same lock inode held. Recheck immediately before each ordinary Git switch. No unit changes or additional signals. Run existing actual-initializer/content/assets/policy/schema/new-process/journal smokes after candidate start; failure uses same proof before switchmaster/start/baseline checks. Leave remediator and alert wiring intact. Final report distinguishes merge, candidate validation, attempted rollout and actual deployment.

## Review gates

Two design reviews before application assembly. Two final candidate reviews and two exact helper approvals before root executes. CI-only DASH12 is separately merged and excluded from deployed payload. Any new reviewer fold or runtime drift is verified before proceeding; do not widen deployment scope to make a gate pass.
