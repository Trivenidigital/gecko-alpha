**New primitives introduced:** `scripts/systemd-drift-watchdog.sh` (daily drift detector + Telegram alert, modeled on `scripts/gecko-backup-watchdog.sh`); `systemd/systemd-drift-watchdog.service` + `systemd/systemd-drift-watchdog.timer` (daily 09:30 fire — staggered after the 09:00 stale-heartbeat watchdog); `tests/test_systemd_drift_watchdog.py` (bash-script logic verified via fixture-driven harness, OPENSSL-safe).

# Plan: BL-NEW-SYSTEMD-DRIFT-PRECOMMIT-HOOK (cycle 10)

**Backlog item:** `BL-NEW-SYSTEMD-DRIFT-PRECOMMIT-HOOK` (filed 2026-05-17 cycle 6 V35 FOLLOW-UP). Per backlog text, the action's preferred form is option (a) "daily cron on srilu running `scripts/check_systemd_drift.sh` + Telegram alert on DRIFT" — NOT option (b) pre-commit hook, because drift is operator-introduced on the deploy host (post-pull, via `systemctl edit`, or via manual file copy), NOT at commit time on the dev machine.

**Goal:** detect any drift between repo-tracked `systemd/*.{service,timer}` and `/etc/systemd/system/<name>` (plus unexpected drop-ins under `<name>.d/`), and alert via Telegram. Daily cadence.

**Architecture:** Bash watchdog script + systemd timer + service unit (canonical pattern from cycle 6's deploy README's drift-audit loop, now automated). Single-binary check; no DB, no state file beyond the cron timing.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| systemd drift detection / config-in-git enforcement | None — DevOps category, none of the 689 Hermes skills cover unit-file diff or drop-in enumeration | Build in-tree |
| Telegram alert delivery (existing in-tree pattern) | N/A — use existing `gecko-backup-watchdog.sh` curl-direct pattern | Reuse |

awesome-hermes-agent: 404 (consistent prior). **Verdict:** custom in-tree, mirrors `scripts/gecko-backup-watchdog.sh` pattern.

## Drift verdict

NET-NEW. Search:
- `find scripts -name "*systemd*drift*"` → empty
- `find scripts -name "*check_systemd*"` → empty
- `systemd/` README has the audit one-liner but no automation
- 3 existing watchdog scripts (`gecko-backup-watchdog.sh`, `held-position-price-watchdog.sh`, `minara-emission-persistence-watchdog.sh`) cover OTHER concerns; no systemd-drift watchdog exists

No parallel-session interleaving (master HEAD `cf49fc0` post-cycle-9; no commits to scripts/ or systemd/ between cycle 6 merge and now).

## File structure

| File | Type | Responsibility |
|---|---|---|
| `scripts/systemd-drift-watchdog.sh` (NEW) | bash | Compare repo `systemd/*.{service,timer}` to prod `/etc/systemd/system/<name>`; enumerate drop-ins; alert via curl-direct on drift; exit 0 OK / 1 drift-alerted / 4+ misconfig |
| `systemd/systemd-drift-watchdog.service` (NEW) | unit | `Type=oneshot`, runs the bash script |
| `systemd/systemd-drift-watchdog.timer` (NEW) | unit | `OnCalendar=daily *-*-* 09:30:00` — staggered after 09:00 stale-heartbeat watchdog to avoid overlapping alerts |
| `tests/test_systemd_drift_watchdog.py` (NEW) | pytest | Fixture-driven: creates a temp `systemd/`-shaped dir + a temp `/etc/systemd/system/`-shaped dir, runs the script via subprocess with a stub `UV_BIN` (matching the testability seam at gecko-backup-watchdog.sh:27), asserts exit code and alert payload for: clean, drift, drop-in present, env mis-config |
| `systemd/README.md` (MODIFY) | doc | Add entry for the new watchdog in Units table; note that the daily run automates the manual drift-audit one-liner |
| `backlog.md` (MODIFY) | doc | Flip `BL-NEW-SYSTEMD-DRIFT-PRECOMMIT-HOOK` to SHIPPED |

## Hermes skill probe verification

Per CLAUDE.md §7b, the Hermes-first check must be done as evidence not assumption. Probed `hermes-agent.nousresearch.com/docs/skills` (689 indexed skills, 2026-05-17 prior probes in cycles 4/5/6/7/8/9). Domains "DevOps", "MLOps", "infra-as-code" surfaced no systemd / config-drift / unit-file-diff skill. awesome-hermes-agent: 404 (consistent). Custom in-tree justified.

## Tasks (TDD, bite-sized)

### Task 1: failing-first test infrastructure

**Files:**
- Create: `tests/test_systemd_drift_watchdog.py`
- Test: structure mirrors how `tests/test_gecko_backup_watchdog.py` exercises the watchdog (subprocess + `UV_BIN` stub)

- [ ] **Step 1:** write failing test `test_clean_returns_zero` — temp repo dir with `systemd/foo.service` + temp prod dir with byte-identical `/etc/systemd/system/foo.service` + no drop-ins → script exits 0
- [ ] **Step 2:** failing test `test_drift_alerts_via_stub` — content diverges → script exits 1, stub UV_BIN invoked with `stub-watchdog-alert "DRIFT: foo.service"` arg
- [ ] **Step 3:** failing test `test_drop_in_alerts` — `<unit>.d/override.conf` exists → script exits 1, stub invoked with `DROP-IN PRESENT: foo.service.d/`
- [ ] **Step 4:** failing test `test_missing_env_file_exits_4` — `ENV_FILE` doesn't exist, `UV_BIN` empty → exits 4
- [ ] **Step 5:** Run, expect fail (script doesn't exist yet). Commit failing tests.

### Task 2: watchdog script

**Files:**
- Create: `scripts/systemd-drift-watchdog.sh`

- [ ] **Step 1:** Implement following gecko-backup-watchdog.sh template:
  - `set -euo pipefail`
  - `REPO_DIR="${GECKO_REPO:-/root/gecko-alpha}"`
  - `PROD_SYSTEMD_DIR="${PROD_SYSTEMD_DIR:-/etc/systemd/system}"`
  - `ENV_FILE` + `UV_BIN` testability seam
  - Enumerate via `find "$REPO_DIR/systemd" -maxdepth 1 -type f \( -name "*.service" -o -name "*.timer" \) -print0` (matches cycle 6 fix pattern)
  - For each: diff against `$PROD_SYSTEMD_DIR/<name>` + check drop-in dir
  - Collect findings into `$DRIFT_REPORT` string; alert if non-empty
  - Telegram delivery via curl-direct (copy lines 71-113 of gecko-backup-watchdog.sh with the message body adjusted to the drift report)
- [ ] **Step 2:** Run tests, expect pass. Local pytest hangs on OPENSSL (per memory `reference_windows_openssl_workaround.md`) — defer to VPS regression.
- [ ] **Step 3:** Commit `feat(scripts): systemd-drift-watchdog.sh (cycle 10 commit 2/4)`

### Task 3: systemd timer + service units

**Files:**
- Create: `systemd/systemd-drift-watchdog.service`
- Create: `systemd/systemd-drift-watchdog.timer`

- [ ] **Step 1:** Service unit:

```ini
[Unit]
Description=Systemd unit-file drift watchdog for gecko-alpha
After=network.target

[Service]
Type=oneshot
WorkingDirectory=/root/gecko-alpha
ExecStart=/root/gecko-alpha/scripts/systemd-drift-watchdog.sh
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2:** Timer unit:

```ini
[Unit]
Description=Daily timer for systemd-drift-watchdog
Requires=systemd-drift-watchdog.service

[Timer]
OnCalendar=*-*-* 09:30:00
Persistent=true
Unit=systemd-drift-watchdog.service

[Install]
WantedBy=timers.target
```

Schedule: `OnCalendar=daily *-*-* 09:30:00` — 30min after the existing gecko-backup-watchdog timer (09:00) to avoid overlapping Telegram alerts during the morning maintenance window. `Persistent=true` ensures missed fires (e.g., during a service outage) trigger on next boot rather than silently skipping.

- [ ] **Step 3:** Commit `feat(systemd): systemd-drift-watchdog unit + timer (cycle 10 commit 3/4)`

### Task 4: README update + backlog flip + memory checkpoint

**Files:**
- Modify: `systemd/README.md` — add the 2 new files to the Units table; note that daily automation supersedes the manual drift-audit one-liner (the one-liner stays for ad-hoc operator use)
- Modify: `backlog.md` — flip `BL-NEW-SYSTEMD-DRIFT-PRECOMMIT-HOOK` to SHIPPED
- Create: `~/.claude/.../memory/project_systemd_drift_watchdog_deployed_2026_05_17.md`

- [ ] **Step 1:** Update README.md Units table to include the watchdog
- [ ] **Step 2:** Flip backlog entry
- [ ] **Step 3:** Memory checkpoint
- [ ] **Step 4:** Commit `feat(systemd): close BL-NEW-SYSTEMD-DRIFT-PRECOMMIT-HOOK (cycle 10 commit 4/4)`

## Deployment plan (post-merge)

```bash
ssh root@srilu-vps
cd /root/gecko-alpha
git pull
# Cycle-6 fold deploy block applies — find-based cp picks up the 2 new units:
sudo find systemd -maxdepth 1 -type f \( -name "*.service" -o -name "*.timer" \) \
    -exec cp -t /etc/systemd/system/ {} +
sudo systemctl daemon-reload
sudo systemctl enable --now systemd-drift-watchdog.timer

# Smoke test: trigger the watchdog manually
sudo systemctl start systemd-drift-watchdog.service
journalctl -u systemd-drift-watchdog.service --since "1 minute ago" --no-pager
```

Expected first-run output: "OK: 8 unit files match /etc/systemd/system/; 0 drop-ins" (since cycle 6 already aligned repo + prod).

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| False-positive drift alert if a new unit file is committed but not yet deployed | Medium | Low | The 9:30 daily fire gives the operator a 24h window post-PR-merge to deploy before alert. Pattern matches the 48h gecko-backup-watchdog stale-after window's tolerance philosophy |
| Telegram alert overlap with 09:00 stale-heartbeat watchdog | Refuted | — | Staggered to 09:30; 30min gap |
| Watchdog script itself has unit drift (i.e., the service+timer get changed without daemon-reload) | Self-referential | Low | The daily fire will catch its own drift; first-run baseline establishes correctness |
| Permission issue: script reads `/etc/systemd/system/` and needs to | Refuted | — | systemd service runs as root by default (no User=); same as gecko-backup-watchdog.service |
| `UV_BIN` testability seam confused with prod execution | Low | Low | Tests use the same seam pattern that gecko-backup-watchdog.sh uses; not invoked in prod |
| New unit forgotten in repo (operator manually `systemctl edit`s) → watchdog catches it next day | Intended | High-positive | This IS the substrate-finding fix — surfaces the operator-introduced drift that motivated BL-NEW-SYSTEMD-UNIT-IN-REPO |

## Out of scope

- Auto-fix of drift (operator decides repo or prod is source of truth)
- Pre-commit hook variant (option (b) per backlog) — backlog explicitly prefers (a) cron+TG, and (b) would only catch dev-side changes, not the deploy-host-side drift this fix targets
- Watchdog for OTHER untracked config (BL-NEW-OTHER-PROD-CONFIG-AUDIT covers that sweep separately as cycle 11)
- §12a freshness-SLO daemon — that's a separate proposal; this watchdog is the targeted §12a-equivalent for systemd-drift specifically
