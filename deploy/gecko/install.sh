#!/usr/bin/env bash
# Install (idempotent) or remove the gecko systemd units. Run as root from the repo:
#   deploy/gecko/install.sh [--dry-run] [--with-coingecko] [--no-start]
#   deploy/gecko/install.sh --remove [--dry-run]
# Installs: gecko-job@.service (+ the coingecko-markets timeout drop-in),
# gecko-telegram-reader.service (NOT enabled: the owner runs tg-login first),
# gecko-db-backup.service, every deploy/gecko/timers/*.timer, and
# /usr/local/bin/gecko-db-backup.sh. Enables + starts every timer except the two
# coingecko-* timers, which need --with-coingecko (a CoinGecko Demo key).
# --no-start installs and enables the same timers but starts none of them.
# --remove disables + stops every gecko timer and unit, deletes the unit files
# and the backup script, and leaves all data (gecko.db, backups, snapshot, .env.gecko).
set -euo pipefail

SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
UNIT_DIR=/etc/systemd/system
DRY=0 WITH_CG=0 REMOVE=0 NO_START=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --with-coingecko) WITH_CG=1 ;;
    --remove) REMOVE=1 ;;
    --no-start) NO_START=1 ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done

run() {
  echo "+ $*"
  if [ "$DRY" = 0 ]; then "$@"; fi
}

timers=()
for t in "$SRC"/timers/*.timer; do timers+=("$(basename "$t")"); done
[ "${#timers[@]}" -gt 0 ] || { echo "no timers under $SRC/timers" >&2; exit 1; }
ENABLE=(systemctl enable --now)
[ "$NO_START" = 0 ] || ENABLE=(systemctl enable)
units=(gecko-job@.service gecko-telegram-reader.service gecko-db-backup.service)
DROPIN=gecko-job@coingecko-markets.service.d

if [ "$REMOVE" = 1 ]; then
  for t in "${timers[@]}"; do
    run systemctl disable --now "$t" || true
  done
  run systemctl disable --now gecko-telegram-reader.service || true
  run systemctl stop 'gecko-job@*.service' gecko-db-backup.service || true
  for t in "${timers[@]}"; do run rm -f "$UNIT_DIR/$t"; done
  for u in "${units[@]}"; do run rm -f "$UNIT_DIR/$u"; done
  run rm -rf "${UNIT_DIR:?}/$DROPIN"
  run rm -f /usr/local/bin/gecko-db-backup.sh
  run systemctl daemon-reload
  echo "removed; data left in place (gecko.db, gecko.db.bak.*, /var/lib/gecko, .env.gecko)"
  exit 0
fi

[ "$DRY" = 1 ] || [ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

for u in "${units[@]}"; do run install -m 0644 "$SRC/$u" "$UNIT_DIR/$u"; done
run install -d -m 0755 "$UNIT_DIR/$DROPIN"
run install -m 0644 "$SRC/$DROPIN/timeout.conf" "$UNIT_DIR/$DROPIN/timeout.conf"
for t in "${timers[@]}"; do run install -m 0644 "$SRC/timers/$t" "$UNIT_DIR/$t"; done
# scripts run from /usr/local/bin: a git pull alone deploys nothing, re-run install.sh
run install -m 0755 "$SRC/gecko-db-backup.sh" /usr/local/bin/gecko-db-backup.sh
run install -d -m 755 /var/lib/gecko
run install -d -m 700 /root/.gecko
run systemctl daemon-reload

for t in "${timers[@]}"; do
  case "$t" in
    gecko-coingecko-*)
      if [ "$WITH_CG" = 1 ]; then
        run "${ENABLE[@]}" "$t"
      else
        echo "skip $t (installed, disabled: pass --with-coingecko once a Demo key exists)"
      fi ;;
    *) run "${ENABLE[@]}" "$t" ;;
  esac
done
echo "gecko-telegram-reader.service installed, NOT enabled (runbook: tg-login first)"
run systemctl list-timers 'gecko-*' --all --no-pager
