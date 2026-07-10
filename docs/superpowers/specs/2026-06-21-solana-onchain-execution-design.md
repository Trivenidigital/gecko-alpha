# Solana On-Chain Execution Adapter — Design Spec

Date: 2026-06-21
Status: Design approved, pending implementation plan
Owner: gecko-alpha live-trading expansion
Related: `docs/runbooks/live-trading-deploy.md`, `Downloads/offshore_handoff_live_trading_2026_06_21.md`

## 1. Goal

Add a Solana on-chain execution venue to gecko-alpha's existing live engine so that
early Solana signals — the strategy's actual edge — can route to a real DEX swap,
while inheriting all existing safety machinery (routing, exposure gates, kill switch,
idempotency, reconciliation, ledgers, dashboard) unchanged.

This trades the strategy's edge directly, rather than proving plumbing on Binance
(which rarely lists the early small-cap tokens gecko-alpha detects).

### Non-goals

- No change to the Binance spot path.
- No Kraken / CCXT / MCP work in this project (separate venue projects later).
- Minara stays **alert-only**. gecko-alpha custodies the wallet and executes swaps
  itself via a DEX aggregator; Minara is not the executor.
- No auto-enable / scaling logic. Rollout stays manual: paper → shadow → tiny live.

## 2. Key Decisions (from brainstorming)

1. **Venue = Jupiter aggregator.** Routes across all Solana DEXes, exposes a clean
   quote + swap-transaction API, and returns real `priceImpactPct` we can feed into
   the existing slippage/depth gate.
2. **gecko holds the keys.** gecko signs and broadcasts swaps. Minara stays alert-only.
3. **Custody (phase 1) = hot wallet on `srilu-vps`** with a tiny, auto-swept float.
   Encrypted at rest, decrypted in-process, never logged or committed. A **signer
   seam** lets us move signing into an isolated signer service later without
   rewriting the adapter.
4. **Architecture = fit the existing `ExchangeAdapter` contract** (Approach A). The
   engine, gates framework, kill switch, ledgers, and dashboard keep treating the new
   venue as "a venue named `solana`." Only the adapter knows it is on-chain.

## 3. Architecture

```
 paper-trade open
        │
        ▼
   LiveEngine.on_paper_trade_opened        (unchanged)
        │
   ┌────┴─────────────────────────────────────────┐
   │ Gates.evaluate()                              │  ← extend: add on-chain gates
   │  kill_switch · allowlist · venue · DEPTH      │     (price-impact, sellability/rug,
   │  · SLIPPAGE · exposure · balance · gas        │      gas reserve)
   └────┬─────────────────────────────────────────┘
        │ pass
        ▼
   RoutingLayer.get_candidates()            ← register "solana" venue + adapter
        │
        ▼
   adapter.place_order_request()    ──►  Jupiter quote → build swap tx
        │                                 → SIGNER seam → send to RPC
        ▼
   adapter.await_fill_confirmation() ──►  poll RPC, parse out-amount = real fill
        │
        ▼
   live_trades ledger  +  correction_counter  +  dashboard   (unchanged)
        │
        ▼
   Evaluator (exit)  ──►  SELL swap (token→USDC) via same adapter → reconcile
```

### New code

- `scout/live/solana_swap_adapter.py` — `SolanaSwapAdapter(ExchangeAdapter)`. The only
  component that knows the venue is on-chain.
- `scout/live/solana/jupiter_client.py` — quote + swap-transaction HTTP calls. **No keys.**
- `scout/live/solana/wallet.py` — the **signer seam**. The **only** file that touches the
  private key. Interface: `pubkey()`, `sign(tx_bytes) -> signed_tx_bytes`.
- `scout/live/solana/rpc.py` — `send_transaction`, `confirm`, `get_token_balance`,
  `simulate_transaction` (shadow mode).

### Reused unchanged

`LiveEngine`, `KillSwitch`, `idempotency`, `reconciliation` scaffolding, all ledger
tables (`live_trades`, `shadow_trades`, `venue_health`, `wallet_snapshots`, etc.), the
dashboard.

### Extended (not rebuilt)

- `Gates` — new on-chain checks (price-impact, sellability/rug, gas reserve).
- `RoutingLayer` — register the `solana` venue.
- `scout/config.py` — new `SOLANA_*` settings block.
- adapter factory wiring + `LIVE_VENUE_PREFERENCE`.

## 4. Components

### 4.1 `SolanaSwapAdapter` — the 8 `ExchangeAdapter` methods, on-chain

| Method | Implementation | Notes |
|---|---|---|
| `resolve_pair_for_symbol(symbol)` | symbol → **mint address** (gecko already has the SPL address; reuse `_looks_like_spl_address`) | "pair" = `MINT/USDC`. |
| `fetch_venue_metadata(canonical)` | Jupiter token list / quote probe → `VenueMetadata(venue="solana", venue_pair=mint, quote="USDC", min_size, decimals)` | Not routable → return `None` (= not listed). |
| `fetch_price(pair)` | Jupiter quote for a tiny notional → `out/in` mid | Quote-derived, no order book. |
| `fetch_depth(pair, limit)` | Jupiter quote **at trade size** → convert `priceImpactPct` into a synthetic `Depth`/slippage the existing gate understands | Price impact replaces the order-book walk. |
| `place_order_request(request)` | quote(`slippageBps`) → build swap tx → `wallet.sign()` → `rpc.send()` → return **tx signature** as `venue_order_id` | `client_order_id` recorded before send. |
| `await_fill_confirmation(...)` | poll `getSignatureStatuses` → confirmed-success / confirmed-failed / dropped-timeout → parse actual out-amount → real fill price + realized slippage | maps to existing `OrderConfirmation` statuses. |
| `fetch_account_balance(asset="USDC")` | `getTokenAccountBalance` for USDC ATA; also check SOL for gas | two-asset check. |
| `send_order` (legacy) | leave `NotImplementedError` | unused by routing path, same as Binance stub. |

### 4.2 Sub-modules

- **`jupiter_client.py`** — `get_quote(input_mint, output_mint, amount, slippage_bps)`,
  `build_swap_tx(quote, user_pubkey, priority_fee)`. Pure HTTP, no keys, fully mockable.
- **`wallet.py`** — signer seam. Phase-1 concrete impl `LocalEncryptedSigner` loads the
  key from an encrypted secret, decrypts in memory, never logs/persists plaintext. Later
  `RemoteSigner` implements the same two methods → zero adapter changes. Only file that
  holds the private key.
- **`rpc.py`** — `send_transaction`, `confirm(signature, timeout)`, `get_token_balance`,
  `simulate_transaction`. Configurable endpoint; recommend a paid/private RPC for
  reliability and some MEV protection.

### 4.3 Buy and sell

Exit (TP/SL/max-duration) is a **second swap (token→USDC)**. The adapter's
`place_order_request` takes `side`; `sell` flips input/output mints. Exit reconciliation
parses the sell tx's USDC out-amount for realized PnL. No separate exit module — same
adapter, same ledger, append-only contract preserved.

### 4.4 Config (`SOLANA_*`)

`SOLANA_RPC_URL`, `SOLANA_WALLET_SECRET` (encrypted), `SOLANA_JUPITER_URL`,
`SOLANA_SLIPPAGE_BPS_CAP`, `SOLANA_PRIORITY_FEE_LAMPORTS`, `SOLANA_MAX_PRICE_IMPACT_PCT`,
`SOLANA_MIN_SOL_GAS_RESERVE`, `SOLANA_FLOAT_CAP_USD`. Reuses all `LIVE_*` gates. Venue
registers under `LIVE_VENUE_PREFERENCE`.

## 5. Data Flow & Lifecycle

### 5.1 Modes (reuse existing `LIVE_MODE`)

- **`paper`** — unchanged. Minara alert still emits its copy-paste command (human cross-check).
- **`shadow`** — get the real Jupiter quote **and** `simulateTransaction` (no broadcast),
  write a `shadow_trades` row with simulated entry price/impact. Proves routing, sizing,
  gas, sellability, and quote quality without spending.
- **`live`** — actually signs + sends.

### 5.2 BUY lifecycle (live)

```
1. paper trade opens on an early Solana signal
2. LiveEngine.on_paper_trade_opened() → eligible? not killed?
3. Gates.evaluate():
     kill_switch → allowlist → venue resolves (mint routable on Jupiter?)
     → DEPTH gate = price-impact ≤ SOLANA_MAX_PRICE_IMPACT_PCT
     → SLIPPAGE gate = quote slippage ≤ cap
     → sellability/rug gate (simulate a sell back; honeypot check)
     → exposure cap (ceiling = SOLANA_FLOAT_CAP_USD) → balance gate (USDC + SOL gas)
4. record_pending_order() → live_trades row, status=open, client_order_id  [BEFORE send]
5. adapter.place_order_request():
     Jupiter quote(slippageBps) → build swap tx → wallet.sign() → rpc.send()
     → return tx signature as venue_order_id
6. adapter.await_fill_confirmation():
     poll getSignatureStatuses → confirmed-success | confirmed-failed | dropped/timeout
     → parse actual out-amount → real fill price + realized slippage_bps
7. persist fill to live_trades (txn lock) + increment correction_counter
8. venue_health / wallet_snapshots updated → dashboard shows position
```

### 5.3 SELL / exit lifecycle

```
Evaluator decides exit (TP / SL / max-duration — existing logic unchanged)
  → adapter.place_order_request(side="sell") → token→USDC swap
  → await_fill_confirmation → parse USDC out → realized PnL
  → live_trades row closed (append-only preserved)
  → daily PnL feeds existing kill_switch daily-loss-cap
```

### 5.4 On-chain states mapped to existing enums

| On-chain outcome | Mapped status | Money impact |
|---|---|---|
| tx confirmed, swap succeeded | `filled` | bought |
| tx confirmed, swap reverted (slippage exceeded on-chain) | `rejected`/failed | only gas lost |
| tx dropped / timed out (never landed) | timeout → reconcile | nothing spent, must verify |
| tx stuck unknown at restart | boot reconciliation re-queries signature | resolved on startup |

`reconciliation.py` gains an on-chain path: on boot, any `live_trades` row with a tx
signature but no terminal status is re-checked against the chain (the signature is the
source of truth). On-chain equivalent of the existing shadow reconciliation.

### 5.5 Float boundary

A **daily sweep job** (new small systemd timer, mirroring existing watchdogs): if wallet
USDC balance > `SOLANA_FLOAT_CAP_USD`, sweep the excess to the configured cold wallet.
Bounds live exposure to the float regardless of wins.

## 6. On-Chain Gates

Existing gates stay; these slot into `Gates.evaluate()` (first-failure-wins):

| Gate | Check | Reject reason | Rationale |
|---|---|---|---|
| Price-impact (replaces depth-walk) | Jupiter `priceImpactPct` ≤ `SOLANA_MAX_PRICE_IMPACT_PCT` | `insufficient_depth` | No order book; impact is the liquidity signal. |
| Slippage | quote `slippageBps` ≤ `LIVE_SLIPPAGE_BPS_CAP` | `slippage_exceeds_cap` | reuse existing gate, fed by quote. |
| **Sellability / rug** | simulate a sell back (token→USDC) for the would-be position; must route + simulate cleanly | `not_sellable` (new) | Most important on-chain gate. Honeypots let you buy but not sell. |
| Gas reserve | SOL balance ≥ `SOLANA_MIN_SOL_GAS_RESERVE` after the trade | `insufficient_balance` | a swap with no SOL for fees just fails. |
| Float / exposure | existing exposure cap, ceiling = `SOLANA_FLOAT_CAP_USD` | `exposure_cap` | bounds live risk to the swept float. |

## 7. Error Handling

Modeled on Binance's error taxonomy so it plugs into the same retry/kill machinery:

- **Transient** (RPC 5xx, blockhash expired, send timeout) → bounded retry with fresh
  blockhash, then `VenueTransientError`. **Idempotency-critical:** retry re-sends only
  if there is no confirmed signature; a confirmed signature is never re-sent.
- **Auth/config** (bad key, RPC rejects) → hard error, never retry, surface to operator.
- **Slippage-revert on-chain** (tx landed, swap failed) → terminal `rejected`, only gas
  lost, logged with signature.
- **Dropped tx** → timeout → boot reconciliation resolves against the chain.
- **RPC/venue down** → `venue_health` marks `solana` unhealthy → routing skips it → no
  blind trades.

## 8. Safety Wiring (handoff hard rules)

- **Kill switch** — inherited. Daily-loss-cap works because sell reconciliation feeds
  realized PnL. Manual kill stops on-chain dispatch like any venue. Plus a **wallet-drain
  tripwire**: an unexpected balance drop beyond tolerance triggers the kill switch +
  Telegram alert.
- **Idempotency** — `client_order_id` recorded before send; confirmed tx signature is the
  on-chain dedup key; never re-send a confirmed signature.
- **Secrets** — private key only in `wallet.py`, encrypted at rest, never logged, never
  committed, excluded from backups.
- **Watchdog + SLO** — new `solana_execution` freshness watchdog (hard rule #3) + the
  daily sweep timer.
- **Telegram** — reuse the alerter with `parse_mode=None` + `source=` labels; alert on
  kill, drain tripwire, sweep, and any automated state reversal.

## 9. Testing (TDD)

Tests written before implementation, mirroring `tests/live/`:

1. `jupiter_client` — mocked HTTP: quote parsing, price-impact extraction, swap-tx build,
   error responses. No network.
2. `wallet` / signer seam — sign produces valid signature bytes; key never appears in
   logs/repr; `RemoteSigner` stub satisfies the same interface (proves the seam).
3. `rpc` — mocked confirm states: success, swap-reverted, dropped/timeout; balance parsing.
4. `SolanaSwapAdapter` contract tests — the same adapter-contract suite Binance passes, so
   the engine can't tell venues apart. Plus on-chain specifics: price-impact→depth mapping,
   buy/sell symmetry, fill-amount parsing.
5. Gates — price-impact reject, sellability/honeypot reject, gas-reserve reject, float cap.
6. Idempotency — never re-send a confirmed signature; concurrent-send race hits the UNIQUE
   constraint.
7. Reconciliation — boot recovery of a pending tx by signature (confirmed / failed / dropped).
8. Integration — `shadow` loop (quote + simulate, no broadcast) opens/evaluates/closes a
   `shadow_trades` row end-to-end against mocks.

## 10. Rollout Ladder (enforced)

`paper` (already running) → **`shadow`** one signal (quote + simulate; prove routing, gas,
sellability) → **tiny live** one signal at `$10` with a `$50` float → prove fill + sell +
reconciliation → only then widen signals/size.

Per the handoff: do not flip to live by flag alone. Requires runtime-state verification,
signal eligibility review, wallet funding, and operator sign-off.

## 11. Open Items for the Implementation Plan

- Exact Jupiter API version/endpoints and quote/swap request shapes.
- Encryption scheme for `SOLANA_WALLET_SECRET` (at-rest + in-memory handling).
- Priority-fee strategy (static vs dynamic) and whether to use Jito bundles for MEV
  protection in a later phase.
- Cold-wallet address + sweep mechanics (also a signed swap/transfer).
- `wallet_snapshots` / `venue_health` row shapes for the `solana` venue.
- Confirmation level (`confirmed` vs `finalized`) and timeout tuning.

### Deferred (described above but NOT yet implemented)

These two safety features are specified (§7, §8) but are **not** implemented in
the first rollout. They are listed here explicitly so no reviewer or operator
assumes they are active:

- **Transient blockhash retry (§7).** The bounded "retry with a fresh blockhash"
  loop is NOT implemented. A dropped / blockhash-expired send currently surfaces
  as an error and the tx (if it ever landed) is recovered on the next boot by
  reconciliation (`solana_reconciliation.py`), which re-checks every open
  `solana` row by its persisted signature against the chain. Code marker:
  `scout/live/solana/rpc.py`.
- **Wallet-drain tripwire (§8).** The "unexpected balance drop beyond tolerance
  → engage kill switch + Telegram alert" tripwire is NOT implemented. The hot
  wallet is currently bounded only by the static `SOLANA_FLOAT_CAP_USD` exposure
  gate (Gates.`evaluate_onchain`) and the daily sweep decision
  (`scripts/solana_sweep.py`); there is no active drain detector. Code markers:
  `scripts/solana_sweep.py`, `scout/live/solana_factory.py`.

## 12. Pre-Implementation Review Addendum (2026-06-21)

A code-grounded + live-API review before execution surfaced four items that
amend this design. The implementation plan
(`docs/superpowers/plans/2026-06-21-solana-onchain-execution.md`) reflects all of
them:

1. **Engine is single-adapter → isolated on-chain fork.** The live engine's
   `_dispatch_live` ignores the routed `venue` and always calls one adapter; `Gates`
   is bound to one adapter too. So "reuse the engine" (Approach A) is implemented as
   an **isolated fork**: `LiveEngine` gains an optional `onchain_adapter`; when present
   it builds a second `Gates` instance and `on_paper_trade_opened` forks Solana-chain
   signals to a new `_dispatch_onchain`. When `onchain_adapter is None`, the Binance
   path and its tests are byte-for-byte unchanged. (Chosen over generalizing the engine
   to multi-adapter, to minimize blast radius on the tested live path.)

2. **`live_trades.status` is CHECK-constrained.** Allowed: `open / closed_tp /
   closed_sl / closed_duration / closed_via_reconciliation / rejected /
   needs_manual_review`. A filled buy is an **open position** → status stays `'open'`
   (record `entry_fill_price`); `'filled'`/`'timeout'` are never written. Failed
   swap → `'rejected'`; dropped/timeout → stays `'open'` for boot reconciliation.

3. **Jupiter v6 endpoints deprecated (Oct 2025).** Use `https://api.jup.ag/swap/v1`
   with a free `x-api-key` (config `SOLANA_JUPITER_API_KEY`), or keyless
   `lite-api.jup.ag`. `priceImpactPct` confirmed a fraction (×100 for percent).

4. **solders idioms verified.** `VersionedTransaction(message, [keypair])` signs
   (current, not deprecated). Test fixtures must build messages with
   `MessageV0.try_compile(payer, ixs, [], Hash.default())` — blockhash is a `Hash`,
   not bytes.

New flagged follow-up (non-blocking): non-native mint resolution (CoinGecko-slug
tokens whose mint is in `platforms.solana`) needs a network lookup; first rollout
snipes native Solana tokens where `coin_id` IS the mint.
