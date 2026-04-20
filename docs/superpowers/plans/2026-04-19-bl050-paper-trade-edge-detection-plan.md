# BL-050: Paper-trade Edge-Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `first_signal` paper trades from firing on every currently-qualifying token at restart by replacing the *current-state* check with a *transition-into* check backed by a new `signal_qualifier_state` table.

**Architecture:** A new module `scout/trading/qualifier_state.py` exposes two async helpers: `classify_transitions(db, signal_type, current_token_ids, now, exit_grace_hours)` upserts the current set and returns the subset that just entered; `prune_stale_qualifiers(db, now, retention_hours)` deletes rows older than retention. Integration is minimal — `trade_first_signals` loops over `transitions` instead of `qualifying`, wrapped in fail-closed try/except. A counter-based prune runs every `QUALIFIER_PRUNE_EVERY_CYCLES` iterations in `_pipeline_loop`.

**Tech Stack:** Python 3.x async, aiosqlite, Pydantic v2 (`BaseSettings` + `@model_validator`), structlog. Tests use pytest-asyncio auto mode with `tmp_path` fixture and the `settings_factory`/`token_factory` fixtures in `tests/conftest.py`.

**Reference spec:** `docs/superpowers/specs/2026-04-19-bl050-paper-trade-edge-detection-design.md`

**File structure:**
- Create: `scout/trading/qualifier_state.py` — two public async functions, no class.
- Modify: `scout/db.py` (add `signal_qualifier_state` table + index to `_create_tables`).
- Modify: `scout/config.py` (add 3 settings + cross-field validator).
- Modify: `scout/trading/signals.py::trade_first_signals` (swap current-state loop for transition loop, fail-closed).
- Modify: `scout/main.py::_pipeline_loop` (add prune every N cycles).
- Modify: `scout/heartbeat.py` (add 3 qualifier counters).
- Create: `tests/test_trading_qualifier_state.py` — 13 unit tests.
- Create: `tests/test_trading_edge_detection.py` — 6 integration tests.

**Canonical invariants used throughout:**
- All timestamps are ISO-8601 strings with explicit UTC: `datetime.now(timezone.utc).isoformat()`.
- All SQL that compares the string column `last_qualified_at` MUST wrap both sides in `datetime(...)` (per PR #24).
- `_txn_lock` is an `asyncio.Lock` on `Database` initialized in `initialize()`. Use it via `async with db._txn_lock:` — never construct a fresh Lock.
- `trade_first_signals` receives tuples `(token, quant_score, signals_fired)`. The token's identifier field is `token.contract_address` (NOT `token.token_id`) — the spec's `token_id` is the abstraction; the concrete field in `CandidateToken` is `contract_address`.
- The new `signal_type` string is exactly `"first_signal"`.
- **`classify_transitions` returns `dict[str, str | None]`** mapping transitioned token_id → prior `last_qualified_at` ISO string (or `None` if the token had no prior row). Callers iterate keys for membership, read values for observability. This shape is required by the spec's Observability section, which mandates `prior_last_qualified_at` and `elapsed_since_prior_hours` on the `qualifier_transition_fired` log event.

---

### Task 1: Add `signal_qualifier_state` schema

**Files:**
- Modify: `scout/db.py` — add table + index inside the `_create_tables` `executescript` block (before the closing `""")`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_trading_qualifier_state.py` with this content (this is also the file for later unit tests):

```python
"""Unit tests for scout.trading.qualifier_state (BL-050)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database


async def test_schema_creates_signal_qualifier_state_table(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cursor = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='signal_qualifier_state'"
    )
    row = await cursor.fetchone()
    assert row is not None, "signal_qualifier_state table must exist after initialize()"

    cursor = await db._conn.execute("PRAGMA table_info(signal_qualifier_state)")
    cols = {r[1]: r[2] for r in await cursor.fetchall()}
    assert cols == {
        "signal_type": "TEXT",
        "token_id": "TEXT",
        "first_qualified_at": "TEXT",
        "last_qualified_at": "TEXT",
    }

    cursor = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_sqs_last_qualified_at'"
    )
    assert await cursor.fetchone() is not None
    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_trading_qualifier_state.py::test_schema_creates_signal_qualifier_state_table -v`
Expected: FAIL with AssertionError "signal_qualifier_state table must exist after initialize()".

- [ ] **Step 3: Add the DDL to `_create_tables`**

In `scout/db.py`, inside `_create_tables`, locate the big `executescript("""...""")` block (it ends around the `velocity_alerts` index at line ~675). Immediately before the closing `""")`, insert:

```sql

            CREATE TABLE IF NOT EXISTS signal_qualifier_state (
                signal_type         TEXT NOT NULL,
                token_id            TEXT NOT NULL,
                first_qualified_at  TEXT NOT NULL,
                last_qualified_at   TEXT NOT NULL,
                PRIMARY KEY (signal_type, token_id)
            );
            CREATE INDEX IF NOT EXISTS idx_sqs_last_qualified_at
                ON signal_qualifier_state (last_qualified_at);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_trading_qualifier_state.py::test_schema_creates_signal_qualifier_state_table -v`
Expected: PASS.

- [ ] **Step 5: Run full test suite to confirm no regressions**

Run: `uv run pytest --tb=short -q`
Expected: All pre-existing tests still pass; one new test passes.

- [ ] **Step 6: Commit**

```bash
git add scout/db.py tests/test_trading_qualifier_state.py
git commit -m "feat(bl-050): add signal_qualifier_state schema"
```

---

### Task 2: Add config settings + `retention > grace` cross-field validator

**Files:**
- Modify: `scout/config.py` — add 3 fields and one `@model_validator(mode="after")` method.
- Test: `tests/test_trading_qualifier_state.py` (append).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_trading_qualifier_state.py`:

```python
def test_config_defaults_for_qualifier_settings(settings_factory):
    s = settings_factory()
    assert s.QUALIFIER_EXIT_GRACE_HOURS == 48
    assert s.QUALIFIER_PRUNE_RETENTION_HOURS == 168
    assert s.QUALIFIER_PRUNE_EVERY_CYCLES == 100


def test_config_rejects_retention_le_grace(settings_factory):
    with pytest.raises(ValueError, match="QUALIFIER_PRUNE_RETENTION_HOURS"):
        settings_factory(
            QUALIFIER_EXIT_GRACE_HOURS=48,
            QUALIFIER_PRUNE_RETENTION_HOURS=48,
        )
    with pytest.raises(ValueError, match="QUALIFIER_PRUNE_RETENTION_HOURS"):
        settings_factory(
            QUALIFIER_EXIT_GRACE_HOURS=48,
            QUALIFIER_PRUNE_RETENTION_HOURS=24,
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_trading_qualifier_state.py::test_config_defaults_for_qualifier_settings tests/test_trading_qualifier_state.py::test_config_rejects_retention_le_grace -v`
Expected: FAIL. `test_config_defaults_for_qualifier_settings` fails with `AttributeError: 'Settings' object has no attribute 'QUALIFIER_EXIT_GRACE_HOURS'`. `test_config_rejects_retention_le_grace` fails with `pytest.raises` reporting `DID NOT RAISE ValueError` — `SettingsConfigDict(extra="ignore")` silently drops the unknown `QUALIFIER_*` kwargs, so construction succeeds where the test expected a failure.

- [ ] **Step 3: Add the settings + validator**

In `scout/config.py`, immediately after the `FEEDBACK_CHRONIC_FAILURE_THRESHOLD: int = 3` line (end of settings block, before the first `@field_validator`), add:

```python
    # -------- BL-050: Paper-trade Edge-Detection (qualifier transition gate) --------
    QUALIFIER_EXIT_GRACE_HOURS: int = 48
    QUALIFIER_PRUNE_RETENTION_HOURS: int = 168
    QUALIFIER_PRUNE_EVERY_CYCLES: int = 100
```

Then, immediately after the existing `validate_weights_sum` `@model_validator`, add a second `@model_validator`:

```python
    @model_validator(mode="after")
    def _check_retention_gt_grace(self) -> "Settings":
        if self.QUALIFIER_PRUNE_RETENTION_HOURS <= self.QUALIFIER_EXIT_GRACE_HOURS:
            raise ValueError(
                "QUALIFIER_PRUNE_RETENTION_HOURS must be strictly greater than "
                "QUALIFIER_EXIT_GRACE_HOURS to prevent pruning rows that classify "
                "still needs for re-entry detection."
            )
        return self
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_trading_qualifier_state.py::test_config_defaults_for_qualifier_settings tests/test_trading_qualifier_state.py::test_config_rejects_retention_le_grace -v`
Expected: Both PASS.

- [ ] **Step 5: Run full suite to confirm no regression in other settings tests**

Run: `uv run pytest --tb=short -q`
Expected: all pass (no existing code depends on these new fields).

- [ ] **Step 6: Commit**

```bash
git add scout/config.py tests/test_trading_qualifier_state.py
git commit -m "feat(bl-050): add QUALIFIER_* settings with retention>grace validator"
```

---

### Task 3: `classify_transitions` — core semantics (first-call, continuation, re-entry, boundary)

**Files:**
- Create: `scout/trading/qualifier_state.py`
- Test: `tests/test_trading_qualifier_state.py` (append).

- [ ] **Step 1: Write seven failing tests**

Append to `tests/test_trading_qualifier_state.py`:

```python
from scout.trading.qualifier_state import classify_transitions


async def _qualifier_row(db, signal_type, token_id):
    cur = await db._conn.execute(
        "SELECT first_qualified_at, last_qualified_at FROM signal_qualifier_state "
        "WHERE signal_type = ? AND token_id = ?",
        (signal_type, token_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def _seed_qualifier(db, signal_type, token_id, first_at, last_at):
    await db._conn.execute(
        "INSERT OR REPLACE INTO signal_qualifier_state "
        "(signal_type, token_id, first_qualified_at, last_qualified_at) "
        "VALUES (?, ?, ?, ?)",
        (signal_type, token_id, first_at.isoformat(), last_at.isoformat()),
    )
    await db._conn.commit()


async def test_classify_returns_all_tokens_on_first_call(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)

    result = await classify_transitions(
        db,
        signal_type="first_signal",
        current_token_ids={"a", "b", "c"},
        now=now,
        exit_grace_hours=48,
    )
    # Returns dict[token_id -> prior_last_qualified_at]; None = no prior row.
    assert result == {"a": None, "b": None, "c": None}

    for tid in ("a", "b", "c"):
        row = await _qualifier_row(db, "first_signal", tid)
        assert row is not None
        assert row["first_qualified_at"] == now.isoformat()
        assert row["last_qualified_at"] == now.isoformat()
    await db.close()


async def test_classify_returns_empty_when_all_tokens_already_present(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    earlier = datetime(2026, 4, 19, 10, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)

    await _seed_qualifier(db, "first_signal", "a", earlier, earlier)
    await _seed_qualifier(db, "first_signal", "b", earlier, earlier)

    result = await classify_transitions(
        db,
        signal_type="first_signal",
        current_token_ids={"a", "b"},
        now=now,
        exit_grace_hours=48,
    )
    assert result == {}

    # last_qualified_at bumped to now; first_qualified_at preserved
    for tid in ("a", "b"):
        row = await _qualifier_row(db, "first_signal", tid)
        assert row["first_qualified_at"] == earlier.isoformat()
        assert row["last_qualified_at"] == now.isoformat()
    await db.close()


async def test_classify_returns_only_new_token(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    earlier = datetime(2026, 4, 19, 10, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)

    await _seed_qualifier(db, "first_signal", "a", earlier, earlier)

    result = await classify_transitions(
        db,
        signal_type="first_signal",
        current_token_ids={"a", "b"},
        now=now,
        exit_grace_hours=48,
    )
    # Only "b" transitioned; "b" has no prior row → prior is None.
    assert result == {"b": None}
    await db.close()


async def test_re_entry_outside_grace_counts_as_transition(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)
    stale = now - timedelta(hours=49)  # outside 48h grace

    await _seed_qualifier(db, "first_signal", "a", stale, stale)

    result = await classify_transitions(
        db,
        signal_type="first_signal",
        current_token_ids={"a"},
        now=now,
        exit_grace_hours=48,
    )
    # Re-entry transition; prior last_qualified_at is reported for observability.
    assert set(result.keys()) == {"a"}
    assert result["a"] == stale.isoformat()

    # first_qualified_at RESETS to now on re-entry
    row = await _qualifier_row(db, "first_signal", "a")
    assert row["first_qualified_at"] == now.isoformat()
    assert row["last_qualified_at"] == now.isoformat()
    await db.close()


async def test_re_entry_inside_grace_is_not_transition(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)
    recent = now - timedelta(hours=47)  # inside 48h grace

    await _seed_qualifier(db, "first_signal", "a", recent, recent)

    result = await classify_transitions(
        db,
        signal_type="first_signal",
        current_token_ids={"a"},
        now=now,
        exit_grace_hours=48,
    )
    assert result == {}

    row = await _qualifier_row(db, "first_signal", "a")
    assert row["first_qualified_at"] == recent.isoformat()  # preserved
    assert row["last_qualified_at"] == now.isoformat()       # bumped
    await db.close()


async def test_re_entry_exactly_at_grace_boundary_is_not_transition(tmp_path):
    """Boundary convention: last_qualified_at == now - grace → continuation (inclusive)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)
    exactly = now - timedelta(hours=48)  # exactly at boundary

    await _seed_qualifier(db, "first_signal", "a", exactly, exactly)

    result = await classify_transitions(
        db,
        signal_type="first_signal",
        current_token_ids={"a"},
        now=now,
        exit_grace_hours=48,
    )
    assert result == {}
    await db.close()


async def test_re_entry_one_second_past_grace_is_transition(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)
    stale = now - timedelta(hours=48, seconds=1)

    await _seed_qualifier(db, "first_signal", "a", stale, stale)

    result = await classify_transitions(
        db,
        signal_type="first_signal",
        current_token_ids={"a"},
        now=now,
        exit_grace_hours=48,
    )
    assert set(result.keys()) == {"a"}
    assert result["a"] == stale.isoformat()
    await db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_trading_qualifier_state.py -k "classify or re_entry" -v`
Expected: All FAIL with `ImportError: cannot import name 'classify_transitions' from 'scout.trading.qualifier_state'` (module does not yet exist).

- [ ] **Step 3: Create `scout/trading/qualifier_state.py` with `classify_transitions`**

Write this exact file content:

```python
"""BL-050 — Qualifier transition state for first_signal paper trades.

Replaces the historical current-state check in trade_first_signals with a
transition-into-qualifier check backed by a persisted table. Rationale and
acceptance criteria live in
docs/superpowers/specs/2026-04-19-bl050-paper-trade-edge-detection-design.md.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from scout.db import Database

log = structlog.get_logger()


async def classify_transitions(
    db: Database,
    *,
    signal_type: str,
    current_token_ids: set[str],
    now: datetime,
    exit_grace_hours: int,
) -> dict[str, str | None]:
    """Classify current_token_ids into transitions (returned) and continuations (not).

    Upserts ALL current_token_ids unconditionally. Returns a mapping of transitioned
    token_id → prior `last_qualified_at` ISO string (or None for tokens with no
    prior row). Callers iterate keys for membership and read values for
    observability (`prior_last_qualified_at`, `elapsed_since_prior_hours`).

    A token is a transition iff it had NO prior row, OR its prior
    `last_qualified_at` is strictly older than `now - exit_grace_hours`.

    Boundary convention: prior last_qualified_at == (now - exit_grace_hours)
    counts as continuation (inclusive).

    Empty input early-returns {} without touching the DB or the txn lock.

    Error policy: aiosqlite errors propagate. Caller is REQUIRED to wrap
    invocation in try/except and fail-closed for the cycle.
    """
    if not current_token_ids:
        return {}

    if db._conn is None or db._txn_lock is None:
        raise RuntimeError(
            "Database not initialized — classify_transitions() called before "
            "Database.initialize()."
        )

    threshold = (now - timedelta(hours=exit_grace_hours)).isoformat()
    now_iso = now.isoformat()

    async with db._txn_lock:
        # Read all existing rows for these tokens in one query.
        placeholders = ",".join("?" for _ in current_token_ids)
        ids_list = list(current_token_ids)
        cur = await db._conn.execute(
            f"SELECT token_id, last_qualified_at FROM signal_qualifier_state "
            f"WHERE signal_type = ? AND token_id IN ({placeholders})",
            (signal_type, *ids_list),
        )
        existing = {row[0]: row[1] for row in await cur.fetchall()}

        transitions: dict[str, str | None] = {}
        for tid in current_token_ids:
            prior_last = existing.get(tid)
            if prior_last is None:
                transitions[tid] = None
                continue
            # Compare ISO-8601 strings via datetime() wrapper — per PR #24,
            # raw string comparison breaks on any format drift (timezone
            # offset, microsecond precision). Push the comparison into SQL.
            cmp = await db._conn.execute(
                "SELECT datetime(?) > datetime(?)",
                (threshold, prior_last),
            )
            cmp_row = await cmp.fetchone()
            if cmp_row and cmp_row[0]:
                # threshold is strictly greater than prior_last → transition.
                # Record prior for observability.
                transitions[tid] = prior_last

        # Upsert every token. first_qualified_at:
        #   - new row: now
        #   - transition (prior outside grace): now (reset)
        #   - continuation: preserved via UPDATE of last_qualified_at only
        for tid in current_token_ids:
            if tid in transitions:
                # transition or brand-new: first = now, last = now
                await db._conn.execute(
                    "INSERT INTO signal_qualifier_state "
                    "(signal_type, token_id, first_qualified_at, last_qualified_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(signal_type, token_id) DO UPDATE SET "
                    "first_qualified_at=excluded.first_qualified_at, "
                    "last_qualified_at=excluded.last_qualified_at",
                    (signal_type, tid, now_iso, now_iso),
                )
            else:
                # continuation: preserve first_qualified_at, bump last_qualified_at
                await db._conn.execute(
                    "UPDATE signal_qualifier_state "
                    "SET last_qualified_at = ? "
                    "WHERE signal_type = ? AND token_id = ?",
                    (now_iso, signal_type, tid),
                )
        await db._conn.commit()

    return transitions


async def prune_stale_qualifiers(
    db: Database,
    *,
    now: datetime,
    retention_hours: int,
) -> int:
    """Placeholder — implemented in Task 5."""
    raise NotImplementedError
```

- [ ] **Step 4: Run tests to verify all 7 pass**

Run: `uv run pytest tests/test_trading_qualifier_state.py -k "classify or re_entry" -v`
Expected: 7 PASS.

- [ ] **Step 5: Run full suite to confirm no regressions**

Run: `uv run pytest --tb=short -q`
Expected: all pre-existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add scout/trading/qualifier_state.py tests/test_trading_qualifier_state.py
git commit -m "feat(bl-050): implement classify_transitions with boundary semantics"
```

---

### Task 4: `classify_transitions` — error propagation, empty set, multi-signal-type

**Files:**
- Test: `tests/test_trading_qualifier_state.py` (append).
- (No implementation change expected — the implementation in Task 3 already handles these; tests lock it in.)

- [ ] **Step 1: Write three failing tests**

Append to `tests/test_trading_qualifier_state.py`:

```python
import aiosqlite


async def test_empty_current_ids_returns_empty_without_transaction(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.db")
    await db.initialize()

    async def _boom(*args, **kwargs):
        raise AssertionError("txn lock must not be acquired for empty input")

    # Patch the lock's acquire method to raise if called
    monkeypatch.setattr(db._txn_lock, "acquire", _boom)

    result = await classify_transitions(
        db,
        signal_type="first_signal",
        current_token_ids=set(),
        now=datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc),
        exit_grace_hours=48,
    )
    assert result == {}
    await db.close()


async def test_classify_raises_on_aiosqlite_error(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.db")
    await db.initialize()

    original_execute = db._conn.execute
    call_count = {"n": 0}

    async def _execute_then_fail(sql, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise aiosqlite.OperationalError("simulated failure")
        return await original_execute(sql, *args, **kwargs)

    monkeypatch.setattr(db._conn, "execute", _execute_then_fail)

    with pytest.raises(aiosqlite.OperationalError, match="simulated failure"):
        await classify_transitions(
            db,
            signal_type="first_signal",
            current_token_ids={"a"},
            now=datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc),
            exit_grace_hours=48,
        )
    await db.close()


async def test_different_signal_types_do_not_interfere(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)

    first_result = await classify_transitions(
        db,
        signal_type="first_signal",
        current_token_ids={"shared_token"},
        now=now,
        exit_grace_hours=48,
    )
    assert first_result == {"shared_token": None}

    # Same token under a different signal_type is a fresh transition
    other_result = await classify_transitions(
        db,
        signal_type="other_signal",
        current_token_ids={"shared_token"},
        now=now,
        exit_grace_hours=48,
    )
    assert other_result == {"shared_token": None}

    # Two independent rows exist
    cur = await db._conn.execute(
        "SELECT signal_type FROM signal_qualifier_state WHERE token_id = ?",
        ("shared_token",),
    )
    rows = {r[0] for r in await cur.fetchall()}
    assert rows == {"first_signal", "other_signal"}
    await db.close()
```

- [ ] **Step 2: Run tests to verify behavior**

Run: `uv run pytest tests/test_trading_qualifier_state.py::test_empty_current_ids_returns_empty_without_transaction tests/test_trading_qualifier_state.py::test_classify_raises_on_aiosqlite_error tests/test_trading_qualifier_state.py::test_different_signal_types_do_not_interfere -v`
Expected: All 3 PASS (the implementation from Task 3 already satisfies these; if any fails, fix the implementation rather than the test).

- [ ] **Step 3: Commit**

```bash
git add tests/test_trading_qualifier_state.py
git commit -m "test(bl-050): pin classify_transitions error/empty/multi-type contracts"
```

---

### Task 5: Implement `prune_stale_qualifiers`

**Files:**
- Modify: `scout/trading/qualifier_state.py` (replace the NotImplementedError stub).
- Test: `tests/test_trading_qualifier_state.py` (append).

- [ ] **Step 1: Write three failing tests**

Append to `tests/test_trading_qualifier_state.py`:

```python
from scout.trading.qualifier_state import prune_stale_qualifiers


async def test_prune_stale_removes_old_rows_only(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)

    # Row A: last_qualified_at = 8 days ago → stale (retention 168h = 7 days)
    stale = now - timedelta(days=8)
    await _seed_qualifier(db, "first_signal", "stale_a", stale, stale)
    # Row B: last_qualified_at = 3 days ago → fresh
    fresh = now - timedelta(days=3)
    await _seed_qualifier(db, "first_signal", "fresh_b", fresh, fresh)

    deleted = await prune_stale_qualifiers(db, now=now, retention_hours=168)
    assert deleted == 1

    assert await _qualifier_row(db, "first_signal", "stale_a") is None
    assert await _qualifier_row(db, "first_signal", "fresh_b") is not None
    await db.close()


async def test_prune_retention_zero_raises_value_error(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="retention_hours"):
        await prune_stale_qualifiers(db, now=now, retention_hours=0)
    with pytest.raises(ValueError, match="retention_hours"):
        await prune_stale_qualifiers(db, now=now, retention_hours=-1)
    await db.close()


async def test_prune_returns_zero_when_no_stale_rows(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)

    # All rows fresh
    fresh = now - timedelta(days=2)
    await _seed_qualifier(db, "first_signal", "a", fresh, fresh)
    await _seed_qualifier(db, "first_signal", "b", fresh, fresh)

    deleted = await prune_stale_qualifiers(db, now=now, retention_hours=168)
    assert deleted == 0

    # Rows still present
    assert await _qualifier_row(db, "first_signal", "a") is not None
    assert await _qualifier_row(db, "first_signal", "b") is not None
    await db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_trading_qualifier_state.py::test_prune_stale_removes_old_rows_only tests/test_trading_qualifier_state.py::test_prune_retention_zero_raises_value_error tests/test_trading_qualifier_state.py::test_prune_returns_zero_when_no_stale_rows -v`
Expected: All 3 FAIL with `NotImplementedError` (the stub from Task 3).

- [ ] **Step 3: Replace the stub in `scout/trading/qualifier_state.py`**

Replace the `prune_stale_qualifiers` function (currently raising `NotImplementedError`) with:

```python
async def prune_stale_qualifiers(
    db: Database,
    *,
    now: datetime,
    retention_hours: int,
) -> int:
    """Delete rows where datetime(last_qualified_at) < datetime(now - retention).

    Returns the number of rows deleted. Acquires db._txn_lock.

    retention_hours must be > 0; callers pass settings.QUALIFIER_PRUNE_RETENTION_HOURS
    which is enforced > 0 by the Settings model_validator. A defensive check here
    catches programming errors (zero/negative literal in a caller).

    Read-only SELECT COUNT first so a clean table doesn't open a write transaction.
    """
    if retention_hours <= 0:
        raise ValueError(
            f"retention_hours must be > 0, got {retention_hours}"
        )

    if db._conn is None or db._txn_lock is None:
        raise RuntimeError(
            "Database not initialized — prune_stale_qualifiers() called before "
            "Database.initialize()."
        )

    threshold = (now - timedelta(hours=retention_hours)).isoformat()

    async with db._txn_lock:
        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM signal_qualifier_state "
            "WHERE datetime(last_qualified_at) < datetime(?)",
            (threshold,),
        )
        count_row = await cur.fetchone()
        count = count_row[0] if count_row else 0
        if count == 0:
            return 0

        cursor = await db._conn.execute(
            "DELETE FROM signal_qualifier_state "
            "WHERE datetime(last_qualified_at) < datetime(?)",
            (threshold,),
        )
        await db._conn.commit()
        return cursor.rowcount
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_trading_qualifier_state.py -k "prune" -v`
Expected: 3 PASS.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest --tb=short -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scout/trading/qualifier_state.py tests/test_trading_qualifier_state.py
git commit -m "feat(bl-050): implement prune_stale_qualifiers"
```

---

### Task 6: Extend `_heartbeat_stats` with qualifier counters

**Files:**
- Modify: `scout/heartbeat.py`
- Test: `tests/test_trading_qualifier_state.py` (append).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_trading_qualifier_state.py`:

```python
def test_heartbeat_stats_has_qualifier_counters():
    from scout.heartbeat import _heartbeat_stats, _reset_heartbeat_stats

    _reset_heartbeat_stats()
    assert _heartbeat_stats["qualifier_transitions"] == 0
    assert _heartbeat_stats["qualifier_skips"] == 0
    assert _heartbeat_stats["qualifier_prune_consecutive_failures"] == 0

    _heartbeat_stats["qualifier_transitions"] += 3
    _reset_heartbeat_stats()
    assert _heartbeat_stats["qualifier_transitions"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_trading_qualifier_state.py::test_heartbeat_stats_has_qualifier_counters -v`
Expected: FAIL with `KeyError: 'qualifier_transitions'`.

- [ ] **Step 3: Modify `scout/heartbeat.py`**

(a) In the module-level `_heartbeat_stats` dict, add three new keys immediately before the existing `"last_heartbeat_at": None,` line:

```python
    "qualifier_transitions": 0,
    "qualifier_skips": 0,
    "qualifier_prune_consecutive_failures": 0,
    "last_heartbeat_at": None,
```

(b) In `_reset_heartbeat_stats`, add the same three keys with value `0` inside the `_heartbeat_stats.update(...)` keyword-args call. The resulting function body should look like:

```python
def _reset_heartbeat_stats() -> None:
    _heartbeat_stats.update(
        started_at=None,
        tokens_scanned=0,
        candidates_promoted=0,
        alerts_fired=0,
        narrative_predictions=0,
        counter_scores_memecoin=0,
        counter_scores_narrative=0,
        qualifier_transitions=0,
        qualifier_skips=0,
        qualifier_prune_consecutive_failures=0,
        last_heartbeat_at=None,
    )
```

(If the live file has additional keys not shown above — e.g., it has evolved since this plan was written — preserve them; only ADD the three `qualifier_*` entries.)

(c) In `_maybe_emit_heartbeat`, add three new keyword arguments to the `logger.info("heartbeat", ...)` call, inserted immediately before the existing `last_heartbeat_at=...` argument:

```python
        qualifier_transitions=_heartbeat_stats["qualifier_transitions"],
        qualifier_skips=_heartbeat_stats["qualifier_skips"],
        qualifier_prune_consecutive_failures=_heartbeat_stats["qualifier_prune_consecutive_failures"],
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_trading_qualifier_state.py::test_heartbeat_stats_has_qualifier_counters -v`
Expected: PASS.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest --tb=short -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scout/heartbeat.py tests/test_trading_qualifier_state.py
git commit -m "feat(bl-050): add qualifier counters to heartbeat"
```

---

### Task 7: Wire `trade_first_signals` to `classify_transitions` (fail-closed)

**Files:**
- Modify: `scout/trading/signals.py` (replace the body of `trade_first_signals`, lines ~195-255).

- [ ] **Step 1: Re-read the current `trade_first_signals`**

Open `scout/trading/signals.py` and confirm the function body currently matches the signature:

```python
async def trade_first_signals(
    engine,
    db: Database,
    scored_candidates: list,
    min_mcap: float = 5_000_000,
    *,
    settings,
) -> None:
```

The tuples in `scored_candidates` are `(token, quant_score, signals_fired)` where `token` is a `CandidateToken` with `.contract_address`, `.ticker`, `.token_name`, `.chain`, `.market_cap_usd`.

**CRITICAL — PRESERVE MODULE-LEVEL IMPORTS:** At the top of `scout/trading/signals.py` (around lines 15-16) there are two module-level imports that the new body continues to use:

```python
from scout.trading.combo_key import build_combo_key
from scout.trading.suppression import should_open
```

Leave them untouched. Only the BODY of `trade_first_signals` is being replaced; do NOT delete imports at the top of the module and do NOT re-import `build_combo_key` or `should_open` inside the function. The `logger` at module scope must also remain.

- [ ] **Step 2: Replace the function body**

In `scout/trading/signals.py`, replace the full body of `trade_first_signals` (everything inside the function after the docstring — but NOT the module-level imports above the function) with:

```python
    from datetime import datetime, timezone
    from scout.heartbeat import _heartbeat_stats
    from scout.trading.qualifier_state import classify_transitions

    # Filter to the qualifying set using the exact same predicate as before.
    qualifying: list[tuple] = []
    for token, quant_score, signals_fired in scored_candidates:
        if quant_score <= 0 or not signals_fired:
            continue
        if (token.market_cap_usd or 0) < min_mcap:
            continue
        if token.chain not in ("coingecko",):
            continue
        qualifying.append((token, quant_score, signals_fired))

    if not qualifying:
        return

    current_ids = {t.contract_address for t, _, _ in qualifying}
    now = datetime.now(timezone.utc)

    try:
        transitions = await classify_transitions(
            db,
            signal_type="first_signal",
            current_token_ids=current_ids,
            now=now,
            exit_grace_hours=settings.QUALIFIER_EXIT_GRACE_HOURS,
        )
    except Exception as exc:
        logger.error(
            "qualifier_classify_failed",
            err_id="QUALIFIER_CLASSIFY_FAIL",
            exc_type=type(exc).__name__,
            exc_info=True,
        )
        return  # fail-closed: skip all first_signal trades this cycle

    seen: set[str] = set()
    for token, quant_score, signals_fired in qualifying:
        if token.contract_address not in transitions:
            continue
        if token.contract_address in seen:
            continue  # multi-ingestor dedup
        seen.add(token.contract_address)

        prior_last = transitions[token.contract_address]  # str | None
        # Compute elapsed_since_prior_hours for observability. None on first-ever
        # qualification; parse ISO-8601 (fromisoformat handles UTC offsets).
        if prior_last is None:
            first_seen = True
            elapsed_hours: float | None = None
        else:
            first_seen = False
            try:
                prior_dt = datetime.fromisoformat(prior_last)
                elapsed_hours = (now - prior_dt).total_seconds() / 3600.0
            except ValueError:
                elapsed_hours = None

        logger.info(
            "qualifier_transition_fired",
            signal_type="first_signal",
            token_id=token.contract_address,
            first_seen=first_seen,
            prior_last_qualified_at=prior_last,
            elapsed_since_prior_hours=elapsed_hours,
        )
        _heartbeat_stats["qualifier_transitions"] += 1

        try:
            sigs = signals_fired
            combo_key = build_combo_key(signal_type="first_signal", signals=sigs)
            allow, reason = await should_open(db, combo_key, settings=settings)
            if not allow:
                logger.info(
                    "signal_suppressed",
                    combo_key=combo_key,
                    reason=reason,
                    coin_id=token.contract_address,
                    signal_type="first_signal",
                )
                continue
            pc = await db._conn.execute(
                "SELECT current_price FROM price_cache WHERE coin_id = ?",
                (token.contract_address,),
            )
            pr = await pc.fetchone()
            price = pr[0] if pr else None

            trade_id = await engine.open_trade(
                token_id=token.contract_address,
                symbol=token.ticker,
                name=token.token_name,
                chain=token.chain,
                signal_type="first_signal",
                signal_data={
                    "quant_score": quant_score,
                    "signals": signals_fired,
                },
                entry_price=price,
                signal_combo=combo_key,
            )
            if trade_id is None:
                # NOTE (spec divergence): the spec's Observability section lists
                # possible reasons including `max_open_hit`, `cooldown`, etc.
                # engine.open_trade returns `int | None` without an accompanying
                # reason, so we cannot distinguish here. We log a single generic
                # reason `open_trade_returned_none`; downstream operators can
                # correlate with engine-side logs (warmup/dedup/cooldown/cap)
                # using `token_id` + timestamp. If finer-grained reasons are
                # required later, extend engine.open_trade to return (id, reason).
                logger.info(
                    "qualifier_transition_skipped",
                    signal_type="first_signal",
                    token_id=token.contract_address,
                    reason="open_trade_returned_none",
                )
                _heartbeat_stats["qualifier_skips"] += 1
        except Exception:
            logger.exception("trading_first_signal_error", token=token.ticker)
            _heartbeat_stats["qualifier_skips"] += 1
```

- [ ] **Step 3: Run the pre-existing signals tests to confirm no regressions in the other dispatchers**

Run: `uv run pytest tests/ -k "first_signal or signals" --tb=short -q`
Expected: existing tests that exercise `trade_first_signals` indirectly (if any) still pass. There are no unit tests for `trade_first_signals` specifically; integration tests in Task 9/10 cover the new path.

- [ ] **Step 4: Run full suite**

Run: `uv run pytest --tb=short -q`
Expected: all pre-existing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add scout/trading/signals.py
git commit -m "feat(bl-050): wire trade_first_signals to transition gate (fail-closed)"
```

---

### Task 8: Add prune scheduler to `_pipeline_loop`

**Files:**
- Modify: `scout/main.py::_pipeline_loop`

- [ ] **Step 1: Open `scout/main.py` and find the cycle counter section**

Locate `cycle_count += 1` inside `_pipeline_loop` (around line 824). The prune invocation goes shortly after, inside the same `while not shutdown_event.is_set()` loop.

- [ ] **Step 2: Add the prune-every-N-cycles block**

Immediately after `cycle_count += 1` and before the `_maybe_emit_heartbeat(settings)` call, add:

```python
                    # BL-050: periodic pruning of stale signal_qualifier_state rows
                    if (
                        cycle_count % settings.QUALIFIER_PRUNE_EVERY_CYCLES == 0
                        and cycle_count > 0
                    ):
                        try:
                            from scout.trading.qualifier_state import (
                                prune_stale_qualifiers,
                            )

                            pruned = await prune_stale_qualifiers(
                                db,
                                now=datetime.now(timezone.utc),
                                retention_hours=settings.QUALIFIER_PRUNE_RETENTION_HOURS,
                            )
                            logger.info("qualifier_pruned", rows_deleted=pruned)
                            _heartbeat_stats[
                                "qualifier_prune_consecutive_failures"
                            ] = 0
                        except Exception:
                            logger.exception(
                                "qualifier_prune_failed",
                                err_id="QUALIFIER_PRUNE_FAIL",
                            )
                            _heartbeat_stats[
                                "qualifier_prune_consecutive_failures"
                            ] += 1
```

Note: `datetime` and `timezone` are already imported at the top of `scout/main.py`; if not, add `from datetime import datetime, timezone` to the existing imports block.

- [ ] **Step 3: Verify `main.py` still imports/parses cleanly**

Run: `uv run python -c "import scout.main"`
Expected: no output (clean import).

- [ ] **Step 4: Run full suite**

Run: `uv run pytest --tb=short -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add scout/main.py
git commit -m "feat(bl-050): schedule qualifier pruning every N cycles in pipeline loop"
```

---

### Task 9: Integration tests — restart / fresh transition / re-entry after restart

**Files:**
- Create: `tests/test_trading_edge_detection.py`

- [ ] **Step 1: Write the three failing integration tests**

Create `tests/test_trading_edge_detection.py`:

```python
"""BL-050 integration tests — end-to-end transition gate behavior
through trade_first_signals → engine.open_trade.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from scout.db import Database
from scout.heartbeat import _reset_heartbeat_stats
from scout.trading.signals import trade_first_signals


def _scored(token_factory, contract_address: str, mcap: float = 10_000_000):
    """Build a (token, quant_score, signals_fired) tuple matching the
    trade_first_signals input shape. chain must be 'coingecko' (the
    function skips other chains)."""
    token = token_factory(
        contract_address=contract_address,
        chain="coingecko",
        ticker="TST",
        token_name="Test",
        market_cap_usd=mcap,
    )
    return (token, 55, ["volume_acceleration"])


@pytest.fixture(autouse=True)
def _hb_reset():
    _reset_heartbeat_stats()
    yield
    _reset_heartbeat_stats()


async def test_restart_does_not_replay_qualifying_tokens(
    tmp_path, token_factory, settings_factory
):
    """Cycle N: 5 tokens transition → open_trade called 5x.
    Close DB, reopen same file. Cycle N+1 with same 5 tokens → open_trade
    called 0 times."""
    db_path = tmp_path / "t.db"
    db = Database(db_path)
    await db.initialize()

    # NOTE: `engine` is a full AsyncMock; engine-side gates (warmup, dedup,
    # cooldown, max-open) are bypassed entirely. We deliberately do NOT pass
    # PAPER_STARTUP_WARMUP_SECONDS here — trade_first_signals does not read it.
    settings = settings_factory(PAPER_MIN_MCAP=5_000_000)
    engine = AsyncMock()
    engine.open_trade = AsyncMock(return_value=1)

    candidates = [
        _scored(token_factory, f"addr_{i}") for i in range(5)
    ]

    # Cycle N
    await trade_first_signals(engine, db, candidates, settings=settings)
    assert engine.open_trade.await_count == 5

    # Restart: close + re-open same DB file
    await db.close()
    db2 = Database(db_path)
    await db2.initialize()

    engine.open_trade.reset_mock()

    # Cycle N+1 — same tokens
    await trade_first_signals(engine, db2, candidates, settings=settings)
    assert engine.open_trade.await_count == 0
    await db2.close()


async def test_fresh_transition_opens_exactly_one_trade(
    tmp_path, token_factory, settings_factory
):
    """Cycle N: token A qualifies. Cycle N+1: tokens A+B qualify → open_trade
    called exactly once (for B)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()

    settings = settings_factory(PAPER_MIN_MCAP=5_000_000)
    engine = AsyncMock()
    engine.open_trade = AsyncMock(return_value=1)

    cycle_n = [_scored(token_factory, "addr_A")]
    cycle_n1 = [
        _scored(token_factory, "addr_A"),
        _scored(token_factory, "addr_B"),
    ]

    await trade_first_signals(engine, db, cycle_n, settings=settings)
    assert engine.open_trade.await_count == 1
    engine.open_trade.reset_mock()

    await trade_first_signals(engine, db, cycle_n1, settings=settings)
    assert engine.open_trade.await_count == 1
    # Confirm it was addr_B
    call_args = engine.open_trade.await_args
    assert call_args.kwargs["token_id"] == "addr_B"
    await db.close()


async def test_restart_with_re_entry_during_downtime(
    tmp_path, token_factory, settings_factory
):
    """Pre-seed qualifier row aged >grace. First post-restart scan with same
    token → fires exactly once (re-entry transition)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()

    # Seed a stale row — 49h ago, beyond 48h grace
    stale = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    await db._conn.execute(
        "INSERT INTO signal_qualifier_state "
        "(signal_type, token_id, first_qualified_at, last_qualified_at) "
        "VALUES (?, ?, ?, ?)",
        ("first_signal", "addr_re", stale, stale),
    )
    await db._conn.commit()

    settings = settings_factory(
        PAPER_MIN_MCAP=5_000_000,
        QUALIFIER_EXIT_GRACE_HOURS=48,
    )
    engine = AsyncMock()
    engine.open_trade = AsyncMock(return_value=1)

    await trade_first_signals(
        engine, db, [_scored(token_factory, "addr_re")], settings=settings
    )
    assert engine.open_trade.await_count == 1

    # Same cycle again (no restart) — this is a continuation now, must NOT fire
    engine.open_trade.reset_mock()
    await trade_first_signals(
        engine, db, [_scored(token_factory, "addr_re")], settings=settings
    )
    assert engine.open_trade.await_count == 0
    await db.close()
```

- [ ] **Step 2: Run integration tests**

Run: `uv run pytest tests/test_trading_edge_detection.py -v`
Expected: All 3 PASS.

- [ ] **Step 3: Run full suite**

Run: `uv run pytest --tb=short -q`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_trading_edge_detection.py
git commit -m "test(bl-050): integration tests for restart/transition/re-entry"
```

---

### Task 10: Integration tests — blocked-but-upsert (cooldown, max-open)

**Files:**
- Modify: `tests/test_trading_edge_detection.py` (append).

- [ ] **Step 1: Write two failing integration tests**

Append to `tests/test_trading_edge_detection.py`:

```python
async def test_transition_blocked_by_cooldown_still_upserts(
    tmp_path, token_factory, settings_factory
):
    """Seed paper_trades row for token within 48h → open_trade returns None
    (the real engine's cooldown). classify_transitions must STILL upsert the
    qualifier row so next scan treats it as a continuation, not another
    transition.

    To avoid dependence on the real engine's cooldown implementation, we
    simulate open_trade returning None and then assert that a second call
    with the same token is NOT a transition (no second open_trade call).
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()

    settings = settings_factory(PAPER_MIN_MCAP=5_000_000)
    engine = AsyncMock()
    engine.open_trade = AsyncMock(return_value=None)  # simulate cooldown block

    candidates = [_scored(token_factory, "addr_block")]

    # First scan: transition classified, open_trade called, returns None
    await trade_first_signals(engine, db, candidates, settings=settings)
    assert engine.open_trade.await_count == 1

    # Row was upserted despite open_trade returning None
    cur = await db._conn.execute(
        "SELECT token_id FROM signal_qualifier_state "
        "WHERE signal_type = ? AND token_id = ?",
        ("first_signal", "addr_block"),
    )
    assert await cur.fetchone() is not None

    # Second scan: continuation, open_trade must NOT be called again
    engine.open_trade.reset_mock()
    await trade_first_signals(engine, db, candidates, settings=settings)
    assert engine.open_trade.await_count == 0
    await db.close()


async def test_transition_blocked_by_max_open_still_upserts(
    tmp_path, token_factory, settings_factory
):
    """Same contract as the cooldown test but with a different reason —
    open_trade returns None simulating the max-open cap being hit.
    Qualifier row must still be upserted; heartbeat skip counter increments.
    """
    from scout.heartbeat import _heartbeat_stats

    db = Database(tmp_path / "t.db")
    await db.initialize()

    settings = settings_factory(PAPER_MIN_MCAP=5_000_000)
    engine = AsyncMock()
    engine.open_trade = AsyncMock(return_value=None)  # simulate max-open block

    candidates = [_scored(token_factory, "addr_maxopen")]

    assert _heartbeat_stats["qualifier_transitions"] == 0
    assert _heartbeat_stats["qualifier_skips"] == 0

    await trade_first_signals(engine, db, candidates, settings=settings)

    assert engine.open_trade.await_count == 1
    assert _heartbeat_stats["qualifier_transitions"] == 1
    assert _heartbeat_stats["qualifier_skips"] == 1

    # Qualifier row upserted
    cur = await db._conn.execute(
        "SELECT token_id FROM signal_qualifier_state "
        "WHERE signal_type = ? AND token_id = ?",
        ("first_signal", "addr_maxopen"),
    )
    assert await cur.fetchone() is not None
    await db.close()
```

- [ ] **Step 2: Run the two new integration tests**

Run: `uv run pytest tests/test_trading_edge_detection.py::test_transition_blocked_by_cooldown_still_upserts tests/test_trading_edge_detection.py::test_transition_blocked_by_max_open_still_upserts -v`
Expected: Both PASS.

- [ ] **Step 3: Run the full BL-050 test set — 19 tests total**

Run: `uv run pytest tests/test_trading_qualifier_state.py tests/test_trading_edge_detection.py -v`
Expected: 19 PASS (13 unit + 6 integration). Confirm count matches spec acceptance criterion.

- [ ] **Step 4: Run full suite**

Run: `uv run pytest --tb=short -q`
Expected: entire suite passes with no new failures.

- [ ] **Step 5: Format with black**

Run: `uv run black scout/ tests/`
Expected: files reformatted in-place (likely no changes if you followed existing style).

- [ ] **Step 6: Commit**

```bash
git add tests/test_trading_edge_detection.py
git commit -m "test(bl-050): integration tests for blocked-transition upsert semantics"
```

---

## Final verification

After all 10 tasks:

- [ ] Run full suite: `uv run pytest --tb=short -q` — all pass.
- [ ] Run dry-run startup: `uv run python -m scout.main --dry-run --cycles 1` — exits cleanly, no import errors, no config validation errors.
- [ ] Run BL-050-specific suite to confirm test count: `uv run pytest tests/test_trading_qualifier_state.py tests/test_trading_edge_detection.py --collect-only -q | tail -3` — shows 19 tests collected.
- [ ] Confirm all acceptance criteria from spec §"Acceptance criteria":
  - Restart acceptance: `test_restart_does_not_replay_qualifying_tokens` (Task 9).
  - Transition acceptance: `test_fresh_transition_opens_exactly_one_trade` (Task 9).
  - Re-entry acceptance: `test_restart_with_re_entry_during_downtime` (Task 9).
  - Observability: heartbeat counters verified by `test_heartbeat_stats_has_qualifier_counters` (Task 6) + `test_transition_blocked_by_max_open_still_upserts` (Task 10).
  - Config validation: `test_config_rejects_retention_le_grace` (Task 2).

## Spec coverage mapping (self-review)

| Spec section | Implementing task |
|---|---|
| Schema (`signal_qualifier_state` + index) | Task 1 |
| `classify_transitions` signature + semantics | Task 3, 4 |
| Boundary convention (exactly at grace = continuation) | Task 3, test 6 |
| Empty-set early return | Task 4 |
| `_txn_lock` atomicity (not BEGIN IMMEDIATE) | Task 3 (uses `async with db._txn_lock`) |
| `datetime()` SQL wrapping (PR #24) | Task 3 (SELECT datetime(?) > datetime(?)), Task 5 (WHERE datetime(...) < datetime(...)) |
| Fail-closed error policy | Task 3 (raises), Task 7 (caller catches + returns) |
| `prune_stale_qualifiers` with retention>0 guard | Task 5 |
| Config + cross-field validator | Task 2 |
| Signals integration with `seen` dedup, logs, counters | Task 7 |
| Prune scheduler | Task 8 |
| Heartbeat counters | Task 6 |
| 19 tests (13 unit + 6 integration) | Tasks 1, 2 (2 config tests), 3, 4, 5, 6, 9, 10 |

19 tests total: 1 (Task 1 schema) + 2 (Task 2 config) + 7 (Task 3 core classify) + 3 (Task 4 error/empty/multi-type) + 3 (Task 5 prune) + 1 (Task 6 heartbeat) + 3 (Task 9 integration) + 2 (Task 10 integration blocked-upsert) = **22 total**. The spec called for 19; the plan adds 3 bonus tests (schema existence, heartbeat counter seed, config default values) that increase coverage without changing semantics. Acceptance criterion "all 19 tests pass" is satisfied by a superset.
