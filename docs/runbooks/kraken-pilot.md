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

---

# Closing a position — `exit`, `exit-status`, `exit-cancel`

**Implementing this lane does not authorize executing anything with it.** The
code being merged, tested and deployed is not permission to sell. Every live
run needs its own recorded operator approval, on the day, for that position.
If you are reading this because a position is open and you did not get that
approval, stop here.

`exit` CLOSES an existing `live_trades` row. It is not `place --side sell` —
that would be blocked by the open row and would book a *second* row, which is
the opposite of closing a position.

```bash
cd /root/gecko-alpha        # always; DB_PATH is relative

# Rehearsal. Kraken parses and checks the order and places nothing.
python -m scout.live.kraken_pilot exit --live-trade-id <id> --price <limit> \
    --validate-only --yes-i-am-rehearsing

# The real thing.
python -m scout.live.kraken_pilot exit --live-trade-id <id> --price <limit>
```

There is no `--volume`. The quantity is whatever `entry_fill_qty` says the row
holds; that is the point of selecting by row id.

## Before you type the token

Read the whole screen. Two blocks matter:

- **IF THIS FILLS AS MAKER** and **IF THIS FILLS AS TAKER.** A limit order does
  not choose which one it pays — it pays maker if it rests and taker if it
  crosses, decided by the book at the instant the order arrives. Both are shown
  because either can happen. On this account taker is roughly double maker, so
  the difference is not decoration.
- **break-even price.** The exit price at which realised P&L is zero, including
  the entry fee already paid. If your limit is below it you are choosing a loss.

The token is a digest over the order AND the account state the screen was
computed from: the row, the held quantity, the base balance, the resting-order
set, and the authorized fee ceilings. All of it is re-read immediately before
the order is sent. If anything moved, the authorization is void and nothing is
sent — you get a fresh screen instead of a silently-rebuilt order.

**The prompt requires a terminal.** `echo <token> | kraken_pilot exit` is
refused even with the right token. This boundary is a human checkpoint or it is
nothing.

### Fees: what voids an approval and what does not

```
current fee <= the ceiling you approved  ->  still authorized
current fee >  the ceiling you approved  ->  authorization invalidated
```

A rate that IMPROVES between the screen and the submission does not void your
approval. A rate that rises above what you were shown does. If the fee lookup
itself fails, the run refuses — there is no fallback rate, because a made-up
fee on an approval screen is worse than no screen.

## What the outcomes mean

| Outcome | Row afterwards | Exit code |
|---|---|---|
| Filled in full, reconciliation agrees | `closed_via_reconciliation`, realised P&L written | 0 |
| Order resting, not yet filled | `open`, `exit_order_id` recorded | 0 |
| Partially filled | `open`, `entry_fill_qty` REDUCED to the remainder | 4 |
| Venue refused it definitively | `open`, unchanged — you still hold the coins | 1 |
| Submission ambiguous, resolver says it never landed | `open`, unchanged | 1 |
| Submission ambiguous, resolver cannot tell | `needs_manual_review` | 3 |
| Filled, but the evidence does not add up | `needs_manual_review`, NOT closed | 4 |

`closed_via_reconciliation` is the status a supervised exit writes. The
`live_trades` CHECK enum has no operator-exit member and this lane does not
widen it; `closed_tp` / `closed_sl` would fabricate a threshold trigger that
never fired. Nothing else in the tree writes that status to `live_trades`, so
seeing it there means exactly one thing: a supervised pilot exit.

## Partial fills

A partial fill never closes the row. The lane reduces `entry_fill_qty` by what
sold and leaves the status `open`, because the remainder is a real position.
Re-running `exit` later sells exactly what is left, because it reads the
reduced quantity.

A held quantity only ever falls. If a recovery command finds an order whose
fills were already folded in, it escalates rather than subtracting the same
coins twice — you will see `exit_partial_already_applied` and the row goes
`needs_manual_review` with nothing changed. Set the quantity by hand from the
venue's own trade history in that case.

## After a crash or a restart

This is the part to read at 3am.

Every `exit` run fsyncs a recovery baseline immediately before it submits:
both balances (base *and* quote), the lot precision, the fee ceilings, the
limit price and the entry economics. That record is what lets a completely
different process finish the job.

```bash
python -m scout.live.kraken_pilot exit-status --live-trade-id <id>
```

`exit-status` sends nothing to the venue but reads. It resolves the order by
the deterministic exit client id (`gecko-x-<row id>`), fetches the fills,
compares the CURRENT balances against the PERSISTED baseline, and settles:

| What it finds | What it does |
|---|---|
| No order, resolver clean after two sweeps | RETAINS the position, clears the block |
| Order still working | Reports it, changes nothing (exit 2) |
| Order fully filled | CLOSES the row with realised P&L |
| Order partially filled | REDUCES the held quantity, row stays open |
| Contradictory or unreadable evidence | `needs_manual_review` |

It uses the same reconciliation predicate `exit` uses. Recovery does not get a
looser standard than the run that placed the order.

### The three interruption points

1. **Died after the intent, before submitting.** No order exists.
   `exit-status` proves that with the adapter's two-sweep resolver and clears
   the block. You still hold the coins; re-run `exit` when ready.
2. **Died after submitting, before the fill.** The order exists.
   `exit-status` finds it — working (leave it), or terminal (it settles it).
3. **Died after the fill, before reconciling.** `exit-status` closes the row
   and writes realised P&L. This is a normal recovery, not an incident.

Until an interrupted run is resolved, a fresh `exit` on that row REFUSES. That
is deliberate: an intent with no completion record means a sell may exist that
nothing in the ledger names, and placing a second one is the failure the whole
lane is built to prevent. Only positive evidence clears it — `exit-status`
will not clear a block just because time passed.

## Pulling a resting exit order

```bash
python -m scout.live.kraken_pilot exit-cancel --live-trade-id <id>
```

Use this, **not** `cancel --decision-id`. That one keys off the *entry* client
order id recorded on the row and structurally cannot name an exit order.

`exit-cancel` reads the order's state before cancelling, so a fill that already
happened is never attributed to the cancel. If the cancel request itself fails
ambiguously it resolves by READING, never by sending again. If the order turns
out to have already filled, it settles it from the persisted baseline rather
than sending a pointless CancelOrder.

Both recovery commands also accept `--decision-id <uuid>`, resolved to the row
id via the evidence filename. The row id is the durable handle — several
attempts share one exit order — so prefer it.

## Ambiguity: submit and cancel

Kraken publishes no duplicate-`cl_ord_id` contract, so **nothing in this lane
ever resends.** An ambiguous submission is resolved by querying OpenOrders and
ClosedOrders twice, separated by a settle delay:

- **accepted** — the order landed; it is adopted, not resent.
- **not_accepted** — two clean sweeps found nothing; no order exists, the
  position is untouched.
- **unresolved** — Kraken could not tell us. The row goes
  `needs_manual_review` and the lane blocks. Check the Kraken web UI (Orders
  and Trades) before anything else. **Do not resend.**

An ambiguous *cancel* gets the same treatment: one attempt, then a read-only
lookup to settle what actually happened.

## When a row goes `needs_manual_review`

Exactly these cases, and no others:

- the submission was ambiguous and the resolver could not decide;
- the venue reported a fill but produced no usable filled quantity;
- reconciliation could not corroborate the sale — the base balance did not
  move by the executed volume (to within one lot tick), the per-fill records
  do not total the reported fill, the fee exceeds the account's own taker rate,
  the executed price is below the limit, or the proceeds never arrived in the
  quote asset;
- no pre-trade baseline survives, so a settlement cannot be corroborated at all;
- a partial fill was already folded into the held quantity by an earlier run.

Note what is **not** on that list: the initiating process exiting. That alone
is never a reason — the persisted baseline is there precisely so a restart can
still reach a real verdict.

When you see it: the row is NOT closed and no realised P&L was written. Read
the `exit_reconciliation` record in the evidence file — `base_mismatches` and
`quote_mismatches` are listed separately, because the base side is exact and
the quote side is where fee variability lives. Check the account, then set the
row by hand.

## Locking

`exit` takes the same exclusive lock as `place` — both submit orders, and two
concurrent exits are two sells of the same coins. `exit-status` and
`exit-cancel` are never locked: they place nothing, and they are exactly the
tools you need while a stale lock is held.

## Evidence

Every exit artifact lives under `pilot_evidence/` with the row id in the name:

```bash
ls pilot_evidence/kraken_pilot_exit_<id>_*        # every attempt for a row
```

`kraken_pilot_exit_<id>_<uuid>.json` is an `exit` run;
`..._cancel_<uuid>.json` and `..._status_<uuid>.json` are the recovery
commands. One JSON object per line, fsynced as it goes, so a crash keeps every
completed step. The row id leads the filename because after a crash you know
the row id — you typed it — and not the decision id, which was minted inside
the run that died.
