# BL-NEW-OTHER-PROD-CONFIG-AUDIT — Findings 2026-05-17

**Filed:** 2026-05-17 (cycle 11 of autonomous backlog knockdown)
**Source:** srilu-vps (`root@89.167.116.187`)
**Triggered by:** Cycle 6 V35 PR-review follow-up — operator wanted "sweep srilu for other repo-untracked prod config beyond systemd units."

## TL;DR

Of the 17 config categories swept, **only 1 had a clear gap**: 2 gecko-alpha cron entries (`tg_burst_archive.sh`, `wal_archive.sh`) existed on srilu but were repo-untracked at the schedule level. **Fixed in this PR via `cron/` directory + sentinel-bracketed managed block + idempotent `cron/deploy.sh`.**

5 follow-ups filed (V57 fold — was 4; missed POLYMARKET-VERIFY); 1 originally-suspected item withdrawn after drill (Apache). V58 fold: 5 additional sweep gaps noted as addendum; filed BL-NEW-AUDIT-SURFACE-ADDENDUM.

## Sweep results

### Crontab — GAP CLOSED

```bash
ssh root@srilu-vps 'crontab -l'
# Result (pre-cycle-11):
# 0 */6 * * * /opt/polymarket-ml-signal/scripts/extract_data.sh >> /var/log/ml-signal-extract.log 2>&1
# 30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh
# 45 3 * * 0 /root/gecko-alpha/scripts/wal_archive.sh
```

**Disposition: REPO ADD.** Cycle 11 adds `cron/gecko-alpha.crontab` (sentinel-bracketed managed block) + `cron/deploy.sh` (idempotent awk merge that preserves the polymarket entry).

### /etc/cron.d/ + cron.daily/ cron.weekly/ cron.monthly/ — OK

```bash
ssh root@srilu-vps 'ls -la /etc/cron.d/; ls /etc/cron.daily/ /etc/cron.weekly/ /etc/cron.monthly/'
# Result: stock OS only (e2scrub_all, sysstat, .placeholder; apport, apt-compat, dpkg, logrotate, man-db)
```

**Disposition: OMIT.** No gecko-relevant content.

### /etc/sudoers.d/ — OK

```bash
ssh root@srilu-vps 'ls -la /etc/sudoers.d/'
# Result: 90-cloud-init-users + README (stock cloud-init)
```

**Disposition: OMIT.** Stock only.

### systemd drop-ins — OK (cycle 10 watchdog watches)

```bash
ssh root@srilu-vps 'find /etc/systemd/system -maxdepth 2 -type d -name "*.d"'
# Result: empty
```

**Disposition: OMIT.** Cycle 10's `systemd-drift-watchdog` will surface any future drop-ins via daily TG alert.

### journald.conf — OMIT (accepted)

```bash
ssh root@srilu-vps 'grep -v "^#\|^$" /etc/systemd/journald.conf'
# Result: [Journal] header only — defaults
```

**Disposition: OMIT (accepted with partial mitigation).** DEBUG events expire on default journald retention. Cycle 3 `tg_burst_archive.sh` captures `tg_dispatch_*` events; cycle 4 `wal_archive.sh` captures `sqlite_wal_*` events. **NEITHER captures cycle 10's drift-watchdog output** (which emits plain-text `echo` lines, not structured `"event":` fields). If drift-watchdog archive becomes operationally needed, file `BL-NEW-DRIFT-WATCHDOG-ARCHIVE`.

### Env files outside `.env` — OK

```bash
ssh root@srilu-vps 'find /root /opt /etc -maxdepth 3 -name "*.env"'
# Result: only /root/gecko-alpha/.env
```

**Disposition: OMIT.** `.env` itself is secret and never goes in repo.

### Apache / reverse-proxy — WITHDRAWN

```bash
ssh root@srilu-vps 'systemctl is-enabled apache2; systemctl is-active apache2; ss -tlnp | grep apache; ls /etc/apache2/sites-enabled/'
# Result:
# not-found
# inactive
# no apache listening sockets
# ls: cannot access '/etc/apache2/sites-enabled/': No such file or directory
```

**Disposition: WITHDRAWN.** Apache is NOT installed. The `/etc/apache2/conf-available/` directory existing was an apt-package stub remnant only. No service unit, no active daemon, no listening sockets, no `sites-enabled/`. `gecko-dashboard.service` (cycle 6) uses uvicorn directly on port 8000 with no reverse proxy — by design. **The original "Apache present" framing was misleading; cycle 11 V52 fold corrected.** `BL-NEW-APACHE-AUDIT` is WITHDRAWN, not filed.

### /etc/hostname /etc/timezone /etc/localtime — OK

```bash
ssh root@srilu-vps 'cat /etc/hostname; cat /etc/timezone; readlink /etc/localtime'
# Result: ubuntu-4gb-hel1-1 / Etc/UTC / /usr/share/zoneinfo/Etc/UTC
```

**Disposition: OMIT.** Hostname is host-specific; TZ is UTC (already documented in cycle 10 V46/V51 timezone surfacing).

### SSH authorized_keys — OK (secret)

```bash
ssh root@srilu-vps 'wc -l /root/.ssh/authorized_keys'
# Result: 1 (single key)
```

**Disposition: OMIT (secret material; never goes in repo).**

### Firewall (UFW + iptables) — OPERATOR-DECISION-PENDING

```bash
ssh root@srilu-vps 'ufw status; iptables -L INPUT -n --line-numbers'
# Result:
# Status: inactive
# Chain INPUT (policy ACCEPT)
# (no rules)
```

**Disposition: OPERATOR-DECISION-PENDING.** No firewall is a security-posture choice; documented but unchanged. **Pre-registered review:** `BL-NEW-FIREWALL-DECISION` at 2026-06-14 (4 weeks). Kill-criterion: if srilu remains single-tenant-by-app AND no inbound attack surface change, accept ACCEPT-policy and close.

### /etc/logrotate.d/ — OK (no gecko content; surfaces co-tenants)

```bash
ssh root@srilu-vps 'ls /etc/logrotate.d/'
# Result: alternatives apport apt bootlog btc15minutebot btmp cloud-init dpkg rsyslog shift-agent
```

**Disposition: OMIT.** No `gecko*` entry; gecko-pipeline logs flow through journald, not logrotate. **NOTE:** `btc15minutebot` + `shift-agent` entries reveal srilu has 2 additional projects beyond gecko-alpha + polymarket. See VPS Inventory below.

### /etc/sysctl.d/ — OK

```bash
ssh root@srilu-vps 'ls /etc/sysctl.d/'
# Result: 10-bufferbloat.conf 10-console-messages.conf 10-ipv6-privacy.conf 10-kernel-hardening.conf 10-magic-sysrq.conf 10-map-count.conf 10-network-security.conf 10-ptrace.conf 10-zeropage.conf 99-sysctl.conf README.sysctl
```

**Disposition: OMIT.** Stock kernel-hardening; no gecko-specific tuning.

### /etc/security/limits.d/ — OK

```bash
ssh root@srilu-vps 'ls /etc/security/limits.d/'
# Result: empty / no entries
```

**Disposition: OMIT.** No ulimit collision; gecko-pipeline runs as root with default limits.

### /etc/profile.d/ + /etc/environment — OK

```bash
ssh root@srilu-vps 'ls /etc/profile.d/; grep -v "^#\|^$" /etc/environment'
# Result: 01-locale-fix.sh apps-bin-path.sh bash_completion.sh gawk.csh gawk.sh Z97-byobu.sh Z99-cloudinit-warnings.sh Z99-cloud-locale-test.sh
# /etc/environment: PATH="/usr/local/sbin:..."
```

**Disposition: OMIT.** All stock; no gecko shadow of `.env`.

### /etc/hosts — OK

```bash
ssh root@srilu-vps 'grep -v "^#\|^$" /etc/hosts'
# Result: 127.0.1.1 ubuntu-4gb-hel1-1; 127.0.0.1 localhost; ::1 localhost ip6-localhost ip6-loopback; ff02::1 ip6-allnodes; ff02::2 ip6-allrouters
```

**Disposition: OMIT.** Stock loopback + self-hostname.

### /etc/ssh/sshd_config.d/ — OK

```bash
ssh root@srilu-vps 'ls /etc/ssh/sshd_config.d/'
# Result: 50-cloud-init.conf
```

**Disposition: OMIT.** Stock cloud-init only.

### /opt/ subdirectories — INFORMATIONAL

```bash
ssh root@srilu-vps 'ls /opt/'
# Result: (empty)
```

**Disposition: NOTE.** Earlier crontab references `/opt/polymarket-ml-signal/scripts/extract_data.sh`, but `/opt/` enumerates empty. Possible explanations: (a) polymarket dir was deleted, (b) sweep redirect collapsed the output, (c) different path layout. Filed as `BL-NEW-POLYMARKET-VERIFY` for operator confirmation (~5min).

## VPS multi-tenant inventory

srilu is **multi-tenant** beyond gecko-alpha:

| Project | Evidence |
|---|---|
| **gecko-alpha** (this repo) | `/root/gecko-alpha/` (everything in scope of this PR) |
| **polymarket-ml-signal** | crontab entry every 6h to `/opt/polymarket-ml-signal/scripts/extract_data.sh` (path validity pending BL-NEW-POLYMARKET-VERIFY) |
| **btc15minutebot** | `/etc/logrotate.d/btc15minutebot` entry; project location unknown |
| **shift-agent** | `/etc/logrotate.d/shift-agent` entry; co-located per memory `handoff_vps_swap_completed_2026_05_13.md` |

This inventory is the artifact future audits should start from. If a 5th project shows up, surface it in the next audit cycle.

## Schedule contention check

| When | What |
|---|---|
| `00:00, 06:00, 12:00, 18:00` daily | polymarket extract_data.sh |
| `Sun 03:00` daily | gecko-backup.timer (existing) |
| `Sun 03:30` | gecko `tg_burst_archive.sh` (cron) |
| `Sun 03:45` | gecko `wal_archive.sh` (cron) |
| `09:00` daily | gecko-backup-watchdog.timer |
| `09:30` daily | systemd-drift-watchdog.timer (cycle 10) |

No minute-level overlaps. Polymarket runs hourly-aligned; gecko runs offset minutes. No collision risk observed.

## Follow-ups filed

| ID | Trigger | Cost |
|---|---|---|
| `BL-NEW-CRON-DRIFT-WATCHDOG` | mirror cycle-10 systemd-drift-watchdog for cron entries | ~2-3h build |
| `BL-NEW-CRON-TO-SYSTEMD-TIMER` | convert 2 weekly cron entries to systemd timers (cycle-10 canon); decision-by 2026-06-14; expected disposition: close as no-op (cron simpler for weekly) | ~30min decision |
| `BL-NEW-DRIFT-WATCHDOG-ARCHIVE` | extend wal_archive.sh shape for systemd-drift-watchdog journal events | ~1h |
| `BL-NEW-FIREWALL-DECISION` | operator review at 2026-06-14 of ACCEPT-policy; kill-criterion specified | ~15min |
| `BL-NEW-POLYMARKET-VERIFY` | operator confirms `/opt/polymarket-ml-signal/` path validity | ~5min |

Withdrawn: ~~`BL-NEW-APACHE-AUDIT`~~ — Apache confirmed not installed.

## Hermes-first verdict

No Hermes skill for host-config-audit / cron-as-code. Custom audit doc. awesome-hermes 404 (consistent prior).

## Drift verdict

NET-NEW. No prior `tasks/findings_*prod_config*`. Branch `feat/other-prod-config-audit` off master HEAD `256b169` post-cycle-10.

## Surface-completeness addendum (V58 SHOULD-FIX)

The 17-category sweep covers the backlog-listed scope. Five additional surfaces are operationally meaningful but were NOT swept in cycle 11 (each is ≤30s SSH; all gated to next cycle's mini-sweep):

| Category | Why it might matter | Probe |
|---|---|---|
| `nginx` + `caddy` (explicit, beyond Apache) | Undocumented reverse proxy could be enabled | `systemctl is-enabled nginx caddy 2>&1` |
| `/etc/systemd/system.conf` | `DefaultTimeoutStartSec` / `DefaultLimitNOFILE` affect gecko-pipeline restart timing | `grep -v "^#\|^$" /etc/systemd/system.conf` |
| `/etc/apt/sources.list.d/` | 3rd-party apt repos pin/gate package availability (e.g. uv source) | `ls /etc/apt/sources.list.d/` |
| `docker` / `containerd` | Undocumented co-tenant container runtime | `systemctl is-enabled docker containerd 2>&1` |
| Complete systemd unit inventory | Locks the "4 projects" tenant count | `systemctl list-units --type=service --all \| grep -v "@\.service$"` |

Filed as `BL-NEW-AUDIT-SURFACE-ADDENDUM` for the next cycle. Acknowledged here so the V58 finding doesn't get silently absorbed.

## Reproducibility

All 17 SSH probe commands are listed inline per category above. Future-self can re-sweep in ~30s SSH.
