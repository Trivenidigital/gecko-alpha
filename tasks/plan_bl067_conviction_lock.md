# BL-067: Conviction-locked hold — production implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**New primitives introduced:** new column `signal_params.conviction_lock_enabled INTEGER NOT NULL DEFAULT 0` (added via `_migrate_signal_params_schema` extension, gated by `paper_migrations` row); new module `scout/trading/conviction.py` with `compute_stack(db, token_id, opened_at) -> int` + `conviction_locked_params(stack, base) -> SignalParams_subset` + helper `_count_stacked_signals_in_window` (consolidated from `scripts/backtest_conviction_lock.py:160-258` — single source of truth); new evaluator hook in `scout/trading/evaluator.py:evaluate_paper_trades` that overlays locked params for trades where `stack >= CONVICTION_LOCK_THRESHOLD AND signal_params.conviction_lock_enabled = 1`; new Settings field `PAPER_CONVICTION_LOCK_ENABLED: bool = False` (master kill-switch); new Settings field `PAPER_CONVICTION_LOCK_THRESHOLD: int = 3` (operator-tunable threshold); new structured log events `conviction_lock_armed`, `conviction_lock_skipped_disabled`, `conviction_lock_skipped_below_threshold`. NO new DB tables. Default fail-closed everywhere (column default 0; settings default False; threshold conservative).

**Prerequisites:** master ≥ `8c2ab32` (BL-067 backtest findings doc merged — gating evidence per `tasks/findings_bl067_backtest_conviction_lock.md`). Operator approval received via direct request.

## Hermes-first analysis

**Domains checked against the 671-skill hub at `hermes-agent.nousresearch.com/docs/skills` (verified 2026-05-04):**

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Real-time signal stack counting in trading systems | None found | Build inline (consolidate from existing `scripts/backtest_conviction_lock.py` helper) |
| Conviction-locked exit gating / dynamic exit-parameter overlay | None found (closest: MLOps category — model-evaluation, not trading) | Build inline (extend existing `scout/trading/evaluator.py` exit-logic state machine) |
| Per-signal feature flags / opt-in mechanism | None found (closest: `webhook-subscriptions` is event-delivery, not config-flag) | Build inline (reuse existing `signal_params.enabled` column pattern; add sibling `conviction_lock_enabled`) |

**Awesome-hermes-agent ecosystem check:** No relevant repos. Closest is `hxsteric/mercury` (multi-chain blockchain analyzer) — different problem (forensics vs. trading control plane).

**Verdict:** Pure project-internal trading-engine extension. No Hermes-skill replacement. Building inline by extending `scout/trading/evaluator.py` (existing exit state machine) + `signal_params` table (existing per-signal feature-flag pattern). The BL-067 backlog spec at `backlog.md:367-413` + the validated findings at `tasks/findings_bl067_backtest_conviction_lock.md` are the design authority.

---

## Drift grounding (per alignment doc Part 3)

**Read before drafting (verified):**

- `backlog.md:367-413` — BL-067 spec: per-stack params delta table (saturate at stack=4), 9 design questions resolved by findings doc, decision gate ≥10% PnL lift (passed at +114% per findings).
- `tasks/findings_bl067_backtest_conviction_lock.md` — gating evidence; recommends N=3 threshold + first_signal/gainers_early opt-in.
- `scripts/backtest_conviction_lock.py:160-258` — `_count_stacked_signals_in_window` helper. **Consolidation per D4/N7 (PR #68):** moving to `scout/trading/conviction.py` so production AND backtest share one implementation.
- `scripts/backtest_conviction_lock.py:218-242` — `conviction_locked_params(stack, base)` composer. Same consolidation.
- `scout/trading/evaluator.py:82-477` — `evaluate_paper_trades` exit state machine. The integration point is `params_for_signal(db, signal_type_row, settings)` at line 157 — overlay locked params AFTER this call returns the base SignalParams.
- `scout/trading/params.py:60-78` — `SignalParams` dataclass: `trail_pct, trail_pct_low_peak, sl_pct, max_duration_hours, enabled` (+5 calibration fields). Conviction lock adjusts `trail_pct, sl_pct, max_duration_hours` per the BL-067 table.
- `scout/db.py:1536-1614` — `_migrate_signal_params_schema` migration pattern: `BEGIN EXCLUSIVE`, idempotent `CREATE TABLE IF NOT EXISTS`, `paper_migrations` row gating, seed via `Settings` class defaults. **Pattern to follow exactly.**
- `scout/db.py:1599-1612` — `signal_params_audit` table — operator opt-in via SQL `UPDATE` should write an audit row (existing convention for operator changes).
- `scout/config.py` — Settings class. New fields: `PAPER_CONVICTION_LOCK_ENABLED`, `PAPER_CONVICTION_LOCK_THRESHOLD`. Both must have defaults (no `.env` requirement).
- BL-076 deploy lessons (`feedback_clear_pycache_on_deploy.md`): `find . -name __pycache__ -exec rm -rf {} +` mandatory after `git pull` for any deploy touching `scout/` Python.

**Pattern conformance:**
- New column on existing table via `ALTER TABLE ADD COLUMN` (matches BL-065 `cashtag_trade_eligible` pattern in `_migrate_feedback_loop_schema`).
- New module `scout/trading/conviction.py` (single responsibility — stack counting + param composition; consumed by both production evaluator and backtest script).
- Master kill-switch via Settings field defaults False — same shape as `PAPER_MOONSHOT_ENABLED` (BL-063).
- Operator opt-in via direct `UPDATE signal_params SET conviction_lock_enabled=1 WHERE signal_type IN ('first_signal', 'gainers_early')` — matches existing Tier 1a per-signal flip pattern.
- Default fail-closed: column DEFAULT 0; Settings.PAPER_CONVICTION_LOCK_ENABLED=False; deploy default unchanged behavior until operator explicitly opts in.

**Bug-evidence basis (from findings doc):**
- N=3 threshold: lift +114.4%, delta_vs_baseline +$7,222, delta_vs_actual +$11,219, locked_count=499 — **PASS compound gate**.
- LAB trade #711: simulated +$549.67 vs actual -$15.96 (operator's manual hypothetical was $531 — within $20).
- B2 first-entry hold: +$5,416 / +837.8% lift across 287 tokens — operator's mental model validated.
- 176 tokens hit N≥3 in 7d window over 30d (cohort overwhelmingly above the "10 = strong case" rubric).

---

**Goal:** Ship BL-067 production code such that operator can opt in `first_signal` + `gainers_early` to conviction-lock by SQL flip, with fail-closed default for all other signals (especially `narrative_prediction` pending its `--max-hours 720` re-run).

**Architecture:** Extend `scout/trading/evaluator.py` exit state machine with a single overlay pass before existing trail/sl/max_duration checks. The overlay (a) reads stack count via `compute_stack(db, token_id, opened_at)` (real-time, no caching — per backlog Q6 which the backtest validated as cheap enough at observed cardinality), (b) consults `signal_params.conviction_lock_enabled` for the trade's signal_type, (c) consults Settings.PAPER_CONVICTION_LOCK_ENABLED master kill, (d) if all gates pass and stack ≥ threshold, replaces the trade's effective `trail_pct, sl_pct, max_duration_hours` with the locked params per the BL-067 table; otherwise leaves base params unchanged.

**Tech Stack:** Python 3.12, async via aiosqlite, structlog, pytest + pytest-asyncio. No new dependencies.

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `scout/db.py` | Add migration step in `_migrate_signal_params_schema`: `ALTER TABLE signal_params ADD COLUMN conviction_lock_enabled INTEGER NOT NULL DEFAULT 0`; record `paper_migrations` row `bl067_conviction_lock_enabled` | Modify |
| `scout/config.py` | Add 2 Settings fields with defaults (kill-switch + threshold) + field validators | Modify |
| `scout/trading/conviction.py` | NEW MODULE: `compute_stack`, `conviction_locked_params`, `_count_stacked_signals_in_window` (consolidated from backtest helper) | Create |
| `scout/trading/params.py` | Add `conviction_lock_enabled: bool = False` to `SignalParams` dataclass; load it in `get_params()` | Modify |
| `scout/trading/evaluator.py` | Wire overlay call site after `params_for_signal` returns base params | Modify |
| `scripts/backtest_conviction_lock.py` | UPDATE to import shared helpers from `scout.trading.conviction` (replace local copies); keep run-time behavior identical | Modify |
| `scripts/backtest_v1_signal_stacking.py` | Optional: also import shared helper. **DEFER** — separate cleanup PR; this PR doesn't touch it | Skip |
| `tests/test_bl067_conviction_lock.py` | New test file — migration, helper, evaluator integration, fail-closed defaults, opt-in flow | Create |

---

## Tasks

### Task 1: Migration — `signal_params.conviction_lock_enabled` column

**Files:**
- Modify: `scout/db.py:_migrate_signal_params_schema` (extend with ALTER TABLE)
- Test: `tests/test_bl067_conviction_lock.py` (new file)

- [ ] **Step 1: Write the failing migration test**

```python
# tests/test_bl067_conviction_lock.py
"""BL-067: conviction-lock production tests.

Tests gated by SKIP_AIOHTTP_TESTS=1 on Windows where they touch aiohttp/
network paths (matches BL-076 + Bundle A pattern).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

_SKIP_AIOHTTP = pytest.mark.skipif(
    sys.platform == "win32" and os.environ.get("SKIP_AIOHTTP_TESTS") == "1",
    reason="Windows + SKIP_AIOHTTP_TESTS=1: skip aiohttp tests",
)

from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.initialize()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_conviction_lock_enabled_column_exists(db):
    """T1 — migration adds column with NOT NULL DEFAULT 0 (fail-closed)."""
    cur = await db._conn.execute("PRAGMA table_info(signal_params)")
    cols = {row[1]: (row[2], row[3], row[4]) for row in await cur.fetchall()}
    # (type, notnull, dflt_value)
    assert "conviction_lock_enabled" in cols
    coltype, notnull, default = cols["conviction_lock_enabled"]
    assert coltype == "INTEGER"
    assert notnull == 1
    assert default == "0"  # fail-closed default


@pytest.mark.asyncio
async def test_conviction_lock_enabled_paper_migrations_row(db):
    """T1b — `bl067_conviction_lock_enabled` recorded in paper_migrations."""
    cur = await db._conn.execute(
        "SELECT name FROM paper_migrations WHERE name = ?",
        ("bl067_conviction_lock_enabled",),
    )
    assert (await cur.fetchone()) is not None


@pytest.mark.asyncio
async def test_conviction_lock_enabled_default_zero_on_seeded_signals(db):
    """T1c — default fail-closed: ALL seeded signals have conviction_lock_enabled=0
    after migration, regardless of signal_type."""
    cur = await db._conn.execute(
        "SELECT signal_type, conviction_lock_enabled FROM signal_params"
    )
    rows = await cur.fetchall()
    assert len(rows) > 0  # seeded by migration
    for row in rows:
        assert row[1] == 0, (
            f"signal_type {row[0]!r} default conviction_lock_enabled "
            f"must be 0 (fail-closed); got {row[1]}"
        )
```

- [ ] **Step 2: Run test to verify it fails**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl067_conviction_lock.py -v --tb=short
```

Expected: FAIL with `assert "conviction_lock_enabled" in cols` (column doesn't exist).

- [ ] **Step 3: Add ALTER TABLE migration step**

In `scout/db.py:_migrate_signal_params_schema`, after the existing `signal_params_audit` index creation (around line 1612), BEFORE `await conn.execute("BEGIN EXCLUSIVE")` block ends:

Locate the existing `_migrate_signal_params_schema` function. Add a new step that ALTERs the table only if the column doesn't exist (idempotent). Use the same `BEGIN EXCLUSIVE` ... `COMMIT` pattern.

```python
# Inside _migrate_signal_params_schema, after the existing seed loop and
# BEFORE the function's final commit (find the existing structure and
# integrate this addition; the test-driven approach makes this concrete):

# BL-067 production: add conviction_lock_enabled column.
# Default 0 = fail-closed; operator opts in per signal_type via SQL.
cur = await conn.execute("PRAGMA table_info(signal_params)")
existing_cols = {row[1] for row in await cur.fetchall()}
if "conviction_lock_enabled" not in existing_cols:
    await conn.execute(
        "ALTER TABLE signal_params "
        "ADD COLUMN conviction_lock_enabled INTEGER NOT NULL DEFAULT 0"
    )
    await conn.execute(
        "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
        "VALUES (?, ?)",
        (
            "bl067_conviction_lock_enabled",
            datetime.now(timezone.utc).isoformat(),
        ),
    )
```

Also extend the post-migration assertion set (search for `bl065_cashtag_trade_eligible` — same shape) to include `bl067_conviction_lock_enabled`.

- [ ] **Step 4: Run test to verify it passes**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl067_conviction_lock.py -v --tb=short
```

Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add scout/db.py tests/test_bl067_conviction_lock.py
git commit -m "feat(BL-067): migration — signal_params.conviction_lock_enabled column (fail-closed default 0)"
```

---

### Task 2: Settings fields + field_validators

**Files:**
- Modify: `scout/config.py`
- Test: `tests/test_bl067_conviction_lock.py`

- [ ] **Step 1: Write failing tests for Settings**

```python
def test_settings_paper_conviction_lock_enabled_default_false():
    """T2 — master kill-switch defaults False (fail-closed)."""
    from scout.config import Settings
    s = Settings()
    assert s.PAPER_CONVICTION_LOCK_ENABLED is False


def test_settings_paper_conviction_lock_threshold_default_3():
    """T2b — threshold defaults to N=3 (per backtest findings)."""
    from scout.config import Settings
    s = Settings()
    assert s.PAPER_CONVICTION_LOCK_THRESHOLD == 3


def test_settings_paper_conviction_lock_threshold_must_be_at_least_two():
    """T2c — validator: threshold < 2 makes no sense (stack=1 = no signals
    fired AFTER the trade, can't be locked)."""
    import pytest
    from pydantic import ValidationError
    from scout.config import Settings
    with pytest.raises(ValidationError):
        Settings(PAPER_CONVICTION_LOCK_THRESHOLD=1)
    with pytest.raises(ValidationError):
        Settings(PAPER_CONVICTION_LOCK_THRESHOLD=0)
```

- [ ] **Step 2: Run tests to verify they fail**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl067_conviction_lock.py -k settings -v
```

Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'PAPER_CONVICTION_LOCK_ENABLED'`.

- [ ] **Step 3: Add Settings fields**

In `scout/config.py`, after the existing `PAPER_MOONSHOT_*` block (search for `PAPER_MOONSHOT_THRESHOLD_PCT`):

```python
    # BL-067 conviction-lock master kill-switch and threshold.
    # When False, evaluator NEVER applies locked params regardless of
    # per-signal opt-in. Operator must flip this AND
    # signal_params.conviction_lock_enabled=1 for a given signal_type to
    # activate locking. Default False = fail-closed.
    PAPER_CONVICTION_LOCK_ENABLED: bool = False
    PAPER_CONVICTION_LOCK_THRESHOLD: int = 3
```

Add field validator:

```python
    @field_validator("PAPER_CONVICTION_LOCK_THRESHOLD")
    @classmethod
    def _validate_conviction_lock_threshold(cls, v: int) -> int:
        if v < 2:
            raise ValueError(
                "PAPER_CONVICTION_LOCK_THRESHOLD must be >= 2 "
                f"(stack=1 means no independent signals fired; got {v})"
            )
        return v
```

- [ ] **Step 4: Run tests to verify they pass**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl067_conviction_lock.py -k settings -v
```

Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add scout/config.py tests/test_bl067_conviction_lock.py
git commit -m "feat(BL-067): Settings fields PAPER_CONVICTION_LOCK_ENABLED + _THRESHOLD"
```

---

### Task 3: `scout/trading/conviction.py` — shared module

**Files:**
- Create: `scout/trading/conviction.py`
- Modify: `scripts/backtest_conviction_lock.py` (replace local helpers with imports)
- Test: `tests/test_bl067_conviction_lock.py`

- [ ] **Step 1: Write failing tests for the shared module**

```python
def test_conviction_locked_params_table_matches_backlog_spec():
    """T3 — pins backlog.md:374-380 spec table.

    Uses base trail=20, sl=25, max=168 (post-BL-076 defaults)."""
    from scout.trading.conviction import conviction_locked_params

    base = {"max_duration_hours": 168, "trail_pct": 20.0, "sl_pct": 25.0}

    # stack=1 (no lock): defaults unchanged
    p = conviction_locked_params(stack=1, base=base)
    assert p["max_duration_hours"] == 168
    assert p["trail_pct"] == 20.0
    assert p["sl_pct"] == 25.0

    # stack=2: +72h, +5pp trail (cap 35), +5pp sl (cap 35)
    p = conviction_locked_params(stack=2, base=base)
    assert p["max_duration_hours"] == 240
    assert p["trail_pct"] == 25.0
    assert p["sl_pct"] == 30.0

    # stack=3: +168h, +10pp trail (cap 35), +10pp sl (cap 40)
    p = conviction_locked_params(stack=3, base=base)
    assert p["max_duration_hours"] == 336
    assert p["trail_pct"] == 30.0
    assert p["sl_pct"] == 35.0

    # stack>=4: +336h, +15pp trail (cap 35), +15pp sl (cap 40)
    p = conviction_locked_params(stack=4, base=base)
    assert p["max_duration_hours"] == 504
    assert p["trail_pct"] == 35.0  # cap
    assert p["sl_pct"] == 40.0


def test_conviction_locked_params_saturates_at_stack_4():
    """T3b — stack=10 returns same as stack=4."""
    from scout.trading.conviction import conviction_locked_params
    base = {"max_duration_hours": 168, "trail_pct": 20.0, "sl_pct": 25.0}
    p4 = conviction_locked_params(stack=4, base=base)
    p10 = conviction_locked_params(stack=10, base=base)
    assert p4 == p10


@pytest.mark.asyncio
async def test_compute_stack_returns_int(db):
    """T3c — compute_stack returns int >= 0; counts distinct sources."""
    from scout.trading.conviction import compute_stack
    now = datetime.now(timezone.utc).isoformat()
    # Seed minimal: a gainers_snapshot for token in window
    await db._conn.execute(
        "INSERT INTO gainers_snapshots "
        "(coin_id, symbol, name, price_change_24h, market_cap, volume_24h, "
        " price_at_snapshot, snapshot_at) "
        "VALUES ('test-coin', 'TEST', 'Test', 12.0, 5_000_000, 1_000, 1.0, ?)",
        (now,),
    )
    await db._conn.commit()
    n = await compute_stack(db, "test-coin", "2026-05-01T00:00:00+00:00")
    assert isinstance(n, int)
    assert n >= 1  # at least gainers source
```

- [ ] **Step 2: Run tests to verify they fail**

Expected: FAIL — module not importable.

- [ ] **Step 3: Create `scout/trading/conviction.py`**

```python
"""BL-067: Conviction-locked hold support.

Shared module for stack counting and locked-param composition. Used by:
- scout/trading/evaluator.py (production exit-logic overlay)
- scripts/backtest_conviction_lock.py (research backtest)

Per backlog.md:374-380 spec table:
- stack=1: defaults (no lock)
- stack=2: +72h max, +5pp trail (cap 35), +5pp sl (cap 35)
- stack=3: +168h max, +10pp trail (cap 35), +10pp sl (cap 40)
- stack>=4: +336h max, +15pp trail (cap 35), +15pp sl (cap 40)

Validated by tasks/findings_bl067_backtest_conviction_lock.md (lift
+114% at N=3 threshold, both compound gates PASS).
"""
from __future__ import annotations

import sqlite3
import structlog
from typing import Iterable

from scout.db import Database

log = structlog.get_logger()


_SIGNAL_SOURCES = [
    ("gainers_snapshots", "snapshot_at", "gainers", "coin_id"),
    ("losers_snapshots", "snapshot_at", "losers", "coin_id"),
    ("trending_snapshots", "snapshot_at", "trending", "coin_id"),
    ("chain_matches", "completed_at", "chains", "token_id"),
    ("predictions", "predicted_at", "narrative", "coin_id"),
    ("velocity_alerts", "detected_at", "velocity", "coin_id"),
    ("volume_spikes", "detected_at", "volume_spike", "coin_id"),
    ("tg_social_signals", "created_at", "tg_social", "token_id"),
]


# Module-level cache of which signal sources are missing from the DB
# (e.g., ran on a partial-rollback snapshot). First miss is logged once,
# subsequent calls skip the source. Other OperationalError types re-raise
# (matches BL-076 + BL-067 backtest defensive narrowing).
_signal_sources_missing: set[str] = set()


async def _table_exists(db: Database, table: str) -> bool:
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return (await cur.fetchone()) is not None


async def _count_stacked_signals_in_window(
    db: Database,
    token_id: str,
    opened_at: str,
    end_at: str,
) -> tuple[int, list[str]]:
    """Count DISTINCT signal-source firings on token_id within the window.

    Each source contributes at most 1 to the stack count. BIO/LAB principle:
    class diversity, not event volume. Per-table OperationalError is narrowed:
    table-missing is acceptable (cached + logged once); column-missing /
    other DB errors re-raise (operator must see real bugs).
    """
    sources: list[str] = []
    for table, ts_col, label, token_col in _SIGNAL_SOURCES:
        if table in _signal_sources_missing:
            continue
        if not await _table_exists(db, table):
            _signal_sources_missing.add(table)
            log.warning(
                "conviction_signal_source_missing",
                table=table,
                hint="stack count will not include contributions from this source",
            )
            continue
        try:
            cur = await db._conn.execute(
                f"""SELECT 1 FROM {table}
                    WHERE {token_col} = ?
                      AND datetime({ts_col}) >= datetime(?)
                      AND datetime({ts_col}) <= datetime(?)
                    LIMIT 1""",
                (token_id, opened_at, end_at),
            )
            if (await cur.fetchone()) is not None:
                sources.append(label)
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                f"OperationalError on {table}.{ts_col} "
                f"(column may have been renamed; surfaced rather than silently "
                f"continuing): {exc}"
            ) from exc

    # paper_trades distinct signal_types on same token (independent confirmation)
    if "paper_trades" not in _signal_sources_missing and await _table_exists(
        db, "paper_trades"
    ):
        try:
            cur = await db._conn.execute(
                """SELECT DISTINCT signal_type FROM paper_trades
                   WHERE token_id = ?
                     AND datetime(opened_at) >= datetime(?)
                     AND datetime(opened_at) <= datetime(?)""",
                (token_id, opened_at, end_at),
            )
            for r in await cur.fetchall():
                sources.append(f"trade:{r[0]}")
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                f"OperationalError on paper_trades stack scan: {exc}"
            ) from exc
    return len(sources), sources


# Per backlog.md:374-380 spec.
_CONVICTION_LOCK_DELTAS = {
    1: {"max_duration_hours": 0, "trail_pct": 0.0, "sl_pct": 0.0,
        "trail_cap": 35.0, "sl_cap": 25.0},
    2: {"max_duration_hours": 72, "trail_pct": 5.0, "sl_pct": 5.0,
        "trail_cap": 35.0, "sl_cap": 35.0},
    3: {"max_duration_hours": 168, "trail_pct": 10.0, "sl_pct": 10.0,
        "trail_cap": 35.0, "sl_cap": 40.0},
    4: {"max_duration_hours": 336, "trail_pct": 15.0, "sl_pct": 15.0,
        "trail_cap": 35.0, "sl_cap": 40.0},
}


def conviction_locked_params(stack: int, base: dict) -> dict:
    """Return base params with BL-067 conviction-lock deltas applied.
    Saturates at stack=4. Stack=1 returns base unchanged."""
    bucket = min(max(stack, 1), 4)
    delta = _CONVICTION_LOCK_DELTAS[bucket]
    return {
        "max_duration_hours": base["max_duration_hours"] + delta["max_duration_hours"],
        "trail_pct": min(base["trail_pct"] + delta["trail_pct"], delta["trail_cap"]),
        "sl_pct": min(base["sl_pct"] + delta["sl_pct"], delta["sl_cap"]),
    }


# Real-time stack window: [opened_at, opened_at + 504h] capped at "now"
# (M1 fix from BL-067 backtest plan v2). 504h = stack=4 max_duration ceiling.
# Per backlog Q6 the backtest validated computing this on every evaluator
# pass is cheap (~9 indexed SELECTs ≈ ms). No persistent column.
from datetime import datetime, timedelta, timezone
_MAX_LOCKED_HOURS = 504


async def compute_stack(
    db: Database, token_id: str, opened_at: str
) -> int:
    """Real-time stack count for a paper trade.

    Window: [opened_at, min(opened_at + 504h, now)] — matches the BL-067
    backtest M1 fix (capture signals that would have fired in the
    extended-lock window, not just the actual closed-trade window).

    Returns 0 for unknown tokens (caller treats stack=0 as no-lock-eligible).
    """
    if not token_id:
        return 0
    open_dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
    if open_dt.tzinfo is None:
        open_dt = open_dt.replace(tzinfo=timezone.utc)
    end_dt = min(
        open_dt + timedelta(hours=_MAX_LOCKED_HOURS),
        datetime.now(timezone.utc),
    )
    n, _ = await _count_stacked_signals_in_window(
        db, token_id, opened_at, end_dt.isoformat()
    )
    return n
```

- [ ] **Step 4: Run tests to verify they pass**

Expected: 3 PASS.

- [ ] **Step 5: Update `scripts/backtest_conviction_lock.py` to import shared helpers**

Replace the local `_count_stacked_signals_in_window` and `conviction_locked_params` definitions with:

```python
from scout.trading.conviction import (
    _count_stacked_signals_in_window as _count_stacked_signals_async,
    conviction_locked_params,
)
```

Wrap the async helper for the synchronous backtest script with a `_run_async()` adapter, OR (simpler) keep the sync version inlined in the backtest as a local helper but document with `# TODO: dedupe` — sync vs async impedance is real and the sync script is read-only research, so a separate-but-aligned implementation is acceptable. **Decision: keep duplicate for v1; both files reference each other in docstrings.** Cost of refactor exceeds benefit at this point.

(Skip — leave backtest unchanged; production module stands alone. TODO comment in `conviction.py` marks the reference.)

- [ ] **Step 6: Commit**

```bash
git add scout/trading/conviction.py tests/test_bl067_conviction_lock.py
git commit -m "feat(BL-067): scout/trading/conviction.py — compute_stack + conviction_locked_params"
```

---

### Task 4: SignalParams adds `conviction_lock_enabled` field

**Files:**
- Modify: `scout/trading/params.py:60-200`
- Test: `tests/test_bl067_conviction_lock.py`

- [ ] **Step 1: Write failing test**

```python
@pytest.mark.asyncio
async def test_get_params_loads_conviction_lock_enabled(db):
    """T4 — get_params reads conviction_lock_enabled from signal_params row."""
    from scout.config import Settings
    from scout.trading.params import get_params

    settings = Settings()
    # Default seed: all signal_types have conviction_lock_enabled=0
    sp = await get_params(db, "first_signal", settings)
    assert sp.conviction_lock_enabled is False

    # Operator opt-in via SQL UPDATE
    await db._conn.execute(
        "UPDATE signal_params SET conviction_lock_enabled = 1 "
        "WHERE signal_type = 'first_signal'"
    )
    await db._conn.commit()
    # bump_cache_version is the project's existing pattern for invalidating
    # the per-signal params cache after operator changes.
    from scout.trading.params import bump_cache_version
    bump_cache_version()
    sp = await get_params(db, "first_signal", settings)
    assert sp.conviction_lock_enabled is True
```

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL — `SignalParams` has no `conviction_lock_enabled` attribute.

- [ ] **Step 3: Modify `SignalParams` and `get_params`**

In `scout/trading/params.py:60-78` `SignalParams` dataclass, add field:

```python
@dataclass(frozen=True)
class SignalParams:
    signal_type: str
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
    conviction_lock_enabled: bool = False  # BL-067 — defaults False fail-closed
```

In `_settings_params` (line 98-114) add:

```python
    return SignalParams(
        signal_type=signal_type,
        ...
        enabled=True,
        conviction_lock_enabled=False,  # BL-067 — Settings has no per-signal flag
    )
```

In `get_params` SQL (line 156-184), extend the SELECT and the SignalParams construction:

```python
        cur = await db._conn.execute(
            """SELECT signal_type, leg_1_pct, leg_1_qty_frac, leg_2_pct, leg_2_qty_frac,
                      trail_pct, trail_pct_low_peak, low_peak_threshold_pct,
                      sl_pct, max_duration_hours, enabled, conviction_lock_enabled
               FROM signal_params WHERE signal_type = ?""",
            (signal_type,),
        )
        ...
        return SignalParams(
            signal_type=row[0],
            leg_1_pct=float(row[1]),
            ...
            enabled=bool(row[10]),
            conviction_lock_enabled=bool(row[11]),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scout/trading/params.py tests/test_bl067_conviction_lock.py
git commit -m "feat(BL-067): SignalParams.conviction_lock_enabled field + get_params loads it"
```

---

### Task 5: Evaluator overlay

**Files:**
- Modify: `scout/trading/evaluator.py:evaluate_paper_trades` after `params_for_signal` call
- Test: `tests/test_bl067_conviction_lock.py`

The overlay must:
1. Run AFTER `params_for_signal` returns `sp` (line 157)
2. Check 3 gates: `settings.PAPER_CONVICTION_LOCK_ENABLED`, `sp.conviction_lock_enabled`, `stack >= settings.PAPER_CONVICTION_LOCK_THRESHOLD`
3. If all pass: compute new (trail_pct, sl_pct, max_duration_hours) from `conviction_locked_params(stack, base)`
4. Build a NEW `SignalParams` with the overlaid values (frozen=True so we can't mutate)
5. Log `conviction_lock_armed` with stack count and locked-param values
6. Use the OVERLAID `sp` for the rest of the evaluator pass (`max_duration`, trail/sl checks)

- [ ] **Step 1: Write failing integration test**

```python
@pytest.mark.asyncio
async def test_evaluator_skips_conviction_lock_when_settings_kill_switch_off(db):
    """T5 — fail-closed: even with per-signal opt-in, master kill-switch
    OFF means no conviction-lock applied. Trade exits on base trail/sl."""
    from scout.config import Settings
    settings = Settings()
    assert settings.PAPER_CONVICTION_LOCK_ENABLED is False  # default
    # ... seed a trade where stack would be >= 3, then verify
    # the eval pass uses BASE max_duration (168h), not locked (336h+).
    # Implementation detail: spy/monkeypatch `conviction_locked_params`
    # and assert it was NOT called when kill-switch is False.
    pass  # Skeleton — fill during Build phase per design v2 test matrix


@pytest.mark.asyncio
async def test_evaluator_skips_conviction_lock_when_signal_not_opted_in(db):
    """T5b — fail-closed at signal level: kill-switch ON + 
    signal_params.conviction_lock_enabled=0 means no lock."""
    pass  # Skeleton


@pytest.mark.asyncio
async def test_evaluator_skips_conviction_lock_when_below_threshold(db):
    """T5c — stack=2 below threshold (default 3) → no lock applied."""
    pass  # Skeleton


@pytest.mark.asyncio
async def test_evaluator_arms_conviction_lock_when_all_gates_pass(db):
    """T5d — all 3 gates pass → locked params used. Asserts log event
    `conviction_lock_armed` with stack count and overlaid params."""
    pass  # Skeleton
```

(Skeletons because evaluator integration test setup is heavy — fill them out during Build phase per the design v2 test matrix. Pin event name + at least one numeric assertion.)

- [ ] **Step 2: Add overlay logic to evaluator**

In `scout/trading/evaluator.py`, immediately after `sp = await params_for_signal(db, signal_type_row, settings)` at line 157:

```python
            # BL-067 conviction-lock overlay. Three gates ALL must pass:
            # 1. Master kill-switch ON
            # 2. Per-signal opt-in (signal_params.conviction_lock_enabled=1)
            # 3. Stack count >= PAPER_CONVICTION_LOCK_THRESHOLD
            # All gates default fail-closed; production deploy is a no-op
            # until operator explicitly opts a signal_type in.
            if (
                settings.PAPER_CONVICTION_LOCK_ENABLED
                and sp.conviction_lock_enabled
            ):
                from scout.trading.conviction import (
                    compute_stack, conviction_locked_params,
                )
                stack = await compute_stack(db, token_id, str(row[3]))
                threshold = settings.PAPER_CONVICTION_LOCK_THRESHOLD
                if stack >= threshold:
                    locked = conviction_locked_params(
                        stack=stack,
                        base={
                            "max_duration_hours": sp.max_duration_hours,
                            "trail_pct": sp.trail_pct,
                            "sl_pct": sp.sl_pct,
                        },
                    )
                    # Replace sp with overlaid frozen dataclass
                    from dataclasses import replace
                    sp = replace(
                        sp,
                        max_duration_hours=locked["max_duration_hours"],
                        trail_pct=locked["trail_pct"],
                        sl_pct=locked["sl_pct"],
                    )
                    log.info(
                        "conviction_lock_armed",
                        trade_id=trade_id,
                        token_id=token_id,
                        signal_type=signal_type_row,
                        stack=stack,
                        threshold=threshold,
                        locked_trail_pct=sp.trail_pct,
                        locked_sl_pct=sp.sl_pct,
                        locked_max_duration_hours=sp.max_duration_hours,
                    )
                else:
                    log.info(
                        "conviction_lock_skipped_below_threshold",
                        trade_id=trade_id,
                        token_id=token_id,
                        stack=stack,
                        threshold=threshold,
                    )
```

`max_duration` is recomputed from the overlaid `sp` on line 158 — already in the existing code path. trail_pct and sl_pct flow into the existing trail/SL state machine via `sp.trail_pct` / `sp.sl_pct` references.

- [ ] **Step 3: Fill out test skeletons + run**

(Will be expanded in Build phase with concrete fixtures. Plan v1 leaves them as skeletons because evaluator integration test setup requires shared fixture infrastructure that's better-defined after design v2.)

- [ ] **Step 4: Commit**

```bash
git add scout/trading/evaluator.py tests/test_bl067_conviction_lock.py
git commit -m "feat(BL-067): evaluator conviction-lock overlay (3 gates, fail-closed default)"
```

---

### Task 6: Final regression sweep + push

- [ ] **Step 1: Run BL-067 test file**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl067_conviction_lock.py -v --tb=short
```

Expected: all PASS.

- [ ] **Step 2: Targeted regression**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_db.py tests/test_config.py tests/test_bl076_junk_filter_and_symbol_name.py -q --tb=short
```

Expected: all PASS (no regression in adjacent modules).

- [ ] **Step 3: Push**

```bash
git push origin feat/bl-067-conviction-lock-production
```

---

## Pre-merge audit (run BEFORE pushing to PR)

**Verify no signal_params row defaults to opted-in:**
```bash
ssh root@89.167.116.187 'sqlite3 /root/gecko-alpha/scout.db "SELECT signal_type, conviction_lock_enabled FROM signal_params"' > .ssh_pre_audit.txt
```
Expected: all 0. (This is the default seed; no migration applies opt-in.)

---

## Deploy verification (§5)

**Sequence (deploy-stop-FIRST per BL-076 plan v3 §5):**

0. **Pre-deploy backup:** `cp /root/gecko-alpha/scout.db /root/gecko-alpha/scout.db.bak.bl067.$(date +%s)` (ensure disk space cleaned per `feedback_vps_backup_rotation.md`).
0a. **Capture error baseline:** `BASELINE_ERR=$(journalctl -u gecko-pipeline --since "10 minutes ago" --no-pager | grep -ciE "error|exception|traceback") ; echo "baseline=$BASELINE_ERR" > /tmp/bl067_baseline.txt`.
1. **Stop pipeline service:** `systemctl stop gecko-pipeline`.
2. **Pull:** `cd /root/gecko-alpha && git pull origin master`.
3. **Clear pycache (lesson from BL-066'/BL-076 deploy):** `find . -name __pycache__ -type d -exec rm -rf {} +`.
4. **Start pipeline:** `systemctl start gecko-pipeline`.
5. **Service started cleanly:** `systemctl status gecko-pipeline` — active+running.
6. **Migration applied + fail-closed:**
   ```bash
   sqlite3 /root/gecko-alpha/scout.db \
     "SELECT name FROM paper_migrations WHERE name='bl067_conviction_lock_enabled'"
   sqlite3 /root/gecko-alpha/scout.db "PRAGMA table_info(signal_params)" | grep conviction_lock
   sqlite3 /root/gecko-alpha/scout.db \
     "SELECT signal_type, conviction_lock_enabled FROM signal_params"
   ```
   Expected: migration row present; column exists with INTEGER NOT NULL DEFAULT 0; ALL signal_types default to 0.
7. **No new exceptions vs baseline:**
   ```bash
   BASELINE_ERR=$(grep "^baseline=" /tmp/bl067_baseline.txt | cut -d= -f2)
   POST=$(journalctl -u gecko-pipeline --since "5 minutes ago" --no-pager | grep -ciE "error|exception|traceback")
   echo "post=$POST baseline=$BASELINE_ERR"
   [ "$POST" -le "$BASELINE_ERR" ] && echo "OK" || echo "REGRESSION: +$((POST - BASELINE_ERR))"
   ```
8. **No `conviction_lock_armed` events fired (kill-switch off + no signal opted in):**
   ```bash
   journalctl -u gecko-pipeline --since "10 minutes ago" --no-pager | \
     grep "conviction_lock" | head -5
   ```
   Expected: zero entries (because both gates default fail-closed).

## Operator opt-in (post-§5 verification, manually)

Once §5 verification clean, operator runs:

```bash
# Step A: enable the master kill-switch via .env (operator-side, no code change)
ssh root@89.167.116.187 'echo "PAPER_CONVICTION_LOCK_ENABLED=true" >> /root/gecko-alpha/.env'
ssh root@89.167.116.187 'systemctl restart gecko-pipeline'

# Step B: opt in first_signal + gainers_early
ssh root@89.167.116.187 'sqlite3 /root/gecko-alpha/scout.db "
  UPDATE signal_params
  SET conviction_lock_enabled = 1
  WHERE signal_type IN (\"first_signal\", \"gainers_early\");
  INSERT INTO signal_params_audit
    (signal_type, field_name, old_value, new_value, reason, applied_by, applied_at)
  VALUES
    (\"first_signal\", \"conviction_lock_enabled\", \"0\", \"1\",
     \"BL-067 conservative rollout per findings doc\",
     \"operator_manual\", datetime(\"now\")),
    (\"gainers_early\", \"conviction_lock_enabled\", \"0\", \"1\",
     \"BL-067 conservative rollout per findings doc\",
     \"operator_manual\", datetime(\"now\"));
"'
```

After the next eval cycle (~30 min), look for:

```bash
journalctl -u gecko-pipeline --since "5 minutes ago" --no-pager | \
  grep "conviction_lock_armed" | head -5
```

Expected: `conviction_lock_armed` events with `stack >= 3` for any open `first_signal` or `gainers_early` trade where the token has accumulated stacked signals.

## Revert path

**Hot-revert (operator-side, no code change):**
```bash
# Disable master kill-switch
ssh root@89.167.116.187 'sed -i "/^PAPER_CONVICTION_LOCK_ENABLED=/d" /root/gecko-alpha/.env'
ssh root@89.167.116.187 'systemctl restart gecko-pipeline'
```

**Code rollback (if a real bug surfaces):**
```bash
ssh root@89.167.116.187 'cd /root/gecko-alpha && systemctl stop gecko-pipeline && git checkout <prev-master-sha> && find . -name __pycache__ -exec rm -rf {} + && systemctl start gecko-pipeline'
```

The migration is forward-only (column persists post-revert; no functional impact since old code doesn't read it). The `signal_params.conviction_lock_enabled` column stays as residual schema; harmless.

---

## Self-Review

**1. Spec coverage:**
- Migration ✓ (Task 1)
- Settings fields + validators ✓ (Task 2)
- `scout/trading/conviction.py` shared module ✓ (Task 3)
- SignalParams field load ✓ (Task 4)
- Evaluator overlay ✓ (Task 5)
- Deploy + opt-in path ✓ (§5)
- 9 design questions resolved per findings doc ✓ (cross-referenced)

**2. Placeholder scan:** Task 5 has skeleton tests because evaluator integration setup is heavy and benefits from design v2 fixture decisions. Will be filled in Build phase per design test matrix. NO `[FILL IN]` style ambiguity — every step has either exact code or "skeleton; expand in Build phase per design v2".

**3. Type consistency:** `conviction_locked_params` returns `dict[str, ...]` consumed by evaluator's `dataclasses.replace(sp, ...)`. SignalParams adds `conviction_lock_enabled: bool = False`. Settings adds 2 fields with explicit types. All consistent.

**4. New primitives marker:** present at top with column / module / 2 Settings fields / 3 log events.

**5. Hermes-first marker:** present per convention. 3/3 negative.

**6. Drift grounding:** explicit refs to evaluator hot path, SignalParams shape, migration pattern, BL-076 deploy lesson.

**7. TDD discipline:** failing-test → impl → passing-test → commit per task. Skeletons for Task 5 documented as intentional with rationale.

**8. No production code that auto-arms:**
- Migration column DEFAULT 0
- Settings.PAPER_CONVICTION_LOCK_ENABLED default False
- Settings.PAPER_CONVICTION_LOCK_THRESHOLD default 3 (conservative; backtest proved both N=2 and N=3 PASS but N=3 is safer)
- Three-layer fail-closed: Settings master + per-signal opt-in + stack threshold.

**9. Honest scope:**
- **NOT in scope:** dashboard surface (`conviction_stack_count` badge on open positions). Defer to BL-067-dashboard follow-up — this PR is the trading-engine integration only.
- **NOT in scope:** narrative_prediction `--max-hours 720` re-run — operator runs that BEFORE flipping `conviction_lock_enabled=1` for narrative_prediction.
- **NOT in scope:** consolidating `_count_stacked_signals_in_window` from `scripts/backtest_conviction_lock.py` (sync vs async impedance + research-script-not-touched policy). The new module in `scout/trading/conviction.py` is async; the backtest stays sync. TODO comment added.
- **DELIBERATELY DEFERRED:** dynamic threshold calibration — operator can change PAPER_CONVICTION_LOCK_THRESHOLD via `.env` but the backtest validated N=3 specifically; lowering to N=2 should be a separate operator decision with re-run.
- **DELIBERATELY DEFERRED:** stack-count caching — backtest validated real-time computation is cheap (~9 indexed SELECTs ≈ ms). Persist only if profiling shows the eval-loop hot path.
- **DELIBERATELY DEFERRED:** conviction_stack downgrade on inactivity — once locked, stays locked through trade life (per backlog Q9 + simpler).

**10. Soak-then-escalate criterion:** monitor `conviction_lock_armed` events daily for 14 days after operator opts in `first_signal` + `gainers_early`. If no regressions (no unusual `trading_open_*_error` events, no PnL delta from previous 14d baseline beyond expected variance), then operator can proceed to flip the next signal_type (`losers_contrarian`, `volume_spike`, `chain_completed`, then narrative_prediction after 720h re-run).
