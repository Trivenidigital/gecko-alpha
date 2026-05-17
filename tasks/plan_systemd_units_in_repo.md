**New primitives introduced:** `systemd/gecko-pipeline.service` + `systemd/gecko-dashboard.service` (capture from srilu /etc/systemd/system/); `systemd/README.md` (deploy workflow doc).

# Plan: BL-NEW-SYSTEMD-UNIT-IN-REPO

**Backlog item:** `BL-NEW-SYSTEMD-UNIT-IN-REPO` (filed 2026-05-16, V4 NOTE from `feat/score-volume-pruning-harden` design review)
**Goal:** Make the two production unit files (`gecko-pipeline.service` + `gecko-dashboard.service`) repo-tracked so PR reviewers see drift between repo and prod (`/etc/systemd/system/`).

**Architecture:** None — pure capture + commit + README.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| systemd unit capture / DevOps infra-in-git | None (no Hermes skill covers config-from-host capture) | Mechanical capture, no skill needed. |

awesome-hermes-agent: 404 (consistent prior). **Verdict:** custom mechanical capture.

## Drift verdict

PARTIAL match. `systemd/` directory already exists in tree with 3 service units (`gecko-backup{,-watchdog}.service` + `minara-emission-persistence-watchdog.service`) + their timer files. The 2 PRIMARY unit files (`gecko-pipeline.service` + `gecko-dashboard.service`) are missing — that's the gap the backlog item targets. Filling it.

## File structure

| File | Responsibility |
|---|---|
| `systemd/gecko-pipeline.service` (NEW) | captured verbatim from srilu `/etc/systemd/system/gecko-pipeline.service` |
| `systemd/gecko-dashboard.service` (NEW) | captured verbatim from srilu `/etc/systemd/system/gecko-dashboard.service` |
| `systemd/README.md` (NEW) | deploy-workflow doc + drift-audit script suggestion |
| `backlog.md` (MODIFY) | flip BL-NEW-SYSTEMD-UNIT-IN-REPO to SHIPPED with capture provenance |

## What was captured (verbatim from srilu 2026-05-17)

```ini
# gecko-pipeline.service
[Unit]
Description=Gecko Alpha Pipeline
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/gecko-alpha
ExecStart=/root/.local/bin/uv run python -m scout.main
Restart=always
RestartSec=10
Environment=PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
```

```ini
# gecko-dashboard.service
[Unit]
Description=Gecko Alpha Dashboard
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/gecko-alpha
ExecStart=/root/.local/bin/uv run uvicorn dashboard.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=10
Environment=PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
```

## Tasks

### Task 1: Drop unit files in tree

- [x] Copy from srilu via SSH; written to `systemd/gecko-pipeline.service` + `systemd/gecko-dashboard.service`.

### Task 2: README.md with deploy workflow

- [ ] Document the operator workflow that pulls repo → `/etc/systemd/system/`:

```bash
# On srilu, post-pull:
sudo cp systemd/gecko-pipeline.service /etc/systemd/system/
sudo cp systemd/gecko-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart gecko-pipeline gecko-dashboard
```

- [ ] Drift-audit one-liner: `diff systemd/gecko-pipeline.service /etc/systemd/system/gecko-pipeline.service`. Commit a `scripts/check_systemd_drift.sh` helper? Defer — the diff is 1 line.

### Task 3: Backlog close + memory checkpoint

- [ ] Flip backlog entry to SHIPPED with PR ref.

## Out of scope

- Auto-deploy daemon for unit-file changes — manual cp is fine; drift audit catches mistakes.
- The 3 already-tracked units (backup + minara-emission-persistence-watchdog) — out-of-scope; they're already in repo.
- Timer files — neither gecko-pipeline nor gecko-dashboard has a timer (they're long-running services).

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Unit file drift between repo and prod after merge | Medium | Low | README adds the audit one-liner; operator runs occasionally |
| Capture missed a unit file env override | Low | Low | Captures verbatim from `/etc/systemd/system/`; any drop-ins in `/etc/systemd/system/gecko-pipeline.service.d/` would be missed |
| Future env-var addition needs to live SOMEWHERE | Low | Low | Out of scope of this PR — env lives in `.env` per current pattern |
