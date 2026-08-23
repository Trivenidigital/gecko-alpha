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

---

## Operational ledger — ad-hoc `/root` snapshot deletions

Ad-hoc snapshots live OUTSIDE the rotation policy above (`scout.db.bak.*` inside
`/root/gecko-alpha`). Nothing ages them out, so each deletion is an explicit,
operator-authorized, irreversible act and is recorded here with its evidence.

### 2026-08-23T04:52:15Z — `/root/scout.db.bak-before-state-fix` DELETED

Command: `rm -v /root/scout.db.bak-before-state-fix`, run over `ssh srilu-vps`
by the agent session recorded in the commit trailer. Deletion time is the
wall clock of the `rm`, not of this entry.

**Authorization:** operator ruling 2026-08-23, scoped to this single file.
Explicitly *not* authorized: `pre500.20260802202122`, `kraken_rehearsal`,
`pre-deploy2-20260703`, `hermes-upgrade-backup-20260801T175814Z`.

**Why it was safe to delete.** Its contents are contained in
`kraken_rehearsal/scout_copy.db` ∪ `scout.db.pre500.20260802202122`.

The claim needs THREE facts per table, not one hull, and an earlier version of
this entry printed only `min(mins)` / `max(maxes)` — from which none of the
three is recoverable. The same two numbers would look identical in a world where
kraken covered 07-17→07-20 and pre500 covered 07-25→08-02, leaving a hole
squarely inside bak. All six numbers are therefore recorded:

| table | bak min → max | kraken min → max | pre500 min → max |
|---|---|---|---|
| signal_events | 07-18 16:24 → 08-01 16:27 | 07-17 19:32 → 07-31 19:33 | 07-19 20:10 → 08-02 20:21 |
| score_history | 07-11 15:35 → 08-01 16:27 | 07-10 19:01 → 07-31 19:33 | 07-12 19:21 → 08-02 20:21 |
| volume_snapshots | 07-11 15:35 → 08-01 16:27 | 07-10 19:01 → 07-31 19:32 | 07-12 19:21 → 08-02 20:21 |
| trending_snapshots | 07-02 15:48 → 08-01 16:27 | 07-01 19:41 → 07-31 19:27 | 07-03 19:44 → 08-02 20:19 |
| volume_spikes | 06-18 05:16 → 08-01 15:24 | 06-17 09:39 → 07-31 07:36 | 06-18 19:31 → 08-02 13:54 |
| trade_decision_events | 06-17 15:32 → 08-01 16:27 | 06-16 18:59 → 07-31 19:32 | 06-18 19:21 → 08-02 20:20 |

The three premises, verified for every row above:

1. `kraken.min ≤ bak.min` — kraken opens before bak on all six;
2. `pre500.max ≥ bak.max` — pre500 closes after bak on all six;
3. `kraken.max ≥ pre500.min` — the two OVERLAP on all six, so their union is
   contiguous and contains no hole for bak's range to fall into.

**The failure mode this method actually has.** These are age-pruned tables, so
retention advances the left edge continuously and a later snapshot has a later
left edge. bak (08-01) sits between kraken and pre500 chronologically, so for
`signal_events` at 14-day retention pre500 alone covers ~07-19→08-02 while bak
opens 07-18 — the whole claim rests on kraken covering that ~1-day sliver.
Premise 1 is what carries it, which is why kraken's own minimum is recorded
above rather than folded into a hull.

**Limit of the claim, stated precisely:** range containment over append-only
tables, not a row-by-row diff. A row is unique to bak only if it falls inside
bak's range but inside neither neighbour's, which premises 1–3 exclude for these
six tables.

**Why age-pruned tables:** only a table with retention can hold rows that the
live DB no longer has. Tables without retention are strict supersets in live, so
they cannot contain bak-only rows.

**SCOPE OF THE VERIFICATION — READ THIS BEFORE RELYING ON THE CLAIM ABOVE.**
Six tables were verified. They are **not** an exhaustive enumeration of the
age-pruned tables, and an earlier version of this entry wrongly said they were
"enumerated from the prune call sites in `_run_hourly_maintenance`". They were
not: that function reaches **17** age-pruned tables, and three of the six
(`signal_events`, `trending_snapshots`, `volume_spikes`) are invoked through a
table-driven `getattr(db, prune_name)` loop that a `db.prune_` grep does not
find at all. The stated method could not have produced this list. The six were
chosen as representative during the forensics pass; the method sentence was
written afterwards and was false.

**Verified (6):** `signal_events`, `score_history`, `volume_snapshots`,
`trending_snapshots`, `volume_spikes`, `trade_decision_events`.

**NOT verified (11), all age-pruned and all reachable from the same function:**
`candidates`, `chain_matches`, `conviction_watchlist_snapshots`,
`cryptopanic_posts`, `detection_decision_receipts`, `holder_snapshots`,
`learn_logs`, `momentum_7d`, `perp_anomalies`, `txns_h1_buys_snapshots`,
**`volume_history_cg`**.

`volume_history_cg` is the one that matters. It is the labeler's price substrate
(`SELECT price, recorded_at FROM volume_history_cg`, `scout/outcome_ledger.py`),
so it is exactly the material the "only surviving source of pre-08-08 history"
note below is about — a return cannot be reconstructed without the price series.
It was not checked.

**This gap is now permanently uncloseable.** bak is deleted, so no future
verification can be run. The deletion may well still have been safe — the
argument for the six is sound and the remaining snapshots are unaffected — but
the record must not claim a completeness it does not have. If a later
investigation needs pre-08-08 `volume_history_cg` and cannot find it, this
paragraph is the reason to stop looking.

**What WAS done, and what the record failed to carry.** The three premises above
were verified against raw per-snapshot ranges before the `rm`, not after. The
original entry recorded only the outer hull, from which none of them is
recoverable — so a reader of that version could only have concluded the check
was never performed. The check was done; the record failed to preserve it. That
distinction is the reason the six numbers per table are now printed.

**Pre-deletion revalidation (all green):**

| check | result |
|---|---|
| exact path | `/root/scout.db.bak-before-state-fix` |
| size | 5,615,038,464 B (5.62 GB) |
| sha256 | `ccd716dc23cc706717ce8b71d67e85511aeb5fcb14c443343bc16eadc938c5da` — matches the 08-23 forensics classification |
| managed backup set | 3 retained (08-21, 08-22, 08-23) |
| latest managed backup `quick_check` | ok |
| live DB `quick_check` | ok |
| pipeline | active running, NRestarts 0 |
| WAL sidecars in `/root` | none (the 08-15 backup-destroying mechanism) |
| backup heartbeats | fresh, 2026-08-23T03:07:34Z |

**Post-deletion verification (all green):**

| measure | before | after |
|---|---|---|
| free bytes | 12,084,363,264 | 17,699,401,728 |
| reclaimed | — | **5,615,038,464 B — exactly the file size** |
| `df` utilization | 85% | **78%** |
| live DB `quick_check` | ok | ok |
| latest managed backup `quick_check` | ok | ok (unchanged) |
| managed backup count | 3 | 3 |
| pipeline / dashboard / trade-alert-watcher | active | active |
| Tracebacks / CRITICAL / "No space" | — | **0** |
| `signal_first_seen` rows / missing | 2594 / 0 | 2594 / 0 |

Other ad-hoc snapshots confirmed untouched at their original sizes.

**Remaining ad-hoc snapshots (~13.2 GB, all PRESERVE):** these are the only
surviving source of pre-2026-08-08 history — live `signal_events` starts at the
14-day retention boundary, so they are also the only material from which a
truthful pre-08-08 `signal_first_seen` could ever be reconstructed. Union gaps
in **`signal_events` specifically**: **07-03 → 07-17** and **08-02 → 08-08**.

That qualifier matters and an earlier version of this entry omitted it. Read
unqualified, the 07-03 → 07-17 gap falls INSIDE bak's recorded range for five of
the six tables above, which would contradict the containment claim. It does not,
because the gap is between `pre-deploy2` and `kraken` on the `signal_events`
timeline only — for the other five tables kraken opens earlier (see the table
above) and no gap exists inside bak's range. Per-table gaps outside bak's range
are irrelevant to whether bak was safe to delete.

**Reading for the next operator:** the backup peak, not the steady state, is the
number that governs headroom. Four backups plus the live DB coexist mid-run — on
2026-08-23 that peaked at **95% used / 3.9 GB free**, recovering to 85% only
after rotation deleted the oldest. Judge any change to keep-N or DB size against
that peak.
