# Kraken supervised pilot — operator runbook

For the single supervised Kraken spot trade (`python -m scout.live.kraken_pilot`).
Read this before the run, not during it.

## Before the day

### Pilot API key permissions

Issue a key with **exactly** these permissions:

- Funds — **Query funds** (balances)
- Orders & trades — **Create & modify orders**, **Query open orders & trades**,
  **Query closed orders & trades**

Nothing else. In particular:

- **No withdrawal permission.** `preflight_credentials_check` probes two
  withdrawal-scoped endpoints and requires BOTH to answer
  `EGeneral:Permission denied`. A key that can withdraw fails preflight and the
  run stops.
- **No "Query ledger entries".** `WithdrawStatus` accepts EITHER withdraw OR
  ledger-query, so a key with ledger-query SUCCEEDS on that probe and preflight
  rejects it — a false reject that looks exactly like a withdraw-capable key.
  If preflight fails with `probe=/0/private/WithdrawStatus outcome=permitted`
  while `WithdrawMethods` denied, this is the cause: reissue without
  ledger-query rather than assuming the key can withdraw.

### Rehearse the migration against a copy of prod

`db.initialize()` runs every pending migration. The pilot must not be the thing
that discovers a migration takes minutes or fails on real data:

```bash
cp /root/gecko-alpha/scout.db /tmp/pilot-rehearsal.db
DB_PATH=/tmp/pilot-rehearsal.db python -m scout.live.kraken_pilot status
```

Time it and read the output. Do this at least a day before the run.

### Config latency

`.env` changes are latent until the process that reads them restarts. The pilot
is a fresh process, so it picks up `.env` immediately — but the **pipeline**
does not. If you flip `LIVE_USE_REAL_SIGNED_REQUESTS` for the pilot, the running
pipeline keeps its old value until it is restarted. Set it, run the pilot, unset
it; the pipeline never needs to see either state.

## On the day

### Run from the deployment root

```bash
cd /root/gecko-alpha        # NOT from anywhere else
python -m scout.live.kraken_pilot status
```

`DB_PATH` resolves relative to the working directory. SQLite creates a database
for any path it is given, so the wrong directory does not fail — it would give
you an empty one with no kill switch, no prior orders to reconcile and zero
daily gross. The runner refuses to start when the database does not exist; do
not "fix" that by creating one.

### Leave the pipeline running

Do not stop `gecko-pipeline` for the pilot. The pilot takes its own connection
with the same busy timeout, and the kill switch it reads is the same one the
pipeline writes — stopping the pipeline removes the thing that would halt
trading if something else went wrong.

### Not within 5 minutes of 00:00 UTC

The daily-gross cap is keyed on the UTC date. A run that starts at 23:58 and
completes at 00:01 has its cap checked against one day and its ledger row
recorded on the next, so the cap silently under-counts. Start well clear of the
boundary.

### One placement at a time

`place` takes an exclusive lock (`<db>.pilot.lock`) before any gate. A second
`place` refuses with `EXIT_BLOCKED` and prints the holder's PID.

**`status` and `cancel` are never locked.** They do not take the lock and are
not blocked by one, so both remain available while a lock is held — which is
the point: a stale lock arises exactly when an earlier run died with an order
possibly resting, and that is the moment you most need to look at the account
and pull the order. Only new placements are blocked.

The lock is never broken automatically. A stale lock means an earlier run did
not reach its cleanup, so before deleting it:

```bash
python -m scout.live.kraken_pilot status                       # what is resting?
python -m scout.live.kraken_pilot cancel --decision-id <uuid>  # pull it if so
rm <db>.pilot.lock                                             # only then
```

## The run

```bash
# Rehearsal — Kraken validates the order and places nothing.
python -m scout.live.kraken_pilot place --side buy --price <limit> \
    --volume <qty> --validate-only --yes-i-am-rehearsing

# The real thing.
python -m scout.live.kraken_pilot place --side buy --price <limit> --volume <qty>
```

Read the whole approval block before typing anything. `exposure counted` is the
figure that matters — on a marketable order it is larger than `limit notional`,
because a limit price bounds the price, not the size.

Authorize by typing the first 8 characters of the decision ID. Anything else —
a wrong prefix, an empty line, a closed stdin — aborts without sending.

```bash
python -m scout.live.kraken_pilot status                       # watch it
python -m scout.live.kraken_pilot cancel --decision-id <uuid>  # pull it
```

## Exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | Done, or the limit order is resting | Nothing. A resting limit order is normal. |
| 1 | A gate refused, or you did not authorize | Read the reason. No order exists. |
| 2 | Lane blocked (prior row, unknown resting order, lock held) | Resolve what it names, then retry. |
| 3 | Unresolved submission, or an unexpected failure | **State is unknown.** Check the Kraken UI before anything else. NEVER resend. |
| 4 | Traded, but the balance move could not be explained | Check the account before the next run. |

## If it goes wrong

Every run writes `pilot_evidence/kraken_pilot_<decision-id>.json` — one JSON
object per step, flushed as it goes, so it survives a crash. It records which
database the run read, what each gate saw, the authorization, and the venue's
own open-order listing before and after.

**On exit 3, do not resend.** Kraken publishes no duplicate-`cl_ord_id`
contract, so a resend is a live double-order risk. Check the Kraken web UI
(Orders and Trades), then cancel or record the outcome by hand.
