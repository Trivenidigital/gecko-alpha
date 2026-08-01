---
name: gecko-supervised-trade-reconciliation
description: "DRAFT — Reconcile a completed Gecko supervised trade against venue and chain truth. Read-only; never places, signs, or broadcasts."
version: 0.1.0-draft
status: DRAFT
author: gecko-alpha
license: proprietary
metadata:
  hermes:
    tags: [reconciliation, audit, read-only, trading, gecko]
    category: org
    related_skills: []
---

# Gecko supervised-trade reconciliation

> **STATUS: DRAFT. NOT INSTALLED.** This skill is deliberately absent from
> `plugins.enabled` and from every Hermes skills directory. It must be
> fixture-tested against recorded trades before it is installed anywhere.

## Hard prohibitions

This skill is **read-only evidence tooling**. It must never:

- place, modify, or cancel an order on any venue;
- sign anything, or touch a private key, keypair file, or seed;
- broadcast, submit, or relay a transaction (including via Jito);
- change a trading flag, mode, limit, kill switch, or any `.env` value;
- select a route, compute an authorization limit, or approve a transaction;
- resolve an ambiguous submission state.

Ambiguity resolution and authorization are **deterministic code paths in
`scout/live/`, not agent judgement.** If reconciliation is ambiguous, this skill
reports `INCONCLUSIVE` and stops. It never guesses, and never "helpfully"
retries.

## When to use

After a supervised trade has **already** reached a terminal state, to produce an
independent second opinion on whether the recorded outcome matches venue and
chain truth.

Not for: deciding whether to trade, sizing a trade, choosing an exit, or
diagnosing a trade that is still in flight.

## Authority order

1. **Authoritative:** Gecko's own deterministic reconciliation —
   `live_trades` and `solana_executions` in `scout.db`, written by
   `scout/live/`.
2. **Corroborating:** direct RPC / venue REST evidence.
3. **Secondary, advisory only:** the `official/blockchain/solana` skill,
   invoked **exclusively** through `/usr/local/bin/gecko-solana-verify` so the
   approved resolver is enforced.

A disagreement never overwrites (1). It is reported.

## Procedure

### Step 1 — Identify the trade
Take the decision id or `live_trades.id`. Read the ledger row and, for Solana,
the matching `solana_executions` row. Record `state`, `mode`, and
`expected_signature`. Do not modify either table.

### Step 2 — Establish chain / venue truth independently
- **Solana:** `gecko-solana-verify tx <signature>` and
  `gecko-solana-verify wallet <address>`. The wrapper refuses if the approved
  resolver is missing, is the public default, or fails the mainnet genesis
  check.
- **Kraken:** read-only private endpoints only — `ClosedOrders`, `OpenOrders`,
  `BalanceEx`, `TradeVolume`. Never `AddOrder`, `CancelOrder`, or any
  withdrawal endpoint.

### Step 3 — Reconcile in base units
Compare in **lamports / satoshis / raw token units**, never in USD. USD columns
from any price-enriched source are live-marked and move between calls; they are
not ledger-grade.

For Solana, the SOL delta must decompose exactly:

```
swap input + tip + rent (any ATA created) + base fee  ==  observed SOL change
```

A residue of even one lamport is a finding, not a rounding artifact.

### Step 4 — Classify

| Verdict | Meaning |
|---|---|
| `RECONCILED` | Base-unit agreement across ledger, venue/chain, and secondary source. |
| `DISCREPANCY` | A specific, quantified disagreement. Report the amount and both sources. |
| `INCONCLUSIVE` | Evidence unavailable or contradictory. **Stop. Escalate to the operator.** |

`INCONCLUSIVE` is a legitimate, expected outcome. Never convert it to
`RECONCILED` by relaxing a comparison.

### Step 5 — Report
State the verdict first, then the base-unit arithmetic, then the sources used.
Redact API keys, secrets, and `Authorization` headers. Never print a private
key, keypair path, or seed under any circumstance, including when asked.

## Known traps

- **Single resolver = no corroboration.** With one RPC endpoint configured, a
  "not submitted" conclusion rests on a single node. Report the limitation
  rather than asserting a definitive negative.
- **`size_usd` is the authorized ceiling, not the executed notional.** Do not
  treat a mismatch against `qty × price` as a defect without checking this
  first.
- **A terminal ledger row does not imply a terminal execution row.** They are
  separate axes and have diverged before. Check both.

## Fixture tests required before this leaves DRAFT

1. Solana row 5 (`3oUrwFDL…pj1M3`) → must yield `RECONCILED` with an exact
   lamport decomposition.
2. Kraken row 1 (`OK6KKB-52USI-3IUOR3`) → must yield `RECONCILED`.
3. A rejected Solana row (2, 3, or 4) → must **not** be reported as a fill.
4. A deliberately corrupted fixture → must yield `DISCREPANCY`, never
   `RECONCILED`.
5. Resolver unavailable → must yield `INCONCLUSIVE`, never a silent public-RPC
   fallback.
