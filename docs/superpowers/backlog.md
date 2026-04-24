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

## BL-065 — Propagate category_name to snapshot tables for full PR #44 gate

**Status:** Deferred (split out of BL-059 on 2026-04-22; renumbered from BL-061 on 2026-04-23 to resolve naming collision with shipped ladder ticket)
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
leaks: wrapped tokens, Chinese memes). This BL-065 upgrade is "nice to have"
unless category-matched junk (e.g. "stock market themed", "music", "murad picks")
starts leaking through the weaker filter. Monitor paper_trades for junk-category
entries; if they appear, prioritize BL-065.

---

## BL-062 — Signal Stacking + Peak-Fade Early Kill (E1 sustained)

**Status:** Ready — write spec next (retro complete, variant chosen 2026-04-23)
**Depends on:** nothing
**Blocks:** nothing
**Motivation:** 281-trade retrospective (2026-04-24) showed two structural leaks:
  1. `first_signal+momentum_ratio` is 49% of volume (138 trades) at +0.15% avg
     P&L — 109 of 138 expire at the 24h timer. Signal bar is too liberal.
  2. 188 of 281 closed trades (67%) expire. Avg peak during hold is +6% but
     avg realized is -1.83% — no exit-on-peak-fade logic before the timer.

**Retro outcomes (2026-04-23):**
- **Q1 answer = A:** require `len(signals_fired) >= 2` for `first_signal`
  admission (any two independent signals, not momentum-specific).
- **Q2 variant chosen = E1 sustained-fade, threshold=10%** after pre-registered
  test of three variants (E1 sustained, E2 deeper, E3 retrace+decay). Pre-reg
  pass criteria: clip ≤ 30%, fires ≥ 15, sign-weighted avg delta ≥ +1.0pp.
  E1 @ thresh=10 posted: 25 fires, 0 clips (structurally — two-observation rule
  makes clip-only-on-last-observation impossible), +1.04pp avg delta. E2 was
  strictly worse (52-92% clip ratios). E3 was untestable on the expired cohort
  (0/201 had gainers_snapshots at T+0 and T+6h — the complement problem;
  see BL-066 for re-scoping).
- **Framing:** E1 is a *temporal* two-signal rule (fade at 6h AND 24h, 18h
  apart) — validates the Q1 intuition that stacking across any independent
  axis beats tuning a single-axis threshold.

Ship scope:
- **Signal stacking (Q1):** `trade_first_signals` admission guard —
  `if len(signals_fired) < 2: continue`. Simple count gate; no per-signal
  allowlist. Combo keys still encode the full signal set (no change).
- **Peak-fade E1 (Q2):** new exit branch — fire when
  `peak_pct >= 10` AND `cp_6h IS NOT NULL` AND `cp_24h IS NOT NULL` AND
  `cp_6h < 0.7 * peak_pct` AND `cp_24h < 0.7 * peak_pct`. Close at market at
  the 24h-observation tick.
- **Pre-ship items (mirror BL-061 cohort discipline):**
  1. **A/B flag column:** `peak_fade_fired_at TIMESTAMP NULL` on `paper_trades`.
     Pre-cutover rows = NULL. Forward A/B scoped to `peak_fade_fired_at IS NOT NULL`
     opened-after-cutover rows (same pattern as `would_be_live`).
  2. **Data-availability precondition:** rule only fires when BOTH checkpoints
     exist — no half-firing on `cp_6h` alone. Prevents naive-retrace HARKing.
  3. **Fire-time semantics:** rule is evaluated at the 24h-observation tick,
     not a continuous poll. Closes on the evaluator pass that records `cp_24h`.
  4. **Exit precedence (hard-coded order):**
     SL > ladder leg 1 > ladder leg 2 > trail-stop > peak_fade > expiry.
     - Ladder legs fire on their own triggers first; peak_fade only applies to
       `remaining_qty` after legs fill.
     - If trail-stop is armed AND eligible on the same evaluator pass as
       peak_fade, trail wins.
     - Peak_fade applies to `remaining_qty` only (can coexist with partial
       ladder fills).
  5. **30-day calibration review:** add checkpoint to the 2026-05-23 BL-061
     review (or file a parallel review). **Stop rule: if forward clip% > 15%
     over ≥20 fires, revert the feature flag.**
- Config knobs under `.env`:
  `PEAK_FADE_ENABLED` (bool, default true),
  `PEAK_FADE_MIN_PEAK_PCT` (default 10),
  `PEAK_FADE_RETRACE_RATIO` (default 0.7 — dual-observation),
  `FIRST_SIGNAL_MIN_SIGNAL_COUNT` (default 2)
- Parametrized tests covering: admission gate (1 vs 2+ signals), E1 fire
  conditions (both checkpoints present, both below ratio), E1 no-fire paths
  (missing checkpoint, only one below ratio), exit precedence ordering,
  remaining_qty handling with ladder legs.

Non-goals:
- Retroactively closing open trades (new rules apply to new opens + forward
  evaluation of existing opens but no forced-close on legacy positions).
- Touching the BL-061 ladder cascade itself (peak_fade is additive).
- Tightening other combos (first_signal+momentum_ratio is the 49%-volume
  culprit; others average better — leave them alone).
- E2 / E3 exit variants (retro rejected E2; E3 moved to BL-066).

---

## BL-066 — Rank-Decay Exit Research on Gainers-List Cohort

**Status:** Deferred — research ticket, queue after BL-062 ships
**Depends on:** nothing (BL-062 is the sister exit rule; BL-066 is independent)
**Blocks:** nothing
**Motivation:** BL-062's E3 variant (retrace + gainers-rank-decay confirmation)
  tested at 0/201 coverage on the expired cohort — because expired-to-noise
  trades are the complement set of trades that reach the gainers list. Rank-decay
  mechanically cannot exist for trades that never had a rank. The axis is real
  but the cohort was wrong.

Scope:
- **Cohort:** trades that DID reach the gainers list (non-NULL rank in
  `gainers_snapshots` at some point during the hold), then faded back.
  *Complement* of BL-062's cohort by construction.
- **Rule candidates to test:** peak_rank crossed → current_rank degraded by X
  places OR dropped off list entirely; combined with a price retrace guard.
- **Pre-registered methodology:** same discipline as BL-062 retro — declare
  pass criteria (clip ratio, fires, sign-weighted avg delta) before running
  any simulation. Exact thresholds TBD based on gainers-list sub-population
  distribution.

Non-goals:
- Running on the expired cohort (E3 already proved that's a dry hole).
- Replacing E1 peak_fade (BL-066 is additive; it targets a different cohort
  and can coexist).
- Adaptive ranking thresholds per signal combo (single global rule for v1).

---

## BL-067 — Score-Decay Exit Research on Expired Cohort (contingent)

**Status:** Contingent — viability confirmed 2026-04-23, empirical coverage TBD
**Depends on:** nothing
**Blocks:** nothing
**Motivation:** If rank-decay cannot target the expired cohort (BL-066 handles
  the complement), score-decay is the next candidate axis for the expired
  population. Score IS written mid-hold for any token that re-appears in
  ingestion (`scout/main.py:602` logs `score_history` per cycle for every
  enriched token). Axis exists mechanically — but empirical coverage is
  gated on how often expired tokens re-surface through ingestion during the
  hold window.

**Viability gate (completed 2026-04-23):**
- `log_score` fires in the per-cycle scoring loop at `scout/main.py:602` for
  every enriched candidate. Not entry-only. ✅ Axis mechanically viable.
- Outstanding: empirical check — of the 201 expired trades, how many have
  `score_history` rows recorded DURING hold (between `opened_at` and
  `closed_at`)? If < ~30% have any mid-hold score updates, axis is
  empirically dead on this cohort.

Scope (if empirical gate passes):
- Define score-decay rule (TBD in brainstorm): e.g. `score_at_entry - current_score >= N`
  over hold window ≥ X hours.
- Pre-registered simulation against expired cohort with same discipline as
  BL-062 (clip, fires, avg delta thresholds declared first).

Non-goals:
- Running if mid-hold coverage < 30% of cohort (stop at viability gate).
- Replacing E1 peak_fade (BL-067 is additive; targets expired cohort alongside).

**Post-hoc axis-swap risk noted:** BL-067 is a separate ticket specifically to
avoid HARKing from BL-062's failed E3 test. Don't merge scope across tickets.

---

## BL-063 — Adaptive Ladder: ATR-Scaled + Narrative-Gated Exit Legs

**Status:** Deferred — revisit after BL-061 ladder has 30+ post-cutover trades
**Depends on:** BL-061 (shipped 2026-04-23), ideally post-cutover calibration data
**Blocks:** nothing
**Motivation:** 281-trade retro showed fixed 25%/50%/trail under-captures on
  trending majors (Katana ran to +140%, we trail-stopped each of 3 waves at
  ~12-16%). Fixed ladder fits meme-cycle tokens (Wojak +40-47% TP fires clean)
  but leaves money on trending-coin runs that genuinely deserve a wider trail
  while the thesis holds.

Scope (TBD in brainstorm):
- **ATR-scaled legs:** replace fixed 25%/50% with legs tied to recent volatility
  (e.g. ATR-14 from score_history price series). Low-vol tokens get tighter
  legs; high-vol trending gets wider.
- **Narrative-gated extensions:** if `category_heating` or `cg_trending_rank`
  is still active when leg 2 fires, push trailing-floor from -12% to -20% OR
  add a leg 3 at +75-100%. Thesis intact → hold longer.
- **Signal-decay hard kill:** mirror of above — if thesis decays (trending
  rank falls, category cools), accelerate exit regardless of price.

Key open questions for brainstorm:
- How do we detect "thesis still intact" at runtime without expensive lookups?
  (Cache category_heating + trending rank on the trade row at open-time,
  refresh every N minutes?)
- Should adaptive legs replace fixed legs or coexist with a config toggle?
- Does ATR-14 compute sensibly for tokens with <14 hours of history?

Non-goals:
- Full machine-learning exit model (rules-based adaptive is enough for current
  data scale — see `feedback_ml_not_yet` memory)
- Adaptive entry logic (this is exit-only; entry is BL-062's territory)

---

## BL-064 — Investigate long_hold Combo: Why So Few Trades?

**Status:** Deferred — investigate after BL-062 ships
**Depends on:** nothing
**Blocks:** nothing
**Motivation:** 281-trade retro showed `long_hold` combo has the best avg PnL
  (+16.76%) across all paper combos — but only 8 trades total. 0 expired, 0 SL,
  0 TP, 8 trailing_stop (avg peak +32%, decent capture). Either the gate is too
  strict (leaving alpha on the floor) or the signal is genuinely rare. Worth
  investigating before we assume it's pure alpha.

Investigation scope:
- Read `scout/trading/paper.py` to understand `long_hold` trigger conditions
- Query `candidates` / `signal_events` for tokens that *nearly* qualified for
  long_hold but were rejected — compare their subsequent price action to the
  8 that did qualify
- If the 8 qualifiers outperform the near-misses by a wide margin → gate is
  correctly tight, leave alone
- If near-misses performed comparably → gate threshold is too strict, propose
  a relaxed variant and A/B via a new signal combo

Non-goals:
- Immediate implementation — this is a research ticket to inform whether
  BL-06X is justified. Produces findings + recommendation, not code.

---

## Previously shipped (historical)

- **BL-052** — GeckoTerminal per-chain trending (PR #35, merged 2026-04-20)
- **BL-053** — CryptoPanic news feed (PR #36, merged 2026-04-20)
- **BL-054** — Perp WS anomaly detector (PR #37, merged 2026-04-20)
- **BL-055** — Live trading execution core (PR #47, merged 2026-04-23, shadow mode)
- **BL-059** — Paper-trading quality: first_signal junk filter (PR #44, merged 2026-04-22)
- **BL-060** — Paper-mirrors-live + would_be_live oracle (PR #48, merged 2026-04-23)
- **BL-061** — Paper-trading ladder redesign (PR #48, merged 2026-04-23; PR #49 hardening merged 2026-04-23)
