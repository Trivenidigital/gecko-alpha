#!/usr/bin/env bash
# gecko.db nightly LOCAL backup + snapshot refresh. Installed to
# /usr/local/bin/gecko-db-backup.sh by deploy/gecko/install.sh and run by
# gecko-db-backup.service (03:24: after the 03:00 legacy backup, off the :00/10 grid).
#
# 0. free space >= 2 x gecko.db + 1 GiB, else exit non-zero (OnFailure pages).
# 1. sqlite3 VACUUM INTO -> /root/gecko-alpha/gecko.db.bak.<UTC ts> (one read
#    transaction: never restarts under foreign commits); DELETE journal;
#    PRAGMA quick_check must say ok, else exit non-zero. A failed run leaves no .partial.
# 2. keep the newest 3 gecko.db.bak.* (the legacy rotation globs scout.db.bak.* only).
# 3. refresh /var/lib/gecko/gecko-snapshot.db (0644) for the read-only Hermes
#    gecko-query skill (gecko-agent cannot read /root): a hardlink of that backup
#    (no extra disk), or a copy when the link fails (another filesystem).
# 4. record a runs row (job db-backup) in gecko.db for freshness.
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

start=$(date -u +%Y-%m-%dT%H:%M:%SZ)  # the runs-table format (gecko.store.utcnow)
need=$(( 2 * $(stat -c %s "$DB") + 1073741824 ))
free=$(df -PB1 "$BAK_DIR" | awk 'NR==2 {print $4}')
[ "${free:-0}" -ge "$need" ] || die "free space ${free:-?} B < 2 x gecko.db + 1 GiB ($need B)"

bak=$BAK_DIR/gecko.db.bak.${start//[-:]/}
rm -f "$BAK_DIR"/gecko.db.bak.*.partial "$BAK_DIR"/gecko.db.bak.*.partial-journal  # a crashed earlier run
trap 'rm -f "$bak.partial" "$bak.partial-journal"' EXIT
# URI mode=ro: a vanished path fails instead of creating an empty database
sqlite3 "file:$DB?mode=ro" ".timeout 30000" "VACUUM INTO '$bak.partial'" || die "backup failed"
# DELETE journal: the copy is one self-contained file, no -wal/-shm sidecars
mode=$(sqlite3 "$bak.partial" "PRAGMA journal_mode=DELETE;") || die "journal_mode on backup failed"
[ "$mode" = delete ] || die "backup journal_mode is $mode"
qc=$(sqlite3 "$bak.partial" "PRAGMA quick_check;") || die "quick_check could not run on backup"
[ "$qc" = ok ] || die "quick_check on backup: $qc"
mv "$bak.partial" "$bak"
trap - EXIT
echo "backup ok: $bak ($(stat -c %s "$bak") bytes)"

# rotation: keep the newest $KEEP complete backups (exact name shape; names sort by UTC time)
mapfile -t old < <(ls -1 "$BAK_DIR" | grep -E '^gecko\.db\.bak\.[0-9]{8}T[0-9]{6}Z$' | sort | head -n -"$KEEP")
for f in "${old[@]}"; do
  rm -f -- "$BAK_DIR/$f" "$BAK_DIR/$f-wal" "$BAK_DIR/$f-shm" "$BAK_DIR/$f-journal"
  echo "rotated: $BAK_DIR/$f"
done

install -d -m 755 "$SNAP_DIR"
# the snapshot is the backup just verified (same inode), not a second read of the live DB
ln -f "$bak" "$SNAP.tmp" 2>/dev/null || cp -f "$bak" "$SNAP.tmp" || die "snapshot failed"
chmod 0644 "$SNAP.tmp"
mv -f "$SNAP.tmp" "$SNAP"
echo "snapshot ok: $SNAP"

end=$(date -u +%Y-%m-%dT%H:%M:%SZ)
row="INSERT INTO runs(job, started_at, finished_at, ok, rows_written)
     VALUES('db-backup', '$start', '$end', 1, 1);"
sqlite3 "file:$DB?mode=rw" ".timeout 30000" "$row" || die "backup ok but no runs row (freshness pages db-backup)"
