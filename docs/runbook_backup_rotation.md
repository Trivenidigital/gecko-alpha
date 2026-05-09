# VPS backup rotation — operator runbook

Closes the recurring "disk-100% during deploy" incident pattern (BL-076 deploy
2026-05-04 + BL-NEW-QUOTE-PAIR deploy 2026-05-09 — see operator memory
`feedback_vps_backup_rotation.md`).

## What it does

- Daily at 03:00 UTC: `gecko-backup-rotate.sh` keeps the top-N most-recent
  `scout.db.bak.*` and `scout.db.bak-*` files in `/root/gecko-alpha/`,
  deletes the rest. N defaults to 3.
- Daily at 09:00 UTC: `gecko-backup-watchdog.sh` checks the heartbeat at
  `/var/lib/gecko-alpha/backup-rotation/backup-last-ok`. If older than 48h,
  missing, or corrupt (empty / non-numeric), fires a Telegram alert via
  direct `curl` to the bot API.

## Pre-install: kernel + systemd version check

```bash
ssh srilu-vps 'uname -r && systemctl --version | head -1'
# Expected: Linux 6.x kernel, systemd 252+ (Ubuntu 24.04 LTS)
```

## One-time install

```bash
ssh srilu-vps 'cd /root/gecko-alpha && git pull'

ssh srilu-vps '
  install -m 0755 /root/gecko-alpha/scripts/gecko-backup-rotate.sh \
                  /usr/local/bin/gecko-backup-rotate.sh
  install -m 0755 /root/gecko-alpha/scripts/gecko-backup-watchdog.sh \
                  /usr/local/bin/gecko-backup-watchdog.sh
'

ssh srilu-vps '
  install -m 0644 /root/gecko-alpha/systemd/gecko-backup.service \
                  /etc/systemd/system/gecko-backup.service
  install -m 0644 /root/gecko-alpha/systemd/gecko-backup.timer \
                  /etc/systemd/system/gecko-backup.timer
  install -m 0644 /root/gecko-alpha/systemd/gecko-backup-watchdog.service \
                  /etc/systemd/system/gecko-backup-watchdog.service
  install -m 0644 /root/gecko-alpha/systemd/gecko-backup-watchdog.timer \
                  /etc/systemd/system/gecko-backup-watchdog.timer
  systemctl daemon-reload
'

ssh srilu-vps '
  systemctl enable --now gecko-backup.timer
  systemctl enable --now gecko-backup-watchdog.timer
  systemctl list-timers gecko-backup gecko-backup-watchdog
'
```

## Smoke test #1: rotation manually fires

```bash
ssh srilu-vps 'systemctl start gecko-backup.service && \
  journalctl -u gecko-backup.service -n 30 --no-pager'
```

Expected: `gecko-backup-rotate: dir=/root/gecko-alpha found=N keep=3` and either
`no rotation needed` OR `rotating X files:` + per-file paths + final line
`heartbeat updated at /var/lib/gecko-alpha/backup-rotation/backup-last-ok`.

## Smoke test #2: watchdog Telegram delivery (R7 MUST-FIX)

Without this step, a misconfigured watchdog fails silently for 48h before the
operator notices.

```bash
ssh srilu-vps '
  date -d "50 hours ago" +%s > /var/lib/gecko-alpha/backup-rotation/backup-last-ok
  systemctl start gecko-backup-watchdog.service
  journalctl -u gecko-backup-watchdog.service -n 20 --no-pager
'
```

Expected: Telegram message arrives in operator's chat within ~5s; journal
shows `ALERT DELIVERED: HTTP 200`. If you see `ERROR: TELEGRAM_BOT_TOKEN
missing/placeholder`, fix `.env` before relying on this watchdog.

After the smoke test, restore the heartbeat:

```bash
ssh srilu-vps 'systemctl start gecko-backup.service'
```

## Manual rotation (e.g., disk pressure before next 03:00)

```bash
ssh srilu-vps 'systemctl start gecko-backup.service && \
  journalctl -u gecko-backup.service -n 30 --no-pager'
```

## Override retention count

Edit `Environment=GECKO_BACKUP_KEEP=3` in
`/etc/systemd/system/gecko-backup.service`, then:

```bash
ssh srilu-vps 'systemctl daemon-reload && systemctl restart gecko-backup.timer'
```

## Persistent=true behavior — operator warning

`systemctl enable --now gecko-backup.timer` after 03:00 UTC fires the rotation
service immediately (within `AccuracySec=1h`). Same for the watchdog at 09:00.
The newest manual backup (if any) is preserved as #1; older backups rotate.
To avoid the immediate fire: install during 03:00–04:00 UTC, or use
`systemctl enable` without `--now`.

## Heartbeat is persistent across reboots

The heartbeat lives in `/var/lib/gecko-alpha/backup-rotation/`
(`StateDirectory=gecko-alpha` + script `mkdir -p`). NOT `/var/run` (`tmpfs`).
The watchdog will NOT false-positive after a reboot.

## Race with operator's manual backup

If `cp scout.db scout.db.bak.X` runs concurrently with the timer, the
rotation script's `flock` guard exits 3 cleanly. Next 03:00 fire processes
both files together by mtime.

## Watchdog alert delivery path

The watchdog reads `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` from
`/root/gecko-alpha/.env` and POSTs directly to the Telegram bot API via
`curl`. It does NOT use `scout.alerter.send_telegram_message` (which takes
3 positional args including `aiohttp.ClientSession` and swallows HTTP errors).

Watchdog exit codes:
- `0` — heartbeat fresh, no alert needed
- `1` — heartbeat stale/missing/corrupt, alert delivered (HTTP 200)
- `4` — env file not found
- `5` — credentials missing/placeholder
- `6` — system python missing
- `7` — Telegram API delivery failed (HTTP non-200)

When you receive a Telegram alert:
1. `journalctl -u gecko-backup.service -n 100 --no-pager`
2. `cat /var/lib/gecko-alpha/backup-rotation/backup-last-ok`
3. Manually trigger `systemctl start gecko-backup.service` after fix.

When the watchdog itself enters `failed` state (no second-channel alert):
1. `systemctl status gecko-backup-watchdog.service`
2. `journalctl -u gecko-backup-watchdog.service -n 50` — diagnose exit code.
3. Fix `.env` credentials or network egress; restart timer.

## Run the test suite on the VPS

```bash
ssh srilu-vps 'cd /root/gecko-alpha && /root/.local/bin/uv run pytest tests/test_backup_rotate_script.py -v'
```

## Disable / revert

```bash
ssh srilu-vps '
  systemctl disable --now gecko-backup.timer gecko-backup-watchdog.timer
  systemctl stop gecko-backup.service gecko-backup-watchdog.service
  rm -f /usr/local/bin/gecko-backup-rotate.sh \
        /usr/local/bin/gecko-backup-watchdog.sh \
        /etc/systemd/system/gecko-backup.{service,timer} \
        /etc/systemd/system/gecko-backup-watchdog.{service,timer}
  systemctl daemon-reload
  rm -rf /var/lib/gecko-alpha/backup-rotation
  rm -f /var/lock/gecko-backup-rotate.lock
'
```

## Future work (out of v1 scope)

- GPG encryption (Phase 2).
- Offsite upload to S3/Backblaze (Phase 2).
- Backup integrity verification (`PRAGMA integrity_check`).
- Pre-deploy backup hook (auto-create backup before each `git pull`).
- Second-channel watchdog alert (e.g., file drop in `/var/log/...`) so
  a Telegram-API outage doesn't leave the operator silently uninformed.
  Today: if Telegram is down, the watchdog enters `failed` state and the
  operator must observe via `systemctl status` or `journalctl`. Acceptable
  trade-off for v1; documented here as known gap.
