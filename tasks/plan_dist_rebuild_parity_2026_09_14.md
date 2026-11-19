# DASH-12 frontend rebuild parity plan

**New primitives introduced:** one small read-only build-output comparator and one
CI job. Reuse the existing Vite build, committed dist, lockfile and asset-existence
guard; no frontend behavior, runtime dependency or production change.

## Hermes-first analysis

Drift first: tests/test_dist_build_guardrail.py:11-14 explicitly implements only
referenced-asset existence. docs/dist_build_guardrail.md:27 and :42 explicitly say
the full Vite rebuild/hash comparison is not wired. scripts/pre-commit-dist-consistency.sh
also checks references, not source/build equivalence. Current test.yml has Python
and reviewer-clearance jobs without a Node build. A stale but internally consistent
old index/bundle therefore passes both existing guards.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Repository-specific Vite committed-artifact parity | none found in accessible ecosystem directory; Skills Hub catalog did not load | Build a small comparator over existing Git blobs and fresh Vite output |
| CI orchestration and dependency installation | no Gecko-specific replacement identified | Reuse GitHub Actions and the existing npm lockfile; Hermes remains orchestrator |

Checked https://hermes-agent.nousresearch.com/docs/skills/ and
https://github.com/0xNyk/awesome-hermes-agent on 2026-09-14. The Hub returned a
loading catalog shell; the accessible directory describes general agent skills,
tools and surfaces. Verdict: repository-specific verification residual only;
this is not an exhaustive negative catalog claim and does not replace Hermes.

## Status, scope and authorization

PLAN ONLY. Worktree C:/projects/gecko-alpha-dash12-20260914, branch
docs/dash12-rebuild-parity-plan-20260914, starts at freshly fetched origin/master
d7a0e2672c8d67ba33dc47cfd1a818c2b2bad5b3. Shared release/PR580 worktrees are untouched.
No checker, workflow edit, dependency installation, Vite build, PR or production
action has been performed. Only this plan and a tasks/todo.md checklist change.

Read CLAUDE.md, relevant lessons, current workflow/package/lock/config, and both
existing guards. Repository AGENTS.md is absent; supplied operator instructions
apply. Using writing-plans, using-git-worktrees and brainstorming guidance, with
the operator's explicit Plan > two reviews > Design > two reviews > Build order
taking precedence over a skill's alternative sequencing or extra approval prompts.

Goal: fail CI when the committed served frontend differs from a fresh build of
the exact checked-out candidate. Keep the always-on Python existence test. This
closes DASH-12's remaining source/build-parity gap, not dashboard deployment,
runtime health, browser behavior or the wider DASH backlog.

## Chosen approach and alternatives

Recommended: a separate frontend-dist-parity job in .github/workflows/test.yml.
It checks out the ordinary pull_request merge candidate or pushed master commit,
installs the locked frontend dependencies, builds into a fresh temporary output
directory, and compares that output to Git blobs at the checkout's exact HEAD.
Log HEAD and tool versions; do not claim PR-head-only verification when Actions
actually checks a synthetic merge ref. Keep the existing test/reviewer jobs intact.

Alternative 1, rebuild over tracked dist then git diff: rejects retained historical
assets when Vite empties outDir and risks comparing a checkout already overwritten
by the build. Alternative 2, compare only filenames referenced by index.html:
misses changed bytes under an unchanged filename and emitted lazy chunks. Neither
is as direct as comparing all files freshly emitted for the current application.

The fresh build defines the comparison set: index.html and every emitted file
(JS/CSS chunks and any copied public assets). Each must exist as a tracked file at
the corresponding dashboard/frontend/dist path in HEAD and have identical bytes.
Require an index with local asset references and confirm its references exist in
the generated tree. Compare index bytes as well as assets. Extra files that exist
only in committed dist are allowed, including historical unreferenced hashes.
Do not delete, demand removal of, or regenerate historical assets to satisfy CI.

## Files and proposed contract for review

- Create scripts/check_dist_rebuild_parity.py: standard-library CLI accepting a
  repo root and an existing fresh build directory; compare against HEAD Git blobs.
  Exit 0 only for byte identity of the complete generated output set; nonzero for
  changed/missing/untracked files, absent entrypoint, unsafe paths or Git failures.
  Print concise differing relative paths and reason, never entire bundle contents.
  It does not install, build, mutate tracked files or delete directories.
- Create tests/test_dist_rebuild_parity.py: small temporary Git repositories and
  fake generated outputs for discriminating comparator tests. No Node dependency
  for these tests; keep tests/test_dist_build_guardrail.py behavior unchanged.
- Modify .github/workflows/test.yml: additive separate frontend-dist-parity job
  on the same push/master and pull_request/master events, without path-based skips,
  continue-on-error, pull_request_target or secret-bearing environment. Use ordinary
  read-only checkout credentials and no publication/deployment action. Do not alter
  repository branch protection or required-check settings as part of this task.
- Update docs/dist_build_guardrail.md to distinguish existence and enforced rebuild
  checks, document commands/toolchain, and remove the now-obsolete unwired claim
  only after real CI succeeds. Update plan/design/report/checklist metadata.

Expected implementation surface excludes dashboard application source, dist,
package.json, package-lock.json, Vite config, scout/, DB/config, deployment scripts
and service settings. If current master cannot reproduce its committed build, stop
with evidence and return to review; do not silently fold source/dist changes into
this guardrail task. PR575/577/578/580/579 remain separately owned/completed work.

## Determinism and CI resource boundary

Existing frontend scripts are build='vite build' and build:codex='vite build
--configLoader native'; config sets outDir='dist'. Lockfile v3 pins Vite6.4.1.
Observed local tooling is Node24.14.0/npm11.9.0. Design should pin Node24.14.0 in
the CI setup step, use the normal production build command, log the resulting npm
version and lockfile digest, and prove Linux output identity without changing the
lock. No npm update or dependency-version migration belongs here.

Use npm ci --ignore-scripts --no-audit --no-fund against the committed lock; retain
optional platform packages required by esbuild/Rollup. This exact installation
mode has already built the current release locally, but its Linux result is a
required implementation check rather than assumed. Failure to install/build is
failed verification, never a skipped green result. No runtime credentials/production
environment are supplied; no paid APIs or external messages.

Create a new build directory under RUNNER_TEMP with mktemp and pass its absolute
path via npm run build -- --outDir. It must not be tracked dist, a parent of source,
or a reused directory. Do not run an unrestricted recursive cleanup in repo paths.
The comparator rejects symlinks, path traversal, special files and missing/empty
output. Use NUL-delimited Git path handling or an equivalent argument-safe API;
never interpolate filename text into a shell command. Missing Node locally does
not weaken the always-on Python guard; the dedicated CI job installs Node and may
not silently skip parity. Bound the CI job to 10 minutes and keep it independent
of the long Python test job so failure is promptly attributable.

No production runtime assumptions are needed: this gate consumes committed
source/assets, locked public packages and an ephemeral CI filesystem. Fresh DB,
feature-flag, dispatch or vendor probes would be unrelated ritual. CI checkout
identity, lockfile/toolchain and filesystem ownership are the state that matters.
The source has LF normalization for frontend/index.html and binary (-text) asset
handling in .gitattributes; compare Git bytes rather than locally normalized asset
names. A documented local diagnostic must preserve tracked files and give the same
result as CI on the same source/toolchain.

## Work units after plan/design approvals

- [ ] Root obtains two independent plan reviews: build/reference correctness and
  workflow/security/false-green behavior. Fold findings and record approved SHA.
- [ ] Write a separate design defining the CLI, Git-byte/path handling, exact CI
  steps/version setup, generated-output boundaries and diagnostics. Obtain two
  design reviews/folds before implementation or a Vite proof build.
- [ ] Write failing comparator tests: matching generated output passes; changed
  bytes fail even when names/references stay valid; changed index fails; missing
  committed emitted file fails; extra historical committed asset passes; nested
  lazy chunk differences fail; symlink/escape/missing index/Git error fails.
- [ ] Implement the minimal comparator, rerun those tests plus the unchanged
  existence tests, inspect diff and commit the verified checker/test slice.
- [ ] Add the separate workflow job and documentation. Prove two fresh builds of
  one clean checkout produce identical output during initial validation; the
  permanent CI job needs only one fresh build, not repeated redundant builds.
- [ ] Run a material falsifier in a disposable fixture/checkout: keep a valid old
  committed index and its assets, change a rendered source string, fresh-build to
  temporary output, and prove the comparator fails while the existence check still
  passes. Restore/discard only explicitly owned fixture changes, not shared work.
- [ ] Verify unchanged HEAD/working tree after a passing check; prove historical
  unused asset retention does not fail. No tracking of node_modules/temp output.
- [ ] Commit meaningful workflow/docs slice, create PR, obtain two independent PR
  reviews/folds and exact final-candidate CI before normal authorized merge.
  Production deployment is neither needed nor part of this task.

## Acceptance and verification

Comparator tests must fail when byte comparison is removed, rather than merely
confirming file existence. Workflow tests or focused structural checks must prove
the comparator is actually reached after a successful build and that install,
build and compare errors propagate to job failure. The original asset-existence
tests remain green and run in the ordinary Python job without Node.

Run python -m pytest tests/test_dist_rebuild_parity.py
tests/test_dist_build_guardrail.py -q --tb=short after implementation. Run the
documented npm-ci/temp-build/comparator command on Linux using the selected Node
version, capture the reproducibility and stale-source falsifier results, then
inspect formatting and git diff --check. Exact final-head GitHub CI must exercise
the new job; local successful builds are not a substitute for wired CI execution.

Current baseline verification: three existing asset-existence tests passed in
0.14seconds using C:/projects/gecko-alpha-integration-20260913/.venv/Scripts/python.exe.
No fresh build has run in this plan task and no future check is claimed passing.

## Review and rollback

Plan self-review: scope matches the precise missing half of DASH-12, preserves
retained historical assets, checks served artifact bytes instead of filenames,
and leaves production paths untouched. User-mandated two-plan/two-design reviews
are pending; this commit authorizes neither build nor CI changes.

After a later merge, rollback is an ordinary revert of the added job/comparator
and corresponding docs; the existing existence guard remains. No app restart,
DB restore, dependency downgrade or artifact deletion is involved. Final report
must distinguish source/build parity from full frontend functional correctness.
