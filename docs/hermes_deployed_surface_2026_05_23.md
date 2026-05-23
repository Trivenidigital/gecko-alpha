# Hermes deployed-surface snapshot — srilu-vps — 2026-05-23

Closes the Hermes-first deployed-surface gate for the cockpit V1 work
(`BL-NEW-LIVE-DECISION-COCKPIT` + `BL-NEW-LIVE-CANDIDATES-CONTRACT-SMOKE`).
Snapshot taken via the SSH two-step pattern per AGENTS.md.

## Topology — two Hermes homes on srilu-vps

| Path | Owner | Purpose | Status |
|---|---|---|---|
| `/root/.hermes/` | root | Bundled-skills install (24 skill categories) + empty cron dir | Inert — no jobs, no active processes; reference install |
| `/home/gecko-agent/.hermes/` | `gecko-agent` user | Active Hermes-agent runtime with gateway + cron | **Active** — `hermes-gateway.service` running 8h, hourly cron tick at `:42` UTC |

The operator's pinned memory previously said "Hermes cron active: gecko-x-narrative-scanner hourly" — this is the `gecko-agent` install. The `/root/.hermes/cron/` directory is empty by design (root install holds bundled skill content, not active jobs).

## Active Hermes processes

| Surface | Evidence |
|---|---|
| `hermes-gateway.service` (systemd) | Loaded, enabled, active(running) 8h on srilu — PID 809020 under `gecko-agent`. Binary at `/home/gecko-agent/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main gateway run --replace`. |
| Gateway warnings on start | `No user allowlists configured` + `No messaging platforms enabled` — gateway accepts local-only delivery, no inbound Telegram/Discord routing through it. |

The hermes-gateway being warning-only "no platforms enabled" is intentional today: gecko-alpha's Telegram delivery goes through `scout/alerter.py`, not the gateway. The gateway is the substrate for future Hermes-side enrichment, not the message bus.

## Hermes cron jobs (active)

Single hourly job in `/home/gecko-agent/.hermes/cron/jobs.json`:

| Field | Value |
|---|---|
| id | `c849fffec986` |
| name | `gecko-x-narrative-scanner` |
| script | `gecko_x_narrative_scanner.sh` |
| schedule | `0 * * * *` (hourly at minute 0) |
| created_at | 2026-05-14T01:55:26Z |
| completed runs | 220 |
| last_run_at | 2026-05-23T04:00:42Z |
| last_status | `ok` |
| last_error | null |
| next_run_at | 2026-05-23T05:00:00Z |
| delivery | `local` |
| enabled | true |
| `no_agent` | true (script runs without Hermes-agent classifier in-process) |

The scanner is Hermes-runs-script (not Hermes-runs-classifier). It's a script-launcher on a cron schedule. Hermes provides the scheduler + tick discipline + structured state file; the actual narrative-scanner logic lives in the script.

## Hermes skill installs

`/root/.hermes/skills/` carries 24 bundled skill categories including:
- `dogfood/` (Hermes self-development skill — operator-installed)
- `software-development`, `github`, `productivity`, `media`, etc. (stock bundled)
- `claude-code`, `codex` (delegation skills)

These are **available** for Hermes invocations but not currently load-bearing for gecko-alpha pipelines. The cockpit V1 (`/api/live_candidates`) and its contract validator do NOT load any of these skills.

`/home/gecko-agent/.hermes/hermes-agent/` carries the agent runtime itself.

## Ownership map — what Hermes vs Codex own today

| Surface | Owner | Evidence |
|---|---|---|
| Code (Python: scout/, dashboard/, tests/, scripts/) | **Codex** | git log: 100% gecko-alpha commits authored via Codex automation flows or operator |
| Repo plan/design/spec docs (`tasks/`) | **Codex** | Same |
| Database schema + migrations (`scout.db`) | **Codex** | `scout/db.py` + alembic-style internal migrations |
| Paper-trade execution + outcomes | **Codex** | `scout/trading/` pipeline; no Hermes calls in dispatch path |
| Live-candidates API (`/api/live_candidates`) | **Codex** | `dashboard/api.py` + `dashboard/db.py`; pure read-only stdlib over SQLite |
| Contract-smoke validator (PR #232) | **Codex** | `scripts/check_live_candidates_contract.py`; stdlib-only, no Hermes load |
| Hourly narrative scanner | **Hermes (scheduler) + Codex (script body)** | jobs.json above; `gecko_x_narrative_scanner.sh` lives outside the gecko-alpha repo (gecko-agent home), exits → writes data Codex reads from |
| Telegram delivery to operator | **Codex** | `scout/alerter.py` direct urllib POST; no gateway hop |
| Gateway warning-only state | **Hermes (latent substrate)** | systemd active but no platforms enabled; reserved for future enrichment routing |
| Backlog state / priority / pinning | **Operator memory + Codex** | `backlog.md` + `tasks/todo.md`; Hermes does not write/read backlog state |

## What is still only planned (not in either system today)

- **Frontend cockpit panel** — gated on `/api/live_candidates` 24-48h soak ending ~2026-05-25 (PR #228 + #229 deploy reference)
- **Signal Trust Roadmap V1** — read-only signal-maturity registry; deferred per operator priority order
- **Hermes-side enrichment for candidate explanation** — explicitly captured as enrichment-only in cockpit V1 design; no current loader
- **Gateway-routed message delivery** — gateway runs but warns "no platforms enabled"; Codex `scout/alerter.py` is the only delivery path today

## Hermes-first gate — formal closure status for live-candidates work

For PR #228 (cockpit V1), PR #229 (counter_flags hotfix), and PR #232 (contract+smoke validator):

| Domain | Check result | Closure status |
|---|---|---|
| HTTP contract / smoke testing | No Hermes skill found on `/root/.hermes/skills/` or in 24 bundled categories | **CLOSED — none applicable** |
| Deterministic label-safety auditing | No Hermes skill found (skills are content-generation tools, not response validators) | **CLOSED — none applicable** |
| Trader candidate explanation over structured DB rows | No Hermes skill loadable today; the `gecko-x-narrative-scanner` produces narrative signals but not per-token candidate explanations; design captures Hermes as enrichment-only for future PRs | **CLOSED — none applicable for V1; reserved for future enrichment PR** |
| Per-source/KOL ranking | No Hermes skill present; operator's pinned safety stance forbids this until source-call price coverage becomes rankable | **N/A — explicitly out of scope** |
| Price truth / PnL / identity / execution | Hermes deliberately not load-bearing (per CLAUDE.md §7b: "Hermes is the brain, not price truth/execution truth") | **CLOSED — keep custom (DB truth only)** |
| Message delivery | `scout/alerter.py` direct urllib (gateway warns no platforms enabled) | **CLOSED — Codex-owned, Hermes-deferred** |

**Verdict: Hermes-first gate is formally CLOSED for the cockpit V1 + contract-smoke work.** The deployed surface contains no skill that materially could have been loaded for these PRs. Future enrichment work that wants to consume the cockpit's `/api/live_candidates` may legitimately add a Hermes-side reader skill, in which case this snapshot should be re-validated before that PR ships.

## Commands used to capture this snapshot (operator-runnable)

```bash
# (1) Root Hermes home — bundled skills + empty cron
ssh root@89.167.116.187 'ls -la ~/.hermes ~/.hermes/skills ~/.hermes/cron' > .ssh_out.txt 2>&1
# read .ssh_out.txt

# (2) gecko-agent Hermes home + jobs.json detail
ssh root@89.167.116.187 'sudo -u gecko-agent ls -la /home/gecko-agent/.hermes/cron && sudo -u gecko-agent jq ".jobs[0]" /home/gecko-agent/.hermes/cron/jobs.json' > .ssh_out.txt 2>&1
# read .ssh_out.txt

# (3) Active hermes-gateway service
ssh root@89.167.116.187 'systemctl status hermes-gateway --no-pager' > .ssh_out.txt 2>&1
# read .ssh_out.txt

# (4) Scanner cron heartbeat (last tick mtime)
ssh root@89.167.116.187 'stat /home/gecko-agent/.hermes/cron/.tick.lock' > .ssh_out.txt 2>&1
# read .ssh_out.txt
```

## Re-validation cadence

This snapshot is fresh as of 2026-05-23. Re-validate before:
- any PR that introduces a Hermes-side reader/enrichment for cockpit data
- any PR that adds a Hermes cron job (because adding a job changes the deployed surface for future Hermes-first checks)
- any PR that enables a messaging platform on the gateway (because that flips the gateway from "latent substrate" to load-bearing for delivery)
