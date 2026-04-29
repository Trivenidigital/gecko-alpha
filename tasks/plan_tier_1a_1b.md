# Tier 1a + 1b — Per-Signal Parameters with Auto-Suspension

**Status:** DESIGN v2 — review-fixes applied
**Branch (target):** `feat/signal-params-self-tuning`
**Estimated effort:** 1.5–2 days
**Date:** 2026-04-29
**Revision:** v2 — applied 5 parallel-reviewer findings (architect, db, code-explorer, silent-failure-hunter, adversarial)

---

## Context (why this exists)

Today every signal type shares one global ladder config (`PAPER_LADDER_*`,
`PAPER_SL_PCT`, `PAPER_MAX_DURATION_HOURS`). Signal types have different
shapes (gainers_early peaks in hours, narrative_prediction over days) but we
tune one set of globals.

Today's manual audit-tune-redeploy loop yielded a 9× $/trade improvement.
Tier 1a + 1b removes the need for the operator to run that loop weekly.

This is **not** ML. It's data-driven static rules with self-resetting
parameters, gated behind operator approval. Real ML is gated on ≥1000
trades/signal stable for 30d (we have 131 on the largest-N).

---

## Scope

### In scope (this PR)

1. New `signal_params` table — per-signal-type ladder + gate config.
2. New `signal_params_audit` table — append-only history of changes.
3. Migration — append as last `_migrate_signal_params_schema()` step in `init_db`. Seeds defaults from current Settings on first run, idempotent.
4. Evaluator wiring — read from `signal_params` with Settings fallback, `Database`-wrapper API.
5. Engine.open_trade — stamps trade with per-signal `sl_pct` / `max_duration_hours` (replaces `main.py:970-972` Settings reads).
6. Signal-dispatch kill-switch — `enabled=False` blocks new opens at `scout/trading/signals.py` (matching the existing `PAPER_SIGNAL_*_ENABLED` pattern at `main.py:461` and `narrative/agent.py:135`).
7. Calibration script (`scout/trading/calibrate.py`) — dry-run by default, `--apply` writes recalibrated values within a single transaction, `--since-deploy` mode for first run after strategy changes.
8. Auto-suspension job — runs in-loop on a daily hour-gate (matching `FEEDBACK_COMBO_REFRESH_HOUR` pattern in `_run_feedback_schedulers`), NOT external cron.
9. Dashboard `/api/signal_params` endpoint in `dashboard/api.py` + Health-tab section. Endpoint returns `effective_source: "settings" | "table"` per row and shows a banner when `SIGNAL_PARAMS_ENABLED=False`.
10. Config additions (master kill switch + suspension thresholds + calibration tunables).
11. ~18 tests covering migration, evaluator fallback, calibration heuristics (positive + negative + threshold + idempotency for each row), auto-suspension, disabled-signal block, and `enabled=False` calibration refusal.

### Out of scope (future PRs)

- Tier 2a A/B cohort tuning (Thompson sampling).
- Tier 2b per-curator BL-064 scoring (would require BL-064 dispatcher to change `signal_type` from bare `tg_social` to channel-scoped).
- Tier 3 outcome model / RL.
- Auto-re-enable of suspended signals (intentionally one-way).
- Calibration of `PAPER_LADDER_LEG_2_*` (defer until leg-1 stable).
- Calibration of `LOW_PEAK_THRESHOLD_PCT` (interaction with `TRAIL_PCT_LOW_PEAK` is non-trivial).
- **Calibration of `leg_1_pct`** — defer per adversarial review. Column is in the schema; calibrator does NOT touch it in v1. Halves heuristic complexity.
- ≥14d-suspended escalation alert (operator-vacation hedge) — follow-up PR.

---

## Schema

### `signal_params`

```sql
CREATE TABLE IF NOT EXISTS signal_params (
  signal_type             TEXT PRIMARY KEY,

  -- Ladder + gate parameters
  leg_1_pct               REAL    NOT NULL,   -- column kept; calibrator does NOT touch in v1
  leg_1_qty_frac          REAL    NOT NULL,
  leg_2_pct               REAL    NOT NULL,
  leg_2_qty_frac          REAL    NOT NULL,
  trail_pct               REAL    NOT NULL,
  trail_pct_low_peak      REAL    NOT NULL,
  low_peak_threshold_pct  REAL    NOT NULL,
  sl_pct                  REAL    NOT NULL,
  max_duration_hours      INTEGER NOT NULL,

  -- Lifecycle
  enabled                 INTEGER NOT NULL DEFAULT 1,
  suspended_at            TEXT,
  suspended_reason        TEXT,                 -- 'pnl_threshold' | 'hard_loss' | 'params_broken' | 'operator'

  -- Audit
  updated_at              TEXT    NOT NULL DEFAULT (datetime('now')),
  updated_by              TEXT    NOT NULL,        -- 'seed' | 'calibration' | 'operator' | 'auto_suspend'
  last_calibration_at     TEXT,
  last_calibration_reason TEXT
);
-- No idx_signal_params_enabled — table has ~10 rows; linear scan is faster.
```

### `signal_params_audit` (resolved Q5: separate table, one row per field change)

```sql
CREATE TABLE IF NOT EXISTS signal_params_audit (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_type     TEXT NOT NULL,
  field_name      TEXT NOT NULL,
  old_value       TEXT,
  new_value       TEXT,
  reason          TEXT NOT NULL,
  applied_by      TEXT NOT NULL,                -- 'calibration' | 'operator' | 'auto_suspend' | 'seed'
  applied_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signal_params_audit_signal_at
  ON signal_params_audit(signal_type, applied_at);
```

### DEFAULT_SIGNAL_TYPES (corrected per code-explorer + db reviewer)

```python
DEFAULT_SIGNAL_TYPES = {
    "gainers_early",
    "losers_contrarian",
    "trending_catch",
    "first_signal",
    "narrative_prediction",
    "volume_spike",
    "chain_completed",       # added — confirmed in scout/trading/signals.py:793
    "tg_social",             # was "channel:*" — BL-064 dispatcher uses 'tg_social' (scout/social/telegram/dispatcher.py:202)
    # NOTE: 'moonshot' removed — it is a trail modifier (BL-063), not a signal_type stored in paper_trades.
}
```

### Migration: `_migrate_signal_params_schema`

Appended as the LAST migration in `init_db` (after `_migrate_live_trading_schema`). No interleaving.

```python
async def _migrate_signal_params_schema(conn: aiosqlite.Connection) -> None:
    # Mirror BL-061/62/63/64 pattern. NO explicit BEGIN IMMEDIATE
    # (matches project _txn_lock; per memory feedback_bl064 #56).
    async with conn.execute("BEGIN EXCLUSIVE"):
        try:
            await conn.execute("""CREATE TABLE IF NOT EXISTS signal_params (...);""")
            await conn.execute("""CREATE TABLE IF NOT EXISTS signal_params_audit (...);""")
            await conn.execute("""CREATE INDEX IF NOT EXISTS idx_signal_params_audit_signal_at ...""")

            # Seed defaults (idempotent — INSERT OR IGNORE on PK)
            for signal_type in DEFAULT_SIGNAL_TYPES:
                await conn.execute(
                    "INSERT OR IGNORE INTO signal_params (signal_type, leg_1_pct, ...) VALUES (?, ?, ...)",
                    (signal_type, settings.PAPER_LADDER_LEG_1_PCT, ...),
                )

            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) VALUES (?, ?)",
                ("signal_params_v1", datetime.now(timezone.utc).isoformat()),
            )
            await conn.execute("COMMIT")
        except Exception:
            await conn.execute("ROLLBACK")
            log.error("SCHEMA_MIGRATION_FAILED", migration="signal_params_v1")
            raise

    # Post-assertion
    cur = await conn.execute("SELECT 1 FROM paper_migrations WHERE name='signal_params_v1'")
    assert await cur.fetchone() is not None, "signal_params_v1 cutover row missing"
```

**Orphan handling** (per silent-failure-hunter H5 + adversarial):
On every calibration / auto-suspend run, query `SELECT signal_type FROM signal_params WHERE signal_type NOT IN (DEFAULT_SIGNAL_TYPES)` and emit a structured WARN log. Do NOT delete — operator decides whether to drop manually.

---

## Evaluator wiring

### `scout/trading/params.py` (new)

```python
@dataclass(frozen=True)
class SignalParams:
    leg_1_pct: float
    leg_1_qty_frac: float
    leg_2_pct: float
    leg_2_qty_frac: float
    trail_pct: float
    trail_pct_low_peak: float
    low_peak_threshold_pct: float
    sl_pct: float
    max_duration_hours: int
    enabled: bool
    source: Literal["table", "settings"]   # for dashboard transparency

async def get_params(
    db: Database,                          # Database wrapper (NOT raw aiosqlite.Connection — per architect Issue #3)
    signal_type: str,
    settings: Settings,
) -> SignalParams:
    """
    Return per-signal params.

    Order of precedence:
      1. SIGNAL_PARAMS_ENABLED=False → always Settings (source='settings')
      2. signal_type in DEFAULT_SIGNAL_TYPES AND row exists → table row (source='table')
      3. signal_type in DEFAULT_SIGNAL_TYPES AND row missing → log error, return Settings (source='settings')
      4. signal_type NOT in DEFAULT_SIGNAL_TYPES → raise UnknownSignalType (typo guard, per silent-failure H1)
    """
```

**Cache:** module-level dict with 5-min TTL + version int bumped on every `--apply`. Pipeline reads on miss. Documented limitation: dashboard process has its own cache, may show stale `last_calibration_at` for up to 5 min after operator runs `--apply`. Operator runbook for `--apply` includes `systemctl restart gecko-pipeline gecko-dashboard` (per adversarial review).

### Disabled-signal block point (resolved Q2)

Per code-explorer review: existing kill switches (`PAPER_SIGNAL_LOSERS_CONTRARIAN_ENABLED`, `PAPER_SIGNAL_TRENDING_CATCH_ENABLED`) live at the signal-dispatch boundary in `main.py:461` / `narrative/agent.py:135`, NOT inside `open_trade()`.

The new `params.enabled` check goes in **`scout/trading/signals.py`** dispatcher functions (`signal_dispatch()` or equivalent), as the canonical layer for ALL signal sources. The existing `PAPER_SIGNAL_*_ENABLED` `.env` flags remain as outer short-circuits (avoid the DB round-trip). All three layers coexist deliberately:

1. `.env` `PAPER_SIGNAL_*_ENABLED` flag — coarse, no-DB
2. `signal_params.enabled` flag — fine-grained, DB-backed
3. (Future) per-channel/per-curator flags — Tier 2b

This is documented inline in the dispatcher.

### `engine.open_trade()` change (added per code-explorer)

`scout/main.py:970-972` currently reads `settings.PAPER_SL_PCT` and `settings.PAPER_MAX_DURATION_HOURS` to stamp the values onto the new `paper_trades` row. With per-signal params, these values must come from `get_params()` so the row records the params actually in effect at open time. Change `engine.open_trade()` signature OR have it call `get_params()` itself before the INSERT.

The evaluator then reads `sl_pct` from the `paper_trades` row (already does, line 245 area), so calibration changes only affect NEW trades. Existing open positions trail/SL on their original params. (Aligns with silent-failure H2 — except for `params_broken` suspension, see below.)

### Sweep-to-close on `params_broken` suspension (per silent-failure H2)

When auto-suspend writes `suspended_reason='params_broken'`, the daily job ALSO emits a Telegram + dashboard notification recommending the operator manually close existing opens. We do NOT auto-close (too destructive). v1 ships with `pnl_threshold` and `hard_loss` reasons only; `params_broken` reason is wired but only set manually by operator.

---

## Calibration script (`scout/trading/calibrate.py`)

### CLI

```
uv run python -m scout.trading.calibrate                        # dry-run, prints diff (default)
uv run python -m scout.trading.calibrate --apply                # writes
uv run python -m scout.trading.calibrate --signal gainers_early # one signal
uv run python -m scout.trading.calibrate --window 30            # days (default 30)
uv run python -m scout.trading.calibrate --since-deploy         # window = since this signal's last_calibration_at (or seed)
```

**Refusal modes (per silent-failure + adversarial):**
- If `SIGNAL_PARAMS_ENABLED=False` → refuses with explanation "table values not in effect, calibration would be cosmetic". Operator must `--force` to run.
- If `--apply` and `TELEGRAM_BOT_TOKEN` is placeholder → refuses with "operator visibility broken; fix token or use `--force-no-alert`". (Per silent-failure B1, adversarial #4.)
- If post-cutover (since `last_calibration_at` or seed) trade count < `CALIBRATION_MIN_TRADES` → SKIPS the signal with structured log.
- `narrative_prediction` is in `CALIBRATION_EXCLUDE_SIGNALS` set (per adversarial #3 — token_id divergence makes outcomes unreliable). Operator can override per-run.

### Heuristics (rolling window — `paper_trades` GROUP BY signal_type, NOT combo_performance)

Per code-explorer: `combo_performance` is keyed by `signal_combo` (composite), not bare `signal_type`. Calibration reads from `paper_trades` directly with `GROUP BY signal_type WHERE datetime(closed_at) >= datetime(?)`.

For each signal_type with `n_trades >= MIN_CALIBRATION_TRADES` (default **50**, raised per silent-failure M4 + architect Q7):

| Trigger (in priority order) | Action | Bound | Notes |
|---|---|---|---|
| `expired_pct > 30%` (closed via `closed_expired`) | `trail_pct -= 2` | floor 5% | Per-signal type tightening |
| `win_rate < 40%` AND `avg_loss_pct < −20%` | `sl_pct += 2` | ceiling 30% | SL widening; takes priority over trail |
| ~~avg_winner_peak heuristic~~ | ~~leg_1 tuning~~ | ~~floor 5%~~ | **DROPPED in v1** per adversarial soft #3 |

**Edge cases (per silent-failure H3):**
- All values rounded to 1 decimal place before write (`round(value, 1)`) — prevents float-precision flap.
- If `avg_loss_pct` undefined (no losers) → skip SL rule, log structured reason.
- If `expired_pct == 30.0` exactly → trigger does NOT fire (strict `>`, documented).
- Idempotency: re-running `--apply` on unchanged data writes 0 audit rows (asserted in test #15).

### Output (dry-run)

```
[CALIBRATE] window=30d (since-deploy mode), threshold=50 trades, telegram=ok
[CALIBRATE] excluded signals: narrative_prediction (token_id divergence — see project memory)

gainers_early   (n=131, win=52.6%, expired=8.4%, since=2026-04-28T22:58Z)
  → no change

losers_contrarian   (n=18)
  → SKIPPED (n_trades 18 < min 50)

[CALIBRATE] orphan rows in signal_params: ['old_signal_x'] — NOT calibrated
[CALIBRATE] dry-run complete. Run with --apply to persist (1 alert will fire to telegram).
```

### Atomic apply (per silent-failure B2)

```python
async with db.transaction():   # or BEGIN EXCLUSIVE
    for diff in diffs:
        await conn.execute("UPDATE signal_params SET trail_pct=?, ... , updated_at=?, updated_by='calibration', last_calibration_at=?, last_calibration_reason=? WHERE signal_type=?",
            (diff.new.trail_pct, ..., now_iso, now_iso, diff.reason, diff.signal_type))
        for field, old_v, new_v in diff.field_changes:
            await conn.execute("INSERT INTO signal_params_audit (signal_type, field_name, old_value, new_value, reason, applied_by, applied_at) VALUES (?, ?, ?, ?, ?, 'calibration', ?)",
                (diff.signal_type, field, str(old_v), str(new_v), diff.reason, now_iso))
    # Telegram fires INSIDE transaction — failure rolls back the writes.
    await alerter.send_telegram_message(summary, session, settings)
    await conn.execute("COMMIT")
```

**`updated_at` uses `datetime.now(timezone.utc).isoformat()` from Python — NOT SQL `datetime('now')` default — for write paths** (per db review point 3).

---

## Auto-suspension (Tier 1b)

### Scheduling (per code-explorer — match project pattern)

NOT external cron. Piggybacks on `_run_feedback_schedulers` in `scout/main.py:98` with new hour-gate constant `SUSPENSION_CHECK_HOUR=1` (1am UTC, after midnight summary writes).

```python
# inside _run_feedback_schedulers
if now_local.hour == settings.SUSPENSION_CHECK_HOUR and not _suspension_ran_today:
    await maybe_suspend_signals(db, settings, session)
    _suspension_ran_today = True
```

### Trigger logic

```python
async def maybe_suspend_signals(db, settings, session):
    for signal_type in await active_signal_types(db):
        if signal_type in CALIBRATION_EXCLUDE_SIGNALS:
            continue   # don't auto-suspend signals we don't auto-tune

        # Per adversarial #2: read trades since last_calibration_at to avoid
        # killing a signal for losses incurred under stale params
        since = max(
            await get_last_calibration_at(db, signal_type),
            datetime.utcnow() - timedelta(days=30),
        )
        n, net_pnl, max_drawdown = await rolling_stats(db, signal_type, since)

        # Hard-loss escape hatch (per silent-failure B4)
        if max_drawdown <= settings.SIGNAL_SUSPEND_HARD_LOSS_USD:
            await suspend(db, signal_type, reason='hard_loss',
                          detail=f"max drawdown ${max_drawdown:.0f}")
            continue

        if n < settings.SIGNAL_SUSPEND_MIN_TRADES:
            continue
        if net_pnl >= settings.SIGNAL_SUSPEND_PNL_THRESHOLD_USD:
            continue

        await suspend(db, signal_type, reason='pnl_threshold',
                      detail=f"30d-since-calib net P&L ${net_pnl:.0f}")
```

### One-way switch

The auto-suspension job NEVER sets `enabled=1`. Re-enable requires manual SQL or operator dashboard action (out of scope).

### Operator visibility (PRIMARY = dashboard, SECONDARY = telegram)

Per silent-failure B1 + adversarial #4:
- **Primary:** dashboard "Signal Params" Health-tab row for the suspended signal turns red, plus a "Recent Param Changes" widget showing the latest 10 audit entries. Operator opening dashboard sees suspension immediately. **This is the load-bearing visibility channel.**
- **Secondary:** Telegram alert (additive, may 404).
- A `signal_params_audit` row is written for every suspension with `applied_by='auto_suspend'`.

---

## Dashboard

### `GET /api/signal_params` in `dashboard/api.py` (correct path per code-explorer)

```json
{
  "flag_enabled": false,
  "params": [
    {
      "signal_type": "gainers_early",
      "enabled": true,
      "effective_source": "settings",   // explicit per silent-failure B3
      "leg_1_pct": 10.0, "trail_pct": 20.0, "sl_pct": 25.0, ...,
      "last_calibration_at": "2026-04-28T22:00:00+00:00",
      "last_calibration_reason": null,
      "suspended_at": null,
      "suspended_reason": null,
      "rolling_30d": {"trades": 131, "net_pnl": 188.05, "win_pct": 52.6}
    }
  ]
}
```

**`rolling_30d` SQL must wrap stored `closed_at` with `datetime()`** — per PR #24 / silent-failure M1.

### Health-tab section

- Banner: "⚠️ SIGNAL_PARAMS_ENABLED=False — table values shown but NOT in use" when `flag_enabled=false`.
- Suspended rows highlighted red.
- "Recent Param Changes" widget showing latest 10 `signal_params_audit` rows.

---

## Config

```python
# Tier 1a + 1b master kill switch
SIGNAL_PARAMS_ENABLED: bool = False  # default OFF — first deploy is no-op

# Auto-suspension
SIGNAL_SUSPEND_PNL_THRESHOLD_USD: float = -200.0
SIGNAL_SUSPEND_HARD_LOSS_USD: float = -500.0   # NEW — extreme-loss escape hatch (silent-failure B4)
SIGNAL_SUSPEND_MIN_TRADES: int = 50            # raised from 20 (silent-failure M4 + architect Q7)
SUSPENSION_CHECK_HOUR: int = 1                 # 1am UTC, in-loop scheduler

# Calibration
CALIBRATION_MIN_TRADES: int = 50               # raised from 20
CALIBRATION_WINDOW_DAYS: int = 30
CALIBRATION_STEP_SIZE_PCT: float = 2.0
CALIBRATION_EXCLUDE_SIGNALS: set[str] = {"narrative_prediction"}  # per adversarial #3
```

---

## Tests (~18, raised from 14)

| # | Test | File |
|---|---|---|
| 1 | migration creates both tables + seeds defaults from Settings | `tests/test_signal_params_migration.py` |
| 2 | migration is idempotent (re-run = no-op) | `tests/test_signal_params_migration.py` |
| 3 | migration writes `paper_migrations` cutover row | `tests/test_signal_params_migration.py` |
| 4 | `get_params` returns table row when present | `tests/test_signal_params.py` |
| 5 | `get_params` falls back to Settings on missing-but-known signal_type, logs error | `tests/test_signal_params.py` |
| 6 | `get_params` raises `UnknownSignalType` on unknown signal_type | `tests/test_signal_params.py` |
| 7 | `get_params` returns Settings + source='settings' when `SIGNAL_PARAMS_ENABLED=False` | `tests/test_signal_params.py` |
| 8 | evaluator uses per-signal `trail_pct` for ladder fire | `tests/test_evaluator_signal_params.py` |
| 9 | engine.open_trade stamps row with per-signal `sl_pct` (not global Settings) | `tests/test_engine_signal_params.py` |
| 10 | signal-dispatch blocks when `params.enabled=False` | `tests/test_signals_dispatch.py` |
| 11 | calibrate dry-run produces correct trail-tightening diff (positive case) | `tests/test_calibrate.py` |
| 12 | calibrate does NOT trigger trail-tightening when expired_pct <= 30 (negative case) | `tests/test_calibrate.py` |
| 13 | calibrate triggers SL widening on win_rate<40 AND avg_loss<-20 (positive) | `tests/test_calibrate.py` |
| 14 | calibrate skips SL rule when `avg_loss_pct` undefined (edge case) | `tests/test_calibrate.py` |
| 15 | calibrate `--apply` writes both signal_params and audit row in single txn | `tests/test_calibrate.py` |
| 16 | calibrate `--apply` is idempotent (re-run = 0 audit rows) | `tests/test_calibrate.py` |
| 17 | calibrate skips signals below `MIN_CALIBRATION_TRADES` | `tests/test_calibrate.py` |
| 18 | calibrate respects floor/ceiling bounds | `tests/test_calibrate.py` |
| 19 | calibrate refuses with `SIGNAL_PARAMS_ENABLED=False` (no `--force`) | `tests/test_calibrate.py` |
| 20 | calibrate excludes `narrative_prediction` by default | `tests/test_calibrate.py` |
| 21 | auto-suspend triggers at threshold breach | `tests/test_auto_suspend.py` |
| 22 | auto-suspend respects `MIN_TRADES` floor | `tests/test_auto_suspend.py` |
| 23 | auto-suspend hard-loss escape hatch fires below MIN_TRADES | `tests/test_auto_suspend.py` |
| 24 | auto-suspend reads trades since `last_calibration_at`, not absolute 30d | `tests/test_auto_suspend.py` |
| 25 | dashboard endpoint returns `effective_source`, banner when flag off | `tests/test_dashboard_signal_params.py` |

All tests follow the project's pytest-asyncio + tmp_path DB pattern.

---

## Rollout plan

1. Merge PR with `SIGNAL_PARAMS_ENABLED=False` default. Migration seeds tables. Evaluator + engine still read Settings. **Zero behaviour change.** Soak 24h.
2. Set `SIGNAL_PARAMS_ENABLED=true` in prod `.env`, restart pipeline + dashboard, verify one cycle reads from table (logs show `signal_params_hit`). Verify dashboard banner clears.
3. **Wait for ≥30d post-PR-#59 data** (so 2026-05-28 minimum, per adversarial #1) before first calibration run. OR use `--since-deploy` mode immediately, which reads only post-PR-#59 trades — but acceptable only if `n_trades >= MIN_CALIBRATION_TRADES=50` per signal.
4. Run `uv run python -m scout.trading.calibrate --since-deploy` on VPS. Operator reviews diff. Run with `--apply` if sensible. Restart pipeline + dashboard for cache invalidation.
5. Auto-suspension activates on first daily 1am UTC tick after merge. Verify in logs.
6. Soak 14d. If no spurious suspensions and calibration recommendations look reasonable, declare success.

---

## Acceptance criteria

- [ ] `uv run pytest --tb=short -q` passes (1389 → ~1407)
- [ ] Migration seeds 8 default rows on fresh DB (`gainers_early`, `losers_contrarian`, `trending_catch`, `first_signal`, `narrative_prediction`, `volume_spike`, `chain_completed`, `tg_social`)
- [ ] Migration is no-op on existing DB
- [ ] Migration writes `paper_migrations` cutover row
- [ ] Evaluator behavior unchanged when `SIGNAL_PARAMS_ENABLED=False`
- [ ] `engine.open_trade` stamps trade with per-signal sl_pct when flag on
- [ ] Calibration dry-run prints diff without DB writes
- [ ] Calibration `--apply` writes both tables atomically
- [ ] Calibration refuses on broken Telegram unless `--force-no-alert`
- [ ] Calibration excludes `narrative_prediction` by default
- [ ] Auto-suspend fires via in-loop hour-gate (no external cron)
- [ ] Auto-suspend hard-loss escape hatch works
- [ ] Dashboard shows `effective_source` per row + banner when flag off
- [ ] No regressions in existing paper-trade tests

---

## Risks (and mitigations)

| Risk | Likelihood | Mitigation |
|---|---|---|
| Calibration tunes on stale (pre-PR-#59) data | High → Medium | `--since-deploy` mode + raised `MIN_TRADES=50` + 30d-after-#59 wait |
| Calibrate/auto-suspend coupling races | Medium | Auto-suspend reads since `last_calibration_at` |
| Telegram silent 404 hides operator-critical events | High | Dashboard is primary visibility; calibration refuses `--apply` on broken token; banner on dashboard |
| Auto-suspend kills profitable-but-unlucky signal | Medium | Hard-loss escape hatch only at -$500 cumulative; threshold path at -$200 net+50 trades; one-way is acknowledged cost |
| Suspended signal stays dead during operator vacation | Medium | Deferred to follow-up PR (≥14d-suspended escalation alert) |
| Cache TTL drift between dashboard and pipeline | Low | Documented; restart-both step in apply runbook |
| Migration partial state | Low | `INSERT OR IGNORE` + BEGIN EXCLUSIVE wrapper |
| Renamed/removed signal types orphan rows | Low | Logged WARN on every calibrate/suspend run; operator manually drops |
| Operator forgets to flip flag and table is stale | Low | Banner on dashboard makes it obvious |

---

## Resolved questions (formerly "open")

- **Q1 cache invalidation:** documented limitation, restart pipeline+dashboard on `--apply`.
- **Q2 disabled-signal block point:** `signals.py` dispatch boundary (matches existing `.env`-flag pattern).
- **Q3 channel:* wildcards:** removed entirely. `tg_social` is the seeded row; per-channel deferred to Tier 2b.
- **Q4 heuristic validity:** raised `MIN_TRADES=50`, dropped leg_1 tuning, rounded to 1dp, explicit edge-case skip-and-log.
- **Q5 audit table:** separate `signal_params_audit` table, schema defined above.
- **Q6 closed-loop bias:** acknowledged; mitigated by dashboard visibility + future ≥14d escalation alert.
- **Q7 threshold magic numbers:** `MIN_TRADES=50`, hard-loss escape at `-$500`.

---

## File inventory

**New:**
- `scout/trading/params.py` — `SignalParams` dataclass + `get_params(db: Database, ...)` + cache
- `scout/trading/calibrate.py` — calibration CLI (`__main__` entry)
- `scout/trading/auto_suspend.py` — `maybe_suspend_signals()` called from `_run_feedback_schedulers`
- `tests/test_signal_params.py`
- `tests/test_signal_params_migration.py`
- `tests/test_evaluator_signal_params.py`
- `tests/test_engine_signal_params.py`
- `tests/test_signals_dispatch.py`
- `tests/test_calibrate.py`
- `tests/test_auto_suspend.py`
- `tests/test_dashboard_signal_params.py`

**Modified:**
- `scout/db.py` — add `_migrate_signal_params_schema()`, append to `init_db`
- `scout/trading/evaluator.py` — replace direct `settings.PAPER_LADDER_*` reads with `await get_params(...)` (lines 134, 336, 356-360, 379-400, 423)
- `scout/trading/engine.py` — `open_trade()` calls `get_params()` before INSERT, replaces `main.py:970-972` Settings reads
- `scout/trading/signals.py` — dispatcher checks `params.enabled` before dispatch
- `scout/main.py` — remove `PAPER_SL_PCT`/`PAPER_MAX_DURATION_HOURS` reads at lines 970-972 (now in engine), add `_run_feedback_schedulers` hour-gate for auto-suspend
- `scout/config.py` — add 6 new fields (see Config section)
- `dashboard/api.py` — add `GET /api/signal_params`
- `dashboard/db.py` — helper for joining signal_params + paper_trades rolling stats
- `dashboard/frontend/src/...` — Health tab section + banner

**No changes:**
- `scout/scorer.py`, `scout/aggregator.py`, `scout/gate.py`, `scout/safety.py`,
  `scout/alerter.py` (existing `send_telegram_message` already fits),
  `scout/mirofish/*`, `scout/ingestion/*`,
  `scout/trading/combo_refresh.py` (calibration reads `paper_trades` directly,
  not `combo_performance`).

---

## Reviewer disposition (5 parallel design reviews)

| Reviewer | Verdict | Status |
|---|---|---|
| code-architect | Approved with changes | All 3 blocker issues incorporated |
| db-agent | Approved with changes | All 5 schema/migration changes incorporated |
| code-explorer | (factual corrections) | All 5 inaccuracies corrected; patterns adopted |
| silent-failure-hunter | 4 launch blockers + 5 high | All blockers + highs incorporated |
| adversarial | Ship with revisions | All 5 hard concerns incorporated; ≥14d-suspended alert deferred |

**Plan is now ready for build.**
