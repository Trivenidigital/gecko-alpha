# Solana DEX execution lane — operator runbook

For the single supervised SOL→USDC swap on mainnet
(`python -m scout.live.solana_lane`). Read this before the run, not during it.

The swap is ~$7. **The success criterion is path validation, not P&L** — see
[Fees and friction](#fees-and-friction).

## Before the day

### Custody

The signing key lives outside the repository, owned by the account that runs the
lane:

```bash
/root/solana-lane/                       # dir 0700, root-owned
/root/solana-lane/lane-keypair.json     # file 0600, root-owned
```

Wallet: `CqnCgVWJCimvkNX1YE1nerNZgBD69qKu8rhrqWGeDDAE`, funded with 0.2 SOL.

`scout.live.solana.signer` refuses to load the key if the mode is wider than
0600 or the file is owned by another uid, and refuses outright on a non-POSIX
platform because it cannot check either there. Do not `chmod` a key that was
ever group- or world-readable — rotate it. A key whose permissions were wrong is
a key that may have been read.

The key is read at call time, used, and dropped. It is never cached, never
passed through argv or the environment, and never logged.

### Set the operating mode

```bash
SOLANA_MODE=SIMULATION_ONLY     # or SUPERVISED_LIVE for a real trade
```

`SOLANA_MODE` is the lane's master control and replaces the old
`SOLANA_PILOT_ENABLED` boolean. **Remove `SOLANA_PILOT_ENABLED` from `.env`** —
config forbids unknown keys, so a stale one fails loudly at startup rather than
being ignored.

| Mode | What it does |
|---|---|
| `DISABLED` | Refuses everything. The default. |
| `SIMULATION_ONLY` | Quotes, builds, inspects, simulates, prompts. Never reads the funded key, never submits. |
| `SUPERVISED_LIVE` | A human types the authorization before the funded key signs. |
| `BOUNDED_AUTONOMOUS` | A policy check replaces the typed prompt. Also requires `SOLANA_BOUNDED_AUTONOMOUS_ENABLED=true`. |
| `EMERGENCY_STOPPED` | Refuses all execution, like the kill switch, and is checked in the same places. |

Moving between modes is configuration. The same runner, signer, limits, state
machine, submission path and reconciliation serve all of them — only the
authorization policy differs between supervised and autonomous, so promoting
the lane cannot quietly change what a trade is allowed to be.

`--simulate-only` on the command line can only ever **narrow** the configured
mode to `SIMULATION_ONLY`. There is deliberately no flag that can raise it: a
flag that could escalate a `DISABLED` lane into a live one would make the
config setting advisory.

### Declare the signer's public key

```bash
SOLANA_PILOT_SIGNER_PUBKEY=CqnCgVWJCimvkNX1YE1nerNZgBD69qKu8rhrqWGeDDAE
```

Required. Everything before the approval prompt is built against this wallet
without opening the key file. It is also a second lock: at signing time the
loaded keypair must equal it, so a key file that points at a different wallet
cannot sign a transaction built for this one.

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

### Optional: add a second resolver endpoint

```bash
# Ordered, comma-separated. Supersedes SOLANA_RESOLVER_RPC_URL when set.
SOLANA_RESOLVER_RPC_URLS=https://<primary-node>/...,https://<secondary-node>/...
```

Configuration only — no code change, and the single-endpoint deployment above
is fully supported. What a second endpoint buys:

* **Read failover.** A resolution that cannot reach its node returns
  `unresolved`, and `unresolved` BLOCKS the lane. On one endpoint, an RPC
  outage is an outage of the recovery path itself.
* **Corroboration.** Before `definitively_not_submitted` is acted on, a second
  endpoint is asked to see the same absence independently. Disagreement
  collapses the verdict: if the second endpoint HAS the signature the verdict
  becomes `landed`, and anything else — a node still below
  `lastValidBlockHeight`, or a probe that failed — becomes `unresolved`. A
  probe that could not be reached is never read as assent.

Every endpoint must be a single dedicated node: the round-robin refusal is an
**all-endpoint** property, because selection is keyed by signature and each
endpoint eventually serves some resolution.

Two related settings:

```bash
SOLANA_RESOLVER_HEALTH_TIMEOUT_SEC=5.0    # budget for the admission probes
SOLANA_RESOLVER_MAX_LATENCY_MS=2000.0     # above this an endpoint is demoted
```

### What the pool checks before it trusts an endpoint

`place` and `resolve` both probe every configured endpoint before reading a
verdict off one, and record the result as the `resolver_pool` evidence step:

| Check | Failing means | Effect |
|---|---|---|
| `getGenesisHash` == mainnet-beta | the node serves another chain or a fork | endpoint EXCLUDED |
| `getHealth` | the node is behind its cluster | endpoint EXCLUDED |
| latency vs `SOLANA_RESOLVER_MAX_LATENCY_MS` | the node is slow | endpoint DEMOTED — still used for failover and corroboration, but new resolutions prefer a faster one |

The genesis check is the one that matters most and is not a formality: a devnet
or forked node answers "absent" to **every** mainnet signature, and absence is
half of the verdict that clears the lane. A URL cannot prove which chain is
behind it; `getGenesisHash` can.

If **no** endpoint passes, `place` refuses in `SUPERVISED_LIVE` and
`BOUNDED_AUTONOMOUS` — a swap that cannot be resolved afterwards has no
recovery path. A `--simulate-only` rehearsal prints a NOTICE and proceeds,
because it provably never submits and therefore can never need the resolver.
One bad endpoint out of several is excluded rather than fatal.

### Endpoint URLs are secret

Alchemy and Helius carry the API key in the **path**; some providers use the
query string. Nothing in the lane prints a resolver or RPC URL: logs, evidence
files and refusal messages all carry an opaque label of the form
`host#<8 hex>` (the fingerprint distinguishes two keys on one host). If you
ever see a full endpoint URL in an evidence file, treat the key as exposed and
rotate it.

### Re-run the real-build capture

The inspector's allowlists — permitted program ids, permitted SPL Token and
associated-token-account discriminators, permitted System instructions — were
derived from what Jupiter's builder actually emits. That builder is a **server-
side service that can change shape without notice**, and a shape change lands as
a refusal on trade day rather than as a deprecation notice. Re-run the capture
against a live build before any future trade. This is not a one-time
verification.

### Confirm nothing prunes `lane_evidence/`

Check that no cleanup job touches it before the run — see
[the evidence directory is the audit record](#the-evidence-directory-is-the-audit-record--keep-it).
Losing a file costs the audit record and narrows recovery of that row to a
24-hour window.

### Rehearse the migration against a copy of prod

`db.initialize()` runs every pending migration, and the lane must not be the
thing that discovers one is slow:

```bash
cp /root/gecko-alpha/scout.db /tmp/solana-rehearsal.db
DB_PATH=/tmp/solana-rehearsal.db python -m scout.live.solana_lane status
```

## On the day

### Run from the deployment root

```bash
cd /root/gecko-alpha        # NOT from anywhere else
python -m scout.live.solana_lane status
```

`DB_PATH` resolves relative to the working directory. SQLite creates a database
for any path it is given, so the wrong directory does not fail — it would give
you an empty one with no kill switch and no prior signature to reconcile. The
runner refuses to start when the database does not exist; do not "fix" that by
creating one.

### Leave the pipeline running

Do not stop `gecko-pipeline` for the lane. The kill switch the lane reads is
the one the pipeline writes; stopping the pipeline removes the mechanism that
would halt trading if something else went wrong.

### One swap at a time

`place` takes an exclusive lock (`<db>.solana_lane.lock`) before any gate. A
second `place` refuses with exit 2 and prints the holder's PID.

**`status` and `resolve` are never locked.** A stale lock arises exactly when an
earlier run died with a signature possibly in flight, which is the moment you
most need to ask what happened. The lock is never broken automatically:

```bash
python -m scout.live.solana_lane status
python -m scout.live.solana_lane resolve --decision-id <uuid>
rm <db>.solana_lane.lock        # only once the on-chain state is known
```

## The run

```bash
# Read-only. Custody, balances, kill state, outstanding rows.
# Constructs no signer: custody comes from stat, and the key file is
# checked only via its public half.
python -m scout.live.solana_lane status

# Rehearsal (or set SOLANA_MODE=SIMULATION_ONLY).
# Quotes, builds, inspects, simulates and prompts.
# Never reads the funded key, signs nothing, submits nothing,
# writes no ledger row.
python -m scout.live.solana_lane place --sol 0.05 \
    --simulate-only --yes-i-am-rehearsing

# The real thing.
python -m scout.live.solana_lane place --sol 0.05

# Ask the cluster what a persisted signature did.
python -m scout.live.solana_lane resolve --decision-id <uuid>
```

There is no `cancel`. Solana has no cancellation primitive: a submitted
transaction either lands before its `lastValidBlockHeight` or it can never land.
`resolve` is how you find out which.

### The funded key does not sign until after you authorize

**Nothing is signed when the approval screen appears.** The key file has not
been opened. Custody is checked from `stat` — mode and owner — which needs no
read, and the wallet's public key comes from `SOLANA_PILOT_SIGNER_PUBKEY`, so
Jupiter can build for it and the inspector can check the fee payer against it
without the private key being involved at all.

This is deliberate and it is the reason the screen shows a hash rather than a
signature. A validly signed transaction is an irreversible capability: whoever
holds those bytes can broadcast them, and the wallet cannot take that back.
Holding them only in memory limits who is likely to see them; it does not undo
the fact that a spendable artifact now exists. So the artifact is not created
until you have said yes.

The key is read once, immediately after your authorization, used to sign, and
dropped. Every refusal path — a wrong phrase, a kill switch, a stale blockhash,
a failed gate, and every `--simulate-only` run — leaves the key file untouched.

`status` never constructs a signer either. It reports custody from `stat`, and
compares the key file's PUBLIC half against `SOLANA_PILOT_SIGNER_PUBKEY` so a
wrong key file is caught before trade day rather than at the signing step. Its
output says which of the two it did. Run it freely; it cannot sign.

### What you type at the prompt

**The first 8 characters of the MESSAGE SHA256** shown on the approval screen.
Not a signature, not a decision ID, not a uuid.

That hash is of the exact bytes the key will sign. Everything you are agreeing
to lives inside those bytes — amount, route, slippage bound, fees, tip,
blockhash, the full instruction list — so changing any one of them changes the
hash, and the phrase you were given stops matching. An authorization cannot be
carried across a rebuild, because the rebuild has a different hash.

After signing, the runner re-checks that the signed message hash equals the one
you authorized, and refuses to submit if it does not.

It is lowercase hex and is matched exactly. Anything else — a wrong prefix, an
empty line, a closed stdin — aborts without signing and without submitting.

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
python -m scout.live.solana_lane resolve --decision-id <uuid>
```

Once the verdict becomes `definitively_not_submitted` — absent from the cluster
AND past `lastValidBlockHeight`, so it can never land — the row is retired
automatically and the lane clears. If it comes back `landed`, you hold USDC:
dispose of it and resolve the row by hand. Escalate either way.

### 2. A stale blockhash invalidates the authorization by design

If the build goes stale between your approval and submission, the run stops with
`AUTHORIZATION INVALIDATED`. The staleness check runs before the key is read, so
nothing was signed and nothing was sent.

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

## The evidence directory is the audit record — keep it

`lane_evidence/` holds the only complete account of what each run did. Keep it
for that reason alone.

It also carries `lastValidBlockHeight`, which the resolver uses to prove a
blockhash expired. That figure is not on the ledger row — `live_trades` has no
column that fits a block height, and this lane does not carry a schema
migration.

**Losing a file is recoverable, but only inside a window.** A row whose evidence
is gone falls back to age-derived expiry: a blockhash is valid for at most 150
slots (~60-90 seconds), so a row older than `SOLANA_RESOLVER_AGE_EXPIRY_MIN_SEC`
(default 1 hour) has provably expired whatever height it carried, and `resolve`
clears it from `created_at` alone.

That fallback stops at `SOLANA_RESOLVER_AGE_EXPIRY_MAX_SEC` (default 24 hours),
and the reason matters: `getSignatureStatuses` only searches as far back as the
node retains ledger history. Past that, a swap that **actually landed** reads as
absent, and clearing the row on that would be the one mistake this whole
subsystem exists to prevent. So beyond the upper bound the verdict is forced to
`unresolved` with reason `history_window_exceeded`, and the row waits for a
human — check the wallet's USDC balance and the signature on an explorer before
disposing of it by hand.

So:

- **Do not** add `lane_evidence/` to any log-rotation, tmp-cleaner or retention
  job. Losing a file costs you the audit record and narrows recovery to a
  24-hour window.
- **Do** carry `lane_evidence/` with the database if the deployment moves
  machines.
- If a file is lost, resolve the row **within a day**:
  `python -m scout.live.solana_lane resolve --decision-id <uuid>`.
- Files for rows that have reached `rejected` or were dispositioned by hand are
  safe to archive.

Every resolution records `expiry_source` — `evidence_last_valid_block_height` or
`row_age` — so a reviewer can always see which fact established expiry.

## If it goes wrong

Every run writes `lane_evidence/solana_lane_<decision-id>.json` — one JSON
object per step, fsynced as it goes, so it survives a crash. It records which
database the run read, which key file signed, every inspector check by name with
its verdict, the simulation result, the expected signature (before submission),
the authorization, and the post-trade reconciliation including the
`meets_minimum_output` comparison that is the slippage guarantee.

## After the lane: drain and destroy

The lane key is single-purpose. When the run is done:

1. Supervised sweep of all balances (SOL and USDC) to an operator-designated
   address.
2. Verify both balances read zero:
   `python -m scout.live.solana_lane status`.
3. `shred -uz /root/solana-lane/lane-keypair.json`
4. `rmdir /root/solana-lane`

Record steps 1 and 3 in the evidence pack — the sweep transaction signature and
the destruction — so the key's whole lifetime is accounted for.
