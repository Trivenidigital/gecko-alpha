**New primitives introduced:** NONE (runbook).

# Runbook: gecko activation on srilu-vps (Plan 3)

Activates the lean `gecko/` package alongside the legacy pipeline: its own
database (`/root/gecko-alpha/gecko.db`), its own env file
(`/root/gecko-alpha/.env.gecko`), its own systemd units (`deploy/gecko/`).
Nothing here touches `scout.db`, the legacy `.env` or any legacy unit.

Every command runs as root on the box (`ssh srilu-vps`) from `/root/gecko-alpha`
unless stated. Over SSH from Windows, redirect output to a file and read the
file (the Bash tool cannot capture SSH stdout).

Each stage needs its own recorded operator approval (Approvals log at the end).
Stop at the first unexpected output; never improvise past a failed check.

## 0. Preflight

```bash
cd /root/gecko-alpha
git status --short --branch                            # clean tracked tree, on master
git fetch origin && git merge --ff-only origin/master  # fails (diverged/dirty): STOP, report, never merge
git rev-parse HEAD                                     # = the approved SHA, else STOP and report
df -h /                                                # >= 8 GiB free (gecko.db + history copy + backups)
command -v sqlite3 && sqlite3 --version                # sqlite3 CLI present
/root/.local/bin/uv sync --frozen                      # gecko deps from uv.lock
find gecko -name __pycache__ -type d -prune -exec rm -rf {} +
/root/.local/bin/uv run --frozen python -m gecko --list   # every job listed
grep -c '^GECKO_' .env                                 # must print 0: GECKO_* keys crash the legacy settings
ls /etc/systemd/system/codex-systemd-failure-alert@.service   # the OnFailure= target exists
systemctl --version | head -1                         # >= 254 for the reader's RestartMode=direct
```

The reader unit's `RestartMode=direct` needs systemd 254 or newer (the VPS runs
255). On older systemd the key is ignored with a warning; auto-restarts do not
pass through the failed state there, so OnFailure pages once, when the start
limit (5 per hour) is hit.

## 1. Install the units (`--no-start`)

Run this immediately after the preflight fast-forward: it needs no
`.env.gecko` and starts nothing, and running it first shrinks the window in
which the drift watchdog sees repo units missing in prod from tens of minutes
to seconds.

```bash
cd /root/gecko-alpha
deploy/gecko/install.sh --dry-run --no-start   # review every action first
deploy/gecko/install.sh --no-start             # install + enable, start nothing yet
```

Installs `gecko-job@.service` (+ the coingecko-markets timeout drop-in),
`gecko-collector@.service` (the same runner without `OnFailure=`),
`gecko-telegram-reader.service` (installed, NOT enabled),
`gecko-db-backup.service`, every `gecko-*.timer`, and
`/usr/local/bin/gecko-db-backup.sh`. With `--no-start` it enables every timer
except the two `coingecko-*` timers but starts none of them, so sections 3 to 6
run by hand before anything is scheduled; the timers are started in section 6b.
It prints `systemctl list-timers 'gecko-*'`.

`scripts/systemd-drift-watchdog.sh` tracks `deploy/gecko/`, its timers and its
drop-ins. Between the code update and this step it would report every gecko
unit as missing in prod; running this step right after the fast-forward keeps
that window to seconds.

Scripts run from `/usr/local/bin`, so a later `git pull` changes nothing there:
re-run `install.sh` after any change under `deploy/gecko/`. The
`--with-coingecko` flag is the single source of truth for the two
`coingecko-*` timers: a run without it disables and stops them, so once
section 10 is done pass it on every re-run. `install.sh` prints their real
`is-enabled` state, and ends with `systemctl try-restart
gecko-telegram-reader.service`, which restarts the reader onto the new code
only if it is already running.

A timer cannot start an instance that is still running, so no job overlaps
itself (the enrich budget no-overlap requirement). A SIGKILLed run still
records no credits; that gap stays open.

Both templates run the job under `/usr/bin/flock -n /run/lock/gecko-<job>.lock`,
so the lock now enforces no overlap across them: a `gecko-job@dexscreener` run
started while a timer-started `gecko-collector@dexscreener` run is live exits 1
at once (that pages under gecko-job@; a collector run lost to the lock is
freshness-covered). Never run a scheduled job as `uv run python -m gecko <job>`
while its timer is live: that bypasses the lock. Start the unit its timer
starts: `systemctl start gecko-collector@<job>.service` for dexscreener,
geckoterminal and geckoterminal-ohlcv, `gecko-job@<job>.service` for every
other job.

Schedules (UTC, `deploy/gecko/timers/`):

| Unit | Schedule |
|---|---|
| dexscreener | every 10 min (`*:00/10`, +up to 60 s random delay) |
| geckoterminal | every 15 min from :07 |
| freshness | every 30 min from :05 |
| enrich / benchmark / rank / coingecko-hourly | hourly at :17 / :35 / :45 / :50 |
| coingecko-markets | 02:30 |
| gecko-db-backup | 03:24 (after the 03:00 legacy backup, off the dexscreener grid) |
| prune | 03:40 |
| geckoterminal-ohlcv | 04:20 |
| ledger | 05:10 |
| research | 06:00 |
| digest | 07:00 |
| scorecard | Monday 08:00 |
| telegram-reader | long-running service (section 9) |

Paging. Every `gecko-job@` instance, the reader and `gecko-db-backup` page the
codex failure handler on each failed run (`OnFailure=`). The dexscreener,
geckoterminal and geckoterminal-ohlcv timers start `gecko-collector@<job>`
instead: the same runner (a test pins the two templates identical apart from
`Description=` and `OnFailure=`) with no `OnFailure=`, so a high-frequency
collector does not page per failed run. Their outages page only through
`freshness` (per-source SLOs, deduplicated for 6 h). An empty `OnFailure=` in
a drop-in does NOT clear the template's list (checked on systemd 255), hence a
separate template. Check after the install:

```bash
systemctl show -p OnFailure --value gecko-collector@dexscreener.service   # prints nothing
systemctl show -p OnFailure --value gecko-job@rank.service                # codex-systemd-failure-alert@...
```

Each job and the reader run under `MemoryMax=512M`; an out-of-memory kill
fails the unit like any other failure.

An enabled timer also starts at the next boot, so do not reboot between this
step and section 6b.

## 2. Env file, directories, history copy

This is the prep script (`plan3_vps_prep.sh`); it is idempotent and never
echoes secret values. Its equivalent steps:

**2a. `.env.gecko` (mode 0600)** by copying values from the legacy `.env`.
A key is written only when its legacy value is non-empty: an empty secret
parses as an empty string, not as unset, and slips past the "key missing"
guards.

```bash
cd /root/gecko-alpha
[ -e .env.gecko ] && echo "exists: stop and review, do not overwrite" && exit 1
val() { grep -E "^$1=" .env | head -1 | cut -d= -f2- | tr -d '\r' \
        | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"; }
put() { v=$(val "$2"); [ -n "$v" ] && printf '%s=%s\n' "$1" "$v" >> .env.gecko \
        || echo "WARN legacy $2 empty: $1 not written"; }
umask 077
# non-secret defaults; secrets are commented out there; drop the placeholders we fill below
grep -v -E '^(GECKO_TELEGRAM_HEALTH_CHAT_ID|GECKO_TELEGRAM_DIGEST_CHAT_ID|GECKO_TG_API_ID)='   deploy/gecko/env.gecko.example > .env.gecko
put GECKO_TELEGRAM_BOT_TOKEN      TELEGRAM_BOT_TOKEN
put GECKO_TELEGRAM_HEALTH_CHAT_ID TELEGRAM_HEALTH_CHAT_ID
put GECKO_TELEGRAM_DIGEST_CHAT_ID TELEGRAM_CHAT_ID
put GECKO_TG_API_ID               TG_SOCIAL_API_ID
put GECKO_TG_API_HASH             TG_SOCIAL_API_HASH
put GECKO_ANTHROPIC_API_KEY       ANTHROPIC_API_KEY
chmod 600 .env.gecko
# keys only, never values:
grep -E '^GECKO_' .env.gecko | sed -E 's/=.*//' | sort | uniq -d   # must print nothing
stat -c '%a %U' .env.gecko                                         # 600 root
```

If the prep script already created `.env.gecko`, verify it instead: keys only,
no duplicates, mode 600, and no line of the form `GECKO_<SECRET>=` with an
empty value (delete such a line rather than leaving it blank).

Owner-provided, not copied:

- `GECKO_COINGECKO_DEMO_API_KEY`: a free CoinGecko Demo key (the Pro key is
  deactivated). Until it exists, the two `coingecko-*` timers stay disabled.
- `GECKO_TG_CHANNELS`: set only in section 9, after `tg-login`, in the same
  step that starts the reader unit.

`GECKO_DB_PATH` in `.env.gecko` must be `/root/gecko-alpha/gecko.db` or absent.
The units also set it, but systemd lets `EnvironmentFile=` values override
`Environment=`; a different value in the file would win.

```bash
grep '^GECKO_DB_PATH=' .env.gecko    # /root/gecko-alpha/gecko.db or nothing
```

**2b. Directories.** `install.sh` already created them in section 1;
re-running is harmless.

```bash
install -d -m 700 /root/.gecko
install -d -m 755 /var/lib/gecko
```

**2c. Read-only history copy** of `scout.db` for `migrate-calls` and the
backtest. Checkpointed into one file, DELETE journal, then 0444. Needs about
1.6 GB free.

```bash
HIST=/root/gecko-history/scout.db
install -d -m 755 /root/gecko-history
sqlite3 /root/gecko-alpha/scout.db ".backup '$HIST.tmp'"
sqlite3 "$HIST.tmp" "PRAGMA journal_mode=DELETE;"                 # prints: delete
sqlite3 "$HIST.tmp" "PRAGMA quick_check;"                         # must print: ok
ls "$HIST.tmp"-wal "$HIST.tmp"-shm 2>/dev/null && echo "STOP: sidecars present"
mv "$HIST.tmp" "$HIST" && chmod 0444 "$HIST"
sqlite3 "file:$HIST?mode=ro&immutable=1" "PRAGMA quick_check;"    # ok
```

## 3. `python -m gecko check`

NOTE: `check` is not read-only. It creates `gecko.db` at the absolute
`GECKO_DB_PATH` and stamps its application id if the file does not exist.
Run it only after `.env.gecko` points at the intended path (the section 2a
`GECKO_DB_PATH` check), or it creates a database in the wrong place.

```bash
cd /root/gecko-alpha
GECKO_DB_PATH=/root/gecko-alpha/gecko.db /root/.local/bin/uv run --frozen python -m gecko check
systemctl start gecko-job@check.service; journalctl -u gecko-job@check.service -n 40 --no-pager
```

The second form proves the unit resolves the same `db_path` as the shell. Add
`--send` once to the shell form to post a test message to the health chat.

On a fresh database these `WARN` lines are expected; every other line must be
`OK`. Any `FAIL`, or any other `WARN`, is a stop.

- `WARN db_path: ... created new empty DB`: the first `check` only. The unit
  form, run second, reads `OK`.
- One `WARN jobs_ran_once: <job> has no ok run; ...` per writer job:
  dexscreener, geckoterminal, geckoterminal-ohlcv, benchmark, enrich, rank,
  ledger, freshness, prune, research, digest, scorecard, db-backup. Each clears
  after the job's first ok run (section 4 and the timers).
- `WARN telegram_reader: reader not activated: ...` until section 9.
- `WARN coingecko: not set: GECKO_COINGECKO_DEMO_API_KEY, markets/hourly dark`
  until section 10.

**OnFailure falsifier:** `systemctl start gecko-job@no-such-job.service` exits
2 (unknown job). The chat the codex handler posts to must receive the
codex-systemd failure page for `gecko-job@no-such-job.service`. If no page
arrives, job crashes are silent: stop and fix before going further. Then clear
the failed state so it does not linger in `--failed` listings:

```bash
systemctl reset-failed gecko-job@no-such-job.service
```

## 4. First manual runs, in order

Run each through its unit so it uses exactly the timer's environment, and read
its journal before the next one:

```bash
for j in dexscreener geckoterminal benchmark rank freshness ledger enrich \
         geckoterminal-ohlcv prune scorecard digest; do
  case $j in dexscreener|geckoterminal*) u="gecko-collector@$j.service" ;;
             *) u="gecko-job@$j.service" ;; esac   # the unit its timer starts
  echo "== $u"; systemctl start "$u"; echo "rc=$?"
  journalctl -u "$u" -n 15 --no-pager
done
```

Expected on a fresh database: `rank` and `scorecard` have little or nothing to
work with; `ledger` has nothing due; `freshness` may page for tables that have
not had a cycle yet; `digest` posts to the digest chat.

**Scorecard and digest sends.** A message over 4,000 characters is split into
parts marked (i/n). A retry after a partial failure re-delivers the earlier
parts too, so duplicates are expected and are not a bug; loss is not possible.

**Digest run rows.** A sent digest records `rows_written=5`: two high-water
marks, two ids-at-mark lists and `as_of`. A health footer reading
`freshness check error <Type>` means the freshness detector itself failed.
That is page-worthy: read the digest job's journal.

`research` (daily 06:00) needs `GECKO_ANTHROPIC_API_KEY`; without it the job
logs "research disabled" and writes nothing.

**Research retries and cost.**

- A token with a valid dossier is never researched again.
- A token whose only rows are markers (`schema_version=0`: invalid JSON, an
  abandoned pause, a timeout, an unreadable 200 body) is retried once every `MARKER_RETRY_DAYS` (7)
  while it stays on the watchlist.
- A malformed or unreadable HTTP 200 body is billed: it writes a `body_error`
  marker (cost `EST_NEXT_USD`, $0.10, counted toward
  `research_daily_usd_cap`, retried after `MARKER_RETRY_DAYS`) and still fails
  the run when no valid dossier results. The usual cause is a wrong
  `GECKO_ANTHROPIC_BASE_URL`: fix the URL.
- `GECKO_RESEARCH_DAILY_USD_CAP` below 0.10 is refused at settings load and
  stops EVERY gecko job from starting. To pause research, unset
  `GECKO_ANTHROPIC_API_KEY` or run
  `systemctl disable --now gecko-research.timer`; never set the cap to 0.
  Likewise GECKO_RESEARCH_MAX_PER_RUN must be ≥ 1 (0 is refused at settings
  load for every job).
- Pricing defaults were verified against the Anthropic docs on 2026-10-16:
  claude-sonnet-5-5 at $2 / $10 per million input / output tokens, web search
  at $10 per 1,000. One dossier can overshoot `research_daily_usd_cap` by
  about one dossier's cost.
- The research job's `rows_written` counts marker rows as well as dossiers.

## 5. `migrate-calls`

Copies the legacy Telegram calls history into `source_calls` and prints the
legacy channel list.

```bash
cd /root/gecko-alpha
GECKO_DB_PATH=/root/gecko-alpha/gecko.db /root/.local/bin/uv run --frozen \
  python -m gecko migrate-calls --history-db /root/gecko-history/scout.db
```

Keep the printed `GECKO_TG_CHANNELS=[...]` line for section 9. Do NOT add it to
`.env.gecko` yet.

## 6. Backtest anchor

```bash
GECKO_DB_PATH=/root/gecko-alpha/gecko.db /root/.local/bin/uv run --frozen \
  python -m gecko backtest --rule F4
echo "rc=$?"
```

Expect `PASS` in the result row and rc 0. rc 1 means EITHER the battery failed
OR the job could not load the study (`backtest: cannot load study: ...`) or
crashed: read the output, never the exit code alone.

### 6b. Start the timers

Every timer except the two `coingecko-*` ones (those wait for a Demo key,
section 10):

```bash
cd /root/gecko-alpha
systemctl start gecko-collector@dexscreener.service   # blocks until the run finishes
systemctl start $(ls deploy/gecko/timers | grep -v '^gecko-coingecko-')
systemctl list-timers 'gecko-*' --all --no-pager     # 13 timers with a NEXT time
```

The dexscreener, geckoterminal and geckoterminal-ohlcv timers start
`gecko-collector@<job>.service` instances; every other job timer starts
`gecko-job@<job>.service`. The timer command starts the collector timers and
`gecko-freshness.timer` together, in no particular order; the explicit
dexscreener start before it is what puts a collector ok run first. Freshness
pages a collector that has never succeeded after its SLO (30 min for
dexscreener).

## 7. Verification SQL

Read-only, against the live database:

```bash
Q() { sqlite3 -readonly -header -column /root/gecko-alpha/gecko.db "$1"; }
Q "SELECT job, COUNT(*) runs, SUM(ok) ok, MAX(finished_at) last, SUM(rows_written) rows
   FROM runs GROUP BY job ORDER BY job;"
Q "SELECT status, COUNT(*) FROM tokens GROUP BY status;"
Q "SELECT source, COUNT(*), MAX(observed_at) FROM observations GROUP BY source ORDER BY source;"
Q "SELECT COUNT(*), MIN(observed_at), MAX(observed_at) FROM observations
   WHERE token_id='benchmark:btc';"
Q "SELECT COUNT(*) FROM source_calls;"
```

Pass: every job run in section 4 has at least one `ok=1` row; `tokens` has
`candidate` rows; `observations` has `dexscreener` and `geckoterminal` rows; the
`benchmark:btc` count is at least 1 and grows hourly.

After 24 h, check the timers fired and nothing failed:

```bash
systemctl list-timers 'gecko-*' --all --no-pager
systemctl list-units 'gecko-*' --failed --no-pager
ls -lai /root/gecko-alpha/gecko.db.bak.* /var/lib/gecko/gecko-snapshot.db  # after 03:24; snapshot inode = newest backup's
```

## 8. `require_security_known` flip criterion

`GECKO_REQUIRE_SECURITY_KNOWN` stays `false` until every active token has at
least one known enrich verdict. This query must return 0:

```sql
SELECT COUNT(*) FROM tokens t
WHERE t.status = 'active'
  AND NOT EXISTS (
    SELECT 1 FROM observations o
    WHERE o.token_id = t.token_id AND o.source = 'enrich'
      AND json_extract(o.features_json, '$.goplus_status') = 'ok'
      AND json_type(o.features_json, '$.security_ok') IN ('true', 'false'));
```

Then set `GECKO_REQUIRE_SECURITY_KNOWN=true` in `.env.gecko`. Every job reads
the env file at each start, so no restart is needed; the next `rank` run uses
it. This is a flag change: record its own approval.

## 9. Telegram reader activation (OWNER)

Order matters. The reader exits 2 while `GECKO_TG_CHANNELS` is unset, and a
channel list without a running reader makes `freshness` page `source_calls`
within 30 min. So log in first, with the list still unset, then write the list
and start the reader in one step.

1. `/root/.gecko` exists with mode 0700 (`install.sh` created it).
2. OWNER, interactively (phone code; a second device on the same account).
   `tg-login` does not need `GECKO_TG_CHANNELS`; leave it unset:
   ```bash
   cd /root/gecko-alpha
   GECKO_DB_PATH=/root/gecko-alpha/gecko.db /root/.local/bin/uv run --frozen python -m gecko tg-login
   stat -c '%a' /root/.gecko/gecko_tg.session      # 600
   ```
3. In one step: add the channel list from section 5 to `.env.gecko` with an
   editor, in single quotes (`GECKO_TG_CHANNELS='["@foo","@bar"]'`), then
   start the unit at once and confirm it polls:
   ```bash
   grep -c '^GECKO_TG_CHANNELS=' .env.gecko         # 1
   systemctl enable --now gecko-telegram-reader.service
   journalctl -u gecko-telegram-reader.service -n 30 --no-pager
   ```
   Exit 2 (configuration or session lock) stops the unit instead of looping
   (`RestartPreventExitStatus=2`); read the journal, fix, start again.
   `freshness` needs no restart; it reads the env file at each run.

## 10. CoinGecko (after the owner creates a Demo key)

Add `GECKO_COINGECKO_DEMO_API_KEY=<key>` to `.env.gecko` with an editor (never
on a command line), then:

```bash
deploy/gecko/install.sh --with-coingecko
systemctl start gecko-job@coingecko-markets.service   # up to 25 min
```

From now on every `install.sh` re-run needs `--with-coingecko`; without it the
two timers are disabled again (its output shows `is-enabled: disabled`).

## 11. Hermes `gecko-query` skill

Install only AFTER the owner pairs a Telegram bot to the Hermes gateway. Today
the gateway serves no messaging platform, so the skill would have no one to
answer. The snapshot it reads (`/var/lib/gecko/gecko-snapshot.db`, 0644) is
refreshed nightly by `gecko-db-backup.service` regardless.

```bash
d=$(mktemp -d) && chmod 755 "$d"
cp deploy/hermes/skills/gecko-query/SKILL.md deploy/hermes/skills/gecko-query/query.py "$d"/
chmod 644 "$d"/*
sudo -u gecko-agent install -d -m 700 /home/gecko-agent/.hermes/skills/gecko-query
sudo -u gecko-agent cp "$d/SKILL.md" "$d/query.py" /home/gecko-agent/.hermes/skills/gecko-query/
rm -rf "$d"
sudo -u gecko-agent python3 /home/gecko-agent/.hermes/skills/gecko-query/query.py \
  "SELECT job, MAX(finished_at) FROM runs GROUP BY job"     # rows, rc 0
sudo -u gecko-agent python3 /home/gecko-agent/.hermes/skills/gecko-query/query.py \
  "DELETE FROM tokens"; echo "rc=$?"                         # refused, rc 2
```

(`/root` is 0700, so gecko-agent cannot copy from the repo directly.)

## 12. Rollback

```bash
cd /root/gecko-alpha
deploy/gecko/install.sh --remove --dry-run
deploy/gecko/install.sh --remove
```

Disables and stops every gecko timer and unit, deletes the unit files and
`/usr/local/bin/gecko-db-backup.sh`, and leaves all data in place: `gecko.db`,
`gecko.db.bak.*`, `/var/lib/gecko`, `.env.gecko`, the Telethon session and
`/root/gecko-history`. Never touch `scout.db` or the legacy units: gecko never
wrote to them. The legacy pipeline keeps running throughout.

The drift watchdog will list the gecko units as missing in prod after
`--remove` (expected; alerted once, then tombstone-suppressed).

## 13. Known limits

- Disk budget for gecko backups: the live `gecko.db` plus 3 backups. The
  snapshot is a hardlink of the newest backup (same inode, no extra space; a
  copy only if `/var/lib/gecko` were on another filesystem). Peak during a run
  is one more `.partial` copy. The backup refuses to start (and pages) unless
  free space is at least 2 x `gecko.db` + 1 GiB. It copies with `VACUUM INTO`
  (one read transaction, so writers never make it restart) and, on success,
  writes a `runs` row with job `db-backup` that `freshness` watches.
- Backups are LOCAL only (newest 3 `gecko.db.bak.*`). Off-host shipping of
  `gecko.db` waits for B2 keys (followups ticket); legacy backup units must not
  be retired before it exists.
- The legacy backup rotation globs `scout.db.bak.*` only, so it never touches
  gecko backups, and the gecko rotation never touches legacy ones.

## Approvals log

| Stage | Action | Class | Approval record (who, where, quote) | Timestamp (UTC) | Executed by |
|---|---|---|---|---|---|
| 0 | preflight | read-only | | | |
| 1 | `install.sh --no-start` (timers enabled, not started) | deploy | | | |
| 2 | `.env.gecko`, dirs, history copy | prod-state | | | |
| 6b | start every non-coingecko timer | deploy | | | |
| 3-7 | check, manual runs, migrate-calls, backtest | prod-state | | | |
| 8 | `GECKO_REQUIRE_SECURITY_KNOWN=true` | flag | | | |
| 9 | tg-login, reader unit, `GECKO_TG_CHANNELS` | flag (OWNER) | | | |
| 10 | Demo key, `--with-coingecko` | flag (OWNER) | | | |
| 11 | Hermes skill install | deploy | | | |
| 12 | rollback (if used) | deploy | | | |

An action without a recorded approval is reported as
`executed-without-recorded-approval`.
