# Disposable selective-release validation

These helpers do not deploy, restart services, assemble source or change production
configuration. Root must inspect them and supply final immutable PR580/master/release
SHAs after merge. They are disposable release artifacts, not a runtime framework.

## Manifest (local, read-only)

Run `python tasks/release_dashboard_manifest.py --repo <repo> --base
d2f0d61edc63cb55ae159ec952cce404991f21f5 --master <final-master-40SHA>
--pr 575=d5b26ff791ff64dac65d77d2a10a841df1c34007
--pr 577=7cbd82dd962ba3e4ce4ad53be1f7019f71e08307
--pr 578=f55271fcff9a1db19884c2cb5e7a97e032e4b920
--pr 580=<merged-40SHA> --candidate <release-40SHA>` on one command line.
Repeat `--metadata <exact-path>` for each release document/harness/test and
tasks/todo.md. Redirect JSON to a private manifest file outside the source archive.
Without `--candidate` the output is a proposal, explicitly `verified=false`.

The verifier unions only dashboard/tests paths from the four squash deltas,
substitutes their FINAL master blobs (including combined dist), and compares EVERY
other candidate file and mode to production. It also compares the AST of all five
dashboard.db functions imported by operational trade-surface alerts. Feature tests
must include tests/test_trade_surface_tg_alerts.py on the old-core candidate.

## Linux backup (root executes only after inspection and fresh prerequisites)

Use the existing interpreter directly; never dependency sync/install during this
step. Run `timeout --signal=TERM --kill-after=5s 150s /usr/bin/python3.12
<helper-path>/release_dashboard_backup.py --candidate <release-40SHA>`.
Use a private log outside source exports. The CLI fixes the production source and
scratch paths; source opens mode=ro/query_only. It requires exclusive new baseline,
owned 0700 directories, 0600 file, 12GiB free/10,000 inodes before backup, 4GiB floor,
120s backup deadline and 30s quick_check deadline. Nonzero/timeout/partial files
are ineligible; retain for explicit inspection, never reuse/overwrite them.

After success, create baseline-init.sqlite and candidate-init.sqlite beside the
closed baseline.sqlite using exclusive `open(path, 'xb')` and binary copying under
umask077. Never copy the live SQLite file with filesystem copy. Keep baseline.sqlite
untouched. Export clean Git archives of production and the verified candidate into
separate empty private source directories beneath that same candidate scratch root;
no .git, .venv, .env, pycache, extra files, symlinks or untracked import shadows.
Do not export artifacts by copying a developer worktree. Preserve executable modes.

## App / initialization checks (separate processes)

Run each with the existing dependency environment's Python, `PYTHONDONTWRITEBYTECODE=1`,
and outer `timeout --signal=TERM --kill-after=5s 180s`:

```text
python <helper-path>/release_dashboard_validate.py --source-root <scratch>/baseline-source --copy <scratch>/baseline-init.sqlite --manifest <manifest.json> --baseline
python <helper-path>/release_dashboard_validate.py --source-root <scratch>/candidate-source --copy <scratch>/candidate-init.sqlite --manifest <manifest.json>
```

Redirect stdout JSON and stderr logs to separate private files. All source imports
are attested to their archive. The process clears environment secrets, blocks
network and subprocesses, constrains SQLite opens to the one existing mutable copy,
denies ATTACH/DETACH and records DML targets including trigger writes. An existing
GET exercises actual old Database.initialize; subsequent selected GETs run with DML
denied. Summary population and Telegram outcome partition guards supplement HTTP200.

Both copies must match the immutable baseline before initialization. Afterwards
compare all columns and row multiplicity of ten named critical tables, tables with
policy columns, migration metadata and every observed DML target to that immutable
baseline. Output hashes/counts only; never rows. Unexpected change is failure even
if baseline and candidate behave identically. Bookkeeping changes also fail and
require explicit source attribution/review, never blanket acceptance. Overall
180s bounds Python hashing/import/cleanup too; SQL progress deadlines are additional.

These helpers are part of the evidence, not the whole release verdict. Root still
must run exact-candidate old-core focused Linux tests, compare response content and
pagination against a pinned read-only copy query, verify HTML/referenced assets,
inspect logs, obtain two candidate reviews and refresh the design's runtime/rollback
prerequisites. Local Linux evidence is not GitHub CI. No paid calls, dispatch,
execution endpoints or production initialization are allowed in verification.

The production venv has httpx but lacks pytest/pytest_asyncio. Use
`/root/gecko-alpha/.venv/bin/python` for copy app checks, leaving its dependencies
untouched. For focused Linux pytest only, create a PRIVATE scratch environment:
`UV_PROJECT_ENVIRONMENT=<scratch>/testvenv uv sync --project <scratch>/candidate-source
--frozen --all-extras` (one line). Free package downloads may fill cache; no production
venv sync. Snapshot production and testenv `importlib.metadata.distributions()` as
name/version pairs and require every production runtime package version to equal
the testenv counterpart before tests. Archive/source identity must still pass; any
testenv setup files belong outside source. Run the focused suite using that private
Python, recording exact releaseSHA, test command/results and runtime version parity.
This setup/test evidence is local Linux validation, distinct from each merged
feature PR's GitHub CI.

Local preparation: seven boundary tests pass using the existing project Python.
`uv run` could not create a fresh environment because package-index TLS trust failed;
no dependency/configuration changes were made. Production-copy/app validation has
not run, and final merged PR580 SHA remains a required input.
