**New primitives introduced:** `scripts/systemd-drift-watchdog.sh` (daily drift detector + Telegram alert, modeled on `scripts/gecko-backup-watchdog.sh`); `systemd/systemd-drift-watchdog.service` + `systemd/systemd-drift-watchdog.timer` (daily 09:30 UTC fire — staggered after the 09:00 stale-heartbeat watchdog); `tests/test_systemd_drift_watchdog.py` (bash-script logic verified via fixture-driven harness, modeled on `tests/test_backup_rotate_script.py:354-379` — NOT `test_gecko_backup_watchdog.py` which does not exist); ack-tombstone file `/var/lib/gecko-alpha/systemd-drift-watchdog/last_alerted_hash` (V46 MUST-FIX — suppresses re-alert until drift-set hash changes).

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
- Test: structure mirrors `tests/test_backup_rotate_script.py:354-379` (V45 MUST-FIX corrected — `test_gecko_backup_watchdog.py` does NOT exist). Pattern: `_make_uv_stub`, `_run_watchdog`, `_run_watchdog_real_path`, `UV_BIN` PATH seam, marker file at `tmp_path/"alert_marker"`, module-level `pytest.mark.skipif(sys.platform == "win32")`. Write fixture content with `write_bytes(content.encode())` (V45 SHOULD-FIX — avoid CRLF hazard from `Path.write_text` default).

**Expanded test matrix (V45 SHOULD-FIX — 4 → 10 tests):**

- [ ] **Step 1:** `test_clean_returns_zero` — temp repo dir with `systemd/foo.service` + temp prod dir with byte-identical `/etc/systemd/system/foo.service` + no drop-ins → script exits 0
- [ ] **Step 2:** `test_drift_alerts_via_stub` — content diverges → exits 1, stub `UV_BIN` invoked with `stub-watchdog-alert "DRIFT: foo.service"` arg
- [ ] **Step 3:** `test_drop_in_alerts` — `<unit>.d/override.conf` exists → exits 1, stub invoked with `DROP-IN PRESENT: foo.service.d/`
- [ ] **Step 4:** `test_missing_env_file_exits_4` — `ENV_FILE` doesn't exist, `UV_BIN` empty → exits 4
- [ ] **Step 5 (V45 MUST-FIX #2):** `test_multi_unit_drift_reports_all` — TWO units diverge simultaneously → exits 1, alert body contains BOTH unit names (locks in `if ! diff -q` instead of bare `diff -q` under `set -e`)
- [ ] **Step 6 (V45 MUST-FIX #3):** `test_prod_only_unit_alerts` — prod has `/etc/systemd/system/baz.service` with `gecko-*` or `minara-*` prefix; repo has no `systemd/baz.service` → exits 1, alert body contains `UNTRACKED PROD UNIT: baz.service`
- [ ] **Step 7 (V45 SHOULD-FIX):** `test_telegram_http_failure_exits_7` — UV_BIN unset, env file present, but Telegram API mock returns HTTP 503 → exits 7
- [ ] **Step 8 (V45 SHOULD-FIX):** `test_placeholder_token_exits_5` — env file has `TELEGRAM_BOT_TOKEN=placeholder` → exits 5
- [ ] **Step 9 (V45 SHOULD-FIX):** `test_filename_with_spaces` — repo has `systemd/foo bar.service` matching `/etc/systemd/system/foo bar.service` → exits 0 (`find -print0` + `read -d ''` handles spaces)
- [ ] **Step 10 (V45 SHOULD-FIX):** `test_payload_truncation_under_4096` — synthesize 100 fake drifts; alert body is truncated with trailing `(N more drifts truncated — see journalctl)` and total length ≤ 4096 chars
- [ ] **Step 11 (V46 MUST-FIX — ack tombstone):** `test_unchanged_drift_set_suppresses_re_alert` — first run with drift writes hash to tombstone + alerts; second run with SAME drift state reads tombstone, hash matches, exits 1 silently (no stub call)
- [ ] **Step 12 (V46 MUST-FIX — ack tombstone, changed):** `test_changed_drift_set_re_alerts` — first run drifts on `foo.service`; second run drifts on `bar.service` → hash differs, alerts AGAIN
- [ ] **Step 13 (V48 MUST-FIX — stable hash):** `test_stable_hash_under_filesystem_order_perturbation` — create 2 drifts; mock filesystem order to return reverse order on second run; hash must remain identical (regression-locks pre-hash sort)
- [ ] **Step 14 (V48 MUST-FIX — HTTP-fail no-ack):** extend `test_telegram_http_failure_exits_7` to assert `ACK_FILE` is absent post-failure (next-day re-alert intended)
- [ ] **Step 15 (V48 SHOULD-FIX — CLEAN heartbeat):** extend `test_clean_returns_zero` to assert heartbeat-file path exists post-run
- [ ] **Step 16:** Run, expect all 14 fail (script doesn't exist yet). Commit failing tests.

### Task 2: watchdog script

**Files:**
- Create: `scripts/systemd-drift-watchdog.sh`

- [ ] **Step 1:** Implement following gecko-backup-watchdog.sh template:
  - `set -euo pipefail`
  - `REPO_DIR="${GECKO_REPO:-/root/gecko-alpha}"`
  - `PROD_SYSTEMD_DIR="${PROD_SYSTEMD_DIR:-/etc/systemd/system}"`
  - `ENV_FILE` + `UV_BIN` testability seam (matches `scripts/gecko-backup-watchdog.sh:27`)
  - `ACK_DIR="${SYSTEMD_DRIFT_ACK_DIR:-/var/lib/gecko-alpha/systemd-drift-watchdog}"` + `ACK_FILE="$ACK_DIR/last_alerted_hash"`
  - **Direction A: repo-side enumeration** — `find "$REPO_DIR/systemd" -maxdepth 1 -type f \( -name "*.service" -o -name "*.timer" \) -print0 | while IFS= read -r -d '' f`
  - **Direction B (V45 MUST-FIX #3):** prod-side enumeration — `find "$PROD_SYSTEMD_DIR" -maxdepth 1 -type f \( -name "gecko-*.service" -o -name "gecko-*.timer" -o -name "minara-*.service" -o -name "minara-*.timer" -o -name "systemd-drift-watchdog.*" \) -print0 | while IFS= read -r -d '' p` — flags any prod-side file with no repo counterpart as `UNTRACKED PROD UNIT: <name>` (this catches operator-introduced units via `systemctl edit --full new.service`)
  - **V45 MUST-FIX #2 — `set -e × diff`:** use `if ! diff -q "$f" "$PROD_SYSTEMD_DIR/$name" >/dev/null 2>&1; then`, NOT bare `diff -q ...`. Without this guard, `set -e` kills the loop on the first drift and subsequent units are never checked
  - **V45 SHOULD-FIX — drop-in idiom:** use `compgen -G "$PROD_SYSTEMD_DIR/${name}.d/*.conf" >/dev/null 2>&1` to detect drop-ins. Pin this idiom verbatim (matches `systemd/README.md` line 53). Empty `.d/` dir → false → CLEAN (not drift)
  - Collect findings into `$DRIFT_REPORT` string (newline-separated)
  - **V46 MUST-FIX — ack tombstone:** compute `$DRIFT_HASH = sha256sum <<< "$DRIFT_REPORT"`; read `$ACK_FILE` if it exists; if hashes match → exit 1 silently (no Telegram, but journalctl logs `silent_suppress_same_drift_set hash=$DRIFT_HASH`); if hashes differ OR ACK_FILE absent → fire alert + write new hash to ACK_FILE. **Reset on CLEAN:** if `$DRIFT_REPORT` is empty, delete `$ACK_FILE` so next drift after a clean-state re-alerts. Operator can manually clear via `rm $ACK_FILE` to force re-alert on stable drift
  - **V45 SHOULD-FIX — payload truncation:** if `${#DRIFT_REPORT}` > 3500, truncate to `${DRIFT_REPORT:0:3500}\n(N more drifts truncated — see journalctl -u systemd-drift-watchdog)`. Total `$TEXT` body cap ≤ 4000 to keep Telegram-margin
  - Telegram delivery via curl-direct (mirror `scripts/gecko-backup-watchdog.sh:71-113` verbatim — same env-extraction, JSON-encoder, `HTTP_STATUS != 200` exit 7)
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

## Deployment plan (post-merge) — V46 MUST-FIX #1: chicken-and-egg gate

```bash
ssh root@srilu-vps
cd /root/gecko-alpha
git pull

# V46 MUST-FIX — first-run self-drift gate. Verify the new watchdog
# units match prod BEFORE enabling the timer. Without this gate, a
# partial cp (e.g., service deployed but timer not, or daemon-reload
# skipped) makes the very first fire self-alert.
sudo find systemd -maxdepth 1 -type f \( -name "*.service" -o -name "*.timer" \) \
    -exec cp -t /etc/systemd/system/ {} +
sudo systemctl daemon-reload

# Pre-flight: verify watchdog units exist + match prod (the new ones
# are byte-identical to repo since cp just happened).
for f in systemd/systemd-drift-watchdog.service systemd/systemd-drift-watchdog.timer; do
    name=$(basename "$f")
    diff -q "$f" "/etc/systemd/system/$name" || { echo "ABORT: $name failed pre-flight"; exit 1; }
done

sudo systemctl enable --now systemd-drift-watchdog.timer

# Smoke test: trigger the watchdog manually
sudo systemctl start systemd-drift-watchdog.service
journalctl -u systemd-drift-watchdog.service --since "1 minute ago" --no-pager
```

**Timezone (V46 SHOULD-FIX):** `OnCalendar=*-*-* 09:30:00` is systemd-local-time = UTC on srilu. Resolves to 04:30 EST / 05:30 BST / 15:00 IST. Telegram is push so delivery latency is zero regardless of operator local TZ.

**Expected first-run output:** "OK: 8 unit files match /etc/systemd/system/; 0 drop-ins; 0 untracked prod units" (since cycle 6 already aligned repo + prod, AND the new watchdog units pre-flight passed).

**Operator override:** `rm /var/lib/gecko-alpha/systemd-drift-watchdog/last_alerted_hash` to force re-alert on the next fire if the drift state is stable but the operator wants the reminder back.

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| False-positive drift alert if a new unit file is committed but not yet deployed | Medium | Low | 9:30 daily fire gives operator 24h window post-PR-merge to deploy before alert. Pattern matches the 48h gecko-backup-watchdog stale-after window's tolerance philosophy |
| Telegram alert overlap with 09:00 stale-heartbeat watchdog | Refuted | — | Staggered to 09:30; 30min gap |
| Watchdog script itself has unit drift (i.e., the service+timer get changed without daemon-reload) | Self-referential | Low | Daily fire catches its own drift; V46 MUST-FIX deployment pre-flight gate catches first-run case |
| Permission issue: script reads `/etc/systemd/system/` and needs to | Refuted | — | systemd service runs as root by default; same as gecko-backup-watchdog.service |
| `UV_BIN` testability seam confused with prod execution | Low | Low | Tests use the same seam pattern as `test_backup_rotate_script.py`; not invoked in prod |
| New unit forgotten in repo (operator manually `systemctl edit`s) → watchdog catches it next day | Intended | High-positive | This IS the substrate-finding fix — surfaces the operator-introduced drift that motivated BL-NEW-SYSTEMD-UNIT-IN-REPO. Direction-B prod-side enumeration (V45 MUST-FIX #3) catches the prod-only case |
| **V46 MUST-FIX — daily-fire-until-fixed alert fatigue** | Refuted via ack tombstone | — | Hash-based ack tombstone: same drift-set silently suppressed; changed drift-set re-alerts. Operator can force re-alert via `rm $ACK_FILE` |
| **V46 SHOULD-FIX — co-firing redundancy** when gecko-backup-watchdog ITSELF drifts | Low | Low | Both alerts on same root cause is acceptable for cycle 10. If observed in practice, file `BL-NEW-WATCHDOG-ALERT-DEDUP` follow-up |
| **V46 SHOULD-FIX — watchdog-can't-run silent failure** (exits 4/5/6/7 are journalctl-only) | Low | Medium | The 09:00 gecko-backup-watchdog itself does NOT monitor other watchdogs; this gap is structural. **Follow-up:** file `BL-NEW-WATCHDOG-META-WATCHDOG` to add `systemd-drift-watchdog.service` to a monitored-unit set. Out of scope for this PR — fixing it requires the §12a daemon work |
| **V46 SHOULD-FIX — TG rate-limit** at 09:00-09:30 window if multiple watchdogs co-fire | Low | Low | Cycle 3 BL-NEW-TG-BURST-PROFILE is already observability for this; rate-limit measurement window already in flight (decision-by 2026-06-14) |
| **V45 MUST-FIX #2 — first drift kills script under `set -e`** | Refuted | — | Spec mandates `if ! diff -q ...` instead of bare `diff -q` in the loop; `test_multi_unit_drift_reports_all` regression-locks this |
| **V45 MUST-FIX #3 — prod-only unit invisible to watchdog** | Refuted | — | Direction-B `find $PROD_SYSTEMD_DIR` enumeration with `gecko-*` / `minara-*` / `systemd-drift-watchdog.*` prefix filter. `test_prod_only_unit_alerts` regression-locks |
| **V45 SHOULD-FIX — `compgen -G` empty `.d/` dir false-positive** | Refuted | — | `compgen -G` returns false on empty dir; empty `.d/` is CLEAN, not DRIFT. Pinned in spec |
| **V45 SHOULD-FIX — Telegram payload > 4096 chars** | Low | Low | Truncate at 3500 chars + `(N more drifts truncated)` footer. `test_payload_truncation_under_4096` regression-locks |
| **V45 SHOULD-FIX — CRLF hazard on test fixtures** | Low | Low | Test harness mandates `write_bytes(content.encode())` or `newline="\n"`. Existing module-level `pytest.mark.skipif(sys.platform == "win32")` keeps tests on Linux CI/VPS where Python defaults to LF anyway |

## Out of scope

- Auto-fix of drift (operator decides repo or prod is source of truth)
- Pre-commit hook variant (option (b) per backlog) — backlog explicitly prefers (a) cron+TG, and (b) would only catch dev-side changes, not the deploy-host-side drift this fix targets
- Watchdog for OTHER untracked config (BL-NEW-OTHER-PROD-CONFIG-AUDIT covers that sweep separately as cycle 11)
- §12a freshness-SLO daemon — that's a separate proposal; this watchdog is the targeted §12a-equivalent for systemd-drift specifically
