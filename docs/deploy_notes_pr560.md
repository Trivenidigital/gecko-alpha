## DEPLOY NOTES — PR #560 (required reading before the next prod pull)

**`git pull` alone leaves part of this PR inert.** Two unit files changed, and
systemd reads `/etc/systemd/system`, not the repo:

    cd /root/gecko-alpha && git pull
    install -m 0644 scripts/recompute-coverage-watchdog.{timer,service} /etc/systemd/system/
    install -m 0644 systemd/systemd-drift-watchdog.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl list-timers recompute-coverage-watchdog.timer   # verify still armed

Skip the install and the new `ExecStartPre=/usr/bin/test -x` preflights are
present in the diff, green in CI, and absent from what systemd executes.

**Three deploy mechanics, verified on the box:**

| artefact | arrives by | in this PR |
|---|---|---|
| `.sh` invoked by `ExecStart` | `git pull` | mode 100644 -> 100755 (2 files) |
| `.py` | `git pull` | test-only |
| `.service` / `.timer` | **`install` + `daemon-reload`** | **2 units gained ExecStartPre** |

**Verification status, per the ops-safety slot's convention:**
- mode changes: **fix verified** by fresh clone of the branch on the target host
  (`-rwxr-xr-x`, `test -x` OK, no chmod required); **not yet on prod** — deployed
  checkout is `cdbb847`.
- `ExecStartPre` additions: **verified in repo only.** Nobody has verified them
  on the box, and they cannot be until the install above is run.

---

## Known limitation of this session's PROSE summaries

The per-commit deltas I described to reviewers in messages drifted from the
actual diff **four times**. Three were harmless: an omitted `backlog.md`, a file
*create* described as a *rewrite*, two mode changes attributed to the wrong
commit.

**The fourth was material.** I described a commit as "two changes, both docs"
when it was four files including a `+64/-9` hardening of
`tests/test_watchdog_ships_executable.py`. A reviewer who trusted it would have
run a blob-identity check, seen only docs move, and never mutated the unit scan
— missing both that the new guard bites and that one component of it does not.

**The durable instruction is not "distrust the prose".** In all four cases the
`git diff` was correct, and specifically the **merge-base** diff:

> Measure from the merge base. That is the axis the deploy actually
> experiences, and it is immune to how the work was sliced into commits.
> Incremental deltas are a property of the authoring process; merge-base
> deltas are a property of what ships.

Every reviewer claim on this PR that was anchored to `cdbb8475..HEAD` or to the
box was untouched by all four drifts. The one discipline that caught the
material drift was a per-file blob-identity check — which is itself a diff.
