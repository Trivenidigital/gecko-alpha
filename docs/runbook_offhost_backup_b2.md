# Off-host backup to Backblaze B2 — operator runbook

Companion to `docs/runbook_backup_rotation.md`, which covers the LOCAL backup
stack (`gecko-backup-create.sh` → `gecko-backup-rotate.sh` →
`gecko-backup-watchdog.sh`). This document covers the copy that leaves the box.

## Why this exists, and why rsync could not do it

The local stack produces, rotates, watchdogs and surfaces backups well — and
every one of them lives on the same VPS filesystem as the live `scout.db`. A
provider-level loss (host gone, volume gone, account suspended) takes the
database and all three backups together.

`scripts/gecko-backup-offhost.sh` already shipped the newest backup elsewhere,
but only over **rsync**: `GECKO_OFFHOST_BACKUP_DEST` is an rsync target spec and
the transfer is `rsync --archive --partial --inplace`. Backblaze B2 is
S3-compatible **object storage** — there is no shell on the far side, no rsync
daemon, no filesystem — so rsync cannot reach it at any configuration value.
That is why this needed an actual object-storage transport rather than a new
destination string.

The script now selects its transport:

| `GECKO_OFFHOST_BACKUP_TRANSPORT` | Transfer | Destination env |
|---|---|---|
| `rsync` (default) | `rsync` | `GECKO_OFFHOST_BACKUP_DEST` |
| `s3` | `rclone` → B2 or any S3-compatible endpoint | `GECKO_OFFHOST_S3_BUCKET` |

Everything above the transfer — the disabled-by-default gate, the `flock` guard,
the completed-backup selection, the heartbeat — is shared. That was deliberate:
the selection logic is the piece that has destroyed prod backups twice, and a
forked parallel script would have duplicated exactly it.

## Prerequisites (all still outstanding — this lane cannot be enabled yet)

1. **`rclone` on the VPS.** It is not installed today. Without it the s3
   transport exits **6** with the install hint; it does not fail silently.
   ```bash
   ssh srilu-vps 'apt install -y rclone && rclone version'
   ```
   Minimum version 1.59 — the verification step uses `rclone lsjson --stat`.
2. **A Backblaze B2 bucket** for these backups and nothing else.
3. **A BUCKET-SCOPED application key** — "Add a New Application Key" restricted
   to that one bucket. Never the account master key. The key this script holds
   can, by design, overwrite the off-host copies; it must not be able to touch
   anything else in the account.
4. **The S3 endpoint** for the bucket's region, e.g.
   `https://s3.us-west-004.backblazeb2.com`.

## Configuration

The s3 transport reads these from the environment (the cron line, or `.env` if
the operator prefers — but note that `.env` is world-readable to anything
running as the same user, so a `chmod 0600` cron-env file is the better home for
the application key):

| Variable | Required | Meaning |
|---|---|---|
| `GECKO_OFFHOST_BACKUP_TRANSPORT` | yes | set to `s3` |
| `GECKO_OFFHOST_S3_BUCKET` | yes | bucket name. **Empty = lane disabled, exit 0** |
| `GECKO_OFFHOST_S3_ENDPOINT` | yes | S3 endpoint URL |
| `GECKO_OFFHOST_S3_KEY_ID` | yes | application key ID |
| `GECKO_OFFHOST_S3_APPLICATION_KEY` | yes | application key (secret) |
| `GECKO_OFFHOST_S3_PREFIX` | no | key prefix inside the bucket, e.g. `hosts/srilu` |
| `GECKO_OFFHOST_S3_REGION` | no | only if the provider needs it |
| `GECKO_OFFHOST_S3_PROVIDER` | no | rclone `provider` value; default `Other`, correct for B2's S3 API |

Naming a bucket while leaving a credential unset is a **misconfiguration
(exit 2)**, not a quiet skip: once a bucket is named the operator believes
backups are leaving the box, and exiting 0 there would satisfy the cron while
shipping nothing.

Credentials are handed to rclone through `RCLONE_CONFIG_GECKOOFFHOST_*`
environment variables exported inside a subshell — never on the command line,
which `ps` and `/proc/*/cmdline` expose to every process on the box. Anything
rclone prints is passed through a literal-substitution redactor before it
reaches the cron log.

## Verification is not an exit code

A zero exit from the upload is **not** accepted as proof the bytes landed. After
every upload the script reads the object's **size and content hash** back from
the remote and compares both to the local file. A mismatch, or a remote that
cannot report a hash at all, is a hard failure that does **not** write the
heartbeat.

The sequence, and why it is shaped this way:

1. Read back the final key. If size **and** md5 already match the local backup,
   skip the upload entirely and refresh the heartbeat — the daily cron then
   costs one metadata request once the day's backup is safe. Keying the skip on
   the hash and not on mere existence is what stops a same-size stale object
   from being accepted as today's backup.
2. Upload to a `.partial-upload` staging key. (The `.partial` is load-bearing:
   if such an object is ever pulled back down into a backup directory, the
   selection loop's reserved-namespace rule skips it.)
3. Verify the staged object. On failure: delete it and exit **5**. The real
   backup name was never written, so nothing corrupt is presented as good.
4. Promote server-side (`rclone moveto`).
5. Verify **the promoted object** — the name a restore would actually fetch.
   The promotion is a server-side copy, so its bytes and hash metadata are the
   remote's word, not ours. On failure: delete it and exit 5.
6. Only then write the heartbeat.

The rsync transport keeps its historical behaviour; the read-back is specific to
the object-storage path, where the upload is an HTTP request that can succeed
while storing something else.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | shipped + verified, OR already present and verified, OR lane disabled |
| 2 | misconfiguration (no backup found, missing required env, unknown transport) |
| 3 | lock contention |
| 4 | transfer failed (rsync/rclone returned non-zero) |
| 5 | **post-upload verification failed** |
| 6 | transport binary missing (`rsync` / `rclone` / `md5sum`) |

## What is shipped, and what is never shipped

The newest **completed** backup only. Two families are excluded, each from an
incident:

- `*.partial*` — the reserved in-progress namespace owned by
  `gecko-backup-create.sh`. Five such files accumulated on prod on 2026-08-08.
- `*-wal`, `*-shm`, `*-journal` — SQLite sidecars minted beside a *completed*
  backup. Their names contain no `.partial`, so the first exclusion never
  covered them.

Both families are systematically **newer** than the backup they sit beside, and
selection is by mtime — so either one can win and be shipped off-host as though
it were a backup, replacing the one copy that exists outside the box with a
stub. The exclusions anchor to the end of the name, so an operator tag that
merely contains one of the words (`scout.db.bak.before-wal-migration`) is still
a backup and is still shipped.

**The local copy is never deleted on upload success.** Off-host shipping is
additive; local keep-N retention belongs to `gecko-backup-rotate.sh` and stays
independent of whether this script ran at all.

## Monitoring (CLAUDE.md §12a)

`scripts/gecko-backup-offhost.sh` writes `offhost-last-ok` only after a copy is
proven good, so freshness of that file means "a VERIFIED off-site copy existed
at that instant". `scripts/alert_channel_watchdog.py` reads it as its sixth
check and pages when it goes stale, missing, or unreadable.

It rides the existing hourly cron line and the existing per-table 24h send
cooldown. Env, read from the cron environment:

| Variable | Default | Meaning |
|---|---|---|
| `OFFHOST_BACKUP_WATCH_ENABLED` | `false` | deploy-without-activate gate |
| `OFFHOST_BACKUP_SLO_HOURS` | `48` | staleness threshold |
| `OFFHOST_BACKUP_HEARTBEAT_FILE` | `/var/lib/gecko-alpha/backup-rotation/offhost-last-ok` | path |

48h rather than 27h because the shipper rides the daily backup schedule: one
missed night on a disaster-recovery lane is not worth a page, two in a row is.

**Turn the watch on in the same change that configures the bucket, never
before.** Off-host shipping is opt-in and unconfigured by default, so a
default-on check would page on a box that has simply never had a destination —
the fastest way to train an operator to ignore this watchdog. Once enabled,
absence stops being ambiguous: a missing or unreadable heartbeat is a breach,
because "the shipper has never run" and "the shipper broke" are both worth being
woken for.

## Enable sequence

```bash
# 1. install rclone (see prerequisites) and confirm the version
ssh srilu-vps 'rclone version | head -1'

# 2. dry-run the shipper by hand, with the lane configured but the watch off
ssh srilu-vps '
  GECKO_OFFHOST_BACKUP_TRANSPORT=s3 \
  GECKO_OFFHOST_S3_BUCKET=<bucket> \
  GECKO_OFFHOST_S3_ENDPOINT=<endpoint> \
  GECKO_OFFHOST_S3_KEY_ID=<key-id> \
  GECKO_OFFHOST_S3_APPLICATION_KEY=<app-key> \
  GECKO_OFFHOST_S3_PREFIX=hosts/srilu \
  GECKO_BACKUP_DIR=/root/gecko-alpha \
  /root/gecko-alpha/scripts/gecko-backup-offhost.sh
' > .offhost_first_run.txt 2>&1
```

Expect `transfer ok and VERIFIED (... size=N md5=...)` followed by
`heartbeat updated`. Anything else — especially exit 5 — stops the sequence.

```bash
# 3. re-run immediately: it must SKIP the upload, not repeat it
#    (expect "already present off-host and verified")

# 4. prove the restore path end-to-end (below) BEFORE trusting the lane

# 5. only then set OFFHOST_BACKUP_WATCH_ENABLED=true on the
#    alert-channel-watchdog cron line
```

## Restore procedure

```bash
# 1. what is off-host?
rclone lsl "$REMOTE:$BUCKET/$PREFIX/"

# 2. download
rclone copyto "$REMOTE:$BUCKET/$PREFIX/scout.db.bak.<ts>" ./restore.db

# 3. verify against the object store's own hash BEFORE trusting the file
md5sum < ./restore.db
rclone lsjson --stat --hash "$REMOTE:$BUCKET/$PREFIX/scout.db.bak.<ts>"

# 4. integrity-check the downloaded file
sqlite3 "file:$PWD/restore.db?mode=ro&immutable=1" 'PRAGMA quick_check;'
```

### `immutable=1` is mandatory, not decoration

A backup carries the live DB's WAL-mode header, so **any** ordinary open —
read-only included — makes SQLite mint `-wal` and `-shm` files beside it.

On 2026-08-15 an operator ran exactly this integrity check over the two newest
local backups with a plain `mode=ro` URI. The sidecars it created had fresh
mtimes, mtime-descending rotation handed them the retention slots, and all three
real backups were deleted (`deleted=5 retained=2`, where the two survivors were
a 32K `-shm` and a 0-byte `-wal`). The integrity check destroyed the thing it
was verifying.

Every read of a backup file — here, in `gecko-backup-create.sh`, and in anything
written later — uses `file:...?mode=ro&immutable=1`. Rotation and off-host
selection also exclude those suffixes outright, because a retention policy must
not depend on every future reader remembering a URI parameter.

```bash
# 5. promote
systemctl stop gecko-pipeline
mv /root/gecko-alpha/scout.db /root/gecko-alpha/scout.db.pre-restore
mv ./restore.db /root/gecko-alpha/scout.db
systemctl start gecko-pipeline
```

## Testing

- `tests/test_offhost_backup_s3_script.py` — the s3 transport, exercised against
  a fake rclone that implements a local-filesystem object store. That fake is
  what makes the verification tests possible: it can store the wrong number of
  bytes, or the right number of wrong bytes, or refuse to report a hash, while
  still exiting 0 — the exact failure class a real endpoint will not produce on
  demand.
- `tests/test_round20_offhost_backup_script.py` — the rsync transport and the
  shared selection logic, including both sidecar incident families.

Both are bash-driven and skip on Windows; CI is authoritative.

## Still outstanding before this lane is real

Nothing in this PR has touched a live endpoint. The transport, the verification
and the watchdog are proven against a fake; **`rclone lsjson --stat --hash`
against a real Backblaze bucket has not been run**, and the whole-object MD5 for
a multipart upload (a 6.8 GB `scout.db` will be multipart) comes from rclone's
`X-Amz-Meta-Md5chksum` metadata rather than the ETag. If B2 does not return it,
the script fails **loudly** at step 3 rather than reporting a false success — so
the failure mode is safe, but the first live run under operator credentials is
what converts this from plausible to proven.
