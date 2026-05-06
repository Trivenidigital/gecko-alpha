**New primitives introduced:** `PAPER_HIGH_PEAK_FADE_*` config block (5 settings + 3 cross-field validators), `signal_params.high_peak_fade_enabled` column (per-signal opt-in flag), new standalone `if close_reason is None` block in `scout/trading/evaluator.py` between trailing_stop and BL-062 peak_fade, `closed_high_peak_fade` exit-status string, `high_peak_fade_would_fire` log event (dry-run telemetry), `_create_table_high_peak_fade_audit` migration creating `high_peak_fade_audit` table for fired-events trail.

# High-Peak Fade Exit Gate — Implementation Plan

## Hermes-first analysis

**Domains checked against the Hermes skill hub at `hermes-agent.nousresearch.com/docs/skills`:**

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Async paper-trading exit-policy evaluation | none found — Hermes skill catalog focuses on agent-orchestration, retrieval, web/email/calendar primitives, not exchange-side exit logic | build from scratch (pure internal pipeline logic) |
| Peak-tracking / retracement detection | none found | build from scratch (existing `peak_price` / `peak_pct` columns suffice; pure scalar math) |
| Cascade-ordering between exit gates | none found — this is an internal evaluator state-machine concern | build from scratch (extends existing BL-061/062/063/067 cascade) |
| SQLite schema migration / per-signal opt-in column | covered by existing aiosqlite + `Database._migrate_*` pattern in `scout/db.py` | use existing internal pattern |
| Structured audit logging | covered by structlog (already in stack) | use existing internal pattern |

**Awesome-hermes-agent ecosystem check:** no relevant repos; the awesome-hermes-agent index lists agent-side skills (web search, code exec, vision, browser automation, etc.), not exchange-side exit-policy machinery.

**Drift-check (per global CLAUDE.md §7a):** `grep -rn "high_peak_fade" scout/ tests/` → 0 hits; `git log --all --oneline | grep -iE "high.peak|hpf"` → 0 hits. No primary primitive shipped or in-flight.

**Verdict:** pure internal exit-engine logic; no Hermes-skill replacement available; building from scratch is the only path. Drift-check clean — proposal does not duplicate existing work.

---



> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a tighter exit gate that fires when a confirmed runner (peak_pct ≥ 75%) retraces ≥ 15% from peak, defaulting to dry-run mode for the first 7 days to validate against live behavior before flipping live. Defers to BL-067 conviction-lock when armed (skipped on `conviction_locked_at IS NOT NULL`). Per-signal opt-in starting with `gainers_early` only.

**Architecture:** New standalone `if close_reason is None` block in `scout/trading/evaluator.py`, ordered between trailing_stop (#5) and BL-062 peak_fade (#6) — MUST be standalone-`if`, NOT `elif` (see findings_high_peak_giveback.md §13.4 for the structural reason). Per-signal opt-in via new `signal_params.high_peak_fade_enabled INTEGER DEFAULT 0` column. Audit trail via new `high_peak_fade_audit` table that records both real fires and dry-run would-fire events. Master kill-switch + dry-run flag in config block.

**Tech stack:** Python 3.12, aiosqlite, pydantic v2 BaseSettings, pytest-asyncio (auto mode), structlog, black formatting.

**References:**
- Proposal: `tasks/findings_high_peak_giveback.md` §0, §5, §14
- Existing exit ladder: `scout/trading/evaluator.py:417-613`
- Existing config: `scout/config.py:236-313` (PAPER_* / PEAK_FADE_* / PAPER_MOONSHOT_* blocks)
- BL-062 standalone-`if` pattern (mirror this): `scout/trading/evaluator.py:572-589`
- BL-067 conviction-lock interaction: `scout/trading/conviction.py`
- Tier 1a per-signal params: `scout/trading/params.py`
- Test patterns: `tests/test_moonshot_exit.py`, `tests/conftest.py`

**Out of scope (deferred follow-ups):**
- Auto-suspend circuit breaker (R3's "negative cohort-Δ for 2 consecutive weeks → master flip off"). Meaningful only after ≥ 14d live data exists; ship as `plan_high_peak_fade_circuit_breaker.md` after this MVP soaks.
- A/B threshold sweep (peak ≥ 60% vs peak ≥ 75%). Operator-driven knob during soak via config flip + audit-table comparison; no plan-stage code.
- Dashboard surface. Existing `/api/exit_reason_breakdown` will pick up `closed_high_peak_fade` automatically; no new endpoint needed.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `scout/config.py` | Modify | Add 5 settings + 3 validators (PAPER_HIGH_PEAK_FADE_*) |
| `scout/db.py` | Modify | Add `_migrate_high_peak_fade_columns_and_audit_table` migration |
| `scout/trading/evaluator.py` | Modify | Add standalone-`if` gate between trailing_stop and BL-062 peak_fade |
| `scout/trading/params.py` | Modify | Add `high_peak_fade_enabled` to `SignalParams` dataclass + read it from row |
| `tests/test_high_peak_fade.py` | Create | New test file (10+ tests covering gate, ordering, BL-067 guard, opt-in) |
| `tests/test_high_peak_fade_audit.py` | Create | Audit-table writer tests |
| `docs/runbook_high_peak_fade.md` | Create | Operator opt-in + dry-run review + live-flip runbook |

---

## Task 1: Add config block + validators

**Files:**
- Modify: `scout/config.py:309-313` (after existing PEAK_FADE block)
- Test: `tests/test_high_peak_fade.py` (NEW)

- [ ] **Step 1: Write failing test for config defaults**

Create `tests/test_high_peak_fade.py` with this content:

```python
"""High-peak fade gate (BL-NEW-HPF) tests."""
from __future__ import annotations

import pytest

from scout.config import Settings


class TestConfigDefaults:
    def test_master_kill_switch_defaults_off(self):
        s = Settings(_env_file=None)
        assert s.PAPER_HIGH_PEAK_FADE_ENABLED is False

    def test_default_min_peak_pct_is_75(self):
        s = Settings(_env_file=None)
        assert s.PAPER_HIGH_PEAK_FADE_MIN_PEAK_PCT == 75.0

    def test_default_retrace_pct_is_15(self):
        s = Settings(_env_file=None)
        assert s.PAPER_HIGH_PEAK_FADE_RETRACE_PCT == 15.0

    def test_dry_run_defaults_on(self):
        s = Settings(_env_file=None)
        assert s.PAPER_HIGH_PEAK_FADE_DRY_RUN is True

    def test_per_signal_opt_in_defaults_on(self):
        s = Settings(_env_file=None)
        assert s.PAPER_HIGH_PEAK_FADE_PER_SIGNAL_OPT_IN is True


class TestConfigValidators:
    def test_min_peak_pct_must_exceed_moonshot_threshold(self):
        with pytest.raises(ValueError, match="must be > PAPER_MOONSHOT_THRESHOLD_PCT"):
            Settings(
                _env_file=None,
                PAPER_HIGH_PEAK_FADE_MIN_PEAK_PCT=30.0,  # below moonshot 40
            )

    def test_retrace_pct_must_be_in_open_unit_interval(self):
        with pytest.raises(ValueError, match="must be in \\(0, 100\\)"):
            Settings(_env_file=None, PAPER_HIGH_PEAK_FADE_RETRACE_PCT=0.0)
        with pytest.raises(ValueError, match="must be in \\(0, 100\\)"):
            Settings(_env_file=None, PAPER_HIGH_PEAK_FADE_RETRACE_PCT=100.0)

    def test_retrace_pct_must_be_tighter_than_moonshot_trail(self):
        with pytest.raises(ValueError, match="must be < PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT"):
            Settings(
                _env_file=None,
                PAPER_HIGH_PEAK_FADE_RETRACE_PCT=35.0,  # >= moonshot 30
            )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_high_peak_fade.py::TestConfigDefaults -v`
Expected: 5 FAILs with `AttributeError: 'Settings' object has no attribute 'PAPER_HIGH_PEAK_FADE_ENABLED'`

- [ ] **Step 3: Add settings + validators to scout/config.py**

Insert after line 313 of `scout/config.py` (after `PEAK_FADE_RETRACE_RATIO: float = 0.7`):

```python
    # BL-NEW-HPF high-peak fade — single-pass tighter exit on confirmed runners.
    # Fires when peak_pct >= MIN_PEAK_PCT AND current price has retraced
    # >= RETRACE_PCT from peak. Tighter than moonshot trail (30%) because
    # the cohort can afford it: capture > give-back at this peak.
    # See tasks/findings_high_peak_giveback.md §14 for backtest evidence
    # (n=10 cohort, +$696 lift, bootstrap p5 = $35.42, slippage-robust to 500bps).
    PAPER_HIGH_PEAK_FADE_ENABLED: bool = False        # master kill, default off
    PAPER_HIGH_PEAK_FADE_MIN_PEAK_PCT: float = 75.0   # below this, regular trail
    PAPER_HIGH_PEAK_FADE_RETRACE_PCT: float = 15.0    # tighter than moonshot 30%
    PAPER_HIGH_PEAK_FADE_DRY_RUN: bool = True         # log-only initially
    PAPER_HIGH_PEAK_FADE_PER_SIGNAL_OPT_IN: bool = True  # require signal_params.high_peak_fade_enabled=1
```

Then add validators after the existing `PEAK_FADE_RETRACE_RATIO` validator (around line 554):

```python
    @field_validator("PAPER_HIGH_PEAK_FADE_RETRACE_PCT")
    @classmethod
    def _validate_high_peak_fade_retrace_pct(cls, v: float) -> float:
        if not (0 < v < 100):
            raise ValueError(
                f"PAPER_HIGH_PEAK_FADE_RETRACE_PCT must be in (0, 100); got={v}"
            )
        return v

    @model_validator(mode="after")
    def _validate_high_peak_fade_cross_fields(self) -> "Settings":
        # MIN_PEAK_PCT must be > moonshot threshold so the gate only fires
        # in the moonshot regime (peak >= 40%). Below that, the regular
        # adaptive trail (sp.trail_pct_low_peak / sp.trail_pct) handles it.
        if self.PAPER_HIGH_PEAK_FADE_MIN_PEAK_PCT <= self.PAPER_MOONSHOT_THRESHOLD_PCT:
            raise ValueError(
                "PAPER_HIGH_PEAK_FADE_MIN_PEAK_PCT must be > "
                "PAPER_MOONSHOT_THRESHOLD_PCT (gate targets moonshot regime); "
                f"got high_peak={self.PAPER_HIGH_PEAK_FADE_MIN_PEAK_PCT}, "
                f"moonshot={self.PAPER_MOONSHOT_THRESHOLD_PCT}"
            )
        # RETRACE_PCT must be tighter than the moonshot trail, otherwise
        # the gate is a no-op (moonshot trail fires first).
        if self.PAPER_HIGH_PEAK_FADE_RETRACE_PCT >= self.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT:
            raise ValueError(
                "PAPER_HIGH_PEAK_FADE_RETRACE_PCT must be < "
                "PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT (must be tighter than "
                "moonshot trail); "
                f"got retrace={self.PAPER_HIGH_PEAK_FADE_RETRACE_PCT}, "
                f"moonshot_trail={self.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT}"
            )
        return self
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_high_peak_fade.py::TestConfigDefaults tests/test_high_peak_fade.py::TestConfigValidators -v`
Expected: 8 PASS

- [ ] **Step 5: Commit**

```bash
git add scout/config.py tests/test_high_peak_fade.py
git commit -m "feat(high-peak-fade): config block + validators (BL-NEW-HPF)"
```

---

## Task 2: Add per-signal opt-in column migration

**Files:**
- Modify: `scout/db.py` (new migration method, called from `initialize`)
- Test: `tests/test_high_peak_fade.py`

- [ ] **Step 1: Write failing test for migration**

Append to `tests/test_high_peak_fade.py`:

```python
class TestMigration:
    @pytest.mark.asyncio
    async def test_signal_params_has_high_peak_fade_enabled_column(self, tmp_path):
        from scout.db import Database
        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        cur = await db._conn.execute("PRAGMA table_info(signal_params)")
        cols = {row[1] for row in await cur.fetchall()}
        assert "high_peak_fade_enabled" in cols
        await db.close()

    @pytest.mark.asyncio
    async def test_high_peak_fade_audit_table_exists(self, tmp_path):
        from scout.db import Database
        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        cur = await db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='high_peak_fade_audit'"
        )
        row = await cur.fetchone()
        assert row is not None
        await db.close()

    @pytest.mark.asyncio
    async def test_high_peak_fade_audit_table_has_required_columns(self, tmp_path):
        from scout.db import Database
        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        cur = await db._conn.execute("PRAGMA table_info(high_peak_fade_audit)")
        cols = {row[1] for row in await cur.fetchall()}
        # MUST contain: id, trade_id, token_id, signal_type, peak_pct,
        # peak_price, current_price, fired_at, dry_run (1=would_fire, 0=real_fire)
        for required in ("id", "trade_id", "token_id", "signal_type",
                         "peak_pct", "peak_price", "current_price",
                         "fired_at", "dry_run"):
            assert required in cols, f"missing column: {required}"
        await db.close()

    @pytest.mark.asyncio
    async def test_existing_signal_params_rows_default_disabled(self, tmp_path):
        from scout.db import Database
        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        # initialize should populate default rows for known signal types
        # (per existing _populate_default_signal_params); each new row's
        # high_peak_fade_enabled must default to 0
        cur = await db._conn.execute(
            "SELECT signal_type, high_peak_fade_enabled FROM signal_params"
        )
        rows = await cur.fetchall()
        assert len(rows) > 0
        for sig_type, opt_in in rows:
            assert opt_in == 0, f"{sig_type} should default to 0, got {opt_in}"
        await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_high_peak_fade.py::TestMigration -v`
Expected: 4 FAILs (column missing / table missing).

- [ ] **Step 3: Add migration to scout/db.py**

Add a new migration method to `Database`. Locate the existing `_migrate_*` chain in `initialize()` and add this AFTER the last existing migration:

```python
    async def _migrate_high_peak_fade_columns_and_audit_table(self) -> None:
        """BL-NEW-HPF: per-signal opt-in column + fire-events audit table.

        Adds:
          - signal_params.high_peak_fade_enabled INTEGER DEFAULT 0
          - high_peak_fade_audit table (records both real and dry-run fires)

        Idempotent: column-add and table-create are guarded by IF NOT EXISTS
        (column-add via try/except on duplicate-column OperationalError,
        consistent with existing migration pattern).
        """
        try:
            await self._conn.execute(
                "ALTER TABLE signal_params "
                "ADD COLUMN high_peak_fade_enabled INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise

        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS high_peak_fade_audit (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id     INTEGER NOT NULL,
                token_id     TEXT    NOT NULL,
                signal_type  TEXT    NOT NULL,
                peak_pct     REAL    NOT NULL,
                peak_price   REAL    NOT NULL,
                current_price REAL   NOT NULL,
                fired_at     TEXT    NOT NULL,
                dry_run      INTEGER NOT NULL,
                FOREIGN KEY (trade_id) REFERENCES paper_trades(id)
            )
            """
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hpf_audit_trade_id "
            "ON high_peak_fade_audit(trade_id)"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hpf_audit_fired_at "
            "ON high_peak_fade_audit(fired_at)"
        )
        await self._conn.commit()
```

Then in `Database.initialize()`, after the last existing `_migrate_*` call, add:

```python
        await self._migrate_high_peak_fade_columns_and_audit_table()
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_high_peak_fade.py::TestMigration -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add scout/db.py tests/test_high_peak_fade.py
git commit -m "feat(high-peak-fade): per-signal opt-in column + audit table migration"
```

---

## Task 3: Extend SignalParams dataclass to expose opt-in flag

**Files:**
- Modify: `scout/trading/params.py` (add field + read it from row)
- Test: `tests/test_high_peak_fade.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_high_peak_fade.py`:

```python
class TestSignalParamsField:
    @pytest.mark.asyncio
    async def test_signal_params_has_high_peak_fade_enabled_field(self, tmp_path, settings_factory):
        from scout.db import Database
        from scout.trading.params import params_for_signal
        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        settings = settings_factory(SIGNAL_PARAMS_ENABLED=True)
        sp = await params_for_signal(db, "first_signal", settings)
        assert hasattr(sp, "high_peak_fade_enabled")
        assert sp.high_peak_fade_enabled is False  # default 0 == False
        await db.close()

    @pytest.mark.asyncio
    async def test_signal_params_reads_opt_in_from_row(self, tmp_path, settings_factory):
        from scout.db import Database
        from scout.trading.params import params_for_signal, clear_cache_for_tests
        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        await db._conn.execute(
            "UPDATE signal_params SET high_peak_fade_enabled = 1 "
            "WHERE signal_type = 'gainers_early'"
        )
        await db._conn.commit()
        clear_cache_for_tests()
        settings = settings_factory(SIGNAL_PARAMS_ENABLED=True)
        sp = await params_for_signal(db, "gainers_early", settings)
        assert sp.high_peak_fade_enabled is True
        await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_high_peak_fade.py::TestSignalParamsField -v`
Expected: 2 FAILs (`AttributeError: 'SignalParams' object has no attribute 'high_peak_fade_enabled'`).

- [ ] **Step 3: Add field to SignalParams dataclass and reader**

In `scout/trading/params.py`, locate the `@dataclass` decorator on `SignalParams` and add the new field. Also update the SELECT in `_load_from_row` (or equivalent) to include the column. Reference shape:

```python
@dataclass(frozen=True)
class SignalParams:
    signal_type: str
    leg_1_pct: float
    leg_1_qty_frac: float
    # ... (existing fields preserved)
    conviction_lock_enabled: bool
    high_peak_fade_enabled: bool  # BL-NEW-HPF — per-signal opt-in
```

In the row-reader function, add after the `conviction_lock_enabled` mapping:

```python
        high_peak_fade_enabled=bool(row["high_peak_fade_enabled"]),
```

Also update the SELECT statement in the loader to include `high_peak_fade_enabled`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_high_peak_fade.py::TestSignalParamsField -v`
Expected: 2 PASS

Also run regression: `uv run pytest tests/test_params.py -v`
Expected: all existing pass (no regression).

- [ ] **Step 5: Commit**

```bash
git add scout/trading/params.py tests/test_high_peak_fade.py
git commit -m "feat(high-peak-fade): expose per-signal opt-in via SignalParams"
```

---

## Task 4: Implement evaluator gate (dry-run mode only first)

**Files:**
- Modify: `scout/trading/evaluator.py` (insert standalone-`if` block)
- Test: `tests/test_high_peak_fade.py`

- [ ] **Step 1: Write failing test for dry-run firing**

Append to `tests/test_high_peak_fade.py`:

```python
class TestEvaluatorGateDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_does_not_close_trade(
        self, tmp_path, settings_factory, token_factory
    ):
        """In dry-run mode, the gate logs would-fire but does NOT set close_reason."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        # opt in gainers_early
        await db._conn.execute(
            "UPDATE signal_params SET high_peak_fade_enabled = 1 "
            "WHERE signal_type = 'gainers_early'"
        )
        await db._conn.commit()

        trader = PaperTrader()
        # open a trade with a high peak (entry $1, peak $1.80 = +80%)
        trade_id = await trader.execute_buy(
            db=db, token_id="tok1", symbol="TOK", name="Tok",
            chain="solana", signal_type="gainers_early",
            signal_data={}, current_price=1.0, amount_usd=100.0,
            tp_pct=200.0, sl_pct=20.0, slippage_bps=0,
            signal_combo="gainers_early",
        )
        # simulate peak having been recorded at $1.80
        await db._conn.execute(
            "UPDATE paper_trades SET peak_price = 1.80, peak_pct = 80.0, "
            "floor_armed = 1, leg_1_filled_at = ?, remaining_qty = quantity * 0.5 "
            "WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), trade_id),
        )
        # current price at $1.50 (16.7% retrace from peak)
        await db._conn.execute(
            "INSERT INTO price_cache (coin_id, current_price, updated_at) "
            "VALUES (?, ?, ?)",
            ("tok1", 1.50, datetime.now(timezone.utc).isoformat()),
        )
        await db._conn.commit()

        settings = settings_factory(
            PAPER_HIGH_PEAK_FADE_ENABLED=True,
            PAPER_HIGH_PEAK_FADE_DRY_RUN=True,
            SIGNAL_PARAMS_ENABLED=True,
        )
        await evaluate_paper_trades(db, settings)

        # Trade should still be open
        cur = await db._conn.execute(
            "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
        )
        row = await cur.fetchone()
        assert row[0] == "open", f"dry-run should not close; got status={row[0]}"

        # Audit-table should have a dry-run row
        cur = await db._conn.execute(
            "SELECT dry_run, peak_pct, current_price FROM high_peak_fade_audit "
            "WHERE trade_id = ?", (trade_id,)
        )
        audit = await cur.fetchone()
        assert audit is not None, "audit row should exist"
        assert audit[0] == 1, "dry_run flag should be 1"
        assert abs(audit[1] - 80.0) < 0.01
        assert abs(audit[2] - 1.50) < 0.01

        await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_high_peak_fade.py::TestEvaluatorGateDryRun -v`
Expected: FAIL — gate not yet implemented; trade remains open BUT audit row missing.

- [ ] **Step 3: Add evaluator gate (dry-run + live behavior in one block)**

In `scout/trading/evaluator.py`, locate the BL-062 peak_fade block at line 572 (`# BL-062 peak-fade — sustained fade at 6h AND 24h checkpoints.`). Insert this NEW block IMMEDIATELY BEFORE it:

```python
            # BL-NEW-HPF high-peak fade — single-pass tighter exit on
            # confirmed runners (peak_pct >= MIN_PEAK_PCT, retrace >= RETRACE_PCT).
            # MUST be standalone `if close_reason is None`, NOT elif. The
            # trailing_stop branch above enters the elif chain whenever
            # floor_armed AND peak_pct >= leg_1_pct, regardless of whether
            # its inner threshold fires; an elif placement here would be
            # structurally unreachable. See findings_high_peak_giveback.md §13.4.
            #
            # Defer to BL-067 conviction-lock when armed: skipped on
            # conviction_locked_at IS NOT NULL. At stack >= 3, the system
            # has explicitly opted into "let it ride"; honoring that is
            # the contract. See findings_high_peak_giveback.md §7.6.
            if (
                close_reason is None
                and settings.PAPER_HIGH_PEAK_FADE_ENABLED
                and (
                    not settings.PAPER_HIGH_PEAK_FADE_PER_SIGNAL_OPT_IN
                    or sp.high_peak_fade_enabled
                )
                and conviction_locked_at is None
                and peak_pct is not None
                and peak_pct >= settings.PAPER_HIGH_PEAK_FADE_MIN_PEAK_PCT
                and peak_price is not None
                and current_price < peak_price * (
                    1 - settings.PAPER_HIGH_PEAK_FADE_RETRACE_PCT / 100.0
                )
                and remaining_qty is not None
                and remaining_qty > 0
            ):
                fired_at = datetime.now(timezone.utc).isoformat()
                dry_run = settings.PAPER_HIGH_PEAK_FADE_DRY_RUN
                await conn.execute(
                    "INSERT INTO high_peak_fade_audit "
                    "(trade_id, token_id, signal_type, peak_pct, peak_price, "
                    " current_price, fired_at, dry_run) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (trade_id, token_id, signal_type_row,
                     float(peak_pct), float(peak_price), float(current_price),
                     fired_at, 1 if dry_run else 0),
                )
                if dry_run:
                    log.info(
                        "high_peak_fade_would_fire",
                        trade_id=trade_id,
                        token_id=token_id,
                        peak_pct=round(float(peak_pct), 2),
                        current_price=current_price,
                        peak_price=float(peak_price),
                        retrace_pp=round(
                            (1 - current_price / float(peak_price)) * 100.0, 2
                        ),
                    )
                else:
                    close_reason = "high_peak_fade"
                    close_status = "closed_high_peak_fade"
                    log.info(
                        "high_peak_fade_fired",
                        trade_id=trade_id,
                        token_id=token_id,
                        peak_pct=round(float(peak_pct), 2),
                        current_price=current_price,
                        give_back_pp=round(
                            float(peak_pct) - float(change_pct), 2
                        ),
                    )
```

- [ ] **Step 4: Run test to verify pass**

Run: `uv run pytest tests/test_high_peak_fade.py::TestEvaluatorGateDryRun -v`
Expected: PASS

Also run full evaluator regression: `uv run pytest tests/test_moonshot_exit.py tests/test_evaluator_cascade.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add scout/trading/evaluator.py tests/test_high_peak_fade.py
git commit -m "feat(high-peak-fade): evaluator gate with dry-run + live modes"
```

---

## Task 5: BL-067 conviction-lock defer guard test

**Files:**
- Test: `tests/test_high_peak_fade.py` (extend existing)

The guard is already coded in Task 4 (`conviction_locked_at is None` clause). This task adds the test that pins it.

- [ ] **Step 1: Write failing test** — but it should pass already. We pin the contract.

Append to `tests/test_high_peak_fade.py`:

```python
class TestConvictionLockDefer:
    @pytest.mark.asyncio
    async def test_gate_skips_when_conviction_locked(
        self, tmp_path, settings_factory
    ):
        """When conviction_locked_at IS NOT NULL, gate must NOT fire even
        in live mode (DRY_RUN=False)."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        await db._conn.execute(
            "UPDATE signal_params SET high_peak_fade_enabled = 1 "
            "WHERE signal_type = 'gainers_early'"
        )
        await db._conn.commit()

        trader = PaperTrader()
        trade_id = await trader.execute_buy(
            db=db, token_id="tok2", symbol="TOK2", name="Tok2",
            chain="solana", signal_type="gainers_early",
            signal_data={}, current_price=1.0, amount_usd=100.0,
            tp_pct=200.0, sl_pct=20.0, slippage_bps=0,
            signal_combo="gainers_early",
        )
        # high peak + conviction locked
        await db._conn.execute(
            "UPDATE paper_trades SET peak_price = 1.80, peak_pct = 80.0, "
            "floor_armed = 1, leg_1_filled_at = ?, remaining_qty = quantity * 0.5, "
            "conviction_locked_at = ?, conviction_locked_stack = 3 "
            "WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(),
             datetime.now(timezone.utc).isoformat(),
             trade_id),
        )
        await db._conn.execute(
            "INSERT INTO price_cache (coin_id, current_price, updated_at) "
            "VALUES (?, ?, ?)",
            ("tok2", 1.50, datetime.now(timezone.utc).isoformat()),
        )
        await db._conn.commit()

        settings = settings_factory(
            PAPER_HIGH_PEAK_FADE_ENABLED=True,
            PAPER_HIGH_PEAK_FADE_DRY_RUN=False,  # live mode
            SIGNAL_PARAMS_ENABLED=True,
        )
        await evaluate_paper_trades(db, settings)

        # Trade must remain open AND no audit row written.
        cur = await db._conn.execute(
            "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
        )
        assert (await cur.fetchone())[0] == "open"

        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM high_peak_fade_audit WHERE trade_id = ?",
            (trade_id,),
        )
        assert (await cur.fetchone())[0] == 0
        await db.close()
```

- [ ] **Step 2: Run test**

Run: `uv run pytest tests/test_high_peak_fade.py::TestConvictionLockDefer -v`
Expected: PASS (guard already in place from Task 4).

- [ ] **Step 3: Commit**

```bash
git add tests/test_high_peak_fade.py
git commit -m "test(high-peak-fade): pin BL-067 conviction-lock defer guard"
```

---

## Task 6: Per-signal opt-in test

**Files:**
- Test: `tests/test_high_peak_fade.py`

- [ ] **Step 1: Write failing test**

Append:

```python
class TestPerSignalOptIn:
    @pytest.mark.asyncio
    async def test_gate_skips_when_signal_not_opted_in(
        self, tmp_path, settings_factory
    ):
        """When PER_SIGNAL_OPT_IN=True and signal_params.high_peak_fade_enabled=0,
        gate must NOT fire."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        # gainers_early NOT opted in (default 0)
        trader = PaperTrader()
        trade_id = await trader.execute_buy(
            db=db, token_id="tok3", symbol="TOK3", name="Tok3",
            chain="solana", signal_type="gainers_early",
            signal_data={}, current_price=1.0, amount_usd=100.0,
            tp_pct=200.0, sl_pct=20.0, slippage_bps=0,
            signal_combo="gainers_early",
        )
        await db._conn.execute(
            "UPDATE paper_trades SET peak_price = 1.80, peak_pct = 80.0, "
            "floor_armed = 1, leg_1_filled_at = ?, remaining_qty = quantity * 0.5 "
            "WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), trade_id),
        )
        await db._conn.execute(
            "INSERT INTO price_cache (coin_id, current_price, updated_at) "
            "VALUES (?, ?, ?)",
            ("tok3", 1.50, datetime.now(timezone.utc).isoformat()),
        )
        await db._conn.commit()

        settings = settings_factory(
            PAPER_HIGH_PEAK_FADE_ENABLED=True,
            PAPER_HIGH_PEAK_FADE_DRY_RUN=True,
            PAPER_HIGH_PEAK_FADE_PER_SIGNAL_OPT_IN=True,
            SIGNAL_PARAMS_ENABLED=True,
        )
        await evaluate_paper_trades(db, settings)

        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM high_peak_fade_audit WHERE trade_id = ?",
            (trade_id,),
        )
        assert (await cur.fetchone())[0] == 0
        await db.close()
```

- [ ] **Step 2: Run test**

Run: `uv run pytest tests/test_high_peak_fade.py::TestPerSignalOptIn -v`
Expected: PASS (logic already in Task 4).

- [ ] **Step 3: Commit**

```bash
git add tests/test_high_peak_fade.py
git commit -m "test(high-peak-fade): pin per-signal opt-in gating"
```

---

## Task 7: Cascade ordering tests

**Files:**
- Test: `tests/test_high_peak_fade.py`

- [ ] **Step 1: Write failing tests for ordering invariants**

Append:

```python
class TestCascadeOrdering:
    @pytest.mark.asyncio
    async def test_trailing_stop_pre_empts_high_peak_fade(
        self, tmp_path, settings_factory
    ):
        """When current_price is below the moonshot trail threshold, the
        trailing_stop branch fires first; gate should NOT also fire."""
        # Setup: peak 80%, moonshot trail 30% from peak, current at 35% retrace.
        # That triggers trailing_stop. Gate at 15% retrace would also be
        # eligible, but trailing_stop fires first.
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        await db._conn.execute(
            "UPDATE signal_params SET high_peak_fade_enabled = 1 "
            "WHERE signal_type = 'gainers_early'"
        )
        await db._conn.commit()

        trader = PaperTrader()
        trade_id = await trader.execute_buy(
            db=db, token_id="tok4", symbol="TOK4", name="Tok4",
            chain="solana", signal_type="gainers_early",
            signal_data={}, current_price=1.0, amount_usd=100.0,
            tp_pct=200.0, sl_pct=20.0, slippage_bps=0,
            signal_combo="gainers_early",
        )
        # peak $1.80, current $1.17 = 35% retrace from peak (below moonshot 30% trail)
        await db._conn.execute(
            "UPDATE paper_trades SET peak_price = 1.80, peak_pct = 80.0, "
            "floor_armed = 1, leg_1_filled_at = ?, "
            "remaining_qty = quantity * 0.5, moonshot_armed_at = ? "
            "WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(),
             datetime.now(timezone.utc).isoformat(),
             trade_id),
        )
        await db._conn.execute(
            "INSERT INTO price_cache (coin_id, current_price, updated_at) "
            "VALUES (?, ?, ?)",
            ("tok4", 1.17, datetime.now(timezone.utc).isoformat()),
        )
        await db._conn.commit()

        settings = settings_factory(
            PAPER_HIGH_PEAK_FADE_ENABLED=True,
            PAPER_HIGH_PEAK_FADE_DRY_RUN=False,
            PAPER_MOONSHOT_ENABLED=True,
            SIGNAL_PARAMS_ENABLED=True,
        )
        await evaluate_paper_trades(db, settings)

        cur = await db._conn.execute(
            "SELECT status, exit_reason FROM paper_trades WHERE id = ?",
            (trade_id,),
        )
        row = await cur.fetchone()
        # Should close via moonshot trail, NOT high_peak_fade
        assert row[0] in ("closed_trailing_stop", "closed_moonshot_trail")
        assert row[1] in ("trailing_stop",)
        await db.close()

    @pytest.mark.asyncio
    async def test_high_peak_fade_pre_empts_bl062_peak_fade(
        self, tmp_path, settings_factory
    ):
        """When BOTH high_peak_fade and BL-062 peak_fade conditions are met
        on the same pass, high_peak_fade fires FIRST (it's ordered earlier)."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        await db._conn.execute(
            "UPDATE signal_params SET high_peak_fade_enabled = 1 "
            "WHERE signal_type = 'gainers_early'"
        )
        await db._conn.commit()

        trader = PaperTrader()
        trade_id = await trader.execute_buy(
            db=db, token_id="tok5", symbol="TOK5", name="Tok5",
            chain="solana", signal_type="gainers_early",
            signal_data={}, current_price=1.0, amount_usd=100.0,
            tp_pct=200.0, sl_pct=20.0, slippage_bps=0,
            signal_combo="gainers_early",
        )
        # peak 80%, current at 17% retrace = peak * 0.83
        # cp_6h_pct and cp_24h_pct both below peak * 0.7 (BL-062 conditions also met)
        await db._conn.execute(
            "UPDATE paper_trades SET peak_price = 1.80, peak_pct = 80.0, "
            "floor_armed = 1, leg_1_filled_at = ?, "
            "remaining_qty = quantity * 0.5, "
            "checkpoint_6h_pct = 40.0, checkpoint_24h_pct = 40.0 "
            "WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), trade_id),
        )
        await db._conn.execute(
            "INSERT INTO price_cache (coin_id, current_price, updated_at) "
            "VALUES (?, ?, ?)",
            ("tok5", 1.49, datetime.now(timezone.utc).isoformat()),
        )
        await db._conn.commit()

        settings = settings_factory(
            PAPER_HIGH_PEAK_FADE_ENABLED=True,
            PAPER_HIGH_PEAK_FADE_DRY_RUN=False,
            PEAK_FADE_ENABLED=True,
            SIGNAL_PARAMS_ENABLED=True,
        )
        await evaluate_paper_trades(db, settings)

        cur = await db._conn.execute(
            "SELECT status, exit_reason FROM paper_trades WHERE id = ?",
            (trade_id,),
        )
        row = await cur.fetchone()
        assert row[0] == "closed_high_peak_fade"
        assert row[1] == "high_peak_fade"
        await db.close()

    @pytest.mark.asyncio
    async def test_below_75_peak_does_not_fire(
        self, tmp_path, settings_factory
    ):
        """Below MIN_PEAK_PCT, gate stays silent; existing trail handles it."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        await db._conn.execute(
            "UPDATE signal_params SET high_peak_fade_enabled = 1 "
            "WHERE signal_type = 'gainers_early'"
        )
        await db._conn.commit()

        trader = PaperTrader()
        trade_id = await trader.execute_buy(
            db=db, token_id="tok6", symbol="TOK6", name="Tok6",
            chain="solana", signal_type="gainers_early",
            signal_data={}, current_price=1.0, amount_usd=100.0,
            tp_pct=200.0, sl_pct=20.0, slippage_bps=0,
            signal_combo="gainers_early",
        )
        # peak 70% (BELOW threshold 75)
        await db._conn.execute(
            "UPDATE paper_trades SET peak_price = 1.70, peak_pct = 70.0, "
            "floor_armed = 1, leg_1_filled_at = ?, remaining_qty = quantity * 0.5 "
            "WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), trade_id),
        )
        await db._conn.execute(
            "INSERT INTO price_cache (coin_id, current_price, updated_at) "
            "VALUES (?, ?, ?)",
            ("tok6", 1.40, datetime.now(timezone.utc).isoformat()),
        )
        await db._conn.commit()

        settings = settings_factory(
            PAPER_HIGH_PEAK_FADE_ENABLED=True,
            PAPER_HIGH_PEAK_FADE_DRY_RUN=True,
            SIGNAL_PARAMS_ENABLED=True,
        )
        await evaluate_paper_trades(db, settings)

        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM high_peak_fade_audit WHERE trade_id = ?",
            (trade_id,),
        )
        assert (await cur.fetchone())[0] == 0
        await db.close()
```

- [ ] **Step 2: Run cascade tests**

Run: `uv run pytest tests/test_high_peak_fade.py::TestCascadeOrdering -v`
Expected: 3 PASS

- [ ] **Step 3: Run full regression**

Run: `uv run pytest tests/test_high_peak_fade.py tests/test_moonshot_exit.py tests/test_evaluator_cascade.py tests/test_adaptive_trail.py -v`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_high_peak_fade.py
git commit -m "test(high-peak-fade): cascade ordering invariants"
```

---

## Task 8: Master kill-switch off → no fire

**Files:**
- Test: `tests/test_high_peak_fade.py`

- [ ] **Step 1: Write failing test**

Append:

```python
class TestMasterKillSwitch:
    @pytest.mark.asyncio
    async def test_master_disabled_no_fire(
        self, tmp_path, settings_factory
    ):
        """When PAPER_HIGH_PEAK_FADE_ENABLED=False (default), gate is dead
        regardless of all other conditions. Audit table stays empty."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        await db._conn.execute(
            "UPDATE signal_params SET high_peak_fade_enabled = 1 "
            "WHERE signal_type = 'gainers_early'"
        )
        await db._conn.commit()

        trader = PaperTrader()
        trade_id = await trader.execute_buy(
            db=db, token_id="tok7", symbol="TOK7", name="Tok7",
            chain="solana", signal_type="gainers_early",
            signal_data={}, current_price=1.0, amount_usd=100.0,
            tp_pct=200.0, sl_pct=20.0, slippage_bps=0,
            signal_combo="gainers_early",
        )
        await db._conn.execute(
            "UPDATE paper_trades SET peak_price = 1.80, peak_pct = 80.0, "
            "floor_armed = 1, leg_1_filled_at = ?, remaining_qty = quantity * 0.5 "
            "WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), trade_id),
        )
        await db._conn.execute(
            "INSERT INTO price_cache (coin_id, current_price, updated_at) "
            "VALUES (?, ?, ?)",
            ("tok7", 1.50, datetime.now(timezone.utc).isoformat()),
        )
        await db._conn.commit()

        settings = settings_factory(
            PAPER_HIGH_PEAK_FADE_ENABLED=False,  # MASTER OFF
            SIGNAL_PARAMS_ENABLED=True,
        )
        await evaluate_paper_trades(db, settings)

        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM high_peak_fade_audit WHERE trade_id = ?",
            (trade_id,),
        )
        assert (await cur.fetchone())[0] == 0
        await db.close()
```

- [ ] **Step 2: Run**

Run: `uv run pytest tests/test_high_peak_fade.py::TestMasterKillSwitch -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_high_peak_fade.py
git commit -m "test(high-peak-fade): master kill-switch off short-circuits"
```

---

## Task 9: End-to-end smoke + full regression

**Files:**
- Run only — no new code or tests.

- [ ] **Step 1: Run full project test suite**

Run: `uv run pytest --tb=short -q`
Expected: all pass; no new test count regressions, expected 6-7 skips on Windows OpenSSL chain.

- [ ] **Step 2: Run linter / formatter**

Run: `uv run black --check scout/ tests/`
Expected: no diffs.

If formatting fails: `uv run black scout/ tests/` then re-run check.

- [ ] **Step 3: Run validator chain via dry run on a fresh DB**

Run:

```bash
uv run python -c "
import asyncio
from scout.db import Database
async def main():
    db = Database('/tmp/hpf_smoke.db')
    await db.initialize()
    cur = await db._conn.execute('PRAGMA table_info(signal_params)')
    cols = [row[1] for row in await cur.fetchall()]
    assert 'high_peak_fade_enabled' in cols
    cur = await db._conn.execute('PRAGMA table_info(high_peak_fade_audit)')
    audit_cols = [row[1] for row in await cur.fetchall()]
    print('signal_params cols:', cols)
    print('high_peak_fade_audit cols:', audit_cols)
    await db.close()
asyncio.run(main())
"
```

Expected: prints column lists including `high_peak_fade_enabled` and the full audit-table schema.

- [ ] **Step 4: Commit nothing (smoke only)** — proceed to Task 10.

---

## Task 10: Operator runbook

**Files:**
- Create: `docs/runbook_high_peak_fade.md`

- [ ] **Step 1: Write the runbook**

```markdown
# High-Peak Fade Exit Gate — Operator Runbook

## What this gate does

Fires when a trade has reached `peak_pct >= PAPER_HIGH_PEAK_FADE_MIN_PEAK_PCT`
(default 75%) AND price has retraced `>= PAPER_HIGH_PEAK_FADE_RETRACE_PCT`
(default 15%) from peak. Fires AFTER existing trailing_stop, BEFORE BL-062
peak_fade. Defers to BL-067 conviction-lock when armed (skipped on
`conviction_locked_at IS NOT NULL`).

Backtest evidence: `tasks/findings_high_peak_giveback.md` §14.

## Default state on deploy

- `PAPER_HIGH_PEAK_FADE_ENABLED=False` (master off)
- All `signal_params.high_peak_fade_enabled=0` (no signals opted in)

This means: zero behavior change on deploy. Safe.

## Activation sequence

### Phase 1 — dry-run telemetry (7 days)

1. **Opt in `gainers_early`:**
   ```sql
   UPDATE signal_params SET high_peak_fade_enabled = 1
   WHERE signal_type = 'gainers_early';
   ```

2. **Flip master ON in dry-run mode:** edit `/root/gecko-alpha/.env`:
   ```
   PAPER_HIGH_PEAK_FADE_ENABLED=True
   PAPER_HIGH_PEAK_FADE_DRY_RUN=True
   ```

3. **Restart pipeline:** `systemctl restart gecko-pipeline`

4. **Verify:** `journalctl -u gecko-pipeline -f | grep high_peak_fade_would_fire`

   Expect 0-2 events per day at observed fire rate (~1.5/week).

### Phase 2 — review dry-run (after 7 days)

```sql
SELECT
  trade_id, signal_type, peak_pct, peak_price, current_price,
  ROUND((1 - current_price/peak_price)*100, 2) AS retrace_pp,
  fired_at
FROM high_peak_fade_audit
WHERE dry_run = 1
ORDER BY fired_at DESC;
```

Cross-reference against the actual closes for those `trade_id`s in
`paper_trades`:

```sql
SELECT pt.id, pt.exit_reason, pt.pnl_pct, pt.pnl_usd, hpf.fired_at AS would_fire_at, pt.closed_at
FROM paper_trades pt
JOIN high_peak_fade_audit hpf ON pt.id = hpf.trade_id
WHERE pt.status LIKE 'closed_%' AND hpf.dry_run = 1;
```

If gate would have fired EARLIER than actual exit and counter-factual PnL
is positive → flip Phase 3.

### Phase 3 — flip live

```
PAPER_HIGH_PEAK_FADE_DRY_RUN=False
```

Restart pipeline. Watch `high_peak_fade_fired` events.

### Rollback (anytime)

```
PAPER_HIGH_PEAK_FADE_ENABLED=False
```

OR opt-out specific signal:

```sql
UPDATE signal_params SET high_peak_fade_enabled = 0 WHERE signal_type = 'gainers_early';
```

No code rollback required.

## Monitoring queries

**Fire rate by week:**

```sql
SELECT
  strftime('%Y-W%W', fired_at) AS week,
  dry_run,
  COUNT(*) AS n_fires
FROM high_peak_fade_audit
GROUP BY week, dry_run
ORDER BY week DESC;
```

**Per-signal effectiveness (live mode only):**

```sql
SELECT
  pt.signal_type,
  COUNT(*) AS n_fires,
  AVG(pt.pnl_pct) AS avg_pnl_pct,
  SUM(pt.pnl_usd) AS total_pnl_usd
FROM paper_trades pt
JOIN high_peak_fade_audit hpf ON pt.id = hpf.trade_id
WHERE hpf.dry_run = 0
  AND pt.status LIKE 'closed_%'
GROUP BY pt.signal_type;
```

**Conviction-lock defer audit (sanity check the guard works):**

```sql
-- Should always be 0: gate skips locked trades
SELECT COUNT(*)
FROM paper_trades pt
JOIN high_peak_fade_audit hpf ON pt.id = hpf.trade_id
WHERE pt.conviction_locked_at IS NOT NULL;
```

## What this gate does NOT do

- Does not affect trades opened before deploy (those stay on existing exits)
- Does not affect BL-067 conviction-locked trades (deferred by design)
- Does not modify entry pricing or sizing
- Does not handle live-mode (BL-055) execution risk separately — slippage
  modeling is paper-mode 50bps; live transition requires re-validation
  per `findings_high_peak_giveback.md` §14.6

## References

- Proposal: `tasks/findings_high_peak_giveback.md`
- Implementation plan: `tasks/plan_high_peak_fade.md`
- Evaluator gate: `scout/trading/evaluator.py` (search for `BL-NEW-HPF`)
- Config: `scout/config.py` (search for `PAPER_HIGH_PEAK_FADE_`)
```

- [ ] **Step 2: Commit runbook**

```bash
git add docs/runbook_high_peak_fade.md
git commit -m "docs(high-peak-fade): operator runbook for opt-in + dry-run + live flip"
```

---

## Task 11: Open PR + dispatch 3-vector reviewers

Per global CLAUDE.md §8 (Multi-Vector Reviewer Dispatch), this change touches
the exit-engine cascade — operator-flagged-critical class. Run the reviewer
pipeline before merge.

- [ ] **Step 1: Push branch + open draft PR**

```bash
git push -u origin feat/high-peak-fade
gh pr create --draft \
  --title "feat: high-peak fade exit gate (BL-NEW-HPF)" \
  --body "$(cat <<'EOF'
## Summary
- Adds tighter exit gate firing at peak_pct >= 75% AND retrace >= 15% from peak
- Standalone if-block (not elif) between trailing_stop and BL-062 peak_fade
- Defers to BL-067 conviction-lock (skips on conviction_locked_at IS NOT NULL)
- Per-signal opt-in via signal_params.high_peak_fade_enabled
- Dry-run mode default (audit-only, no closes) for first 7-day soak

Backtest: existing-data battery (n=10 cohort) shows +$696 lift,
bootstrap 5th-percentile mean $35.42, slippage-robust to 500bps.
See tasks/findings_high_peak_giveback.md §14.

## Test plan
- [ ] All tests pass: uv run pytest --tb=short -q
- [ ] Manual smoke: dry-run audit table populates on synthetic high-peak trade
- [ ] Master kill-switch off → zero behavior change

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Dispatch reviewers per global CLAUDE.md §8**

Three parallel reviewers along orthogonal vectors:

1. **Statistical / data-soundness** — re-verify §14 battery doesn't break under edge cases (zero-cohort, regime shift)
2. **Code / structural correctness** — verify standalone-`if` placement; cascade ordering invariants; race conditions; BL-067 guard test coverage
3. **Strategy / judgment** — verify per-signal opt-in (gainers_early only at launch); dry-run flow; rollback path

- [ ] **Step 3: Address findings, re-run regression, mark PR ready**

Apply each MUST-FIX in commits; re-run `uv run pytest --tb=short -q`; flip PR from draft to ready.

- [ ] **Step 4: Squash-merge**

After merge approval:

```bash
gh pr merge <PR#> --squash --delete-branch
```

---

## Task 12: Deploy + activate dry-run

- [ ] **Step 1: SSH stop-FIRST sequence** (per memory `feedback_clear_pycache_on_deploy.md`)

```bash
ssh root@89.167.116.187 'systemctl stop gecko-pipeline && cd /root/gecko-alpha && git pull && find . -name __pycache__ -exec rm -rf {} + ; systemctl start gecko-pipeline' > .ssh_deploy_hpf.txt 2>&1
```

Read `.ssh_deploy_hpf.txt` for confirmation.

- [ ] **Step 2: Verify migration ran**

```bash
ssh root@89.167.116.187 'sqlite3 /root/gecko-alpha/scout.db ".schema signal_params" | grep high_peak_fade_enabled' > .ssh_verify_hpf_col.txt 2>&1
```

Expected: column present.

- [ ] **Step 3: Opt in `gainers_early` (per runbook Phase 1)**

```bash
ssh root@89.167.116.187 'sqlite3 /root/gecko-alpha/scout.db "UPDATE signal_params SET high_peak_fade_enabled = 1 WHERE signal_type = '\''gainers_early'\''"' > .ssh_opt_in_hpf.txt 2>&1
```

- [ ] **Step 4: Flip master ON in dry-run mode**

Edit `.env` on VPS:

```bash
ssh root@89.167.116.187 'cd /root/gecko-alpha && grep -q PAPER_HIGH_PEAK_FADE .env || cat >> .env <<EOF
PAPER_HIGH_PEAK_FADE_ENABLED=True
PAPER_HIGH_PEAK_FADE_DRY_RUN=True
EOF
systemctl restart gecko-pipeline' > .ssh_flip_hpf.txt 2>&1
```

- [ ] **Step 5: Watch first 24h for would-fire events**

```bash
ssh root@89.167.116.187 'journalctl -u gecko-pipeline --since "1 hour ago" | grep high_peak_fade' > .ssh_hpf_journal.txt 2>&1
```

If gainers_early is still auto-suspended at deploy time (per memory
`project_session_2026_05_05_high_peak_park.md` §13.2), expect ZERO events
until auto_suspend reverses. Document the wait and proceed to Phase 2 only
when fires accumulate.

---

## Self-review checklist (pre-build sanity)

Before any subagent starts Task 1, the implementer should verify:

- [ ] §14 of `findings_high_peak_giveback.md` still PASSES on a re-run of
      `scripts/backtest_high_peak_existing_data_battery.py --window 30`
      (the proposal could have decayed since the plan was written).
- [ ] `gainers_early.high_peak_fade_enabled` defaults to 0 (matching the
      "no behavior change on deploy" guarantee).
- [ ] BL-067 14d soak end-date (2026-05-18) is far enough out that the
      MVP can ship + observe BEFORE locked-trade exits accumulate, not
      against them. If BL-067 has shipped fixes during the build window,
      re-verify §14.4 cohort split before merging.

If ANY check fails, halt build and re-evaluate.

---

## Done criteria

- All 11 tests in `tests/test_high_peak_fade.py` pass
- Full regression `uv run pytest --tb=short -q` clean
- `uv run black --check scout/ tests/` clean
- PR merged via squash
- VPS deploy successful (column present, master off by default)
- Runbook published at `docs/runbook_high_peak_fade.md`
- Operator informed of opt-in path
