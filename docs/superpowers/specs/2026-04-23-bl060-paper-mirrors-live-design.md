# BL-060: Paper mirrors live — design

**Date:** 2026-04-23
**Author:** trivenidigital
**Status:** approved for implementation

## Goal

Shape paper trading so its aggregate P&L answers "what would the live cohort have done this week?" The current paper run admits 100+ concurrent trades against a $200k wallet; live would admit ~20 against a ~$20k wallet. BL-060 adds three mechanisms so the comparison is legitimate:

1. A quant-score admission gate on `trade_first_signals` so paper admit rate is operator-tunable (target 40–60 concurrent opens).
2. A `would_be_live` flag stamped at open-time using a concurrent 20-slot capital-constraint oracle.
3. Dashboard + weekly digest surfaces that separate the live-eligible cohort from the beyond-cap cohort, with a two-week side-by-side A/B.

## Non-goals

- Real capital deployment (BL-055).
- Auto-tuning threshold.
- Backfill of pre-existing rows into the A/B cohort.
- Changes to signal dispatchers other than `trade_first_signals`.
- Per-combo A/B breakdown (n too small).

## Memory principles anchoring the design

- **Mid-flight flag migration** (feedback memory, 2026-04-23) — new open-time flag is nullable with no default; pre-existing rows remain NULL; all A/B analysis filters `WHERE flag IS NOT NULL`; never default-stamp, never force-close.
- **Paper mirrors live** (feedback memory) — paper volume ok, but the capital-constrained live-eligible subset must always be marked.

Both principles apply to two distinct cutovers:
- **Schema cutover** — the ALTER TABLE migration leaves pre-existing rows NULL.
- **Regime cutover** — `PAPER_MIN_QUANT_SCORE=0` defines "no admission regime set," also stamped NULL. Real 0/1 stamps only begin when the operator sets the threshold.

A/B queries filtering `WHERE would_be_live IS NOT NULL` handle both cutovers uniformly.

## Schema

### Column

Added via the existing `_migrate_feedback_loop_schema` path in `scout/db.py:820`. One new entry in `expected_cols`:

```python
expected_cols = {
    "signal_combo": "TEXT",
    "lead_time_vs_trending_min": "REAL",
    "lead_time_vs_trending_status": "TEXT",
    "would_be_live": "INTEGER",   # NEW — nullable, no default
}
```

Rationale:
- **Nullable**: two NULL-producing regimes (pre-cutover rows, pre-threshold stamps) must be indistinguishably "unknown" to downstream queries.
- **No default**: every INSERT computes the value explicitly via subquery; "someone forgot to set it" is not a reachable state.
- **No CHECK constraint**: SQLite `ALTER TABLE ADD COLUMN` rejects CHECK; adding one would require a table rebuild. Invariant enforced by the INSERT subquery, which returns exactly NULL, 0, or 1.
- **Fresh-install parity**: `would_be_live INTEGER` also added to the `CREATE TABLE paper_trades` block at `scout/db.py:552` so clean installs and migrated installs are schema-identical.

### Index

```sql
CREATE INDEX IF NOT EXISTS idx_paper_trades_would_be_live_status
  ON paper_trades(would_be_live, status);
```

Column-order rationale — load-bearing for query 2, not query 1:
- **Query 1** (stamp subquery, `status='open' AND would_be_live=1`) has equality predicates on both columns; SQLite uses both regardless of index order.
- **Query 2** (digest A/B, `WHERE would_be_live IS NOT NULL GROUP BY would_be_live`) needs `would_be_live` as the leading column for an index-only scan. This is the reason for the ordering — do not reorder "for selectivity" on query 1.

**Partial index considered, deferred.** `CREATE INDEX … WHERE would_be_live IS NOT NULL` would stay small as pre-cutover NULLs accumulate indefinitely (no backfill ever). At 131 rows the full index is trivial; revisit when `paper_trades` NULL-count passes ~10k (expected timescale: >6 months at current admit rate).

### Rollback safety

The migration is purely additive: new column, nullable, no default, no data change. Reverting the code leaves orphan `would_be_live` values that new code ignores. **Do not propose a destructive "cleanup" migration** — SQLite's awkward DROP COLUMN semantics aside, the NULL column values carry no cost and potentially retain value for future audits.

## Stamp logic

### Site

`scout/trading/paper.py:71` inside `PaperTrader.execute_buy`. Single modified INSERT with subquery stamp + RETURNING read of the resolved value. No changes to `TradingEngine`.

### INSERT with atomic subquery

```sql
INSERT INTO paper_trades
  (token_id, symbol, name, chain, signal_type, signal_data,
   entry_price, amount_usd, quantity,
   tp_pct, sl_pct, tp_price, sl_price,
   status, opened_at,
   signal_combo, lead_time_vs_trending_min, lead_time_vs_trending_status,
   would_be_live)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?,
  (SELECT CASE
     WHEN ? = 0 THEN NULL
     WHEN COUNT(*) < ? THEN 1
     ELSE 0
   END
   FROM paper_trades
   WHERE status='open' AND would_be_live=1))
RETURNING would_be_live
```

Bind parameters: `(..., min_quant_score, live_eligible_cap)`.

Three outcomes:
- `min_quant_score == 0` → **NULL** (regime undefined; A/B excluded).
- `min_quant_score > 0` AND live-eligible open-count `<` cap → **1**.
- `min_quant_score > 0` AND live-eligible open-count `>=` cap → **0**.

`RETURNING would_be_live` avoids a second `SELECT … WHERE id=?` round trip. `cursor.lastrowid` stays populated after `fetchone()` (asserted in test #1).

### In-code comment at the stamp site

Retained as a non-obvious WHY:

```python
# The inline subquery makes would_be_live stamping race-free at the SQL
# layer. Today, Database._conn is single-writer (aiosqlite serializes all
# ops on one connection), so the race cannot surface. The subquery is
# defensive against a future per-writer refactor. Load-bearing invariant:
# one of {single-writer connection, atomic subquery} must hold — don't
# remove both at once.
```

### Settings threading

`live_eligible_cap: int` and `min_quant_score: int` added to `PaperTrader.execute_buy` as **required kwargs** (no defaults). `TradingEngine.open_trade` reads `settings.PAPER_LIVE_ELIGIBLE_CAP` and `settings.PAPER_MIN_QUANT_SCORE` and passes them through. Defaulted kwargs would let a test or caller silently fall back to a hardcoded value — exactly the bug the kwarg-threading was meant to prevent.

### Cap-hit observability

Info log fires only when the stamp resolves to 0:

```python
if would_be_live_stamped == 0:
    log.info(
        "paper_live_slot_cap_reached",
        cap=live_eligible_cap,
        signal_type=signal_type,
        signal_combo=signal_combo,
        token_id=token_id,
    )
```

Structured event; answers "why did this trade stamp =0?" without a DB query. No log on NULL or 1.

## Score-threshold gate

### Config knobs

Added to `scout/config.py`:

| Knob | Default | Purpose |
|---|---|---|
| `PAPER_MIN_QUANT_SCORE` | `0` | Minimum `quant_score` to admit a `first_signal` trade. `0` disables the gate (and NULL-stamps `would_be_live`). |
| `PAPER_LIVE_ELIGIBLE_CAP` | `20` | Concurrent live-slot cap. Trades beyond this stamp `would_be_live=0`. |

Default-`0` for `PAPER_MIN_QUANT_SCORE` is deliberate — deploying code must not change admission behavior. Operator explicitly sets the threshold after running the audit.

### Gate location

`scout/trading/signals.py:322-324`:

```python
min_quant = settings.PAPER_MIN_QUANT_SCORE
skipped_below_threshold = 0
for token, quant_score, signals_fired in scored_candidates:
    if quant_score <= 0 or not signals_fired:
        continue
    if quant_score < min_quant:
        skipped_below_threshold += 1
        logger.debug(  # debug-level, not info — sticky rejections would spam
            "signal_gated_below_threshold",
            coin_id=token.contract_address,
            symbol=token.ticker,
            quant_score=quant_score,
            min_quant=min_quant,
            signal_type="first_signal",
        )
        continue
    # ...existing junk / mcap / chain filters unchanged
```

`skipped_below_threshold=N` added to the existing end-of-loop summary log (`scout/trading/signals.py:378 trade_first_signals_filtered`) alongside `skipped_large` / `skipped_junk`. The summary-log gating condition expands to include `skipped_below_threshold`.

Per-token log is **debug**, not info — `scored_candidates` regenerates each 15-min cycle; sticky low-score tokens would re-log every cycle (~2,900 info events/day at 30 sticky tokens). Operators watching info logs see aggregate; debuggers get detail on demand.

### Scope boundary

The gate lives **only** inside `trade_first_signals`. All other dispatchers (`trade_losers_contrarian`, `trade_gainers_early`, `trade_volume_spikes`, `trade_trending_catch`, `trade_narrative_predictions`, `trade_chain_completions`, `trade_long_holds`) are unchanged.

`trade_narrative_predictions` is explicitly deferred with a revisit trigger at **15 concurrent narrative trades** — today's 7-concurrent cohort is below the sample size where a threshold could be calibrated from data, so adding `PAPER_MIN_NARRATIVE_FIT_SCORE` now would be guessed, which defeats the audit-first principle.

## Backfill audit script

Path: `scripts/bl060_threshold_audit.py`. One-shot, read-only.

### Query

```sql
SELECT
    json_extract(signal_data, '$.quant_score') AS qscore,
    status,
    opened_at
FROM paper_trades
WHERE signal_type = 'first_signal'
  AND opened_at >= datetime('now', '-7 days')
```

JSON path verified against `scout/trading/signals.py:368-371` — `signal_data={"quant_score": quant_score, "signals": signals_fired}` for `first_signal` trades.

### Output

Distribution histogram by score bucket, plus a projection table with **two lines per threshold**:

```
Projection @ threshold T:
  T=30 → 47 projected steady-state concurrent (ratio)
  T=30 → 41 current open survives (direct)
```

- **Ratio line**: `current_concurrent × (admits_at_T / admits_at_T=0)`. Steady-state estimate assuming trade-duration distribution is insensitive to quant_score.
- **Direct line**: `SELECT COUNT(*) FROM paper_trades WHERE signal_type='first_signal' AND status='open' AND json_extract(signal_data,'$.quant_score') >= :T`. Current-open survival count.

The gap between the two lines tells the operator how skewed the open cohort is. If they diverge significantly, the steady-state projection is less trustworthy and the operator knows to re-calibrate sooner.

### Printed caveats

- Projection assumes trade-duration distribution is independent of `quant_score`.
- 7-day window chosen because it post-dates the BL-059 junk-filter deploy (2026-04-22); longer windows would mix regimes.
- Script does not predict `would_be_live=1` stamp rate (depends on arrival ordering, not threshold).

## Dashboard

Changes scoped to `dashboard/frontend/components/TradingTab.jsx` + one backend whitelist extension.

### Rank column

Prepended to the open-positions table. Label: **`Rank`** (not `#` — avoids "row counter" expectation).

Stable P&L rank computed once per render:

```jsx
const pnlRankMap = useMemo(() => {
  const byPnl = [...positions].sort(
    (a, b) => (b.unrealized_pnl_pct ?? -Infinity) - (a.unrealized_pnl_pct ?? -Infinity)
  )
  const m = new Map()
  byPnl.forEach((p, idx) => m.set(p.id, idx + 1))
  return m
}, [positions])
```

Header: `<SortHeader col="pnl_pct" label="Rank" />` — clicking triggers P&L sort, which matches the column's meaning.

**Null-pnl rank renders as `—`, not a number.** `unrealized_pnl_pct == null` (price cache miss) must not display as `#79` / `#80` — that looks like "worst performers" when the data is just missing. The `-Infinity` coalesce still sorts them to the bottom; the cell shows a dash.

### Live-eligible badge

Rendered inside the Rank cell:

| `would_be_live` | Rendered | Meaning |
|---|---|---|
| `1` | `5 ⚡` (green) | live-eligible |
| `0` | `5` (plain) | beyond-cap |
| `null` | `5 ·` (muted dot, tooltip "unscoped") | pre-cutover OR pre-threshold |

Badge on Rank cell anchors "this is N of 20 live slots." No separate column; tight visual association.

### Summary-line breakdown

`TradingTab.jsx:318`:

```
80 active (20 live-eligible ⚡ · 59 beyond-cap · 1 unscoped)
```

Counts:

```jsx
const liveEligibleCount = positions.filter(p => p.would_be_live === 1).length
const beyondCapCount    = positions.filter(p => p.would_be_live === 0).length
const unscopedCount     = positions.filter(p => p.would_be_live === null).length
```

**Gating** — breakdown renders only when `positions.some(p => 'would_be_live' in p)` (migration has run at least once). Pre-migration, the legacy `"{openCount} active paper trade{s}"` text renders unchanged; `would_be_live` field is absent from the payload and the breakdown stays hidden.

Label **"unscoped"** (not "pre-cutover") because two NULL-producing regimes now coexist: pre-cutover rows (pre-migration) and pre-threshold stamps (`PAPER_MIN_QUANT_SCORE=0`). Both render identically; "unscoped = excluded from A/B" is the actionable semantic.

### Backend whitelist extension

`dashboard/db.py:_get_trading_positions_inner` at line 890 — add `would_be_live` to the SELECT column list. Single-line diff; must be explicit in the implementation plan (the SELECT is a whitelist, not `SELECT *`).

### Closed-trades table — deliberately unchanged

Closed-trades rendering below the open table gets no changes. The A/B story for closed trades is the weekly digest's job, not a per-row badge.

## Weekly digest A/B

Extends the existing weekly digest (`scout/feedback/digest.py` or equivalent — confirm at implementation time) with one new A/B section and one per-path line.

### Cohort filter (every digest query in this section)

```sql
WHERE signal_type IN (...)
  AND status IN ('closed_tp', 'closed_sl', 'closed_expired', 'closed_trailing_stop')
  AND would_be_live IS NOT NULL
  AND opened_at >= :window_start
```

`CLOSED_COUNTABLE_STATUSES` from `scout/trading/paper.py:15` is reused. `would_be_live IS NOT NULL` is load-bearing per feedback-memory principle.

### Two-week side-by-side layout

```
BL-060 A/B — live-eligible vs beyond-cap
=========================================
Window:  this week (2026-04-30 → 2026-05-07) vs last week (2026-04-23 → 2026-04-30)
Context: 20 live-eligible open · 59 beyond-cap open · 0 unscoped

LIVE-ELIGIBLE (would_be_live=1, closed trades only):
  Win-rate:  47.2% this week | 44.8% last week   (n_closed=87 | 81)
  Avg P&L:   +3.1% this week | +2.7% last week   (n_closed=87 | 81)
  Sharpe:    0.42 this week  | 0.31 last week    (n_closed=87 | 81)

BEYOND-CAP (would_be_live=0, closed trades only):
  Win-rate:  32.1% this week | 30.9% last week   (n_closed=243 | 201)
  Avg P&L:   -1.2% this week | -0.8% last week   (n_closed=243 | 201)
  Sharpe:    0.15 this week  | 0.09 last week    (n_closed=243 | 201)

Delta (live-eligible minus beyond-cap):
  Win-rate:  +15.1pp this week | +13.9pp last week
  Avg P&L:   +4.3pp this week  | +3.5pp last week

Per-path within live-eligible cohort:
  first_signal            41.8% win, +2.1% avg  (n_closed=55)
  narrative_prediction    62.5% win, +7.9% avg  (n_closed= 8)  ← small-n caveat
  trending_catch          50.0% win, +3.4% avg  (n_closed=14)  ← small-n caveat
  ...
```

### Metric rules

- **`n_closed` counts closed trades only.** Open trades appear once at the section header as context, never in metric `n`.
- **Sharpe: always-show with `noisy` annotation below `n_closed=30`**. Example: `Sharpe: 0.42 (n_closed=22, noisy)`. Preserves week-over-week trajectory. Binary suppression at n=20 discards a useful-if-imprecise signal.
- **Delta line excludes Sharpe.** If either side is below n=30, a Sharpe delta is too noisy to be meaningful.
- **Per-path small-n caveat suffix** (`← small-n caveat`) when `n_closed < 20`. Same principle as Sharpe.
- **Delta units are percentage points (`pp`)**, not `%` — avoids absolute-vs-relative ambiguity.

### First-week edge

When `window_start - 7d` returns zero eligible rows (first digest post-cutover), the last-week column renders `—`:

```
Win-rate:  47.2% this week | — last week   (n_closed=87 | —)
```

No crash; operator sees explicit "no reference frame yet."

### Per-path structural-advantage watch

The per-path line surfaces whether `narrative_prediction` (ungated) is winning live-slot arrival races against `first_signal` (gated). If narrative's real signal quality is lower but it keeps winning slots by arriving first, the A/B would show narrative "beating" first_signal for the wrong reason. No automatic interpretation — digest shows the numbers, operator judges.

## Testing

### File mapping

| File | Tests |
|---|---|
| `tests/test_trading_db_migration.py` | schema add-column + parity |
| `tests/test_paper_trader.py` | stamp logic on shared conn |
| `tests/test_paper_trader_concurrency.py` **(new)** | multi-writer race |
| `tests/test_trading_signals.py` | score-threshold gate + scope boundary |
| `tests/test_trading_dashboard.py` | whitelist SELECT extension |
| `tests/test_trading_digest.py` | A/B cohort + WoW + Sharpe-noisy |

### Test list

| # | Test | Pins down |
|---|---|---|
| 1 | Fresh DB: first N opens up to cap stamp `=1`; (N+1)th stamps `=0`. Assert both `trade_id > 0` AND `would_be_live_stamped in (0, 1)` on the same cursor. Fallback path documented: `SELECT last_insert_rowid()` if aiosqlite `RETURNING + lastrowid` interaction is broken. | Baseline subquery correctness + aiosqlite `RETURNING + lastrowid` compatibility. |
| 2 | Close a `=1` trade → next open stamps `=1`. | Concurrent-cap semantic (slots free on close). |
| 3a | Shared-conn sanity: 40 coroutines on `db._conn`, cap=20 → exactly 20 rows `=1`. | Subquery returns correct 0/1 distribution. |
| 3b | Multi-writer stress: 4 separate `aiosqlite.connect` with WAL + `PRAGMA busy_timeout=5000`, 40 concurrent INSERTs, cap=20 → exactly 20 rows `=1`. In-test comment: *"WAL required because rollback-journal mode rejects concurrent writers with SQLITE_BUSY — this pins the SQL invariant, not the locking model. Prod's safety comes from the single-writer connection."* | Belt-and-suspenders: SQL is race-free if a future refactor adds per-writer connections. |
| 4 | Pre-cutover row (manually seeded NULL) stays NULL after migration runs twice. | Idempotent migration doesn't overwrite NULLs. |
| 5 | Stamp=0 fires `paper_live_slot_cap_reached` info log with `cap`, `signal_type`, `signal_combo`, `token_id`. | Observability contract. |
| 6 | `PAPER_LIVE_ELIGIBLE_CAP=0` → every open stamps `=0` (when threshold active). | Edge: cap=0 disables live-eligibility entirely. |
| 7 | Closed trades with `=1` do NOT count toward the cap. | Subquery predicate correctness. |
| 8 | `get_trading_positions` returns `would_be_live` after whitelist extension. | Dashboard contract. |
| 9 | Weekly digest query filters `WHERE would_be_live IS NOT NULL` and groups correctly. | A/B cohort scoping. |
| 10 | Fresh install (`_create_tables`) and migrated install both have `would_be_live` column + index. | Schema parity. |
| 11 | Candidate with `quant_score < PAPER_MIN_QUANT_SCORE` is skipped; `skipped_below_threshold` increments in summary log; per-token log is **not** INFO (debug-level only). | Gate logic + log-level discipline. |
| 12 | `PAPER_MIN_QUANT_SCORE=0` admits all (pre-BL-060 behavior preserved) AND stamps `would_be_live=NULL`. | Default-0 is non-breaking AND implements regime cutover. |
| 13 | Other dispatchers (`trade_losers_contrarian`, `trade_narrative_predictions`, etc.) admit normally regardless of threshold. | Scope-boundary regression guard. |
| 14 | Pre-cutover rows (NULL) and pre-threshold rows (NULL) both excluded from both A/B cohorts. | A/B cohort scoping for both NULL regimes. |
| 15 | `n_closed=22` → renders `"0.42 (n_closed=22, noisy)"`; `n_closed=31` → `"0.42 (n_closed=31)"`. | Sharpe noisy annotation threshold. |
| 16 | First-week post-cutover → last-week column renders `—`; no crash. | Week-over-week first-week edge. |

### Test patterns reused

- `structlog.testing.capture_logs` — for #5, #11
- `tmp_path` + `Database.initialize()` — shared-conn tests
- `pytest-asyncio` auto-mode
- `aiosqlite.connect` direct — only #3b

### TDD task sequence

1. Schema migration → #4, #10
2. Stamp logic + cap-hit log → #1, #2, #5, #6, #7, #3a, #3b
3. Dashboard whitelist → #8
4. Score-threshold gate + regime NULL → #11, #12, #13
5. Digest A/B → #14, #15, #16, #9

### Not tested

- End-to-end pipeline (covered by existing ingestion-to-paper integration tests).
- React component (manual dashboard smoke, same stance as existing project convention).
- Audit script (one-shot operator tool, human-read output; JSON path correctness covered by pre-commit grep).

## Config knobs summary

| Knob | Default | Env | Purpose |
|---|---|---|---|
| `PAPER_MIN_QUANT_SCORE` | `0` | `.env` | Minimum quant_score for `first_signal` admission. 0 disables gate AND NULL-stamps. |
| `PAPER_LIVE_ELIGIBLE_CAP` | `20` | `.env` | Concurrent live-slot cap for `would_be_live=1` stamps. |

## Files touched

### Modified

| File | What |
|---|---|
| `scout/db.py` | `expected_cols` adds `would_be_live`; `_create_tables` adds same column; new index create |
| `scout/config.py` | Two new Settings fields |
| `scout/trading/paper.py` | `execute_buy` INSERT with subquery + RETURNING; cap-hit log; required kwargs |
| `scout/trading/engine.py` | `open_trade` reads settings and passes through |
| `scout/trading/signals.py` | Gate inside `trade_first_signals`; `skipped_below_threshold` summary log |
| `dashboard/db.py` | `_get_trading_positions_inner` SELECT includes `would_be_live` |
| `dashboard/frontend/components/TradingTab.jsx` | Rank column, badge, summary breakdown |
| existing digest module | A/B section + per-path + WoW two-week layout |

### Created

| File | Purpose |
|---|---|
| `scripts/bl060_threshold_audit.py` | One-shot threshold calibration tool |
| `tests/test_paper_trader_concurrency.py` | Multi-writer race stress (#3b) |
