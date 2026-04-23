# BL-061 — Paper-Trading Ladder Redesign (TP Ladder + Floor + BL-060 Retirement)

**Status:** approved 2026-04-23
**Supersedes:** BL-060 `would_be_live` threshold gate
**Builds on:** BL-059 junk filter, BL-055 live-trading core (shadow mode)

## Goal

Replace the fixed +20% TP / -10% SL exit policy on all paper trades with a three-leg ladder (+25% / +50% / 12% trailing stop on runner) plus floor protection, widen SL to 15%, and retire the BL-060 `would_be_live` score-based A/B gate. Primary motivation: the current exit policy captures ~11–14% average final P&L on trades whose observed peaks average 14–29% — a systematic 13–15% giveback that the ladder is designed to reduce.

Secondary purpose: the redesign is **as much a measurement intervention as a strategy change**. Historical peak data is right-censored by the current +20% TP, so the EV direction of ladder-vs-flat-TP cannot be proved from backward-looking data. Ship with `ladder_leg_fired` / `floor_activated` instrumentation so a 30-day post-cutover review can do the honest EV comparison.

## Architecture

Single unified exit policy for all six signal types. The `PaperEvaluator` loop already visits every open trade on each tick and has access to current price, entry price, and peak tracking. The ladder is implemented as additional state on `paper_trades` (`leg_1_filled_at`, `leg_2_filled_at`, `remaining_qty`, `floor_armed`) and additional branches in the evaluator's exit-check cascade, executed in this priority order:

1. Stop loss (-15% from entry on the **remaining** quantity, cancelled once floor is armed)
2. Leg 1 TP (at +25%, sells 30% of original quantity, records leg_1_filled_at)
3. Leg 2 TP (at +50%, sells 30% of original quantity, records leg_2_filled_at)
4. Floor exit (if floor_armed and current price ≤ entry, sells remaining runner slice)
5. Trailing stop (12% below peak, active on runner slice only, cancelled by floor if it would exit below entry)
6. Expiry (existing 48h timeout, sells remaining qty at current price)

Partial fills record individual `paper_trade_fills` rows; the parent `paper_trades` row aggregates realized_pnl incrementally. Close status becomes one of: `closed_sl`, `closed_ladder_complete`, `closed_floor`, `closed_trailing_stop`, `closed_expired`.

## Tech Stack

Unchanged: Python 3.12 async, aiosqlite, Pydantic v2 settings, structlog. No new dependencies.

## Components

### 1. Schema additions (migration)

Add nullable columns to `paper_trades`:

| Column | Type | Meaning |
|--------|------|---------|
| `leg_1_filled_at` | TEXT | ISO timestamp of +25% partial sell, NULL until fired |
| `leg_1_exit_price` | REAL | fill price at leg 1 |
| `leg_2_filled_at` | TEXT | ISO timestamp of +50% partial sell, NULL until fired |
| `leg_2_exit_price` | REAL | fill price at leg 2 |
| `remaining_qty` | REAL | qty still open; equals `quantity` pre-leg-1, decays with each leg |
| `floor_armed` | INTEGER | 0/1; set to 1 when leg 1 fires |
| `realized_pnl_usd` | REAL | running total from partial fills; finalized when last slice exits |

Pre-cutover rows (opened before migration) keep NULL for these columns and run to completion under **old** policy. Post-cutover rows initialize `remaining_qty = quantity`, `floor_armed = 0`. The evaluator branches on "is this a pre-cutover row" by checking `leg_1_filled_at IS NULL AND created_at < cutover_ts`.

**Cutover ts is captured at migration time as a single row in a new `paper_migrations` table**, so the evaluator's post-vs-pre check remains deterministic across service restarts.

### 2. Config (`scout/config.py`)

New fields (all with sensible defaults, env-overridable):

```python
PAPER_LADDER_LEG_1_PCT: float = 25.0
PAPER_LADDER_LEG_1_QTY_FRAC: float = 0.30
PAPER_LADDER_LEG_2_PCT: float = 50.0
PAPER_LADDER_LEG_2_QTY_FRAC: float = 0.30
PAPER_LADDER_TRAIL_PCT: float = 12.0
PAPER_SL_PCT: float = 15.0  # WAS 10.0 — widened
PAPER_LADDER_FLOOR_ARM_ON_LEG_1: bool = True
```

**Removed fields:**
- `PAPER_MIN_QUANT_SCORE` (BL-060 gate — always NULL-stamped in prod, never activated)
- `PAPER_LIVE_ELIGIBLE_CAP` (BL-060 FCFS slot cap — defunct with gate removal)

### 3. Execute buy (`scout/trading/paper.py`)

**Remove** BL-060 stamping subquery from `INSERT_SQL`. The `would_be_live` column continues to exist and accept NULL writes (default) but no caller stamps it. Kwargs `live_eligible_cap` and `min_quant_score` removed from `execute_buy` signature.

**Add** initialization of `remaining_qty = quantity` and `floor_armed = 0` in the INSERT. `realized_pnl_usd` initializes to 0.0.

BL-055 live_engine handoff hook (lines 173-195) **unchanged** — still fires on paper admission for eligible signal types. This is load-bearing for BL-055 shadow observation (see First_Signal Rationale below).

### 4. Evaluator exit cascade (`scout/trading/evaluator.py`)

Replace the current single-exit check with the six-branch cascade listed under Architecture above. Each leg fire:

1. Computes partial sell qty: `leg_qty = initial_quantity * LEG_N_QTY_FRAC`
2. Applies exit slippage
3. Updates `remaining_qty -= leg_qty`, sets `leg_N_filled_at`, `leg_N_exit_price`
4. Increments `realized_pnl_usd`
5. Emits `ladder_leg_fired` structlog event with `{trade_id, leg, peak_pct_at_fire, fill_price, realized_pnl_usd}`
6. On leg 1: sets `floor_armed = 1`, emits `floor_activated` event

### 5. Signals gate (`scout/trading/signals.py`)

Line 320 `min_quant = settings.PAPER_MIN_QUANT_SCORE` and the 327-336 skip block: **remove**. `first_signal` admission returns to pre-BL-060 behavior (quant_score > 0 and signals_fired, no threshold gate).

### 6. BL-060 retirement

Per prod-state audit (2026-04-23):
- 273 rows all NULL-stamped (gate default-off produced NULL per design)
- Column and index exist
- VPS `.env` never set `PAPER_MIN_QUANT_SCORE` / `PAPER_LIVE_ELIGIBLE_CAP`

**Disposition of stamped values:** ignore. All values are already NULL. No data reinterpretation needed.

**Atomic retirement commit:** removes stamp subquery from `execute_buy`, kwargs from call sites (`scout/trading/engine.py:234`, `scout/trading/evaluator.py:219`, `scout/trading/weekly_digest.py:142`), config fields + validators, signal gate in `signals.py:320`, weekly digest A/B reporting blocks. Tests: delete BL-060 behavior tests, keep schema-migration test to validate the column survives upgrade-from-pre-BL-060.

**Column stays in DB.** Dropping a column in sqlite requires a full table rebuild; the NULL-stamped rows are harmless.

### 7. Dashboard frontend rebuild

VPS bundle dated 2026-04-20 23:16 — pre-BL-060. Frontend TradingTab.jsx (lines 320-380) already has PnL rank + live-eligible ⚡ badge code. Rebuild steps:

- Remove the ⚡ badge rendering (JSX lines 330-340, 377-378) since BL-060 is retired
- Keep PnL rank column — orthogonal, already works from `unrealized_pnl_pct` sort
- Add new "Legs" column rendering `leg_1_filled_at` / `leg_2_filled_at` state (▣ for filled, ○ for pending)
- `npm run build` on VPS, reload `gecko-dashboard.service`

### 8. Instrumentation

New structlog events:
- `ladder_leg_fired` — fires on each partial sell, includes `trade_id, leg, peak_pct_at_fire, fill_price, realized_pnl_usd, remaining_qty`
- `floor_activated` — fires once per trade when leg 1 fires, includes `trade_id, peak_pct_at_activation`
- `floor_exit` — fires when floor triggers a close at entry, includes `trade_id, peak_pct_at_exit`

Weekly digest adds a new section: "Ladder performance per signal" showing leg 1 hit rate, leg 2 hit rate, avg realized_pnl_pct, avg peak_pct — the data that drives the 30-day calibration review.

## Data flow (for a +45% peak trade)

```
admit → open (remaining_qty=Q, floor_armed=0, SL=-15%)
   ↓ price climbs past +25%
leg 1 fires → sell 0.3Q at +25%, remaining_qty=0.7Q, floor_armed=1, realized=+7.5% notional
   ↓ price continues to +45% peak
   ↓ price retraces past 12% trail from +45% peak (i.e. price at +33.0% from entry)
trailing stop fires → sell 0.7Q at +33.0%, realized += 0.7 * 33.0% = +23.1% notional
   ↓
final realized_pnl_pct ≈ (0.3 * 25 + 0.7 * 33) / 1.0 = 30.6%
close status: closed_trailing_stop
```

Compare to old policy: would have closed entire position at +20% TP for +20% final.

## Error handling

- Slippage failures on partial sells: retry once with fresh price; if second attempt also fails, log `ladder_leg_sell_failed` and leave remaining_qty unchanged (skip to next tick)
- Floor + trailing stop race (both want to exit below entry simultaneously): floor wins (exit at entry, not below)
- Database write failure during partial sell: rollback the fill; re-attempt next tick (idempotent via `leg_N_filled_at IS NULL` guard)

## Testing strategy

1. Unit: ladder fires in peak-trajectory order for simulated price paths (rising monotonic, V-shaped, W-shaped)
2. Unit: floor blocks below-entry close on runner slice after leg 1 fires
3. Unit: floor does not affect already-realized leg 1 / leg 2 proceeds
4. Unit: SL at 15% fires on pre-leg-1 trades, does not fire after leg 1 (replaced by floor)
5. Unit: pre-cutover rows (leg_1_filled_at IS NULL AND created_at < cutover_ts) continue with old 20/10 policy
6. Unit: new rows (created_at >= cutover_ts) use new ladder
7. Unit: BL-060 retirement — `execute_buy` signature no longer accepts `live_eligible_cap` / `min_quant_score`, all call sites updated
8. Integration: full open-to-close trajectory with instrumentation event capture
9. Migration: upgrade-from-pre-BL-060 still works (tests/test_trading_db_migration.py must not regress)

## First_signal rationale (load-bearing analysis)

**Decision: keep first_signal under the unified ladder.**

`first_signal` has the worst paper-trading P&L (-$287, 37% win rate, 98 closed trades, avg peak 8.1%). Under the proposed ladder, leg 1 at +25% will essentially never fire for this cohort (peak cap is 12% in observed data). Most first_signal trades will exit via SL (now -15%) or expiry.

Retiring first_signal from paper admission would save ~$3/trade × 50 trades/week ≈ $150/week. But it would also eliminate BL-055 shadow mode's observation channel: `PaperTrader.execute_buy` (paper.py:173-195) is the sole trigger for `LiveEngine.on_paper_trade_opened`, and the VPS `LIVE_SIGNAL_ALLOWLIST=first_signal` means no other signal currently reaches shadow.

**Coupling is load-bearing, not orthogonal.** Decoupling shadow observation from paper admission (dispatching directly from `signals.py` before the paper gate) is possible but out of scope — it would require a second handoff path in the signal pipeline and a new telemetry channel, for an estimated savings of ~$150/week. Not worth the complexity at current scale.

**Revisit trigger:** if first_signal paper loss exceeds $1,500/week OR BL-055 shadow produces enough data that the paper-coupled observation channel is no longer needed (estimated after soak completes 2026-04-30), open a follow-up spec to either (a) decouple shadow from paper, or (b) retire first_signal from paper while keeping shadow observation via a direct signal hook.

## Threshold speculation disclosure

The +25% / +50% leg thresholds and 12% trail default are **chosen to match observed cadence, not to maximize measured EV**. Current peak data is right-censored by the existing +20% TP — for every `closed_tp` trade, the true peak (absent early exit) is unknown. Rough math:

- For TP-hitting trades whose true peak falls in the +20–40% band (likely the majority given observed peaks average 14–29%), the new ladder captures ~13% (0.3×25 + 0.7×peak-12) vs the old 20% — a **loss** of ~7pct for these trades
- The ladder only wins on true peaks ≥+50%, which fire rarely in observed data (~5-8% of non-first_signal trades)
- Net EV direction across all trades is genuinely unclear with current data

**This redesign is a measurement intervention as much as a strategy change.** The `ladder_leg_fired` / `floor_activated` events are the primary deliverable; the 30-day post-cutover review (2026-05-23) is the real calibration event. Thresholds will be tuned based on post-cutover data, not locked in from this spec.

## Migration & cutover

1. Deploy new code, run migration (adds columns, creates `paper_migrations` row with cutover_ts)
2. Existing 95 open trades (including ~80 first_signal) continue under old policy — `leg_1_filled_at IS NULL AND created_at < cutover_ts` branch
3. All trades opened after cutover_ts run under new ladder
4. No force-close, no backfill. Per the "mid-flight flag migration" project lesson, pre-cutover rows stay in their pre-cutover policy.

## Deferred / out of scope

- Per-signal ladder thresholds (unified policy for v1)
- Decoupling BL-055 shadow observation from paper admission
- Dropping `would_be_live` column (sqlite column-drop is costly; NULL rows are harmless)
- Dynamic trail % that tightens post-peak (complexity not justified pre-calibration)
- Ladder extension to leg 3 at +100% (peaks ≥+100% are 0 in observed data)

## 30-day calibration review plan

On 2026-05-23, query post-cutover `paper_trades`:

1. Leg 1 hit rate per signal (`leg_1_filled_at IS NOT NULL` / total post-cutover)
2. Leg 2 hit rate per signal
3. Avg realized_pnl_pct per signal, ladder-era vs pre-cutover comparable window
4. Distribution of `peak_pct_at_fire` at leg 1 — is +25% the right threshold, or should it move?
5. Distribution of trail give-back on runner slice — should trail move from 12%?
6. First_signal paper loss running total — has it exceeded $1,500/week revisit trigger?

Act on data, not intuition. Possible outcomes: tighten leg 1 to +20%, widen trail to 15%, restore `PAPER_MIN_QUANT_SCORE` gate for first_signal only, or keep current defaults.
