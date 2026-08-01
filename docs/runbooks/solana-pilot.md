# Solana supervised pilot — operator runbook

For the single supervised SOL→USDC swap on mainnet
(`python -m scout.live.solana_pilot`). Read this before the run, not during it.

The swap is ~$7. **The success criterion is path validation, not P&L** — see
[Fees and friction](#fees-and-friction).

## Before the day

### Custody

The signing key lives outside the repository, owned by the account that runs the
pilot:

```bash
/root/solana-pilot/                       # dir 0700, root-owned
/root/solana-pilot/pilot-keypair.json     # file 0600, root-owned
```

Wallet: `CqnCgVWJCimvkNX1YE1nerNZgBD69qKu8rhrqWGeDDAE`, funded with 0.2 SOL.

`scout.live.solana.signer` refuses to load the key if the mode is wider than
0600 or the file is owned by another uid, and refuses outright on a non-POSIX
platform because it cannot check either there. Do not `chmod` a key that was
ever group- or world-readable — rotate it. A key whose permissions were wrong is
a key that may have been read.

The key is read at call time, used, and dropped. It is never cached, never
passed through argv or the environment, and never logged.

### Pin the resolver's RPC endpoint

```bash
SOLANA_RESOLVER_RPC_URL=https://<your-dedicated-node>/...
```

**`place` refuses to run without this** when `SOLANA_RPC_URL` is a public
round-robin such as `api.mainnet-beta.solana.com`. The reason is specific: the
resolver reaches `definitively_not_submitted` — the one verdict that clears the
lane and permits a rerun — by combining two facts read in two separate calls,
that the signature is absent AND that the block height has passed
`lastValidBlockHeight`. Behind a load balancer those calls can land on different
nodes, and a node that is simultaneously ahead on height and missing the
signature from its cache invents that verdict. Acting on it means retiring a row
for a transaction that is still in flight, and then swapping again: two trades
against one authorization.

### Re-run the real-build capture

The inspector's allowlists — permitted program ids, permitted SPL Token and
associated-token-account discriminators, permitted System instructions — were
derived from what Jupiter's builder actually emits. That builder is a **server-
side service that can change shape without notice**, and a shape change lands as
a refusal on trade day rather than as a deprecation notice. Re-run the capture
against a live build before any future trade. This is not a one-time
verification.

### Confirm nothing prunes `pilot_evidence/`

Check that no cleanup job touches it before the run — see
[the evidence directory is operationally load-bearing](#-the-evidence-directory-is-operationally-load-bearing--do-not-prune-it).
Losing a file there can leave a row permanently unresolvable.

### Rehearse the migration against a copy of prod

`db.initialize()` runs every pending migration, and the pilot must not be the
thing that discovers one is slow:

```bash
cp /root/gecko-alpha/scout.db /tmp/solana-rehearsal.db
DB_PATH=/tmp/solana-rehearsal.db python -m scout.live.solana_pilot status
```

## On the day

### Run from the deployment root

```bash
cd /root/gecko-alpha        # NOT from anywhere else
python -m scout.live.solana_pilot status
```

`DB_PATH` resolves relative to the working directory. SQLite creates a database
for any path it is given, so the wrong directory does not fail — it would give
you an empty one with no kill switch and no prior signature to reconcile. The
runner refuses to start when the database does not exist; do not "fix" that by
creating one.

### Leave the pipeline running

Do not stop `gecko-pipeline` for the pilot. The kill switch the pilot reads is
the one the pipeline writes; stopping the pipeline removes the mechanism that
would halt trading if something else went wrong.

### One swap at a time

`place` takes an exclusive lock (`<db>.solana_pilot.lock`) before any gate. A
second `place` refuses with exit 2 and prints the holder's PID.

**`status` and `resolve` are never locked.** A stale lock arises exactly when an
earlier run died with a signature possibly in flight, which is the moment you
most need to ask what happened. The lock is never broken automatically:

```bash
python -m scout.live.solana_pilot status
python -m scout.live.solana_pilot resolve --decision-id <uuid>
rm <db>.solana_pilot.lock        # only once the on-chain state is known
```

## The run

```bash
# Read-only. Custody, balances, kill state, outstanding rows.
python -m scout.live.solana_pilot status

# Rehearsal — quotes, builds, inspects, simulates, signs, and prompts.
# Submits nothing and writes no ledger row.
python -m scout.live.solana_pilot place --sol 0.05 \
    --simulate-only --yes-i-am-rehearsing

# The real thing.
python -m scout.live.solana_pilot place --sol 0.05

# Ask the cluster what a persisted signature did.
python -m scout.live.solana_pilot resolve --decision-id <uuid>
```

There is no `cancel`. Solana has no cancellation primitive: a submitted
transaction either lands before its `lastValidBlockHeight` or it can never land.
`resolve` is how you find out which.

### What you type at the prompt

**The first 8 characters of the EXPECTED SIGNATURE** shown on the approval
screen. Not a decision ID, not a uuid.

This is what binds the authorization to one exact transaction. The signature is
a pure function of the transaction's bytes and the key, so any rebuild — a fresh
quote, a new blockhash, a different route — produces a different signature and
therefore a different phrase. An authorization can never be carried across a
rebuild, because the phrase that authorized the old one does not authorize the
new one.

It is case-sensitive; base58 distinguishes `A` from `a`. Anything else — a wrong
prefix, an empty line, a closed stdin — aborts without submitting.

**You have well under a minute.** A Jupiter build is valid for roughly 150
blocks (~60 seconds), the runner spends a few seconds inspecting and simulating,
and the freshness re-check keeps a 15-block margin. Read the screen in the
rehearsal until you know where each field is; on the real run, expect to have
40-50 seconds. Running out is safe (rule 2) but it costs you the attempt.

## Exit codes

For `place`:

| Code | Meaning | What to do |
|---|---|---|
| 0 | The swap landed and reconciled, or nothing was outstanding | Nothing. The USDC is yours to dispose of; the ledger row stays `open` until you do. |
| 1 | A gate refused, you did not authorize, or the transaction definitively did not execute | Read the reason. No position exists. |
| 2 | Lane blocked — an unresolved prior signature, or another run holds the lock | Resolve what it names. Do not place anything. |
| 3 | **Unresolved submission**, or an unexpected failure | State is UNKNOWN. See rule 1 below. |
| 4 | The swap executed but the balance move could not be explained | Check the wallet before the next run. Read the `reconciliation` record. |

`resolve` reuses the same codes for its own verdict, so read them differently:

| Code | Verdict | Meaning |
|---|---|---|
| 0 | `definitively_not_submitted` | Can never land. Row retired, lane clear, a fresh `place` is safe. |
| 2 | `landed` or `failed_on_chain` | Terminal but needs your disposition — a position to close, or a paid fee to record. The row stays. |
| 3 | `unresolved` | Still unknown. Run it again later. Do not rebuild. |

`status` exits 0 whatever it finds; read its output, not its code.

## The three rules for trade day

### 1. On exit 3, never rebuild, never resubmit, never re-authorize

Exit 3 means the cluster could not tell us whether the transaction exists. It
may still be in flight.

A rebuild produces a **different blockhash and a different signature**, and both
transactions can land. That is two swaps where you authorized one, and neither
Solana nor Jupiter has any dedup that would prevent it. The lane blocks itself
deliberately: every subsequent `place` refuses at startup reconciliation until
the row resolves.

The response is `resolve`, repeatedly, as the blockhash expires:

```bash
python -m scout.live.solana_pilot resolve --decision-id <uuid>
```

Once the verdict becomes `definitively_not_submitted` — absent from the cluster
AND past `lastValidBlockHeight`, so it can never land — the row is retired
automatically and the lane clears. If it comes back `landed`, you hold USDC:
dispose of it and resolve the row by hand. Escalate either way.

### 2. A stale blockhash invalidates the authorization by design

If the build goes stale between your approval and submission, the run stops with
`AUTHORIZATION INVALIDATED` and nothing is sent.

**A rerun is a full new loop, not a retry.** New quote, new transaction, new
signature, new typed authorization. There is no mechanism to "resume" the old
one and there should not be: the numbers you approved are no longer the numbers
that would execute.

### 3. A firing inspector check is a stopped trade, not evidence of an attack

Fail-closed means the trade stops safely. The check name tells you exactly what
to investigate — read it as a question, not a verdict.

The specific case to expect: **`system_instructions_recognised` firing on System
discriminator 3 (`createAccountWithSeed`)**. This is the temporary-wSOL account
pattern that a normal Jupiter SOL→USDC route emits. It is not an attack.

It is also **not** something to wave through. The response is a deliberate,
reviewed allowlist widening **with constraints** —

- base account == our signer
- owner == the token program
- space == 165
- lamports <= the rent-exempt minimum for 165 bytes

— followed by re-verification against a real build. Never a blanket permit of
discriminator 3: the same instruction with a different base account creates an
account we fund and someone else owns.

Apply the same framing to any other check. `spl_token_instructions_recognised`
firing on `Approve` is the one that would matter most — an unlimited delegation
moves no balance, so simulation cannot see it and the loss it enables is not
bounded by trade size.

## Fees and friction

On a $5-10 swap these are material, which is why the run is judged on whether
the path works and not on the result:

- **ATA rent** — ~0.00204 SOL per account created. A first-ever swap creates two
  (wSOL and USDC). This is a rent-exempt **deposit**, not a fee, and the wSOL
  account is closed back to the owner at the end of the swap, so most of it
  returns. The lamports must still be available while the transaction runs, so
  both the fee ceiling and the balance gate count them.
- **Priority fee** — market-dependent, bounded by
  `SOLANA_PILOT_MAX_PRIORITY_FEE_LAMPORTS`.
- **Jito tip** — `SOLANA_PILOT_JITO_TIP_LAMPORTS`, default 100,000 lamports.
  Jito's documented minimum is 1,000 and it warns that the minimum "might not be
  sufficient" under load.

MEV protection is being validated here as a **correctness property for later
size**, not because a $7 swap is an attractive sandwich target. `bundleOnly=true`
buys revert protection at the cost of landing probability — a transaction that
does not land resolves via blockhash expiry to `definitively_not_submitted`, and
a fresh, fully-authorized rerun is safe.

## ⚠ The evidence directory is operationally load-bearing — do not prune it

**`pilot_evidence/` is not just an audit trail. Deleting from it can strand the
lane.**

`lastValidBlockHeight` lives *only* in the evidence file, not on the ledger row.
The resolver needs it: `definitively_not_submitted` requires proving the
blockhash expired, and without that height the proof is unavailable, so the
verdict degrades to `unresolved` — permanently. A row in that state blocks every
future `place` and cannot be cleared by re-running `resolve`; it needs manual
intervention.

It is stored there because `live_trades` has no column that fits a block height
and this lane does not carry a schema migration. It fails closed — the lane
refuses rather than trades — but the cost of losing the file is a blocked lane,
not a lost audit record.

Concretely:

- **Do not** add `pilot_evidence/` to any log-rotation, tmp-cleaner or
  retention job.
- **Do not** delete a decision's evidence file while its `live_trades` row is
  still `open` or `needs_manual_review`. Check with
  `python -m scout.live.solana_pilot status` first.
- **Do** carry `pilot_evidence/` with the database if the deployment ever moves
  machines. The two are one unit; a database restored without its evidence
  directory has unresolvable rows.
- Files for rows that have reached `rejected` or have been dispositioned by
  hand are safe to archive.

## If it goes wrong

Every run writes `pilot_evidence/solana_pilot_<decision-id>.json` — one JSON
object per step, fsynced as it goes, so it survives a crash. It records which
database the run read, which key file signed, every inspector check by name with
its verdict, the simulation result, the expected signature (before submission),
the authorization, and the post-trade reconciliation including the
`meets_minimum_output` comparison that is the slippage guarantee.

## After the pilot: drain and destroy

The pilot key is single-purpose. When the run is done:

1. Supervised sweep of all balances (SOL and USDC) to an operator-designated
   address.
2. Verify both balances read zero:
   `python -m scout.live.solana_pilot status`.
3. `shred -uz /root/solana-pilot/pilot-keypair.json`
4. `rmdir /root/solana-pilot`

Record steps 1 and 3 in the evidence pack — the sweep transaction signature and
the destruction — so the key's whole lifetime is accounted for.
