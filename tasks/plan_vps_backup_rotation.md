**New primitives introduced:** `scripts/gecko-backup-rotate.sh` (bash script), `scripts/gecko-backup-watchdog.sh` (bash watchdog — R1 fix), `systemd/gecko-backup.service`, `systemd/gecko-backup.timer`, `systemd/gecko-backup-watchdog.service`, `systemd/gecko-backup-watchdog.timer`, `docs/runbook_backup_rotation.md`, heartbeat file `/var/run/gecko-backup-last-ok`. No DB schema changes, no Settings additions, no migration. (Telegram alerter is reused from `scout.alerter` — wired post-2026-05-06.)

# Plan — VPS backup rotation automation

## Hermes-first analysis

Mandatory per CLAUDE.md §7b. Checked the Hermes skill hub at `hermes-agent.nousresearch.com/docs/skills` (2026-05-09).

| Domain | Hermes skill found? | Decision |
|---|---|---|
| DevOps / SQLite backup retention | None found in 18-domain catalog ("DevOps" is generic infra automation, no SQLite-rotation skill) | Build from scratch — bash script + systemd timer |
| File-rotation utility (logrotate-style) | None found | Build from scratch — `find -mtime` is sufficient |
| Backup encryption / S3 upload | Not in scope (would be Phase 2) | Defer — local rotation is the immediate gap |

awesome-hermes-agent ecosystem check: no community skill for SQLite backup rotation. Verdict: **build from scratch is correct**.

**Sister-project pattern reuse:** `shift-agent` on the same VPS already uses a `systemd-service` + `systemd-timer` + `bash-script` triad (`/etc/systemd/system/shift-agent-backup.{service,timer}`, `/usr/local/bin/shift-agent-backup.sh`). The gecko-alpha implementation **borrows the architecture only** — the shift-agent script is heavy (GPG / S3 / YAML config / tail-logger pause / Pushover notification), all out-of-scope for the immediate gap. The gecko-alpha script will be ~30 lines.

## Drift-check (per CLAUDE.md §7a)

Verified 2026-05-09 against current tree (commit `0a50abc`):

1. `find . -name "*.sh" -path "*/scripts/*"` — repo has no shell scripts (only `scripts/backtest_*.py` and `scripts/bl060_*.py`).
2. `find . -name "*backup*" | grep -v __pycache__ | grep -v .venv` — no backup primitives in repo.
3. VPS `ls /etc/systemd/system/ | grep -E "gecko|backup"` returns only `gecko-pipeline.service` + `gecko-dashboard.service` — **no backup timer for gecko-alpha**.
4. VPS `crontab -l` returns only an unrelated polymarket-extract job — **no cron-based gecko backup**.
5. Memory `feedback_vps_backup_rotation.md` confirms operator-flagged but unautomated — disk hit 100% during BL-076 deploy AND again during BL-NEW-QUOTE-PAIR deploy today (2026-05-09T16:38Z, same incident pattern).

Drift verdict: **net-new**, no in-tree match, demonstrably needed (incident recurred today).

## Why this matters

`feedback_vps_backup_rotation.md` was elevated to operator memory after the BL-076 deploy (2026-05-04) when disk hit 100% from accumulated `scout.db.bak.*` files (8 backups × ~4.5GB). It recurred today — the same condition blocked PR #85's `git pull`, requiring manual cleanup mid-deploy. Two incidents in five days; needs automation.

Backup files have grown from ~640MB (April) to ~4.5GB (May) — 7× over four weeks. At current growth, every two manual deploys produces ~9GB of backup material. The 75GB VPS disk has < 10GB headroom.

## What's in scope

1. **Bash script `scripts/gecko-backup-rotate.sh` (post R1+R2 reviewer fixes):**
   - Strict mode: `set -euo pipefail`.
   - **Required env `GECKO_BACKUP_DIR`** — script aborts with explicit error if unset OR doesn't exist (R1 MUST-FIX: no baked-in default; on a different VPS the wrong default would silently rotate the wrong dir).
   - **Optional env `GECKO_BACKUP_KEEP`** — default 3.
   - **Single unified `find` invocation** combining both name patterns under one mtime sort (R2 MUST-FIX: separate-bucket pattern would keep N=3 from each = up to 6 files; here all matches go through one sort+slice):
     ```bash
     find "$GECKO_BACKUP_DIR" -maxdepth 1 -type f \
         \( -name 'scout.db.bak.*' -o -name 'scout.db.bak-*' \) \
         -printf '%T@ %p\n'
     ```
   - **`mapfile` into bash array, then slice** (R2 MUST-FIX: pipeline `sort | tail -n +N | xargs rm` triggers SIGPIPE under `pipefail`. `mapfile` consumes the full subshell first, no open pipe):
     ```bash
     mapfile -t files < <(find ... -printf '%T@ %p\n' | sort -rn | cut -d' ' -f2-)
     to_delete=("${files[@]:$GECKO_BACKUP_KEEP}")
     [[ ${#to_delete[@]} -gt 0 ]] && rm -v -- "${to_delete[@]}"
     ```
   - **`cut -d' ' -f2-` not `awk '{print $2}'`** (R2 NIT: awk with single-space delimiter splits filenames containing spaces; `cut` with field-2-onwards preserves them).
   - **No bare globs** — `find` handles empty-dir natively returning nothing (R2 MUST-FIX: bare glob `for f in pat*` on empty dir loops once over the literal pattern under `set -u` semantics, then `rm` fails on a literal "scout.db.bak.*" file).
   - **Heartbeat write on success** (R1 MUST-FIX: silent-failure observability gap given recurring incident — see watchdog spec below).
   - Returns 0 on success even if zero backups present (idempotent on fresh deploys).
   - All `rm` paths use absolute paths returned by `find -printf '%p'` — no `cd` games. The `-maxdepth 1` + dir-anchored `find` is the safety boundary.

2. **systemd unit `systemd/gecko-backup.service`:**
   - `Type=oneshot`, `User=root`, `Group=root`.
   - `ExecStart=/usr/local/bin/gecko-backup-rotate.sh`.
   - No `EnvironmentFile=` (defaults are baked into the script, env override available for testing).
   - `TimeoutStartSec=120` — script should complete in seconds.

3. **systemd unit `systemd/gecko-backup.timer`:**
   - `OnCalendar=*-*-* 03:00:00` (3am UTC daily — outside the high-traffic 13:00-23:00 UTC window when CG/DS/GT are most active).
   - `Persistent=true` — runs on next boot if missed (post-reboot catch-up).
   - `AccuracySec=1h` (R1 MUST-FIX: damps Persistent=true into a smeared-1h window so a boot that just-missed 03:00 doesn't fire seconds-later in surprise).
   - `Unit=gecko-backup.service`.
   - `[Install] WantedBy=timers.target`.

3.5. **Watchdog primitive (R1 MUST-FIX — closes silent-failure observability gap):**
   - **`scripts/gecko-backup-watchdog.sh`:** reads `/var/run/gecko-backup-last-ok` heartbeat file; if missing OR mtime > 48h ago, emit Telegram alert via `scout.alerter.send_telegram_message` (already wired post-2026-05-06) AND log structured `gecko_backup_watchdog_stale` event.
   - **`systemd/gecko-backup-watchdog.service`:** Type=oneshot, runs `gecko-backup-watchdog.sh`.
   - **`systemd/gecko-backup-watchdog.timer`:** `OnCalendar=*-*-* 09:00:00` (6h after main timer — gives main timer a wide window to complete + handles boot-window edge cases). Daily check.
   - **Heartbeat file write:** rotation script writes `date +%s` to `/var/run/gecko-backup-last-ok` ONLY on successful exit (last line of script). Watchdog reads + compares against `now - 48h`. Failure modes the watchdog catches: ExecStart binary missing (no exec → no heartbeat), `set -e` early bail (no heartbeat), unit got disabled but operator forgot to re-enable, bad config preventing dir access.
   - Rationale per R1: Phase-2 deferral was wrong given the incident recurred twice in 5 days. ~10 LOC of bash + 2 systemd units closes the gap.

4. **Runbook `docs/runbook_backup_rotation.md`:**
   - One-time install steps (copy script + systemd units, enable timer).
   - Operator-test command (`systemctl start gecko-backup` on demand).
   - How to verify last-run via `journalctl -u gecko-backup.service`.
   - How to disable / change retention (env override).
   - Phase 2 future-work pointer (encryption, S3 offsite — explicitly NOT in scope).

5. **Tests** — bash script gets a pytest harness in `tests/test_backup_rotate_script.py`:
   - Creates a tmp dir with N=5 fake `scout.db.bak.*` files at staggered mtimes.
   - Runs the script with `GECKO_BACKUP_DIR=tmp` + `GECKO_BACKUP_KEEP=3`.
   - Asserts top-3-by-mtime survive; bottom 2 are gone; exit code 0.
   - Asserts re-running on the trimmed dir is a no-op (idempotent).
   - Asserts running on an empty dir is a no-op (no error, exit 0).
   - Asserts running with `GECKO_BACKUP_KEEP=0` deletes everything (operator escape hatch).
   - Asserts both naming patterns (`bak.<tag>.<unix>` AND `bak-<iso>`) participate in **a single unified sort** — mix 4 of pattern A + 4 of pattern B at staggered mtimes, KEEP=3, assert exactly 3 survive (R2 MUST-FIX regression-lock against the separate-bucket bug).
   - Asserts `GECKO_BACKUP_DIR` unset → script exits non-zero with clear error (R1 MUST-FIX).
   - Asserts `GECKO_BACKUP_DIR` set to non-existent path → script exits non-zero (R1 MUST-FIX).
   - Asserts pathological filename with embedded space (e.g., `scout.db.bak. extra-tag`) is correctly preserved/rotated by mtime ordering (R2 NIT — locks the `cut -d' ' -f2-` choice over `awk '{print $2}'`).
   - Asserts heartbeat file `/var/run/gecko-backup-last-ok` is written on success (override path via env `GECKO_BACKUP_HEARTBEAT_FILE` for testability) (R1 MUST-FIX).
   - Asserts heartbeat file is NOT updated on failure (e.g., when `GECKO_BACKUP_DIR` doesn't exist).
   - Asserts symlink-following is disabled — a symlink matching the glob doesn't get followed (`find ... -type f` excludes symlinks; defensive against operator creating one accidentally).

## What's out of scope

- **Encryption (GPG / age).** Backups stay plaintext-on-disk. Phase 2 work; the immediate gap is rotation, not confidentiality.
- **Offsite upload (S3 / Backblaze).** Same — plaintext local-only is the minimum-viable fix.
- **Pre-deploy backup hook.** Existing manual `cp scout.db scout.db.bak.<tag>` pattern still in operator hands. Adding a deploy hook would require deeper deploy-tooling integration; out of scope for this PR.
- **Backup integrity verification.** No `sqlite3 ... PRAGMA integrity_check` on rotated files. Acceptable risk: a corrupt backup is no worse than no backup.
- **Retention by size (e.g., "keep most-recent 30GB").** Count-based (`KEEP=3`) is sufficient for the observed 4.5GB/file × 3 = 13.5GB ceiling.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Glob pattern mismatch — a backup named `scout.db.backup-foo` gets ignored and accumulates | Plan covers both observed naming conventions: `scout.db.bak.*` AND `scout.db.bak-*`. Any future variant that introduces a third name pattern needs script update. |
| Script deletes a live `scout.db` by mistake | Glob is bounded to `scout.db.bak.*` AND `scout.db.bak-*`; live `scout.db` does not match either. Tests assert this. |
| Timer fires during a manual operator backup-in-flight | `find -mtime +<seconds>` would skip it because it's freshly-modified; rotation operates on completed files only. Add a 5-minute `mtime` floor to be safe. Actually simpler: sort by mtime descending and keep top N — by definition the in-flight backup is the newest, so it's preserved as #1. |
| Disk fills BEFORE 03:00 next run | Operator can manually trigger via `systemctl start gecko-backup.service`. Runbook documents this. Phase 2 idea: trigger on disk-pressure threshold via an inotify watcher; not in scope. |
| Script accidentally deletes shift-agent backups (sister project, same VPS) | Glob is anchored to `scout.db.bak*` — shift-agent backups live in `/opt/shift-agent/backups/` as `*.tar.gz.gpg` and never match. Path scoping further protects (script CDs to `$GECKO_BACKUP_DIR`). |
| systemd timer fails silently | `journalctl -u gecko-backup.service` shows last-run; runbook documents the check command. Phase 2 idea: alert if last-run > 48h ago. |
| Operator wants different retention (e.g., 5 not 3) | Override via `GECKO_BACKUP_KEEP=N` env var on the systemd service `Environment=` line; runbook documents. |

## Tasks

1. Write `scripts/gecko-backup-rotate.sh` (bash, ~30 lines) — strict mode, glob, sort by mtime, head -N preserve, rm rest.
2. Write `systemd/gecko-backup.service` + `systemd/gecko-backup.timer`.
3. Write `tests/test_backup_rotate_script.py` (pytest) — 5 cases per acceptance criteria.
4. Write `docs/runbook_backup_rotation.md` — install + ops + revert.
5. Manual VPS install: copy script to `/usr/local/bin/`, copy units to `/etc/systemd/system/`, enable + start timer, verify first run.

## Acceptance criteria

- `tests/test_backup_rotate_script.py` passes (5 cases).
- `bash -n scripts/gecko-backup-rotate.sh` (syntax-check) clean.
- `shellcheck scripts/gecko-backup-rotate.sh` clean (if shellcheck installed; else manual review).
- VPS install verified: `systemctl status gecko-backup.timer` shows `enabled` + `active (waiting)`; `systemctl list-timers` shows next-fire time.
- Manual trigger (`systemctl start gecko-backup.service`) on a VPS with > 3 backups produces correct rotation; `journalctl -u gecko-backup.service` shows the `rm -v` lines.
- Disk usage drops; `df -h /` shows freed space proportional to deleted backups.

## Soak + revert plan

- Default: timer runs daily at 03:00 UTC. First fire on next 03:00 after install.
- Revert: `systemctl disable --now gecko-backup.timer && systemctl stop gecko-backup.service && rm /usr/local/bin/gecko-backup-rotate.sh /etc/systemd/system/gecko-backup.{service,timer}`.
- Soak window: D+3 (after first 3 nightly runs). Operator verifies 3 backups on disk, 4th got rotated.
- If timer mis-fires or rotates the wrong files: stop timer immediately, manual `cp` of any recoverable backups, debug script.

## Reviewer dispatch (plan-stage, 2 parallel)

- **R1 (operational / ops-discipline):** Is the systemd timer model the right shape vs cron? What happens if the script fails halfway? What's the failure mode if `find` returns paths with spaces / newlines (none are expected, but is the glob+rm pattern robust)? What permissions does the script need (root)? Are there race conditions with the operator running a manual `cp scout.db scout.db.bak.X` while the timer fires?
- **R2 (correctness / bash safety):** Audit the glob pattern + sort-by-mtime + head -N pattern. Is the script POSIX-portable or bash-specific? Does `set -euo pipefail` interact correctly with `find ... | head` SIGPIPE? What happens with zero matching files?
