# BL-062 — Signal Stacking + Peak-Fade Early Kill (E1 sustained)

**Status:** Approved 2026-04-23 — ready for implementation plan
**Retro basis:** Paper-trading retrospective run against production `scout.db`
on 2026-04-23 (300 closed trades). Pre-registered decision rule applied to
three peak-fade variants (E1 sustained, E2 deeper, E3 retrace+decay).
Pass criteria: `clip_ratio <= 0.30 AND fires >= 15 AND avg_delta >= +1.0pp`.

## Goal

Close two structural leaks in paper-trading that the retro surfaced:
1. `first_signal` admits trades on a single scoring signal — of 235 historical
   `first_signal` trades, 234 (99.6%) had exactly one scoring signal in
   `signals_fired`. Rule raises the bar to require two scoring signals before
   opening.
2. 201 of 300 closed trades (67%) expire at the 24h timer. Avg peak during
   hold is +6.0% but avg realized is -1.83%. Add an explicit peak-fade exit
   that fires before expiry when both the 6h and 24h checkpoints show the
   price sustained below 70% of peak.

## Architecture

Two additive changes to `scout/trading/`:

1. **Admission gate** in `trade_first_signals` — short-circuit when fewer than
   two scoring signals are in `signals_fired`. Combo keys, DB writes, scoring
   — all downstream logic unchanged.
2. **New exit branch** in the evaluator loop — fires at the 24h-observation
   tick when both checkpoint observations are present and both show sustained
   fade below 70% of peak. Writes `peak_fade_fired_at` timestamp on the
   `paper_trades` row and closes at market on `remaining_qty`. Sits in a
   fixed precedence chain with existing exits (SL > ladder > trail > peak_fade
   > expiry).

A/B evaluability is preserved with the same cohort-discipline pattern used by
BL-060's `would_be_live` and BL-061's ladder cutover: new column is
`NULL`-valued for pre-cutover rows, and the A/B boundary is keyed off the
`paper_migrations` row written during migration (not off the feature flag).

## Why This Works — The Temporal Two-Signal Framing

The Q1 answer (require ≥2 scoring signals on admission) is a two-signal rule
across the signal *roster*. The Q2 answer (E1 sustained fade at 6h AND 24h)
is a two-signal rule across *time*. Both sit on the same generalization:
stacking across any independent axis beats tuning a single-axis threshold.
The retro tested a naive single-observation retrace rule first (variant E2,
deeper retrace + single observation) — it posted 52-92% clip ratios because
dip-then-recover false positives swamp the signal. E1 requires the fade to
persist across an 18h gap, which is structurally why its clip ratio
collapses to zero on retro data.

BL-066 (gainers-list cohort, rank-decay axis) and BL-067 (expired cohort,
score-decay axis, contingent) extend the same pattern to different axes;
intentionally not bundled here to avoid HARKing.

## Problem — Retro Data

### First-signal volume leak (motivates Q1)

Historical `first_signal` admissions by scoring-signal count in
`signals_fired`:

| n_signals | n_trades |
|---|---|
| 1 | 234 |
| 2 | 1 |

Closed `first_signal` trades by combo key:

| combo_key | total | closed | avg pnl |
|---|---|---|---|
| `first_signal+momentum_ratio` | 210 | 150 | +0.75% |
| `cg_trending_rank+first_signal` | 24 | 17 | +2.19% |
| `first_signal+vol_acceleration` | 1 | 1 | +7.32% |

Note: combo keys include at most one "extra" scoring signal by design
(truncation rule in `scout/trading/combo_key.py:33`). So 210 trades labelled
`first_signal+momentum_ratio` means "first_signal trigger family, with
`momentum_ratio` as the alphabetically-first scoring signal" — does not
imply all 210 had only that one scoring signal (though in practice 234/235
did have exactly one).

The leak: `momentum_ratio` as a lone scoring signal is weak. 99.6% of
`first_signal` admissions sit at `len(signals_fired) == 1`, and the bulk
realized +0.15% avg on the largest sub-combo. Raising to `>= 2` effectively
halts first_signal admission until the scorer surfaces a second signal.
This is intended per Q1=A decision (2026-04-23): require two signals across
any combination.

### Expired-cohort leak (motivates Q2)

201 expired-branch trades analyzed (sourced via `exit_reason = 'expired'`
on `paper_trades`). Peak-bucket histogram:

| peak bucket | n | pos | neg | avg pnl |
|---|---|---|---|---|
| NULL (no peak recorded during hold — early-close before peak_pct update or checkpoint gap) | 22 | 0 | 22 | -7.44% |
| 00-02% | 42 | 1 | 41 | -5.34% |
| 02-04% | 37 | 4 | 33 | -4.31% |
| 04-06% | 35 | 14 | 21 | -1.67% |
| 06-08% | 16 | 7 | 9 | -1.47% |
| 08-10% | 8 | 4 | 4 | -2.66% |
| 10-12% | 18 | 14 | 4 | +2.06% |
| 12-14% | 10 | 4 | 6 | +0.07% |
| 14-16% | 3 | 3 | 0 | +13.28% |
| 16-18% | 4 | 4 | 0 | +12.36% |
| 18-20%+ | 6 | 6 | 0 | +13.95% |

Threshold at 10% catches the uncertain middle (10-14% buckets mixed) before
it drifts negative; above 14% every trade closed positive so peak_fade is a
no-op there (no fade deep enough to trigger).

### Variant test (pre-registered)

Same 201-row cohort, same pass rule:

| variant | thresh | fires | clips | avg Δ | verdict |
|---|---|---|---|---|---|
| E1 sustained (cp6 AND cp24 both < 0.7·peak) | 10 | 25 | 0 | +1.04 | ✅ PASS |
| E2 deeper retrace (single obs, 0.5·peak) | 10 | 40 | 21 | +0.37 | ❌ FAIL (clip 52.5%) |
| E3 retrace + gainers-rank-decay | 10 | 0 | 0 | n/a | N/A (0/201 coverage) |

E3 untestable on expired cohort because expired-to-noise trades are the
*complement* of trades that reached the gainers list — no rank exists for
those rows. Axis re-scoped as BL-066.

## Signal Stacking (Q1)

### Rule

In `scout/trading/signals.py::trade_first_signals`, before opening a trade:

```python
if len(signals_fired) < settings.FIRST_SIGNAL_MIN_SIGNAL_COUNT:
    continue
```

### Semantics — what `signals_fired` counts

`signals_fired` is the scorer output (list returned from
`scout.scorer.score(...)`). It contains **scoring signal names only** —
e.g. `["momentum_ratio"]`, `["momentum_ratio", "vol_acceleration"]`,
`["cg_trending_rank"]`.

It does **not** include the trigger family identifier `"first_signal"`.
That string only appears as the `signal_type` argument to `build_combo_key`
(which then prepends it into the combo key string). The admission gate
counts scoring signals exclusively.

### Behavior

- `FIRST_SIGNAL_MIN_SIGNAL_COUNT` default = 2. Single-scoring-signal
  admissions drop.
- Combo key and signal-list persistence unchanged (`build_combo_key` still
  receives the full set of fired scoring signals; combo_key truncation
  rules in `scout/trading/combo_key.py` unchanged).
- Forward combo-performance analysis automatically separates pre-cutover
  single-signal admissions from post-cutover two-signal-minimum admissions
  via the `paper_migrations.cutover_ts` row.

### Expected impact (exact, from historical data)

- Of 235 historical `first_signal` admissions, 234 had
  `len(signals_fired) == 1` and 1 had `len(signals_fired) == 2`.
- Under the new rule, 234/235 (99.6%) would have been blocked and 1/235
  admitted. Net effect on current data: **first_signal admission is
  effectively halted** until the scorer surfaces a second scoring signal
  organically on a new candidate.
- This is intended. The retro established that single-signal admissions
  carry insufficient signal-to-noise (99.6% of volume on a combo averaging
  +0.15% P&L). If the first_signal admission rate drops to near zero for
  an extended period, that is the rule doing exactly what it was designed
  to do — not a bug.

## Peak-Fade E1 (Q2)

### Rule

In the evaluator pass that records the 24h checkpoint, for any open
position with `remaining_qty > 0`:

```
peak_pct                = max observed unrealized % during hold
cp_6h                   = checkpoint_6h_pct (NULL until 6h observation)
cp_24h                  = checkpoint_24h_pct (NULL until 24h observation)

fire if all:
  peak_pct       >= PEAK_FADE_MIN_PEAK_PCT           (default 10)
  cp_6h          IS NOT NULL
  cp_24h         IS NOT NULL
  cp_6h          <  peak_pct * PEAK_FADE_RETRACE_RATIO   (default 0.7)
  cp_24h         <  peak_pct * PEAK_FADE_RETRACE_RATIO
```

When fired:
- Close `remaining_qty` at market (current price).
- Stamp `peak_fade_fired_at = now(UTC)`.
- Set `exit_reason = 'closed_peak_fade'`.

### Fire-once semantics

The rule cannot re-fire on subsequent evaluator passes because closing sets
`remaining_qty = 0`, which fails the `remaining_qty > 0` guard. Additionally,
a row with `peak_fade_fired_at IS NOT NULL` is already closed — standard
closed-row filtering in the evaluator loop excludes it. Explicit tests
cover both conditions.

### Exit precedence (hard-coded, top to bottom)

1. **Stop-loss** (SL at -15% — unchanged)
2. **Ladder leg 1** (25% at +25% — unchanged, fires on own trigger, reduces
   `remaining_qty`)
3. **Ladder leg 2** (50% at +50% — unchanged, fires on own trigger, reduces
   `remaining_qty` further)
4. **Trailing stop** (armed after leg 1, floor -12% — unchanged)
5. **Peak-fade E1** (NEW — fires on `remaining_qty` only, at 24h-observation
   tick)
6. **Expiry** (24h timer — unchanged fallback)

Precedence rules:
- Ladder legs and trail fire on their own price triggers at any evaluator
  tick. Peak_fade is a 24h-tick-only rule. An evaluator pass that records
  `cp_24h` and would fire peak_fade runs after SL / ladder / trail checks on
  the same pass — so an armed trail wins the tie.
- Peak_fade applies only to `remaining_qty`. If legs 1+2 fully drained the
  position, peak_fade cannot fire (no qty to close).
- Expiry is the residual — fires only if nothing above fired and `hold_h >= 24`.

### Why E1 and not a simpler retrace

A single-observation retrace (E2 family) cannot distinguish "faded" from
"dip-then-recover." The 6h-AND-24h dual-observation structurally requires the
fade to persist across an 18h gap. On retro data that collapsed false fires
to zero; E2 at matching fire-count thresholds posted 52-92% clip ratios.

## Pre-Ship Items (non-negotiable before merge)

### 1. A/B flag column — PRAGMA-gated migration

**Depends on:** BL-061 ladder migration being applied first. If a VPS is
missing BL-061 when BL-062 migration runs, the `remaining_qty` /
`floor_armed` / etc. columns needed for precedence logic will be absent and
the evaluator will error. Migration framework must either skip gracefully
or halt with a clear message. Simplest: BL-062 migration appends its column
to the same `expected_cols` dict in `scout/db.py:_create_tables` that
BL-061 added `remaining_qty` etc. to — that pattern is already idempotent
and runs both migrations in natural dependency order at startup.

**SQLite gotcha:** `ALTER TABLE ... ADD COLUMN` is **not** idempotent —
re-running on a DB that already has the column raises. The BL-061 pattern
(`scout/db.py:881-892`) uses `PRAGMA table_info(paper_trades)` to check
existing columns and skips the ADD when present. BL-062 follows the same
pattern by simply adding `peak_fade_fired_at` to the `expected_cols` dict:

```python
# scout/db.py, inside _create_tables, extend expected_cols:
expected_cols = {
    # ... existing BL-060 and BL-061 columns ...
    "peak_fade_fired_at": "TEXT",   # BL-062, NULL until fire
}
# existing loop (lines 881-892) does the PRAGMA check + guarded ALTER
```

- Pre-cutover rows: NULL (unset).
- Post-cutover rows: NULL until fire, populated on fire.
- Forward A/B scopes to `opened_at >= cutover_ts` (read from
  `paper_migrations` where `name = 'bl062_peak_fade'`), matching BL-061's
  pattern.

Index added in the **same migration step**, not in `_create_tables` (the
latter is a no-op for existing tables; see `feedback_ddl_before_alter`):

```sql
CREATE INDEX IF NOT EXISTS idx_paper_trades_peak_fade_fired_at
  ON paper_trades(peak_fade_fired_at)
  WHERE peak_fade_fired_at IS NOT NULL;
```

(`CREATE INDEX IF NOT EXISTS` **is** idempotent — only the column add
needs PRAGMA-gating.)

### 2. Cutover timestamp — paper_migrations row

Mirror BL-061's pattern exactly (`scout/db.py:894-905`): append a row to
`paper_migrations` during the migration block.

```python
await conn.execute(
    "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
    "VALUES (?, ?)",
    ("bl062_peak_fade", datetime.now(timezone.utc).isoformat()),
)
```

- `INSERT OR IGNORE` guarantees first-run write only; restarts don't
  overwrite the cutover_ts.
- Written inside the same `_create_tables` invocation as the column ADD,
  so there's no gap between schema availability and cutover recording.
- Evaluator loads `bl062_peak_fade` cutover_ts at startup (mirror
  `_load_bl061_cutover_ts` in `scout/trading/evaluator.py:20`) — add
  `_load_bl062_cutover_ts` and cache on module init.
- Forward metrics and the 30-day review query filter on
  `opened_at >= bl062_cutover_ts`.

### 3. Data-availability precondition

E1 fires only when **both** `cp_6h IS NOT NULL AND cp_24h IS NOT NULL`. No
single-observation firing — that's the guard against E2-style clip behavior.

Tests must cover:
- `cp_6h` present, `cp_24h` NULL → no fire
- `cp_6h` NULL, `cp_24h` present → no fire
- Both present, both below ratio → fire
- Both present, only one below ratio → no fire

### 4. Fire-time semantics

Peak_fade is evaluated **only** on the evaluator pass that records `cp_24h`.
Not a continuous-poll rule. In practice: the evaluator loop writes
`checkpoint_24h_pct` and then runs the peak_fade check on the same pass,
before falling through to the expiry check.

This keeps the rule simple (one evaluation site, not a cross-cutting guard)
and means the fire time is deterministic relative to the 24h checkpoint —
clean signal for the 30-day review.

### 5. Exit precedence ordering + 30-day review

The exit-check function must enforce the order in "Exit precedence" above.
Tests must cover each of these scenarios explicitly:

| scenario | expected exit |
|---|---|
| SL triggered AND peak_fade eligible | SL |
| Leg 1 triggered AND peak_fade eligible | Leg 1 (peak_fade on remaining) |
| Leg 2 triggered AND peak_fade eligible | Leg 2 (peak_fade on remaining) |
| Trail armed + eligible AND peak_fade eligible on same pass | Trail |
| Trail armed but trigger NOT met on this pass AND peak_fade eligible | peak_fade |
| Only peak_fade eligible (trail not armed), `remaining_qty > 0` | peak_fade |
| Peak_fade eligible, `remaining_qty == 0` | no action (already fully closed) |
| Peak_fade NOT eligible, `hold_h >= 24` | expiry |

**30-day calibration review:**
- Checkpoint 30 days after the `bl062_peak_fade` cutover (add date to
  `docs/superpowers/reviews/` alongside BL-061's review).
- Review query (forward cohort only, `opened_at >= bl062_cutover_ts`):
  - `fires`: count of rows with `peak_fade_fired_at IS NOT NULL`.
  - `clip_pct`: fraction of fires where the would-have-been expiry P&L (or
    next checkpoint observation if a later one exists) was higher than the
    peak_fade-realized P&L. A "clip" = we closed a position that would
    have recovered.
  - `avg_delta`: mean (peak_fade_pnl - would_be_pnl_at_expiry) across fires.

**Two-tier auto-revert stop rule:**

| tier | trigger | action |
|---|---|---|
| early warning | `fires >= 10 AND clip_pct > 0.25` | flip `PEAK_FADE_ENABLED=false` + file investigation ticket |
| primary | `fires >= 20 AND clip_pct > 0.15` | flip `PEAK_FADE_ENABLED=false` + file investigation ticket |

Rationale: at retro's ~12% fire rate on 201 expired trades (~25/201),
reaching 20 forward fires takes ~167 forward trades. At typical paper-trade
flow that may exceed 30 days. Early-warning tier fires at 10 fires with
a tighter 25% clip threshold to compensate for smaller sample.

`FIRST_SIGNAL_MIN_SIGNAL_COUNT` has no paired stop rule because the effect
is deterministic (block all `len(signals_fired)<N` admissions) — no
forward clip/delta measurement applicable. Revert via config only.

## Configuration

New `.env` keys (all have safe defaults):

```
FIRST_SIGNAL_MIN_SIGNAL_COUNT=2
PEAK_FADE_ENABLED=true
PEAK_FADE_MIN_PEAK_PCT=10
PEAK_FADE_RETRACE_RATIO=0.7
```

Pydantic `Settings` validation:
- `FIRST_SIGNAL_MIN_SIGNAL_COUNT`: int, >= 1. Tests cover `1` (disabled
  stacking) and `2` (default).
- `PEAK_FADE_MIN_PEAK_PCT`: float, > 0. Rejects negative and zero.
- `PEAK_FADE_RETRACE_RATIO`: float, > 0 and < 1. Rejects out-of-range.

## Dashboard

Bundled in this ticket (small, operator needs it for ongoing visibility
during the 30-day measurement window — the review itself is the SQL at
"30-day calibration review" above; the dashboard is the at-a-glance
eyeball):
- New exit-reason tally: `closed_peak_fade` count on the per-day / rolling
  summary panel where `closed_expired` / `closed_trailing_stop` etc. are
  already displayed.
- No new view, no new component — just one more row in the existing
  exit-reason breakdown.

## Testing Plan

Parametrized tests under `tests/trading/`:

### Admission gate (`test_trade_first_signals_signal_count.py`)
- 1 scoring signal fired, default settings → skip (no open_trade call)
- 2 scoring signals fired → proceed to open_trade
- 3+ scoring signals fired → proceed to open_trade
- `FIRST_SIGNAL_MIN_SIGNAL_COUNT=1` override → 1 signal admits
- `FIRST_SIGNAL_MIN_SIGNAL_COUNT=3` override → 2 signals skip
- Explicit test that the trigger-family string `"first_signal"` is NOT
  counted (pass `signals_fired=["momentum_ratio"]` and confirm
  `len(signals_fired) == 1` at the gate, not `2`)

### Peak-fade fire conditions (`test_peak_fade_exit.py`)
- Both checkpoints present, both < 0.7·peak, peak >= 10 → fire
- Both checkpoints present, both < 0.7·peak, peak < 10 → no fire
- cp_6h present, cp_24h NULL → no fire
- cp_6h NULL, cp_24h present → no fire
- cp_6h < 0.7·peak, cp_24h >= 0.7·peak → no fire
- cp_6h >= 0.7·peak, cp_24h < 0.7·peak → no fire
- `PEAK_FADE_ENABLED=false` → no fire even when conditions met
- Fire-once: row fires on pass N, confirm pass N+1 does not re-fire
  (remaining_qty = 0 after close)

### Exit precedence (`test_exit_precedence.py`)
One test per row of the precedence table above.

### `remaining_qty` handling (`test_peak_fade_remaining_qty.py`)
- Leg 1 filled, `remaining_qty = 0.75` → peak_fade closes 0.75
- Legs 1+2 filled, `remaining_qty = 0.25` → peak_fade closes 0.25
- Legs 1+2 filled, `remaining_qty = 0` → no peak_fade action possible

### Config validation (`test_settings_peak_fade.py`)
- `PEAK_FADE_RETRACE_RATIO=1.0` → validation error
- `PEAK_FADE_RETRACE_RATIO=-0.1` → validation error
- `PEAK_FADE_MIN_PEAK_PCT=0` → validation error
- `FIRST_SIGNAL_MIN_SIGNAL_COUNT=0` → validation error

### Migration (`test_bl062_migration.py`)
- Fresh DB (no paper_trades table yet) → migration creates table with
  `peak_fade_fired_at` column.
- Existing DB without `peak_fade_fired_at` (BL-061 schema) → migration adds
  column, writes `bl062_peak_fade` row to `paper_migrations`.
- Existing DB WITH `peak_fade_fired_at` (re-run) → migration skips ADD
  COLUMN via PRAGMA check, `INSERT OR IGNORE` leaves existing cutover_ts
  intact, no errors.
- Index exists after migration and is on `peak_fade_fired_at`.
- `paper_migrations` has exactly one row with `name = 'bl062_peak_fade'`
  and the expected ISO-format cutover_ts.

### Cutover loader (`test_bl062_cutover_loader.py`)
- `_load_bl062_cutover_ts` returns expected ISO string from seeded row.
- Returns `None` when row missing (graceful degradation, mirrors BL-061).

## Non-Goals

- Retroactive close of open trades. New rules apply to new opens and to
  forward evaluator passes on existing opens. No backfill-close.
- Touching the BL-061 ladder cascade (legs, trail, SL) — peak_fade sits on
  top as an additional exit branch for `remaining_qty`.
- Tightening other signal combos. The stacking gate addresses the
  `first_signal` admission path generically; combos like
  `cg_trending_rank+first_signal` (avg +2.19%) are left to the gate's
  natural filter — they'll pass iff they have 2+ scoring signals.
- Per-signal allowlists / weighting for the admission gate — strictly count.
- E2 / E3 exit variants. Retro rejected E2 (clip); E3 re-scoped as BL-066.
- Score-decay as an exit axis — tracked as BL-067 contingent (viability
  confirmed at `scout/main.py:602` but empirical coverage on the expired
  cohort TBD).

## Operational Notes

- Rollout: merge with `PEAK_FADE_ENABLED=true` and
  `FIRST_SIGNAL_MIN_SIGNAL_COUNT=2` as defaults. No phased flag flip needed
  — the A/B cohort is defined by `opened_at >= bl062_cutover_ts`, not by a
  feature flag, so the forward measurement is clean either way.
- Cutover timestamp: written to `paper_migrations` row (name =
  `bl062_peak_fade`) during migration. First-run only; restarts no-op.
- Revert path: `PEAK_FADE_ENABLED=false` and
  `FIRST_SIGNAL_MIN_SIGNAL_COUNT=1` both revert to pre-BL-062 behavior
  without schema rollback. The `peak_fade_fired_at` column and cutover row
  remain in place for historical analysis.
- **Cutover-row recovery:** if the `bl062_peak_fade` row in
  `paper_migrations` ever needs correction (debugging, recovery), **edit
  the cutover_ts in place** — do not delete the row and rely on
  re-migration. `INSERT OR IGNORE` silently writes a *new, later*
  cutover_ts on next startup if the row is missing, which shifts the A/B
  boundary forward and invalidates any historical comparison against the
  original cutover.
