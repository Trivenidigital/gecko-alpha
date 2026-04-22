# Backlog

Tracked items that have a defined scope but are not yet scheduled.
Format: `BL-XXX` identifiers. Add to this list before starting a brainstorm
so future sessions can pick up context.

---

## BL-055 — Live Trading: Execution Core + Binance Spot + Venue Registry

**Status:** Spec approved 2026-04-22 — ready for implementation plan
**Spec:** `docs/superpowers/specs/2026-04-22-bl055-live-trading-execution-core-design.md`
**Depends on:** nothing
**Blocks:** BL-056, BL-057, BL-058

Foundation: define the trade lifecycle, pre-trade safety gates, order state
machine, and a single working CEX adapter (Binance spot). Prove the end-to-end
path with tiny test orders before expanding to more venues.

Scope:
- `scout/live/` package with `ExecutionEngine` + adapter interface
- Binance-only adapter (CCXT or python-binance — decided during brainstorm)
- Venue registry: given `coin_id`, list supported venues + trading pair
- Pre-trade gates: price staleness, balance check, daily loss cap, kill switch
- `live_trades` data model (or mode column on `paper_trades` — decided during brainstorm)
- Full-auto execution, no manual confirmation
- Configurable via `.env`: enabled signals allowlist, position size, max open, daily loss cap

---

## BL-056 — Live Trading: Multi-CEX Expansion (Bybit, MEXC, Kraken, Kucoin, Coinbase)

**Status:** Deferred
**Depends on:** BL-055

Add adapters for remaining major CEXs once the core interface is proven. Mostly
config + credentials + per-venue quirk handling. Scope per exchange:
- API key management
- Per-exchange symbol mapping (e.g. `btc` → `BTCUSDT` on Binance vs `BTC-USD` on Coinbase)
- Fee schedule
- Per-venue rate limits
- Venue-specific order types / time-in-force quirks

---

## BL-057 — Live Trading: On-Chain Execution (ETH / BASE / Solana)

**Status:** Deferred
**Depends on:** BL-055

Add wallet-based spot execution across three chains. Completely different
plumbing from CEX — wallet signing, RPC, DEX aggregators, gas/MEV/slippage
management.

Scope:
- EVM adapter (ETH + BASE): 0x aggregator or 1inch, wallet via Privy/Turnkey
  or local-signed hot wallet with strict balance cap
- Solana adapter: Jupiter aggregator, Phantom-style signer or Turnkey
- Per-chain gas/fee estimation
- MEV protection (Flashbots for ETH, Jito for Solana)
- Bridge detection (don't buy the wrapped token on wrong chain)
- Token contract safety check (reuse existing GoPlus integration)

---

## BL-058 — Live Trading: Signal→Execution Bridge + Performance-Gated Auto-Enable

**Status:** Deferred
**Depends on:** BL-055

Bridge between the paper-trading layer and live execution. Uses
`combo_performance` to auto-promote proven signal combos to live trading,
auto-demote failing ones. Also contains the top-level risk orchestration that
spans venues.

Scope:
- Per-signal-type live-enable flag (.env or DB-driven)
- Auto-promotion rules: `combo_performance` win_rate >= X% AND trades >= N
- Auto-demotion rules: drawdown, loss-streak, win-rate decay
- Global kill switch: daily loss cap, per-venue exposure cap, equity floor
- Reconciliation job: detect drift between local `live_trades` state and
  exchange/wallet actual positions

---

## Previously shipped (historical)

- **BL-052** — GeckoTerminal per-chain trending (PR #35, merged 2026-04-20)
- **BL-053** — CryptoPanic news feed (PR #36, merged 2026-04-20)
- **BL-054** — Perp WS anomaly detector (PR #37, merged 2026-04-20)
