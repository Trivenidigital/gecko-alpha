# BL-063 — Moonshot Exit Upgrade

**Status:** Design (revised after 5-reviewer pass + missed-80 audit)
**Date:** 2026-04-26
**Author:** Claude (with srilu)

## Revision history

- v1 (initial): proposed both (A) chain-lead admission and (B) moonshot exit upgrade
- v2 (this): **Approach A dropped.** Audit of 80 chain-detected tokens that were never paper-traded shows zero >50% movers in that cohort (vs 19 in the also-traded cohort). The "miss" was correct filtering — adding chain_lead admission would only add trades on duds. **Scope reduced to B (moonshot exit upgrade) only.**

## Problem statement

7d net paper P&L is roughly -$32 despite chain detection's 91% hit rate. Audit on the 11 dashboard winners:

| Token | Token peak | Best paper peak captured | Gap |
|---|---|---|---|
| MAGA | +591% | +37% | -554pp |
| BSB | +200% | +52% (long_hold) | -148pp |
| CHIP | +145% | +41% | -104pp |
| OPG | +93% | +54% | -39pp |
| ORCA | +76% | +70% | -6pp |

The BL-061 ladder caps trailing-stop drawdown at ~15-21pp and hard-TP at +20%. On +200% movers this clips us in the first leg of the run. Across last 7d: 14 take-profit closes (avg +42%), 83 trailing-stop closes (avg +14%, max peak 104%), 374 expired closes (avg -2.4%). The trailing-stop tail is where the upside lives but it's getting clipped early.

## Goals

- **Primary:** raise the **tail-capture ratio** (`exit_pct / peak_pct`) on trades that peak past +40% from ~50% baseline to ≥75% on the test set.
- **Secondary:** maintain or improve net 7d paper P&L vs status quo. No round-trip-from-+40-to-loss regressions in backtest.

## Non-goals

- New admission paths (chain_lead dropped — no upside per missed-80 audit).
- Partial-leg exit on moonshot arming (deferred to v2 — keep this PR mechanically minimal).
- Changes to existing signal types' admission, scoring, or default ladder.
- Live trading wire-up (stays in BL-055 scope).

## Approach

When an open paper trade's `peak_pct` crosses `PAPER_MOONSHOT_THRESHOLD_PCT` (default 40):
1. Atomically arm the moonshot (single UPDATE … WHERE moonshot_armed_at IS NULL)
2. Disable the fixed TP exit via a new boolean column `tp_disabled` (NOT by writing `+inf` to `tp_price` — SQLite REAL doesn't roundtrip infinity reliably)
3. Widen trailing-stop drawdown to `PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT` (default 30pp from peak — must be > existing baseline trail, enforced by config validator)
4. Preserve original parameters in `original_tp_pct`, `original_trail_drawdown_pct` for audit/reporting
5. On final exit via the (widened) trailing stop, status becomes `closed_moonshot_trail`

This applies to ALL signal types — chain_lead is gone but moonshot benefits any signal whose trade peaks past the threshold.

## Architecture

```
paper price-tick evaluator (existing, scout/trading/evaluator.py)
  ↓ for each open trade, on tick:
  ↓ update peak_pct if new high
  ↓ NEW: if PAPER_MOONSHOT_ENABLED and peak_pct >= PAPER_MOONSHOT_THRESHOLD_PCT:
        await PaperTrader.arm_moonshot(db, trade_id, peak_pct_now)
            (atomic; no-op if already armed; no-op if disabled)
  ↓ existing exit checks:
        - if not tp_disabled and current_price >= tp_price: close as closed_tp
        - SL check unchanged
        - trailing stop: drawdown threshold = settings.PAPER_TRAIL_DRAWDOWN_PCT
          OR (if moonshot armed) trade.original_trail_drawdown_pct overridden
            → CHANGE: read effective_trail = original_trail_drawdown_pct if armed
                                              else PAPER_TRAIL_DRAWDOWN_PCT
        - on trailing-stop close while armed: status = 'closed_moonshot_trail'
```

`arm_moonshot` mirrors the proven `execute_partial_sell` pattern (`scout/trading/paper.py:194-251`): single conditional UPDATE, rowcount check, structured log on success and on race-lost. No multi-statement transaction.

## Schema migration (`scout/db.py`)

New migration adding to `paper_trades`:
- `moonshot_armed_at TEXT NULL` — arm flag + timestamp; NULL = not armed
- `original_tp_pct REAL NULL` — preserved at arm time
- `original_trail_drawdown_pct REAL NULL` — preserved at arm time
- `tp_disabled INTEGER NOT NULL DEFAULT 0` — checked by exit evaluator before TP comparison

Index `idx_paper_trades_moonshot ON paper_trades(moonshot_armed_at) WHERE moonshot_armed_at IS NOT NULL`. **Index lives in the migration step**, NOT in `_create_tables` (per BL-060 DDL lesson — `CREATE TABLE IF NOT EXISTS` is a no-op for existing tables, so an index next to the column would never be created on prod).

A `paper_migrations` row with `name='bl063_moonshot'` and `cutover_at=now()` is inserted at migration time (matches BL-061/062 pattern). Mid-flight: pre-cutover trades have all four new columns NULL/0; A/B in digest is scoped to `opened_at >= cutover_ts`, NOT row-age (per BL-060 mid-flight-flag-migration lesson).

## Pydantic model extension (`scout/trading/models.py`)

Add to `PaperTrade`:
```python
moonshot_armed_at: datetime | None = None
original_tp_pct: float | None = None
original_trail_drawdown_pct: float | None = None
tp_disabled: bool = False
```

Update the existing `status:` docstring enum to include `closed_moonshot_trail`.

## Configuration (`scout/config.py`)

```python
PAPER_MOONSHOT_ENABLED: bool = False
PAPER_MOONSHOT_THRESHOLD_PCT: float = 40.0
PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT: float = 30.0
```

Pydantic `model_validator(mode="after")` (mirrors PR #49 cross-field validator pattern):
- `PAPER_MOONSHOT_THRESHOLD_PCT > 0`
- `0 < PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT < 100`
- `PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT > PAPER_TRAIL_DRAWDOWN_PCT` — moonshot must be wider, NOT narrower (silent regression risk: a misconfig that tightens the trail at the moonshot threshold would clip even more)

`tests/test_config.py` adds three regression tests covering the validator branches.

## Domain exceptions (`scout/exceptions.py`)

Add `MoonshotArmFailed(DomainError)` raised when the atomic UPDATE returns rowcount=0 unexpectedly (i.e., trade not found, not just already-armed). Logged with `error_id`, never silently swallowed. Already-armed and disabled cases are normal returns, not exceptions.

## Structured logging events

Mirror existing `ladder_leg_fired` / `floor_activated` style:
- `moonshot_armed` — fields: `trade_id`, `peak_pct_at_arm`, `original_tp_pct`, `original_trail_drawdown_pct`, `new_trail_drawdown_pct`
- `moonshot_arm_skipped_already_armed` — fields: `trade_id`
- `moonshot_arm_skipped_disabled` — fields: `trade_id` (when flag is off)
- `moonshot_trail_exit` — fields: `trade_id`, `peak_pct`, `exit_pct`, `give_back_pp`

## Backtest harness (`scripts/backtest_moonshot.py`)

Standalone, no live network calls.

- Reads last 14d paper_trades + price_cache snapshots
- For each closed trade where `peak_pct >= 40`, replay the post-arm tick stream under (status_quo policy, moonshot policy)
- Output JSON: per-trade exit_pct delta, aggregate `tail_capture_ratio`, give-back distribution, round-trip count (trades that armed at +40 then closed below 0)
- **No exact-P&L assertions in tests.** Test harness uses 5 hand-crafted fixture tokens (winner, loser, peak-and-dump, slow-grind, sideways) and asserts STRUCTURAL invariants:
  - `moonshot_policy_exit_pct >= status_quo_exit_pct - PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT` (worst-case bound)
  - On the winner fixture: `moonshot > status_quo`
  - On the peak-and-dump fixture: `status_quo > moonshot` (round-trip case proves the risk is real)
  - Output JSON keys present, deterministic ordering

Slippage: 30bps applied symmetrically. Same constant as live.

## Testing

- `tests/test_moonshot_arm.py`
  - Atomic arm: single UPDATE rowcount=1 path
  - Already-armed: returns False, no double-write, no log spam
  - Disabled flag: returns False without DB write
  - Race: two `asyncio.gather()`-ed `arm_moonshot` calls → exactly one wins, the other returns False with `moonshot_arm_skipped_already_armed` log
  - Pydantic model load: round-trips the new fields including `datetime | None`
- `tests/test_moonshot_exit.py`
  - When armed, trailing stop uses the wider drawdown
  - When armed, fixed TP is not triggered even if `current_price >= tp_price`
  - On trailing exit while armed, status = `closed_moonshot_trail`
  - When NOT armed, all existing exit paths behave unchanged (regression gate)
- `tests/test_db_migration_bl063.py`
  - Migration adds 4 columns + index idempotently
  - Pre-existing rows have moonshot_armed_at=NULL, tp_disabled=0
  - `paper_migrations` row inserted with `name='bl063_moonshot'` and `cutover_at` set
  - Re-running migration is a no-op
- `tests/test_config.py` (extend)
  - Validator rejects threshold ≤ 0
  - Validator rejects trail ∉ (0, 100)
  - Validator rejects moonshot trail ≤ baseline trail (the silent-regression guard)
- `tests/test_backtest_moonshot.py`
  - Structural invariants on fixture cohort (NOT exact P&L)
  - Deterministic JSON keys, slippage application monotonicity
- All existing `tests/test_paper_*.py` must pass unchanged. Settings factory keeps `PAPER_MOONSHOT_ENABLED=False` by default so existing test ladder paths are not perturbed.

## Rollout

1. Merge with `PAPER_MOONSHOT_ENABLED=False`. Schema migrated on prod via existing migration pipeline.
2. Run backtest on prod 14d closed-trade cohort, document fit/test split results in PR comment.
3. `PAPER_MOONSHOT_ENABLED=True` on prod (paper-only impact).
4. Soak 7 days. Monitor: count of `moonshot_armed` events, distribution of `give_back_pp`, count of `closed_moonshot_trail` vs status quo trailing-stop closes, net 7d P&L vs prior 7d.
5. **Decision gate (HARD):** prod-soak data, NOT backtest, is ground truth. Keep on if median tail capture on armed trades > 50% AND zero net-P&L regression vs prior 7d on the full paper portfolio. Otherwise revert via flag.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Moonshot round-trips a +40% peak that dumps -50% (give-back tax > capture gain) | Backtest measures round-trip rate on real cohort; validator forces trail > baseline (no silent tightening); flag-off rollback is instant |
| Cohort sparsity in test set undermines A/B significance | Validator + race tests are unit-deterministic; live A/B is "all-on / all-off" via flag with longer soak window if needed |
| `tp_price = +inf` SQLite footgun (per silent-failure review) | Replaced with explicit `tp_disabled INTEGER` column |
| moonshot_upgrade non-atomic causes half-mutated state | Single conditional UPDATE with rowcount check, mirrors `execute_partial_sell` |
| Race between two evaluator ticks | `WHERE moonshot_armed_at IS NULL` guard + rowcount check + dedicated race test |
| Pre-cutover rows mixed into A/B comparison | A/B scoped to `opened_at >= cutover_ts` via `paper_migrations` row (BL-060 lesson) |
| Validator misses `MOONSHOT_TRAIL ≤ BASELINE_TRAIL` silent regression | Cross-field `model_validator` rejects this combination |
| Existing ladder/peak-fade tests regress because moonshot path activates at peak +40 in their fixtures | Settings factory defaults `PAPER_MOONSHOT_ENABLED=False`; explicit per-test override required |
| Tail-capture KPI gameable | Decision gate also requires "no net P&L regression" — both KPIs must clear |

## Resolved open questions (from v1)

1. **Moonshot trigger shape (fixed peak_pct vs momentum-aware)?** → fixed `peak_pct >= threshold` for v1. Simpler, fewer knobs, easier to validate. Revisit if backtest suggests momentum-aware would prevent peak-and-dump round-trips.
2. **chain_lead admission?** → DROPPED. Missed-80 audit shows no upside in the cohort.
3. **Schema: separate columns vs `signal_data` JSON pack?** → Separate columns. Matches BL-054/053/060 precedent; queryable; SQLite doesn't index JSON by default (perf trap).
4. **Partial-exit at moonshot arm time?** → Deferred to v2. v1 keeps mechanics minimal — no new `leg=3` extension to `execute_partial_sell`. If v1 backtest shows partial would help, ship it as a follow-on.
