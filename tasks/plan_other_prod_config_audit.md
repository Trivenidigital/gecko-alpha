**New primitives introduced:** NONE. Analysis-only PR — `tasks/findings_other_prod_config_audit_2026_05_17.md` + selective additions to repo (cron entries; possible new directory `cron/` mirroring the `systemd/` pattern) + backlog flip on `BL-NEW-OTHER-PROD-CONFIG-AUDIT` + memory checkpoint. Net adds depend on what the sweep surfaces; default is doc-only.

# Plan: BL-NEW-OTHER-PROD-CONFIG-AUDIT (cycle 11)

**Backlog item:** `BL-NEW-OTHER-PROD-CONFIG-AUDIT` (filed 2026-05-17 cycle 6 V35 FOLLOW-UP). Per backlog: "sweep srilu for cron entries, /etc/sudoers.d/, drop-in dirs under /etc/systemd/system/*.service.d/, nginx/caddy config, journald.conf overrides, env files outside .env. One audit pass forecloses future 'we have a third one of these' findings."

**Goal:** enumerate every operator-meaningful config-on-host that should arguably be repo-tracked. Output: findings doc + selective repo additions OR explicit documentation of why each is intentionally omitted.

**Architecture:** None — analysis + selective commits.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Generic host-config audit / drift detection | None | Project-internal manual sweep |
| Cron-as-code patterns | None | Project-internal; cycle 10 just shipped systemd-timer pattern as canonical |
| Reverse-proxy / firewall config-in-git | None | Operator-decision: assess current state but don't add |

awesome-hermes-agent: 404 (consistent prior). **Verdict:** custom audit doc.

## Drift verdict

NET-NEW. No prior `tasks/findings_*prod_config*` or `*config_audit*`. Branch `feat/other-prod-config-audit` off master HEAD `256b169` post-cycle-10. No parallel-session interleaving on the audit surface (the parallel `codex/chain-anchor-pipeline-fix` branch visible at deploy time targets cycle-8 follow-up, not config-audit).

## Sweep results (already gathered — see `.ssh_c11_sweep.txt`)

| Category | State | Disposition |
|---|---|---|
| **Crontab** (`crontab -l`) | 3 entries: 1 polymarket (out-of-scope, separate project) + 2 gecko-alpha (`tg_burst_archive.sh` Sunday 03:30 + `wal_archive.sh` Sunday 03:45) | **GAP** — gecko cron entries are repo-untracked at the schedule level. The shell scripts are in repo; the cron schedule is not. **Repo addition needed: `cron/` directory + README + crontab fragment.** |
| **/etc/cron.d/** | Stock OS only (`e2scrub_all`, `sysstat`, `.placeholder`) | OK — no gecko content |
| **/etc/cron.daily/{weekly,monthly}/** | Stock OS only (`apport`, `apt-compat`, `dpkg`, `logrotate`, `man-db`, `sysstat`) | OK — no gecko content |
| **/etc/sudoers.d/** | Only `90-cloud-init-users` + `README` (stock cloud-init) | OK |
| **systemd drop-ins** `*.service.d/`, `*.timer.d/` | EMPTY (the cycle-10 drift watchdog will catch any future drop-ins) | OK |
| **journald.conf** | Defaults only (`[Journal]` header, no overrides) | **NOTE** — DEBUG events expire on default retention. Cycle 3 TG-burst archive + cycle 4 WAL archive already mitigate via weekly `.jsonl.gz` capture. Adequate. |
| **Env files outside .env** | Only `/root/gecko-alpha/.env` | OK |
| **Reverse-proxy / web server** | Apache present at `/etc/apache2/` | **POSSIBLE-GAP** — but `gecko-dashboard.service` (cycle 6) uses uvicorn directly on port 8000 without a reverse proxy. Apache may be vestigial / unused. Operator verification needed before any action. |
| **/etc/hostname /etc/timezone** | `ubuntu-4gb-hel1-1` / `Etc/UTC` | OK — TZ is UTC; explicitly documented in cycle 10 V46/V51 timezone surfacing |
| **SSH authorized_keys** | 1 key in `/root/.ssh/authorized_keys` | OK — single operator access; doesn't belong in repo (secret) |
| **Firewall (UFW + iptables)** | UFW inactive; iptables INPUT policy = ACCEPT (no rules) | **OPERATOR-DECISION-PENDING** — no firewall is a security posture choice. Doc but don't change without operator greenlight |

## Tasks

### Task 1: cron entries → repo

**Files:**
- Create: `cron/gecko-alpha.crontab` — fragment with the 2 gecko entries (NOT the polymarket entry)
- Create: `cron/README.md` — deploy workflow mirroring `systemd/README.md`
- Modify: `systemd/README.md` deploy block — mention cron deploy alongside systemd unit deploy (so future operator following the deploy block doesn't miss cron)

Crontab fragment content:

```
# gecko-alpha crontab fragment. Deploy via:
#   crontab -l | grep -v '/root/gecko-alpha/scripts/' > /tmp/cron.tmp
#   cat /root/gecko-alpha/cron/gecko-alpha.crontab >> /tmp/cron.tmp
#   crontab /tmp/cron.tmp && rm /tmp/cron.tmp
30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh
45 3 * * 0 /root/gecko-alpha/scripts/wal_archive.sh
```

### Task 2: findings doc

**Files:**
- Create: `tasks/findings_other_prod_config_audit_2026_05_17.md` — enumerate each category from the table above with state + disposition

### Task 3: backlog close + 2 follow-ups

**Files:**
- Modify: `backlog.md` — flip `BL-NEW-OTHER-PROD-CONFIG-AUDIT` to SHIPPED-WITH-FINDINGS; file BL-NEW-APACHE-AUDIT (verify vestigial) + BL-NEW-CRON-DRIFT-WATCHDOG (mirror cycle-10 watchdog for cron entries)
- Create: `~/.claude/.../memory/project_prod_config_audit_2026_05_17.md`

## Pre-registered decision criteria

For each gap surfaced in the sweep, this audit must produce ONE of:
1. **Repo addition** (e.g., `cron/gecko-alpha.crontab`) — if operator-meaningful AND not a secret
2. **Explicit OMIT documentation** (e.g., authorized_keys, .env) — secret OR host-specific
3. **Follow-up backlog item** (e.g., BL-NEW-APACHE-AUDIT) — needs operator decision

No silent omissions.

## Hermes skill probe verification

Per CLAUDE.md §7b: probed hermes-agent.nousresearch.com/docs/skills (689 skills, prior probes in cycles 4-10). DevOps category surfaced no host-config-sweep skill. awesome-hermes-agent: 404 (consistent). Verdict: custom audit doc.

## Deployment

Post-merge operator action:
```bash
ssh root@srilu-vps
cd /root/gecko-alpha
git pull
# Deploy cron fragment (re-merge with polymarket entry):
(crontab -l | grep -v '/root/gecko-alpha/scripts/'; cat cron/gecko-alpha.crontab | grep -v '^#') | crontab -
crontab -l   # verify both polymarket + gecko entries present
```

systemd units (cycle-6 fold) flow unchanged.

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Operator forgets to deploy cron fragment post-merge | Low | Low | Existing cron entries on srilu stay intact (this PR adds the repo source-of-truth; deploy is operator-elective) |
| Cron drift between repo and srilu (BL-NEW-CRON-DRIFT-WATCHDOG follow-up) | Medium | Low | File follow-up; cycle-10 systemd-drift pattern is the template |
| Apache vestigial removal causes outage (BL-NEW-APACHE-AUDIT follow-up) | Low | Medium | DO NOT touch Apache in this PR; file follow-up to verify safe |
| journald default retention drops DEBUG events | Refuted | — | cycles 3+4 archive scripts already mitigate |
| Firewall posture: no UFW/iptables | Operator-decision | — | Document; do not change |

## Out of scope

- Apache config removal (file follow-up)
- Firewall enablement (operator-decision)
- journald.conf customization (already addressed via cycle 3+4 archives)
- /root/.ssh/authorized_keys (secret; never in repo)
- /etc/cron.d/ stock OS entries (out of project scope)
- polymarket-ml-signal cron entry (separate project, not this repo)
