# Solana On-Chain Execution — Follow-up Work (from PR #375 review)

> Status: scoping doc. These are the items the PR #375 review flagged that were NOT
> fixed inline (the polish/correctness items were — commit 54b033d). PR #375 is
> deliberately **BUY-only / shadow-first**; the items below gate the move to live $.
> Each needs its own brainstorm → plan → TDD cycle before implementation.

**New primitives introduced:** NONE (this is a scoping/backlog doc)

## Ordering (do not reach "$10 live" until #1 and #2 are done)

1. **#2 real-RPC sellability** — investigation-first; blocks even a meaningful SHADOW run.
2. **#1 on-chain exit/sell path** — blocks any live $ (and #7 lands with it).
3. **#3 in-loop reconciliation** + **#4 durable exposure cap** — harden before live.
4. **#7 decimals normalization** — lands inside #1.

---

## #2 — Real-RPC sellability (BLOCKING; investigation before code)

**Problem.** `SolanaSwapAdapter.is_sellable` builds a sell of `expected_out_amount`
of the target token and calls `simulateTransaction`. At gate time the wallet holds
ZERO of that token, so against a real RPC the sell instruction fails on insufficient
balance → `err` non-None → `is_sellable` → False → **every buy is blocked**. This is
invisible to the suite because all tests mock `simulate_transaction` to a bare bool.
It bites in **shadow mode too** (shadow runs the same gate), so the shadow milestone
is not trustworthy until this is resolved.

**Step 0 — verify (no code yet).** From the VPS/a funded-pubkey context, run a real
`get_quote` + `build_swap_tx` + `simulateTransaction` for a known-good liquid Solana
token against the configured RPC and confirm whether `is_sellable` returns True or
False. Capture the exact RPC error. This decides the fix.

**Proposed fix (recommended v1).** Split the honeypot check by phase:
- **Pre-buy gate:** treat a successful **sell QUOTE** (Jupiter can route token→USDC at
  acceptable impact) as the honeypot proxy — a route existing is the cheap, reliable
  "can we get out" signal that does NOT require holding the token.
- **At actual sell time** (item #1, when the wallet DOES own the token): run the full
  `simulateTransaction` sell-sim, which is now valid against real balance.

**Alternatives to weigh in the brainstorm:** `simulateTransaction` with an `accounts`
state override to inject a synthetic token balance; `replaceRecentBlockhash`. These are
heavier and RPC-provider-dependent — prefer the phase-split unless the override proves
clean.

**Tests.** A test that exercises the no-balance path against a realistic mock (sell sim
returns insufficient-funds err) and asserts the v1 gate still passes on a routable token.

---

## #1 — On-chain exit / sell path (BLOCKING before live $)

**Problem.** `_dispatch_onchain` is only reached from `on_paper_trade_opened`. There is
NO `on_paper_trade_closed` → on-chain sell. The bot can BUY memecoins on-chain but has
no programmatic SELL: a filled position sits in the hot wallet at full downside until a
human manually swaps out. The adapter already supports `side="sell"` (`_mints_for_side`),
but nothing invokes it. (Our own lesson stands: exits are the edge.)

**Approach.**
- Add `LiveEngine.on_paper_trade_closed(paper_trade)` (or hook the paper evaluator's exit
  event) that, for a `live_trades` row with `venue='solana'` and an open/filled entry,
  builds a SELL via the two-phase adapter (`prepare_order(side="sell")` →
  `record` exit signature **before** broadcast → `broadcast_prepared` →
  `await_fill_confirmation`), then closes the row with realized PnL and a `closed_*`
  status (mirror the CEX close states).
- Reconcile sell signatures the same way as buys (item #3).
- **Decisions for the brainstorm:** v1 = full-position exit only, or mirror the paper
  laddered legs on-chain? How does the paper close-reason map to the on-chain sell? What
  happens if the sell tx fails/timeouts (position still held — needs manual-review state +
  retry)?

**Tie-in:** realized PnL here REQUIRES #7 (decimals normalization) to be correct.

**Until shipped:** PR #375 must state in bold that it is **BUY-only; exits are manual**,
and the runbook must NOT advance to "$10 live" — only to shadow — until this lands.

---

## #3 — In-loop reconciliation + session timeout (HIGH)

**Problem.** `reconcile_open_solana_trades` runs ONLY at boot (`main.py`). A `timeout`
confirmation leaves the row `'open'` and its real on-chain position unverified until a
restart. Compounding: the Solana adapter borrows Binance's `ClientSession`
(`ClientTimeout(total=10.0)`), so a `sendTransaction` slower than 10s under congestion
raises → broadcast-failed → also waits for reboot.

**Approach.** Add a periodic reconciliation loop (same pattern as the other `scout/live`
loops) that calls `reconcile_open_solana_trades` every N minutes; and/or a confirm-retry
on the await path. Give Solana its own `ClientSession` with a longer timeout tuned for
Solana RPC, instead of borrowing Binance's 10s session.

---

## #4 — Durable exposure-cap fix (HIGH)

**Problem.** The float-cap gate sums open Solana notional BEFORE `_dispatch_onchain`
inserts the new row (TOCTOU). Sequential dispatch closes the window today, but
`on_paper_trade_opened` is documented fire-and-forget; concurrent dispatch could let two
intents both pass and jointly exceed `SOLANA_FLOAT_CAP_USD`.

**Approach.** Make check-and-insert atomic: either hold `_db._txn_lock` across the
exposure SUM and the pending-row INSERT, or write a reserved-intent row first and have the
gate count reservations. (The inline code now documents this as a known limitation.)

---

## #7 — Decimals normalization (MEDIUM; lands with #1)

**Problem.** `mid` (→ `shadow_trades.mid_at_entry`) and `fill_price`
(→ `live_trades.entry_fill_price`) are WHOLE-USDC per output-token BASE unit, NOT
normalized by the token's own decimals. Documented as "no consumer yet" — but the values
ARE persisted, so the first exit/PnL reader that consumes them raw gets silent mispricing.

**Approach.** Normalize by the output token's decimals (from the mint / Jupiter token
metadata) when computing entry price, so realized PnL in #1 is correct. Add a test
asserting a known token's normalized price is human-scaled. Must land before or with the
exit path.
