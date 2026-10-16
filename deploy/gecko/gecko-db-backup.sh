#!/usr/bin/env bash
# gecko.db nightly LOCAL backup + snapshot refresh. Installed to
# /usr/local/bin/gecko-db-backup.sh by deploy/gecko/install.sh and run by
# gecko-db-backup.service (03:20, after the 03:00 legacy backup).
#
# 1. sqlite3 .backup -> /root/gecko-alpha/gecko.db.bak.<UTC ts> (DELETE journal);
#    PRAGMA quick_check must say ok, else exit non-zero (OnFailure pages).
# 2. keep the newest 3 gecko.db.bak.* (the legacy rotation globs scout.db.bak.* only).
# 3. refresh /var/lib/gecko/gecko-snapshot.db (DELETE journal, 0644) for the
#    read-only Hermes gecko-query skill (gecko-agent cannot read /root).
#
# LOCAL ONLY: this script never calls the legacy off-host transport
# (scripts/gecko-backup-offhost.sh globs scout.db.bak.* and B2 keys are not
# provisioned). Off-host shipping of gecko.db is a ticket in the followups doc.
set -euo pipefail

DB=/root/gecko-alpha/gecko.db
BAK_DIR=/root/gecko-alpha
KEEP=3
SNAP_DIR=/var/lib/gecko
SNAP=$SNAP_DIR/gecko-snapshot.db

die() { echo "gecko-db-backup: $*" >&2; exit 1; }

[ -f "$DB" ] || die "$DB missing"
command -v sqlite3 >/dev/null || die "sqlite3 missing"

ts=$(date -u +%Y%m%dT%H%M%SZ)
bak=$BAK_DIR/gecko.db.bak.$ts
rm -f "$BAK_DIR"/gecko.db.bak.*.partial  # a crashed earlier run; this unit never overlaps itself
sqlite3 "$DB" ".timeout 30000" ".backup '$bak.partial'" || die "backup failed"
# DELETE journal: the copy is one self-contained file, no -wal/-shm sidecars
mode=$(sqlite3 "$bak.partial" "PRAGMA journal_mode=DELETE;") || die "journal_mode on backup failed"
[ "$mode" = delete ] || die "backup journal_mode is $mode"
qc=$(sqlite3 "$bak.partial" "PRAGMA quick_check;") || die "quick_check could not run on backup"
[ "$qc" = ok ] || die "quick_check on backup: $qc"
mv "$bak.partial" "$bak"
echo "backup ok: $bak ($(stat -c %s "$bak") bytes)"

# rotation: keep the newest $KEEP complete backups (exact name shape; names sort by UTC time)
mapfile -t old < <(ls -1 "$BAK_DIR" | grep -E '^gecko\.db\.bak\.[0-9]{8}T[0-9]{6}Z$' | sort | head -n -"$KEEP")
for f in "${old[@]}"; do
  rm -f -- "$BAK_DIR/$f" "$BAK_DIR/$f-wal" "$BAK_DIR/$f-shm" "$BAK_DIR/$f-journal"
  echo "rotated: $BAK_DIR/$f"
done

install -d -m 755 "$SNAP_DIR"
rm -f "$SNAP.tmp"
sqlite3 "$DB" ".timeout 30000" ".backup '$SNAP.tmp'" || die "snapshot backup failed"
mode=$(sqlite3 "$SNAP.tmp" "PRAGMA journal_mode=DELETE;") || die "snapshot journal_mode failed"
[ "$mode" = delete ] || die "snapshot journal_mode is $mode"
chmod 0644 "$SNAP.tmp"
mv -f "$SNAP.tmp" "$SNAP"
echo "snapshot ok: $SNAP"
