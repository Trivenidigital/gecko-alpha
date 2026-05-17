**New primitives introduced:** Same as `tasks/plan_other_prod_config_audit.md` post V52/V53 fold (`7d867de`) — NONE (no new code primitives). Repo additions: `cron/gecko-alpha.crontab` + `cron/README.md` + `cron/deploy.sh` (sentinel-bracketed merge script).

# Design: BL-NEW-OTHER-PROD-CONFIG-AUDIT (cycle 11)

**Plan reference:** `tasks/plan_other_prod_config_audit.md` (`8fa62d6` + V52/V53 fold `7d867de`)

## Hermes-first analysis

Same as plan. No DevOps host-config-audit skill. Custom audit doc + new `cron/` directory mirroring cycle-6's `systemd/` pattern.

## Design decisions

### D1. New `cron/` directory mirrors `systemd/` precedent

Cycle 6 established `systemd/` as canonical home for repo-tracked system config. Cycle 11 extends with `cron/` for cron schedules that can't be expressed as systemd timers OR are simpler as cron (the 2 archive scripts run weekly; systemd-timer conversion = follow-up).

Structure:
```
cron/
  gecko-alpha.crontab     # sentinel-bracketed managed block
  deploy.sh               # awk-based idempotent merge
  README.md               # operator workflow
```

### D2. Sentinel-bracketed managed block (V53 MUST-FIX)

```
# === BEGIN gecko-alpha managed block (do not edit between sentinels) ===
<lines>
# === END gecko-alpha managed block ===
```

The `deploy.sh` awk script preserves anything OUTSIDE the sentinels (operator manual entries, polymarket entries) and idempotently replaces the inside. Re-running yields identical crontab.

### D3. Findings doc enumeration matches plan's disposition table verbatim

The findings doc is the operator-facing artifact; plan + design are process docs. Findings MUST include (V55 MUST-FIX folds inline):

1. **Inventory** of what was swept — full category list
2. **Disposition** per item (repo-add / OMIT / follow-up)
3. **VPS multi-tenant inventory** (gecko-alpha + polymarket-ml-signal + btc15minutebot + shift-agent)
4. **Schedule contention check** (no overlaps observed)
5. **Reproducibility (V55 MUST-FIX)** — literal SSH probe commands inline per category:
   - `crontab -l`
   - `ls -la /etc/cron.d/ /etc/cron.daily/ /etc/cron.weekly/ /etc/cron.monthly/`
   - `ls -la /etc/sudoers.d/`
   - `find /etc/systemd/system -maxdepth 2 -type d -name "*.d"`
   - `grep -v "^#\|^$" /etc/systemd/journald.conf`
   - `find /root /opt /etc -maxdepth 3 -name "*.env"`
   - `systemctl is-enabled apache2; systemctl is-active apache2; ss -tlnp | grep apache; ls /etc/apache2/sites-enabled/`
   - `cat /etc/hostname; cat /etc/timezone; readlink /etc/localtime`
   - `wc -l /root/.ssh/authorized_keys`
   - `ufw status; iptables -L INPUT -n --line-numbers`
   - `ls /etc/logrotate.d/`
   - `ls /etc/sysctl.d/`
   - `ls /etc/security/limits.d/`
   - `ls /etc/profile.d/; grep -v "^#\|^$" /etc/environment`
   - `grep -v "^#\|^$" /etc/hosts`
   - `ls /etc/ssh/sshd_config.d/`
   - `ls /opt/`

   Future-self can re-run all of these in ~30s SSH.

6. **Withdrawn item callout (V55 MUST-FIX)** — explicit subsection citing BL-NEW-APACHE-AUDIT withdrawal evidence (`systemctl is-enabled apache2 = not-found`, no listening sockets, no `sites-enabled/`). Prevents future re-investigation.

### D4. Five follow-ups filed (V52/V53 folds + 1 withdrawn)

1. **BL-NEW-CRON-DRIFT-WATCHDOG** — bash watchdog mirroring cycle-10 systemd-drift-watchdog, but for `crontab -l`. Decision-by: cheap, file after cycle 11 ships.
2. **BL-NEW-CRON-TO-SYSTEMD-TIMER** — convert 2 weekly cron entries to systemd timers. **V55 SHOULD-FIX:** expected disposition at decision time is "likely close as no-op (cron is simpler for weekly schedule; cycle-10 systemd-timer canon applies more naturally to high-cadence triggers)." Filing only to document the design tension was considered. Decision-by 2026-06-14.
3. **BL-NEW-DRIFT-WATCHDOG-ARCHIVE** — extend wal_archive.sh shape for systemd-drift-watchdog journal events.
4. **BL-NEW-FIREWALL-DECISION** — operator review at 2026-06-14 of ACCEPT-policy.
5. **BL-NEW-POLYMARKET-VERIFY** — V55 SHOULD-FIX: operator confirms `/opt/polymarket-ml-signal/` exists OR polymarket cron is stale. ~5min effort. Filing > memory because inventory accuracy depends on it.
6. ~~BL-NEW-APACHE-AUDIT~~ — withdrawn; Apache not installed (V52 drill).

### D4b. Memory checkpoint must be self-sufficient (V55 MUST-FIX)

`~/.claude/.../memory/project_prod_config_audit_2026_05_17.md` MUST inline:
- **FIREWALL kill-criterion**: "if srilu remains single-tenant-by-app AND no inbound attack surface change, accept ACCEPT-policy and close BL-NEW-FIREWALL-DECISION at 2026-06-14"
- **Single-tenant verification command** (for the 2026-06-14 reviewer to re-run):
  ```bash
  ssh root@srilu-vps 'crontab -l | grep -v "/root/gecko-alpha"; ls /opt/; ls /etc/logrotate.d/'
  ```
- **Cron-vs-systemd-timer pros/cons one-liner**: cron stays = 2 lines simpler; systemd-timer canon = consistency with cycle-10. Default disposition: cron stays.
- Inline operator deploy command for `cron/deploy.sh`

### D5. Cross-file invariants

| Invariant | Source | Verification |
|---|---|---|
| `cron/gecko-alpha.crontab` is sentinel-bracketed | committed file content | manual inspection |
| `cron/deploy.sh` idempotent across re-runs (V54 MUST-FIX: `matched=1` in /BEGIN/ rule + tempfile staging) | awk script | shell-test on srilu (one-off operator verification post-merge) |
| Findings doc lists every backlog-text category + V52-extended categories | findings doc table | reviewer cross-ref |
| All disposition rows fall into 3 declared categories (repo-add / OMIT / follow-up) | plan §pre-registered criteria | line-by-line table check |
| `cron/` mirrors `systemd/` directory pattern | repo structure | trivial |
| BL-NEW-* follow-ups linked by tag (`cycle-11-followup`) | backlog.md | grep |
| **`systemd/README.md` cross-references cron deploy (V55 SHOULD-FIX)** | README.md modification in commit 1 | reviewer cross-ref |
| **Findings doc includes literal SSH probe commands inline (V55 MUST-FIX)** | findings doc §Reproducibility | grep for command blocks |
| **Memory checkpoint inlines firewall kill-criterion + verification command (V55 MUST-FIX)** | memory file content | reviewer cross-ref |

## Commit sequence

3 commits, bisect-safe:

1. `feat(cron): cron/ directory + sentinel-bracketed fragment + idempotent deploy.sh (cycle 11 commit 1/3)` — `cron/gecko-alpha.crontab`, `cron/deploy.sh`, `cron/README.md`
2. `feat(audit): other-prod-config audit findings (cycle 11 commit 2/3)` — `tasks/findings_other_prod_config_audit_2026_05_17.md`
3. `docs(backlog): close BL-NEW-OTHER-PROD-CONFIG-AUDIT + 4 follow-ups + memory checkpoint (cycle 11 commit 3/3)` — backlog flip + memory file

## Risk register additions

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `cron/deploy.sh` awk script edge case if crontab is empty | Low | Low | `crontab -l 2>/dev/null || true` defensive; awk handles empty input as no-op |
| `cron/deploy.sh` corrupts crontab on awk error | Very Low | Medium | `set -e` kills before `crontab -` if awk fails; existing crontab untouched. Operator can recover via `crontab -l > /tmp/crontab.backup` pre-deploy |
| Sentinel marker collision with operator-added comments | Refuted | — | The exact sentinel strings are project-specific and unlikely to collide |
| Future contributor commits cron entry OUTSIDE the sentinel block | Medium | Low | `cron/README.md` documents the convention; PR review catches |

## Out of scope

(Per plan §Out of scope — unchanged.)

## Deployment

Post-merge:
```bash
ssh root@srilu-vps
cd /root/gecko-alpha
git pull
bash cron/deploy.sh
crontab -l  # verify both polymarket + gecko entries
```
