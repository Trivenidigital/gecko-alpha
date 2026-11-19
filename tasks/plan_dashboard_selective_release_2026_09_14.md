# Selective read-only dashboard release plan

**New primitives introduced:** NONE. Release assembly and verification only; reuse
merged dashboard artifacts, existing service, production DB and existing core.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Selective Gecko dashboard release | none found in accessible directory; Hub catalog loading shell | Reuse git revision isolation and existing dashboard unit; no new release system |
| Service restart / rollback | no Gecko-specific skill found | Existing systemd and recorded revision rollback; Hermes remains orchestrator |

Drift first: production-baseline source has no selective-dashboard release helper
or plan. Existing systemd/gecko-dashboard.service:7-8 fixes working directory and
entrypoint; dashboard/main.py:20-23 already consumes built dist or installs/builds
if absent. Existing full-tree deployment advice is not safe for this release.
Checked https://hermes-agent.nousresearch.com/docs/skills/ and
https://github.com/0xNyk/awesome-hermes-agent on2026-09-14. Catalog loading limits
the negative result; accessible directory supplies generic orchestration, not the
required old-core/new-dashboard compatibility proof. Verdict: no new primitives.

## Status, ownership and authorization

PLAN ONLY. No code assembly, cherry-picks, design, production writes, service
stops, branch switch, merge or deploy performed. New isolated worktree
C:/projects/gecko-alpha-dashboard-release-20260914, branch
docs/dashboard-selective-release-plan-20260914, starts at observed production
base d2f0d61edc63cb55ae159ec952cce404991f21f5. Only this plan and tasks/todo.md may
change now. PR580 worktree/candidate remains separate and untouched.

Current operator production-push request authorizes reversible read-only dashboard
deployment after merged feature PRs, CI, verification and independent review. It
does not authorize dispatch/config/migration/capital/vendor actions. Root owns
production coordination and freshness checks. Plan requires two reviews/folds,
then separate design with two reviews/folds, then assembly and candidate review.
Standing CLAUDE.md approval records must cite this explicit task authorization and
each satisfied condition; no approval may be inferred from another owner's action.

## Release objective and why full master is excluded

Release only the read-only dashboard effects of PR575,577,578,580 AFTER each is
merged with its required reviews and green CI. PR575 and577 already merged;
578/580 completion and final master pin are prerequisites, not assumed outcomes.
Keep every production core file and operational setting unchanged.

Critical coupling: dashboard/api.py:70-94 _get_scout_db lazily imports scout.db and
calls Database.initialize from existing GET handlers. Full master includes capture
PR572/576 source/migrations: switching full checkout then restarting dashboard can
apply them even if pipeline never restarts. Do not equate a dashboard restart with
a read-only application. The selective tree must retain the old initialization
code, and compatibility/prod-copy tests must show even that unchanged old code is
idempotent on current schema before release.

Chosen path: exact reviewed selective release tree at existing /root/gecko-alpha;
stop/start gecko-dashboard ONLY. No service unit/drop-in change, symlink directory
swap, .env edit, dependency upgrade, migration invocation, cron deployment or
pipeline restart. Directory releases add relative DB/env/import risks here and
are excluded. Root refreshes completed capture ownership before any deployment.

## Assembly and immutable manifest requirements (after approvals)

Obtain final merged master SHA containing all four viewer PRs and record their
squash SHAs. Enumerate exact dashboard source/assets and focused test/doc paths
from those PR deltas; do not cherry-pick intermediate merge commits, full master
history or whole-tree snapshots. Reconcile docs separately from runtime source.

Construct candidate from d2f0d61e in this isolated worktree only after plan/design
approvals. For every selected dashboard source file, resulting Git blob must be
byte-identical to final reviewed merged master. Preserve all other dashboard files
from production baseline unless explicitly included by a selected PR delta. Any
conflict requiring novel source adaptation invalidates the identical-artifact
assumption and returns to review; never opportunistically pull new scout code.

Rebuild frontend with the already-pinned package/lock versions in isolated local
workspace, include combined dist, and verify it matches final reviewed master
bundle byte-for-byte. A mismatch requires explanation and renewed review, not
assuming minification churn harmless. dist/index.html references only included
assets. Missing dist is a hard stop: main.py would otherwise run npm install.

Maintain machine-readable allowlist and SHA/blob manifest in the later release
report. Require exact equality versus d2f0d61e for EVERY tracked path outside the
explicit dashboard/test/release-document allowlist, not only a handful of modules.
In particular scout/, scripts/, systemd/, cron/, .github/, .claude/, config files,
pyproject.toml, uv.lock, Docker/start files and unrelated docs remain identical.
No new Python/runtime dependency, package-lock change or writable DB adapter.
Untracked .env, scout.db/WAL/SHM, sessions and runtime artifacts are not overwritten,
removed, copied into git or included in a published release artifact.

## Runtime assumptions: root must verify before design/release

1. HEAD/branch exactly expected and all tracked production paths clean; enumerate
   untracked/ignored paths secret-safely and compare to candidate changed paths.
   Any collision or unexplained tracked modification stops release; never stash,
   reset, force-checkout, overwrite or clean production automatically.
2. Actual systemd units AND drop-ins: WorkingDirectory, ExecStart, executable path,
   Restart policy, KillMode/TimeoutStopSec, EnvironmentFile path (not secret values),
   restart-related hooks and dependencies. Source unit defaults are not runtime
   proof. Confirm stopping dashboard does not stop/restart pipeline via unit deps.
3. Running process working directories/import origins and Python executable; inspect
   sys.path and module __file__ without importing live app/initializing production
   Database. Verify production editable install points at this same checkout, not
   another tree. Record installed dependency versions and unchanged lock hash.
4. uv run service startup must not re-resolve/install changed dependencies. Prove
   current venv matches retained lock/runtime packages; if startup would mutate
   shared environment, halt for design revision rather than edit unit/env ad hoc.
5. Filesystem free space/inodes for Git objects, built assets, logs and safe DB-copy
   validation. Determine production DB/WAL size and backup/copy feasibility before
   initiating copy. No raw cp of a live SQLite main file ignoring WAL.
6. Active owner state, deployed checkout, dashboard/pipeline PID and start time,
   config-file hashes (no contents), schema fingerprint and migration rows. Capture
   collector/watchdog inactive state remains unchanged; do not enable them.
7. Current table/column/cutover assumptions for all four new readers. Missing state
   gives explicit unavailable, but unexplained loss versus preflight blocks deploy.
   Previous snapshots are dated evidence, not claims of continuing health.

Root is querying production size/schema/units/import/dependency evidence and copy
feasibility. Do not claim these prerequisites passed from this source-only plan.
SSH results must use redirect-to-file then separate read; never print secrets.

## Compatibility, production-copy and exact-candidate validation

Run tests from the selective candidate with OLD scout tree and retained dependency
versions, not with current master source on sys.path. Prove module origins/core
manifest before tests. Run selected viewer/API/contract/frontend suites and
baseline dashboard regressions; classifier unchanged, read-only paths retain
missing-schema/cancellation semantics. Full application startup/router imports
must succeed with old core. Synthetic network-disabled fixtures are preferred for
ordinary tests; no external sends, trades or vendor probes.

Root must establish a safe SQLite backup/copy method and space bound. Use an
isolated snapshot/backup with production permissions retained; never commit/upload
DB content. On this copy, exercise actual application startup and existing
_GET routes that reach _get_scout_db/initialize, plus the new GET routes; compare
schema/migration rows before/after and inspect SQL side effects. No new migrations
or changed configuration may be required. If old initialize mutates schema on this
copy, halt: the no-migration release premise is not satisfied. No production
initialize probe is permitted as a shortcut. Database copy write permissions apply
only to isolated test copy, never production; delete only named test artifacts
under verified scratch paths after review/evidence extraction.

Feature CI and selective-release validation have different evidence scopes.
Each selected feature PR must already be merged with its own exact-candidate
GitHub CI green. Baseline .github/workflows/test.yml:3-7 only targets master;
its PR checkout tests a merge ref, so it is not proof of the old-core combination.
No extra GitHub workflow, configuration change, release PR merge or operator gate
is required: the user's deploy authorization requires merged reviewed features,
smoke tests and rollback notes, with this plan adding the necessary compatibility
verification for the selective combination.

Run focused Linux tests, actual app import/startup and production-copy checks on
the exact selective SHA and attest the old-core manifest. Label that evidence
honestly as local Linux candidate validation, not GitHub CI. Publish the immutable
release manifest/report with selected PR source/CI links and the local validation
commands/results. Two independent release-candidate reviewers cover import/runtime/
migration reachability and operational rollback/isolation; all findings must be
terminal and folded. Do not merge the selective branch into product master.

## Conditional rollout and rollback requirements

No rollout until all upstream merges, exact selective-candidate local Linux verification,
two candidate review clearances and fresh runtime preflight pass. Root records
conditions and release SHA with rollback SHA d2f0d61e and original branch.

During authorized rollout: stop only gecko-dashboard; verify its process exits
under actual unit stop bound. Pipeline PID/start time and operational core manifest
must remain unchanged. Fetch only necessary reviewed objects, verify release SHA,
switch the clean existing checkout to the exact reviewed release branch without
force. Recheck tracked/operational manifests before starting dashboard. No
systemd daemon-reload/unit installation/cron script/config change. Start dashboard;
verify actual loaded path/revision and retained dependencies rather than checkout
SHA alone. Abort and rollback if stop/start or health smoke fails.

Smoke existing status/history and the four viewer surfaces with GET requests;
compare current counts/cutovers/descriptives to an equivalent pinned read-only
snapshot. Check bundled assets resolve and new panels load; no production action
buttons or live messages. Compare schema fingerprint/migration rows, config hashes,
pipeline PID/start time and core manifest against preflight. Pipeline data rows
may legitimately accumulate while running: do not demand whole-DB byte equality
or falsely attribute ordinary writes to release. Require no schema/config changes.

Rollback: stop only dashboard, switch clean checkout to recorded pre-release
branch/SHA, verify original core/assets, start dashboard and repeat baseline smoke.
No data restore is planned because no migration is allowed. Unexpected schema
change halts automated rollout/rollback reasoning and requires findings; never
perform destructive restore/deletion to hide it. Record downtime boundaries,
post-restart-only logs, old/new SHAs and merged-versus-deployed status separately.

## Checklist and present review result

- [x] Own isolated production-baseline worktree; source drift and Hermes checks.
- [x] Plan-only release boundaries, runtime assumptions and distinct CI/local-validation evidence documented.
- [ ] Two parallel plan reviews/folds.
- [ ] Root runtime/copy feasibility evidence and local Linux validation path confirmed.
- [ ] Separate design and two parallel reviews/folds before assembly.
- [ ] Assemble exact dashboard artifact, prove full core parity, test old-core/copy.
- [ ] Publish release report with two reviews, merged-feature CI links and local Linux validation.
- [ ] Root-controlled conditional dashboard-only rollout/smokes/rollback evidence.

Self-review: no source or production actions taken. Production-push authorization
is conditional and has not yet been satisfied. Existing no-deploy report remains
true until root records verified rollout; a plan does not close that gate.

## Runtime and authorization clarification

Root2026-09-14T00:54:39UTC: DB2.7GB,28GBfree (63percent diskused),
Python3.12.3/aiosqlite0.22.1/SQLite3.45.1; dashboardPID3032127 and pipeline3032091
both cwd/root/gecko-alpha; only10-telegram-onfailure.conf drop-ins; tracked-clean
masterd2f0d61e. These observations inform copy feasibility but do not by themselves
prove completed backup/copy, dependency parity, import correctness or unit-dependency
isolation. Recheck immediately before release. No new GitHub CI/config/operator gate
is imposed; per-feature merged CI plus exact selective-candidate local Linux/app/
prod-copy validation and two-vector review are the distinct required evidence.
