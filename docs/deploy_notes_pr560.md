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
