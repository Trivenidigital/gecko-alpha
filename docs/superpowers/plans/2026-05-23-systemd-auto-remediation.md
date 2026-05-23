# Systemd Auto-Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic, guarded auto-remediation for failed Codex/Hermes systemd units while fixing the escaped-unit alert bug.

**Architecture:** The existing failure alert script remains the first notification path. A new remediator script performs one safe restart attempt only when the unit is in an exact long-running allowlist, `LoadState=loaded`, `Type=simple|notify`, `UnitFileState=enabled|enabled-runtime`, not already in cooldown, and not concurrently being repaired. Systemd templates pass `%i` so scripts receive the canonical unit name and never the unsafe `%I` slash form.

**Tech Stack:** Python stdlib, systemd one-shot templates, existing `/usr/local/bin/codex-telegram-send`, pytest.

---

### Task 1: Fix Alert Unit Normalization

**Files:**
- Modify: `scripts/codex_systemd_failure_alert.py`
- Modify: `tests/test_codex_telegram_helpers.py`

- [ ] **Step 1: Write failing tests**

Add tests that call the alert helper with `hermes-gateway.service` and with the accidentally unescaped `hermes/gateway.service`, expecting the alert-only unit to normalize to `hermes-gateway.service` so the alert path no longer emits invalid-unit noise.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_codex_telegram_helpers.py -q`

Expected: new normalization tests fail because no normalization helper exists.

- [ ] **Step 3: Implement normalization**

Add a small `normalize_alert_unit_name(unit: str) -> str` helper. For the alert script only, replace `/` with `-` when the value ends with `.service`; otherwise preserve the string. Use the normalized value for alert `systemctl`, `journalctl`, and message display. The remediator in Task 2 must reject slash-containing input instead of normalizing it.

- [ ] **Step 4: Verify GREEN**

Run: `uv run pytest tests/test_codex_telegram_helpers.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

Run: `git add scripts/codex_systemd_failure_alert.py tests/test_codex_telegram_helpers.py && git commit -m "fix: normalize systemd failure alert units"`

### Task 2: Add Remediator Core

**Files:**
- Create: `scripts/codex_systemd_auto_remediate.py`
- Modify: `tests/test_codex_telegram_helpers.py`

- [ ] **Step 1: Write failing tests**

Add tests for:

- disabled unit returns a skipped action
- masked unit returns a skipped action
- unallowlisted enabled unit returns a skipped action
- oneshot enabled unit returns a skipped action
- static, generated/transient, not-found, and bad load states return skipped actions
- slash-containing unit input returns skipped and never calls `reset-failed` or `start`
- handler units return skipped
- cooldown returns a skipped action
- cooldown timestamp is persisted before `reset-failed` / `start`
- unwritable cooldown state fails closed without `reset-failed` / `start`
- concurrent lock returns skipped without mutation
- active-after-start returns success
- still-failed-after-poll returns `needs_operator_action`
- Telegram send failure is audited but does not block `reset-failed`, `start`, and polling
- command order is `show` / cooldown-write / `reset-failed` / `start` / poll
- audit JSONL rows include unit, action, status, reason, and telegram error when present
- messages include no `parse_mode`

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_codex_telegram_helpers.py -q`

Expected: import fails because `scripts.codex_systemd_auto_remediate` does not exist.

- [ ] **Step 3: Implement minimal remediator**

Create `codex_systemd_auto_remediate.py` with injectable command runner, clock, and sender. Use stdlib only. Store cooldown timestamps under a configurable state directory; default `/var/lib/codex-remediation`.

Policy constants:

```python
REPAIR_ALLOWLIST = {
    "hermes-gateway.service",
    "gecko-pipeline.service",
    "gecko-dashboard.service",
    "nginx.service",
    "shift-agent-cockpit.service",
}
ALLOWED_UNIT_FILE_STATES = {"enabled", "enabled-runtime"}
ALLOWED_SERVICE_TYPES = {"simple", "notify"}
HANDLER_PREFIXES = (
    "codex-systemd-failure-alert@",
    "codex-systemd-auto-remediate@",
)
```

Do not normalize slash-containing units. Return `skipped_invalid_unit` before any systemd mutation.

- [ ] **Step 4: Verify GREEN**

Run: `uv run pytest tests/test_codex_telegram_helpers.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

Run: `git add scripts/codex_systemd_auto_remediate.py tests/test_codex_telegram_helpers.py && git commit -m "feat: add guarded systemd auto remediation"`

### Task 3: Record Deployment Templates

**Files:**
- Modify: `tasks/todo.md`
- Modify: `docs/superpowers/plans/2026-05-23-systemd-auto-remediation.md`

- [ ] **Step 1: Update task status**

Add an active-work checklist item for this remediator deployment and record the exact templates:

```ini
[Service]
Type=oneshot
User=root
Group=root
Restart=no
ExecStart=/usr/local/bin/codex-systemd-failure-alert %i
```

```ini
[Service]
Type=oneshot
User=root
Group=root
Restart=no
ExecStart=/usr/local/bin/codex-systemd-auto-remediate %i
```

- [ ] **Step 2: Verify docs**

Run: `git diff --check`

Expected: no whitespace errors.

- [ ] **Step 3: Commit**

Run: `git add tasks/todo.md docs/superpowers/plans/2026-05-23-systemd-auto-remediation.md && git commit -m "docs: plan systemd auto remediation deployment"`

### Task 4: Deploy and Verify

**Files:**
- Deploy: `/usr/local/bin/codex-systemd-failure-alert`
- Deploy: `/usr/local/bin/codex-systemd-auto-remediate`
- Deploy: `/etc/systemd/system/codex-systemd-failure-alert@.service`
- Deploy: `/etc/systemd/system/codex-systemd-auto-remediate@.service`

- [ ] **Step 1: Copy scripts to all VPSes**

Use `scp`, then `chmod 755`.

- [ ] **Step 2: Install templates and drop-ins**

Use `ExecStart=... %i`. Drop-ins contain both alert and remediation:

```ini
[Unit]
OnFailure=codex-systemd-failure-alert@%n.service codex-systemd-auto-remediate@%n.service
```

Do not install any drop-in on `codex-systemd-failure-alert@.service` or `codex-systemd-auto-remediate@.service`.

- [ ] **Step 3: Reload systemd**

Run: `systemctl daemon-reload`.

- [ ] **Step 4: Install state paths**

Run:

```bash
install -d -o root -g root -m 0755 /run/codex-remediation /var/lib/codex-remediation
touch /var/log/codex-remediation.log
chmod 0644 /var/log/codex-remediation.log
```

- [ ] **Step 5: Synthetic verification**

Run the remediator against a disabled synthetic/nonexistent unit and verify it sends a skipped Telegram message without changing real services.

Install disposable verification units on one VPS:

- `codex-remediation-always-fails.service`: exits 1 and proves `OnFailure` launches both handlers with `%i`.
- `codex-remediation-flaky.service`: fails once, then succeeds when remediator runs `reset-failed` + `start`.

Also run:

```bash
systemd-analyze verify /etc/systemd/system/codex-systemd-failure-alert@.service /etc/systemd/system/codex-systemd-auto-remediate@.service
systemctl show codex-systemd-failure-alert@.service -p OnFailure -p Restart --no-pager
systemctl show codex-systemd-auto-remediate@.service -p OnFailure -p Restart --no-pager
systemctl cat hermes-gateway.service
```

- [ ] **Step 6: Focused tests**

Run: `uv run pytest tests/test_codex_telegram_helpers.py tests/test_codex_fleet_telegram_status.py -q`

Expected: all tests pass.

### Task 5: PR and Review

**Files:**
- All changed files

- [ ] **Step 1: Push branch**

Run: `git push -u origin codex/systemd-auto-remediation`.

- [ ] **Step 2: Create PR**

Open PR against `master` with summary, tests, and deployment notes.

- [ ] **Step 3: Dispatch two reviewers**

Reviewer A: systemd/ops safety vector.

Reviewer B: code/test correctness vector.

- [ ] **Step 4: Fold Critical and Important findings**

Fix, test, commit, and push any required changes.
