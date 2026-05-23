**New primitives introduced:** `codex-systemd-auto-remediate@.service`, `/usr/local/bin/codex-systemd-auto-remediate`, per-unit cooldown state under `/var/lib/codex-remediation`, exact repair allowlist for long-running services.

# Systemd Auto-Remediation for Codex/Hermes VPS Units

## Problem

The current `OnFailure=` path sends Telegram but does not repair. It also passes `%n` into a systemd template and the alert service uses `%I`, so `hermes-gateway.service` is unescaped as `hermes/gateway.service`; `systemctl` then reports an invalid unit name inside the alert. The result is noisy failure reporting and no autonomous recovery.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Systemd failure repair | none found in Hermes Skills Hub DevOps category for systemd unit repair | Build from scratch: repair is host-local and must be deterministic, auditable, and systemd-aware. |
| Agent orchestration | yes — `kanban-orchestrator`, `kanban-worker`, `kanban-codex-lane` in Hermes Skills Hub | Defer heavy investigation/escalation to Hermes/Codex after deterministic repair fails; do not put LLM reasoning on the critical restart path. |
| Telegram delivery | none needed; existing repo helper `/usr/local/bin/codex-telegram-send` already sends plain-text Telegram | Reuse existing helper, no custom Telegram implementation. |
| GitHub/PR workflow | yes — Hermes Skills Hub lists GitHub workflow/code review skills | Keep future repo-changing repairs as an escalation path, not part of the first safe remediator. |

Awesome Hermes Agent ecosystem check: checked `awesome-hermes-agent`; no specific systemd auto-remediation primitive found. Verdict: use Hermes/Codex as an escalation/investigation layer later, while shipping a local deterministic repair primitive now.

Sources checked: `https://hermes-agent.nousresearch.com/docs/skills` and `https://github.com/0xarkstar/awesome-hermes-agent`.

## Drift check

Existing in tree:

- `scripts/codex_systemd_failure_alert.py` sends Telegram alerts for systemd failures.
- `scripts/codex_telegram_send.py` is the plain-text Telegram transport.
- `docs/runbooks/live-trading-deploy.md` documents manual `systemctl reset-failed ... && systemctl start ...`.
- `docs/superpowers/plans/2026-05-23-fleet-telegram-status.md` documents current `OnFailure=codex-systemd-failure-alert@%n.service`.

No existing general remediator or cooldown state machine was found.

## Design

`OnFailure=` should trigger two one-shot units:

```ini
OnFailure=codex-systemd-failure-alert@%n.service codex-systemd-auto-remediate@%n.service
```

Both templates should pass `%i` to their scripts, not `%I`. `%i` preserves the unit name as `hermes-gateway.service`; `%I` is what produced the unsafe `hermes/gateway.service` string. The remediator treats raw `%i` as canonical and rejects any unit containing `/`. The alert script may recover legacy `foo/bar.service` into `foo-bar.service` for alert display/status only, but the repair path never mutates systemd state from slash-containing input.

The remediator is intentionally narrow:

1. Acquire a nonblocking per-unit lock under `/run/codex-remediation/<escaped-unit>.lock`.
2. Load policy from CLI defaults:
   - one attempt per unit per 30 minutes
   - no repair when unit is not in the exact long-running allowlist:
     - `hermes-gateway.service`
     - `gecko-pipeline.service`
     - `gecko-dashboard.service`
     - `nginx.service`
     - `shift-agent-cockpit.service`
   - no repair when unit is `masked`, `disabled`, `static`, `indirect`, `generated`, `transient`, `bad`, `not-found`, or any state other than `enabled` / `enabled-runtime`
   - no repair unless `systemctl show` reports `LoadState=loaded` and `Type=simple` or `Type=notify`
   - no repair when unit name is not a `.service`
   - no repair for handler units (`codex-systemd-failure-alert@*.service`, `codex-systemd-auto-remediate@*.service`)
3. Send Telegram: repair attempt started/skipped.
4. After acquiring the lock, atomically persist the attempt timestamp before mutating systemd state. If cooldown state cannot be read or written, fail closed: send/log `skipped_state_unavailable` and do not restart.
5. Run `systemctl reset-failed <unit>`.
6. Run `systemctl start <unit>`.
7. Poll up to 60 seconds for `systemctl is-active <unit> == active`.
8. Send Telegram: repair succeeded or `needs_operator_action`, with status and recent journal tail.
9. Append a JSONL audit row to `/var/log/codex-remediation.log`.

Telegram send failures must never block deterministic repair. The remediator catches sender exceptions, records them in audit/log output, and continues through reset/start/poll.

## Guardrails

- Never unmask or enable a unit automatically in V1.
- Never repair non-service units.
- Never repair one-shot Codex automation services; failed one-shots alert only because re-running them may repeat side effects.
- Never repair handler units or attach `OnFailure=` to handler templates.
- Explicitly set `Restart=no` on handler templates.
- Never run more than one repair for the same unit concurrently.
- Never retry more than once per cooldown window.
- Telegram messages must omit `parse_mode`.
- Failed/skipped repair must be operator-visible.
- The alert script must stop producing `hermes/gateway.service` invalid-unit noise.

## Deployment scope

Install on all 3 VPSes:

- `/usr/local/bin/codex-systemd-failure-alert`
- `/usr/local/bin/codex-systemd-auto-remediate`
- `/etc/systemd/system/codex-systemd-failure-alert@.service`
- `/etc/systemd/system/codex-systemd-auto-remediate@.service`
- drop-ins for the monitored Codex/Hermes units
- install-time creation of `/run/codex-remediation`, `/var/lib/codex-remediation`, and `/var/log/codex-remediation.log` with root ownership

V1 auto-repair units are only the exact long-running allowlist above. Codex automation one-shots/timers remain alert-only unless a later design proves a specific idempotent restart contract.

## Verification

- Unit tests cover unit normalization, message formatting, disabled/masked skip policy, cooldown skip, active success, and failed escalation.
- Unit tests also cover slash-containing remediator input never mutates systemd, unallowlisted/oneshot/static/not-found units skip, Telegram sender failure does not block repair, command order, cooldown persistence before mutation, lock behavior, audit row content, and handler-unit denylist.
- Local focused pytest passes.
- On VPS, disposable verification units prove real `OnFailure` template rendering:
  - a failing service launches both handlers with `%i` and does not produce slash-mangled unit names
  - a controlled service fails once then succeeds on remediator `reset-failed` + `start`
- On VPS, template files show `ExecStart=... %i`.
- `systemd-analyze verify`, `systemctl show -p OnFailure`, and `systemctl cat` pass for handler templates and monitored units.
- Telegram smoke proves messages land in `Codex Hermes Ops`.
