**New primitives introduced:** `signal_params.drawdown_baseline_at TEXT` column (per-signal rolling-window floor, stamped on operator revival), `Database.revive_signal_with_baseline()` async method (atomic enabled=1 + baseline-stamp + audit), `_migrate_autosuspend_baseline_column` migration in `scout/db.py` (BEGIN EXCLUSIVE wrapped, paper_migrations + schema_version stamped), combined-gate `hard_loss` semantics in `scout/trading/auto_suspend.py:maybe_suspend_signals` (replaces drawdown-only check with `net_pnl <= hard_loss OR (drawdown <= hard_loss AND net_pnl <= 0)`), audit detail string format change (now includes both metrics).

# Auto-Suspend Hard-Loss Rule Fix

## Hermes-first analysis

**Domains checked against the Hermes skill hub at `hermes-agent.nousresearch.com/docs/skills`:**

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Per-signal trading-rule auto-suspension | none found — agent-orchestration / retrieval / web focus, not exchange-side risk-rule machinery | build from scratch (pure internal pipeline logic) |
| Rolling-window drawdown computation | none found | build from scratch (pure scalar math over closed_trades rows) |
| SQLite migration with idempotent ALTER + audit stamp | covered by existing `_migrate_high_peak_fade_columns_and_audit_table` pattern in `scout/db.py:1836` | use existing internal pattern |
| Atomic UPDATE + audit-row helper | covered by existing `_suspend()` helper in `scout/trading/auto_suspend.py:94` | mirror existing internal pattern in revival direction |

**Awesome-hermes-agent ecosystem check:** none relevant — the awesome-hermes-agent index is agent-side capabilities (web search, code exec, browser automation), not exchange-side risk-rule machinery.

**Drift-check (per global CLAUDE.md §7a):**
- `grep -rn "drawdown_baseline" scout/ tests/` → 0 hits
- `grep -rn "revive_signal" scout/ tests/` → 0 hits
- No prior PR mentions `BL-NEW-AUTOSUSPEND` or "rolling baseline"

**Verdict:** pure internal exit-engine / risk-rule logic; no Hermes-skill replacement available; building from scratch is the only path. Drift-check clean.

---

## Goal

Fix the `auto_suspend` hard-loss rule that produced two false positives in the prod data:

| Signal | 30d net | 30d peak-to-trough drawdown | Old rule fired? | Right call? |
|---|---|---|---|---|
| losers_contrarian | **+$635** | -$857 | YES (kill) | **NO** — profitable signal killed for normal volatility |
| gainers_early | +$120 | -$1640 | YES (kill) | NO — barely profitable but not bleeding |
| first_signal | -$109 | -$593 | YES (kill) | YES — net negative, deep drawdown |

The old rule fires on peak-to-trough drawdown alone (`max_drawdown <= -500`), regardless of whether the signal is net profitable in the window. losers_contrarian peaked at +$1,492 (running cumulative net) and pulled back to +$635 — that's a -$857 drawdown but a +$635 *profit*. Killing it was a false positive.

## Policy change

Replace `if max_drawdown <= hard_loss:` with a **combined gate**:

```python
fires_hard_loss = (
    net_pnl <= hard_loss             # pure-loser path (catastrophic net bleed)
    or (
        max_drawdown <= hard_loss
        and net_pnl <= 0             # pump-then-crash path (drew up, fell below zero)
    )
)
```

This catches:
- **Pure losers**: net <= -$500 → fires regardless of trade count (catastrophic bleed)
- **Pump-then-dump**: drew to +$1000, crashed to -$10 → drawdown ≪ -$500 AND net ≤ 0 → fires
- **Profitable volatility**: drew to +$1500, pulled back to +$635 → drawdown ≪ -$500 BUT net > 0 → **does NOT fire** (this is the losers_contrarian case)

The `pnl_threshold` rule (line 226-244) is unchanged — still requires `n >= MIN_TRADES` AND `net_pnl < -200`. That continues to catch slow bleeders.

## Operator-revival baseline

Add `signal_params.drawdown_baseline_at TEXT` column. When set, `_rolling_stats` uses `MAX(last_calibration_at, drawdown_baseline_at, 30d_default)` as the window floor.

Operator revival flips `enabled=1` AND stamps `drawdown_baseline_at = NOW()` so historical drawdown can't carry into the new rolling window. The next auto_suspend tick computes against post-revival data only.

This solves the secondary problem: after the fix ships, manually reviving a previously-killed signal must NOT immediately re-trigger on the same window of pre-kill drawdown.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `scout/db.py` | Modify | Add `_migrate_autosuspend_baseline_column` migration + `revive_signal_with_baseline()` method |
| `scout/trading/auto_suspend.py` | Modify | Combined-gate hard_loss rule + window-floor extended to MAX(last_cal, baseline, 30d) |
| `tests/test_signal_params_auto_suspend.py` | Extend | Add tests for the new combined-gate semantics + baseline behavior |
| `tests/test_db.py` (or `test_signal_params.py`) | Extend | Add migration test |

No changes to: config defaults (`SIGNAL_SUSPEND_HARD_LOSS_USD = -500.0` stays), `pnl_threshold` rule, scheduling.

---

## Task 1: Failing tests for combined-gate semantics

- [ ] **Step 1.1: Profitable signal with deep drawdown is NOT killed**

Append to `tests/test_signal_params_auto_suspend.py`:

```python
async def test_hard_loss_does_not_kill_profitable_signal_with_deep_drawdown(
    tmp_path, settings_factory
):
    """losers_contrarian-style case: signal peaked at +$1500, gave back $850
    to net +$650. Drawdown -$850 (below -$500) but net positive — must NOT fire."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Sequence: 15 wins of +$100 each (net +$1500), then 8 losses of -$110
    # each (running -$880 from peak). Final net = $1500 - $880 = +$620.
    # Drawdown trough = -$880 from peak. n = 23.
    for _ in range(15):
        await _insert_closed_trade(db, signal_type="gainers_early", pnl_usd=100)
    for _ in range(8):
        await _insert_closed_trade(db, signal_type="gainers_early", pnl_usd=-110)
    await db._conn.commit()

    s = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        SIGNAL_SUSPEND_HARD_LOSS_USD=-500.0,
        SIGNAL_SUSPEND_PNL_THRESHOLD_USD=-200.0,
        SIGNAL_SUSPEND_MIN_TRADES=50,  # threshold path blocked by floor
    )
    suspended = await maybe_suspend_signals(db, s, session=None)
    assert suspended == [], (
        f"Profitable signal must not be killed for volatility; got {suspended}"
    )
    cur = await db._conn.execute(
        "SELECT enabled FROM signal_params WHERE signal_type='gainers_early'"
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()
```

- [ ] **Step 1.2: Pure-loss signal (net <= hard_loss) IS killed without trade floor**

```python
async def test_hard_loss_kills_pure_loser_no_min_trades_floor(
    tmp_path, settings_factory
):
    """Old "escape hatch" semantics preserved: 10 losses of -$60 each = net -$600.
    Hard loss -$500. Must fire regardless of MIN_TRADES floor."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    for _ in range(10):
        await _insert_closed_trade(db, signal_type="gainers_early", pnl_usd=-60)
    await db._conn.commit()

    s = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        SIGNAL_SUSPEND_HARD_LOSS_USD=-500.0,
        SIGNAL_SUSPEND_MIN_TRADES=50,
    )
    suspended = await maybe_suspend_signals(db, s, session=None)
    assert any(
        x["signal_type"] == "gainers_early" and x["reason"] == "hard_loss"
        for x in suspended
    )
    await db.close()
```

- [ ] **Step 1.3: Pump-then-crash IS killed (drawdown deep AND net <= 0)**

```python
async def test_hard_loss_kills_pump_then_crash(tmp_path, settings_factory):
    """Drew to +$300, crashed to -$300. Drawdown -$600, net -$300.
    Drawdown <= hard_loss AND net <= 0 → fires (pump-then-dump path)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # 3 wins of +$100 (peak +$300), then 6 losses of -$100 (net -$300, drawdown -$600)
    for _ in range(3):
        await _insert_closed_trade(db, signal_type="gainers_early", pnl_usd=100)
    for _ in range(6):
        await _insert_closed_trade(db, signal_type="gainers_early", pnl_usd=-100)
    await db._conn.commit()

    s = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        SIGNAL_SUSPEND_HARD_LOSS_USD=-500.0,
        SIGNAL_SUSPEND_MIN_TRADES=50,
    )
    suspended = await maybe_suspend_signals(db, s, session=None)
    assert any(
        x["signal_type"] == "gainers_early" and x["reason"] == "hard_loss"
        for x in suspended
    )
    await db.close()
```

- [ ] **Step 1.4: Audit detail string includes both metrics**

```python
async def test_hard_loss_audit_detail_records_both_metrics(
    tmp_path, settings_factory
):
    """The reason string in signal_params_audit must surface both net_pnl and
    max_drawdown so operators can debug false-positive concerns."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    for _ in range(10):
        await _insert_closed_trade(db, signal_type="gainers_early", pnl_usd=-60)
    await db._conn.commit()

    s = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        SIGNAL_SUSPEND_HARD_LOSS_USD=-500.0,
    )
    await maybe_suspend_signals(db, s, session=None)
    cur = await db._conn.execute(
        "SELECT reason FROM signal_params_audit "
        "WHERE signal_type='gainers_early' AND applied_by='auto_suspend'"
    )
    row = await cur.fetchone()
    assert row is not None
    reason = row[0]
    assert "hard_loss" in reason
    assert "net" in reason.lower() or "$-" in reason  # net_pnl shown
    assert "drawdown" in reason.lower()  # max_drawdown shown
    await db.close()
```

- [ ] **Step 1.5: Run** — `uv run pytest tests/test_signal_params_auto_suspend.py -v`. Steps 1.1, 1.4 should FAIL (1.1 because old rule kills, 1.4 because old detail doesn't include "net"). Steps 1.2, 1.3 should already PASS (existing rule catches them, just for different stated reasons).

---

## Task 2: Schema migration — drawdown_baseline_at column

- [ ] **Step 2.1: Failing test for column existence**

Append:

```python
async def test_signal_params_has_drawdown_baseline_at_column(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(signal_params)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "drawdown_baseline_at" in cols
    await db.close()


async def test_drawdown_baseline_at_defaults_null_on_seed(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT signal_type, drawdown_baseline_at FROM signal_params"
    )
    rows = await cur.fetchall()
    assert len(rows) > 0
    for sig, baseline in rows:
        assert baseline is None, (
            f"{sig} should default to NULL; got {baseline!r}"
        )
    await db.close()
```

- [ ] **Step 2.2: Add migration**

In `scout/db.py`, mirror `_migrate_high_peak_fade_columns_and_audit_table` shape. Add new method after the HPF migration:

```python
async def _migrate_autosuspend_baseline_column(self) -> None:
    """BL-NEW-AUTOSUSPEND-FIX: per-signal drawdown rolling-window floor.

    Adds:
      - signal_params.drawdown_baseline_at TEXT (nullable)

    Operator revival stamps this column with NOW() so the auto_suspend
    rolling window doesn't carry historical drawdown across the revival
    boundary. Existing rows default to NULL — no behavior change for
    signals that have never been suspended/revived.

    Wrapped in BEGIN EXCLUSIVE / ROLLBACK + paper_migrations cutover row
    + schema_version stamp, matching the BL-NEW-HPF migration pattern.
    """
    import structlog
    _log = structlog.get_logger()
    if self._conn is None:
        raise RuntimeError("Database not initialized.")
    conn = self._conn
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        await conn.execute("BEGIN EXCLUSIVE")

        # Defensive create — same as HPF migration
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS paper_migrations (
                name TEXT PRIMARY KEY,
                cutover_ts TEXT NOT NULL
            )
            """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version    INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL,
                description TEXT NOT NULL
            )
            """)

        # PRAGMA-guarded ALTER (idempotent)
        cur_pragma = await conn.execute("PRAGMA table_info(signal_params)")
        existing_cols = {row[1] for row in await cur_pragma.fetchall()}
        if "drawdown_baseline_at" not in existing_cols:
            await conn.execute(
                "ALTER TABLE signal_params "
                "ADD COLUMN drawdown_baseline_at TEXT"
            )

        await conn.execute(
            "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
            "VALUES (?, ?)",
            ("bl_autosuspend_baseline_v1", now_iso),
        )
        await conn.execute(
            "INSERT OR IGNORE INTO schema_version "
            "(version, applied_at, description) VALUES (?, ?, ?)",
            (20260506, now_iso, "bl_autosuspend_baseline_v1"),
        )
        await conn.commit()
    except Exception:
        try:
            await conn.execute("ROLLBACK")
        except Exception as rb_err:
            _log.exception("schema_migration_rollback_failed", err=str(rb_err))
        _log.error("SCHEMA_DRIFT_DETECTED", migration="bl_autosuspend_baseline_v1")
        raise

    # Post-assertion
    cur = await conn.execute(
        "SELECT 1 FROM paper_migrations WHERE name = ?",
        ("bl_autosuspend_baseline_v1",),
    )
    if (await cur.fetchone()) is None:
        raise RuntimeError(
            "bl_autosuspend_baseline_v1 cutover row missing after migration"
        )
```

Wire into `Database.initialize()`:

```python
await self._migrate_high_peak_fade_columns_and_audit_table()
await self._migrate_autosuspend_baseline_column()  # NEW
```

- [ ] **Step 2.3: Run** — both Step 2.1 tests now pass.

---

## Task 3: Combined-gate hard_loss rule + extended window floor

- [ ] **Step 3.1: Modify `_active_signal_types`'s caller in `maybe_suspend_signals`**

In `scout/trading/auto_suspend.py`:

1. Extend the window-floor calc to consider `drawdown_baseline_at`:

```python
# Replace lines 174-180:
cur = await conn.execute(
    "SELECT last_calibration_at, drawdown_baseline_at "
    "FROM signal_params WHERE signal_type = ?",
    (signal_type,),
)
row = await cur.fetchone()
last_cal = row[0] if row else None
baseline = row[1] if row else None
# Window floor = MAX(last_cal, baseline, 30d_default)
candidates_iso = [
    iso for iso in (last_cal, baseline, fixed_window_iso) if iso
]
since_iso = max(candidates_iso)  # ISO-8601 lex-sort matches chronological
```

2. Replace the hard-loss check at line 186:

```python
# OLD:
# if max_drawdown <= hard_loss:

# NEW combined gate:
fires_hard_loss = (
    net_pnl <= hard_loss
    or (max_drawdown <= hard_loss and net_pnl <= 0)
)
if fires_hard_loss:
    detail = f"net ${net_pnl:.0f}, drawdown ${max_drawdown:.0f} (n={n})"
    # ... rest unchanged
```

The `detail` string change makes audit rows show both metrics. The `_suspend()` helper writes this into `signal_params_audit.reason` as `f"{reason}: {detail}"` (line 122) — operators see e.g. `"hard_loss: net $-600, drawdown $-650 (n=10)"`.

- [ ] **Step 3.2: Update Telegram message format** — line 200-204:

```python
await alerter.send_telegram_message(
    f"⚠ signal {signal_type} auto-suspended (hard_loss): "
    f"net ${net_pnl:.0f}, drawdown ${max_drawdown:.0f}, n={n}",
    session,
    settings,
)
```

- [ ] **Step 3.3: Update docstring** at top of file:

```python
"""...
Triggers (any one is sufficient, in priority order):

1. ``hard_loss``      — Combined gate: ``net_pnl <= SIGNAL_SUSPEND_HARD_LOSS_USD``
                        OR (``max_drawdown <= hard_loss`` AND ``net_pnl <= 0``).
                        First disjunct catches catastrophic net bleed (no MIN_TRADES
                        floor). Second disjunct catches pump-then-crash (drew up
                        then fell below zero with deep peak-to-trough). Profitable
                        signals with normal volatility (drew up, gave some back,
                        still net positive) do NOT fire.
2. ``pnl_threshold``  — unchanged.

Window: trades closed since MAX(``signal_params.last_calibration_at``,
``signal_params.drawdown_baseline_at``, last 30d) — last_calibration_at protects
against killing a signal for losses under stale params; drawdown_baseline_at
protects against carrying historical drawdown across operator revival.
..."""
```

- [ ] **Step 3.4: Run regressions** — `uv run pytest tests/test_signal_params_auto_suspend.py -v`. All Task 1 tests now pass + all original tests still pass.

---

## Task 4: Revival helper + baseline test

- [ ] **Step 4.1: Failing test**

```python
async def test_revive_signal_with_baseline_stamps_baseline_and_audit(
    tmp_path, settings_factory
):
    """Operator revival path: enabled 0→1, drawdown_baseline_at = NOW(),
    audit row written."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Pre-suspend
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0, suspended_reason='auto_suspend' "
        "WHERE signal_type='gainers_early'"
    )
    await db._conn.commit()

    await db.revive_signal_with_baseline(
        "gainers_early",
        reason="operator: post-fix revival",
        operator="operator",
    )

    cur = await db._conn.execute(
        "SELECT enabled, drawdown_baseline_at "
        "FROM signal_params WHERE signal_type='gainers_early'"
    )
    enabled, baseline = await cur.fetchone()
    assert enabled == 1
    assert baseline is not None
    # Sanity: baseline is recent ISO timestamp
    from datetime import datetime, timezone
    parsed = datetime.fromisoformat(baseline)
    assert (datetime.now(timezone.utc) - parsed).total_seconds() < 5

    cur = await db._conn.execute(
        "SELECT field_name, old_value, new_value, applied_by "
        "FROM signal_params_audit WHERE signal_type='gainers_early' "
        "ORDER BY applied_at DESC LIMIT 1"
    )
    row = await cur.fetchone()
    assert row[0] == "enabled"
    assert row[1] == "0"
    assert row[2] == "1"
    assert row[3] == "operator"
    await db.close()


async def test_baseline_overrides_30d_window_floor(
    tmp_path, settings_factory
):
    """When drawdown_baseline_at is more recent than the 30d default, the
    window starts at the baseline. Pre-baseline drawdown is excluded."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # 10 -$100 closes 25d ago (drawdown -$1000 in pre-baseline window)
    from datetime import datetime, timedelta, timezone
    old_close = (datetime.now(timezone.utc) - timedelta(days=25)).isoformat()
    for i in range(10):
        await db._conn.execute(
            """INSERT INTO paper_trades
               (token_id, symbol, name, chain, signal_type, signal_data,
                entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
                status, exit_price, pnl_usd, pnl_pct, peak_pct,
                opened_at, closed_at)
               VALUES (?, 'TOK', 'T', 'coingecko', 'gainers_early', '{}',
                       1.0, 100.0, 100.0, 20.0, 15.0, 1.2, 0.85,
                       'closed_sl', 0.0, -100.0, -33.0, 5.0, ?, ?)""",
            (f"old-{i}", old_close, old_close),
        )
    # Stamp baseline at NOW (1 second after old closes)
    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "UPDATE signal_params SET drawdown_baseline_at = ? "
        "WHERE signal_type='gainers_early'",
        (now_iso,),
    )
    await db._conn.commit()

    s = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        SIGNAL_SUSPEND_HARD_LOSS_USD=-500.0,
    )
    suspended = await maybe_suspend_signals(db, s, session=None)
    # Window is post-baseline (no rows) → no fire
    assert suspended == []
    await db.close()
```

- [ ] **Step 4.2: Implement helper in `scout/db.py`**

Add as a `Database` method (near other signal_params helpers):

```python
async def revive_signal_with_baseline(
    self,
    signal_type: str,
    *,
    reason: str,
    operator: str = "operator",
) -> None:
    """Atomic operator revival: enabled=1, stamp drawdown_baseline_at=NOW(),
    write audit row.

    Used by operator dashboards / scripts to revive a previously-suspended
    signal without having historical drawdown immediately re-trip the rule.
    The baseline anchors the auto_suspend rolling window to the revival
    instant; pre-revival drawdown is excluded.
    """
    if self._conn is None:
        raise RuntimeError("Database not initialized.")
    conn = self._conn
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        await conn.execute("BEGIN EXCLUSIVE")
        cur = await conn.execute(
            "SELECT enabled FROM signal_params WHERE signal_type = ?",
            (signal_type,),
        )
        row = await cur.fetchone()
        if row is None:
            raise ValueError(f"unknown signal_type: {signal_type}")
        old_enabled = row[0]

        await conn.execute(
            """UPDATE signal_params
               SET enabled = 1,
                   suspended_at = NULL,
                   suspended_reason = NULL,
                   drawdown_baseline_at = ?,
                   updated_at = ?,
                   updated_by = ?
               WHERE signal_type = ?""",
            (now_iso, now_iso, operator, signal_type),
        )
        await conn.execute(
            """INSERT INTO signal_params_audit
               (signal_type, field_name, old_value, new_value,
                reason, applied_by, applied_at)
               VALUES (?, 'enabled', ?, '1', ?, ?, ?)""",
            (signal_type, str(old_enabled), reason, operator, now_iso),
        )
        await conn.commit()
    except Exception:
        try:
            await conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
```

- [ ] **Step 4.3: Run** — Step 4.1 tests pass.

---

## Task 5: Full regression + black

- [ ] **Step 5.1**: `uv run pytest --tb=short -q` — all green, no regressions.
- [ ] **Step 5.2**: `uv run black --check scout/ tests/` — no diffs.

---

## Task 6: PR + 3-vector reviewer dispatch (per CLAUDE.md §8)

- [ ] **Step 6.1**: Push branch + open draft PR.
- [ ] **Step 6.2**: Dispatch 3 parallel reviewers along orthogonal vectors:
  1. **Statistical / policy correctness** — does the new combined gate kill the right signals across edge cases (zero trades, all-wins-with-volatility, pump-then-crash, slow-bleed-below-MIN_TRADES, signals at/around the boundary)? Run a synthetic scoreboard against current production data and compare old-rule vs new-rule outcomes.
  2. **Code structural** — are the migration + combined-gate + revival helper correct, idempotent, race-safe, properly logged? Window-floor `max(iso_strings)` correctness across timezone formats. Conftest fixture interaction. No regressions to existing tests beyond intentional behavior changes.
  3. **Strategy / blast radius** — what happens at deploy? Currently-suspended signals stay suspended (revival is opt-in via the helper). What if a profitable signal that was previously killed *correctly* (genuine pump-then-dump) now slips through under combined gate? Risk surface from the `drawdown_baseline_at` knob in operator hands (mis-stamping baseline = effectively immortalizing a signal).
- [ ] **Step 6.3**: Address all MUST-FIX findings; mark ready; squash-merge.

---

## Task 7: Deploy + verify

- [ ] **Step 7.1**: SSH stop-pull-pycache-start (per `feedback_clear_pycache_on_deploy.md`):

```bash
ssh root@89.167.116.187 'systemctl stop gecko-pipeline && cd /root/gecko-alpha && git pull && find . -name __pycache__ -exec rm -rf {} + ; systemctl start gecko-pipeline' > .ssh_deploy_autosusp.txt 2>&1
```

- [ ] **Step 7.2**: Verify column + cutover row:

```bash
ssh root@89.167.116.187 'sqlite3 /root/gecko-alpha/scout.db "PRAGMA table_info(signal_params)" && echo --- && sqlite3 /root/gecko-alpha/scout.db "SELECT * FROM paper_migrations WHERE name = '\''bl_autosuspend_baseline_v1'\''"' > .ssh_verify_autosusp.txt 2>&1
```

- [ ] **Step 7.3**: Manual smoke — run `maybe_suspend_signals` once + verify no rule changes against current state:

```bash
ssh root@89.167.116.187 'cd /root/gecko-alpha && uv run python -c "
import asyncio
from scout.db import Database
from scout.config import Settings
from scout.trading.auto_suspend import maybe_suspend_signals
async def main():
    db = Database(\"scout.db\")
    await db.initialize()
    s = Settings()
    out = await maybe_suspend_signals(db, s, session=None)
    print(\"suspended:\", out)
    await db.close()
asyncio.run(main())
"' > .ssh_smoke_autosusp.txt 2>&1
```

Expected: `suspended: []` — no currently-enabled signals (chain_completed / narrative_prediction / volume_spike / tg_social) trip the new rule on their current data.

---

## Task 8: Revive losers_contrarian

- [ ] **Step 8.1**: Flip .env gate:

```bash
ssh root@89.167.116.187 'cd /root/gecko-alpha && sed -i "s/PAPER_SIGNAL_LOSERS_CONTRARIAN_ENABLED=false/PAPER_SIGNAL_LOSERS_CONTRARIAN_ENABLED=true/" .env && grep LOSERS_CONTRARIAN .env' > .ssh_env_lc.txt 2>&1
```

- [ ] **Step 8.2**: Call revive_signal_with_baseline via uv inline:

```bash
ssh root@89.167.116.187 'cd /root/gecko-alpha && uv run python -c "
import asyncio
from scout.db import Database
async def main():
    db = Database(\"scout.db\")
    await db.initialize()
    await db.revive_signal_with_baseline(
        \"losers_contrarian\",
        reason=\"operator REVERSAL post-BL-NEW-AUTOSUSPEND-FIX — 30d net +\\$635, killed 2026-05-02 by drawdown-only rule (now combined-gate)\",
    )
    await db.close()
asyncio.run(main())
"' > .ssh_revive_lc.txt 2>&1
```

- [ ] **Step 8.3**: Restart pipeline + verify:

```bash
ssh root@89.167.116.187 'systemctl restart gecko-pipeline && sleep 5 && sqlite3 /root/gecko-alpha/scout.db "SELECT signal_type, enabled, drawdown_baseline_at, suspended_at FROM signal_params WHERE signal_type=\"losers_contrarian\""' > .ssh_verify_lc.txt 2>&1
```

Expected: enabled=1, baseline ≈ now, suspended_at NULL.

---

## Task 9: Revive gainers_early

Same pattern as Task 8 but no `.env` gate to flip (gainers_early has only DB-side enable).

```bash
ssh root@89.167.116.187 'cd /root/gecko-alpha && uv run python -c "
import asyncio
from scout.db import Database
async def main():
    db = Database(\"scout.db\")
    await db.initialize()
    await db.revive_signal_with_baseline(
        \"gainers_early\",
        reason=\"operator REVERSAL post-BL-NEW-AUTOSUSPEND-FIX — 30d net +\\$120, killed 2026-05-04 by drawdown-only rule (now combined-gate)\",
    )
    await db.close()
asyncio.run(main())
"' > .ssh_revive_ge.txt 2>&1
```

---

## Task 10: HPF dry-run activation

- [ ] **Step 10.1**: Opt in both signals for HPF:

```bash
ssh root@89.167.116.187 'sqlite3 /root/gecko-alpha/scout.db "UPDATE signal_params SET high_peak_fade_enabled = 1 WHERE signal_type IN (\"gainers_early\", \"losers_contrarian\")"' > .ssh_hpf_optin.txt 2>&1
```

- [ ] **Step 10.2**: Append HPF master flags to `.env`:

```bash
ssh root@89.167.116.187 'cd /root/gecko-alpha && grep -q PAPER_HIGH_PEAK_FADE .env || cat >> .env << EOF
PAPER_HIGH_PEAK_FADE_ENABLED=True
PAPER_HIGH_PEAK_FADE_DRY_RUN=True
EOF
systemctl restart gecko-pipeline' > .ssh_hpf_flip.txt 2>&1
```

- [ ] **Step 10.3**: Verify config loaded:

```bash
ssh root@89.167.116.187 'journalctl -u gecko-pipeline --since "1 minute ago" | grep -E "high_peak_fade|HighPeakFade|started|listening"' > .ssh_hpf_verify.txt 2>&1
```

---

## Task 11: Memory writes + todo updates

- [ ] **Step 11.1**: Memory file `project_bl_autosuspend_fix_2026_05_06.md` — PR #, deploy time, revival times, baselines stamped, HPF activation state.
- [ ] **Step 11.2**: Update `tasks/todo.md` with new soak windows:
  - 2026-05-13: losers_contrarian revival 7d soak — keep on if net stays positive
  - 2026-05-13: gainers_early revival 7d soak — same gate
  - 2026-05-13: HPF dry-run 7d soak — review `high_peak_fade_audit` rows for would-fire pattern
- [ ] **Step 11.3**: Index entry in `MEMORY.md`.

---

## Done criteria

- All new tests in `tests/test_signal_params_auto_suspend.py` pass; existing tests unchanged.
- Full regression `uv run pytest --tb=short -q` clean.
- `uv run black --check` clean.
- PR merged via squash.
- VPS deploy successful (column present, cutover row stamped).
- Smoke-test maybe_suspend_signals returns [] on current data (no false positives on chain_completed / narrative_prediction / volume_spike / tg_social).
- losers_contrarian revived (enabled=1, baseline stamped, .env flipped); gainers_early revived (enabled=1, baseline stamped).
- HPF dry-run activated (master ON, both signals opted in).
- Memory + todo.md updated.
