**New primitives introduced:** NONE.

# BL-NEW-POLYMARKET-VERIFY — Findings 2026-05-18

**Data freshness:** Computed against srilu prod 2026-05-18. Re-verify via commands below if cycle re-touches `/opt/` or crontab.

**Source:** srilu `ssh srilu-vps` (HEAD = `cdeb31f` = origin/master).
**Scope:** cycle-11 V52 + V53 follow-up — verify `/opt/polymarket-ml-signal/` existence; recommend or remove stale cron entry if absent.

**Drift-check (per CLAUDE.md §7a):** worktree HEAD = `cdeb31f` = origin/master (zero divergence). grep for `polymarket` in repo returns only `backlog.md` entry — no other references; no parallel session.

**Hermes-first verdict:** In-tree operator infrastructure audit. No Hermes skill applies — this is "operator's crontab on srilu has a stale line."

## TL;DR

**Path `/opt/polymarket-ml-signal/` does NOT exist.** `/opt/` is empty (`total 8`; only `.` and `..`). Crontab still has `0 */6 * * * /opt/polymarket-ml-signal/scripts/extract_data.sh >> /var/log/ml-signal-extract.log 2>&1` — silently failing every 6 hours (the cron job runs successfully, but the shell `extract_data.sh` invocation immediately exits non-zero because the binary doesn't exist; output goes to `/var/log/ml-signal-extract.log` which the operator hasn't cited as monitored).

**Recommended action: REMOVE the stale cron line.** Safety bounds verified:
1. The line is **OUTSIDE the gecko-alpha managed block** (it precedes the `# === BEGIN gecko-alpha managed block` sentinel) → removal doesn't affect gecko-alpha cron entries
2. The script doesn't exist → removal restores **observability** (the cron exit-non-zero is currently invisible; removing the entry stops the silent failure and frees up `/var/log/ml-signal-extract.log` from churn)
3. The polymarket project is unrelated to gecko-alpha (per `feedback_lunarcrush_dropped.md` and gecko-alpha CLAUDE.md, polymarket isn't in the project's scope)

This PR does NOT auto-execute the removal (operator constraint: "do not change live config without explicit operator approval"). Operator pastes the command below.

## Evidence

```text
=== P1: /opt/polymarket-ml-signal/ existence ===
ls: cannot access '/opt/polymarket-ml-signal/': No such file or directory

=== P2: /opt/ tree ===
total 8
drwxr-xr-x  2 root root 4096 May 13 03:59 .
drwxr-xr-x 23 root root 4096 Mar 16 23:15 ..

=== P3: crontab entries mentioning polymarket ===
0 */6 * * * /opt/polymarket-ml-signal/scripts/extract_data.sh >> /var/log/ml-signal-extract.log 2>&1

=== P4: full crontab (for context) ===
0 */6 * * * /opt/polymarket-ml-signal/scripts/extract_data.sh >> /var/log/ml-signal-extract.log 2>&1
# === BEGIN gecko-alpha managed block (do not edit between sentinels) ===
30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh
45 3 * * 0 /root/gecko-alpha/scripts/wal_archive.sh
# === END gecko-alpha managed block ===
```

`/opt/` mtime is `May 13 03:59` — `/opt/polymarket-ml-signal/` was likely deleted or never installed; no other tenant data in `/opt/`.

## Operator action: remove the stale cron line

**Safe scope:** only the polymarket line is removed; the gecko-alpha managed block is untouched.

```bash
ssh srilu-vps "(crontab -l | grep -v '/opt/polymarket-ml-signal/') | crontab -"
```

Verify after:

```bash
ssh srilu-vps 'crontab -l' > /tmp/crontab_after.txt
```

Expected: only the gecko-alpha managed block remains.

**Optional cleanup of the stale log file:**

```bash
ssh srilu-vps 'rm -f /var/log/ml-signal-extract.log'
```

(Already silent today; removing the file reclaims a small amount of disk space.)

## Decision

**Close BL-NEW-POLYMARKET-VERIFY** at the audit phase. The two follow-up actions (cron removal + optional log cleanup) are operator-pastable, scoped, and reversible (simply re-adding the line restores the prior state if for any reason the polymarket project comes back).

## Cross-references

- `backlog.md` BL-NEW-POLYMARKET-VERIFY (originating, now flipping to AUDITED)
- `tasks/findings_other_prod_config_audit_2026_05_17.md` (cycle 11 originator)
- gecko-alpha managed cron block: `cron/` directory (cycle 11 BL-NEW-CRON-DRIFT-WATCHDOG context)
