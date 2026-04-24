# BL-062 Signal Stacking + Peak-Fade Early Kill — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close two paper-trading leaks: require ≥2 scoring signals for `first_signal` admission, and add a peak-fade exit that fires when both 6h and 24h checkpoints sit below 70% of peak.

**Architecture:** Two additive changes in `scout/trading/`. Admission gate in `trade_first_signals` short-circuits when `len(signals_fired) < 2`. New evaluator branch in the BL-061 ladder cascade, inserted between trailing-stop and expiry, fires when both checkpoint observations are present and sustained below the retrace ratio. New column `peak_fade_fired_at` on `paper_trades` records fires for the A/B review. Cutover timestamp in `paper_migrations` (name `bl062_peak_fade`) keys the 30-day calibration review.

**Tech Stack:** Python 3.12, `aiosqlite`, Pydantic v2 `BaseSettings`, `pytest-asyncio` (auto mode), `uv` for env management, React/JSX (dashboard reason-badge only).

**Spec:** `docs/superpowers/specs/2026-04-23-bl062-signal-stacking-peak-fade-design.md`

**File map (all modifications — no new Python modules):**

| file | responsibility after change |
|---|---|
| `scout/config.py` | adds 4 new Settings fields + validators |
| `scout/db.py` | extends `expected_cols` with `peak_fade_fired_at`; adds `bl062_peak_fade` row + index in migration block |
| `scout/trading/evaluator.py` | new `_load_bl062_cutover_ts` helper; SELECT extended with cp_6h_pct/cp_24h_pct; new peak-fade branch between trail and expiry |
| `scout/trading/signals.py` | admission gate in `trade_first_signals` |
| `scout/trading/paper.py` | adds `peak_fade` → `closed_peak_fade` entry in `execute_sell` status_map |
| `dashboard/frontend/components/TradingTab.jsx` | new `PEAK_FADE` branch in `reasonBadge` |
| `docs/superpowers/reviews/2026-05-23-bl062-peak-fade-review.md` | scheduled review stub (SQL query + stop rule) |

**Test files (all new or appended):**

| file | coverage |
|---|---|
| `tests/test_config.py` (append) | 4 new validator paths |
| `tests/test_trading_db_migration.py` (append) | `peak_fade_fired_at` column + `bl062_peak_fade` row + idempotent re-run |
| `tests/test_paper_evaluator.py` (append) | `_load_bl062_cutover_ts`; peak-fade fire conditions; exit precedence matrix; `remaining_qty` handling; fire-once |
| `tests/test_trading_signals.py` (append) | admission gate — 1 vs 2 vs 3+ signals; override; trigger-family-not-counted |
| `tests/test_paper_trader.py` (append) | `execute_sell(reason="peak_fade")` writes `exit_reason='peak_fade'` and `status='closed_peak_fade'` |

---

## Task 1: Settings fields + validators

Add four new config knobs with Pydantic validators. Must land first because every downstream task reads `settings.PEAK_FADE_*` or `settings.FIRST_SIGNAL_MIN_SIGNAL_COUNT`.

**Files:**
- Modify: `scout/config.py:238` (append BL-062 fields under the BL-061 ladder block)
- Modify: `scout/config.py:392` (add validators in the existing validator section)
- Test: `tests/test_config.py` (append BL-062 validator tests)

- [ ] **Step 1: Write the failing validator tests**

Append to `tests/test_config.py`:

```python
# ---------------------------------------------------------------------------
# BL-062 signal-stacking + peak-fade validators
# ---------------------------------------------------------------------------


def test_first_signal_min_signal_count_default_is_two(monkeypatch):
    from scout.config import Settings
    monkeypatch.delenv("FIRST_SIGNAL_MIN_SIGNAL_COUNT", raising=False)
    s = Settings()
    assert s.FIRST_SIGNAL_MIN_SIGNAL_COUNT == 2


def test_first_signal_min_signal_count_rejects_zero(monkeypatch):
    import pytest
    from scout.config import Settings
    monkeypatch.setenv("FIRST_SIGNAL_MIN_SIGNAL_COUNT", "0")
    with pytest.raises(ValueError, match="FIRST_SIGNAL_MIN_SIGNAL_COUNT"):
        Settings()


def test_peak_fade_enabled_default_true(monkeypatch):
    from scout.config import Settings
    monkeypatch.delenv("PEAK_FADE_ENABLED", raising=False)
    s = Settings()
    assert s.PEAK_FADE_ENABLED is True


def test_peak_fade_min_peak_pct_rejects_zero(monkeypatch):
    import pytest
    from scout.config import Settings
    monkeypatch.setenv("PEAK_FADE_MIN_PEAK_PCT", "0")
    with pytest.raises(ValueError, match="PEAK_FADE_MIN_PEAK_PCT"):
        Settings()


def test_peak_fade_min_peak_pct_rejects_negative(monkeypatch):
    import pytest
    from scout.config import Settings
    monkeypatch.setenv("PEAK_FADE_MIN_PEAK_PCT", "-5")
    with pytest.raises(ValueError, match="PEAK_FADE_MIN_PEAK_PCT"):
        Settings()


def test_peak_fade_retrace_ratio_rejects_one(monkeypatch):
    import pytest
    from scout.config import Settings
    monkeypatch.setenv("PEAK_FADE_RETRACE_RATIO", "1.0")
    with pytest.raises(ValueError, match="PEAK_FADE_RETRACE_RATIO"):
        Settings()


def test_peak_fade_retrace_ratio_rejects_zero(monkeypatch):
    import pytest
    from scout.config import Settings
    monkeypatch.setenv("PEAK_FADE_RETRACE_RATIO", "0")
    with pytest.raises(ValueError, match="PEAK_FADE_RETRACE_RATIO"):
        Settings()


def test_peak_fade_retrace_ratio_accepts_half(monkeypatch):
    from scout.config import Settings
    monkeypatch.setenv("PEAK_FADE_RETRACE_RATIO", "0.5")
    s = Settings()
    assert s.PEAK_FADE_RETRACE_RATIO == 0.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -k "first_signal_min_signal_count or peak_fade" -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'FIRST_SIGNAL_MIN_SIGNAL_COUNT'` (or equivalent for the other fields).

- [ ] **Step 3: Add fields to Settings class**

In `scout/config.py`, find the BL-061 ladder block (around line 238, after `PAPER_LADDER_FLOOR_ARM_ON_LEG_1`). Append:

```python
    # BL-062 signal-stacking: require ≥N scoring signals for first_signal admission
    FIRST_SIGNAL_MIN_SIGNAL_COUNT: int = 2
    # BL-062 peak-fade early-kill: sustained-fade exit between trail and expiry
    PEAK_FADE_ENABLED: bool = True
    PEAK_FADE_MIN_PEAK_PCT: float = 10.0
    PEAK_FADE_RETRACE_RATIO: float = 0.7
```

- [ ] **Step 4: Add validators**

In `scout/config.py`, find the existing validator section (around line 392, near `_validate_ladder_qty_frac`). Append:

```python
    @field_validator("FIRST_SIGNAL_MIN_SIGNAL_COUNT")
    @classmethod
    def _validate_first_signal_min_count(cls, v: int) -> int:
        if v < 1:
            raise ValueError(
                f"FIRST_SIGNAL_MIN_SIGNAL_COUNT must be >= 1; got={v}"
            )
        return v

    @field_validator("PEAK_FADE_MIN_PEAK_PCT")
    @classmethod
    def _validate_peak_fade_min_peak_pct(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(
                f"PEAK_FADE_MIN_PEAK_PCT must be > 0; got={v}"
            )
        return v

    @field_validator("PEAK_FADE_RETRACE_RATIO")
    @classmethod
    def _validate_peak_fade_retrace_ratio(cls, v: float) -> float:
        if not (0.0 < v < 1.0):
            raise ValueError(
                f"PEAK_FADE_RETRACE_RATIO must be in (0, 1); got={v}"
            )
        return v
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -k "first_signal_min_signal_count or peak_fade" -v`
Expected: PASS (8 tests).

- [ ] **Step 6: Run full config test file as regression check**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (all existing + new tests).

- [ ] **Step 7: Commit**

```bash
git add scout/config.py tests/test_config.py
git commit -m "feat(BL-062): add signal-stacking and peak-fade settings + validators"
```

---

## Task 2: Schema migration — peak_fade_fired_at column + cutover row + index

Add the `peak_fade_fired_at` column via the PRAGMA-gated `expected_cols` loop at `scout/db.py:881-892`, append the `bl062_peak_fade` row to `paper_migrations` (mirror of BL-061's pattern at `scout/db.py:894-905`), and create the partial index. Re-run on an already-migrated DB must be a no-op.

**Files:**
- Modify: `scout/db.py:870` (extend `expected_cols` dict)
- Modify: `scout/db.py:901` (append INSERT OR IGNORE + CREATE INDEX after existing BL-061 block)
- Test: `tests/test_trading_db_migration.py` (append BL-062 test block)

- [ ] **Step 1: Write the failing migration tests**

Append to `tests/test_trading_db_migration.py`:

```python
# ---------------------------------------------------------------------------
# BL-062: peak_fade_fired_at column + paper_migrations cutover row + index
# ---------------------------------------------------------------------------


async def test_bl062_peak_fade_column_added(tmp_path):
    from scout.db import Database
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(paper_trades)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "peak_fade_fired_at" in cols, (
        f"peak_fade_fired_at column missing from paper_trades; have {sorted(cols)}"
    )
    await db.close()


async def test_bl062_cutover_row_written(tmp_path):
    from scout.db import Database
    from datetime import datetime
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT cutover_ts FROM paper_migrations WHERE name='bl062_peak_fade'"
    )
    row = await cur.fetchone()
    assert row is not None, "bl062_peak_fade row must exist after initialize()"
    parsed = datetime.fromisoformat(row[0])
    assert parsed.tzinfo is not None, "cutover_ts must be ISO with tz"
    await db.close()


async def test_bl062_index_created(tmp_path):
    from scout.db import Database
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='idx_paper_trades_peak_fade_fired_at'"
    )
    row = await cur.fetchone()
    assert row is not None, "idx_paper_trades_peak_fade_fired_at must exist"
    await db.close()


async def test_bl062_migration_idempotent_re_run(tmp_path):
    """Re-initialize an existing DB: no errors, cutover_ts preserved."""
    from scout.db import Database
    db_path = tmp_path / "t.db"
    db = Database(db_path)
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT cutover_ts FROM paper_migrations WHERE name='bl062_peak_fade'"
    )
    (first_ts,) = await cur.fetchone()
    await db.close()

    # Second init must not fail on ADD COLUMN (PRAGMA gate) or INSERT (OR IGNORE)
    db2 = Database(db_path)
    await db2.initialize()
    cur = await db2._conn.execute(
        "SELECT cutover_ts FROM paper_migrations WHERE name='bl062_peak_fade'"
    )
    (second_ts,) = await cur.fetchone()
    assert second_ts == first_ts, (
        f"cutover_ts must be preserved across re-init; first={first_ts} second={second_ts}"
    )
    await db2.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_trading_db_migration.py -k "bl062" -v`
Expected: FAIL — column missing / row missing / index missing.

- [ ] **Step 3: Extend expected_cols dict**

In `scout/db.py`, find the `expected_cols` dict near line 870 (inside `_create_tables`, after the BL-061 ladder columns). Add `"peak_fade_fired_at"`:

```python
            expected_cols = {
                "lead_time_vs_trending_status": "TEXT",
                "would_be_live": "INTEGER",
                # BL-061 ladder state
                "leg_1_filled_at": "TEXT",
                "leg_1_exit_price": "REAL",
                "leg_2_filled_at": "TEXT",
                "leg_2_exit_price": "REAL",
                "remaining_qty": "REAL",
                "floor_armed": "INTEGER",
                "realized_pnl_usd": "REAL",
                # BL-062 peak-fade exit marker (NULL until fire)
                "peak_fade_fired_at": "TEXT",
            }
```

The existing loop (`for col, coltype in expected_cols.items()` at lines 883-892) handles the PRAGMA check and guarded ALTER. No change to the loop itself.

- [ ] **Step 4: Append cutover row + index after BL-061 block**

In `scout/db.py`, find the existing BL-061 block that creates `paper_migrations` and inserts `bl061_ladder` (lines 894-905). Immediately after that block (and before the existing `CREATE INDEX` calls near line 907), append:

```python
            # BL-062: peak-fade cutover row + index on fire-time column
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                ("bl062_peak_fade", datetime.now(timezone.utc).isoformat()),
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_paper_trades_peak_fade_fired_at "
                "ON paper_trades(peak_fade_fired_at) "
                "WHERE peak_fade_fired_at IS NOT NULL"
            )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_trading_db_migration.py -k "bl062" -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Run full migration test file as regression check**

Run: `uv run pytest tests/test_trading_db_migration.py -v`
Expected: PASS (all existing + new tests).

- [ ] **Step 7: Commit**

```bash
git add scout/db.py tests/test_trading_db_migration.py
git commit -m "feat(BL-062): schema migration for peak_fade_fired_at + cutover row"
```

---

## Task 3: Cutover loader `_load_bl062_cutover_ts`

Add a loader mirroring `_load_bl061_cutover_ts` at `scout/trading/evaluator.py:20-31`. The evaluator doesn't yet need it for gating (peak-fade applies to all BL-061 rows), but the 30-day review query and potential future A/B filtering both read this value; having the helper keeps the module symmetric with BL-061 and lets tests assert the wiring.

**Files:**
- Modify: `scout/trading/evaluator.py:31` (append `_load_bl062_cutover_ts`)
- Test: `tests/test_paper_evaluator.py` (append loader test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_paper_evaluator.py`:

```python
async def test_bl062_cutover_ts_returns_iso_timestamp(tmp_path):
    from scout.db import Database
    from scout.trading.evaluator import _load_bl062_cutover_ts
    from datetime import datetime

    db = Database(tmp_path / "t.db")
    await db.initialize()
    ts = await _load_bl062_cutover_ts(db._conn)
    assert ts is not None, "loader must return the cutover_ts written by migration"
    parsed = datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None
    await db.close()


async def test_bl062_cutover_ts_returns_none_when_missing(tmp_path):
    from scout.db import Database
    from scout.trading.evaluator import _load_bl062_cutover_ts

    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Simulate a corrupted DB where the row was manually deleted
    await db._conn.execute(
        "DELETE FROM paper_migrations WHERE name = 'bl062_peak_fade'"
    )
    await db._conn.commit()
    ts = await _load_bl062_cutover_ts(db._conn)
    assert ts is None
    await db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_paper_evaluator.py -k "bl062_cutover" -v`
Expected: FAIL with `ImportError: cannot import name '_load_bl062_cutover_ts'`.

- [ ] **Step 3: Add the loader**

In `scout/trading/evaluator.py`, immediately after `_load_bl061_cutover_ts` (line 31), append:

```python
async def _load_bl062_cutover_ts(conn) -> str | None:
    """Load BL-062 peak-fade cutover timestamp from paper_migrations.

    Returns None if the row is missing. Callers treat None as 'no
    cutover recorded' and should not use this value to filter fires —
    it exists for the 30-day calibration review query.
    """
    cur = await conn.execute(
        "SELECT cutover_ts FROM paper_migrations WHERE name = 'bl062_peak_fade'"
    )
    row = await cur.fetchone()
    return row[0] if row else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_paper_evaluator.py -k "bl062_cutover" -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add scout/trading/evaluator.py tests/test_paper_evaluator.py
git commit -m "feat(BL-062): add _load_bl062_cutover_ts helper"
```

---

## Task 4: Admission gate in `trade_first_signals`

Add `len(signals_fired) < settings.FIRST_SIGNAL_MIN_SIGNAL_COUNT` short-circuit at the top of the per-token loop in `trade_first_signals` (`scout/trading/signals.py:322`). `signals_fired` here is the scorer output — a list of scoring signal names — and the trigger family string `"first_signal"` is NOT included in that list (it is supplied separately to `build_combo_key` via `signal_type=`).

**Files:**
- Modify: `scout/trading/signals.py:322` (add admission gate inside the for-loop)
- Test: `tests/test_trading_signals.py` (append admission-gate tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_trading_signals.py`:

```python
# ---------------------------------------------------------------------------
# BL-062 admission gate — trade_first_signals requires len(signals_fired) >= N
# ---------------------------------------------------------------------------


class _FakeEngine:
    def __init__(self):
        self.opens = []

    async def open_trade(self, **kwargs):
        self.opens.append(kwargs)


def _make_candidate(contract_address="tok-a"):
    from scout.models import CandidateToken
    return CandidateToken(
        contract_address=contract_address,
        ticker="TOK",
        token_name="Token",
        chain="coingecko",
        market_cap_usd=10_000_000,
        price_usd=1.0,
    )


async def test_first_signal_admission_blocks_single_scoring_signal(
    tmp_path, settings_factory
):
    from scout.db import Database
    from scout.trading.signals import trade_first_signals

    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES ('tok-a', 1.0, '2026-04-23T00:00:00+00:00')"
    )
    await db._conn.commit()
    engine = _FakeEngine()
    settings = settings_factory(FIRST_SIGNAL_MIN_SIGNAL_COUNT=2)
    candidate = _make_candidate()
    # Single scoring signal — blocked by new gate
    scored = [(candidate, 50.0, ["momentum_ratio"])]
    await trade_first_signals(engine, db, scored, settings=settings)
    assert engine.opens == [], "single-signal admission must be blocked"
    await db.close()


async def test_first_signal_admission_accepts_two_scoring_signals(
    tmp_path, settings_factory
):
    from scout.db import Database
    from scout.trading.signals import trade_first_signals

    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES ('tok-b', 1.0, '2026-04-23T00:00:00+00:00')"
    )
    await db._conn.commit()
    engine = _FakeEngine()
    settings = settings_factory(FIRST_SIGNAL_MIN_SIGNAL_COUNT=2)
    candidate = _make_candidate(contract_address="tok-b")
    scored = [(candidate, 70.0, ["momentum_ratio", "vol_acceleration"])]
    await trade_first_signals(engine, db, scored, settings=settings)
    assert len(engine.opens) == 1
    assert engine.opens[0]["signal_type"] == "first_signal"
    await db.close()


async def test_first_signal_admission_override_to_one_admits_single(
    tmp_path, settings_factory
):
    from scout.db import Database
    from scout.trading.signals import trade_first_signals

    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES ('tok-c', 1.0, '2026-04-23T00:00:00+00:00')"
    )
    await db._conn.commit()
    engine = _FakeEngine()
    settings = settings_factory(FIRST_SIGNAL_MIN_SIGNAL_COUNT=1)
    candidate = _make_candidate(contract_address="tok-c")
    scored = [(candidate, 50.0, ["momentum_ratio"])]
    await trade_first_signals(engine, db, scored, settings=settings)
    assert len(engine.opens) == 1, "MIN=1 must admit single-signal"
    await db.close()


async def test_first_signal_admission_trigger_family_not_counted(
    tmp_path, settings_factory
):
    """Guard against future refactor that accidentally counts 'first_signal'
    trigger identifier into signals_fired. Passing ['momentum_ratio'] plus
    default MIN=2 must block — the 'first_signal' string is the signal_type
    argument to build_combo_key, NOT a scoring signal.
    """
    from scout.db import Database
    from scout.trading.signals import trade_first_signals

    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES ('tok-d', 1.0, '2026-04-23T00:00:00+00:00')"
    )
    await db._conn.commit()
    engine = _FakeEngine()
    settings = settings_factory(FIRST_SIGNAL_MIN_SIGNAL_COUNT=2)
    candidate = _make_candidate(contract_address="tok-d")
    scored = [(candidate, 50.0, ["momentum_ratio"])]  # NOT including "first_signal"
    await trade_first_signals(engine, db, scored, settings=settings)
    assert engine.opens == [], (
        "The trigger-family identifier 'first_signal' must not be counted "
        "in signals_fired; the gate must see len=1 and block."
    )
    await db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_trading_signals.py -k "admission" -v`
Expected: FAIL — single-signal test fails because `engine.opens` is non-empty (no gate yet).

- [ ] **Step 3: Add admission gate**

In `scout/trading/signals.py`, find `trade_first_signals` (line 298). At line 322, the existing loop starts:

```python
    for token, quant_score, signals_fired in scored_candidates:
        if quant_score <= 0 or not signals_fired:
            continue
```

Immediately after that early-return, add the new gate:

```python
    for token, quant_score, signals_fired in scored_candidates:
        if quant_score <= 0 or not signals_fired:
            continue
        if len(signals_fired) < settings.FIRST_SIGNAL_MIN_SIGNAL_COUNT:
            continue
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_trading_signals.py -k "admission" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Run full signals test file as regression check**

Run: `uv run pytest tests/test_trading_signals.py -v`
Expected: PASS. Existing tests may need `FIRST_SIGNAL_MIN_SIGNAL_COUNT=1` via `settings_factory` to keep passing if they only pass one signal — update any such test with an inline override rather than changing the default. Read failing test output carefully and add `FIRST_SIGNAL_MIN_SIGNAL_COUNT=1` override to any pre-existing `trade_first_signals` test that exercises single-signal admission intentionally.

- [ ] **Step 6: Commit**

```bash
git add scout/trading/signals.py tests/test_trading_signals.py
git commit -m "feat(BL-062): admission gate requires len(signals_fired) >= 2"
```

---

## Task 5: `execute_sell` status mapping for `peak_fade`

Add `"peak_fade": "closed_peak_fade"` to the status_map in `PaperTrader.execute_sell` (`scout/trading/paper.py:303`). This keeps the peak-fade exit symmetric with `expired`, `trailing_stop`, etc., and removes the need for a `status_override` at the call site in the evaluator.

**Files:**
- Modify: `scout/trading/paper.py:303` (append key to status_map dict)
- Test: `tests/test_paper_trader.py` (append status-map test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_paper_trader.py`:

```python
async def test_execute_sell_peak_fade_sets_closed_peak_fade_status(
    tmp_path,
):
    from scout.db import Database
    from scout.trading.paper import PaperTrader

    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    trade_id = await trader.execute_buy(
        db=db, token_id="tok-pf", symbol="PF", name="PeakFade",
        chain="coingecko", signal_type="first_signal", signal_data={},
        current_price=1.00, amount_usd=100.0, tp_pct=20.0, sl_pct=15.0,
        slippage_bps=0, signal_combo="first_signal+momentum_ratio",
    )
    closed = await trader.execute_sell(
        db=db, trade_id=trade_id, current_price=1.05,
        reason="peak_fade", slippage_bps=0,
    )
    assert closed is True
    cur = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    status, reason = await cur.fetchone()
    assert status == "closed_peak_fade"
    assert reason == "peak_fade"
    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_paper_trader.py -k "peak_fade" -v`
Expected: FAIL with `status == "closed_manual"` (current default for unmapped reasons).

- [ ] **Step 3: Extend status_map**

In `scout/trading/paper.py`, find the `status_map` dict in `execute_sell` (line 303). Add `peak_fade`:

```python
        status_map = {
            "take_profit": "closed_tp",
            "stop_loss": "closed_sl",
            "expired": "closed_expired",
            "trailing_stop": "closed_trailing_stop",
            "peak_fade": "closed_peak_fade",
            "manual": "closed_manual",
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_paper_trader.py -k "peak_fade" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scout/trading/paper.py tests/test_paper_trader.py
git commit -m "feat(BL-062): map peak_fade reason to closed_peak_fade status"
```

---

## Task 6: Peak-fade exit branch in evaluator

This is the meatiest task. Three sub-changes in `scout/trading/evaluator.py`:

1. Extend the SELECT at line 64-73 to pull `checkpoint_6h_pct` and `checkpoint_24h_pct`.
2. Decode those two columns in the row unpacking block (after existing cp_6h/cp_24h price extractions).
3. Insert a new `peak_fade` branch in the BL-061 cascade (lines 204-274), positioned between the trail-stop branch (line 241-252) and the expiry branch (line 253-256). On fire, first UPDATE `peak_fade_fired_at`, then call `execute_sell(reason="peak_fade")`.

**Files:**
- Modify: `scout/trading/evaluator.py:64-73` (SELECT column list)
- Modify: `scout/trading/evaluator.py:108-111` (cp_* variable extraction — add _pct variables)
- Modify: `scout/trading/evaluator.py:253` (insert peak_fade branch)
- Test: `tests/test_paper_evaluator.py` (append peak-fade suite)

- [ ] **Step 1: Write the failing fire-condition tests**

Append to `tests/test_paper_evaluator.py`:

```python
# ---------------------------------------------------------------------------
# BL-062 peak-fade: fire conditions, precedence, remaining_qty, fire-once
# ---------------------------------------------------------------------------


async def _seed_post_leg1_trade(db, token_id, settings):
    """Helper: open a trade and arm the ladder floor via a simulated leg 1 fill.

    Leaves remaining_qty at 70% of original amount_usd, floor_armed=1,
    peak_pct set to the argument peak_at_seed. Caller seeds price_cache
    and checkpoint columns as needed.
    """
    from scout.trading.paper import PaperTrader
    from datetime import datetime, timezone

    trader = PaperTrader()
    trade_id = await trader.execute_buy(
        db=db, token_id=token_id, symbol=token_id.upper(), name=token_id,
        chain="coingecko", signal_type="first_signal", signal_data={},
        current_price=1.00, amount_usd=100.0, tp_pct=40.0, sl_pct=15.0,
        slippage_bps=0, signal_combo="first_signal+momentum_ratio",
    )
    # Simulate leg 1 fill at +25% — arms the floor, reduces qty to 70%
    await trader.execute_partial_sell(
        db=db, trade_id=trade_id, leg=1,
        sell_qty_frac=settings.PAPER_LADDER_LEG_1_QTY_FRAC,
        current_price=1.25, slippage_bps=0,
    )
    # Backdate opened_at to 25h ago so the 24h checkpoint path is legal
    twenty_five_h_ago = (datetime.now(timezone.utc).timestamp() - 25 * 3600)
    backdate_iso = datetime.fromtimestamp(
        twenty_five_h_ago, tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S")
    await db._conn.execute(
        "UPDATE paper_trades SET opened_at = ?, created_at = ? WHERE id = ?",
        (backdate_iso, backdate_iso, trade_id),
    )
    await db._conn.commit()
    return trade_id


async def _set_checkpoints_and_peak(
    db, trade_id, *, peak_pct, cp_6h_pct, cp_24h_pct
):
    """Manually set peak_pct + both checkpoint_*_pct columns."""
    await db._conn.execute(
        "UPDATE paper_trades SET peak_pct = ?, peak_price = ?, "
        "checkpoint_6h_pct = ?, checkpoint_6h_price = ?, "
        "checkpoint_24h_pct = ?, checkpoint_24h_price = ? "
        "WHERE id = ?",
        (
            peak_pct, 1.0 + peak_pct / 100,
            cp_6h_pct, 1.0 + cp_6h_pct / 100,
            cp_24h_pct, 1.0 + cp_24h_pct / 100,
            trade_id,
        ),
    )
    await db._conn.commit()


async def _seed_current_price(db, token_id, price):
    from datetime import datetime, timezone
    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        (token_id, price, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()


async def test_peak_fade_fires_when_both_checkpoints_below_ratio(
    tmp_path, settings_factory
):
    from scout.db import Database
    from scout.trading.evaluator import evaluate_paper_trades

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(
        PEAK_FADE_ENABLED=True,
        PEAK_FADE_MIN_PEAK_PCT=10.0,
        PEAK_FADE_RETRACE_RATIO=0.7,
    )
    trade_id = await _seed_post_leg1_trade(db, "tok-pf1", settings)
    # peak = 20%, 0.7 * 20 = 14. Both cps below 14 → fire.
    await _set_checkpoints_and_peak(
        db, trade_id, peak_pct=20.0, cp_6h_pct=10.0, cp_24h_pct=8.0
    )
    await _seed_current_price(db, "tok-pf1", 1.08)  # current +8%

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT status, exit_reason, peak_fade_fired_at "
        "FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    status, reason, fired_at = await cur.fetchone()
    assert status == "closed_peak_fade"
    assert reason == "peak_fade"
    assert fired_at is not None
    await db.close()


async def test_peak_fade_no_fire_when_peak_below_threshold(
    tmp_path, settings_factory
):
    from scout.db import Database
    from scout.trading.evaluator import evaluate_paper_trades

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(PEAK_FADE_MIN_PEAK_PCT=10.0)
    trade_id = await _seed_post_leg1_trade(db, "tok-pf2", settings)
    # peak = 8% (below threshold) — no fire even with full retrace
    await _set_checkpoints_and_peak(
        db, trade_id, peak_pct=8.0, cp_6h_pct=1.0, cp_24h_pct=0.5
    )
    await _seed_current_price(db, "tok-pf2", 1.01)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT status, peak_fade_fired_at FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    status, fired_at = await cur.fetchone()
    assert fired_at is None
    assert status != "closed_peak_fade"
    await db.close()


async def test_peak_fade_no_fire_when_cp_6h_missing(
    tmp_path, settings_factory
):
    from scout.db import Database
    from scout.trading.evaluator import evaluate_paper_trades

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory()
    trade_id = await _seed_post_leg1_trade(db, "tok-pf3", settings)
    # peak ok, cp_24h below ratio, cp_6h NULL → no fire (dual-observation required)
    await db._conn.execute(
        "UPDATE paper_trades SET peak_pct = ?, peak_price = ?, "
        "checkpoint_6h_pct = NULL, checkpoint_6h_price = NULL, "
        "checkpoint_24h_pct = ?, checkpoint_24h_price = ? "
        "WHERE id = ?",
        (20.0, 1.20, 5.0, 1.05, trade_id),
    )
    await db._conn.commit()
    await _seed_current_price(db, "tok-pf3", 1.05)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT peak_fade_fired_at FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    (fired_at,) = await cur.fetchone()
    assert fired_at is None
    await db.close()


async def test_peak_fade_no_fire_when_cp_24h_missing(
    tmp_path, settings_factory
):
    from scout.db import Database
    from scout.trading.evaluator import evaluate_paper_trades

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory()
    trade_id = await _seed_post_leg1_trade(db, "tok-pf4", settings)
    await db._conn.execute(
        "UPDATE paper_trades SET peak_pct = ?, peak_price = ?, "
        "checkpoint_6h_pct = ?, checkpoint_6h_price = ?, "
        "checkpoint_24h_pct = NULL, checkpoint_24h_price = NULL "
        "WHERE id = ?",
        (20.0, 1.20, 5.0, 1.05, trade_id),
    )
    await db._conn.commit()
    await _seed_current_price(db, "tok-pf4", 1.05)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT peak_fade_fired_at FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    (fired_at,) = await cur.fetchone()
    assert fired_at is None
    await db.close()


async def test_peak_fade_no_fire_when_only_one_cp_below_ratio(
    tmp_path, settings_factory
):
    from scout.db import Database
    from scout.trading.evaluator import evaluate_paper_trades

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory()
    trade_id = await _seed_post_leg1_trade(db, "tok-pf5", settings)
    # peak = 20%, threshold = 0.7 * 20 = 14. cp_6h = 10 (below), cp_24h = 16 (above) → no fire
    await _set_checkpoints_and_peak(
        db, trade_id, peak_pct=20.0, cp_6h_pct=10.0, cp_24h_pct=16.0
    )
    await _seed_current_price(db, "tok-pf5", 1.16)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT peak_fade_fired_at FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    (fired_at,) = await cur.fetchone()
    assert fired_at is None
    await db.close()


async def test_peak_fade_disabled_flag_suppresses_fire(
    tmp_path, settings_factory
):
    from scout.db import Database
    from scout.trading.evaluator import evaluate_paper_trades

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(PEAK_FADE_ENABLED=False)
    trade_id = await _seed_post_leg1_trade(db, "tok-pf6", settings)
    await _set_checkpoints_and_peak(
        db, trade_id, peak_pct=20.0, cp_6h_pct=5.0, cp_24h_pct=5.0
    )
    await _seed_current_price(db, "tok-pf6", 1.05)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT peak_fade_fired_at FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    (fired_at,) = await cur.fetchone()
    assert fired_at is None, "PEAK_FADE_ENABLED=False must suppress all fires"
    await db.close()


async def test_peak_fade_sl_wins_when_both_eligible(
    tmp_path, settings_factory
):
    """SL triggers before peak-fade in the precedence chain."""
    from scout.db import Database
    from scout.trading.evaluator import evaluate_paper_trades

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory()
    # Fresh trade (no leg 1) so floor is not armed — SL eligibility path
    from scout.trading.paper import PaperTrader
    from datetime import datetime, timezone

    trader = PaperTrader()
    trade_id = await trader.execute_buy(
        db=db, token_id="tok-pf7", symbol="PF7", name="pf7",
        chain="coingecko", signal_type="first_signal", signal_data={},
        current_price=1.00, amount_usd=100.0, tp_pct=40.0, sl_pct=15.0,
        slippage_bps=0, signal_combo="first_signal+momentum_ratio",
    )
    twenty_five_h = datetime.now(timezone.utc).timestamp() - 25 * 3600
    backdate = datetime.fromtimestamp(twenty_five_h, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    await db._conn.execute(
        "UPDATE paper_trades SET opened_at = ?, created_at = ?, "
        "peak_pct = 20.0, peak_price = 1.20, "
        "checkpoint_6h_pct = 5.0, checkpoint_6h_price = 1.05, "
        "checkpoint_24h_pct = -20.0, checkpoint_24h_price = 0.80 "
        "WHERE id = ?",
        (backdate, backdate, trade_id),
    )
    await db._conn.commit()
    # Current price at -20% — trips SL before peak_fade check
    await _seed_current_price(db, "tok-pf7", 0.80)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT exit_reason, peak_fade_fired_at FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    reason, fired_at = await cur.fetchone()
    assert reason == "stop_loss"
    assert fired_at is None, "SL must win; peak_fade must not fire"
    await db.close()


async def test_peak_fade_trail_wins_when_both_eligible(
    tmp_path, settings_factory
):
    """Trailing-stop triggers before peak-fade on the same evaluator pass."""
    from scout.db import Database
    from scout.trading.evaluator import evaluate_paper_trades

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(
        PAPER_LADDER_TRAIL_PCT=12.0,
    )
    trade_id = await _seed_post_leg1_trade(db, "tok-pf8", settings)
    # peak = 30%, trail threshold = peak_price * (1 - 0.12) = 1.30 * 0.88 = 1.144
    # cp_6h = 5, cp_24h = 5 (both below 0.7*30 = 21) — peak_fade eligible
    await _set_checkpoints_and_peak(
        db, trade_id, peak_pct=30.0, cp_6h_pct=5.0, cp_24h_pct=5.0
    )
    # Current price 1.10 → below trail_threshold 1.144 → trail fires first
    await _seed_current_price(db, "tok-pf8", 1.10)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT exit_reason, peak_fade_fired_at FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    reason, fired_at = await cur.fetchone()
    assert reason == "trailing_stop", (
        f"trail must win; got reason={reason}"
    )
    assert fired_at is None
    await db.close()


async def test_peak_fade_fires_when_trail_not_tripped(
    tmp_path, settings_factory
):
    """Trail armed but current price ABOVE trail threshold on this pass;
    peak-fade must still fire based on the 6h/24h observations."""
    from scout.db import Database
    from scout.trading.evaluator import evaluate_paper_trades

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(
        PAPER_LADDER_TRAIL_PCT=12.0,
    )
    trade_id = await _seed_post_leg1_trade(db, "tok-pf9", settings)
    # peak = 20%, trail threshold = 1.20 * 0.88 = 1.056
    # cp_6h = 8, cp_24h = 8 (both below 0.7*20 = 14) — peak_fade eligible
    await _set_checkpoints_and_peak(
        db, trade_id, peak_pct=20.0, cp_6h_pct=8.0, cp_24h_pct=8.0
    )
    # Current price 1.08 → above trail_threshold 1.056 → trail does NOT fire
    await _seed_current_price(db, "tok-pf9", 1.08)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT exit_reason, peak_fade_fired_at FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    reason, fired_at = await cur.fetchone()
    assert reason == "peak_fade"
    assert fired_at is not None
    await db.close()


async def test_peak_fade_closes_remaining_qty_only(
    tmp_path, settings_factory
):
    from scout.db import Database
    from scout.trading.evaluator import evaluate_paper_trades

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory()
    trade_id = await _seed_post_leg1_trade(db, "tok-pf10", settings)
    # Confirm remaining_qty is 70% of original (100 USD * 0.70 = 70 USD)
    cur = await db._conn.execute(
        "SELECT remaining_qty FROM paper_trades WHERE id = ?", (trade_id,)
    )
    (rem_before,) = await cur.fetchone()
    assert rem_before is not None and rem_before > 0
    await _set_checkpoints_and_peak(
        db, trade_id, peak_pct=20.0, cp_6h_pct=8.0, cp_24h_pct=8.0
    )
    await _seed_current_price(db, "tok-pf10", 1.08)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    status, reason = await cur.fetchone()
    assert status == "closed_peak_fade"
    assert reason == "peak_fade"
    await db.close()


async def test_peak_fade_does_not_refire_once_closed(
    tmp_path, settings_factory
):
    """Second evaluator pass on an already-closed trade must be a no-op."""
    from scout.db import Database
    from scout.trading.evaluator import evaluate_paper_trades

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory()
    trade_id = await _seed_post_leg1_trade(db, "tok-pf11", settings)
    await _set_checkpoints_and_peak(
        db, trade_id, peak_pct=20.0, cp_6h_pct=8.0, cp_24h_pct=8.0
    )
    await _seed_current_price(db, "tok-pf11", 1.08)

    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT peak_fade_fired_at FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    (first_fire,) = await cur.fetchone()
    assert first_fire is not None

    # Run again — trade is closed (status != 'open'), SELECT in evaluator
    # filters to status='open', so no second fire attempt.
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT peak_fade_fired_at FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    (second_fire,) = await cur.fetchone()
    assert second_fire == first_fire, "peak_fade_fired_at must not be rewritten"
    await db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_paper_evaluator.py -k "peak_fade" -v`
Expected: FAIL with errors like `sqlite3.OperationalError: no such column: checkpoint_6h_pct` (SELECT doesn't include it yet) or `peak_fade_fired_at` being NULL (no branch yet).

- [ ] **Step 3: Extend evaluator SELECT**

In `scout/trading/evaluator.py`, line 64-73, expand the SELECT list to include `checkpoint_6h_pct` and `checkpoint_24h_pct`:

```python
    # 1. Get all open trades
    cursor = await conn.execute("""SELECT id, token_id, entry_price, opened_at,
                  tp_price, sl_price, tp_pct, sl_pct,
                  checkpoint_1h_price, checkpoint_6h_price,
                  checkpoint_24h_price, checkpoint_48h_price,
                  peak_price, peak_pct, signal_data, symbol, name, chain,
                  amount_usd, quantity, signal_type,
                  created_at, leg_1_filled_at, leg_2_filled_at,
                  remaining_qty, floor_armed, realized_pnl_usd,
                  checkpoint_6h_pct, checkpoint_24h_pct
           FROM paper_trades
           WHERE status = 'open'""")
```

The new columns appear at indices 26 and 27 (after `realized_pnl_usd` at index 25).

- [ ] **Step 4: Decode new columns + insert peak-fade branch**

In `scout/trading/evaluator.py`, inside the BL-061 cascade block (lines 197-274), just after the `is_bl061 = (...)` assignment and before `if is_bl061:` at line 204, pull the pct values. More precisely: inside the `if is_bl061:` block, before the cascade logic, add pct extraction. Then insert the peak-fade branch between the trailing-stop branch (line 241-252) and the expiry branch (line 253-256).

Full replacement for the BL-061 cascade block (lines 197-274). Locate the existing block and replace with:

```python
            is_bl061 = (
                remaining_qty is not None
                and created_at_dt is not None
                and cutover_dt is not None
                and created_at_dt >= cutover_dt
            )

            if is_bl061:
                # BL-062 peak-fade checkpoint pct values (may be NULL)
                cp_6h_pct = row[26] if len(row) > 26 and row[26] is not None else None
                cp_24h_pct = row[27] if len(row) > 27 and row[27] is not None else None

                close_reason = None
                close_status: str | None = None
                # SL applies only before leg 1 arms the floor
                if not floor_armed and sl_price > 0 and current_price <= sl_price:
                    close_reason = "stop_loss"
                    close_status = "closed_sl"
                # Leg 1
                elif leg_1_filled is None and change_pct >= settings.PAPER_LADDER_LEG_1_PCT:
                    await _trader.execute_partial_sell(
                        db=db, trade_id=trade_id, leg=1,
                        sell_qty_frac=settings.PAPER_LADDER_LEG_1_QTY_FRAC,
                        current_price=current_price, slippage_bps=slippage_bps,
                    )
                    continue
                # Leg 2
                elif (
                    leg_1_filled is not None
                    and leg_2_filled is None
                    and change_pct >= settings.PAPER_LADDER_LEG_2_PCT
                ):
                    await _trader.execute_partial_sell(
                        db=db, trade_id=trade_id, leg=2,
                        sell_qty_frac=settings.PAPER_LADDER_LEG_2_QTY_FRAC,
                        current_price=current_price, slippage_bps=slippage_bps,
                    )
                    continue
                # Floor exit — once armed, don't let the runner slice close below entry
                elif floor_armed and current_price <= entry_price:
                    close_reason = "floor"
                    close_status = "closed_floor"
                    log.info(
                        "floor_exit",
                        trade_id=trade_id, peak_pct=round(peak_pct or 0, 2),
                        current_price=current_price,
                    )
                # Trailing stop on runner (post-leg-1 only)
                elif (
                    floor_armed
                    and peak_price is not None
                    and peak_pct is not None
                    and peak_pct >= settings.PAPER_LADDER_LEG_1_PCT
                ):
                    trail_threshold = peak_price * (
                        1 - settings.PAPER_LADDER_TRAIL_PCT / 100.0
                    )
                    if current_price < trail_threshold:
                        close_reason = "trailing_stop"
                        close_status = "closed_trailing_stop"
                # BL-062 peak-fade — sustained fade at 6h AND 24h checkpoints
                if (
                    close_reason is None
                    and settings.PEAK_FADE_ENABLED
                    and peak_pct is not None
                    and peak_pct >= settings.PEAK_FADE_MIN_PEAK_PCT
                    and cp_6h_pct is not None
                    and cp_24h_pct is not None
                    and cp_6h_pct < peak_pct * settings.PEAK_FADE_RETRACE_RATIO
                    and cp_24h_pct < peak_pct * settings.PEAK_FADE_RETRACE_RATIO
                ):
                    close_reason = "peak_fade"
                    close_status = "closed_peak_fade"
                    await conn.execute(
                        "UPDATE paper_trades SET peak_fade_fired_at = ? WHERE id = ?",
                        (datetime.now(timezone.utc).isoformat(), trade_id),
                    )
                # Expiry — last resort
                if close_reason is None and elapsed >= max_duration:
                    close_reason = "expired"
                    close_status = "closed_expired"

                if close_reason is not None:
                    closed = await _trader.execute_sell(
                        db=db, trade_id=trade_id,
                        current_price=current_price,
                        reason=close_reason,
                        slippage_bps=slippage_bps,
                        status_override=close_status,
                    )
                    if closed:
                        log.info(
                            "paper_trade_eval_closed",
                            trade_id=trade_id, token_id=token_id,
                            reason=close_reason,
                            current_price=current_price,
                            change_pct=round(change_pct, 2),
                        )
                continue  # skip old cascade entirely for BL-061 rows
```

Note: the peak-fade and expiry branches are converted from `elif` to standalone `if close_reason is None and ...` blocks so they correctly fall through after the SL/floor/trail branches. The trail branch still uses `elif` because it's an alternative to SL/leg/floor within the same chain; peak-fade sits *after* that chain as an independent check.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_paper_evaluator.py -k "peak_fade" -v`
Expected: PASS (11 tests).

- [ ] **Step 6: Run full evaluator test file as regression check**

Run: `uv run pytest tests/test_paper_evaluator.py -v`
Expected: PASS — confirm no BL-061 ladder tests regressed.

- [ ] **Step 7: Run the full test suite**

Run: `uv run pytest --tb=short -q`
Expected: PASS. If any pre-existing test that uses `evaluate_paper_trades` regresses, it's probably because the test seeded a post-leg-1 trade with peak_pct + cp_*_pct values that now trigger peak-fade. Fix by setting `PEAK_FADE_ENABLED=False` in that test's `settings_factory` call.

- [ ] **Step 8: Commit**

```bash
git add scout/trading/evaluator.py tests/test_paper_evaluator.py
git commit -m "feat(BL-062): peak-fade exit branch in ladder cascade"
```

---

## Task 7: Dashboard reason badge for `PEAK_FADE`

One-line UI addition in `reasonBadge` so the new `closed_peak_fade` exit-reason renders with a distinct badge rather than falling through to the generic `<span className="outcome-badge">{reason}</span>` default. Closes the "operator needs ongoing visibility during the 30-day measurement window" requirement.

**Files:**
- Modify: `dashboard/frontend/components/TradingTab.jsx:97` (insert PEAK_FADE branch in reasonBadge)

- [ ] **Step 1: Add the badge branch**

In `dashboard/frontend/components/TradingTab.jsx`, find `reasonBadge` at line 92. Insert a new branch between the EXPIRED and MANUAL branches:

```jsx
function reasonBadge(reason) {
  if (!reason) return <span className="outcome-badge">-</span>
  const r = reason.toUpperCase()
  if (r === 'TP' || r === 'TAKE_PROFIT') return <span className="outcome-badge win">TP</span>
  if (r === 'SL' || r === 'STOP_LOSS') return <span className="outcome-badge loss">SL</span>
  if (r === 'EXPIRED' || r === 'TIMEOUT') return <span className="outcome-badge" style={{ background: 'var(--color-bar-bg)', color: 'var(--color-text-secondary)' }}>Expired</span>
  if (r === 'PEAK_FADE') return <span className="outcome-badge" style={{ background: 'rgba(255, 183, 77, 0.15)', color: 'var(--color-accent-amber)' }}>Peak Fade</span>
  if (r === 'MANUAL') return <span className="outcome-badge" style={{ background: 'rgba(255, 183, 77, 0.15)', color: 'var(--color-accent-amber)' }}>Manual</span>
  return <span className="outcome-badge">{reason}</span>
}
```

- [ ] **Step 2: Build the frontend**

Run: `cd dashboard/frontend && npm run build`
Expected: build succeeds; `dashboard/frontend/dist/` is regenerated.

- [ ] **Step 3: Visual smoke check**

Run: `uv run uvicorn dashboard.main:app --reload --port 8000` (in a background shell, if convenient — otherwise just eyeball on next VPS deploy).
Navigate to `http://localhost:8000/` → Trading tab → Closed Trades table. Any row with `exit_reason = 'peak_fade'` (none in local DB unless seeded) should render as a "Peak Fade" amber badge. If no such rows exist locally, this step is deferred to post-deploy verification on the VPS.

- [ ] **Step 4: Commit**

```bash
git add dashboard/frontend/components/TradingTab.jsx dashboard/frontend/dist/
git commit -m "feat(BL-062): dashboard reason badge for peak_fade"
```

---

## Task 8: Schedule the 30-day review

Spec item 5 requires a review checkpoint 30 days after the `bl062_peak_fade` cutover. Record the review plan, SQL query, and two-tier stop rule so a future session can execute it without re-deriving the spec.

**Files:**
- Create: `docs/superpowers/reviews/2026-05-23-bl062-peak-fade-review.md`

- [ ] **Step 1: Write the review stub**

Create `docs/superpowers/reviews/2026-05-23-bl062-peak-fade-review.md`:

```markdown
# BL-062 Peak-Fade 30-Day Calibration Review

**Scheduled:** 2026-05-23 (approximately 30 days after the `bl062_peak_fade`
cutover recorded in `paper_migrations` at merge time).

**Spec reference:** `docs/superpowers/specs/2026-04-23-bl062-signal-stacking-peak-fade-design.md`

## Procedure

1. Load cutover_ts:

   ```sql
   SELECT cutover_ts FROM paper_migrations WHERE name = 'bl062_peak_fade';
   ```

2. Compute fire count, clip rate, and average delta on the forward cohort:

   ```sql
   WITH cutover AS (
       SELECT cutover_ts AS ts FROM paper_migrations WHERE name = 'bl062_peak_fade'
   ),
   fired AS (
       SELECT id, peak_pct, pnl_pct, checkpoint_48h_pct
       FROM paper_trades, cutover
       WHERE peak_fade_fired_at IS NOT NULL
         AND opened_at >= cutover.ts
   )
   SELECT
       COUNT(*) AS fires,
       SUM(CASE WHEN checkpoint_48h_pct IS NOT NULL
                 AND checkpoint_48h_pct > pnl_pct THEN 1 ELSE 0 END) AS clips,
       ROUND(AVG(pnl_pct - COALESCE(checkpoint_48h_pct, pnl_pct)), 4) AS avg_delta
   FROM fired;
   ```

   (Note: `checkpoint_48h_pct` is the best proxy for "would-have-been-expiry
   P&L" available without a counterfactual. If coverage is thin, fall back
   to a median-of-peers estimate or widen the window.)

3. Compute clip_pct = clips / fires.

## Stop Rule

| tier | trigger | action |
|---|---|---|
| early warning | `fires >= 10 AND clip_pct > 0.25` | set `PEAK_FADE_ENABLED=false` in VPS `.env` + restart gecko-pipeline.service + file investigation ticket |
| primary | `fires >= 20 AND clip_pct > 0.15` | same actions as early warning |

If neither tier triggers, leave the rule on and re-review in another 30 days
(or merge into the ongoing BL-061 ladder review cadence).

## Revert Procedure (if triggered)

```bash
ssh srilu-vps
sudo sed -i 's/^PEAK_FADE_ENABLED=.*/PEAK_FADE_ENABLED=false/' /root/gecko-alpha/.env
sudo systemctl restart gecko-pipeline.service
```

Then file a new ticket capturing: the forward cohort's fire count, the
clip_pct, and the top 5 clipped trades (by delta) for root-cause analysis.

## Cutover Recovery Note

If the `bl062_peak_fade` row in `paper_migrations` is ever manually corrupted
or deleted, **edit the cutover_ts in place** — do not delete and rely on the
migration to re-insert. `INSERT OR IGNORE` writes a *new later* timestamp on
startup, which shifts the A/B boundary forward and invalidates historical
comparisons.
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/reviews/2026-05-23-bl062-peak-fade-review.md
git commit -m "docs(BL-062): schedule 30-day peak-fade calibration review"
```

---

## Final Verification

- [ ] **Full test suite green**

Run: `uv run pytest --tb=short -q`
Expected: all tests PASS, zero regressions.

- [ ] **Black formatting**

Run: `uv run black scout/ tests/`
Expected: no changes (all new code already formatted), or apply formatting and re-commit under `chore: black formatting`.

- [ ] **Dry-run pipeline smoke test**

Run: `uv run python -m scout.main --dry-run --cycles 1`
Expected: clean exit, no schema errors, no `AttributeError` on new Settings fields.

- [ ] **Migration dry-run against a copy of prod scout.db** (recommended before VPS deploy, not required for merge)

```bash
scp srilu-vps:/root/gecko-alpha/scout.db /tmp/scout-copy.db
uv run python -c "
import asyncio
from scout.db import Database
async def main():
    db = Database('/tmp/scout-copy.db')
    await db.initialize()
    cur = await db._conn.execute(
        \"SELECT name FROM sqlite_master WHERE type='index' AND name='idx_paper_trades_peak_fade_fired_at'\"
    )
    print('index:', await cur.fetchone())
    cur = await db._conn.execute('SELECT COUNT(*) FROM paper_trades')
    print('rows preserved:', (await cur.fetchone())[0])
    await db.close()
asyncio.run(main())
"
```

Expected: index row present, paper_trades count matches prod.

---

## Out of Scope (explicit non-goals per spec)

- No retroactive close of open trades.
- No changes to BL-061 ladder legs, trail, or SL.
- No tightening of non-`first_signal` combos.
- No per-signal allowlist/weighting for the admission gate.
- No E2/E3 exit variants (BL-066/BL-067 track those separately).
- No score-decay exit axis (BL-067 contingent, gated on mid-hold coverage check).
