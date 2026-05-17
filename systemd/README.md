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

## Deploy workflow

After pulling a PR that touches anything in `systemd/`:

```bash
ssh root@srilu-vps
cd /root/gecko-alpha
git pull
sudo cp systemd/gecko-pipeline.service /etc/systemd/system/
sudo cp systemd/gecko-dashboard.service /etc/systemd/system/
sudo cp systemd/gecko-backup.service /etc/systemd/system/
sudo cp systemd/gecko-backup.timer /etc/systemd/system/
sudo cp systemd/gecko-backup-watchdog.service /etc/systemd/system/
sudo cp systemd/gecko-backup-watchdog.timer /etc/systemd/system/
sudo cp systemd/minara-emission-persistence-watchdog.service /etc/systemd/system/
sudo cp systemd/minara-emission-persistence-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart gecko-pipeline gecko-dashboard
```

Timers don't need restart unless their schedule changed; `daemon-reload` is sufficient.

## Drift audit

One-liner to spot drift:

```bash
for f in systemd/*.service systemd/*.timer; do
    name=$(basename "$f")
    if ! diff -q "$f" "/etc/systemd/system/$name" >/dev/null 2>&1; then
        echo "DRIFT: $name"
        diff "$f" "/etc/systemd/system/$name"
    fi
done
```

Drop-ins (`/etc/systemd/system/<unit>.service.d/*.conf`) are NOT tracked in repo. If you add a drop-in, surface it here for a follow-up PR.

## Why this exists

Without unit files in git, PR reviewers cannot see when a deploy implicitly depends on a `Restart=always` policy, a custom `RestartSec`, or an `Environment=` override. Substrate finding from the 2026-05-16 backlog drift audit: config-not-in-git is the same class that drove that audit.
