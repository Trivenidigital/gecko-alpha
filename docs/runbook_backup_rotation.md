# VPS backup rotation — operator runbook

Closes the recurring "disk-100% during deploy" incident pattern (BL-076 deploy
2026-05-04 + BL-NEW-QUOTE-PAIR deploy 2026-05-09 — see operator memory
`feedback_vps_backup_rotation.md`).

## What it does

- Daily at 03:00 UTC: `gecko-backup-rotate.sh` keeps the top-N most-recent
  `scout.db.bak.*` and `scout.db.bak-*` files in `/root/gecko-alpha/`,
  deletes the rest. N defaults to 3.
- Daily at 09:00 UTC: `gecko-backup-watchdog.sh` checks the heartbeat
  `/var/lib/gecko-alpha/backup-last-ok`. If older than 48h or missing,
  fires a Telegram alert via `scout.alerter.send_telegram_message`.

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

## Verify install

```bash
ssh srilu-vps 'systemctl status gecko-backup.timer gecko-backup-watchdog.timer'
# Both should show: active (waiting), enabled
```

```bash
ssh srilu-vps 'systemctl start gecko-backup.service && \
  journalctl -u gecko-backup.service -n 30 --no-pager'
# Should print: dir=/root/gecko-alpha found=N keep=3
# Either "no rotation needed" OR "rm -v" lines + "deleted=X retained=3"
# Final line: "heartbeat updated at /var/lib/gecko-alpha/backup-last-ok"
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

`systemctl enable --now gecko-backup.timer` sees that today's 03:00 window has
been missed (assuming install happens after 03:00 UTC) and per `Persistent=true`
fires `gecko-backup.service` IMMEDIATELY (within the `AccuracySec=1h` smear
window). Same for the watchdog at 09:00.

This is benign but surprising. The operator's manual backup, if just-created,
is the NEWEST file and is preserved as #1; older backups rotate normally. To
avoid the immediate fire entirely:

- Install during the 03:00–04:00 UTC window (no missed window).
- OR `systemctl enable gecko-backup.timer` WITHOUT `--now`, then
  `systemctl start` only when ready.

## Heartbeat is persistent across reboots

The heartbeat file lives in `/var/lib/gecko-alpha/backup-last-ok` — a
persistent path managed by the systemd `StateDirectory=gecko-alpha` directive,
not `/var/run/` (which is `tmpfs` and clears at boot). The watchdog will NOT
false-positive after a reboot.

## Race with operator's manual backup

If the operator runs `cp scout.db scout.db.bak.X` while the timer fires
concurrently, the rotation script's `flock` guard exits 3 cleanly without
rotating. Next 03:00 fire processes both files together by mtime. No
corruption, no data loss.

## Watchdog alert

If `gecko-backup.service` fails or is silently disabled, the watchdog timer
fires daily at 09:00 UTC and sends a Telegram alert via
`scout.alerter.send_telegram_message` (uses `.env` Telegram credentials —
verified wired 2026-05-06).

When you receive the alert:

1. `journalctl -u gecko-backup.service -n 100 --no-pager` — last successful run.
2. `cat /var/lib/gecko-alpha/backup-last-ok` — heartbeat timestamp.
3. Manually trigger via `systemctl start gecko-backup.service` once root
   cause is fixed.

## Run the test suite on the VPS (verification)

```bash
ssh srilu-vps 'cd /root/gecko-alpha && uv run pytest tests/test_backup_rotate_script.py -v'
# 17 tests should pass. They skip on Windows but run on Linux.
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
  rm -rf /var/lib/gecko-alpha
  rm -f /var/lock/gecko-backup-rotate.lock
'
```

## Future work (out of v1 scope)

- GPG encryption (Phase 2).
- Offsite upload to S3/Backblaze (Phase 2).
- Backup integrity verification (`PRAGMA integrity_check`).
- Pre-deploy backup hook (auto-create backup before each `git pull`).
