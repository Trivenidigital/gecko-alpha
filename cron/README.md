# cron/

Repo-tracked source-of-truth for the gecko-alpha crontab entries on srilu. Mirrors the `systemd/` pattern from cycle 6.

## Files

| File | Purpose |
|---|---|
| `gecko-alpha.crontab` | Sentinel-bracketed managed block with the 2 weekly archive cron entries |
| `deploy.sh` | Idempotent awk-based merge script |

## Deploy

After pulling a PR that touches anything in `cron/`:

```bash
ssh root@srilu-vps
cd /root/gecko-alpha
git pull
bash cron/deploy.sh
crontab -l   # verify
```

The deploy script:
- Reads `cron/gecko-alpha.crontab` (the sentinel-bracketed managed block)
- Reads current crontab via `crontab -l`
- Replaces the existing managed block (if present) OR appends if first deploy
- Preserves ANY operator-added entries OUTSIDE the sentinels (e.g., polymarket cron entry)
- Stages to tempfile, then atomically installs via `crontab <tempfile>`

Idempotent: re-running `cron/deploy.sh` produces an identical crontab.

## Sentinel convention

The managed block is bracketed by:

```
# === BEGIN gecko-alpha managed block (do not edit between sentinels) ===
<lines>
# === END gecko-alpha managed block ===
```

**Do not hand-edit the sentinel lines** — the awk regex matches them as literal anchors. Edits to the entries themselves go via repo PR + redeploy.

## What's in scope

Currently 2 weekly entries:
- `30 3 * * 0` — `scripts/tg_burst_archive.sh` (Sunday 03:30 UTC)
- `45 3 * * 0` — `scripts/wal_archive.sh` (Sunday 03:45 UTC)

Future high-cadence triggers should prefer `systemd/*.timer` (cycle 10 canon) over cron. The 2 weekly entries stay as cron for simplicity (see BL-NEW-CRON-TO-SYSTEMD-TIMER decision-by 2026-06-14).

## Why this exists

Cycle 11 audit (`tasks/findings_other_prod_config_audit_2026_05_17.md`) found these 2 cron entries existed on srilu but were repo-untracked at the schedule level. The shell scripts were in repo, but their cron schedule was operator-only. Same substrate class as cycle 6 (BL-NEW-SYSTEMD-UNIT-IN-REPO).

## Drift detection

Cycle 11 follow-up `BL-NEW-CRON-DRIFT-WATCHDOG` (TBD) will add a daily watchdog mirroring cycle 10's `systemd-drift-watchdog.sh` for cron entries.
