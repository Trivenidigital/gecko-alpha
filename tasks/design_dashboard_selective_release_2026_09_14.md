# Selective dashboard release design

**New primitives introduced:** NONE. Immutable release assembly and disposable
verification harnesses only; existing service, core, environment and DB retained.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Gecko selective release | none found in accessible directory; Hub catalog loading | Existing Git blob identity and systemd workflow |
| Consistent SQLite copy / rollback | no repository-specific skill identified | SQLite backup API and existing service restart; no runtime framework |

Drift/Hermes check2026-09-14 in approved plan. Sources:
https://hermes-agent.nousresearch.com/docs/skills/ (catalog unavailable) and
https://github.com/0xNyk/awesome-hermes-agent (directory inspected). Verdict:
release existing artifacts without introducing orchestration or deployment tools.

## Review status and current evidence

Both plan reviewers APPROVE bc4c5837c5727e971215f290d997eeac6028e694, no folds.
Both design reviewers APPROVE 38150404fdfd4c649a69fdd0fbeefde0fb61ddbe, no folds.
Root authorized local disposable harness preparation only. No code assembly until
PR580 merges, and no production actions in this preparation task.
Worktree C:/projects/gecko-alpha-dashboard-release-20260914 remains based on
production d2f0d61edc63cb55ae159ec952cce404991f21f5; release docs and disposable
tasks/release_* harnesses plus their boundary tests change here.

Root00:58:43UTC runtime evidence, not independently re-attested by this agent:
actual uv run uvicorn dashboard.main:app --host0.0.0.0 --port8000; Restartalways,
KillModemixed,20second stop timeout. No ExecStartPre/Post/Stop/Post, Wants/BindsTo/
PartOf pipeline coupling; Requiresmount/systemslice/sysinit only. The existing
10-telegram-onfailure.conf sends failure alerts and runs guarded auto-remediation.
Remediator only reset-failed/starts the SAME unit; no Git/code patch. It serializes
through /run/codex-remediation/gecko-dashboard.service.lock. Preserve these hooks.

uv sync --check --offline --frozen reports no changes for80packages. UVparent3032127,
uvicornchild3032151, /usr/bin/python3.12, cwd/root/gecko-alpha; module origins/sys.path
point at root checkout without DB initialization. Python3.12.3,aiosqlite0.22.1,
SQLite3.45.1. DB2.7GB/WAL33MB,28GBfree/4521298freeinodes. Baseline envSHA256
59861dd6d6c228be0e4d79205988ea9d098bca476883e3586af03850f44ecd67;
pyprojectbe0bb26a47414ed537461dfdaff26028b60b9a6f45c58003155ceb1a633acecc;
uvlock4f26325d242022e4b62dbe475f88b5fda6dd18d119567e050f100bf70f098ac4.
These are preflight observations, not release-time invariants already satisfied.

## Immutable selective candidate

Wait for merged575/577/578/580 with per-feature terminal reviews and exact feature
GitHub CI. Resolve actual squash SHAs plus final merged master SHA from GitHub and
Git, verifying each ancestor relation. Capture values into a release manifest at
assembly time; missing/unmerged references make verifier fail, never use a moving
branch name as the release identity. Design does not invent final580SHA.

Candidate base is exact production d2f0d61e. Enumerate selected PR parent-to-squash
deltas; retain only explicitly reviewed dashboard source/dist and focused tests/
release documents. Do not replay their intermediate master merges. Every selected
source path must have identical Git blob to final merged master. Every other tracked
path must have identical blob/mode to production base, including ALL scout, scripts,
cron, systemd, .github, .claude, configs, dependencies and unrelated docs. Resolve
conflicts by selecting already-reviewed final dashboard blobs; any novel source
adaptation returns for review rather than importing a new core dependency.

Manifest fields: production_base_sha, final_feature_master_sha, selected_prs with
squash_sha/review/CI links, release_sha, exact allowlist, each selected path's old/
new blob/mode, full nonallowlist-tree digest, dist references/hashes, validation
commands/results, reviewer verdicts and rollback branch/SHA. Release SHA may be
recorded in an external report/sidecar after commit to avoid self-referential hash.
Changing tracked manifest metadata makes a new SHA but must preserve runtime blobs;
verify that explicitly for any later clearance commit.

Build combined frontend in isolated workspace from retained package.json/lock;
require resulting dist byte-identical to final reviewed master. If generated assets
mismatch, investigate/re-review; do not deploy an unexplained rebuild. Confirm every
index.html asset exists and serves. Never let production main.py auto-run npm:
complete dist must already be present. No prod dependency install or lock change.

Keep release branch published/immutable for root to fetch, with report evidence;
do not merge it into product master. Per-feature CI establishes feature correctness;
exact OLD-core local Linux compatibility validation below establishes this selective
combination. No extra GitHub workflow/configuration/operator gate is introduced.

## Safe production-copy preparation (after design approval)

Root creates one dedicated scratch directory, /root/gecko-dashboard-release-20260914,
mode0700, ownedroot, refusing existing symlinks/untrusted permissions. Use an exact
candidate-SHA child; resolve and verify paths remain under this directory. umask077
for DB and logs. Keep it outside /root/gecko-alpha so candidate checkout cannot
collide with tests/data. Before creation require >=12GBfree and >=10000freeinodes;
current28GB is sufficient for baseline + two mutable2.7GBcopies +WAL/headroom.
Abort cleanly if space drops below4GB during preparation. No production file delete.

Create baseline.sqlite using Python sqlite3 online backup API, with source opened
file:/root/gecko-alpha/scout.db?mode=ro, uri=True, timeout=1 and query_only=ON.
Destination is a NEW O_EXCL-created0600 file in validated scratch path. Use
source.backup(destination,pages=256,sleep=0.05,progress=callback). Progress callback
checks monotonic120second deadline and available disk; raise on timeout/lowspace.
Run the backup subprocess with150second outer timeout; terminate only that helper
if unresponsive. Any partial destination is ineligible and may be removed only
after helper exited and exact scratch path revalidated. Never copy the live main
DB directly or checkpoint/modify production WAL. SQLite backup completion supplies
a consistent snapshot even while pipeline writes. Record source/destination sizes,
page counts/start/end/elapsed time without copying token/user records to logs.

Run PRAGMA quick_check on completed backup with30second SQL progress deadline
and45second process cap. Require resultok. Fingerprint sqlite_master(schema SQL,
including indexes/triggers), schema_version/user_version and migration marker rows;
retain the immutable baseline0600copy. Use normal file copies of this CLOSED
baseline to baseline-init.sqlite and candidate-init.sqlite, each0600. No open-copy
cp or production replacement/restore. Maintain checked free space before each copy.
Delete only specifically named scratch copies after verification/report retention;
never enumerate and remove production DB/session files.

## Old-core Linux validation and initialization comparisons

Use separate baseline and candidate source directories in scratch, exported from
exact commits without secrets/configs. Verify all core/dependency hashes and import
origins before execution. Use existing production Python/dependencies without
install/sync changes. Run fixture tests with explicit test-only environment values
in the helper process; do not copy production .env into scratch or overwrite live
environment. Block network in the disposable validation harness so no app import/
GET path can send real messages, vendor probes, orders or callbacks. Paths to the
copy are explicit create_app(db_path), never default scout.db; assert every native
SQLite open resolves beneath scratch and reject production path before connection.

Run actual application construction/startup and all relevant viewer GETs on the
candidate copy. Explicitly exercise an existing GET calling _get_scout_db and its
real OLD Database.initialize; mutation is allowed only on isolated copy. Run the
same initialization/GET sequence on baseline-init.sqlite under baseline code.
Compare schema SQL and migration markers to immutable backup and to each other:
ANY new schema/migration marker stops release; old-core code equality alone is not
proof it is idempotent on current data. Compare control tables (signal_params,
source enable/suspension/config records as identified by initialize trace) before/
after; unexplained operator-state changes stop release. Existing benign bookkeeping
writes, if any, must be equal between baseline and candidate and explicitly
classified; never call this entire application a read-only server.

The four new reader paths must show zero mutations under SQLite authorizer/tracing
on the candidate copy. Check history row/shortfall preservation, postmortem list
25+6pagination when snapshot has31, Telegram outcome grouping/counts and summary
355/23-style current-copy parity; compare against SQL on this copy, never hardcode
historical snapshots as perpetual expected counts. Validate all error/empty states
through fixtures and no paid/action routes. Run focused API/classifier/frontend/
contract/regression suites under candidate source + OLD scout; record exact SHA,
module paths, Python/package versions and commands as local Linux validation.

Process cap180seconds for production-copy app/init comparison; SQL/harness tracing
must prevent any non-scratch DB open and network access. On timeout/failure, stop
only the validation helper, preserve report, no production deployment. Production
backup and init-copy operations never import newer master scout code. Root reviews
copy feasibility and test evidence before release candidate reviewers are dispatched.

## Release serialization and preflight

After source/CI/local validation and two independent candidate reviews, root alone
runs the rollout. Hold the EXISTING remediator lock using fcntl.flock(LOCK_EX|LOCK_NB)
on /run/codex-remediation/gecko-dashboard.service.lock throughout preflight, stop,
switch,start,smokes and any rollback. Verify actual remediator uses same flock inode
and no symlink; open with O_NOFOLLOW and secure ownership. If already held, return
busy without stopping anything; do not remove/truncate lock or disable alerts/
drop-ins. Lock process remains alive until completed/rolled back. Explicit service
stops are intentional; any failure alert remains enabled, while lock prevents
concurrent same-unit start from racing the manual rollback. Lock does not claim
protection from humans/other deployers: refresh owner state and productionHEAD.

Under lock record current production branch/SHA, tracked-clean status, exact
untracked/ignored collision check against changed manifest paths, core tree and
.env/dependency hashes, package sync-check no-change verdict, unit/drop-in hashes,
pipelinePID/start time and schema/migration fingerprints. Any mismatch to approved
baseline makes this release stale; no force checkout/stash/clean/reset. Require
remaining12GBfree preflight (or release-specific documented copy retention footprint)
and valid named rollbackobjects already fetched. No service actions while resolving
a missing object or dirty checkout. Prefetch reviewed Git objects before downtime.

## Dashboard-only rollout, smoke and rollback

1. Stop gecko-dashboard.service only. Wait for inactive/no worker under actual
   20second unit stop limit plus5second observation allowance. Failure to stop
   means abort, no checkout. Do not stop pipeline/Hermes or daemon-reload.
2. Switch clean existing /root/gecko-alpha checkout to recorded release branch/SHA
   using ordinary non-force git switch; verify expected HEAD, clean tracked tree,
   full nonallowlist parity and selected blob/asset manifest before start.
3. Start dashboard only. Poll up to30seconds for active service and /api/status
   returning200 using bounded per-call3second HTTP timeout. Verify new process
   start time, executable,cwd/import paths (source manifest and command), retained
   dependency/env hashes. Any start/smoke failure enters rollback while lock held.
4. GET current history, postmortem list, Telegram outcomes and shortfall summary;
   confirm response metadata, pagination/explicit availability and matching pinned
   read-only SQL snapshot. GET index plus referenced assets. No mutation endpoints.
   Total smoke budget60seconds; retain post-start-only error logs and timestamps.
5. Recheck pipelinePID/starttime, core/config/env/unit hashes, schema/migration
   fingerprint unchanged. Natural pipeline row activity is expected; do not compare
   live fullDBbytes or reset counters. Record old/new dashboardPID, releaseSHA,
   root deployment evidence and source-feature merged/deployed state separately.

On any switch/start/smoke failure: stop dashboard if needed, verify no new tracked
changes or untracked collisions, ordinary switch to saved pre-release branch/SHA,
verify baseline blobs/dist, start dashboard, repeat30second health and basicGET
smoke. Keep remediationlock through rollback. No data restore or schema manipulation;
unexpected schema/control-state mutation requires findings and root escalation,
not destructive repair. If rollback cannot restore health, release lock only after
recording exact state and allowing existing alert/remediation policy to operate;
never leave a detached background cleanup/lock holder silently running.

## Required evidence and next gate

Plan and design approvals do not authorize a premature rollout: each selected
feature merged/CIgreen, immutable selective manifest, old-core/copy checks, two
candidate terminal reviews, fresh preflight and rollback artifacts must exist.
Root's user production-push authorization supplies the deployment permission once
these conditions are satisfied; no additional operator approval ask is invented.

Current result: design approved; local harnesses prepared for inspection. Actual
assembly and Linux copy validation remain pending. See tasks/release_dashboard_usage.md.
No production mutations in this task.
