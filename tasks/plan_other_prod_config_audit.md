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
| **journald.conf** | Defaults only (`[Journal]` header, no overrides) | **OMIT (accepted; partial mitigation only — V53 fold)** — Cycles 3+4 archive scripts capture `sqlite_wal_*` + `tg_dispatch_*` events specifically. Cycle 10 drift-watchdog emits PLAIN-TEXT `echo` lines (not structured `"event":` fields) so NEITHER archive captures it. Accept loss of drift-watchdog journal events beyond default journald retention; if needed later, file follow-up `BL-NEW-DRIFT-WATCHDOG-ARCHIVE` (cheap, mirrors wal_archive.sh shape). |
| **Env files outside .env** | Only `/root/gecko-alpha/.env` | OK |
| **Reverse-proxy / web server** | Apache NOT INSTALLED (V52 MUST-FIX drill 2026-05-17: `systemctl is-enabled apache2` = `not-found`; `is-active` = `inactive`; no listening sockets; no `sites-enabled/`). Only `/etc/apache2/conf-available/` directory remnant. | OK (no action) — earlier "Apache present" framing was misleading; the conf-available dir is apt-package stub. BL-NEW-APACHE-AUDIT follow-up **NOT NEEDED**, withdrawn. |
| **/etc/hostname /etc/timezone** | `ubuntu-4gb-hel1-1` / `Etc/UTC` | OK — TZ is UTC; explicitly documented in cycle 10 V46/V51 timezone surfacing |
| **SSH authorized_keys** | 1 key in `/root/.ssh/authorized_keys` | OK — single operator access; doesn't belong in repo (secret) |
| **Firewall (UFW + iptables)** | UFW inactive; iptables INPUT policy = ACCEPT (no rules) | **OPERATOR-DECISION-PENDING (V53 fold — decision-by 2026-06-14)** — no firewall is a security posture choice. Doc but don't change without operator greenlight. Re-review at 2026-06-14 (4w); kill-criterion: if srilu remains single-tenant-by-app *AND* no inbound attack surface change, accept ACCEPT-policy and close. |

### Extended sweep (V52 SHOULD-FIX — additional categories)

| Category | State | Disposition |
|---|---|---|
| `/etc/logrotate.d/` | Contains `btc15minutebot` + `shift-agent` entries (additional projects on srilu) + stock OS. **NO gecko-alpha entry.** | OK — gecko-pipeline logs flow through journald, NOT logrotate. Cycles 3+4 archive scripts handle gecko-specific retention. Intentional, not gap. |
| `/etc/sysctl.d/` | 10 stock kernel-hardening files (`10-bufferbloat.conf`, `10-kernel-hardening.conf`, etc.) + `99-sysctl.conf`. **No gecko-specific tuning.** | OK — no gecko-relevant network/IO tuning observed |
| `/etc/security/limits.d/` | (no result — empty or absent) | OK — gecko-pipeline runs as root via systemd; no ulimit collision |
| `/etc/profile.d/` | 8 stock entries (locale, bash_completion, byobu, cloud-init). **No gecko shell init.** | OK |
| `/etc/environment` | Sets PATH only, no env overrides | OK — no shadow of `.env` |
| `/etc/hosts` | Stock loopback + `ubuntu-4gb-hel1-1` self-hostname. No DNS overrides. | OK |
| `/etc/ssh/sshd_config.d/` | Only `50-cloud-init.conf` (stock) | OK |
| `/opt/` (multi-tenant check) | Empty in second sweep — earlier crontab referenced `/opt/polymarket-ml-signal/` but actual dir may not exist OR was deleted. Worth operator confirmation. | NOTE — polymarket cron entry may point at non-existent path; operator-decision (file BL-NEW-POLYMARKET-VERIFY if needed) |

### VPS inventory (V52 SHOULD-FIX)

srilu is **multi-tenant**. Beyond gecko-alpha, evidence of:
- **polymarket-ml-signal** (crontab entry every 6h to `/opt/polymarket-ml-signal/scripts/extract_data.sh`)
- **btc15minutebot** (logrotate.d entry)
- **shift-agent** (logrotate.d entry — Hermes-related per memory `handoff_vps_swap_completed_2026_05_13.md`)
- gecko-alpha (this project)

Operator-visible inventory artifact filed at `tasks/findings_other_prod_config_audit_2026_05_17.md` so future audits start from a single source.

### Schedule contention check (V52 SHOULD-FIX)

| When | What |
|---|---|
| `00:00, 06:00, 12:00, 18:00` daily | polymarket extract_data.sh |
| `Sun 03:30` | gecko `tg_burst_archive.sh` |
| `Sun 03:45` | gecko `wal_archive.sh` |
| `09:00` daily | gecko-backup-watchdog.timer |
| `09:30` daily | systemd-drift-watchdog.timer (cycle 10) |
| `Sun 03:00` daily | gecko-backup.timer (via gecko-backup.service) |

No minute-level overlaps. polymarket runs hourly-aligned; gecko runs offset minutes. No collision risk observed.

## Tasks

### Task 1: cron entries → repo

**Files:**
- Create: `cron/gecko-alpha.crontab` — fragment with the 2 gecko entries (NOT the polymarket entry)
- Create: `cron/README.md` — deploy workflow mirroring `systemd/README.md`
- Modify: `systemd/README.md` deploy block — mention cron deploy alongside systemd unit deploy (so future operator following the deploy block doesn't miss cron)

Crontab fragment content (V53 MUST-FIX — sentinel-bracketed managed block):

```
# === BEGIN gecko-alpha managed block (do not edit between sentinels) ===
30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh
45 3 * * 0 /root/gecko-alpha/scripts/wal_archive.sh
# === END gecko-alpha managed block ===
```

Deploy script (replaces inline grep -v path-prefix; preserves any operator-added manual entries OUTSIDE the sentinels):

```bash
#!/usr/bin/env bash
# cron/deploy.sh — idempotent crontab merge between sentinels
# V54 MUST-FIX folded:
#   (1) `matched=1` set inside /BEGIN/ rule so END guard works correctly
#       (without it, subsequent deploys would APPEND a second fragment copy)
#   (2) Tempfile staging via mktemp + trap so `crontab` install is atomic;
#       pipe-to-`crontab -` could partial-install on awk mid-stream failure
set -euo pipefail
FRAGMENT="$(cat /root/gecko-alpha/cron/gecko-alpha.crontab)"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
crontab -l 2>/dev/null \
    | awk -v fragment="$FRAGMENT" '
        /^# === BEGIN gecko-alpha managed block/ { skip=1; matched=1; print fragment; next }
        /^# === END gecko-alpha managed block/ { skip=0; next }
        !skip
        END { if (!matched) { print fragment } }
    ' \
    > "$TMP"
crontab "$TMP"
echo "OK: gecko-alpha cron block updated"
crontab -l || true   # V54 SHOULD-FIX: guard empty-crontab nonzero exit
```

Operator runs: `bash /root/gecko-alpha/cron/deploy.sh`. Idempotent: re-running yields same crontab. V54 invariant: on second deploy, the BEGIN rule fires, sets `matched=1`, prints the fragment ONCE, sets `skip=1`. END block sees `matched=1`, does NOT append. Crontab stable.

### Task 2: findings doc

**Files:**
- Create: `tasks/findings_other_prod_config_audit_2026_05_17.md` — enumerate each category from the table above with state + disposition

### Task 3: backlog close + follow-ups

**Files:**
- Modify: `backlog.md` — flip `BL-NEW-OTHER-PROD-CONFIG-AUDIT` to SHIPPED-WITH-FINDINGS; file:
  - **BL-NEW-CRON-DRIFT-WATCHDOG** (mirror cycle-10 watchdog for cron entries — needs to grep crontab -l, find managed block, diff vs `cron/gecko-alpha.crontab`)
  - **BL-NEW-CRON-TO-SYSTEMD-TIMER** (V53 SHOULD-FIX design-tension fold — convert the 2 weekly cron entries to systemd timers for consistency with cycle-10 canon; alternative: keep cron for simplicity. Decision-by 2026-06-14)
  - **BL-NEW-DRIFT-WATCHDOG-ARCHIVE** (V53 SHOULD-FIX — extend wal_archive.sh shape to capture systemd-drift-watchdog journal events before default retention drops them)
  - **BL-NEW-FIREWALL-DECISION** (V53 — pre-registered 2026-06-14 review of ACCEPT-policy)
  - **BL-NEW-POLYMARKET-VERIFY** (V52/V53 — operator confirms `/opt/polymarket-ml-signal/` path exists or polymarket cron is stale; informational, no gecko impact)
  - ~~BL-NEW-APACHE-AUDIT~~ WITHDRAWN (V52 fold: Apache confirmed not installed; only conf-available dir remnant)
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
