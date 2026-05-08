**New primitives introduced:** `Settings.LIVE_TRADING_ENABLED: bool = False` master kill (`scout/config.py`) — Layer 1 of the 4-layer kill stack, gates ALL live execution at engine entry. `Settings.LIVE_MAX_TRADE_NOTIONAL_USD: float = 100.0` per-trade hard cap. `Settings.LIVE_MAX_OPEN_EXPOSURE_USD: float = 1000.0` aggregate hard cap across all venues. Three Pydantic field validators for non-negative + sane-bound checks. New `signal_params.live_eligible INTEGER NOT NULL DEFAULT 0` column (migration `bl_live_eligible_v1`, schema_version 20260508) — per-signal opt-in to live execution; default fail-closed. New `cross_venue_exposure` SQL view aggregating open `live_trades.size_usd` + open chain-native `paper_trades.amount_usd` (DEX side returns empty in M1 — wired but inert until M2). Updated `Gate 7` (`scout/live/gates.py:209-231`) to query the view instead of `shadow_trades`. New module `scout/live/balance_gate.py` — implements the BL-055 balance-availability check that was missing as of 2026-05-03. New idempotency contract on `scout/live/binance_adapter.py`: every order submission carries `client_order_id = f"gecko-{paper_trade_id}-{intent_uuid}"`; pre-retry dedup query against open orders + recent fills. New `live_orders_skipped_*` metric family (`master_kill`, `mode_paper`, `signal_disabled`, `exposure_cap`, `kill_switch`, `notional_cap`). New Telegram startup notification when `LIVE_TRADING_ENABLED=True` (`scout/main.py` startup hook).

# Live Trading Milestone 1 — CEX-live (BL-055) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver milestone 1 of the live-trading hybrid architecture — Binance USDT-margined perp execution via BL-055 — to a state where Phase 0 (operator-in-loop with `LIVE_MODE=shadow` then `live`) can begin. Implements the 4-layer kill-switch stack, hard capital caps, per-signal opt-in, balance-availability gate, idempotency contract, cross-venue exposure tracking, and observability needed before any real money lands. **Does NOT enable live trading itself** — operator flips `LIVE_TRADING_ENABLED=True` in `.env` post-deploy after reviewing prerequisites.

**Architecture:** Implements the design at `tasks/design_live_trading_hybrid.md` (committed `263c419`). All work lives in `scout/live/` (existing module), `scout/config.py` (Settings), `scout/db.py` (migration), and `scout/main.py` (startup hook). Mirrors the BL-NEW-AUTOSUSPEND-FIX migration pattern (`BEGIN EXCLUSIVE` + `paper_migrations` cutover + `schema_version` stamp). DEX side (Minara via `MinaraAdapter`) is OUT of scope — this is M1 only; M2 unblocks separately per design's serial-milestone discipline.

**Tech Stack:** Python 3.12, aiosqlite, pydantic v2 BaseSettings + field_validator, pytest-asyncio (auto mode), structlog, black formatting. No new external dependencies.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `scout/config.py` | Modify | Add 3 new Settings fields + 3 field validators near the existing `LIVE_MODE` block (line ~330) |
| `scout/db.py` | Modify | Add `_migrate_live_eligible_column` migration (after `_migrate_moonshot_opt_out_column`); add `_create_cross_venue_exposure_view` called from `_create_tables` |
| `scout/trading/params.py` | Modify | Add `live_eligible: bool = False` to `SignalParams` dataclass; extend `get_params` SELECT to read row[13] |
| `scout/live/balance_gate.py` | **Create** | New module — implements pre-trade balance check against Binance account balance |
| `scout/live/binance_adapter.py` | Modify | Add `client_order_id` generation + dedup query; persist `client_order_id` to `live_trades` row |
| `scout/live/gates.py` | Modify | Update Gate 7 to query `cross_venue_exposure` view; add Gate 8 (master kill) and Gate 9 (notional cap) |
| `scout/live/engine.py` | Modify | Wire master-kill check + notional-cap check + per-signal opt-in check before adapter dispatch |
| `scout/live/metrics.py` | Modify | Document new `live_orders_skipped_*` counter names (existing `inc()` is generic — no code change needed, but plan adds the increments at the gate sites) |
| `scout/main.py` | Modify | Add startup hook: when `LIVE_TRADING_ENABLED=True`, send Telegram notification once at pipeline boot |
| `tests/test_live_master_kill.py` | **Create** | New test file — master kill, capital caps, per-signal opt-in |
| `tests/test_live_balance_gate.py` | **Create** | New test file — balance_gate behavior |
| `tests/test_live_idempotency.py` | **Create** | New test file — client_order_id contract |
| `tests/test_live_cross_venue_exposure.py` | **Create** | New test file — view + Gate 7 |
| `tests/test_live_eligible_migration.py` | **Create** | New test file — migration + dataclass field |

---

## Task 0: Setup — branch + prerequisite verification

**Files:** none modified — verification only.

- [ ] **Step 1: Create feature branch**

```bash
git checkout master
git pull
git checkout -b feat/live-trading-m1-cex
```

- [ ] **Step 2: Verify prerequisite state (paste each command, capture output)**

```bash
ls scout/live/balance_gate.py 2>&1   # expect: No such file or directory (will be created in Task 8)
grep -n "live_eligible" scout/db.py scout/trading/params.py 2>&1 | head -5  # expect: empty (will be added in Task 2)
grep -n "client_order_id\|clientOrderId" scout/live/binance_adapter.py 2>&1  # expect: empty (will be added in Task 9)
grep -n "shadow_trades WHERE status" scout/live/gates.py 2>&1  # expect: line 210 (Gate 7 will be updated in Task 6)
```

- [ ] **Step 3: Commit nothing** — Task 0 is verification only.

---

## Task 1: Settings fields + validators (master kill + capital caps)

**Files:**
- Modify: `scout/config.py` (insert after the existing `LIVE_MODE` line ~332)
- Test: `tests/test_live_master_kill.py` (NEW)

- [ ] **Step 1: Write failing test for Settings defaults**

Create `tests/test_live_master_kill.py`:

```python
"""BL-NEW-LIVE-HYBRID milestone 1: master kill + capital caps tests."""
from __future__ import annotations

import pytest

from scout.config import Settings


class TestLiveTradingSettings:
    def test_master_kill_defaults_off(self):
        s = Settings(_env_file=None)
        assert s.LIVE_TRADING_ENABLED is False

    def test_max_trade_notional_default(self):
        s = Settings(_env_file=None)
        assert s.LIVE_MAX_TRADE_NOTIONAL_USD == 100.0

    def test_max_open_exposure_default(self):
        s = Settings(_env_file=None)
        assert s.LIVE_MAX_OPEN_EXPOSURE_USD == 1000.0


class TestLiveTradingValidators:
    def test_max_trade_notional_must_be_positive(self):
        with pytest.raises(ValueError, match="must be > 0"):
            Settings(_env_file=None, LIVE_MAX_TRADE_NOTIONAL_USD=0.0)
        with pytest.raises(ValueError, match="must be > 0"):
            Settings(_env_file=None, LIVE_MAX_TRADE_NOTIONAL_USD=-50.0)

    def test_max_open_exposure_must_be_positive(self):
        with pytest.raises(ValueError, match="must be > 0"):
            Settings(_env_file=None, LIVE_MAX_OPEN_EXPOSURE_USD=0.0)

    def test_max_open_exposure_must_exceed_single_trade(self):
        with pytest.raises(ValueError, match=">= LIVE_MAX_TRADE_NOTIONAL_USD"):
            Settings(
                _env_file=None,
                LIVE_MAX_TRADE_NOTIONAL_USD=500.0,
                LIVE_MAX_OPEN_EXPOSURE_USD=400.0,  # below single-trade cap
            )
```

- [ ] **Step 2: Run test — expect 6 FAILs**

```bash
uv run pytest tests/test_live_master_kill.py::TestLiveTradingSettings tests/test_live_master_kill.py::TestLiveTradingValidators -v
```

Expected: 6 FAIL with `AttributeError: 'Settings' object has no attribute 'LIVE_TRADING_ENABLED'`.

- [ ] **Step 3: Add Settings fields**

In `scout/config.py`, locate the `LIVE_MODE` line (~332). Insert immediately after:

```python
    # -------- BL-NEW-LIVE-HYBRID milestone 1 (design 2026-05-06) --------
    # Layer 1 of the 4-layer kill-switch stack. Master kill — when False,
    # all live execution short-circuits at engine entry regardless of
    # LIVE_MODE / per-signal opt-in / kill_switch state. Operator-controlled
    # via .env edit + pipeline restart.
    LIVE_TRADING_ENABLED: bool = False

    # Hard per-trade notional cap. Engine refuses to execute any single
    # intent above this. Sized to match paper-trade default ($100). Operator
    # increases per .env edit only after Phase 1 demonstrates fills behave
    # as expected.
    LIVE_MAX_TRADE_NOTIONAL_USD: float = 100.0

    # Hard ceiling on the SUM of open live-position notionals across ALL
    # venues. Engine refuses to open a new position if post-trade aggregate
    # exposure would exceed this. Sized 10x single-trade default — allows
    # ~10 concurrent positions.
    LIVE_MAX_OPEN_EXPOSURE_USD: float = 1000.0
```

- [ ] **Step 4: Add field validators**

In `scout/config.py`, find the existing validator block (search for `_validate_revival_min_soak_days` — most recent validator). Add after it:

```python
    @field_validator("LIVE_MAX_TRADE_NOTIONAL_USD")
    @classmethod
    def _validate_live_max_trade_notional_usd(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(
                f"LIVE_MAX_TRADE_NOTIONAL_USD must be > 0; got={v}"
            )
        return v

    @field_validator("LIVE_MAX_OPEN_EXPOSURE_USD")
    @classmethod
    def _validate_live_max_open_exposure_usd(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(
                f"LIVE_MAX_OPEN_EXPOSURE_USD must be > 0; got={v}"
            )
        return v

    @model_validator(mode="after")
    def _validate_live_caps_relation(self) -> "Settings":
        if self.LIVE_MAX_OPEN_EXPOSURE_USD < self.LIVE_MAX_TRADE_NOTIONAL_USD:
            raise ValueError(
                "LIVE_MAX_OPEN_EXPOSURE_USD must be >= "
                "LIVE_MAX_TRADE_NOTIONAL_USD (aggregate cap can't be "
                "smaller than per-trade cap); "
                f"got open={self.LIVE_MAX_OPEN_EXPOSURE_USD}, "
                f"trade={self.LIVE_MAX_TRADE_NOTIONAL_USD}"
            )
        return self
```

- [ ] **Step 5: Run tests — expect 6 PASS**

```bash
uv run pytest tests/test_live_master_kill.py::TestLiveTradingSettings tests/test_live_master_kill.py::TestLiveTradingValidators -v
```

Expected: 6 PASS.

- [ ] **Step 6: Commit**

```bash
git add scout/config.py tests/test_live_master_kill.py
git commit -m "feat(live-m1): LIVE_TRADING_ENABLED + capital caps Settings (BL-NEW-LIVE-HYBRID)"
```

---

## Task 2: signal_params.live_eligible column migration

**Files:**
- Modify: `scout/db.py` (add `_migrate_live_eligible_column` after `_migrate_moonshot_opt_out_column`)
- Modify: `scout/trading/params.py` (add field to dataclass + extend SELECT)
- Test: `tests/test_live_eligible_migration.py` (NEW)

- [ ] **Step 1: Write failing migration test**

Create `tests/test_live_eligible_migration.py`:

```python
"""BL-NEW-LIVE-HYBRID milestone 1: live_eligible column migration."""
from __future__ import annotations

import pytest

from scout.db import Database


@pytest.mark.asyncio
async def test_signal_params_has_live_eligible_column(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(signal_params)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "live_eligible" in cols
    await db.close()


@pytest.mark.asyncio
async def test_live_eligible_defaults_to_0_for_seed_signals(tmp_path):
    """Default fail-closed: every existing row gets live_eligible=0.
    Operator must explicitly UPDATE to opt-in per signal."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT signal_type, live_eligible FROM signal_params"
    )
    rows = await cur.fetchall()
    assert len(rows) > 0
    for sig, opt in rows:
        assert opt == 0, f"{sig} should default to 0; got {opt}"
    await db.close()


@pytest.mark.asyncio
async def test_migration_idempotent_on_rerun(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._migrate_live_eligible_column()  # second run
    cur = await db._conn.execute("PRAGMA table_info(signal_params)")
    cols = [row[1] for row in await cur.fetchall()]
    assert cols.count("live_eligible") == 1
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_migrations WHERE name = ?",
        ("bl_live_eligible_v1",),
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()
```

- [ ] **Step 2: Run test — expect 3 FAILs**

```bash
uv run pytest tests/test_live_eligible_migration.py -v
```

Expected: 3 FAILs (`live_eligible` not in cols; method doesn't exist).

- [ ] **Step 3: Add migration method to scout/db.py**

After `_migrate_moonshot_opt_out_column` (around line ~2025 — search for it), add:

```python
    async def _migrate_live_eligible_column(self) -> None:
        """BL-NEW-LIVE-HYBRID M1: per-signal live-execution opt-in flag.

        Adds:
          - signal_params.live_eligible INTEGER NOT NULL DEFAULT 0

        Default 0 = fail-closed. Operator opts a signal in via
        UPDATE signal_params SET live_eligible=1 WHERE signal_type='X'.
        Layer 3 of the 4-layer kill-switch stack.

        Wrapped in BEGIN EXCLUSIVE / ROLLBACK + paper_migrations cutover
        + schema_version 20260508 stamp. PRAGMA-guarded ALTER (idempotent).
        Mirrors the bl_moonshot_opt_out_v1 / bl_autosuspend_baseline_v1
        / bl_hpf_v1 patterns. Post-assertion INSIDE try block.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            await conn.execute("BEGIN EXCLUSIVE")

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

            cur_pragma = await conn.execute("PRAGMA table_info(signal_params)")
            existing_cols = {row[1] for row in await cur_pragma.fetchall()}
            if "live_eligible" not in existing_cols:
                await conn.execute(
                    "ALTER TABLE signal_params "
                    "ADD COLUMN live_eligible INTEGER NOT NULL DEFAULT 0"
                )

            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                ("bl_live_eligible_v1", now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260508, now_iso, "bl_live_eligible_v1"),
            )

            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?",
                ("bl_live_eligible_v1",),
            )
            if (await cur.fetchone()) is None:
                raise RuntimeError(
                    "bl_live_eligible_v1 cutover row missing after migration"
                )

            await conn.commit()
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            _log.error("SCHEMA_DRIFT_DETECTED", migration="bl_live_eligible_v1")
            raise
```

**Schema version reservation:** verified against `scout/db.py` at plan-write time (2026-05-06). Current values in tree:
- 20260429 (`tier_1a_signal_params_v1`)
- 20260505 (`bl_hpf_v1_high_peak_fade`)
- 20260506 (`bl_autosuspend_baseline_v1`)
- 20260507 (`bl_moonshot_opt_out_v1`)

This plan reserves:
- **20260508 → `bl_live_eligible_v1`** (Task 2)
- **20260509 → `bl_live_client_order_id_v1`** (Task 8)

Use these exact numbers in BOTH the migration body and the test. Update the new-primitives-marker at the top of this plan if you need to change them (but you shouldn't need to).

- [ ] **Step 4: Wire the migration into `Database.initialize()`**

Find the `initialize()` method (around scout/db.py:80). After `await self._migrate_moonshot_opt_out_column()`, add:

```python
        await self._migrate_live_eligible_column()
```

- [ ] **Step 5: Add field to SignalParams dataclass**

In `scout/trading/params.py`, find the `moonshot_enabled: bool = True` line (the current last field of SignalParams). Add after it:

```python
    # BL-NEW-LIVE-HYBRID M1 — Layer 3 per-signal opt-in for live execution.
    # Default False fail-closed: a signal must be explicitly opted in via
    # UPDATE signal_params SET live_eligible=1 WHERE signal_type='X'.
    live_eligible: bool = False
```

- [ ] **Step 6: Extend the SELECT + constructor**

In `scout/trading/params.py`, find the SELECT around line 167 (search for `moonshot_enabled` in the SELECT). Add `live_eligible` as the 14th column:

```python
    cursor = await db._conn.execute(
        """SELECT leg_1_pct, leg_1_qty_frac, leg_2_pct, leg_2_qty_frac,
                  trail_pct, trail_pct_low_peak, low_peak_threshold_pct,
                  sl_pct, max_duration_hours, enabled,
                  conviction_lock_enabled,
                  high_peak_fade_enabled,
                  moonshot_enabled,
                  live_eligible
           FROM signal_params WHERE signal_type = ?""",
        (signal_type,),
    )
```

In the constructor (around line 200), add `live_eligible=bool(row[13])` after `moonshot_enabled=bool(row[12])`.

- [ ] **Step 7: Run tests — expect 3 PASS**

```bash
uv run pytest tests/test_live_eligible_migration.py -v
```

- [ ] **Step 8: Commit**

```bash
git add scout/db.py scout/trading/params.py tests/test_live_eligible_migration.py
git commit -m "feat(live-m1): signal_params.live_eligible per-signal opt-in column"
```

---

## Task 3: Master kill switch enforcement in engine

**Files:**
- Modify: `scout/live/engine.py` (add master-kill check at LiveEngine entry)
- Test: `tests/test_live_master_kill.py` (extend existing)

- [ ] **Step 1: Append failing test**

Append to `tests/test_live_master_kill.py`:

```python
class TestMasterKillEnforcement:
    @pytest.mark.asyncio
    async def test_engine_skips_when_master_kill_off(
        self, tmp_path, settings_factory
    ):
        """When LIVE_TRADING_ENABLED=False, LiveEngine.execute_intent
        short-circuits without calling the adapter, increments
        live_orders_skipped_master_kill, and leaves no live_trades row."""
        from scout.db import Database
        from scout.live.engine import LiveEngine
        from scout.live.types import _PaperTradeLike  # may need adjustment

        db = Database(tmp_path / "t.db")
        await db.initialize()
        s = settings_factory(LIVE_TRADING_ENABLED=False, LIVE_MODE="live")

        # Build a stub paper_trade — minimal fields per Protocol
        class StubPaperTrade:
            id = 1
            coin_id = "bitcoin"
            symbol = "BTC"
            signal_type = "first_signal"
            amount_usd = 50.0
            chain = "coingecko"

        # Stub adapter that fails the test if called
        class StubAdapter:
            venue_name = "binance"
            calls = []
            async def fetch_depth(self, pair):
                self.calls.append(("fetch_depth", pair))
                raise AssertionError("adapter must not be called")
            async def submit_order(self, *args, **kwargs):
                raise AssertionError("adapter must not be called")

        adapter = StubAdapter()
        engine = LiveEngine(db=db, adapter=adapter, settings=s)
        await engine.execute_intent(StubPaperTrade())

        # Assert: no live_trades row, no adapter calls
        cur = await db._conn.execute("SELECT COUNT(*) FROM live_trades")
        assert (await cur.fetchone())[0] == 0
        assert adapter.calls == []

        # Assert: skip metric incremented
        from scout.live.metrics import inc as _  # confirm module importable
        cur = await db._conn.execute(
            "SELECT value FROM live_metrics_daily "
            "WHERE metric = 'live_orders_skipped_master_kill'"
        )
        row = await cur.fetchone()
        assert row is not None and row[0] >= 1
        await db.close()
```

- [ ] **Step 2: Run test — expect FAIL**

```bash
uv run pytest tests/test_live_master_kill.py::TestMasterKillEnforcement -v
```

Expected: FAIL — adapter is called OR `LIVE_TRADING_ENABLED` is not respected.

- [ ] **Step 3: Add master-kill check at engine entry**

In `scout/live/engine.py`, find the `execute_intent` (or whatever the engine entry point is — likely an async method on `LiveEngine` that takes the paper_trade). At the very top of the method body, BEFORE any other gate or DB operation, add:

```python
        # Layer 1: master kill switch (BL-NEW-LIVE-HYBRID M1).
        # Operator-controlled via .env LIVE_TRADING_ENABLED. When False,
        # all live execution short-circuits regardless of LIVE_MODE,
        # per-signal opt-in, or kill_switch state.
        if not self._settings.LIVE_TRADING_ENABLED:
            from scout.live.metrics import inc
            log.info(
                "live_execution_skipped_master_kill",
                trade_id=paper_trade.id,
                coin_id=paper_trade.coin_id,
                signal_type=paper_trade.signal_type,
            )
            await inc(self._db, "live_orders_skipped_master_kill")
            return
```

(The exact local variable names — `paper_trade`, `self._settings`, `self._db`, `log` — should match what `engine.py` already uses. Inspect the file before editing.)

- [ ] **Step 4: Run test — expect PASS**

```bash
uv run pytest tests/test_live_master_kill.py::TestMasterKillEnforcement -v
```

- [ ] **Step 5: Commit**

```bash
git add scout/live/engine.py tests/test_live_master_kill.py
git commit -m "feat(live-m1): master kill enforcement at engine entry"
```

---

## Task 4: Per-trade notional cap enforcement

**Files:**
- Modify: `scout/live/gates.py` (add Gate 8: notional cap)
- Test: `tests/test_live_master_kill.py` (extend)

- [ ] **Step 1: Append failing test**

Append to `tests/test_live_master_kill.py`:

```python
class TestNotionalCapEnforcement:
    @pytest.mark.asyncio
    async def test_intent_above_cap_rejected(self, tmp_path, settings_factory):
        """A paper_trade with amount_usd > LIVE_MAX_TRADE_NOTIONAL_USD must
        be rejected with reject_reason='notional_cap_exceeded' and a
        live_trades row written (status='rejected')."""
        from scout.db import Database
        from scout.live.engine import LiveEngine

        db = Database(tmp_path / "t.db")
        await db.initialize()
        s = settings_factory(
            LIVE_TRADING_ENABLED=True,
            LIVE_MAX_TRADE_NOTIONAL_USD=100.0,
            LIVE_MODE="shadow",  # writes to shadow_trades, not live_trades
        )
        # Need to opt-in the signal too (Layer 3) — covered in Task 7.
        await db._conn.execute(
            "UPDATE signal_params SET live_eligible=1 WHERE signal_type='first_signal'"
        )
        await db._conn.commit()

        class StubPaperTrade:
            id = 1
            coin_id = "bitcoin"
            symbol = "BTC"
            signal_type = "first_signal"
            amount_usd = 250.0  # above cap of 100
            chain = "coingecko"

        class StubAdapter:
            venue_name = "binance"
            async def fetch_depth(self, pair): raise AssertionError("must not be called")
            async def submit_order(self, *args, **kwargs): raise AssertionError("must not be called")

        engine = LiveEngine(db=db, adapter=StubAdapter(), settings=s)
        await engine.execute_intent(StubPaperTrade())

        cur = await db._conn.execute(
            "SELECT status, reject_reason FROM shadow_trades "
            "WHERE paper_trade_id = 1"
        )
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == "rejected"
        assert row[1] == "notional_cap_exceeded"
        await db.close()
```

- [ ] **Step 2: Run test — expect FAIL**

```bash
uv run pytest tests/test_live_master_kill.py::TestNotionalCapEnforcement -v
```

- [ ] **Step 3: Add notional-cap reject_reason to schema CHECK constraint**

In `scout/db.py`, find the `live_trades` CHECK constraint at line ~1500-1504 (search for `'venue_unavailable'`). Add `'notional_cap_exceeded'` to BOTH `live_trades` and `shadow_trades` reject_reason CHECK lists:

```python
                reject_reason       TEXT CHECK (reject_reason IS NULL OR reject_reason IN (
                    'no_venue','insufficient_depth','slippage_exceeds_cap','insufficient_balance',
                    'daily_cap_hit','kill_switch','exposure_cap','override_disabled',
                    'venue_unavailable','notional_cap_exceeded'
                )),
```

This requires a migration if the table already exists in prod with the old constraint. Add a new migration `_migrate_reject_reason_notional_cap`:

```python
    async def _migrate_reject_reason_notional_cap(self) -> None:
        """BL-NEW-LIVE-HYBRID M1: extend live_trades + shadow_trades
        reject_reason CHECK to include 'notional_cap_exceeded'.

        SQLite CHECK constraints are immutable on existing tables; the
        only way to alter them is rebuild-via-rename. Do that here only
        if the constraint is the OLD shape.
        """
        # ... full implementation per BL-061-style table-rename pattern ...
```

**Pragmatic shortcut:** if no live_trades or shadow_trades rows exist in any prod-relevant DB yet (M1 hasn't gone live), just bump the CHECK in the original CREATE TABLE statement. The migration only matters once tables have rows. Confirm with operator: are there any historical shadow_trades rows? If yes, write the migration; if no, skip it (Task 4 step 3 becomes a one-line CREATE TABLE update + paper_migrations stamp).

- [ ] **Step 4: Implement Gate 8 in scout/live/gates.py**

Find the existing `evaluate()` method in `gates.py`. After Gate 7 (exposure cap, line ~207), add Gate 8:

```python
        # Gate 8: per-trade notional cap (BL-NEW-LIVE-HYBRID M1).
        # Layer 4-equivalent — refuses any intent whose notional exceeds
        # LIVE_MAX_TRADE_NOTIONAL_USD. Sized 100 USD by default.
        if amount_usd > settings.LIVE_MAX_TRADE_NOTIONAL_USD:
            await inc(db, "live_orders_skipped_notional_cap")
            return GateResult(
                approved=False,
                reject_reason="notional_cap_exceeded",
                detail=(
                    f"amount_usd=${amount_usd:.2f} exceeds "
                    f"LIVE_MAX_TRADE_NOTIONAL_USD=${settings.LIVE_MAX_TRADE_NOTIONAL_USD:.2f}"
                ),
            )
```

- [ ] **Step 5: Run test — expect PASS**

```bash
uv run pytest tests/test_live_master_kill.py::TestNotionalCapEnforcement -v
```

- [ ] **Step 6: Commit**

```bash
git add scout/live/gates.py scout/db.py tests/test_live_master_kill.py
git commit -m "feat(live-m1): per-trade notional cap (Gate 8)"
```

---

## Task 5: cross_venue_exposure SQL view + Gate 7 update

**Files:**
- Modify: `scout/db.py` (add view creation in `_create_tables` or new helper `_create_cross_venue_exposure_view`)
- Modify: `scout/live/gates.py` (Gate 7 query target)
- Test: `tests/test_live_cross_venue_exposure.py` (NEW)

- [ ] **Step 1: Write failing test for view existence + Gate 7 behavior**

Create `tests/test_live_cross_venue_exposure.py`:

```python
"""BL-NEW-LIVE-HYBRID M1: cross_venue_exposure view + Gate 7 update."""
from __future__ import annotations

import pytest

from scout.db import Database


@pytest.mark.asyncio
async def test_cross_venue_exposure_view_exists(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view' AND name='cross_venue_exposure'"
    )
    assert (await cur.fetchone()) is not None
    await db.close()


@pytest.mark.asyncio
async def test_view_aggregates_open_live_trades_binance(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Insert a synthetic live_trades row (open, $200 size)
    await db._conn.execute(
        """INSERT INTO live_trades
           (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
            size_usd, status, created_at)
           VALUES (1, 'btc', 'BTC', 'binance', 'BTCUSDT', 'first_signal',
                   '200.0', 'open', '2026-05-06T12:00:00+00:00')""",
    )
    await db._conn.commit()
    cur = await db._conn.execute(
        "SELECT venue, open_exposure_usd FROM cross_venue_exposure "
        "WHERE venue='binance'"
    )
    row = await cur.fetchone()
    assert row is not None
    assert float(row[1]) == 200.0
    await db.close()


@pytest.mark.asyncio
async def test_gate_7_uses_cross_venue_view(tmp_path, settings_factory):
    """Gate 7 must query cross_venue_exposure (not shadow_trades alone).
    With $900 already open + new $200 intent + cap $1000, gate refuses."""
    from scout.live.gates import GateChain  # adjust to actual class name

    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        """INSERT INTO live_trades
           (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
            size_usd, status, created_at)
           VALUES (1, 'btc', 'BTC', 'binance', 'BTCUSDT', 'first_signal',
                   '900.0', 'open', '2026-05-06T12:00:00+00:00')""",
    )
    await db._conn.commit()
    s = settings_factory(
        LIVE_MAX_OPEN_EXPOSURE_USD=1000.0,
        LIVE_MAX_TRADE_NOTIONAL_USD=500.0,
    )
    # Construct a Gate-7-only invocation (helper in gates.py, see step 3)
    # ... assert returns reject_reason='exposure_cap'
    await db.close()
```

- [ ] **Step 2: Run tests — expect FAILs**

- [ ] **Step 3: Create the view in scout/db.py**

In `_create_tables()` (after the table CREATEs), add:

```python
    await self._conn.execute("""
        CREATE VIEW IF NOT EXISTS cross_venue_exposure AS
        SELECT
            'binance' AS venue,
            COALESCE(SUM(CAST(size_usd AS REAL)), 0) AS open_exposure_usd,
            COUNT(*) AS open_count
        FROM live_trades
        WHERE status = 'open'
        UNION ALL
        SELECT
            'minara_' || COALESCE(chain, 'unknown') AS venue,
            COALESCE(SUM(amount_usd), 0) AS open_exposure_usd,
            COUNT(*) AS open_count
        FROM paper_trades
        WHERE status = 'open' AND chain != 'coingecko'
        GROUP BY chain
    """)
```

(Sized DEX side returns empty in M1 — wired but inert until M2 ships MinaraAdapter that creates real DEX trades.)

- [ ] **Step 4: Update Gate 7 in scout/live/gates.py**

Find Gate 7 around line 207-231 (search for `FROM shadow_trades WHERE status = 'open'`). Replace the query:

```python
        # Gate 7: cross-venue exposure cap (BL-NEW-LIVE-HYBRID M1).
        # Queries cross_venue_exposure view to aggregate open positions
        # across CEX + DEX. M1 only sees Binance live_trades; M2 adds
        # the chain-native paper_trades branch.
        cur = await conn.execute(
            "SELECT COALESCE(SUM(open_exposure_usd), 0) FROM cross_venue_exposure"
        )
        open_total = float((await cur.fetchone())[0])
        if open_total + amount_usd > settings.LIVE_MAX_OPEN_EXPOSURE_USD:
            await inc(db, "live_orders_skipped_exposure_cap")
            return GateResult(
                approved=False,
                reject_reason="exposure_cap",
                detail=(
                    f"open=${open_total:.2f} + new=${amount_usd:.2f} > "
                    f"LIVE_MAX_OPEN_EXPOSURE_USD=${settings.LIVE_MAX_OPEN_EXPOSURE_USD:.2f}"
                ),
            )
```

- [ ] **Step 5: Run tests — expect PASS**

- [ ] **Step 6: Commit**

```bash
git add scout/db.py scout/live/gates.py tests/test_live_cross_venue_exposure.py
git commit -m "feat(live-m1): cross_venue_exposure view + Gate 7 cross-venue update"
```

---

## Task 6: Per-signal opt-in gate

**Files:**
- Modify: `scout/live/gates.py` (Gate 9: signal opt-in)
- Test: `tests/test_live_master_kill.py` (extend)

- [ ] **Step 1: Append failing test for opt-in gate**

```python
class TestSignalOptInEnforcement:
    @pytest.mark.asyncio
    async def test_signal_with_live_eligible_0_rejected(
        self, tmp_path, settings_factory
    ):
        """Default fail-closed: signal with live_eligible=0 must reject."""
        from scout.db import Database
        from scout.live.engine import LiveEngine

        db = Database(tmp_path / "t.db")
        await db.initialize()
        # All signals default live_eligible=0 — don't opt-in
        s = settings_factory(LIVE_TRADING_ENABLED=True, LIVE_MODE="shadow")

        class StubPaperTrade:
            id = 1; coin_id = "btc"; symbol = "BTC"
            signal_type = "first_signal"; amount_usd = 50.0; chain = "coingecko"

        class StubAdapter:
            venue_name = "binance"
            async def fetch_depth(self, pair): raise AssertionError
            async def submit_order(self, *args, **kw): raise AssertionError

        engine = LiveEngine(db=db, adapter=StubAdapter(), settings=s)
        await engine.execute_intent(StubPaperTrade())
        cur = await db._conn.execute(
            "SELECT reject_reason FROM shadow_trades WHERE paper_trade_id=1"
        )
        row = await cur.fetchone()
        assert row is not None and row[0] == "signal_disabled"
        await db.close()
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Add `signal_disabled` to reject_reason CHECK constraint**

(Same pattern as Task 4 step 3.)

- [ ] **Step 4: Add Gate 9**

In `gates.py`, AFTER Gate 8 (notional cap), add:

```python
        # Gate 9: per-signal opt-in (BL-NEW-LIVE-HYBRID M1, Layer 3).
        # Reads signal_params.live_eligible. Default 0 = fail-closed.
        # Operator opts a signal in via UPDATE signal_params SET
        # live_eligible=1 WHERE signal_type='X'.
        cur = await conn.execute(
            "SELECT live_eligible FROM signal_params WHERE signal_type = ?",
            (paper_trade.signal_type,),
        )
        row = await cur.fetchone()
        is_eligible = bool(row[0]) if row is not None else False
        if not is_eligible:
            await inc(db, "live_orders_skipped_signal_disabled")
            return GateResult(
                approved=False,
                reject_reason="signal_disabled",
                detail=f"signal_params.live_eligible=0 for {paper_trade.signal_type}",
            )
```

- [ ] **Step 5: Run test — expect PASS**

- [ ] **Step 6: Commit**

---

## Task 7: balance_gate.py implementation

**Files:**
- Create: `scout/live/balance_gate.py`
- Modify: `scout/live/binance_adapter.py` (add `fetch_account_balance` method)
- Test: `tests/test_live_balance_gate.py` (NEW)

- [ ] **Step 1: Write failing tests**

Create `tests/test_live_balance_gate.py`:

```python
"""BL-NEW-LIVE-HYBRID M1: balance_gate tests."""
from __future__ import annotations

import pytest

from scout.live.balance_gate import check_sufficient_balance, BalanceGateResult


@pytest.mark.asyncio
async def test_sufficient_balance_approves():
    class StubAdapter:
        async def fetch_account_balance(self, asset="USDT"):
            return 500.0
    result = await check_sufficient_balance(
        adapter=StubAdapter(),
        required_usd=100.0,
        margin_factor=1.1,
    )
    assert isinstance(result, BalanceGateResult)
    assert result.approved is True


@pytest.mark.asyncio
async def test_insufficient_balance_rejects():
    class StubAdapter:
        async def fetch_account_balance(self, asset="USDT"):
            return 50.0
    result = await check_sufficient_balance(
        adapter=StubAdapter(),
        required_usd=100.0,
        margin_factor=1.1,
    )
    assert result.approved is False
    assert result.reject_reason == "insufficient_balance"


@pytest.mark.asyncio
async def test_margin_factor_creates_buffer():
    class StubAdapter:
        async def fetch_account_balance(self, asset="USDT"):
            return 105.0
    # 105 USDT but need 100 * 1.1 = 110 (buffer for fees + slippage).
    # Should reject.
    result = await check_sufficient_balance(
        adapter=StubAdapter(),
        required_usd=100.0,
        margin_factor=1.1,
    )
    assert result.approved is False
```

- [ ] **Step 2: Create scout/live/balance_gate.py**

```python
"""BL-NEW-LIVE-HYBRID M1: balance availability gate.

Pre-trade check that the venue account has at least
required_usd × margin_factor in the quote asset (USDT for Binance perps).
The margin_factor (default 1.1) creates a 10% buffer above notional to
absorb fees, slippage, and minor tick rounding.

Per BL-055 spec §5: this gate runs AFTER the depth check (Gate 6) and
BEFORE order submission. It does NOT account for already-open positions'
margin requirement; that's BL-055 follow-up scope (BL-NEW-LIVE-MARGIN).
For M1, we treat USDT balance as a simple liquid-cash check.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import structlog

log = structlog.get_logger(__name__)


class _AdapterWithBalance(Protocol):
    async def fetch_account_balance(self, asset: str = "USDT") -> float: ...


@dataclass(frozen=True)
class BalanceGateResult:
    approved: bool
    reject_reason: str | None
    detail: str | None
    available_usd: float


async def check_sufficient_balance(
    *,
    adapter: _AdapterWithBalance,
    required_usd: float,
    margin_factor: float = 1.1,
) -> BalanceGateResult:
    """Return BalanceGateResult. approved=True iff
    available >= required_usd * margin_factor.
    """
    if margin_factor < 1.0:
        raise ValueError(f"margin_factor must be >= 1.0; got {margin_factor}")
    if required_usd <= 0:
        raise ValueError(f"required_usd must be > 0; got {required_usd}")

    available_usd = await adapter.fetch_account_balance(asset="USDT")
    threshold = required_usd * margin_factor

    if available_usd >= threshold:
        return BalanceGateResult(
            approved=True,
            reject_reason=None,
            detail=None,
            available_usd=available_usd,
        )
    return BalanceGateResult(
        approved=False,
        reject_reason="insufficient_balance",
        detail=(
            f"available={available_usd:.2f} USDT < "
            f"required={required_usd:.2f} × margin={margin_factor:.2f} "
            f"= {threshold:.2f}"
        ),
        available_usd=available_usd,
    )
```

- [ ] **Step 3: Add `fetch_account_balance` to binance_adapter**

In `scout/live/binance_adapter.py`, add a method:

```python
    async def fetch_account_balance(self, asset: str = "USDT") -> float:
        """Return free + locked balance for `asset` from /fapi/v2/balance.
        Returns 0.0 if the asset is not in the account.
        """
        async with self._session.get(
            f"{self._base_url}/fapi/v2/balance",
            headers=self._signed_headers(),
        ) as resp:
            resp.raise_for_status()
            balances = await resp.json()
            for entry in balances:
                if entry.get("asset") == asset:
                    return float(entry.get("balance", 0.0))
            return 0.0
```

(Adjust to actual binance_adapter.py shape — `_session`, `_base_url`, `_signed_headers` are illustrative; use whatever the existing adapter uses for authenticated requests.)

- [ ] **Step 4: Wire balance_gate into the gate chain**

In `gates.py`, AFTER the depth-check gate (Gate 6) and BEFORE order submission, call balance_gate:

```python
        from scout.live.balance_gate import check_sufficient_balance
        balance_result = await check_sufficient_balance(
            adapter=adapter,
            required_usd=amount_usd,
            margin_factor=1.1,
        )
        if not balance_result.approved:
            await inc(db, "live_orders_skipped_insufficient_balance")
            return GateResult(
                approved=False,
                reject_reason=balance_result.reject_reason,
                detail=balance_result.detail,
            )
```

- [ ] **Step 5: Run tests — expect PASS**

```bash
uv run pytest tests/test_live_balance_gate.py -v
```

- [ ] **Step 6: Commit**

---

## Task 8: client_order_id idempotency contract

**Files:**
- Modify: `scout/live/binance_adapter.py` (generate + persist client_order_id; pre-retry dedup)
- Modify: `scout/db.py` (add `client_order_id TEXT` column to live_trades + shadow_trades — migration)
- Test: `tests/test_live_idempotency.py` (NEW)

- [ ] **Step 1: Write failing test**

Create `tests/test_live_idempotency.py`:

```python
"""BL-NEW-LIVE-HYBRID M1: client_order_id idempotency contract."""
from __future__ import annotations

import pytest

# Pin the contract: every order submission generates client_order_id of
# format "gecko-{paper_trade_id}-{intent_uuid}". A retry MUST query
# /fapi/v1/openOrders by client_order_id and skip submission if matched.


class TestClientOrderIdGeneration:
    @pytest.mark.asyncio
    async def test_client_order_id_format(self):
        from scout.live.binance_adapter import BinanceAdapter
        adapter = BinanceAdapter(...)  # adjust constructor
        order_id = adapter._generate_client_order_id(paper_trade_id=42)
        assert order_id.startswith("gecko-42-")
        # UUID4 hex is 32 chars; full id is "gecko-{int}-{32hex}"
        prefix, ptid, uuid_part = order_id.split("-", 2)
        assert prefix == "gecko"
        assert ptid == "42"
        assert len(uuid_part) >= 8


class TestRetryDedup:
    @pytest.mark.asyncio
    async def test_retry_finds_existing_order_skips_resubmit(self):
        """When an order with the same client_order_id already exists in
        Binance's open orders, the adapter must NOT re-submit on retry.

        Uses aioresponses to mock the /fapi/v1/openOrders + /fapi/v1/order
        endpoints. The first call returns an existing order matching the
        client_order_id we'd generate; the second (submit) endpoint is
        registered with assert_call_count=0 so the test fails if the
        adapter naively re-submits.
        """
        from aioresponses import aioresponses
        from scout.live.binance_adapter import BinanceAdapter
        import aiohttp

        # Pin the client_order_id by stubbing _generate_client_order_id
        # (otherwise uuid4 randomness defeats the open-orders match).
        FIXED_COID = "gecko-42-deadbeefcafebabe"

        async with aiohttp.ClientSession() as session:
            adapter = BinanceAdapter(
                session=session,
                api_key="test", api_secret="test",
                base_url="https://fapi.binance.com",
            )
            adapter._generate_client_order_id = lambda paper_trade_id: FIXED_COID

            with aioresponses() as m:
                # /fapi/v1/openOrders returns an existing matching order
                m.get(
                    "https://fapi.binance.com/fapi/v1/openOrders?symbol=BTCUSDT",
                    payload=[{
                        "orderId": 12345,
                        "clientOrderId": FIXED_COID,
                        "symbol": "BTCUSDT",
                        "status": "NEW",
                        "side": "BUY",
                        "origQty": "0.001",
                    }],
                    status=200,
                )
                # /fapi/v1/order — registered but should NOT be hit
                m.post(
                    "https://fapi.binance.com/fapi/v1/order",
                    payload={"orderId": 99999, "status": "NEW"},
                    status=200,
                )

                result = await adapter._submit_order_with_dedup(
                    paper_trade_id=42,
                    symbol="BTCUSDT",
                    side="BUY",
                    quantity=0.001,
                )

                # Assert: returned the EXISTING order, did NOT submit a new one
                assert result["clientOrderId"] == FIXED_COID
                assert result["orderId"] == 12345  # NOT 99999 (which would mean second submit)

                # Verify /fapi/v1/order was NOT called by inspecting recorded calls
                post_calls = [
                    call for url, calls in m.requests.items() for call in calls
                    if "fapi/v1/order" in str(url[1]) and url[0] == "POST"
                ]
                assert len(post_calls) == 0, (
                    f"adapter must skip submit when client_order_id "
                    f"already exists; got {len(post_calls)} POST calls"
                )
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Add `client_order_id` column migration**

In `scout/db.py`, add a new migration `_migrate_live_trades_client_order_id`:

```python
    async def _migrate_live_trades_client_order_id(self) -> None:
        """BL-NEW-LIVE-HYBRID M1: add client_order_id idempotency column.
        PRAGMA-guarded ALTER on both live_trades and shadow_trades."""
        # ... full pattern matching prior migrations ...
        # Adds: live_trades.client_order_id TEXT
        # Adds: shadow_trades.client_order_id TEXT
        # paper_migrations marker: bl_live_client_order_id_v1
        # schema_version: 20260509 (reserved by this plan; see Task 2 §"Schema version reservation")
```

Wire into `initialize()` after the previous migration.

- [ ] **Step 4: Implement client_order_id generation in binance_adapter**

```python
    def _generate_client_order_id(self, paper_trade_id: int) -> str:
        """gecko-{paper_trade_id}-{uuid4_hex}. Persisted to live_trades
        row at submit-time; used as Binance's clientOrderId. Allows
        retry-safe dedup: query /fapi/v1/openOrders by clientOrderId
        before resubmitting on transient error."""
        import uuid
        return f"gecko-{paper_trade_id}-{uuid.uuid4().hex}"

    async def _submit_order_with_dedup(
        self, *, paper_trade_id: int, symbol: str, side: str,
        quantity: float, **kwargs
    ):
        client_order_id = self._generate_client_order_id(paper_trade_id)
        # Pre-check: is there already an open order with this client_order_id?
        # (Defensive; covers retry from a prior process run.)
        existing = await self._fetch_open_orders_by_client_id(symbol, client_order_id)
        if existing:
            log.warning(
                "binance_submit_dedup_skipped",
                client_order_id=client_order_id,
                existing_status=existing.get("status"),
            )
            return existing
        # Submit with explicit clientOrderId
        return await self._submit(
            symbol=symbol, side=side, quantity=quantity,
            newClientOrderId=client_order_id, **kwargs
        )
```

- [ ] **Step 5: Run tests — expect PASS**

- [ ] **Step 6: Commit**

---

## Task 9: Telegram startup notification

**Files:**
- Modify: `scout/main.py` (add startup hook)
- Test: `tests/test_live_master_kill.py` (extend with startup-hook test using `structlog.testing.capture_logs`)

- [ ] **Step 1: Write failing test using capture_logs (NOT caplog — see PR #81 lesson)**

```python
class TestStartupNotification:
    @pytest.mark.asyncio
    async def test_telegram_alert_emitted_on_live_enabled_startup(
        self, tmp_path, settings_factory
    ):
        """When LIVE_TRADING_ENABLED=True, pipeline boot emits a Telegram
        notification (or at minimum a structlog event) so operator
        cannot forget the state."""
        from structlog.testing import capture_logs
        from scout.main import _emit_live_trading_startup_notification

        s = settings_factory(LIVE_TRADING_ENABLED=True)
        with capture_logs() as captured:
            await _emit_live_trading_startup_notification(settings=s, session=None)
        events = [e for e in captured if e.get("event") == "live_trading_startup_notice"]
        assert events, f"expected startup notice; got: {[e.get('event') for e in captured]}"
```

- [ ] **Step 2: Run — expect FAIL** (function doesn't exist)

- [ ] **Step 3: Implement**

In `scout/main.py`, add:

```python
async def _emit_live_trading_startup_notification(
    *, settings, session
) -> None:
    """When LIVE_TRADING_ENABLED=True at pipeline boot, fire a Telegram
    alert + structlog WARNING. Operator can never silently boot in live
    mode without seeing the state."""
    if not settings.LIVE_TRADING_ENABLED:
        return
    log.warning(
        "live_trading_startup_notice",
        live_mode=settings.LIVE_MODE,
        max_trade_notional_usd=settings.LIVE_MAX_TRADE_NOTIONAL_USD,
        max_open_exposure_usd=settings.LIVE_MAX_OPEN_EXPOSURE_USD,
    )
    if session is not None:
        from scout import alerter
        await alerter.send_telegram_message(
            message=(
                f"🔴 LIVE TRADING ENABLED — pipeline started\n"
                f"  LIVE_MODE = {settings.LIVE_MODE}\n"
                f"  per-trade cap = ${settings.LIVE_MAX_TRADE_NOTIONAL_USD:.0f}\n"
                f"  aggregate cap = ${settings.LIVE_MAX_OPEN_EXPOSURE_USD:.0f}\n"
                f"  master kill = ON (operator-controlled via .env)"
            ),
            session=session,
            settings=settings,
        )
```

Wire it into the existing pipeline-startup sequence in `main.py` (find where the aiohttp session is created + pipeline begins).

- [ ] **Step 4: Run test — expect PASS**

- [ ] **Step 5: Commit**

---

## Task 10: Full regression + black

- [ ] **Step 1: Run full regression**

```bash
uv run pytest --tb=short -q
```

Expect: all green; no regression on existing 1389 tests; +N new tests from this plan all pass.

- [ ] **Step 2: Format**

```bash
uv run black scout/ tests/
```

Expected: clean (no diffs).

- [ ] **Step 3: Verify nothing else broke**

```bash
git diff --stat HEAD~10..HEAD  # confirm only intended files changed
```

- [ ] **Step 4: Commit any black diffs**

```bash
git add -A
git commit -m "chore(live-m1): black formatting" || true
```

---

## Task 11: PR + 3-vector reviewer dispatch

Per CLAUDE.md §8, this change touches money flows + irreversible class. Multi-vector review required.

- [ ] **Step 1: Push branch + open draft PR**

```bash
git push -u origin feat/live-trading-m1-cex
gh pr create --draft \
  --title "feat: live trading M1 — CEX (BL-055) ready to soak (BL-NEW-LIVE-HYBRID)" \
  --body "$(cat <<'EOF'
## ⚠️ DO NOT MERGE WITHOUT 3-VECTOR REVIEW

Implements milestone 1 of the live-trading hybrid architecture (design
at tasks/design_live_trading_hybrid.md, committed 263c419).

This PR implements the SCAFFOLDING for live execution. It does NOT
enable live trading — operator must explicitly flip
LIVE_TRADING_ENABLED=True in .env post-deploy AND meet all
prerequisites in the design's open-questions section before any real
money lands.

## Scope (M1 only)
- LIVE_TRADING_ENABLED master kill + LIVE_MAX_TRADE_NOTIONAL_USD +
  LIVE_MAX_OPEN_EXPOSURE_USD Settings
- signal_params.live_eligible per-signal opt-in (Layer 3)
- balance_gate.py (was missing per BL-055 prereq)
- client_order_id idempotency contract on Binance adapter
- cross_venue_exposure SQL view + Gate 7 update
- Telegram startup notification when live enabled
- New live_orders_skipped_* metric family

## Out of scope (M2)
- MinaraAdapter for DEX execution
- VenueResolver chain-aware extension
- minara_health.py + circuit breaker
- Live position aggregator

## Test plan
- [ ] All N new tests pass (master_kill, balance_gate, idempotency,
      cross_venue_exposure, live_eligible_migration)
- [ ] Existing 1389 tests still pass
- [ ] black --check clean
- [ ] Manual: deploy to VPS, verify migration runs, verify
      cross_venue_exposure view returns 0 (no live_trades yet)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Dispatch 3 parallel reviewers**

Per CLAUDE.md §8, three orthogonal axes:

1. **Code/structural reviewer** — verify migration shape (PRAGMA-guarded ALTER, schema_version uniqueness, post-assertion inside try); Gate ordering (master kill BEFORE notional BEFORE exposure BEFORE depth BEFORE balance — verify); idempotent client_order_id generation; signal_params dataclass field placement (default after defaults).

2. **Strategy/blast-radius reviewer** — verify deploy path (no behavior change with defaults — LIVE_TRADING_ENABLED=False); migration safety on prod scout.db; verify operator-can-discover the new flags via heartbeat; confirm there's no auto-enable path.

3. **Statistical/policy reviewer** — verify default values (100/$1000) match the design's pre-registered numbers; verify gate failure modes log + count correctly; verify reject_reasons land in the schema's CHECK constraint.

- [ ] **Step 3: Apply MUST-FIX findings + commit**

- [ ] **Step 4: Mark PR ready + squash-merge**

```bash
gh pr ready <PR#>
gh pr merge <PR#> --squash --delete-branch
```

- [ ] **Step 5: Deploy to VPS** (per CLAUDE.md SSH two-step pattern)

```bash
ssh root@89.167.116.187 'systemctl stop gecko-pipeline && cd /root/gecko-alpha && git pull && find . -name __pycache__ -exec rm -rf {} + ; systemctl start gecko-pipeline && sleep 5 && systemctl is-active gecko-pipeline' > .ssh_deploy_live_m1.txt 2>&1
```

Read `.ssh_deploy_live_m1.txt` to verify.

- [ ] **Step 6: Verify migration ran on prod scout.db**

```bash
ssh root@89.167.116.187 'sqlite3 /root/gecko-alpha/scout.db "PRAGMA table_info(signal_params)" | grep live_eligible' > .ssh_verify_live_eligible.txt 2>&1
ssh root@89.167.116.187 'sqlite3 /root/gecko-alpha/scout.db "SELECT * FROM cross_venue_exposure"' > .ssh_verify_view.txt 2>&1
```

- [ ] **Step 7: Confirm LIVE_TRADING_ENABLED is False on prod (default)** — operator confirms .env has no `LIVE_TRADING_ENABLED=True` line.

- [ ] **Step 8: Memory + todo update**

Write memory entry `project_live_m1_shipped_2026_05_06.md`. Update `tasks/todo.md` with milestone 1 prerequisites status + the new soak windows that fire when operator flips LIVE_TRADING_ENABLED=True.

---

## Done criteria for Milestone 1

- All new tests pass; full regression clean; black clean
- PR merged via squash; deployed to VPS
- migration ran cleanly; cross_venue_exposure view returns []
- LIVE_TRADING_ENABLED defaults False on prod; no signal has live_eligible=1 yet
- Operator has all 4 design open-questions answered + balance funded + first-signal selected before flipping master kill
- Memory entry recording M1 ship state
- todo.md updated with M1 soak windows tied to approval-removal criteria firing
