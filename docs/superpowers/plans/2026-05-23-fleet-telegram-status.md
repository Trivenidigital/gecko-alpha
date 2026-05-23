# Fleet Telegram Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send Telegram fleet status every 8 hours from main-vps and instant Telegram alerts when monitored Codex/Hermes systemd units fail.

**Architecture:** Main-vps owns the 8-hour fleet digest. Each VPS owns local Telegram credentials and a systemd OnFailure hook so failures do not wait for the digest. The digest uses GitHub public events plus local/SSH host probes and formats a concise rolling 7-hour summary.

**Tech Stack:** Python 3 stdlib, GitHub CLI (`gh`) on main-vps, systemd timers/services, Telegram Bot API over HTTPS.

---

### Task 1: Repo-Backed Digest Formatter

**Files:**
- Create: `scripts/codex_fleet_telegram_status.py`
- Create: `tests/test_codex_fleet_telegram_status.py`

- [x] Write tests for rolling 7-hour window math.
- [x] Write tests for GitHub event summary: distinct PRs, PR event count, pushed branches without matching PR events, and just-outside-window PRs.
- [x] Write tests for the operator-facing message shape.
- [x] Implement pure formatter and collector functions.
- [x] Verify with `uv run pytest tests/test_codex_fleet_telegram_status.py -q`.

### Task 2: Fleet Telegram Runtime

**Files:**
- Deploy: `/usr/local/bin/codex-fleet-telegram-status` on `main-vps`
- Deploy: `/etc/codex-telegram.env` on `main-vps`, `vpin-vps`, `srilu-vps`
- Deploy: `/usr/local/bin/codex-telegram-send` on all three VPSes
- Deploy: `/usr/local/bin/codex-systemd-failure-alert` on all three VPSes

- [ ] Copy Telegram bot token/chat id from `srilu-vps:/root/gecko-alpha/.env` into `/etc/codex-telegram.env` on all three VPSes without printing the secret.
- [ ] Install common Telegram sender script with `parse_mode` omitted.
- [ ] Install common systemd failure alert script.
- [ ] Smoke-test Telegram sender with a short message from each VPS.

### Task 3: Main 8-Hour Digest Timer

**Files:**
- Create: `/etc/systemd/system/codex-fleet-telegram-status.service`
- Create: `/etc/systemd/system/codex-fleet-telegram-status.timer`

- [ ] Install the tested digest script on `main-vps`.
- [ ] Schedule at `06:41`, `14:41`, and `22:41` UTC with `Persistent=true`.
- [ ] Run the service once manually and verify Telegram delivery.
- [ ] Verify timer appears in `systemctl list-timers`.

### Task 4: Instant Failure Hooks

**Files:**
- Create: `/etc/systemd/system/codex-systemd-failure-alert@.service`
- Create drop-ins: `/etc/systemd/system/<unit>.d/10-telegram-onfailure.conf`

- [ ] Attach `OnFailure=codex-systemd-failure-alert@%n.service` to Codex/Hermes units on each VPS.
- [ ] Run `systemctl daemon-reload`.
- [ ] Verify drop-ins with `systemctl cat`.
- [ ] Do not intentionally fail production services; verify the alert service can run manually with a synthetic unit name.

### Task 5: Final Verification

- [ ] Run focused local tests again.
- [ ] Verify all three VPSes have Telegram env and sender installed.
- [ ] Verify main digest service succeeds and sends Telegram.
- [ ] Verify failure alert script succeeds manually on all three VPSes.
- [ ] Report any pre-existing timer/systemd warnings separately.
