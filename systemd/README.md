# systemd unit files

Production unit files for the gecko-alpha services. Captured verbatim from `srilu-vps:/etc/systemd/system/` and tracked in git so PR reviewers can see drift between repo and prod.

## Units

| File | Service | Notes |
|---|---|---|
| `gecko-pipeline.service` | main scout pipeline (`scout.main`) | long-running |
| `gecko-dashboard.service` | FastAPI dashboard (`dashboard.main:app`) | port 8000 |
| `gecko-backup.service` + `.timer` | daily backup at 03:00 | runs `scripts/backup_db.sh` |
| `gecko-backup-watchdog.service` + `.timer` | stale-heartbeat watchdog | 09:00 daily |
| `minara-emission-persistence-watchdog.service` + `.timer` | Minara emission persistence freshness | hourly |
| `systemd-drift-watchdog.service` + `.timer` | repo↔prod unit-file drift detection + drop-in enumeration | daily 09:30 UTC; alerts via TG; ack-tombstone suppresses re-alert on unchanged drift-set; manual override `rm /var/lib/gecko-alpha/systemd-drift-watchdog/last_alerted_hash` |

## Deploy workflow

After pulling a PR that touches anything in `systemd/`:

```bash
ssh root@srilu-vps
cd /root/gecko-alpha
git pull
# Post-cycle-6 review fix: enumerate via `find` instead of brace+glob.
# `cp systemd/*.{service,timer} ...` leaves the literal pattern when
# either glob has zero matches in default bash, causing cp to fail.
# `find` no-ops safely when a category is empty.
sudo find systemd -maxdepth 1 -type f \( -name "*.service" -o -name "*.timer" \) \
    -exec cp -t /etc/systemd/system/ {} +
sudo systemctl daemon-reload
sudo systemctl restart gecko-pipeline gecko-dashboard
```

**Restart blast-radius (V35 fold):**

`sudo systemctl restart gecko-pipeline` interrupts the scout pipeline for ~10-20s — that window costs missed CG scan cycles, missed paper-trade evaluations, and may trip the ingestion-starvation watchdog and the 09:00 stale-heartbeat watchdog. Prefer windows between scan cycles. `gecko-dashboard` restart drops in-flight HTTP connections (operator-facing, less critical).

**Reload semantics (V35 fold):**

- Long-running services (`gecko-pipeline`, `gecko-dashboard`) — `daemon-reload` re-reads unit files, but the running process keeps the OLD definitions until **explicit `restart`**.
- Timer-triggered oneshot services (`gecko-backup`, `gecko-backup-watchdog`, `minara-emission-persistence-watchdog`) — pick up changes on next fire after `daemon-reload`; no restart needed.
- If a `.timer` schedule (`OnCalendar=` / `OnUnitActiveSec=`) changes, additionally `systemctl restart <unit>.timer`.

## Drift audit

One-liner to spot drift:

```bash
# Post-cycle-6 review fix: same `find` enumeration as the deploy command.
# With raw globs, when a `*.timer` glob has zero matches the literal
# `systemd/*.timer` flows into the loop and the diff produces a false
# DRIFT line (basename `*.timer`, diff of nonexistent paths). `find -print0`
# + `read -d ''` is robust to empty categories AND filenames-with-spaces.
while IFS= read -r -d '' f; do
    name=$(basename "$f")
    if ! diff -q "$f" "/etc/systemd/system/$name" >/dev/null 2>&1; then
        echo "DRIFT: $name"
        diff "$f" "/etc/systemd/system/$name"
    fi
done < <(find systemd -maxdepth 1 -type f \( -name "*.service" -o -name "*.timer" \) -print0)

# Drop-in enumeration (V34 fold): surface any drop-ins for tracked units
# so the next PR can capture them. systemctl edit creates these invisibly.
while IFS= read -r -d '' f; do
    name=$(basename "$f")
    if compgen -G "/etc/systemd/system/${name}.d/*.conf" >/dev/null 2>&1; then
        echo "DROP-IN PRESENT: ${name}.d/"
        ls -la "/etc/systemd/system/${name}.d/"
    fi
done < <(find systemd -maxdepth 1 -type f \( -name "*.service" -o -name "*.timer" \) -print0)
```

**Do NOT use `sudo systemctl edit <unit>` (V34 fold)** — it writes a drop-in under `/etc/systemd/system/<unit>.service.d/override.conf`, which bypasses this audit and re-introduces the very drift this directory is meant to prevent. Any future change must go via repo PR + the deploy workflow above.

## Why this exists

Without unit files in git, PR reviewers cannot see when a deploy implicitly depends on a `Restart=always` policy, a custom `RestartSec`, or an `Environment=` override. Substrate finding from the 2026-05-16 backlog drift audit: config-not-in-git is the same class that drove that audit.
