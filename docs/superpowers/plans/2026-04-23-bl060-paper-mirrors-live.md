# BL-060 Paper-Mirrors-Live Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stamp every paper trade with a `would_be_live` flag at open-time (FCFS 20-slot cap), add a tunable quant-score admission gate on `trade_first_signals`, surface a P&L-ranked live-eligible cohort on the dashboard, and ship a two-week A/B in the weekly digest.

**Architecture:** One additive schema migration (nullable column + composite index) drives everything. INSERT stamps `would_be_live` via an inline SQL subquery (`CASE WHEN min_quant_score=0 THEN NULL WHEN COUNT(open=1) < cap THEN 1 ELSE 0 END`) returning the value via `RETURNING`. Two NULL-producing regimes (pre-cutover, pre-threshold) both filter out of A/B via `WHERE would_be_live IS NOT NULL`. Score gate is local to `trade_first_signals`; other dispatchers untouched.

**Tech Stack:** Python 3.11, aiosqlite, aiohttp, Pydantic v2, pytest-asyncio, structlog, React (dashboard).

**Spec:** `docs/superpowers/specs/2026-04-23-bl060-paper-mirrors-live-design.md`

---

## Task 0: RETURNING + lastrowid probe on pinned aiosqlite

**Files:**
- Create: `scripts/bl060_returning_probe.py` (throwaway — delete after Task 3)

Goal: confirm that after `INSERT ... RETURNING v` + `cursor.fetchone()`, `cursor.lastrowid` is still populated on the pinned aiosqlite version. If the probe fails, Task 3 falls back to `SELECT last_insert_rowid()` in the same transaction.

- [ ] **Step 1: Write the probe script**

```python
import asyncio
import aiosqlite


async def probe() -> None:
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, v INTEGER)"
    )
    cur = await db.execute("INSERT INTO t (v) VALUES (42) RETURNING v")
    row = await cur.fetchone()
    assert row is not None and row[0] == 42, f"RETURNING row broken: {row}"
    assert cur.lastrowid and cur.lastrowid > 0, (
        f"lastrowid broken: got {cur.lastrowid}"
    )
    print(f"OK: RETURNING returned {row[0]}, lastrowid={cur.lastrowid}")
    await db.close()


if __name__ == "__main__":
    asyncio.run(probe())
```

- [ ] **Step 2: Run the probe**

Run: `uv run python scripts/bl060_returning_probe.py`

Expected stdout: `OK: RETURNING returned 42, lastrowid=1`

- [ ] **Step 3: Record the result in the task note**

If the probe passes, Task 3 uses `RETURNING + cursor.lastrowid` directly.

If it fails (lastrowid==0 or None after `fetchone`), Task 3 swaps the read pattern:

```python
cur = await db.execute(INSERT_SQL, params)
row = await cur.fetchone()
would_be_live_stamped = row[0] if row else None
id_cur = await db.execute("SELECT last_insert_rowid()")
trade_id = (await id_cur.fetchone())[0]
```

Probe script is deleted at the end of Task 3.

---

## Task 1: Schema migration — add `would_be_live` column + composite index

**Files:**
- Modify: `scout/db.py` (`_migrate_feedback_loop_schema` at line ~820 + `_create_tables` `paper_trades` block at line ~552)
- Test: `tests/test_trading_db_migration.py` (may exist; extend rather than replace)

- [ ] **Step 1: Write failing test — migration adds the column nullable**

Add to `tests/test_trading_db_migration.py`:

```python
import aiosqlite
import pytest
from scout.db import Database


@pytest.mark.asyncio
async def test_migration_adds_would_be_live_column(tmp_path):
    db_path = tmp_path / "gecko.db"
    db = Database(str(db_path))
    await db.initialize()

    async with aiosqlite.connect(str(db_path)) as conn:
        cur = await conn.execute("PRAGMA table_info(paper_trades)")
        rows = await cur.fetchall()
        cols = {row[1]: {"type": row[2], "notnull": row[3], "dflt": row[4]}
                for row in rows}

    assert "would_be_live" in cols, f"column missing; got {list(cols)}"
    assert cols["would_be_live"]["type"] == "INTEGER"
    assert cols["would_be_live"]["notnull"] == 0, "must be nullable"
    assert cols["would_be_live"]["dflt"] is None, "must not have default"
    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_trading_db_migration.py::test_migration_adds_would_be_live_column -v`

Expected: FAIL with `AssertionError: column missing` (column not yet defined).

- [ ] **Step 3: Add `would_be_live` to `expected_cols` in `_migrate_feedback_loop_schema`**

Locate the `expected_cols` dict in `scout/db.py` (around line 820) and add the new entry. Snippet of final state:

```python
expected_cols = {
    "signal_combo": "TEXT",
    "lead_time_vs_trending_min": "REAL",
    "lead_time_vs_trending_status": "TEXT",
    "would_be_live": "INTEGER",
}
```

- [ ] **Step 4: Add `would_be_live INTEGER` to the fresh-install `CREATE TABLE paper_trades`**

Locate the `CREATE TABLE paper_trades` block in `_create_tables` (around line 552) and add `would_be_live INTEGER` alongside `signal_combo`, `lead_time_vs_trending_min`, `lead_time_vs_trending_status`. Position: after `lead_time_vs_trending_status`.

Final column block (only the new column shown in context):

```sql
...
signal_combo TEXT,
lead_time_vs_trending_min REAL,
lead_time_vs_trending_status TEXT,
would_be_live INTEGER,
...
```

- [ ] **Step 5: Write failing test — composite index exists**

Add to `tests/test_trading_db_migration.py`:

```python
@pytest.mark.asyncio
async def test_migration_adds_would_be_live_index(tmp_path):
    db_path = tmp_path / "gecko.db"
    db = Database(str(db_path))
    await db.initialize()

    async with aiosqlite.connect(str(db_path)) as conn:
        cur = await conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='paper_trades'"
        )
        idx_rows = await cur.fetchall()

    names = {row[0] for row in idx_rows}
    assert "idx_paper_trades_would_be_live_status" in names, (
        f"index missing; got {names}"
    )
    sql = next(row[1] for row in idx_rows
               if row[0] == "idx_paper_trades_would_be_live_status")
    assert "would_be_live" in sql and "status" in sql
    assert sql.find("would_be_live") < sql.find("status"), (
        "would_be_live must be the leading column for digest index-only scan"
    )
    await db.close()
```

- [ ] **Step 6: Run test to verify it fails**

Run: `uv run pytest tests/test_trading_db_migration.py::test_migration_adds_would_be_live_index -v`

Expected: FAIL — index not yet defined.

- [ ] **Step 7: Create the index in `_create_tables`**

In `scout/db.py` `_create_tables`, after the existing `paper_trades` indexes, add:

```python
await self._conn.execute(
    "CREATE INDEX IF NOT EXISTS idx_paper_trades_would_be_live_status "
    "ON paper_trades(would_be_live, status)"
)
```

- [ ] **Step 8: Write failing test — migration is idempotent, does not overwrite NULLs**

This is test #4 from the spec. Add:

```python
@pytest.mark.asyncio
async def test_migration_preserves_pre_cutover_nulls(tmp_path):
    db_path = tmp_path / "gecko.db"
    db = Database(str(db_path))
    await db.initialize()

    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            "INSERT INTO paper_trades "
            "(token_id, symbol, name, chain, signal_type, signal_data, "
            "entry_price, amount_usd, quantity, tp_pct, sl_pct, "
            "tp_price, sl_price, status, opened_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("tok1", "SYM", "Name", "eth", "first_signal", "{}",
             1.0, 100.0, 100.0, 40.0, 20.0, 1.4, 0.8, "open",
             "2026-04-22T00:00:00"),
        )
        await conn.commit()

    await db._migrate_feedback_loop_schema()
    await db._migrate_feedback_loop_schema()

    async with aiosqlite.connect(str(db_path)) as conn:
        cur = await conn.execute(
            "SELECT would_be_live FROM paper_trades WHERE token_id='tok1'"
        )
        row = await cur.fetchone()
    assert row[0] is None, f"pre-cutover row must stay NULL; got {row[0]}"
    await db.close()
```

- [ ] **Step 9: Run all three migration tests**

Run: `uv run pytest tests/test_trading_db_migration.py -v`

Expected: all three PASS. If `_migrate_feedback_loop_schema` uses `ALTER TABLE ADD COLUMN` (it does), it is already idempotent because `expected_cols` checks `PRAGMA table_info` before adding.

- [ ] **Step 10: Commit**

```bash
git add scout/db.py tests/test_trading_db_migration.py
git commit -m "feat(bl060): add would_be_live column + composite index to paper_trades"
```

---

## Task 2: Config knobs — `PAPER_MIN_QUANT_SCORE` and `PAPER_LIVE_ELIGIBLE_CAP`

**Files:**
- Modify: `scout/config.py`
- Test: `tests/test_config.py` (if exists; otherwise skip — defaults verified via integration tests in later tasks)

- [ ] **Step 1: Add the two fields to the `Settings` BaseSettings class**

Style constraint: match existing `PAPER_*` fields in `scout/config.py:200-230` — bare annotations with inline `#` comments. Do NOT use `Field(default=..., description=...)`.

Add inside the `Settings` class alongside other `PAPER_*` fields:

```python
PAPER_MIN_QUANT_SCORE: int = 0   # 0 disables gate AND NULL-stamps would_be_live
PAPER_LIVE_ELIGIBLE_CAP: int = 20  # concurrent live-eligible slot cap
```

- [ ] **Step 2: Verify Settings still loads**

Run: `uv run python -c "from scout.config import get_settings; s = get_settings(); print(s.PAPER_MIN_QUANT_SCORE, s.PAPER_LIVE_ELIGIBLE_CAP)"`

Expected stdout: `0 20`

- [ ] **Step 3: Commit**

```bash
git add scout/config.py
git commit -m "feat(bl060): add PAPER_MIN_QUANT_SCORE and PAPER_LIVE_ELIGIBLE_CAP knobs"
```

---

## Task 3: Stamp logic — INSERT with subquery + RETURNING + cap-hit log + immutability

**Files:**
- Modify: `scout/trading/paper.py` (`PaperTrader.execute_buy` at line ~15-113)
- Modify: `scout/trading/engine.py` (`TradingEngine.open_trade` — pass settings through)
- Modify: `scout/trading/evaluator.py:219` (`long_hold` rollover caller)
- Test: `tests/test_paper_trader.py` (extend)

This task implements spec tests #1, #2, #3a, #5, #6, #7, #12, #17.

- [ ] **Step 1: Write failing test #1 — baseline subquery correctness + RETURNING/lastrowid**

Add to `tests/test_paper_trader.py`:

```python
import pytest
from scout.db import Database
from scout.trading.paper import PaperTrader


@pytest.mark.asyncio
async def test_stamp_fresh_db_first_n_up_to_cap_are_live_eligible(tmp_path):
    db = Database(str(tmp_path / "gecko.db"))
    await db.initialize()
    trader = PaperTrader(db)

    results = []
    for i in range(21):  # cap=20; 21st should stamp =0
        trade_id, stamped = await trader.execute_buy(
            token_id=f"tok{i}",
            symbol=f"S{i}",
            name=f"Name{i}",
            chain="eth",
            signal_type="first_signal",
            signal_data={"quant_score": 50},
            entry_price=1.0,
            amount_usd=100.0,
            tp_pct=40.0,
            sl_pct=20.0,
            signal_combo="first_signal",
            lead_time_vs_trending_min=None,
            lead_time_vs_trending_status=None,
            live_eligible_cap=20,
            min_quant_score=1,  # non-zero: stamps real 0/1
        )
        assert trade_id > 0, "trade_id must be populated"
        assert stamped in (0, 1), f"stamped must be 0 or 1; got {stamped}"
        results.append(stamped)

    assert sum(r == 1 for r in results) == 20, (
        f"first 20 must be =1; got {results}"
    )
    assert results[20] == 0, f"21st must be =0; got {results[20]}"
    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_paper_trader.py::test_stamp_fresh_db_first_n_up_to_cap_are_live_eligible -v`

Expected: FAIL — `execute_buy` signature does not yet accept `live_eligible_cap` / `min_quant_score` kwargs.

- [ ] **Step 3: Update `PaperTrader.execute_buy` signature to require the two kwargs**

In `scout/trading/paper.py`, change `execute_buy` signature to include required kwargs (no defaults):

```python
async def execute_buy(
    self,
    token_id: str,
    symbol: str,
    name: str,
    chain: str,
    signal_type: str,
    signal_data: dict,
    entry_price: float,
    amount_usd: float,
    tp_pct: float,
    sl_pct: float,
    signal_combo: str,
    lead_time_vs_trending_min: float | None,
    lead_time_vs_trending_status: str | None,
    *,
    live_eligible_cap: int,
    min_quant_score: int,
) -> tuple[int, int | None]:
```

Return signature changes from `int` (trade_id) to `tuple[int, int | None]` (trade_id, stamped value).

- [ ] **Step 4: Replace the INSERT inside `execute_buy` with subquery + RETURNING pattern**

Final INSERT SQL (single statement, 18 positional `?` + 2 subquery `?`):

```python
INSERT_SQL = """
INSERT INTO paper_trades
  (token_id, symbol, name, chain, signal_type, signal_data,
   entry_price, amount_usd, quantity,
   tp_pct, sl_pct, tp_price, sl_price,
   status, opened_at,
   signal_combo, lead_time_vs_trending_min, lead_time_vs_trending_status,
   would_be_live)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?,
  (SELECT CASE
     WHEN ? = 0 THEN NULL
     WHEN COUNT(*) < ? THEN 1
     ELSE 0
   END
   FROM paper_trades
   WHERE status='open' AND would_be_live=1))
RETURNING would_be_live
"""
```

Python body:

```python
import json
import structlog

log = structlog.get_logger(__name__)

quantity = amount_usd / entry_price
tp_price = entry_price * (1 + tp_pct / 100)
sl_price = entry_price * (1 - sl_pct / 100)
opened_at = self._now_iso()

params = (
    token_id, symbol, name, chain, signal_type, json.dumps(signal_data),
    entry_price, amount_usd, quantity,
    tp_pct, sl_pct, tp_price, sl_price,
    opened_at,
    signal_combo, lead_time_vs_trending_min, lead_time_vs_trending_status,
    min_quant_score, live_eligible_cap,
)

cur = await self._db._conn.execute(INSERT_SQL, params)
row = await cur.fetchone()
would_be_live_stamped = row[0] if row else None
trade_id = cur.lastrowid or 0
await self._db._conn.commit()

if would_be_live_stamped == 0:
    log.info(
        "paper_live_slot_cap_reached",
        cap=live_eligible_cap,
        signal_type=signal_type,
        signal_combo=signal_combo,
        token_id=token_id,
    )

return trade_id, would_be_live_stamped
```

**In-code comment above the SQL** (required — non-obvious WHY):

```python
# The inline subquery makes would_be_live stamping race-free at the SQL
# layer. Today, Database._conn is single-writer (aiosqlite serializes all
# ops on one connection), so the race cannot surface. The subquery is
# defensive against a future per-writer refactor. Load-bearing invariant:
# one of {single-writer connection, atomic subquery} must hold — don't
# remove both at once.
```

**If Task 0 probe failed**: swap the stamped/trade_id reads:

```python
cur = await self._db._conn.execute(INSERT_SQL, params)
row = await cur.fetchone()
would_be_live_stamped = row[0] if row else None
id_cur = await self._db._conn.execute("SELECT last_insert_rowid()")
id_row = await id_cur.fetchone()
trade_id = id_row[0] if id_row else 0
await self._db._conn.commit()
```

- [ ] **Step 5: Run test #1 — must now pass**

Run: `uv run pytest tests/test_paper_trader.py::test_stamp_fresh_db_first_n_up_to_cap_are_live_eligible -v`

Expected: PASS.

- [ ] **Step 6: Update `TradingEngine.open_trade` to thread settings**

In `scout/trading/engine.py`, find `open_trade` and update the `execute_buy` call to pass the two required kwargs:

```python
trade_id, would_be_live_stamped = await self._paper_trader.execute_buy(
    token_id=token.contract_address,
    symbol=token.ticker,
    name=token.name,
    chain=token.chain,
    signal_type=signal_type,
    signal_data=signal_data,
    entry_price=entry_price,
    amount_usd=self._settings.PAPER_TRADE_AMOUNT_USD,
    tp_pct=self._settings.PAPER_TP_PCT,
    sl_pct=self._settings.PAPER_SL_PCT,
    signal_combo=signal_combo,
    lead_time_vs_trending_min=lead_time_min,
    lead_time_vs_trending_status=lead_time_status,
    live_eligible_cap=self._settings.PAPER_LIVE_ELIGIBLE_CAP,
    min_quant_score=self._settings.PAPER_MIN_QUANT_SCORE,
)
```

If `open_trade` currently only returns `trade_id`, adjust the unpack to drop the stamped value (not used downstream at this site).

- [ ] **Step 7: Update `scout/trading/evaluator.py:219` `long_hold` rollover caller**

Locate the direct `execute_buy` call for `long_hold` rollovers. Add the two kwargs:

```python
await self._paper_trader.execute_buy(
    # ...existing args unchanged...
    live_eligible_cap=self._settings.PAPER_LIVE_ELIGIBLE_CAP,
    min_quant_score=0,  # rollovers are continuations, not new admissions — NULL-stamp
)
```

**Why `min_quant_score=0`**: rollovers carry forward an existing position; they are not a new admission decision and must be excluded from the A/B. NULL-stamp is semantically correct.

- [ ] **Step 8: Write failing test #2 — closing a =1 trade frees a slot**

```python
@pytest.mark.asyncio
async def test_closing_live_eligible_trade_frees_slot(tmp_path):
    db = Database(str(tmp_path / "gecko.db"))
    await db.initialize()
    trader = PaperTrader(db)

    async def open_one(i: int):
        return await trader.execute_buy(
            token_id=f"tok{i}", symbol=f"S{i}", name=f"N{i}", chain="eth",
            signal_type="first_signal", signal_data={"quant_score": 50},
            entry_price=1.0, amount_usd=100.0, tp_pct=40.0, sl_pct=20.0,
            signal_combo="first_signal",
            lead_time_vs_trending_min=None, lead_time_vs_trending_status=None,
            live_eligible_cap=2, min_quant_score=1,
        )

    (id1, s1), (id2, s2), (id3, s3) = [await open_one(i) for i in range(3)]
    assert (s1, s2, s3) == (1, 1, 0)

    async with db._conn.execute(
        "UPDATE paper_trades SET status='closed_tp' WHERE id=?", (id1,)
    ):
        pass
    await db._conn.commit()

    _, s4 = await open_one(99)
    assert s4 == 1, f"slot freed by close; new open must be =1; got {s4}"
    await db.close()
```

Run: `uv run pytest tests/test_paper_trader.py::test_closing_live_eligible_trade_frees_slot -v`

Expected: PASS (subquery counts `status='open' AND would_be_live=1` so closed trades are excluded automatically).

- [ ] **Step 9: Write failing test #5 — cap-hit log on stamp=0**

```python
import structlog
from structlog.testing import capture_logs


@pytest.mark.asyncio
async def test_stamp_zero_fires_cap_reached_log(tmp_path):
    db = Database(str(tmp_path / "gecko.db"))
    await db.initialize()
    trader = PaperTrader(db)

    async def open_one(i: int):
        return await trader.execute_buy(
            token_id=f"tok{i}", symbol=f"S{i}", name=f"N{i}", chain="eth",
            signal_type="first_signal", signal_data={"quant_score": 50},
            entry_price=1.0, amount_usd=100.0, tp_pct=40.0, sl_pct=20.0,
            signal_combo="first_signal",
            lead_time_vs_trending_min=None, lead_time_vs_trending_status=None,
            live_eligible_cap=1, min_quant_score=1,
        )

    await open_one(0)  # stamps =1, no log
    with capture_logs() as logs:
        await open_one(1)  # stamps =0, log fires
    events = [e for e in logs if e.get("event") == "paper_live_slot_cap_reached"]
    assert len(events) == 1, f"expected 1 cap log; got {events}"
    assert events[0]["cap"] == 1
    assert events[0]["signal_type"] == "first_signal"
    assert events[0]["signal_combo"] == "first_signal"
    assert events[0]["token_id"] == "tok1"
    await db.close()
```

Run: `uv run pytest tests/test_paper_trader.py::test_stamp_zero_fires_cap_reached_log -v`

Expected: PASS.

- [ ] **Step 10: Write failing test #6 — cap=0 stamps all =0 (when threshold active)**

```python
@pytest.mark.asyncio
async def test_cap_zero_stamps_all_zero(tmp_path):
    db = Database(str(tmp_path / "gecko.db"))
    await db.initialize()
    trader = PaperTrader(db)

    results = []
    for i in range(5):
        _, stamped = await trader.execute_buy(
            token_id=f"tok{i}", symbol=f"S{i}", name=f"N{i}", chain="eth",
            signal_type="first_signal", signal_data={"quant_score": 50},
            entry_price=1.0, amount_usd=100.0, tp_pct=40.0, sl_pct=20.0,
            signal_combo="first_signal",
            lead_time_vs_trending_min=None, lead_time_vs_trending_status=None,
            live_eligible_cap=0, min_quant_score=1,
        )
        results.append(stamped)
    assert all(r == 0 for r in results), results
    await db.close()
```

Run: `uv run pytest tests/test_paper_trader.py::test_cap_zero_stamps_all_zero -v`

Expected: PASS (subquery `COUNT(*) < 0` is always False → ELSE branch → 0).

- [ ] **Step 11: Write failing test #7 — closed =1 trades do not count toward cap**

```python
@pytest.mark.asyncio
async def test_closed_live_eligible_excluded_from_cap_count(tmp_path):
    db = Database(str(tmp_path / "gecko.db"))
    await db.initialize()
    trader = PaperTrader(db)

    # Seed 5 closed =1 rows directly
    for i in range(5):
        await db._conn.execute(
            "INSERT INTO paper_trades "
            "(token_id, symbol, name, chain, signal_type, signal_data, "
            "entry_price, amount_usd, quantity, tp_pct, sl_pct, "
            "tp_price, sl_price, status, opened_at, signal_combo, "
            "lead_time_vs_trending_min, lead_time_vs_trending_status, "
            "would_be_live) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"seeded{i}", "S", "N", "eth", "first_signal", "{}",
             1.0, 100.0, 100.0, 40.0, 20.0, 1.4, 0.8,
             "closed_tp", "2026-04-22T00:00:00",
             "first_signal", None, None, 1),
        )
    await db._conn.commit()

    _, stamped = await trader.execute_buy(
        token_id="fresh", symbol="F", name="Fresh", chain="eth",
        signal_type="first_signal", signal_data={"quant_score": 50},
        entry_price=1.0, amount_usd=100.0, tp_pct=40.0, sl_pct=20.0,
        signal_combo="first_signal",
        lead_time_vs_trending_min=None, lead_time_vs_trending_status=None,
        live_eligible_cap=2, min_quant_score=1,
    )
    assert stamped == 1, f"closed =1s should not block cap; got {stamped}"
    await db.close()
```

Run: `uv run pytest tests/test_paper_trader.py::test_closed_live_eligible_excluded_from_cap_count -v`

Expected: PASS.

- [ ] **Step 12: Write failing test #12 — `min_quant_score=0` admits and NULL-stamps**

```python
@pytest.mark.asyncio
async def test_min_quant_score_zero_null_stamps(tmp_path):
    db = Database(str(tmp_path / "gecko.db"))
    await db.initialize()
    trader = PaperTrader(db)

    results = []
    for i in range(3):
        _, stamped = await trader.execute_buy(
            token_id=f"tok{i}", symbol=f"S{i}", name=f"N{i}", chain="eth",
            signal_type="first_signal", signal_data={"quant_score": 50},
            entry_price=1.0, amount_usd=100.0, tp_pct=40.0, sl_pct=20.0,
            signal_combo="first_signal",
            lead_time_vs_trending_min=None, lead_time_vs_trending_status=None,
            live_eligible_cap=20, min_quant_score=0,
        )
        results.append(stamped)
    assert all(r is None for r in results), (
        f"regime-null stamps expected; got {results}"
    )
    await db.close()
```

Run: `uv run pytest tests/test_paper_trader.py::test_min_quant_score_zero_null_stamps -v`

Expected: PASS (subquery `WHEN ? = 0 THEN NULL`).

- [ ] **Step 13: Write failing test #17 — no-force-close regression guard**

```python
@pytest.mark.asyncio
async def test_stamped_rows_immutable_across_migrations(tmp_path):
    db = Database(str(tmp_path / "gecko.db"))
    await db.initialize()
    trader = PaperTrader(db)

    trade_id, stamped = await trader.execute_buy(
        token_id="stable", symbol="S", name="N", chain="eth",
        signal_type="first_signal", signal_data={"quant_score": 50},
        entry_price=1.0, amount_usd=100.0, tp_pct=40.0, sl_pct=20.0,
        signal_combo="first_signal",
        lead_time_vs_trending_min=None, lead_time_vs_trending_status=None,
        live_eligible_cap=20, min_quant_score=1,
    )
    assert stamped == 1

    await db._migrate_feedback_loop_schema()
    await db._migrate_feedback_loop_schema()

    cur = await db._conn.execute(
        "SELECT status, would_be_live FROM paper_trades WHERE id=?", (trade_id,)
    )
    row = await cur.fetchone()
    assert row[0] == "open", f"status must stay open; got {row[0]}"
    assert row[1] == 1, f"stamped value must stay 1; got {row[1]}"
    await db.close()
```

Run: `uv run pytest tests/test_paper_trader.py::test_stamped_rows_immutable_across_migrations -v`

Expected: PASS.

- [ ] **Step 14: Write failing test #3a — 40 sequential-on-shared-conn inserts → exactly 20 `=1`**

```python
import asyncio


@pytest.mark.asyncio
async def test_stamp_subquery_correctness_under_shared_conn(tmp_path):
    db = Database(str(tmp_path / "gecko.db"))
    await db.initialize()
    trader = PaperTrader(db)

    async def one(i: int):
        return await trader.execute_buy(
            token_id=f"tok{i}", symbol=f"S{i}", name=f"N{i}", chain="eth",
            signal_type="first_signal", signal_data={"quant_score": 50},
            entry_price=1.0, amount_usd=100.0, tp_pct=40.0, sl_pct=20.0,
            signal_combo="first_signal",
            lead_time_vs_trending_min=None, lead_time_vs_trending_status=None,
            live_eligible_cap=20, min_quant_score=1,
        )

    results = await asyncio.gather(*[one(i) for i in range(40)])
    stamps = [s for _, s in results]
    ones = sum(1 for s in stamps if s == 1)
    zeros = sum(1 for s in stamps if s == 0)
    assert ones == 20 and zeros == 20, (
        f"expected 20/20 split; got ones={ones} zeros={zeros}. "
        "Note: aiosqlite serializes shared-conn ops at worker-thread level; "
        "this test proves subquery COUNT/CASE arithmetic is correct, NOT "
        "atomicity under true parallelism (see test_paper_trader_concurrency.py)"
    )
    await db.close()
```

Run: `uv run pytest tests/test_paper_trader.py::test_stamp_subquery_correctness_under_shared_conn -v`

Expected: PASS.

- [ ] **Step 15: Delete the probe script**

```bash
rm scripts/bl060_returning_probe.py
```

- [ ] **Step 16: Run the full paper_trader test file**

Run: `uv run pytest tests/test_paper_trader.py -v`

Expected: all new tests PASS; no regressions on pre-existing tests.

- [ ] **Step 17: Commit**

```bash
git add scout/trading/paper.py scout/trading/engine.py scout/trading/evaluator.py tests/test_paper_trader.py
git rm scripts/bl060_returning_probe.py
git commit -m "feat(bl060): stamp would_be_live at INSERT via atomic subquery + RETURNING"
```

---

## Task 4: Multi-writer stress test (#3b)

**Files:**
- Create: `tests/test_paper_trader_concurrency.py`

Proves SQL correctness if the codebase ever moves from single-shared-connection to per-writer connections. WAL serializes writers (one writer at a time), so this test proves SQL correctness under contention, not true parallelism.

- [ ] **Step 1: Write the stress test**

```python
import asyncio
import aiosqlite
import pytest
from scout.db import Database


@pytest.mark.asyncio
async def test_stamp_subquery_race_free_under_multi_writer_stress(tmp_path):
    db_path = tmp_path / "gecko.db"
    db = Database(str(db_path))
    await db.initialize()
    await db.close()

    busy_retries = 0

    async def make_conn() -> aiosqlite.Connection:
        conn = await aiosqlite.connect(str(db_path))
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        return conn

    INSERT_SQL = """
    INSERT INTO paper_trades
      (token_id, symbol, name, chain, signal_type, signal_data,
       entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
       status, opened_at, signal_combo,
       lead_time_vs_trending_min, lead_time_vs_trending_status, would_be_live)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?,
      (SELECT CASE
         WHEN ? = 0 THEN NULL
         WHEN COUNT(*) < ? THEN 1
         ELSE 0
       END
       FROM paper_trades
       WHERE status='open' AND would_be_live=1))
    RETURNING would_be_live
    """

    async def insert_with_retry(conn, token_id: str):
        nonlocal busy_retries
        while True:
            try:
                params = (
                    token_id, "S", "N", "eth", "first_signal", "{}",
                    1.0, 100.0, 100.0, 40.0, 20.0, 1.4, 0.8,
                    "2026-04-22T00:00:00",
                    "first_signal", None, None,
                    1, 20,  # min_quant_score, live_eligible_cap
                )
                cur = await conn.execute(INSERT_SQL, params)
                row = await cur.fetchone()
                await conn.commit()
                return row[0]
            except aiosqlite.OperationalError as exc:
                if "SQLITE_BUSY" in str(exc) or "database is locked" in str(exc):
                    busy_retries += 1
                    await asyncio.sleep(0.01)
                    continue
                raise

    conns = [await make_conn() for _ in range(4)]

    async def worker(conn, start: int, count: int):
        return [
            await insert_with_retry(conn, f"tok{i}")
            for i in range(start, start + count)
        ]

    results = await asyncio.gather(
        worker(conns[0], 0, 10),
        worker(conns[1], 10, 10),
        worker(conns[2], 20, 10),
        worker(conns[3], 30, 10),
    )
    flat = [s for sub in results for s in sub]

    for conn in conns:
        await conn.close()

    ones = sum(1 for s in flat if s == 1)
    zeros = sum(1 for s in flat if s == 0)
    assert ones == 20 and zeros == 20, (
        f"WAL multi-writer must preserve exact cap; got ones={ones} zeros={zeros}"
    )
    # In-test comment: WAL permits one writer at a time; this test proves SQL
    # correctness under contention, not true parallelism. Prod's safety comes
    # from the single-writer connection (Database._conn).
    assert busy_retries >= 1, (
        f"expected contention; got {busy_retries} retries — "
        "is PRAGMA busy_timeout triggering instead of SQLITE_BUSY?"
    )
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/test_paper_trader_concurrency.py -v`

Expected: PASS. If `busy_retries == 0`, the WAL busy_timeout resolved contention silently (still SQL-correct, but weakens the contention-observed assertion). If this happens, increase worker count to 8 or decrease busy_timeout to 100ms in the test.

- [ ] **Step 3: Commit**

```bash
git add tests/test_paper_trader_concurrency.py
git commit -m "test(bl060): multi-writer stress proves SQL correctness under WAL contention"
```

---

## Task 5: Score-threshold gate in `trade_first_signals` + regime NULL + scope boundary

**Files:**
- Modify: `scout/trading/signals.py` (`trade_first_signals` at line ~298-384)
- Test: `tests/test_trading_signals.py`

Implements spec tests #11, #12 (signals side), #13.

- [ ] **Step 1: Write failing test #11 — below-threshold candidate is skipped + summary increments + debug-level per-token log**

Add to `tests/test_trading_signals.py` (if test file doesn't exist, create; otherwise extend):

```python
import pytest
from structlog.testing import capture_logs
from scout.config import get_settings
from scout.trading.signals import trade_first_signals
# plus the test's existing fixtures for db, trending_tracker, etc.


@pytest.mark.asyncio
async def test_candidate_below_min_quant_score_skipped(
    db_fixture, trending_tracker_fixture, combo_stats_fixture,
    settings_factory,
):
    settings = settings_factory(PAPER_MIN_QUANT_SCORE=40)
    # Seed scored_candidates with two: one at 30 (skip), one at 50 (admit).
    # Use existing scoring-stub pattern from other tests in file.
    # Expected: summary log has skipped_below_threshold=1, admitted=1,
    # debug log "signal_gated_below_threshold" captured once,
    # no info-level "signal_gated_below_threshold" present.
    with capture_logs() as logs:
        await trade_first_signals(db_fixture, trending_tracker_fixture,
                                  combo_stats_fixture, settings)

    summary = next(
        (e for e in logs if e.get("event") == "trade_first_signals_filtered"),
        None,
    )
    assert summary is not None, f"missing summary log; got events: {[e.get('event') for e in logs]}"
    assert summary.get("skipped_below_threshold") == 1, summary

    gated = [e for e in logs if e.get("event") == "signal_gated_below_threshold"]
    assert len(gated) == 1
    assert gated[0].get("log_level") == "debug", (
        f"per-token must be debug, not info; got {gated[0].get('log_level')}"
    )
```

Note: the exact fixture construction depends on existing test patterns in `tests/test_trading_signals.py`. Match existing style (mock scorer, seed trending tracker, etc.). The implementer should read the file for patterns before writing the stub.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_trading_signals.py::test_candidate_below_min_quant_score_skipped -v`

Expected: FAIL (gate logic does not yet exist).

- [ ] **Step 3: Add the gate to `trade_first_signals`**

Locate `scout/trading/signals.py:322` (the loop over `scored_candidates`). Add the gate at the top of the loop body:

```python
min_quant = settings.PAPER_MIN_QUANT_SCORE
skipped_below_threshold = 0

for token, quant_score, signals_fired in scored_candidates:
    if quant_score <= 0 or not signals_fired:
        continue
    if quant_score < min_quant:
        skipped_below_threshold += 1
        logger.debug(
            "signal_gated_below_threshold",
            coin_id=token.contract_address,
            symbol=token.ticker,
            quant_score=quant_score,
            min_quant=min_quant,
            signal_type="first_signal",
        )
        continue
    # ...existing junk / mcap / chain filters unchanged
```

- [ ] **Step 4: Add `skipped_below_threshold` to the summary log at line ~378**

Locate the existing `logger.info("trade_first_signals_filtered", ...)` call near line 378 and add the new key to its kwargs:

```python
logger.info(
    "trade_first_signals_filtered",
    total=total_scored,
    admitted=admitted,
    skipped_large=skipped_large,
    skipped_junk=skipped_junk,
    skipped_below_threshold=skipped_below_threshold,
)
```

- [ ] **Step 5: Expand the summary-log gating condition at line ~377**

The current code guards the summary log behind `if skipped_large or skipped_junk:` (to suppress noise when nothing was filtered). Expand to include threshold skips:

```python
if skipped_large or skipped_junk or skipped_below_threshold:
    logger.info(
        "trade_first_signals_filtered",
        ...  # (args as Step 4)
    )
```

Without this expansion, the summary silently drops when only threshold skips occurred — hiding the `skipped_below_threshold=N` counter the test asserts on.

- [ ] **Step 6: Run test #11 — must pass**

Run: `uv run pytest tests/test_trading_signals.py::test_candidate_below_min_quant_score_skipped -v`

Expected: PASS.

- [ ] **Step 7: Write failing test #12-signals — `PAPER_MIN_QUANT_SCORE=0` admits all**

```python
@pytest.mark.asyncio
async def test_default_zero_threshold_admits_all(
    db_fixture, trending_tracker_fixture, combo_stats_fixture,
    settings_factory,
):
    settings = settings_factory(PAPER_MIN_QUANT_SCORE=0)
    # Seed scored_candidates with a low-score (25) token that would normally
    # pass the original dispatcher. Run. Assert candidate was admitted.
    # (Stamp-side NULL assertion covered by Task 3 test #12.)
    # (Precise assertion: execute_buy was called with min_quant_score=0.)
```

- [ ] **Step 8: Run to verify it passes (because gate is `quant_score < 0` which never hits)**

Run: `uv run pytest tests/test_trading_signals.py::test_default_zero_threshold_admits_all -v`

Expected: PASS (no admission changes when threshold=0).

- [ ] **Step 9: Write failing test #13 — scope-boundary regression guard**

```python
@pytest.mark.asyncio
async def test_other_dispatchers_ignore_threshold(
    db_fixture, trending_tracker_fixture, combo_stats_fixture,
    settings_factory,
):
    settings = settings_factory(PAPER_MIN_QUANT_SCORE=99)  # blocks everything
    # Invoke each non-first-signal dispatcher with a low-score seed:
    #   trade_losers_contrarian, trade_gainers_early, trade_volume_spikes,
    #   trade_trending_catch, trade_narrative_predictions,
    #   trade_chain_completions, trade_long_holds
    # Assert each admits the trade regardless of threshold.
    # Match existing test patterns for dispatcher invocation.
```

- [ ] **Step 10: Run — must pass (gate only touches `trade_first_signals`)**

Run: `uv run pytest tests/test_trading_signals.py::test_other_dispatchers_ignore_threshold -v`

Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add scout/trading/signals.py tests/test_trading_signals.py
git commit -m "feat(bl060): add PAPER_MIN_QUANT_SCORE gate to trade_first_signals"
```

---

## Task 6: Dashboard backend — whitelist SELECT extension (#8)

**Files:**
- Modify: `dashboard/db.py` (`_get_trading_positions_inner` SELECT at line ~890)
- Test: `tests/test_trading_dashboard.py` (create if absent; otherwise extend)

- [ ] **Step 1: Write failing test — `get_trading_positions` returns `would_be_live`**

```python
import pytest
from scout.db import Database
from scout.trading.paper import PaperTrader
from dashboard.db import get_trading_positions


@pytest.mark.asyncio
async def test_dashboard_returns_would_be_live(tmp_path):
    db_path = tmp_path / "gecko.db"
    db = Database(str(db_path))
    await db.initialize()
    trader = PaperTrader(db)

    _, stamped1 = await trader.execute_buy(
        token_id="live", symbol="L", name="Live", chain="eth",
        signal_type="first_signal", signal_data={"quant_score": 50},
        entry_price=1.0, amount_usd=100.0, tp_pct=40.0, sl_pct=20.0,
        signal_combo="first_signal",
        lead_time_vs_trending_min=None, lead_time_vs_trending_status=None,
        live_eligible_cap=1, min_quant_score=1,
    )
    _, stamped2 = await trader.execute_buy(
        token_id="cap", symbol="C", name="Cap", chain="eth",
        signal_type="first_signal", signal_data={"quant_score": 50},
        entry_price=1.0, amount_usd=100.0, tp_pct=40.0, sl_pct=20.0,
        signal_combo="first_signal",
        lead_time_vs_trending_min=None, lead_time_vs_trending_status=None,
        live_eligible_cap=1, min_quant_score=1,
    )
    assert (stamped1, stamped2) == (1, 0)

    positions = await get_trading_positions(str(db_path))
    by_tok = {p["token_id"]: p for p in positions}
    assert "would_be_live" in by_tok["live"]
    assert by_tok["live"]["would_be_live"] == 1
    assert by_tok["cap"]["would_be_live"] == 0
    await db.close()
```

- [ ] **Step 2: Run — FAIL (column not in whitelist)**

Run: `uv run pytest tests/test_trading_dashboard.py -v`

Expected: FAIL (KeyError or assertion).

- [ ] **Step 3: Extend the SELECT whitelist at `dashboard/db.py:890`**

Locate the `SELECT id, token_id, ...` inside `_get_trading_positions_inner`. Append `would_be_live` to the column list. The ordering follows existing style — prefer appending at the end (easy diff to review):

```python
query = """
    SELECT id, token_id, symbol, name, chain, signal_type, signal_data,
           entry_price, amount_usd, quantity,
           tp_pct, sl_pct, tp_price, sl_price,
           status, opened_at,
           signal_combo, lead_time_vs_trending_min, lead_time_vs_trending_status,
           would_be_live
    FROM paper_trades
    WHERE status='open'
    ...
"""
```

(Implementer: match the existing exact ordering and WHERE/ORDER BY clauses; only the new column addition matters.)

- [ ] **Step 4: Run the test — must pass**

Run: `uv run pytest tests/test_trading_dashboard.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/db.py tests/test_trading_dashboard.py
git commit -m "feat(bl060): expose would_be_live from dashboard positions endpoint"
```

---

## Task 7: Dashboard frontend — Rank column + live-eligible badge + summary breakdown

**Files:**
- Modify: `dashboard/frontend/components/TradingTab.jsx` (open-positions table, lines ~134-378)

No unit tests (existing project convention — manual dashboard smoke). This task must be smoke-tested by running the dev server and opening the dashboard.

- [ ] **Step 1: Add the `pnlRankMap` useMemo**

Locate the component function body near line ~200 (where `enrichedHistory` useMemo already lives). Add, alongside it:

```jsx
const pnlRankMap = useMemo(() => {
  const byPnl = [...positions].sort(
    (a, b) => (b.unrealized_pnl_pct ?? -Infinity) - (a.unrealized_pnl_pct ?? -Infinity)
  )
  const m = new Map()
  byPnl.forEach((p, idx) => m.set(p.id, idx + 1))
  return m
}, [positions])
```

- [ ] **Step 2: Add the `Rank` header as the first column**

Locate the open-positions `<thead>`. Prepend a new header cell:

```jsx
<SortHeader col="pnl_pct" label="Rank" />
```

- [ ] **Step 3: Add the Rank cell with badge**

In the `<tbody>` row render, prepend a new `<td>` as the first cell:

```jsx
<td className="rank-cell">
  {p.unrealized_pnl_pct == null ? (
    '—'
  ) : (
    <>
      {pnlRankMap.get(p.id) ?? '—'}
      {p.would_be_live === 1 && <span className="badge-live" title="live-eligible">⚡</span>}
      {p.would_be_live === null && <span className="badge-unscoped" title="unscoped — excluded from A/B">·</span>}
    </>
  )}
</td>
```

Rules:
- `would_be_live === 1` → append `⚡` (green class).
- `would_be_live === 0` → plain rank number (no badge).
- `would_be_live === null` → append muted `·` with tooltip.
- `unrealized_pnl_pct == null` → entire cell renders `—`, no rank number, no badge.

- [ ] **Step 4: Add the summary-line breakdown at line ~318**

Locate the existing summary line that renders "N active" at around line 318. Replace with a gated, three-segment version:

```jsx
{positions.length > 0 && (
  <div className="summary-line">
    {positions.length} active
    {positions.every(p => 'would_be_live' in p) && (
      <>
        {' ('}
        <span>{positions.filter(p => p.would_be_live === 1).length} live-eligible ⚡</span>
        {' · '}
        <span>{positions.filter(p => p.would_be_live === 0).length} beyond-cap</span>
        {' · '}
        <span>{positions.filter(p => p.would_be_live === null).length} unscoped</span>
        {')'}
      </>
    )}
  </div>
)}
```

**Gating rule**: `positions.every(p => 'would_be_live' in p)`. Using `.some` would mis-render partial payloads (undefined ≠ null). `.every` keeps legacy rendering until the whole payload is post-migration.

- [ ] **Step 5: Smoke test the dashboard**

In one terminal, start the backend (if not already running locally):

```bash
uv run python -m dashboard.server
```

In another terminal, start the frontend dev server (match existing project command, e.g. `npm run dev` inside `dashboard/frontend/`).

Open the dashboard in a browser. Verify:
- `Rank` column exists and is the first column.
- Row with highest P&L shows `1 ⚡` (or `1` without badge if it's a beyond-cap row).
- Row with `unrealized_pnl_pct === null` shows `—`.
- Summary line reads `N active (X live-eligible ⚡ · Y beyond-cap · Z unscoped)`.
- Clicking Rank header sorts by P&L.

If local DB is empty of post-migration rows, manually seed a mix via `uv run python -c "import asyncio; from scout.db import Database; from scout.trading.paper import PaperTrader; ..."` OR run a `--cycles 1` pass against the dev environment.

Record smoke-test result in the commit body.

- [ ] **Step 6: Commit**

```bash
git add dashboard/frontend/components/TradingTab.jsx
git commit -m "feat(bl060): dashboard Rank column with live-eligible badge and summary breakdown"
```

---

## Task 8: Backfill audit script (`scripts/bl060_threshold_audit.py`)

**Files:**
- Create: `scripts/bl060_threshold_audit.py`

Standalone one-shot tool. Read-only. No tests (operator tool, human-read output; JSON path correctness is pre-commit-grep verified).

- [ ] **Step 1: Write the script**

```python
#!/usr/bin/env python3
"""
BL-060 threshold calibration audit.

Reads the last 7 days of first_signal paper trades, prints a quant_score
histogram, and for each candidate threshold T prints:
  - a steady-state ratio projection (current_concurrent * admits_T / admits_0)
  - a direct current-open survival count at that threshold

Usage:
    uv run python scripts/bl060_threshold_audit.py [--db path/to/gecko.db]
"""

import argparse
import asyncio
import sys
from collections import Counter

import aiosqlite


WINDOW_DAYS = 7
THRESHOLDS = [10, 20, 25, 30, 35, 40, 45, 50, 60]


async def run(db_path: str) -> int:
    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute(
            """
            SELECT
              json_extract(signal_data, '$.quant_score') AS qscore,
              status,
              opened_at
            FROM paper_trades
            WHERE signal_type = 'first_signal'
              AND opened_at >= datetime('now', ?)
            """,
            (f"-{WINDOW_DAYS} days",),
        )
        rows = await cur.fetchall()

        cur2 = await conn.execute(
            "SELECT COUNT(*) FROM paper_trades "
            "WHERE signal_type='first_signal' AND status='open'"
        )
        current_concurrent_row = await cur2.fetchone()
        current_concurrent = current_concurrent_row[0] if current_concurrent_row else 0

    if not rows:
        print("No first_signal trades in window. Cannot calibrate threshold.")
        return 2

    scores = [r[0] for r in rows if r[0] is not None]
    if not scores:
        print("All rows have NULL quant_score in signal_data. Cannot calibrate.")
        return 2

    bucket = Counter()
    for s in scores:
        b = int(s) // 10 * 10
        bucket[b] += 1

    print(f"# BL-060 threshold audit — last {WINDOW_DAYS} days")
    print(f"Total first_signal admits: {len(rows)}")
    print(f"Current concurrent open: {current_concurrent}")
    print()
    print("Score histogram (10-pt buckets):")
    for key in sorted(bucket):
        print(f"  {key:3d}-{key+9:3d}: {bucket[key]:4d}  {'#' * bucket[key]}")
    print()

    admits_at_zero = len(scores)
    print("Projection per threshold:")
    print("  (ratio = steady-state; direct = current-open survival)")
    for t in THRESHOLDS:
        admits_at_t = sum(1 for s in scores if s >= t)
        ratio = admits_at_t / admits_at_zero if admits_at_zero > 0 else 0.0
        steady_state = int(current_concurrent * ratio)

        async with aiosqlite.connect(db_path) as conn:
            c = await conn.execute(
                "SELECT COUNT(*) FROM paper_trades "
                "WHERE signal_type='first_signal' AND status='open' "
                "AND json_extract(signal_data, '$.quant_score') >= ?",
                (t,),
            )
            direct_row = await c.fetchone()
            direct = direct_row[0] if direct_row else 0

        print(f"  T={t:2d} -> {steady_state:3d} projected steady-state (ratio)")
        print(f"  T={t:2d} -> {direct:3d} current open survives (direct)")

    print()
    print("Caveats:")
    print(
        f"- Projection assumes trade-duration distribution is independent of "
        "quant_score."
    )
    print(
        f"- {WINDOW_DAYS}-day window chosen because it post-dates the BL-059 "
        "junk-filter deploy (2026-04-22); longer windows mix regimes."
    )
    print(
        "- Script does not predict would_be_live=1 stamp rate (depends on "
        "arrival ordering, not threshold)."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/gecko.db")
    args = parser.parse_args()
    return asyncio.run(run(args.db))


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the script against the local dev DB (or fail gracefully with exit code 2)**

```bash
uv run python scripts/bl060_threshold_audit.py --db data/gecko.db
```

Expected: prints histogram and projection table, exits 0. If local dev DB is empty, prints "No first_signal trades in window. Cannot calibrate threshold." and exits 2.

- [ ] **Step 3: Commit**

```bash
git add scripts/bl060_threshold_audit.py
git commit -m "feat(bl060): backfill audit script for threshold calibration"
```

---

## Task 9: Weekly digest A/B (tests #9, #14, #15, #16, #18)

**Files:**
- Modify: `scout/trading/weekly_digest.py` (add `_build_bl060_ab` + WoW two-week layout)
- Test: `tests/test_trading_digest.py` (create if absent; otherwise extend)

- [ ] **Step 1: Write failing test #9 — cohort query filters `would_be_live IS NOT NULL`**

```python
import pytest
from datetime import datetime
from scout.db import Database
from scout.trading.weekly_digest import _build_bl060_ab


@pytest.mark.asyncio
async def test_digest_ab_cohort_excludes_nulls(tmp_path, settings_factory):
    db = Database(str(tmp_path / "gecko.db"))
    await db.initialize()

    async def seed_closed(token_id, wbl, pnl_pct, opened="2026-04-25T00:00:00"):
        await db._conn.execute(
            "INSERT INTO paper_trades "
            "(token_id, symbol, name, chain, signal_type, signal_data, "
            " entry_price, amount_usd, quantity, tp_pct, sl_pct, "
            " tp_price, sl_price, status, opened_at, "
            " pnl_pct, signal_combo, "
            " lead_time_vs_trending_min, lead_time_vs_trending_status, "
            " would_be_live) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (token_id, "S", "N", "eth", "first_signal", "{}",
             1.0, 100.0, 100.0, 40.0, 20.0, 1.4, 0.8,
             "closed_tp", opened,
             pnl_pct, "first_signal", None, None, wbl),
        )
    # seed 3 live-eligible, 3 beyond-cap, 3 NULL (all within window)
    for i in range(3):
        await seed_closed(f"L{i}", 1, 5.0)
        await seed_closed(f"B{i}", 0, -2.0)
        await seed_closed(f"N{i}", None, 10.0)
    await db._conn.commit()

    section = await _build_bl060_ab(
        db, end_date=datetime(2026, 5, 2), settings=settings_factory(),
    )
    assert "live-eligible" in section.lower()
    # Live cohort should have n_closed=3 (NULLs excluded)
    assert "n_closed=3" in section
    # NULLs must NOT appear in either cohort
    await db.close()
```

- [ ] **Step 2: Run — FAIL (`_build_bl060_ab` not yet defined)**

Run: `uv run pytest tests/test_trading_digest.py -v`

Expected: FAIL with ImportError.

- [ ] **Step 3: Add the coroutine to `scout/trading/weekly_digest.py`**

Add alongside the existing `_build_*` functions. The coroutine builds a two-week side-by-side A/B section with the layout from the spec. Structure:

```python
from datetime import timedelta
import math


CLOSED_COUNTABLE_STATUSES = (
    "closed_tp", "closed_sl", "closed_expired", "closed_trailing_stop",
)


async def _build_bl060_ab(db, end_date, settings) -> str:
    """Two-week side-by-side A/B for BL-060 live-eligible cohort."""
    this_start = end_date - timedelta(days=7)
    prev_start = end_date - timedelta(days=14)

    async def cohort_stats(wbl: int, start, end):
        placeholders = ",".join("?" * len(CLOSED_COUNTABLE_STATUSES))
        cur = await db._conn.execute(
            f"""
            SELECT pnl_pct FROM paper_trades
            WHERE signal_type IN ('first_signal','trending_catch','volume_spike',
                                  'losers_contrarian','gainers_early',
                                  'narrative_prediction','chain_completion','long_hold')
              AND status IN ({placeholders})
              AND would_be_live = ?
              AND opened_at >= ?
              AND opened_at < ?
            """,
            (*CLOSED_COUNTABLE_STATUSES, wbl,
             start.isoformat(), end.isoformat()),
        )
        rows = await cur.fetchall()
        pnls = [r[0] for r in rows if r[0] is not None]
        n = len(pnls)
        if n == 0:
            return {"n": 0, "win_rate": None, "avg_pnl": None, "sharpe": None}
        wins = sum(1 for p in pnls if p > 0)
        avg = sum(pnls) / n
        if n >= 2:
            var = sum((p - avg) ** 2 for p in pnls) / (n - 1)
            sd = math.sqrt(var) if var > 0 else 0.0
            sharpe = (avg / sd) if sd > 0 else 0.0
        else:
            sharpe = 0.0
        return {
            "n": n,
            "win_rate": wins / n * 100,
            "avg_pnl": avg,
            "sharpe": sharpe,
        }

    live_this = await cohort_stats(1, this_start, end_date)
    live_prev = await cohort_stats(1, prev_start, this_start)
    beyond_this = await cohort_stats(0, this_start, end_date)
    beyond_prev = await cohort_stats(0, prev_start, this_start)

    # Context counts (open rows)
    cur = await db._conn.execute(
        "SELECT "
        "SUM(CASE WHEN would_be_live=1 THEN 1 ELSE 0 END) AS live_open, "
        "SUM(CASE WHEN would_be_live=0 THEN 1 ELSE 0 END) AS beyond_open, "
        "SUM(CASE WHEN would_be_live IS NULL THEN 1 ELSE 0 END) AS null_open "
        "FROM paper_trades WHERE status='open'"
    )
    ctx = await cur.fetchone()
    live_open, beyond_open, null_open = (ctx[0] or 0, ctx[1] or 0, ctx[2] or 0)

    out = []
    out.append("BL-060 A/B - live-eligible vs beyond-cap")
    out.append("=" * 41)
    out.append(
        f"Window:  this week ({this_start.date()} -> {end_date.date()}) "
        f"vs last week ({prev_start.date()} -> {this_start.date()})"
    )
    out.append(
        f"Context: {live_open} live-eligible open "
        f"| {beyond_open} beyond-cap open | {null_open} unscoped"
    )
    out.append("")
    out.append(_render_cohort("LIVE-ELIGIBLE (would_be_live=1, closed only)",
                              live_this, live_prev))
    out.append("")
    out.append(_render_cohort("BEYOND-CAP (would_be_live=0, closed only)",
                              beyond_this, beyond_prev))
    out.append("")
    out.append(_render_delta(live_this, live_prev, beyond_this, beyond_prev))
    out.append("")
    out.append(await _render_per_path(db, this_start, end_date))
    return "\n".join(out)


def _fmt_pct(x):
    return f"{x:+.1f}%" if x is not None else "-"


def _fmt_wr(x):
    return f"{x:.1f}%" if x is not None else "-"


def _fmt_sharpe(x, n):
    if x is None or n == 0:
        return "-"
    if n < 30:
        return f"{x:.2f} (n_closed={n}, noisy)"
    return f"{x:.2f}"


def _render_cohort(label, this_w, prev_w) -> str:
    lines = [f"{label}:"]
    lines.append(
        f"  Win-rate:  {_fmt_wr(this_w['win_rate'])} this week "
        f"| {_fmt_wr(prev_w['win_rate'])} last week   "
        f"(n_closed={this_w['n']} | {prev_w['n'] if prev_w['n'] else '-'})"
    )
    lines.append(
        f"  Avg P&L:   {_fmt_pct(this_w['avg_pnl'])} this week "
        f"| {_fmt_pct(prev_w['avg_pnl'])} last week   "
        f"(n_closed={this_w['n']} | {prev_w['n'] if prev_w['n'] else '-'})"
    )
    lines.append(
        f"  Sharpe:    {_fmt_sharpe(this_w['sharpe'], this_w['n'])} this week "
        f"| {_fmt_sharpe(prev_w['sharpe'], prev_w['n'])} last week"
    )
    return "\n".join(lines)


def _render_delta(live_t, live_p, beyond_t, beyond_p) -> str:
    lines = ["Delta (live-eligible minus beyond-cap):"]
    if live_t["n"] > 0 and beyond_t["n"] > 0:
        lines.append(
            f"  Win-rate:  "
            f"{live_t['win_rate'] - beyond_t['win_rate']:+.1f}pp this week "
            f"| {('-' if (not live_p['n'] or not beyond_p['n']) else f\"{live_p['win_rate'] - beyond_p['win_rate']:+.1f}pp\")} last week"
        )
        lines.append(
            f"  Avg P&L:   "
            f"{live_t['avg_pnl'] - beyond_t['avg_pnl']:+.1f}pp this week "
            f"| {('-' if (not live_p['n'] or not beyond_p['n']) else f\"{live_p['avg_pnl'] - beyond_p['avg_pnl']:+.1f}pp\")} last week"
        )
        # Delta excludes Sharpe when either side has n_closed < 30
        if live_t["n"] >= 30 and beyond_t["n"] >= 30:
            lines.append(
                f"  Sharpe:    "
                f"{live_t['sharpe'] - beyond_t['sharpe']:+.2f} this week"
            )
    else:
        lines.append("  - insufficient data for delta")
    return "\n".join(lines)


async def _render_per_path(db, start, end) -> str:
    placeholders = ",".join("?" * len(CLOSED_COUNTABLE_STATUSES))
    cur = await db._conn.execute(
        f"""
        SELECT signal_type,
               COUNT(*) AS n,
               AVG(CASE WHEN pnl_pct > 0 THEN 1.0 ELSE 0.0 END) * 100 AS wr,
               AVG(pnl_pct) AS avg
        FROM paper_trades
        WHERE status IN ({placeholders})
          AND would_be_live = 1
          AND opened_at >= ?
          AND opened_at < ?
        GROUP BY signal_type
        ORDER BY n DESC
        """,
        (*CLOSED_COUNTABLE_STATUSES, start.isoformat(), end.isoformat()),
    )
    rows = await cur.fetchall()
    if not rows:
        return "Per-path within live-eligible cohort: (no closed trades)"
    lines = ["Per-path within live-eligible cohort:"]
    for sig, n, wr, avg in rows:
        suffix = "  <- small-n caveat" if n < 20 else ""
        lines.append(
            f"  {sig:24s} {wr:.1f}% win, {avg:+.1f}% avg  (n_closed={n})"
            + suffix
        )
    return "\n".join(lines)
```

- [ ] **Step 4: Wire `_build_bl060_ab` into the existing digest assembly**

Find the weekly digest's top-level section-building code (uses `_try_section` pattern per spec). Add a call:

```python
sections.append(await _try_section(
    "bl060_ab", _build_bl060_ab(db, end_date, settings),
))
```

Match the exact pattern used by other `_build_*` coroutines in the same file.

- [ ] **Step 5: Run test #9 — must pass**

Run: `uv run pytest tests/test_trading_digest.py::test_digest_ab_cohort_excludes_nulls -v`

Expected: PASS.

- [ ] **Step 6: Write failing test #14 — pre-cutover + pre-threshold NULLs both excluded**

```python
@pytest.mark.asyncio
async def test_digest_ab_excludes_both_null_regimes(tmp_path, settings_factory):
    # Seed 2 pre-cutover NULLs (old rows, NULL for migration reason)
    # + 2 pre-threshold NULLs (new rows stamped NULL when min_quant_score=0)
    # + 2 live-eligible =1
    # + 2 beyond-cap =0
    # All within window.
    # Assert: live cohort n_closed=2, beyond cohort n_closed=2, NULLs vanish.
```

Run + verify.

- [ ] **Step 7: Write failing test #15 — Sharpe noisy boundary at n_closed < 30**

```python
@pytest.mark.asyncio
async def test_sharpe_noisy_boundary(...):
    # Case A: seed n_closed=22 live-eligible -> rendered contains "(n_closed=22, noisy)"
    # Case B: seed n_closed=30 live-eligible -> rendered is plain (non-noisy)
    # Case C: seed n_closed=31 live-eligible -> rendered is plain
    # Uses _fmt_sharpe directly for unit-purity.
```

Run + verify.

- [ ] **Step 8: Write failing test #16 — first-week post-cutover + zero-n_closed guard**

```python
@pytest.mark.asyncio
async def test_first_week_post_cutover_and_zero_n_guard(...):
    # Case A: seed rows only in current week (prev_start->this_start empty)
    #   -> last-week column renders "-", no crash, no divide-by-zero.
    # Case B: seed current week with non-empty cohort but 0 closed rows
    #   (all still status='open') -> metric renders "-" too.
```

Run + verify.

- [ ] **Step 9: Write failing test #18 — delta excludes Sharpe under small-n**

```python
@pytest.mark.asyncio
async def test_delta_excludes_sharpe_under_small_n(...):
    # Seed live-eligible n_closed=25, beyond-cap n_closed=60.
    # Digest delta line contains "Win-rate: +Xpp" and "Avg P&L: +Xpp"
    # but NOT any "Sharpe:" line.
```

Run + verify.

- [ ] **Step 10: Run the full digest test file**

Run: `uv run pytest tests/test_trading_digest.py -v`

Expected: all tests PASS. Also run the wider weekly-digest tests to confirm no regressions:

Run: `uv run pytest tests/test_trading_digest.py tests/test_weekly_digest.py -v`

- [ ] **Step 11: Commit**

```bash
git add scout/trading/weekly_digest.py tests/test_trading_digest.py
git commit -m "feat(bl060): weekly digest A/B with WoW two-week layout and per-path split"
```

---

## Task 10: End-to-end verification + full test suite + lint

**Files:** (none created; verification only)

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest --tb=short -q`

Expected: all tests PASS. Existing scaffold tests must not regress.

If any pre-existing test fails, diagnose — do NOT mask. Most likely failures:
- `execute_buy` call sites in test fixtures missing the required kwargs → update each call to pass `live_eligible_cap=20, min_quant_score=0`.
- Dashboard test fixtures expecting the old column list → extend.

- [ ] **Step 2: Format**

Run: `uv run black scout/ tests/ scripts/ dashboard/`

- [ ] **Step 3: Dry-run the pipeline**

Run: `uv run python -m scout.main --dry-run --cycles 1`

Expected: exits 0, logs show no schema errors, no stamp errors.

- [ ] **Step 4: Run the audit script**

Run: `uv run python scripts/bl060_threshold_audit.py --db data/gecko.db`

Expected: prints histogram (or "No first_signal trades in window." + exit 2 if dev DB is empty). Either outcome is OK for this step — the script just must not crash.

- [ ] **Step 5: Commit any lint fixups**

```bash
git add -A
git diff --cached --stat
git commit -m "style(bl060): black formatting"
```

(Skip if the commit would be empty.)

---

## Task 11: Push branch + create PR

- [ ] **Step 1: Confirm branch name**

Run: `git branch --show-current`

Expected: `feat/bl060-paper-mirrors-live` (if not, `git checkout -b feat/bl060-paper-mirrors-live` first).

- [ ] **Step 2: Push**

Run: `git push -u origin feat/bl060-paper-mirrors-live`

- [ ] **Step 3: Create PR**

PR title: `feat(paper-trading): BL-060 paper mirrors live — would_be_live flag + threshold gate + dashboard rank + digest A/B`

PR body (save to `/tmp/bl060_pr_body.md` first, then use `gh pr create --body-file /tmp/bl060_pr_body.md`):

```markdown
## Summary

- Adds `would_be_live` flag stamped at INSERT via atomic SQL subquery (FCFS 20-slot cap). Nullable; two NULL regimes (pre-cutover + pre-threshold) both filter out of A/B.
- Adds `PAPER_MIN_QUANT_SCORE` admission gate scoped to `trade_first_signals`. Default 0 = no gate + NULL-stamps.
- Dashboard: Rank column with `pnlRankMap`, live-eligible badge, three-segment summary breakdown (gated on full payload).
- Weekly digest: two-week side-by-side A/B + per-path line + Sharpe noisy annotation at n<30.
- Backfill audit script + multi-writer stress test.

## Design

See `docs/superpowers/specs/2026-04-23-bl060-paper-mirrors-live-design.md`.

## Test plan

- [x] Schema migration + index + idempotent NULL preservation
- [x] Stamp logic: cap, slot-free-on-close, cap=0, cap-hit log, threshold=0 NULL, immutability
- [x] Multi-writer stress (WAL) proves SQL correctness under contention
- [x] Score-threshold gate: skipped_below_threshold log; other dispatchers untouched
- [x] Dashboard whitelist exposes would_be_live
- [x] Digest A/B: cohort scoping, both NULL regimes, Sharpe noisy, zero-n guard, delta excludes Sharpe
- [x] Full suite green
- [x] Dry-run cycle 1 green
- [x] Manual dashboard smoke
```

Run: `gh pr create --title "feat(paper-trading): BL-060 paper mirrors live" --body-file /tmp/bl060_pr_body.md`

- [ ] **Step 4: Capture PR URL**

Run: `gh pr view --json url -q .url`

Record the URL for downstream tasks (#109 PR-diff review, #111 deploy).

---
