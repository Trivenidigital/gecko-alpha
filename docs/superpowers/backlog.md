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

## BL-059 — Paper-Trading Quality: Close first_signal Category + Non-ASCII Leak

**Status:** Ready — next to implement (un-deferred 2026-04-22)
**Depends on:** nothing
**Blocks:** BL-060 (clean paper input is a prerequisite for live-mirror accuracy)

The `first_signal` pipeline has no category filter — PR #44 added `_JUNK_CATEGORIES`
+ `_is_junk_coinid()` gates to the narrative-prediction path, but `first_signal`
never touches them. Two Chinese-meme tokens (`我踏马来了`, `币安人生`) slipped
through 2026-04-22 and bled -$57 combined in open paper positions.

Scope:
- Route `first_signal` predictions through the same `_normalize_category()` +
  `_is_junk_coinid()` filters that narrative-prediction already uses
- Add non-ASCII symbol filter to catch Chinese-meme / cyrillic / other
  non-Latin token symbols that currently pass every existing gate
- Backfill-close any open paper trades matching the new filters (preserve
  closed-trade history for combo_performance training)
- 19-test parametrized coverage matching PR #44 pattern

Non-goals:
- Broader symbol-blacklist UI (just code-level filters for v1)
- Retroactive deletion of historical closed trades (learning data is valuable)

---

## BL-060 — Paper-Mirrors-Live: Score Threshold + `would_be_live` Oracle + Dashboard Numbering

**Status:** Ready — implement after BL-059
**Depends on:** BL-059 (clean paper input), ideally before BL-055 shadow soak so
  the soak's `would_be_live` subset is representative
**Blocks:** nothing directly; feeds BL-055 shadow-soak quality

**Core principle (locked 2026-04-22):** paper-trading must mimic the capital
constraints of live trading. $2000 capital supports ~20 concurrent positions in
live. Paper currently runs at 139 concurrent. Without constraint, `combo_performance`
trains on marginal signals that would never execute live, biasing the learning
signal toward what-we-can-detect instead of what-we-can-trade.

**Approach — Combined B + C (decided after considering A=hard-cap, B=dual-track,
C=score-threshold alone):**

- **C (input pool cleanup):** Raise quant-score threshold to target
  **40-60 concurrent open trades** (down from ~139). Strips bottom-half-by-score
  signals that never should have entered. Threshold is a .env knob, operator-tunable.
- **B (live-eligible oracle):** Within the cleaner pool, mark trades at open-time
  with `would_be_live: bool`. Stamping logic: if currently-open `would_be_live=true`
  count < 20 → flag true, else false. **FCFS within slots — no dynamic re-ranking,
  no closing an in-profit slot to chase a higher-score signal** (that would
  introduce the signal-churn trap we already know hurts). Flag is immutable once
  set at open.

**Dashboard UX (bundled in same PR):**
- Number each open paper trade 1-N by **P&L rank** (1 = strongest gainer, current
  sort order). Renumbering happens on each sort/refresh — not a persistent ID.
- Visual badge / emoji for `would_be_live=true` rows so operator can eyeball
  "which 20 we'd actually have traded live."
- Summary line: "N open (M live-eligible, K beyond-cap)"

**A/B evaluation (the payoff):**
Once enough closed trades accumulate, compare the two populations:
- Win-rate, avg P&L, Sharpe of `would_be_live=true` subset
- Same metrics for `would_be_live=false` subset
- If the top-20 genuinely outperforms, the constraint is validated and BL-055
  shadow soak uses `would_be_live=true` rows as its oracle. If not, the score
  threshold needs re-tuning OR the ranking logic is wrong — either way, we
  learn it before real money is on the line.

**Why considered and NOT chosen:**
- **A (hard cap at 20, refuse trade 21+):** throws away learning data. We'd
  never find out whether signals 21-100 would have been winners. Blunt.
- **B alone (keep volume, add flag):** doesn't address the garbage-in-garbage-out
  problem for combo_performance — it still trains on marginal signals.
- **C alone (raise threshold only):** gives cleaner data but no A/B mechanism
  to validate that the top-20 actually outperforms the rest. You'd flip to
  live blind.

Scope:
- Schema: `paper_trades.would_be_live INTEGER NOT NULL DEFAULT 0 CHECK (would_be_live IN (0,1))`
- Config: `PAPER_MIN_SCORE_THRESHOLD` (existing knob — retune) + `PAPER_LIVE_ELIGIBLE_CAP` (new, default 20)
- Logic: `PaperTrader.open_trade()` computes `would_be_live` flag at commit time
- Dashboard: P&L rank numbering + live-eligible badge in `open_positions` component
- Metrics surface: rolling win-rate / P&L for each subset (updates in existing weekly digest)

Non-goals:
- Dynamic re-flagging mid-life (FCFS is the point — mimics live's "don't churn")
- Variable `PAPER_LIVE_ELIGIBLE_CAP` per signal type (one global cap for v1)
- Auto-tuning the score threshold (operator picks it based on target volume)

---

## BL-061 — Propagate category_name to snapshot tables for full PR #44 gate

**Status:** Deferred (split out of BL-059 on 2026-04-22)
**Depends on:** nothing
**Blocks:** nothing directly; strengthens filters applied by BL-059

BL-059 could only apply `_is_junk_coinid()` + non-ASCII ticker to the 6
non-prediction paper-trade paths, because the intermediate snapshot tables
those paths query (`gainers_snapshots`, `losers_snapshots`, `trending_snapshots`,
volume-spike source, `first_signals` source, `chain_matches`) carry no
`category_name` column. Only the `predictions` table has it — which is why
PR #44's `_normalize_category()` gate works there.

Result: narrative-prediction has a stronger junk filter than the other 6 paper
paths. Any category-matched junk that doesn't look wrapped/bridged by coin_id
AND has an ASCII ticker can still leak via volume_spike / gainers / losers /
first_signal / trending / chain_completed.

Scope:
- Schema: add `category_name TEXT` column to each of the 6 snapshot/source
  tables (one migration per table or one combined migration — decided during plan)
- Ingestion: backfill category_name at write time — category comes from
  CoinGecko `/coins/markets` response (primary) or from a join against
  `category_snapshots` (fallback when markets call missed it)
- Signals: replace the `_is_junk_coinid()` + non-ASCII `ticker` filter in the
  6 `trade_*` functions with the full `_normalize_category()` + `_is_junk_coinid()`
  gate that `trade_predictions` uses
- `CandidateToken.category_name: str | None` field + factory-method population
  (from_coingecko, from_dexscreener, from_geckoterminal) — DexScreener and
  GeckoTerminal may not carry category; document the None fallback
- Tests: ingestion-side tests that category populates correctly per source;
  signals-side tests that category-matched junk is now filtered on all 6 paths

Non-goals:
- Retroactively backfilling `category_name` on historical rows (only new writes
  need to carry it — the filter applies to newly-opened trades)
- Removing `_is_junk_coinid()` + non-ASCII filter (they catch cases category
  alone would miss — all three filters should coexist)
- Category propagation to trade logs / paper_trades rows (not needed for gating;
  only needed at open-time decision)

**Why deferred:** BL-059 shipped with coin_id + non-ASCII only (closes observed
leaks: wrapped tokens, Chinese memes). This BL-061 upgrade is "nice to have"
unless category-matched junk (e.g. "stock market themed", "music", "murad picks")
starts leaking through the weaker filter. Monitor paper_trades for junk-category
entries; if they appear, prioritize BL-061.

---

## Previously shipped (historical)

- **BL-052** — GeckoTerminal per-chain trending (PR #35, merged 2026-04-20)
- **BL-053** — CryptoPanic news feed (PR #36, merged 2026-04-20)
- **BL-054** — Perp WS anomaly detector (PR #37, merged 2026-04-20)
