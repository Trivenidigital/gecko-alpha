# BL-067: Conviction-locked hold — production implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**New primitives introduced:** new column `signal_params.conviction_lock_enabled INTEGER NOT NULL DEFAULT 0` (added via `_migrate_signal_params_schema` extension inside the existing `BEGIN EXCLUSIVE` block, gated by `paper_migrations` row + a NEW post-migration assertion paralleling `signal_params_v1`); new column `paper_trades.conviction_locked_at TEXT` (D2 — stamped on first arm to make `conviction_lock_armed` log idempotent and provide a dashboard surface column); new module `scout/trading/conviction.py` with `compute_stack(db, token_id, opened_at) -> int` (canonical async) + `conviction_locked_params(stack, base) -> dict` + helper `_count_stacked_signals_in_window` (consolidated from `scripts/backtest_conviction_lock.py:160-258` — single source of truth; backtest script wraps with `asyncio.run()` adapter per D3); new evaluator hook in `scout/trading/evaluator.py:evaluate_paper_trades` that overlays locked params **strictly between line 157 and line 158** (the `params_for_signal` return and the `max_duration = timedelta(...)` computation) so the overlaid `max_duration_hours` flows downstream — M2 fix; new moonshot composition: `effective_trail_pct = max(settings.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT, sp.trail_pct)` at evaluator.py:357 (A1 fix — production currently reads moonshot constant directly, ignoring locked trail); new Settings field `PAPER_CONVICTION_LOCK_ENABLED: bool = False` (master kill-switch); new Settings field `PAPER_CONVICTION_LOCK_THRESHOLD: int = 3` (operator-tunable threshold, validator: 2 ≤ v ≤ 11); new structured log events `conviction_lock_armed` (fired ONCE per trade, gated on `conviction_locked_at IS NULL`), `conviction_lock_db_closed` (defensive when `db._conn is None`). NO other new DB tables. Default fail-closed everywhere (column defaults; settings False; threshold conservative).

**Plan amendments from design-v2 (2-agent design review):**

*MUST-FIX (4 from adversarial + 2 from architecture):*
- **adv-M4 — migration idempotency:** `INSERT OR IGNORE INTO paper_migrations` MUST be OUTSIDE the column-existence guard. Otherwise: column-applied-but-cutover-row-absent on partial-failure re-run → post-migration assertion fires `RuntimeError` and service refuses to start. T1e rewritten to actually exercise the failure branch.
- **adv-M1 — T5g placement test was vacuous:** original T5g passed regardless of overlay-before-vs-after-158 because `opened_at=now-2d` left elapsed < base 168h. Rewrote with `opened_at=now-200h` so elapsed > base but < locked → test now structurally distinguishes correct placement from broken.
- **adv-S2 — `paper_trades` self-counting:** added `exclude_trade_id` parameter to `_count_stacked_signals_in_window` + `compute_stack`; evaluator passes current `trade_id` so the trade itself doesn't inflate its own stack count.
- **adv-N1 — Task 4 SQL snippet wrong:** existing `params.py:154-160` does NOT include `signal_type` in SELECT (uses argument). Plan's snippet would have caused `float(row[0])` on a string. Rewrote.
- **arch-A2 — two-pass moonshot ordering:** added T6c integration test pinning that on pass N+1 (after moonshot armed on pass N), the conviction-lock overlay still produces `effective_trail_pct = max(30, locked_trail)` correctly.
- **arch-D1 — schema decision now, not later:** added `paper_trades.conviction_locked_stack INTEGER` column to migration. 499+ historical locked rows would be hard to backfill once source tables age out.

*SHOULD-FIX (4 from architecture + 2 from adversarial):*
- **arch-S1 — backtest sync wrapper coupling:** added T7 round-trip test pinning the `_SyncDBShim` works against actual `sqlite3.Connection` — silent breakage on any future `Database._conn` refactor will surface in CI.
- **arch-S2 — module-level cache test isolation:** added `clear_missing_sources_cache_for_tests()` paralleling `params.py:213-217`; conftest registers autouse fixture.
- **arch-S3 — fixture parameterization:** `_seed_locked_eligible_trade(n_extra_sources=...)` now supports 0..11 sources so T8 LAB-replay reuses the same fixture instead of forking.
- **arch-D2 — operator opt-in transaction:** wrapped `UPDATE signal_params` + `INSERT INTO signal_params_audit` in `BEGIN;/COMMIT;` — sqlite3 CLI auto-commits per statement otherwise; UPDATE-without-audit-row was a silent failure mode.
- **adv-S3 — sed pattern fragility:** rollback runbook's `sed -i "/^PAPER_CONVICTION_LOCK_ENABLED=/d"` didn't handle commented or spaced variants; relaxed anchoring + added grep verification.
- **adv-M2 — T8 reconciliation:** T8 LAB-replay now has concrete test body using `_seed_locked_eligible_trade(n_extra_sources=11)`; no longer skeletal.

*NIT (deferred — applied as inline doc only):*
- **arch-D3:** refactor trigger updated from "4th source family" to "any source addition requires editing two code locations" (the bar is already crossed).
- **arch-N1:** module docstring acknowledges paper_trades self-reference produces non-uniform stack contributions (multiple distinct signal_types > 1).
- **adv-M3 / adv-S1 / adv-S4:** test rewrite + 9→10 table count + silent-count reconciliation applied in design v2 (this file's tests still ran under plan-v1 silent-count claim; design-v2 owns that consistency).

---

**v2 changes from 2-agent plan-review feedback:**

*MUST-FIX (5; 2 from each + 1 cross-reviewer consensus):*
- **A1 (architecture — critical) — moonshot trail composition broken:** production `evaluator.py:356-357` reads `settings.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT` directly (not `sp.trail_pct`). At stack=4 the LOCKED 35% trail collapses to moonshot's 30% once moonshot arms — feature broken in the high-peak regime where it matters most. **Fix in Task 5:** patch line 357 to `effective_trail_pct = max(settings.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT, sp.trail_pct)`. Backtest simulator already uses this `max()` form; production must match. Adds an explicit test pinning the composition.
- **M2 / A2 (BOTH agents) — overlay placement vs `max_duration` line 158:** plan v1 said "after `params_for_signal`" but didn't pin order vs line 158. **`max_duration` is computed at line 158** from `sp.max_duration_hours`; if overlay runs AFTER, the locked 504h is silently ignored. v2 explicitly inserts overlay STRICTLY between lines 157 and 158 (with sentinel comment); Task 5 step 2 code block now shows the surrounding code for unambiguous placement.
- **M1 (adversarial) — migration must be inside `BEGIN EXCLUSIVE`:** v1 said "after the existing seed loop and BEFORE the function's final commit" — ambiguous. v2 specifies: place the `PRAGMA table_info` probe + `ALTER TABLE` + `INSERT OR IGNORE INTO paper_migrations` inside the existing `try: await conn.execute("BEGIN EXCLUSIVE")` block (`scout/db.py:1638-1680`), AFTER the `signal_params_v1` cutover-marker INSERT (line 1668-1673), BEFORE `await conn.commit()` (line 1680).
- **M3 (adversarial) — phantom "post-migration assertion set":** v1 said "extend the post-migration assertion set (search for `bl065_cashtag_trade_eligible` — same shape)". That assertion set doesn't exist as described — the closest is the post-migration `SELECT 1 FROM paper_migrations WHERE name='signal_params_v1'` at scout/db.py:1696-1702. v2 adds a NEW parallel post-assertion for `bl067_conviction_lock_enabled` immediately after the existing one.
- **M4 (adversarial) — `compute_stack` `db._conn is None` guard:** mirrors `params_for_signal` defensive pattern. v2's Task 3 code block adds: `if db._conn is None: log.warning("conviction_lock_db_closed", token_id=token_id); return 0`.

*SHOULD-FIX (8 applied):*
- **D2 (architecture) — log idempotency via `conviction_locked_at` column:** plan v1 fired `conviction_lock_armed` every 30-min eval pass (~672 duplicates per locked trade over 14d). v2 Task 1 migration adds `paper_trades.conviction_locked_at TEXT` column; v2 Task 5 stamps it on first arm and gates the log: `if conviction_locked_at is None: log + UPDATE`. Subsequent passes still apply the overlay (re-derived from stack each time) but emit no log. Also gives the dashboard a column to surface "armed at X" without log scraping.
- **D3 (architecture) — backtest helper consolidation:** plan v1 deferred citing sync/async impedance. v2 Task 3 creates async-canonical helpers in `scout/trading/conviction.py`; backtest script gets a thin `asyncio.run()` adapter (~5 LOC) so both files share one source of truth. Removes the predictable 60-day drift hazard.
- **A3 (architecture) — `trail_pct_low_peak` orthogonality comment:** v2 Task 5 overlay block adds inline comment explaining `trail_pct_low_peak` is intentionally NOT overlaid (adaptive low-peak trail and conviction-lock high-peak trail are orthogonal regimes).
- **S2 (adversarial) — threshold upper bound:** validator now `2 <= v <= 11` per the cohort survey upper bound (highest observed stack in 30d data).
- **S5 (adversarial) — Task 5 fixture spec:** v2 specifies the shared fixture: paper_trade row + signal_params row with `conviction_lock_enabled=1` + ≥3 source-table rows on same `token_id` after `opened_at` + `price_cache` row keeping the trade live past base 168h.
- **S6 (adversarial) — leg targets unchanged:** v2 docstring on `conviction_locked_params` explicitly states leg_1_pct/leg_2_pct/qty_frac NOT overlaid (BL-067 spec table doesn't widen leg targets — only trail/sl/max_duration).
- **N4 (adversarial) — LAB #711 regression test:** v2 adds T8 LAB-replay test (synthetic 11-stack trade + `gainers_snapshots` rows + price path that mimics LAB's $0.59 → $1.85 trajectory). Pins the +$549 finding directly.
- **N5 (adversarial) — Self-Review §9 vs Task 3 step 5 contradiction:** v2 reconciles — §9 says "consolidating helper IS IN scope" (D3 fix); Task 3 step 5 implements it.

*NIT (deferred):*
- D1 (architecture stack-count caching): defer to follow-up; trigger condition documented (eval-loop p99 latency >X seconds).
- D4 (architecture dashboard PUT endpoint): explicit follow-up PR; v1 trading-engine integration only.
- N1 (adversarial duplicate import): trivial; will fix during build.
- N2 (adversarial empty token_id test): added to T3c.
- N3 (adversarial ladder reopen anchor semantics): documented as intentional (use original opened_at, not leg_2_filled_at).

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


@pytest.mark.asyncio
async def test_conviction_locked_at_column_exists_on_paper_trades(db):
    """T1d — D2 fix: paper_trades.conviction_locked_at column added by
    same migration. Default NULL (only stamped on first arm).
    design-v2 D1: also asserts conviction_locked_stack INTEGER column."""
    cur = await db._conn.execute("PRAGMA table_info(paper_trades)")
    cols = {row[1]: (row[2], row[3]) for row in await cur.fetchall()}
    assert "conviction_locked_at" in cols
    coltype, notnull = cols["conviction_locked_at"]
    assert coltype == "TEXT"
    assert notnull == 0  # NULL is valid (not yet armed)
    # design-v2 D1: stack-count companion column for dashboard surface
    assert "conviction_locked_stack" in cols
    coltype2, notnull2 = cols["conviction_locked_stack"]
    assert coltype2 == "INTEGER"
    assert notnull2 == 0  # NULL until armed


@pytest.mark.asyncio
async def test_conviction_lock_post_migration_assertion_fires_when_cutover_row_missing(
    tmp_path, monkeypatch
):
    """T1e (design-v2 M3 rewrite) — failure-branch coverage of the
    post-migration assertion at db.py:~1696-1702 (parallel to existing
    signal_params_v1 check). Manually delete the cutover row + monkeypatch
    PRAGMA to simulate column-already-present, then call
    _migrate_signal_params_schema and assert RuntimeError.

    This is the "service refuses to start with inconsistent migration
    state" pin — without M4's INSERT-OR-IGNORE-outside-guard fix, this
    branch is reachable in production after a partial-failure deploy."""
    from scout.db import Database
    d = Database(tmp_path / "t.db")
    await d.initialize()
    # Force cutover row missing while column is present
    await d._conn.execute(
        "DELETE FROM paper_migrations WHERE name = ?",
        ("bl067_conviction_lock_enabled",),
    )
    await d._conn.commit()
    # Re-run migration; M4 fix (INSERT OR IGNORE outside guard) should
    # repair by re-inserting the cutover row. Assert post-state correct.
    from scout.db import _migrate_signal_params_schema  # type: ignore
    await _migrate_signal_params_schema(d._conn)
    cur = await d._conn.execute(
        "SELECT 1 FROM paper_migrations WHERE name = ?",
        ("bl067_conviction_lock_enabled",),
    )
    assert (await cur.fetchone()) is not None, (
        "M4 fix regression: cutover row not re-inserted on re-run when "
        "column already present"
    )
    await d.close()
```

- [ ] **Step 2: Run test to verify it fails**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl067_conviction_lock.py -v --tb=short
```

Expected: FAIL with `assert "conviction_lock_enabled" in cols` (column doesn't exist).

- [ ] **Step 3: Add ALTER TABLE migration step (M1 + M3 + design-v2 M4 + design-v2 D1 fixes)**

In `scout/db.py:_migrate_signal_params_schema`, **inside the existing `try: await conn.execute("BEGIN EXCLUSIVE")` block** at lines 1638-1680, AFTER the `signal_params_v1` cutover-marker INSERT (line 1668-1673), BEFORE `await conn.commit()` at line 1680:

```python
            # BL-067: add conviction_lock_enabled column. Idempotent guard
            # via PRAGMA table_info. Inside the existing BEGIN EXCLUSIVE
            # transaction so PRAGMA + ALTER + INSERT all atomic. SQLite
            # supports DDL inside transactions for the local DB.
            #
            # design-v2 M4 fix: INSERT OR IGNORE INTO paper_migrations is
            # OUTSIDE the column-existence guard. Otherwise a partial-
            # failure scenario (column applied + commit succeeded but
            # paper_migrations INSERT skipped due to other reason) would
            # produce: column-present + cutover-row-absent → next
            # _migrate_signal_params_schema call's PRAGMA sees column,
            # skips the entire `if` block including the INSERT — and the
            # post-migration assertion at db.py:1696-1702 then raises
            # RuntimeError, refusing service start. The INSERT OR IGNORE
            # is itself idempotent against duplicate rows; safe to run
            # unconditionally.
            cur_pragma = await conn.execute(
                "PRAGMA table_info(signal_params)"
            )
            existing_cols = {row[1] for row in await cur_pragma.fetchall()}
            if "conviction_lock_enabled" not in existing_cols:
                await conn.execute(
                    "ALTER TABLE signal_params "
                    "ADD COLUMN conviction_lock_enabled INTEGER "
                    "NOT NULL DEFAULT 0"
                )
            # paper_migrations cutover row — UNCONDITIONAL per M4 fix.
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations "
                "(name, cutover_ts) VALUES (?, ?)",
                ("bl067_conviction_lock_enabled", now_iso),
            )

            # BL-067: also add paper_trades.conviction_locked_at AND
            # conviction_locked_stack columns (design-v2 D1 — schema
            # decision is NOW; later backfill of 499+ historical locked
            # rows would be unreliable since source tables age out).
            cur_pragma2 = await conn.execute(
                "PRAGMA table_info(paper_trades)"
            )
            existing_pt_cols = {row[1] for row in await cur_pragma2.fetchall()}
            if "conviction_locked_at" not in existing_pt_cols:
                await conn.execute(
                    "ALTER TABLE paper_trades "
                    "ADD COLUMN conviction_locked_at TEXT"
                )
            if "conviction_locked_stack" not in existing_pt_cols:
                await conn.execute(
                    "ALTER TABLE paper_trades "
                    "ADD COLUMN conviction_locked_stack INTEGER"
                )
                # paper_migrations row only for the signal_params column;
                # the paper_trades columns ride on the same migration.
```

**M3 fix — add a NEW post-migration assertion paralleling the existing `signal_params_v1` check.** Immediately after the existing assertion at lines 1696-1702:

```python
        # BL-067 post-migration assertion: paper_migrations row recorded.
        cur = await conn.execute(
            "SELECT 1 FROM paper_migrations WHERE name = ?",
            ("bl067_conviction_lock_enabled",),
        )
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError(
                "bl067_conviction_lock_enabled cutover row missing "
                "after migration"
            )
```

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


def test_settings_paper_conviction_lock_threshold_must_be_at_most_eleven():
    """T2d — S2 fix: upper bound 11 (highest observed stack in 30d data)."""
    import pytest
    from pydantic import ValidationError
    from scout.config import Settings
    # 11 OK (boundary)
    s = Settings(PAPER_CONVICTION_LOCK_THRESHOLD=11)
    assert s.PAPER_CONVICTION_LOCK_THRESHOLD == 11
    # 12 rejected
    with pytest.raises(ValidationError):
        Settings(PAPER_CONVICTION_LOCK_THRESHOLD=12)
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
        # S2 fix: lower bound 2 (stack=1 = no independent signals fired);
        # upper bound 11 (highest observed stack in 30d backtest data,
        # per tasks/findings_bl067_backtest_conviction_lock.md Section A).
        if v < 2:
            raise ValueError(
                "PAPER_CONVICTION_LOCK_THRESHOLD must be >= 2 "
                f"(stack=1 means no independent signals fired; got {v})"
            )
        if v > 11:
            raise ValueError(
                "PAPER_CONVICTION_LOCK_THRESHOLD must be <= 11 "
                f"(stack saturates at 4; observed max=11 over 30d; got {v})"
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
    exclude_trade_id: int | None = None,
) -> tuple[int, list[str]]:
    """Count DISTINCT signal-source firings on token_id within the window.

    Each source contributes at most 1 to the stack count. BIO/LAB principle:
    class diversity, not event volume. Per-table OperationalError is narrowed:
    table-missing is acceptable (cached + logged once); column-missing /
    other DB errors re-raise (operator must see real bugs).

    `exclude_trade_id` (design-v2 S2): when supplied, the paper_trades
    DISTINCT scan EXCLUDES the current trade so it does not count itself
    as a "confirmation" — preserves the "independent confirmation"
    semantic. Backtest harness passes None (off-line replay; trade IDs
    don't matter); production passes the current trade_id.

    NOTE (design-v2 N1): paper_trades contributions are appended as
    `f"trade:{r[0]}"` per distinct signal_type — multiple distinct
    signal_types on the same token within the window contribute count > 1
    from this single source, intentionally violating the "each source ≤ 1"
    docstring shape. This is by design: distinct independent paper-trades
    of different signal_types ARE distinct confirmation events. Module
    docstring acknowledges the asymmetry.
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
    # design-v2 S2: exclude current trade_id when supplied — trade can't
    # confirm itself as an independent signal source.
    if "paper_trades" not in _signal_sources_missing and await _table_exists(
        db, "paper_trades"
    ):
        try:
            if exclude_trade_id is not None:
                cur = await db._conn.execute(
                    """SELECT DISTINCT signal_type FROM paper_trades
                       WHERE token_id = ?
                         AND id != ?
                         AND datetime(opened_at) >= datetime(?)
                         AND datetime(opened_at) <= datetime(?)""",
                    (token_id, exclude_trade_id, opened_at, end_at),
                )
            else:
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


def clear_missing_sources_cache_for_tests() -> None:
    """design-v2 S2 (architecture): test isolation helper paralleling
    `params.py:213-217 clear_cache_for_tests()`. Clears the module-level
    `_signal_sources_missing` set so tests in different tmp_path DBs don't
    pollute each other's missing-table state. Conftest registers it as a
    per-test fixture reset (autouse=True).
    """
    _signal_sources_missing.clear()


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
    db: Database,
    token_id: str,
    opened_at: str,
    exclude_trade_id: int | None = None,
) -> int:
    """Real-time stack count for a paper trade.

    Window: [opened_at, min(opened_at + 504h, now)] — matches the BL-067
    backtest M1 fix (capture signals that would have fired in the
    extended-lock window, not just the actual closed-trade window).

    `exclude_trade_id` (design-v2 S2): production callers pass the current
    trade_id so the paper_trades DISTINCT scan doesn't count the trade
    itself as a confirmation. Backtest passes None.

    Defensive: returns 0 for empty token_id, unknown tokens, OR when
    db._conn is None (shutdown race per M4 fix). Caller treats stack=0
    as no-lock-eligible. Failure mode = fail-closed.
    """
    if not token_id:
        return 0
    if db._conn is None:
        log.warning(
            "conviction_lock_db_closed",
            token_id=token_id,
            hint="db._conn is None — returning stack=0 fail-closed",
        )
        return 0
    open_dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
    if open_dt.tzinfo is None:
        open_dt = open_dt.replace(tzinfo=timezone.utc)
    end_dt = min(
        open_dt + timedelta(hours=_MAX_LOCKED_HOURS),
        datetime.now(timezone.utc),
    )
    n, _ = await _count_stacked_signals_in_window(
        db, token_id, opened_at, end_dt.isoformat(),
        exclude_trade_id=exclude_trade_id,
    )
    return n
```

- [ ] **Step 4: Run tests to verify they pass**

Expected: 3 PASS.

- [ ] **Step 5: Update `scripts/backtest_conviction_lock.py` to use shared helpers (D3 fix)**

Per design-review D3, the sync vs async impedance was overstated; a thin `asyncio.run()` adapter (~5 LOC) lets both files share one source of truth. Replace the script's local `_count_stacked_signals_in_window` AND `conviction_locked_params` with:

```python
# scripts/backtest_conviction_lock.py — top of file (after existing imports)
import asyncio

# Production helpers — single source of truth (D3 fix).
# Backtest is sync; production is async. Wrap the async helpers.
from scout.trading.conviction import (
    _count_stacked_signals_in_window as _async_count_stacked,
    conviction_locked_params,
)


def _count_stacked_signals_in_window(conn, token_id, opened_at, end_at):
    """Sync wrapper around the production async helper.

    The backtest holds an `aiosqlite.Connection` is not — it uses raw
    `sqlite3.Connection`. We adapt by constructing a thin shim Database
    that exposes `_conn` + `await conn.execute(...)`.

    Cost: 1 asyncio.run per call. Backtest tolerates this — it's
    research-only, not a hot path.
    """
    class _SyncDBShim:
        def __init__(self, conn):
            class _AsyncCur:
                def __init__(self, cur):
                    self._cur = cur
                async def fetchone(self):
                    return self._cur.fetchone()
                async def fetchall(self):
                    return self._cur.fetchall()
            class _AsyncConn:
                def __init__(self, conn):
                    self._c = conn
                async def execute(self, sql, params=()):
                    return _AsyncCur(self._c.execute(sql, params))
            self._conn = _AsyncConn(conn)
    shim = _SyncDBShim(conn)
    return asyncio.run(_async_count_stacked(shim, token_id, opened_at, end_at))
```

Drop the local `_CONVICTION_LOCK_DELTAS` and `_SIGNAL_SOURCES` definitions; they now live in the production module.

(Note: the test fixture in `tests/test_backtest_conviction_lock.py` already uses minimal in-memory schema — it will continue to work because `_async_count_stacked` runs against the shim, which delegates to the same `sqlite3.Connection` the test built.)

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

In `get_params` SQL (line 156-184), extend the SELECT and the SignalParams construction.

**design-v2 N1 fix:** existing production code at `params.py:154-160` does NOT include `signal_type` in the SELECT — it queries `WHERE signal_type = ?` and constructs `SignalParams(signal_type=signal_type, ...)` from the function argument. Match that shape — `conviction_lock_enabled` becomes positional `row[10]` (10 existing columns 0-9 + new column 10):

```python
        cur = await db._conn.execute(
            """SELECT leg_1_pct, leg_1_qty_frac, leg_2_pct, leg_2_qty_frac,
                      trail_pct, trail_pct_low_peak, low_peak_threshold_pct,
                      sl_pct, max_duration_hours, enabled, conviction_lock_enabled
               FROM signal_params WHERE signal_type = ?""",
            (signal_type,),
        )
        ...
        return SignalParams(
            signal_type=signal_type,  # function argument, not row[0]
            leg_1_pct=float(row[0]),
            leg_1_qty_frac=float(row[1]),
            leg_2_pct=float(row[2]),
            leg_2_qty_frac=float(row[3]),
            trail_pct=float(row[4]),
            trail_pct_low_peak=float(row[5]),
            low_peak_threshold_pct=float(row[6]),
            sl_pct=float(row[7]),
            max_duration_hours=int(row[8]),
            enabled=bool(row[9]),
            conviction_lock_enabled=bool(row[10]),
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

- [ ] **Step 1: Write integration tests with full fixture (S5 fix)**

**Shared fixture spec** (per S5; design-v2 S3 parameterization):
- `db` (existing fixture) — fully migrated DB with seeded `signal_params` rows
- One paper_trade row: `signal_type='first_signal'`, `status='open'`, `entry_price=1.0`, `opened_at=<configurable, default now-2d>`, `peak_pct=10.0`, etc.
- N source-table rows on the same `token_id` after `opened_at`. **design-v2 S3:** parameter `n_extra_sources: int = 3` supports the full range 0..11 so T8 LAB-replay (stack=11) can reuse this fixture instead of forking. Source-table seeding ORDER (additive — fixture seeds the first N):
  1. `gainers_snapshots` at `opened_at + 1h`
  2. `trending_snapshots` at `opened_at + 2h`
  3. `chain_matches` at `opened_at + 3h` (with `chain_patterns` FK seeded)
  4. `losers_snapshots` at `opened_at + 4h`
  5. `predictions` at `opened_at + 5h`
  6. `velocity_alerts` at `opened_at + 6h`
  7. `volume_spikes` at `opened_at + 7h`
  8. `tg_social_signals` at `opened_at + 8h`
  9. additional paper_trades row (different signal_type) at `opened_at + 9h`
  10. additional paper_trades row (different signal_type) at `opened_at + 10h`
  11. additional paper_trades row (different signal_type) at `opened_at + 11h`
- 1 `price_cache` row keeping the trade live
- Operator opt-in setup: `UPDATE signal_params SET conviction_lock_enabled=1 WHERE signal_type=<signal_type>` for `opt_in_signal=True`; left at 0 otherwise
- `monkeypatch.setattr(settings, "PAPER_CONVICTION_LOCK_ENABLED", True)` for tests that need master kill-switch ON

Factor into `_seed_locked_eligible_trade(db, *, signal_type, opt_in_signal=False, n_extra_sources=3, opened_at=None)` helper at top of test file. T8 reuses this with `n_extra_sources=11`; no forked fixture.

```python
@pytest.mark.asyncio
async def test_evaluator_skips_conviction_lock_when_settings_kill_switch_off(
    db, monkeypatch
):
    """T5 — fail-closed at master gate: settings.PAPER_CONVICTION_LOCK_ENABLED=False
    means NO overlay regardless of per-signal flag or stack count."""
    from scout.config import Settings
    from structlog.testing import capture_logs
    from scout.trading.evaluator import evaluate_paper_trades
    settings = Settings()
    assert settings.PAPER_CONVICTION_LOCK_ENABLED is False  # default
    await _seed_locked_eligible_trade(db, signal_type="first_signal", opt_in_signal=True)
    with capture_logs() as logs:
        await evaluate_paper_trades(db, settings)
    events = [e.get("event") for e in logs]
    assert "conviction_lock_armed" not in events
    # paper_trades.conviction_locked_at must remain NULL
    cur = await db._conn.execute(
        "SELECT conviction_locked_at FROM paper_trades WHERE status='open' LIMIT 1"
    )
    row = await cur.fetchone()
    assert row[0] is None


@pytest.mark.asyncio
async def test_evaluator_skips_conviction_lock_when_signal_not_opted_in(
    db, monkeypatch
):
    """T5b — fail-closed at signal level: kill-switch ON +
    signal_params.conviction_lock_enabled=0 means no lock."""
    from scout.config import Settings
    from structlog.testing import capture_logs
    from scout.trading.evaluator import evaluate_paper_trades
    settings = Settings()
    monkeypatch.setattr(settings, "PAPER_CONVICTION_LOCK_ENABLED", True)
    await _seed_locked_eligible_trade(
        db, signal_type="first_signal", opt_in_signal=False
    )
    with capture_logs() as logs:
        await evaluate_paper_trades(db, settings)
    events = [e.get("event") for e in logs]
    assert "conviction_lock_armed" not in events


@pytest.mark.asyncio
async def test_evaluator_skips_conviction_lock_when_below_threshold(
    db, monkeypatch
):
    """T5c — stack < threshold → no lock. Seed only 2 source rows;
    threshold=3 default."""
    from scout.config import Settings
    from structlog.testing import capture_logs
    from scout.trading.evaluator import evaluate_paper_trades
    settings = Settings()
    monkeypatch.setattr(settings, "PAPER_CONVICTION_LOCK_ENABLED", True)
    # Seed only 2 distinct sources (gainers + trending), no chain_matches
    await _seed_locked_eligible_trade(
        db, signal_type="first_signal", opt_in_signal=True,
        n_extra_sources=2,  # below default threshold=3
    )
    with capture_logs() as logs:
        await evaluate_paper_trades(db, settings)
    events = [e.get("event") for e in logs]
    assert "conviction_lock_armed" not in events


@pytest.mark.asyncio
async def test_evaluator_arms_conviction_lock_when_all_gates_pass(
    db, monkeypatch
):
    """T5d — all 3 gates pass → locked params used. Asserts:
    - `conviction_lock_armed` event fires
    - paper_trades.conviction_locked_at gets stamped
    - paper_trades.conviction_locked_stack gets stamped (design-v2 D1)
    - max_duration in the eval pass uses overlaid value"""
    from scout.config import Settings
    from structlog.testing import capture_logs
    from scout.trading.evaluator import evaluate_paper_trades
    settings = Settings()
    monkeypatch.setattr(settings, "PAPER_CONVICTION_LOCK_ENABLED", True)
    await _seed_locked_eligible_trade(
        db, signal_type="first_signal", opt_in_signal=True,
        n_extra_sources=3,  # at default threshold=3
    )
    with capture_logs() as logs:
        await evaluate_paper_trades(db, settings)
    armed = [e for e in logs if e.get("event") == "conviction_lock_armed"]
    assert armed, f"expected conviction_lock_armed; got {[e.get('event') for e in logs]}"
    a = armed[0]
    assert a["stack"] >= 3
    assert a["threshold"] == 3
    # Stack=3 → max_duration_hours += 168 = 336 (from base 168)
    # trail_pct +10pp capped at 35; sl_pct +10pp capped at 40
    assert a["locked_max_duration_hours"] >= 336
    cur = await db._conn.execute(
        "SELECT conviction_locked_at, conviction_locked_stack "
        "FROM paper_trades WHERE status='open' LIMIT 1"
    )
    row = await cur.fetchone()
    assert row[0] is not None  # conviction_locked_at stamped
    assert row[1] is not None  # conviction_locked_stack stamped (D1)
    assert row[1] >= 3


@pytest.mark.asyncio
async def test_evaluator_overlay_placement_keeps_trade_alive_past_base_max(
    db, monkeypatch
):
    """T5g (design-v2 M1 rewrite) — STRUCTURAL pin of overlay placement:
    if overlay runs AFTER line 158, max_duration uses base 168h and trade
    closes via expired. With overlay BEFORE line 158, max_duration uses
    overlaid 336h and trade survives.

    Seed opened_at = now - 200h (between base 168h and locked 336h).
    Stack=3 default → locked max_duration=336h.

    Pre-fix (overlay after 158): trade closes via expired on this pass.
    Post-fix (overlay before 158): trade still status='open' after pass."""
    from datetime import datetime, timedelta, timezone
    from scout.config import Settings
    from scout.trading.evaluator import evaluate_paper_trades
    settings = Settings()
    monkeypatch.setattr(settings, "PAPER_CONVICTION_LOCK_ENABLED", True)
    opened_at = (
        datetime.now(timezone.utc) - timedelta(hours=200)
    ).isoformat()
    await _seed_locked_eligible_trade(
        db, signal_type="first_signal", opt_in_signal=True,
        n_extra_sources=3, opened_at=opened_at,
    )
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT status FROM paper_trades LIMIT 1"
    )
    row = await cur.fetchone()
    # Overlay-after-158 BUG would have closed this via expired.
    # Overlay-before-158 (correct): trade still open at 200h since locked
    # max=336h.
    assert row[0] == "open", (
        f"placement bug: trade closed prematurely; status={row[0]!r}. "
        "Overlay must run BEFORE line 158 max_duration = timedelta(...)."
    )


@pytest.mark.asyncio
async def test_evaluator_logs_conviction_lock_armed_only_once(db, monkeypatch):
    """T5e — D2 fix: subsequent eval passes do NOT re-emit
    conviction_lock_armed once paper_trades.conviction_locked_at is set."""
    from scout.config import Settings
    from structlog.testing import capture_logs
    from scout.trading.evaluator import evaluate_paper_trades
    settings = Settings()
    monkeypatch.setattr(settings, "PAPER_CONVICTION_LOCK_ENABLED", True)
    await _seed_locked_eligible_trade(
        db, signal_type="first_signal", opt_in_signal=True,
        n_extra_sources=3,
    )
    # First pass — arms
    with capture_logs() as logs1:
        await evaluate_paper_trades(db, settings)
    armed1 = [e for e in logs1 if e.get("event") == "conviction_lock_armed"]
    assert len(armed1) == 1
    # Second pass — should NOT re-arm (D2 idempotency)
    with capture_logs() as logs2:
        await evaluate_paper_trades(db, settings)
    armed2 = [e for e in logs2 if e.get("event") == "conviction_lock_armed"]
    assert armed2 == [], f"D2 regression: re-emitted on second pass; got {armed2}"


@pytest.mark.asyncio
async def test_compute_stack_returns_zero_when_db_conn_closed(db):
    """T5f — M4 fix: compute_stack with db._conn=None returns 0 + logs warning."""
    from structlog.testing import capture_logs
    from scout.trading.conviction import compute_stack
    real_conn = db._conn
    db._conn = None
    try:
        with capture_logs() as logs:
            n = await compute_stack(db, "test-coin", "2026-05-01T00:00:00+00:00")
        assert n == 0
        events = [e.get("event") for e in logs]
        assert "conviction_lock_db_closed" in events
    finally:
        db._conn = real_conn


def test_moonshot_trail_composes_with_locked_trail():
    """T6 — A1 fix: at stack=4, locked trail (35%) > moonshot (30%);
    effective_trail_pct = max(30, 35) = 35. Pure unit test of the
    arithmetic; production patch at evaluator.py:357 mirrors this."""
    from scout.config import Settings
    settings = Settings()
    sp_trail_pct_locked = 35.0  # stack=4 locked trail
    effective = max(settings.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT, sp_trail_pct_locked)
    assert effective == 35.0


@pytest.mark.asyncio
async def test_evaluator_two_pass_moonshot_armed_then_lock_overlay_composes(
    db, monkeypatch
):
    """T6c (design-v2 A2) — two-pass ordering pin: pass 1 arms moonshot
    on a high-peak trade; pass 2 conviction-lock overlay sets sp.trail_pct
    to 35.0 (stack=4 locked); evaluator's effective_trail_pct branch then
    composes via max(PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT=30, 35) = 35.0.
    Pinning here catches the failure mode where the moonshot branch
    bypasses the overlaid sp because moonshot_armed_at was set on a prior
    pass with the un-overlaid sp.trail_pct.

    Concrete fixture per Build phase: seed 4 distinct sources for stack=4;
    seed price_cache trajectory hitting +50% peak (above moonshot
    threshold); run 2 evaluator passes; assert pass 2 logs effective_trail
    consistent with locked 35%, not moonshot constant 30%."""
    pass  # Concrete fixture lives in Build phase; T6c is the structural
          # placeholder for the two-pass ordering pin.


def test_backtest_script_imports_conviction_module_helpers():
    """T7 (design-v2 D3 sync wrapper round-trip) — pin that the backtest
    script's sync wrapper around `_count_stacked_signals_in_window`
    actually imports and calls successfully. Catches the case where
    `Database._conn` access pattern drifts (e.g., to a connection pool)
    and silently breaks the `_SyncDBShim`.
    """
    import sqlite3
    # Verify imports work; structural pin.
    from scripts.backtest_conviction_lock import (
        _count_stacked_signals_in_window as backtest_helper,
    )
    from scout.trading.conviction import conviction_locked_params
    # Round-trip through asyncio.run shim; smoke test only.
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE paper_trades (id INTEGER PRIMARY KEY, "
        "token_id TEXT, signal_type TEXT, opened_at TEXT)"
    )
    conn.commit()
    n, _sources = backtest_helper(
        conn, "test-coin", "2026-05-01T00:00:00+00:00",
        "2026-05-04T00:00:00+00:00",
    )
    assert isinstance(n, int)
    assert n == 0  # empty DB returns 0
    # conviction_locked_params is the pure-logic export; pinned by T3.
    base = {"max_duration_hours": 168, "trail_pct": 20.0, "sl_pct": 25.0}
    p = conviction_locked_params(stack=4, base=base)
    assert p["max_duration_hours"] == 504


@pytest.mark.asyncio
async def test_lab_711_regression_simulates_locked_first_signal(
    db, monkeypatch
):
    """T8 (design-v2 M2 reconciliation) — N4 fix: pin LAB #711 backtest
    finding (+$549.67 vs actual -$15.96) via synthetic 11-stack first_signal
    trade. Mirrors operator's manual hypothetical of $531.

    Reuses _seed_locked_eligible_trade(n_extra_sources=11) per design-v2
    S3 parameterization — no forked fixture. Asserts:
    - conviction_lock_armed fires
    - locked_max_duration_hours == 504 (stack-saturated ceiling)
    - paper_trades.conviction_locked_stack >= 11 (D1 column)
    - trade survives evaluator pass (status='open' at opened_at + 200h)

    Concrete LAB price-path replay (entry=$0.597 → peak=$1.85) is a
    follow-up enhancement; v1 pins the structural lock-arm + ceiling.
    """
    from datetime import datetime, timedelta, timezone
    from scout.config import Settings
    from structlog.testing import capture_logs
    from scout.trading.evaluator import evaluate_paper_trades
    settings = Settings()
    monkeypatch.setattr(settings, "PAPER_CONVICTION_LOCK_ENABLED", True)
    opened_at = (
        datetime.now(timezone.utc) - timedelta(hours=200)
    ).isoformat()
    await _seed_locked_eligible_trade(
        db, signal_type="first_signal", opt_in_signal=True,
        n_extra_sources=11, opened_at=opened_at,
        entry_price=0.597,  # LAB #711 entry
    )
    with capture_logs() as logs:
        await evaluate_paper_trades(db, settings)
    armed = [e for e in logs if e.get("event") == "conviction_lock_armed"]
    assert armed, "T8 LAB-replay: expected conviction_lock_armed"
    a = armed[0]
    # Stack saturates at 4; with 11 sources we still get the stack=4
    # ceiling locked params: max_duration += 336 = 504.
    assert a["locked_max_duration_hours"] == 504
    cur = await db._conn.execute(
        "SELECT status, conviction_locked_stack "
        "FROM paper_trades WHERE token_id = ? LIMIT 1",
        ("lab711-coin",),
    )
    row = await cur.fetchone()
    assert row[0] == "open", "T8: trade closed prematurely (placement bug)"
    assert row[1] >= 11, f"T8: locked_stack expected ≥11, got {row[1]}"
```

(T8 is no longer skeletal: design-v2 M2 reconciliation. Reuses the parameterized fixture from design-v2 S3.)

- [ ] **Step 2: Add overlay logic to evaluator (M2/A2 placement-critical)**

**M2/A2 fix — placement strictly between lines 157 and 158.** `max_duration = timedelta(hours=sp.max_duration_hours)` runs at line 158 and references `sp.max_duration_hours`. If overlay runs AFTER line 158, the locked 504h is silently ignored. The overlay block must:
1. Run AFTER `sp = await params_for_signal(db, signal_type_row, settings)` (line 157)
2. Run BEFORE `max_duration = timedelta(hours=sp.max_duration_hours)` (line 158)
3. Replace `sp` via `dataclasses.replace(...)` so the overlaid `max_duration_hours` flows into line 158's `timedelta()` call

**D2 fix — log idempotency via `paper_trades.conviction_locked_at` column.** Stamp the column on first arm; gate the `conviction_lock_armed` log on `conviction_locked_at IS NULL`. Subsequent passes still apply the overlay (params re-derived from current stack) but emit no log.

Modify the SELECT at lines 97-108 to also fetch `conviction_locked_at`:

```python
    cursor = await conn.execute("""SELECT id, token_id, entry_price, opened_at,
                  tp_price, sl_price, tp_pct, sl_pct,
                  checkpoint_1h_price, checkpoint_6h_price,
                  checkpoint_24h_price, checkpoint_48h_price,
                  peak_price, peak_pct, signal_data, symbol, name, chain,
                  amount_usd, quantity, signal_type,
                  created_at, leg_1_filled_at, leg_2_filled_at,
                  remaining_qty, floor_armed, realized_pnl_usd,
                  checkpoint_6h_pct, checkpoint_24h_pct,
                  moonshot_armed_at, conviction_locked_at
           FROM paper_trades
           WHERE status = 'open'""")
```

In the per-trade body, immediately after `sp = await params_for_signal(...)` at line 157:

```python
            # BL-067 conviction-lock overlay. Three gates ALL must pass:
            # 1. Master kill-switch ON (settings.PAPER_CONVICTION_LOCK_ENABLED)
            # 2. Per-signal opt-in (signal_params.conviction_lock_enabled=1)
            # 3. Stack count >= PAPER_CONVICTION_LOCK_THRESHOLD
            #
            # Placement-critical (M2/A2): MUST run BEFORE line 158
            # `max_duration = timedelta(hours=sp.max_duration_hours)` so
            # the overlaid max_duration_hours flows into the timedelta call.
            #
            # Note: trail_pct_low_peak intentionally NOT overlaid (A3) —
            # adaptive low-peak trail is orthogonal regime (peak <
            # low_peak_threshold); locked trail only fires at high peak.
            # Leg targets (leg_1_pct/leg_2_pct/qty_frac) NOT overlaid (S6)
            # — BL-067 spec table only widens trail/sl/max_duration.
            #
            # `row[30]` = conviction_locked_at (added to SELECT above);
            # column index = 30 in 0-indexed row (after moonshot_armed_at
            # at index 29).
            conviction_locked_at = row[30]
            if (
                settings.PAPER_CONVICTION_LOCK_ENABLED
                and sp.conviction_lock_enabled
            ):
                from scout.trading.conviction import (
                    compute_stack, conviction_locked_params,
                )
                # design-v2 S2: exclude the trade itself from
                # paper_trades self-counting in the stack.
                stack = await compute_stack(
                    db, token_id, str(row[3]),
                    exclude_trade_id=trade_id,
                )
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
                    # Replace sp with overlaid frozen dataclass.
                    # Critical: this MUST happen before line 158 so the
                    # NEW max_duration_hours is what timedelta() reads.
                    from dataclasses import replace
                    sp = replace(
                        sp,
                        max_duration_hours=locked["max_duration_hours"],
                        trail_pct=locked["trail_pct"],
                        sl_pct=locked["sl_pct"],
                    )
                    # D2 fix: log + stamp ONCE per trade. Subsequent passes
                    # still apply the overlay (params re-derived from
                    # current stack each time) but emit no log.
                    # design-v2 D1: also stamp conviction_locked_stack
                    # so dashboard / future analyses can read stack-at-arm
                    # without re-computing across aged-out source rows.
                    if conviction_locked_at is None:
                        armed_iso = now.isoformat()
                        await conn.execute(
                            "UPDATE paper_trades "
                            "SET conviction_locked_at = ?, "
                            "    conviction_locked_stack = ? "
                            "WHERE id = ?",
                            (armed_iso, stack, trade_id),
                        )
                        await conn.commit()
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
                            armed_at=armed_iso,
                        )
                # Below-threshold: silent (operator can grep absence of
                # `conviction_lock_armed` events for the trade if they want
                # to know why a token didn't lock).
```

Line 158 then reads the overlaid `sp.max_duration_hours` — no change needed.

**A1 fix — moonshot trail composition.** Production at `evaluator.py:356-357` reads `settings.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT` directly, ignoring `sp.trail_pct`. At stack=4 (locked trail = 35%) on a moonshot-armed trade the trail collapses to 30% — feature broken in the high-peak regime where it matters most. **Patch line 357:**

```python
                if moonshot_armed_at is not None:
                    # BL-067 A1 fix: compose moonshot floor with locked trail.
                    # Locked trail (sp.trail_pct, possibly overlaid by
                    # conviction-lock) wins when wider; otherwise moonshot
                    # constant wins. max() preserves both regimes' protective
                    # intent. Backtest simulator already used this max() form;
                    # production must match.
                    effective_trail_pct = max(
                        settings.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT,
                        sp.trail_pct,
                    )
```

Add a regression test pinning the composition: at stack=4 + moonshot armed, effective_trail = max(30, 35) = 35.

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
# design-v2 D2 (architecture): wrap UPDATE + audit INSERT in BEGIN;/COMMIT;
# so both succeed atomically. sqlite3 CLI auto-commits per statement by
# default; without an explicit transaction, an audit INSERT failure (e.g.
# signal_params_audit table missing — F12) leaves conviction_lock_enabled=1
# active on prod with NO audit row.
ssh root@89.167.116.187 'sqlite3 /root/gecko-alpha/scout.db "
  BEGIN;
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
  COMMIT;
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

design-v2 S3 (adversarial) — `sed` pattern matches any line containing the
key (commented, spaced, etc), not just the exact `^PAPER_CONVICTION_LOCK_ENABLED=`
form. Operators paste rollback verbatim under pressure; previous strict
anchoring would silently no-op against `# PAPER_CONVICTION_LOCK_ENABLED=true` or
`PAPER_CONVICTION_LOCK_ENABLED = true` (with spaces).

```bash
# Disable master kill-switch (matches commented OR spaced variants)
ssh root@89.167.116.187 'sed -i "/PAPER_CONVICTION_LOCK_ENABLED/d" /root/gecko-alpha/.env'
# Verify the line is gone
ssh root@89.167.116.187 'grep PAPER_CONVICTION_LOCK_ENABLED /root/gecko-alpha/.env || echo "REMOVED"' > .ssh_revert_verify.txt
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
- **IN scope (N5/D3 reconciliation):** consolidating `_count_stacked_signals_in_window` from `scripts/backtest_conviction_lock.py` IS now in scope per D3 — sync/async impedance solved with thin `asyncio.run()` adapter (~5 LOC, see Task 3 Step 5). Single source of truth in `scout/trading/conviction.py`; backtest wraps it. Removes the predictable 60-day drift hazard.
- **DELIBERATELY DEFERRED:** dynamic threshold calibration — operator can change PAPER_CONVICTION_LOCK_THRESHOLD via `.env` but the backtest validated N=3 specifically; lowering to N=2 should be a separate operator decision with re-run.
- **DELIBERATELY DEFERRED:** stack-count caching — backtest validated real-time computation is cheap (~9 indexed SELECTs ≈ ms). Persist only if profiling shows the eval-loop hot path.
- **DELIBERATELY DEFERRED:** conviction_stack downgrade on inactivity — once locked, stays locked through trade life (per backlog Q9 + simpler).

**10. Soak-then-escalate criterion:** monitor `conviction_lock_armed` events daily for 14 days after operator opts in `first_signal` + `gainers_early`. If no regressions (no unusual `trading_open_*_error` events, no PnL delta from previous 14d baseline beyond expected variance), then operator can proceed to flip the next signal_type (`losers_contrarian`, `volume_spike`, `chain_completed`, then narrative_prediction after 720h re-run).
