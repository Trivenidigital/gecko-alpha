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
git fetch -q origin && git status --short --branch     # clean tracked tree
git rev-parse HEAD                                     # = the approved SHA
df -h /                                                # >= 8 GiB free (gecko.db + history copy + backups)
command -v sqlite3 && sqlite3 --version                # sqlite3 CLI present
/root/.local/bin/uv sync --frozen                      # gecko deps from uv.lock
find gecko -name __pycache__ -type d -prune -exec rm -rf {} +
/root/.local/bin/uv run --frozen python -m gecko --list   # every job listed
grep -c '^GECKO_' .env                                 # must print 0: GECKO_* keys crash the legacy settings
ls /etc/systemd/system/codex-systemd-failure-alert@.service   # the OnFailure= target exists
```

## 1. Stage 1: env file, directories, history copy

This is the prep script (`plan3_vps_prep.sh`); it is idempotent and never
echoes secret values. Its equivalent steps:

**1a. `.env.gecko` (mode 0600)** by copying values from the legacy `.env`.
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
- `GECKO_TG_CHANNELS`: set only in section 9, after `tg-login` and the reader
  unit start.

`GECKO_DB_PATH` in `.env.gecko` must be `/root/gecko-alpha/gecko.db` or absent.
The units also set it, but systemd lets `EnvironmentFile=` values override
`Environment=`; a different value in the file would win.

```bash
grep '^GECKO_DB_PATH=' .env.gecko    # /root/gecko-alpha/gecko.db or nothing
```

**1b. Directories.** `install.sh` also creates them; doing it here is harmless.

```bash
install -d -m 700 /root/.gecko
install -d -m 755 /var/lib/gecko
```

**1c. Read-only history copy** of `scout.db` for `migrate-calls` and the
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

## 2. Stage 2: install the units

```bash
cd /root/gecko-alpha
deploy/gecko/install.sh --dry-run    # review every action first
deploy/gecko/install.sh              # without --with-coingecko until a Demo key exists
```

Installs `gecko-job@.service` (+ the coingecko-markets timeout drop-in),
`gecko-telegram-reader.service` (installed, NOT enabled),
`gecko-db-backup.service`, every `gecko-*.timer`, and
`/usr/local/bin/gecko-db-backup.sh`. Enables and starts every timer except the
two `coingecko-*` timers, and prints `systemctl list-timers 'gecko-*'`.

Scripts run from `/usr/local/bin`, so a later `git pull` changes nothing there:
re-run `install.sh` after any change under `deploy/gecko/`.

A timer cannot start an instance that is still running, so no job overlaps
itself (the enrich budget no-overlap requirement). A SIGKILLed run still
records no credits; that gap stays open.

The timers start firing as soon as they are enabled. To run section 4 by hand
first, install, then immediately `systemctl stop 'gecko-*.timer'`, do sections
3 to 6, then `systemctl start` the timers you stopped (never the coingecko pair
without a key).

## 3. `python -m gecko check`

NOTE: `check` is not read-only. It creates `gecko.db` at the absolute
`GECKO_DB_PATH` and stamps its application id.

```bash
cd /root/gecko-alpha
GECKO_DB_PATH=/root/gecko-alpha/gecko.db /root/.local/bin/uv run --frozen python -m gecko check
systemctl start gecko-job@check.service; journalctl -u gecko-job@check.service -n 40 --no-pager
```

The second form proves the unit resolves the same `db_path` as the shell. Every
line must be `OK` (or an expected `WARN` for the missing CoinGecko key). Add
`--send` once to the shell form to post a test message to the health chat.

**OnFailure falsifier:** `systemctl start gecko-job@no-such-job.service` exits
2 (unknown job). The health chat must receive the codex-systemd failure page
for `gecko-job@no-such-job.service`. If no page arrives, job crashes are
silent: stop and fix before going further.

## 4. First manual runs, in order

Run each through its unit so it uses exactly the timer's environment, and read
its journal before the next one:

```bash
for j in dexscreener geckoterminal benchmark rank freshness ledger enrich \
         geckoterminal-ohlcv prune scorecard digest; do
  echo "== $j"; systemctl start "gecko-job@$j.service"; echo "rc=$?"
  journalctl -u "gecko-job@$j.service" -n 15 --no-pager
done
```

Expected on a fresh database: `rank` and `scorecard` have little or nothing to
work with; `ledger` has nothing due; `freshness` may page for tables that have
not had a cycle yet; `digest` posts to the digest chat. A partial multi-part
Telegram send is re-sent whole on the next run, so duplicates are possible and
loss is not.

`research` (daily 06:00) needs `GECKO_ANTHROPIC_API_KEY`; without it the job
logs "research disabled" and writes nothing.

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
ls -la /root/gecko-alpha/gecko.db.bak.* /var/lib/gecko/gecko-snapshot.db   # after 03:20
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

Order matters. Setting `GECKO_TG_CHANNELS` before the reader has run makes
`freshness` page `source_calls` within 30 min.

1. `/root/.gecko` exists with mode 0700 (`install.sh` created it).
2. OWNER, interactively (phone code; a second device on the same account):
   ```bash
   cd /root/gecko-alpha
   GECKO_DB_PATH=/root/gecko-alpha/gecko.db /root/.local/bin/uv run --frozen python -m gecko tg-login
   stat -c '%a' /root/.gecko/gecko_tg.session      # 600
   ```
3. Start the unit and confirm it polls:
   ```bash
   systemctl enable --now gecko-telegram-reader.service
   journalctl -u gecko-telegram-reader.service -n 30 --no-pager
   ```
   Exit 2 (configuration or session lock) stops the unit instead of looping
   (`RestartPreventExitStatus=2`); read the journal, fix, start again.
4. THEN add the channel list from section 5 to `.env.gecko`, in single quotes:
   `GECKO_TG_CHANNELS='["@foo","@bar"]'`. Restart the reader so it reads the
   list: `systemctl restart gecko-telegram-reader.service`. `freshness` needs
   no restart; it reads the env file at each run.

## 10. CoinGecko (after the owner creates a Demo key)

Add `GECKO_COINGECKO_DEMO_API_KEY=<key>` to `.env.gecko` with an editor (never
on a command line), then:

```bash
deploy/gecko/install.sh --with-coingecko
systemctl start gecko-job@coingecko-markets.service   # up to 25 min
```

## 11. Hermes `gecko-query` skill

Install only AFTER the owner pairs a Telegram bot to the Hermes gateway. Today
the gateway serves no messaging platform, so the skill would have no one to
answer. The snapshot it reads (`/var/lib/gecko/gecko-snapshot.db`, 0644) is
refreshed nightly by `gecko-db-backup.service` regardless.

```bash
install -d -m 755 /tmp/gecko-query && cp deploy/hermes/skills/gecko-query/* /tmp/gecko-query/
sudo -u gecko-agent install -d -m 700 /home/gecko-agent/.hermes/skills/gecko-query
sudo -u gecko-agent cp /tmp/gecko-query/SKILL.md /tmp/gecko-query/query.py \
  /home/gecko-agent/.hermes/skills/gecko-query/
rm -rf /tmp/gecko-query
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

## 13. Known limits

- Backups are LOCAL only (newest 3 `gecko.db.bak.*`). Off-host shipping of
  `gecko.db` waits for B2 keys (followups ticket); legacy backup units must not
  be retired before it exists.
- The legacy backup rotation globs `scout.db.bak.*` only, so it never touches
  gecko backups, and the gecko rotation never touches legacy ones.

## Approvals log

| Stage | Action | Class | Approval record (who, where, quote) | Timestamp (UTC) | Executed by |
|---|---|---|---|---|---|
| 0 | preflight | read-only | | | |
| 1 | `.env.gecko`, dirs, history copy | prod-state | | | |
| 2 | `install.sh` (timers enabled) | deploy | | | |
| 3-7 | check, manual runs, migrate-calls, backtest | prod-state | | | |
| 8 | `GECKO_REQUIRE_SECURITY_KNOWN=true` | flag | | | |
| 9 | tg-login, reader unit, `GECKO_TG_CHANNELS` | flag (OWNER) | | | |
| 10 | Demo key, `--with-coingecko` | flag (OWNER) | | | |
| 11 | Hermes skill install | deploy | | | |
| 12 | rollback (if used) | deploy | | | |

An action without a recorded approval is reported as
`executed-without-recorded-approval`.
