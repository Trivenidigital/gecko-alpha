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
5. **The shipper on a schedule, deployed and observed no-op'ing first.** See
   Step 0 of the enable sequence. This is a prerequisite for the *watchdog*, not
   for the upload: enabling check 6 over a job nothing runs produces a page
   every cooldown window forever.
6. **An age-based lifecycle rule on the bucket.** Nothing prunes the remote
   side; see "Off-host retention" below.
7. **The multipart metadata probe, passed.** See "Pre-enable probe" below. It
   is the one assumption in this design that cannot be settled without real
   credentials.

## Deployment: the `/usr/local/bin` trap

`cron/gecko-alpha.crontab` invokes this script by its **repo** path
(`/root/gecko-alpha/scripts/gecko-backup-offhost.sh`), matching every other line
in that file, so for the cron lane `git pull` really does deploy the new code.

That is **not** true of the rest of the backup stack. `gecko-backup-create.sh`,
`gecko-backup-rotate.sh` and `gecko-backup-watchdog.sh` are run by systemd units
that point at `/usr/local/bin/`, so a `git pull` alone deploys **nothing** for
them — the on-box copy is whatever was last `install`ed. If you ever chain this
shipper into `gecko-backup.service` instead of leaving it on cron, it inherits
that trap:

```bash
ssh srilu-vps '
  install -m 0755 /root/gecko-alpha/scripts/gecko-backup-offhost.sh \
                  /usr/local/bin/gecko-backup-offhost.sh
  # verify the EFFECTIVE copy, not the repo copy
  md5sum /root/gecko-alpha/scripts/gecko-backup-offhost.sh \
         /usr/local/bin/gecko-backup-offhost.sh
  systemctl show gecko-backup.service -p ExecStart
'
```

Two identical md5s and an `ExecStart` naming the path you just installed. Any
other outcome means the box is running code you are not looking at.

## Configuration

The s3 transport reads these from the environment. Put them in
**`/etc/gecko-alpha/offhost.env`, mode 0600, root:root** — the cron line sources
that file. Do **not** inline them into the cron entry or into an `ssh` command
(see the enable sequence for why), and do not put the application key in `.env`,
which is readable by anything running as the same user:

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

### Keep vs delete: "wrong" and "unproven" are different verdicts

A failed verification has two distinct causes and the script acts differently on
each:

- **Provably wrong** — the remote reported a size or hash that differs from the
  local file. The object is garbage; it is **deleted** and the run exits 5.
- **Unprovable** — the remote reported no hash, or its metadata could not be
  read. The object may be a perfectly good backup that the backend simply would
  not checksum. It is **left in place** and the run exits 5.

Deleting on "unprovable" would destroy a copy to punish our own ignorance, and
it buys nothing: the heartbeat is withheld either way, so the watchdog pages
identically. It matters most in exactly the scenario the pre-enable probe below
covers — a multipart promotion that drops its metadata would otherwise delete
and re-upload the entire database every night.

Either way the heartbeat is **not** written, so a failed verification always
presents to the watchdog as a missing off-host backup.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | shipped + verified, OR already present and verified, OR lane disabled |
| 2 | misconfiguration (no backup found, missing required env, unknown transport) |
| 3 | lock contention |
| 4 | transfer failed (rsync/rclone returned non-zero) |
| 5 | **post-upload verification failed** (see keep-vs-delete below) |
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

### Plausibility floor: is the selected file actually a database?

The exclusions above answer "is this one of the artifact shapes we know about".
They cannot answer "is this a backup". A 0-byte `scout.db.bak.<tag>` newer than
every real backup passes every exclusion, is selected, uploads fine, and
**verifies** — 0 == 0, and the md5 of nothing matches the md5 of nothing — then
writes a green heartbeat over a lane whose only off-site copy is an empty file.

So the selected file must also be at least `GECKO_OFFHOST_MIN_BACKUP_BYTES`
(default 512, the smallest a valid SQLite database can be) **and** begin with
the SQLite header. Anything else aborts the run with exit 2 rather than shipping
a stub. It does not silently fall back to an older backup: a fallback would ship
a stale copy while hiding that the newest one is broken.

`gecko-backup-create.sh` cannot produce that shape, but an interrupted ad-hoc
`cp scout.db scout.db.bak.<tag>` can — and that hand-made workflow is one this
script deliberately keeps supporting.

**Known limit, stated rather than implied:** this catches empty and
non-database stubs. It does **not** catch a `cp` interrupted late, which is
already past the floor and still carries a valid header. Proving completeness
would mean an integrity check over the whole multi-GB file on every run;
`gecko-backup-create.sh` already integrity-checks what *it* produces, and this
floor is the cheap guard for the hand-made path.

> **Operator warning — a stray stub takes the whole lane down.** If a file
> matching `scout.db.bak.*` that fails the floor is the NEWEST thing in the
> backup directory, the shipper exits 2 and ships nothing. It does **not** fall
> back to the valid older backup, deliberately: a silent fallback would keep the
> lane green while hiding that the newest backup is broken. But note the
> asymmetry — a `-wal` sidecar is excluded **by name** and simply ignored,
> whereas a hand-made stub is not excluded by name and therefore halts the run.
> So an interrupted `cp scout.db scout.db.bak.tmp` stops off-host shipping until
> someone removes the file. That is loud and intended; it should not be
> discovered during an incident. If it happens, check
> `journalctl -u gecko-backup-offhost.service` for "does not look like a SQLite
> database", delete the stub, and re-run the unit.

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

**`offhost_configured` on `/health` comes from a marker, not the env.** The
dashboard runs as a separate service with no access to
`/etc/gecko-alpha/offhost.env`, and it must not be given access: that file holds
the B2 application key, and a network-facing process has no business holding it
to render a boolean. The shipper therefore writes a non-secret marker
(`/var/lib/gecko-alpha/backup-rotation/offhost-configured`, containing only the
destination label) on every configured run and removes it when the lane is
un-configured. `/health` re-reads it per request, so it needs no dashboard
restart to track reality.

**What check 6 does and does not measure.** It measures **shipper liveness** —
"the shipper ran and proved a copy landed" — not the freshness of the DATA in
that copy. If `gecko-backup-create.sh` stalls and the newest local backup goes a
week stale, the shipper keeps re-verifying that same stale object, keeps writing
a fresh heartbeat, and check 6 stays green the whole time. That is correct
division of labour, not a gap: producer freshness is
`gecko-backup-watchdog.sh`'s job, via the separate `create-last-ok` /
`backup-last-ok` heartbeats. Both watchdogs are needed; neither substitutes for
the other, and reading check 6 as "my off-site data is current" is the mistake
to avoid.

**Turn the watch on in the same change that configures the bucket, never
before.** Off-host shipping is opt-in and unconfigured by default, so a
default-on check would page on a box that has simply never had a destination —
the fastest way to train an operator to ignore this watchdog. Once enabled,
absence stops being ambiguous: a missing or unreadable heartbeat is a breach,
because "the shipper has never run" and "the shipper broke" are both worth being
woken for.

## Enable sequence

### Step 0 — INSTALL THE TRIGGER FIRST

Nothing else in this sequence is safe until the job actually runs on its own.
The watchdog measures a heartbeat; if the only thing that ever writes that
heartbeat is you, by hand, then it goes stale on schedule and check 6 pages
every cooldown window forever over a lane nothing runs. A watchdog that cries
wolf is worse than no watchdog, because it teaches the operator to skim past the
one page that was real.

**The shipper is event-triggered, not scheduled.** `gecko-backup.service` runs
`OnSuccess=gecko-backup-offhost.service`, so the shipper runs when — and only
when — a backup has just been created and rotated successfully.

Do **not** "simplify" this into a timer 30 minutes after the backup. That was
the first attempt and it is wrong: `gecko-backup.timer` sets `AccuracySec=1h` so
the backup may start anywhere in 03:00–04:00, `Persistent=true` also fires
missed runs at boot, and the backup legitimately runs for many minutes
(`TimeoutStartSec=1800`, set because 600s expired mid-`.backup` on a 6.82 GB
database). On any deferred day a fixed-time shipper re-verifies **yesterday's**
backup and writes a fresh heartbeat — a lane that reads healthy while being
permanently one day stale, and every individual run succeeds so nothing catches
it.

The units are **inert until configured** — with no bucket set the script prints
"off-host backup disabled" and exits 0. Install them first and let them no-op:

```bash
ssh srilu-vps 'cd /root/gecko-alpha && git pull'
ssh srilu-vps '
  install -m 0755 /root/gecko-alpha/scripts/gecko-backup-offhost.sh \
                  /usr/local/bin/gecko-backup-offhost.sh
  install -m 0644 /root/gecko-alpha/systemd/gecko-backup-offhost.service \
                  /etc/systemd/system/gecko-backup-offhost.service
  install -m 0644 /root/gecko-alpha/systemd/gecko-backup.service \
                  /etc/systemd/system/gecko-backup.service
  systemctl daemon-reload
  systemctl show gecko-backup.service -p OnSuccess
  systemctl show gecko-backup-offhost.service -p ExecStart
'
```

Expect `OnSuccess=gecko-backup-offhost.service` and an `ExecStart` naming
`/usr/local/bin/gecko-backup-offhost.sh`. Then fire the chain by hand and
confirm the no-op:

```bash
ssh srilu-vps 'systemctl start gecko-backup.service'
ssh srilu-vps 'journalctl -u gecko-backup-offhost.service -n 20 --no-pager'
# expect: "off-host backup disabled (set the env to enable)"
```

That proves the trigger, the installed path, the journal target and the
permissions before a single credential exists.

Note the `/usr/local/bin` install above is **not optional** — see the
Deployment section. `git pull` alone changes nothing about what these units
execute.

### Step 1 — install rclone

```bash
ssh srilu-vps 'apt install -y rclone && rclone version | head -1'
```

### Step 2 — write the credentials to a 0600 env file

**Never put the application key on a command line.** Not in the cron entry, not
in an `ssh` argument. An `ssh srilu-vps 'KEY=... script.sh'` invocation writes
the secret into your local shell history, your local ssh process argv, and the
remote's argv and environment, where any user who can read `/proc` picks it up
with `ps auxwwe`. That would defeat the argv discipline the script itself pays
for, and it is the reason the shipper takes its configuration from the
environment rather than from flags.

Create the file with a heredoc, which keeps the value out of argv:

```bash
ssh srilu-vps 'install -d -m 0700 /etc/gecko-alpha'
ssh srilu-vps 'umask 077; cat > /etc/gecko-alpha/offhost.env' <<'EOF'
GECKO_OFFHOST_BACKUP_TRANSPORT=s3
GECKO_OFFHOST_S3_BUCKET=<bucket>
GECKO_OFFHOST_S3_ENDPOINT=<endpoint>
GECKO_OFFHOST_S3_KEY_ID=<key-id>
GECKO_OFFHOST_S3_APPLICATION_KEY=<application-key>
GECKO_OFFHOST_S3_PREFIX=hosts/srilu
EOF
ssh srilu-vps 'ls -l /etc/gecko-alpha/offhost.env'   # expect -rw------- root root
```

### Step 2b — verify rclone's exit codes on THIS box

The shipper distinguishes "the object is absent" from "the metadata request
failed", because the first is reported to you as *treat the off-host copy as
missing* and the second must never be. That mapping uses rclone's documented
codes — **3 = directory not found, 4 = file not found** — and documentation is
not the running program:

```bash
ssh srilu-vps '
  set -a; . /etc/gecko-alpha/offhost.env; set +a
  export RCLONE_CONFIG_PROBE_TYPE=s3
  export RCLONE_CONFIG_PROBE_PROVIDER=Other
  export RCLONE_CONFIG_PROBE_ACCESS_KEY_ID="$GECKO_OFFHOST_S3_KEY_ID"
  export RCLONE_CONFIG_PROBE_SECRET_ACCESS_KEY="$GECKO_OFFHOST_S3_APPLICATION_KEY"
  export RCLONE_CONFIG_PROBE_ENDPOINT="$GECKO_OFFHOST_S3_ENDPOINT"
  rclone lsjson --stat --hash "probe:$GECKO_OFFHOST_S3_BUCKET/definitely-not-here"
  echo "not-found exit code: $?"
'
```

Expect **3 or 4**. Any other code means a genuinely missing object would be
reported as "unprovable" instead — the shipper would keep re-uploading and
never claim something is missing, which is the safe direction, but you should
know the mapping is off rather than discover it during a restore.

### Step 3 — first real run, by hand, sourcing that file

```bash
ssh srilu-vps '
  set -a; . /etc/gecko-alpha/offhost.env; set +a
  GECKO_BACKUP_DIR=/root/gecko-alpha \
  /root/gecko-alpha/scripts/gecko-backup-offhost.sh
' > .offhost_first_run.txt 2>&1
```

Expect `transfer ok and VERIFIED (... size=N md5=...)` followed by
`heartbeat updated`. Anything else — especially exit 5 — stops the sequence.

### Step 4 — prove idempotence

Re-run step 3 immediately. It must **skip** the upload, not repeat it: expect
`already present off-host and verified`. A second full upload means the
read-back is not matching and the daily cron would re-ship the whole database
every night.

### Step 5 — run the multipart metadata probe

Mandatory before enabling. See the next section; it cannot be answered without
real credentials and a >5 GiB object.

### Step 6 — prove the restore path end-to-end

Below. Do this **before** trusting the lane, not after the first incident.

### Step 7 — only now, turn on the watch

Set `OFFHOST_BACKUP_WATCH_ENABLED=true` on the alert-channel-watchdog cron line.
By this point the shipper has been running on a schedule for days and the
heartbeat is known-fresh, so the first thing the watchdog does is agree with
reality.

## Pre-enable probe: does the promoted object keep its md5?

**This is the one thing about this design that is unproven, and it cannot be
proven without real credentials.**

The shipper uploads to a `.partial-upload` key and promotes with `rclone
moveto`. S3's single `CopyObject` tops out at 5 GB, so a 6.8-7.4 GB `scout.db`
is promoted by a **multipart** server-side copy, which issues a fresh
`CreateMultipartUpload`. User metadata is not guaranteed to survive that, and
the whole-object md5 for a multipart upload lives in rclone's
`X-Amz-Meta-Md5chksum` **metadata** rather than in the ETag.

If B2 drops it, the promoted object reports no md5, the final verification
returns "unprovable", and the run exits 5 every night — shipping the full
database daily and never writing a heartbeat.

Run this once, under real credentials, before enabling:

```bash
# a >5 GiB object, so the promotion is genuinely multipart
ssh srilu-vps '
  set -a; . /etc/gecko-alpha/offhost.env; set +a
  dd if=/dev/urandom of=/tmp/probe.bin bs=1M count=5200
  export RCLONE_CONFIG_PROBE_TYPE=s3
  export RCLONE_CONFIG_PROBE_PROVIDER=Other
  export RCLONE_CONFIG_PROBE_ACCESS_KEY_ID="$GECKO_OFFHOST_S3_KEY_ID"
  export RCLONE_CONFIG_PROBE_SECRET_ACCESS_KEY="$GECKO_OFFHOST_S3_APPLICATION_KEY"
  export RCLONE_CONFIG_PROBE_ENDPOINT="$GECKO_OFFHOST_S3_ENDPOINT"
  B="probe:$GECKO_OFFHOST_S3_BUCKET/_probe"
  rclone copyto /tmp/probe.bin "$B.partial-upload"
  rclone lsjson --stat --hash "$B.partial-upload"   # md5 present here?
  rclone moveto "$B.partial-upload" "$B"
  rclone lsjson --stat --hash "$B"                  # STILL present here?
  rclone deletefile "$B"
  rm -f /tmp/probe.bin
'
```

**Pass:** the second `lsjson` reports a non-empty `md5` equal to the first.
**Fail:** the md5 is absent or empty after the `moveto`.

On failure, switch the shipper to upload straight to the final key
(`copyto "$NEWEST" "$REMOTE_OBJ"`, dropping the staging key and the promotion)
and rely on the single post-upload verification. That trades the
never-a-bad-object-under-a-good-name property for a working lane; take the
tradeoff only if the probe forces it.

Note that the "leave an unprovable object in place" behaviour already removes
the worst outcome here: a stripped-metadata promotion costs a page and a manual
verification, not a deleted backup and a nightly 7 GB re-upload.

## Off-host retention — this grows without bound

Each run writes a **new key** named for the backup's timestamp. At roughly 7 GB
per daily backup that is ~7 GB/day added forever. Nothing in this repo prunes
the remote side, and nothing will: the shipper deliberately never deletes, and
B2's version-based lifecycle rules do not help because every day is a distinct
**key**, not a new version of one key.

Retention has to be configured on the bucket, by the operator, as an
**age-based lifecycle rule**. Decide the window explicitly:

| Window | Steady-state size (~7 GB/day) |
|---|---|
| 7 days | ~50 GB |
| 30 days | ~210 GB |
| 90 days | ~630 GB |

The cost model that motivated this lane assumed roughly 30 daily copies
(~222 GB). If that is the intent, set a `daysFromUploadingToHiding` /
`daysFromHidingToDeleting` lifecycle rule on the bucket for 30 days **before**
the first month elapses, or the bill diverges from the plan silently.

Set it at enable time, in the same change as the bucket. It is much easier than
discovering it at 2 TB.

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
against a real Backblaze bucket has not been run.**

The specific unproven assumption is the multipart one: the whole-object MD5 for
a multipart upload (a 6.8 GB `scout.db` will be multipart) comes from rclone's
`X-Amz-Meta-Md5chksum` metadata rather than the ETag, and the `moveto` promotion
is itself a multipart server-side copy whose metadata may not survive. If B2
does not return it, the script fails **loudly** rather than reporting a false
success, and — since the unprovable case now leaves the object in place — the
cost is a page and a manual check rather than a deleted backup and a nightly
7 GB re-upload. The failure mode is safe; the pre-enable probe is what converts
this from plausible to proven.

Everything else on the "Prerequisites" list is likewise operator work that no
amount of testing here can substitute for: the bucket, the bucket-scoped key,
the lifecycle rule, and letting the schedule no-op for a day before the watch
goes on.
