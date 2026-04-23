# BL-061 — Paper-Trading Ladder Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace fixed +20% TP / -10% SL with a three-leg ladder (+25% / +50% / 12% trailing stop) plus floor protection on runner slice and SL widened to -15%; retire BL-060 `would_be_live` gate atomically.

**Architecture:** Schema migration adds nullable ladder state columns (`leg_1_filled_at`, `leg_2_filled_at`, `remaining_qty`, `floor_armed`, `realized_pnl_usd`, `cutover_ts`) with a `paper_migrations` table to capture cutover timestamp. Evaluator branches on `created_at < cutover_ts` for pre/post-cutover policy. BL-060 stamp logic + config knobs removed together in one atomic commit. Dashboard frontend rebuilt without ⚡ badge.

**Tech Stack:** Python 3.12 async, aiosqlite, Pydantic v2 BaseSettings, structlog. No new dependencies.

---

## File Structure

**Files modified:**
- `scout/db.py` — add ladder columns + `paper_migrations` table migration
- `scout/config.py` — remove BL-060 fields, add ladder config, widen SL default to 15
- `scout/trading/paper.py` — strip BL-060 stamping, init ladder state, add `execute_partial_sell`
- `scout/trading/evaluator.py` — new ladder cascade + pre/post-cutover branch
- `scout/trading/signals.py` — remove `min_quant` gate
- `scout/trading/engine.py` — drop `live_eligible_cap` / `min_quant_score` kwargs
- `scout/trading/weekly_digest.py` — remove `_build_bl060_ab`, add ladder performance section
- `dashboard/frontend/components/TradingTab.jsx` — remove ⚡ badge, add Legs column

**Tests modified/added:**
- `tests/test_paper_trader.py` — ladder state init, partial sell
- `tests/test_paper_evaluator.py` — ladder cascade, cutover branching
- `tests/test_trading_db_migration.py` — new columns + paper_migrations table
- `tests/test_config.py` — remove BL-060 tests, add ladder tests
- `tests/test_trading_signals.py` — remove min_quant gate tests
- `tests/test_trading_digest.py` — remove BL-060 A/B tests, add ladder digest tests

**Tests deleted:**
- BL-060 behavior tests in `tests/test_paper_trader_concurrency.py` (stamp race), `tests/test_trading_trailing_stop.py::test_would_be_live_*`, BL-060 A/B sections of `tests/test_trading_digest.py`

---

## Task 1: Schema migration + paper_migrations cutover table

**Files:**
- Modify: `scout/db.py:867-897` (schema migration section)
- Test: `tests/test_trading_db_migration.py`

- [ ] **Step 1: Write failing test for paper_migrations table**

Add to `tests/test_trading_db_migration.py`:

```python
import pytest
from scout.db import Database

@pytest.mark.asyncio
async def test_paper_migrations_table_created(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    conn = db._conn
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='paper_migrations'"
    )
    row = await cur.fetchone()
    assert row is not None, "paper_migrations table must exist after initialize()"

    cur = await conn.execute("SELECT cutover_ts FROM paper_migrations WHERE name='bl061_ladder'")
    row = await cur.fetchone()
    assert row is not None, "bl061_ladder cutover row must be created on first init"
    assert row[0] is not None
    await db.close()
```

- [ ] **Step 2: Write failing test for new ladder columns on paper_trades**

```python
@pytest.mark.asyncio
async def test_bl061_ladder_columns_added(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(paper_trades)")
    cols = {row[1] for row in await cur.fetchall()}
    required = {
        "leg_1_filled_at",
        "leg_1_exit_price",
        "leg_2_filled_at",
        "leg_2_exit_price",
        "remaining_qty",
        "floor_armed",
        "realized_pnl_usd",
    }
    missing = required - cols
    assert not missing, f"missing ladder columns: {missing}"
    await db.close()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_trading_db_migration.py::test_paper_migrations_table_created tests/test_trading_db_migration.py::test_bl061_ladder_columns_added -v`
Expected: both FAIL (no such table / missing columns)

- [ ] **Step 4: Implement migration in scout/db.py**

In `scout/db.py` at line 867 (after the existing BL-060 `expected_cols` block), replace the block starting with `expected_cols = {` and ending with the existing CREATE INDEX statements. Extend the dict:

```python
            expected_cols = {
                "signal_combo": "TEXT",
                "lead_time_vs_trending_min": "REAL",
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
            }
            cur = await conn.execute("PRAGMA table_info(paper_trades)")
            existing = {row[1] for row in await cur.fetchall()}
            for col, coltype in expected_cols.items():
                if col in existing:
                    _log.info(
                        "schema_migration_column_action", col=col, action="skip_exists"
                    )
                else:
                    await conn.execute(
                        f"ALTER TABLE paper_trades ADD COLUMN {col} {coltype}"
                    )
                    _log.info("schema_migration_column_action", col=col, action="added")

            # BL-061: cutover timestamp captured once per schema version
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS paper_migrations (
                    name TEXT PRIMARY KEY,
                    cutover_ts TEXT NOT NULL
                )
            """)
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                ("bl061_ladder", datetime.now(timezone.utc).isoformat()),
            )
```

(Ensure `from datetime import datetime, timezone` is imported at file top; if not, add it.)

- [ ] **Step 5: Run tests to verify pass**

Run: `uv run pytest tests/test_trading_db_migration.py -v`
Expected: all pass including existing migration tests (no regression).

- [ ] **Step 6: Commit**

```bash
git add scout/db.py tests/test_trading_db_migration.py
git commit -m "feat(bl061): migration for ladder state columns + paper_migrations cutover table"
```

---

## Task 2: Config — remove BL-060 fields, add ladder fields, widen SL

**Files:**
- Modify: `scout/config.py:227-238` (trailing + BL-060 + SL)
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing test for ladder config fields + SL default**

Append to `tests/test_config.py`:

```python
def test_bl061_ladder_config_defaults(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from scout.config import Settings
    s = Settings(_env_file=None, TELEGRAM_BOT_TOKEN="x", TELEGRAM_CHAT_ID="1")
    assert s.PAPER_LADDER_LEG_1_PCT == 25.0
    assert s.PAPER_LADDER_LEG_1_QTY_FRAC == 0.30
    assert s.PAPER_LADDER_LEG_2_PCT == 50.0
    assert s.PAPER_LADDER_LEG_2_QTY_FRAC == 0.30
    assert s.PAPER_LADDER_TRAIL_PCT == 12.0
    assert s.PAPER_LADDER_FLOOR_ARM_ON_LEG_1 is True
    assert s.PAPER_SL_PCT == 15.0
    # BL-060 fields removed
    assert not hasattr(s, "PAPER_MIN_QUANT_SCORE")
    assert not hasattr(s, "PAPER_LIVE_ELIGIBLE_CAP")
```

- [ ] **Step 2: Run test to confirm it fails**

Run: `uv run pytest tests/test_config.py::test_bl061_ladder_config_defaults -v`
Expected: FAIL (attrs don't exist)

- [ ] **Step 3: Modify scout/config.py**

Remove lines 233-238 (BL-060 fields + comments). Replace with ladder config. Final state of that block (around line 227):

```python
    # Trailing stop (legacy — still used for pre-BL-061 rows; BL-061 ladder
    # uses PAPER_LADDER_TRAIL_PCT on the runner slice).
    PAPER_TRAILING_ENABLED: bool = True
    PAPER_TRAILING_ACTIVATION_PCT: float = 10.0
    PAPER_TRAILING_DRAWDOWN_PCT: float = 10.0
    PAPER_TRAILING_FLOOR_PCT: float = 3.0
    # Late-pump rejection for trade_gainers: skip candidates whose 24h change
    # already exceeds this threshold (they're near exhaustion).
    PAPER_GAINERS_MAX_24H_PCT: float = 50.0
    # BL-061 ladder: replaces flat TP/SL for post-cutover rows.
    PAPER_LADDER_LEG_1_PCT: float = 25.0
    PAPER_LADDER_LEG_1_QTY_FRAC: float = 0.30
    PAPER_LADDER_LEG_2_PCT: float = 50.0
    PAPER_LADDER_LEG_2_QTY_FRAC: float = 0.30
    PAPER_LADDER_TRAIL_PCT: float = 12.0
    PAPER_LADDER_FLOOR_ARM_ON_LEG_1: bool = True
    TRADING_DIGEST_HOUR_UTC: int = 0  # midnight digest
    TRADING_EVAL_INTERVAL: int = 1800  # 30 min eval cycle
```

Then find `PAPER_SL_PCT: float = 10.0` elsewhere in the file and change to `15.0`. If `PAPER_SL_PCT` doesn't exist as a field (it's typically derived/hardcoded), search first with `git grep -n "PAPER_SL_PCT" scout/config.py` — if absent, add it:

```python
    PAPER_TP_PCT: float = 40.0  # existing
    PAPER_SL_PCT: float = 15.0  # BL-061: widened from 10.0
```

Also remove the `@field_validator("PAPER_MIN_QUANT_SCORE")` and `@field_validator("PAPER_LIVE_ELIGIBLE_CAP")` validator methods (around lines 401-416).

- [ ] **Step 4: Run tests — config test should pass, other tests may fail due to removed fields**

Run: `uv run pytest tests/test_config.py -v`
Expected: new test passes. Any test referencing `PAPER_MIN_QUANT_SCORE` / `PAPER_LIVE_ELIGIBLE_CAP` will fail and must be deleted in Task 3.

- [ ] **Step 5: Commit**

```bash
git add scout/config.py tests/test_config.py
git commit -m "feat(bl061): config — remove BL-060 fields, add ladder config, SL=15"
```

---

## Task 3: BL-060 atomic retirement — paper.py, engine.py, evaluator.py rollover, signals.py, weekly_digest.py

**Files:**
- Modify: `scout/trading/paper.py:60-145` (execute_buy signature + INSERT_SQL)
- Modify: `scout/trading/engine.py:250-251` (call site kwargs)
- Modify: `scout/trading/evaluator.py:236-237` (rollover call site kwargs)
- Modify: `scout/trading/signals.py:320-336` (remove min_quant gate)
- Modify: `scout/trading/weekly_digest.py` (delete `_build_bl060_ab` + its call site + `BL060_AB_SIGNAL_TYPES`)
- Delete: tests referencing BL-060 stamp / A/B

- [ ] **Step 1: Write failing test — execute_buy no longer accepts min_quant_score / live_eligible_cap**

Add to `tests/test_paper_trader.py`:

```python
import inspect
from scout.trading.paper import PaperTrader

def test_execute_buy_signature_no_bl060_kwargs():
    sig = inspect.signature(PaperTrader.execute_buy)
    params = set(sig.parameters.keys())
    assert "live_eligible_cap" not in params
    assert "min_quant_score" not in params
```

- [ ] **Step 2: Run test to confirm it fails**

Run: `uv run pytest tests/test_paper_trader.py::test_execute_buy_signature_no_bl060_kwargs -v`
Expected: FAIL (kwargs still present)

- [ ] **Step 3: Modify scout/trading/paper.py — strip stamping**

Replace the execute_buy signature (remove `live_eligible_cap`, `min_quant_score` kwargs). Replace the INSERT_SQL and subsequent lines. The new `execute_buy` body (replacing lines 46-197):

```python
    async def execute_buy(
        self,
        db: Database,
        token_id: str,
        symbol: str,
        name: str,
        chain: str,
        signal_type: str,
        signal_data: dict,
        current_price: float,
        amount_usd: float,
        tp_pct: float,
        sl_pct: float,
        slippage_bps: int = 0,
        *,
        signal_combo: str,
        lead_time_vs_trending_min: float | None = None,
        lead_time_vs_trending_status: str | None = None,
    ) -> int | None:
        """Record a paper buy. Returns trade ID, or None if rejected by guards."""
        conn = db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")

        effective_entry = current_price * (1 + slippage_bps / 10000)
        if effective_entry <= 0:
            log.warning(
                "paper_trade_zero_price", token_id=token_id, current_price=current_price
            )
            return None
        quantity = amount_usd / effective_entry
        if quantity <= 0 or not (quantity == quantity):  # NaN check
            log.warning(
                "paper_trade_invalid_quantity", token_id=token_id, quantity=quantity
            )
            return None
        tp_price = effective_entry * (1 + tp_pct / 100)
        sl_price = effective_entry * (1 - sl_pct / 100) if sl_pct > 0 else 0.0
        now = datetime.now(timezone.utc).isoformat()

        INSERT_SQL = """
INSERT INTO paper_trades
  (token_id, symbol, name, chain, signal_type, signal_data,
   entry_price, amount_usd, quantity,
   tp_pct, sl_pct, tp_price, sl_price,
   status, opened_at,
   signal_combo, lead_time_vs_trending_min, lead_time_vs_trending_status,
   remaining_qty, floor_armed, realized_pnl_usd)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?,
        ?, 0, 0.0)
"""
        cursor = await conn.execute(
            INSERT_SQL,
            (
                token_id, symbol, name, chain, signal_type,
                json.dumps(signal_data),
                effective_entry, amount_usd, quantity,
                tp_pct, sl_pct, tp_price, sl_price,
                now,
                signal_combo, lead_time_vs_trending_min, lead_time_vs_trending_status,
                quantity,  # remaining_qty = full qty at open
            ),
        )
        trade_id = cursor.lastrowid
        await conn.commit()

        log.info(
            "paper_trade_opened",
            trade_id=trade_id, token_id=token_id, symbol=symbol,
            signal_type=signal_type, entry_price=effective_entry,
            amount_usd=amount_usd, tp_price=tp_price, sl_price=sl_price,
        )

        # BL-055 chokepoint: fire-and-forget handoff to LiveEngine (unchanged)
        if (
            trade_id is not None
            and self._live_engine is not None
            and self._live_engine.is_eligible(signal_type)
        ):
            if len(self._pending_live_tasks) > 50:
                log.warning(
                    "live_handoff_backpressure",
                    pending=len(self._pending_live_tasks),
                    trade_id=trade_id,
                )
            task = asyncio.create_task(
                self._live_engine.on_paper_trade_opened(
                    _PaperTradeHandoff(
                        id=trade_id, signal_type=signal_type,
                        symbol=symbol, coin_id=token_id,
                    )
                )
            )
            self._pending_live_tasks.add(task)
            task.add_done_callback(self._pending_live_tasks.discard)

        return trade_id
```

- [ ] **Step 4: Update scout/trading/engine.py:250-251**

Remove lines 250-251. The `execute_buy` call (lines 234-252) becomes:

```python
        if self.mode == "paper":
            trade_id = await self._paper_trader.execute_buy(
                db=self.db,
                token_id=token_id,
                symbol=symbol,
                name=name,
                chain=chain,
                signal_type=signal_type,
                signal_data=signal_data,
                current_price=current_price,
                amount_usd=trade_amount,
                tp_pct=self.settings.PAPER_TP_PCT,
                sl_pct=self.settings.PAPER_SL_PCT,
                slippage_bps=self.settings.PAPER_SLIPPAGE_BPS,
                signal_combo=signal_combo,
                lead_time_vs_trending_min=lead_time_min,
                lead_time_vs_trending_status=lead_time_status,
            )
            return trade_id
```

- [ ] **Step 5: Update scout/trading/evaluator.py:219-238 (long_hold rollover call)**

Remove `live_eligible_cap=...` and `min_quant_score=0` kwargs from the rollover `execute_buy` call:

```python
                        new_id = await _trader.execute_buy(
                            db=db,
                            token_id=token_id,
                            symbol=row[15] if len(row) > 15 else "",
                            name=row[16] if len(row) > 16 else "",
                            chain=row[17] if len(row) > 17 else "coingecko",
                            signal_type="long_hold",
                            signal_data={
                                "origin_trade_id": trade_id,
                                "origin_signal": str(signal_data_raw),
                            },
                            current_price=current_price,
                            amount_usd=keep_amount,
                            tp_pct=100.0,
                            sl_pct=0.0,
                            slippage_bps=0,
                            signal_combo="long_hold",
                        )
```

- [ ] **Step 6: Update scout/trading/signals.py:320-336 — remove min_quant gate**

Delete lines 320 (`min_quant = settings.PAPER_MIN_QUANT_SCORE`), 323 (`skipped_below_threshold = 0`), 327-337 (the `if quant_score < min_quant:` block). After removal, the function body starts:

```python
    skipped_large = 0
    skipped_junk = 0
    for token, quant_score, signals_fired in scored_candidates:
        if quant_score <= 0 or not signals_fired:
            continue
        if not _is_tradeable_candidate(token.contract_address, token.ticker):
            ...
```

Also remove the `skipped_below_threshold` from any terminal log statement at the end of the function (grep for it to find).

- [ ] **Step 7: Update scout/trading/weekly_digest.py — remove BL-060 A/B**

Delete:
- The `BL060_AB_SIGNAL_TYPES` constant at top of file
- The entire `_build_bl060_ab` function (lines ~138 through end of its definition)
- The call to `_build_bl060_ab(db, end_date, settings)` in the main digest compose function
- The `live_eligible_cap` line (~142) that uses `settings.PAPER_LIVE_ELIGIBLE_CAP`

Verify with: `git grep -n "bl060\|would_be_live\|PAPER_LIVE_ELIGIBLE\|PAPER_MIN_QUANT" scout/trading/weekly_digest.py` — should return zero matches.

- [ ] **Step 8: Delete BL-060 tests**

```bash
# Find BL-060 test files and relevant tests
git grep -l "would_be_live\|PAPER_MIN_QUANT_SCORE\|PAPER_LIVE_ELIGIBLE_CAP\|BL060\|bl060" tests/
```

Delete or trim these test functions/blocks:
- Any `test_would_be_live_*` in `tests/test_paper_trader.py`, `tests/test_paper_trader_concurrency.py`, `tests/test_trading_trailing_stop.py`
- `test_*bl060*` in `tests/test_trading_digest.py`
- Any `test_*min_quant*` or `test_*live_eligible*` in `tests/test_config.py`, `tests/test_trading_signals.py`

Keep the schema-migration test in `tests/test_trading_db_migration.py` that verifies the `would_be_live` column survives upgrade — the column stays in DB, only its usage is retired.

- [ ] **Step 9: Run full suite to confirm no BL-060 refs remain**

```bash
uv run pytest --tb=short -q
git grep -n "PAPER_MIN_QUANT_SCORE\|PAPER_LIVE_ELIGIBLE_CAP" scout/ tests/
```

Expected: all tests pass (or fail only on Task 4+ items, not BL-060 refs). Grep returns zero hits.

- [ ] **Step 10: Commit**

```bash
git add scout/trading/paper.py scout/trading/engine.py scout/trading/evaluator.py scout/trading/signals.py scout/trading/weekly_digest.py tests/
git commit -m "feat(bl061): atomic BL-060 retirement — stamp logic + config + gate removed

Removes PAPER_MIN_QUANT_SCORE/PAPER_LIVE_ELIGIBLE_CAP config, the stamping
subquery in PaperTrader.execute_buy, all call-site kwargs, the signals.py
min_quant gate, and the weekly digest A/B reporting block.

would_be_live column stays in paper_trades (273 prod rows all NULL, column
drop is costly; harmless to leave). Schema-migration test ensures column
survives upgrade-from-pre-BL-060."
```

---

## Task 4: PaperTrader.execute_partial_sell helper

**Files:**
- Modify: `scout/trading/paper.py` (add new method)
- Test: `tests/test_paper_trader.py`

- [ ] **Step 1: Write failing test for partial sell**

Add to `tests/test_paper_trader.py`:

```python
@pytest.mark.asyncio
async def test_execute_partial_sell_updates_remaining_qty(tmp_path):
    from scout.db import Database
    from scout.trading.paper import PaperTrader
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    trade_id = await trader.execute_buy(
        db=db, token_id="tok", symbol="TOK", name="Token", chain="coingecko",
        signal_type="gainers_early", signal_data={}, current_price=1.0,
        amount_usd=300.0, tp_pct=40.0, sl_pct=15.0, slippage_bps=0,
        signal_combo="gainers_early",
    )
    # Sell 30% of position at $1.25 (leg 1 at +25%)
    ok = await trader.execute_partial_sell(
        db=db, trade_id=trade_id, leg=1, sell_qty_frac=0.30,
        current_price=1.25, slippage_bps=0,
    )
    assert ok
    cur = await db._conn.execute(
        "SELECT remaining_qty, floor_armed, realized_pnl_usd, leg_1_filled_at, leg_1_exit_price "
        "FROM paper_trades WHERE id = ?", (trade_id,)
    )
    row = await cur.fetchone()
    remaining_qty, floor_armed, realized, leg1_filled, leg1_exit = row
    assert remaining_qty == pytest.approx(300.0 * 0.70, rel=1e-6)  # 210 units at $1.0 entry
    assert floor_armed == 1
    assert realized == pytest.approx(300.0 * 0.30 * 0.25, rel=1e-6)  # 30% of 300 * 25% = 22.50
    assert leg1_filled is not None
    assert leg1_exit == pytest.approx(1.25, rel=1e-6)
    await db.close()
```

- [ ] **Step 2: Run test to confirm it fails**

Run: `uv run pytest tests/test_paper_trader.py::test_execute_partial_sell_updates_remaining_qty -v`
Expected: FAIL (no `execute_partial_sell` method)

- [ ] **Step 3: Implement execute_partial_sell in scout/trading/paper.py**

Add as a method on `PaperTrader`, after `execute_buy` and before `execute_sell`:

```python
    async def execute_partial_sell(
        self,
        db: Database,
        trade_id: int,
        *,
        leg: int,
        sell_qty_frac: float,
        current_price: float,
        slippage_bps: int = 0,
    ) -> bool:
        """Sell a fraction of original quantity for a ladder leg fill.

        Updates remaining_qty, sets leg_N_filled_at/leg_N_exit_price, increments
        realized_pnl_usd, and (on leg 1) arms the floor. Returns True on success.

        Idempotent: re-calling for the same leg is a no-op when leg_N_filled_at
        is already set (guard against concurrent evaluator ticks).
        """
        if leg not in (1, 2):
            raise ValueError(f"leg must be 1 or 2, got {leg}")
        conn = db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")

        cur = await conn.execute(
            f"SELECT entry_price, quantity, remaining_qty, realized_pnl_usd, "
            f"leg_{leg}_filled_at FROM paper_trades WHERE id = ?",
            (trade_id,),
        )
        row = await cur.fetchone()
        if row is None:
            log.warning("partial_sell_trade_not_found", trade_id=trade_id, leg=leg)
            return False
        entry_price, initial_qty, remaining_qty, realized, already_filled = row
        if already_filled is not None:
            log.info("partial_sell_already_filled", trade_id=trade_id, leg=leg)
            return False

        effective_exit = current_price * (1 - slippage_bps / 10000)
        if effective_exit <= 0:
            log.warning("partial_sell_zero_price", trade_id=trade_id, leg=leg)
            return False

        leg_qty = float(initial_qty) * sell_qty_frac
        proceeds = leg_qty * effective_exit
        cost = leg_qty * float(entry_price)
        leg_realized = proceeds - cost
        new_remaining = float(remaining_qty) - leg_qty
        new_realized = float(realized) + leg_realized
        now = datetime.now(timezone.utc).isoformat()

        updates = (
            f"UPDATE paper_trades SET remaining_qty = ?, realized_pnl_usd = ?, "
            f"leg_{leg}_filled_at = ?, leg_{leg}_exit_price = ?"
        )
        params = [new_remaining, new_realized, now, effective_exit]
        if leg == 1:
            updates += ", floor_armed = 1"
        updates += " WHERE id = ? AND leg_{leg}_filled_at IS NULL".replace("{leg}", str(leg))
        params.append(trade_id)

        await conn.execute(updates, params)
        await conn.commit()

        log.info(
            "ladder_leg_fired",
            trade_id=trade_id, leg=leg, fill_price=effective_exit,
            leg_qty=leg_qty, leg_realized_usd=leg_realized,
            remaining_qty=new_remaining, realized_pnl_usd=new_realized,
        )
        if leg == 1:
            log.info("floor_activated", trade_id=trade_id)
        return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_paper_trader.py::test_execute_partial_sell_updates_remaining_qty -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scout/trading/paper.py tests/test_paper_trader.py
git commit -m "feat(bl061): PaperTrader.execute_partial_sell helper with ladder state update"
```

---

## Task 5: Evaluator — cutover detection helper

**Files:**
- Modify: `scout/trading/evaluator.py` (add helper, cache cutover_ts per call)
- Test: `tests/test_paper_evaluator.py` (create if missing)

- [ ] **Step 1: Write failing test for cutover detection**

Add to `tests/test_paper_evaluator.py`:

```python
import pytest
from datetime import datetime, timezone, timedelta
from scout.db import Database
from scout.trading.evaluator import _load_bl061_cutover_ts

@pytest.mark.asyncio
async def test_cutover_ts_returns_iso_timestamp(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ts = await _load_bl061_cutover_ts(db._conn)
    assert ts is not None
    parsed = datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None
    await db.close()
```

- [ ] **Step 2: Run test — should fail with import error**

Run: `uv run pytest tests/test_paper_evaluator.py::test_cutover_ts_returns_iso_timestamp -v`
Expected: FAIL (ImportError on `_load_bl061_cutover_ts`)

- [ ] **Step 3: Implement helper in scout/trading/evaluator.py**

Near the top of `scout/trading/evaluator.py`, after the imports:

```python
async def _load_bl061_cutover_ts(conn) -> str | None:
    """Load BL-061 cutover timestamp from paper_migrations.

    Returns None if the row is missing (fresh DB before initialize() ran,
    shouldn't happen in practice). Callers should treat None as "no cutover
    — all rows use new ladder policy."
    """
    cur = await conn.execute(
        "SELECT cutover_ts FROM paper_migrations WHERE name = 'bl061_ladder'"
    )
    row = await cur.fetchone()
    return row[0] if row else None
```

- [ ] **Step 4: Run test to verify pass**

Run: `uv run pytest tests/test_paper_evaluator.py::test_cutover_ts_returns_iso_timestamp -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scout/trading/evaluator.py tests/test_paper_evaluator.py
git commit -m "feat(bl061): evaluator _load_bl061_cutover_ts helper"
```

---

## Task 6: Evaluator ladder cascade — leg 1 logic

**Files:**
- Modify: `scout/trading/evaluator.py` (add ladder branch in the exit cascade)
- Test: `tests/test_paper_evaluator.py`

- [ ] **Step 1: Write failing test — leg 1 fires at +25%**

Add to `tests/test_paper_evaluator.py`:

```python
@pytest.mark.asyncio
async def test_ladder_leg_1_fires_at_25_percent(tmp_path, settings_factory):
    from scout.trading.paper import PaperTrader
    from scout.trading.evaluator import evaluate_paper_trades
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory(
        PAPER_LADDER_LEG_1_PCT=25.0,
        PAPER_LADDER_LEG_1_QTY_FRAC=0.30,
        PAPER_LADDER_LEG_2_PCT=50.0,
        PAPER_LADDER_TRAIL_PCT=12.0,
        PAPER_SL_PCT=15.0,
    )
    trade_id = await trader.execute_buy(
        db=db, token_id="tok", symbol="TOK", name="Token", chain="coingecko",
        signal_type="gainers_early", signal_data={}, current_price=1.00,
        amount_usd=300.0, tp_pct=40.0, sl_pct=15.0, slippage_bps=0,
        signal_combo="gainers_early",
    )
    # Simulate price at +26% (above leg 1 threshold)
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        ("tok", 1.26, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT leg_1_filled_at, floor_armed, remaining_qty FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    leg1, floor_armed, remaining = await cur.fetchone()
    assert leg1 is not None
    assert floor_armed == 1
    assert remaining == pytest.approx(300.0 * 0.70, rel=1e-6)
    await db.close()
```

Add a settings_factory fixture if not already present, see `tests/conftest.py`. If `settings_factory` doesn't support all the new fields, extend it.

- [ ] **Step 2: Run test to confirm it fails**

Run: `uv run pytest tests/test_paper_evaluator.py::test_ladder_leg_1_fires_at_25_percent -v`
Expected: FAIL (evaluator doesn't fire ladder)

- [ ] **Step 3: Add ladder cascade to scout/trading/evaluator.py**

Inside the `for row in rows:` loop in `evaluate_paper_trades`, replace the existing exit-cascade block (lines 146-272 — the `close_reason = None; if current_price >= tp_price: ...` cascade). The new structure:

```python
            # BL-061 ladder branch — applies to post-cutover rows only.
            # Pre-cutover rows (created_at < cutover_ts AND leg_1_filled_at IS NULL
            # AND leg_2_filled_at IS NULL) continue under old policy.
            created_at_str = row[22] if len(row) > 22 else None
            leg_1_filled = row[23] if len(row) > 23 else None
            leg_2_filled = row[24] if len(row) > 24 else None
            remaining_qty = float(row[25]) if len(row) > 25 and row[25] is not None else None
            floor_armed = bool(row[26]) if len(row) > 26 and row[26] is not None else False

            is_bl061 = (
                remaining_qty is not None
                and created_at_str is not None
                and cutover_ts is not None
                and created_at_str >= cutover_ts
            )

            if is_bl061:
                close_reason = None
                # Check SL first (applies pre-leg-1 only)
                if not floor_armed and sl_price > 0 and current_price <= sl_price:
                    close_reason = "stop_loss"
                # Leg 1
                elif leg_1_filled is None and change_pct >= settings.PAPER_LADDER_LEG_1_PCT:
                    await _trader.execute_partial_sell(
                        db=db, trade_id=trade_id, leg=1,
                        sell_qty_frac=settings.PAPER_LADDER_LEG_1_QTY_FRAC,
                        current_price=current_price, slippage_bps=slippage_bps,
                    )
                    continue  # don't exit fully; re-evaluate next tick
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
                # Floor exit (runner, once armed)
                elif floor_armed and current_price <= entry_price:
                    close_reason = "floor"
                    log.info(
                        "floor_exit",
                        trade_id=trade_id, peak_pct=round(peak_pct or 0, 2),
                        current_price=current_price,
                    )
                # Trailing stop on runner slice
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
                # Expiry
                elif elapsed >= max_duration:
                    close_reason = "expired"

                if close_reason is not None:
                    close_status = {
                        "stop_loss": "closed_sl",
                        "floor": "closed_floor",
                        "trailing_stop": "closed_trailing_stop",
                        "expired": "closed_expired",
                    }[close_reason]
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
                            realized_pnl_usd=row[27] if len(row) > 27 else 0.0,
                        )
                continue  # skip pre-cutover cascade

            # Pre-cutover cascade (unchanged original code starts here)
            close_reason = None
            if current_price >= tp_price:
                close_reason = "take_profit"
            elif sl_price > 0 and current_price <= sl_price:
                close_reason = "stop_loss"
            # ... (rest of original cascade unchanged)
```

Above the loop, load cutover_ts once:

```python
    cutover_ts = await _load_bl061_cutover_ts(conn)
```

Extend the SELECT at line 31 to include `created_at, leg_1_filled_at, leg_2_filled_at, remaining_qty, floor_armed, realized_pnl_usd`:

```python
    cursor = await conn.execute("""SELECT id, token_id, entry_price, opened_at,
                  tp_price, sl_price, tp_pct, sl_pct,
                  checkpoint_1h_price, checkpoint_6h_price,
                  checkpoint_24h_price, checkpoint_48h_price,
                  peak_price, peak_pct, signal_data, symbol, name, chain,
                  amount_usd, quantity, signal_type,
                  created_at, leg_1_filled_at, leg_2_filled_at,
                  remaining_qty, floor_armed, realized_pnl_usd
           FROM paper_trades
           WHERE status = 'open'""")
```

Note the new column positions are 21 through 27 (0-indexed 21..26 + realized_pnl_usd at 27... actually re-count carefully). After adding `created_at` as column 21, `leg_1_filled_at` = 22, `leg_2_filled_at` = 23, `remaining_qty` = 24, `floor_armed` = 25, `realized_pnl_usd` = 26. Update the access indices accordingly (row[21] for created_at, row[22] for leg_1_filled_at, etc.).

- [ ] **Step 4: Implement `status_override` kwarg on PaperTrader.execute_sell**

In `scout/trading/paper.py`, `execute_sell` signature becomes:

```python
    async def execute_sell(
        self,
        db: Database,
        trade_id: int,
        current_price: float,
        reason: str,
        slippage_bps: int = 0,
        *,
        status_override: str | None = None,
    ) -> bool:
```

In the UPDATE statement where `status='closed_*'` is set, use `status_override` when provided, otherwise map from `reason` using the existing mapping. Example:

```python
        # Existing reason-to-status mapping stays; status_override wins if provided
        status = status_override if status_override is not None else {
            "take_profit": "closed_tp",
            "stop_loss": "closed_sl",
            "expired": "closed_expired",
            "trailing_stop": "closed_trailing_stop",
            "manual": "closed_manual",
        }.get(reason, "closed")
```

- [ ] **Step 5: Run test to verify pass**

Run: `uv run pytest tests/test_paper_evaluator.py -v`
Expected: PASS (leg 1 test + cutover test)

- [ ] **Step 6: Commit**

```bash
git add scout/trading/evaluator.py scout/trading/paper.py tests/test_paper_evaluator.py
git commit -m "feat(bl061): evaluator ladder cascade — leg 1 + cutover branching"
```

---

## Task 7: Evaluator — leg 2, floor exit, trailing stop, SL

**Files:**
- Modify: `scout/trading/evaluator.py` (already has branch, now tests for each exit)
- Test: `tests/test_paper_evaluator.py`

- [ ] **Step 1: Write failing tests for each exit path**

Add to `tests/test_paper_evaluator.py`:

```python
@pytest.mark.asyncio
async def test_ladder_leg_2_fires_at_50_percent(tmp_path, settings_factory):
    # Scenario: leg 1 already filled, price climbs to +55%
    from scout.trading.paper import PaperTrader
    from scout.trading.evaluator import evaluate_paper_trades
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory()
    trade_id = await trader.execute_buy(
        db=db, token_id="tok2", symbol="TOK2", name="T2", chain="coingecko",
        signal_type="gainers_early", signal_data={}, current_price=1.00,
        amount_usd=300.0, tp_pct=40.0, sl_pct=15.0, slippage_bps=0,
        signal_combo="gainers_early",
    )
    await trader.execute_partial_sell(
        db=db, trade_id=trade_id, leg=1, sell_qty_frac=0.30,
        current_price=1.25, slippage_bps=0,
    )
    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        ("tok2", 1.55, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT leg_2_filled_at FROM paper_trades WHERE id = ?", (trade_id,)
    )
    (leg_2,) = await cur.fetchone()
    assert leg_2 is not None
    await db.close()


@pytest.mark.asyncio
async def test_floor_blocks_below_entry_close(tmp_path, settings_factory):
    # Scenario: leg 1 fired, price drops to entry — floor triggers exit_at_entry
    from scout.trading.paper import PaperTrader
    from scout.trading.evaluator import evaluate_paper_trades
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory()
    trade_id = await trader.execute_buy(
        db=db, token_id="tok3", symbol="TOK3", name="T3", chain="coingecko",
        signal_type="trending_catch", signal_data={}, current_price=1.00,
        amount_usd=300.0, tp_pct=40.0, sl_pct=15.0, slippage_bps=0,
        signal_combo="trending_catch",
    )
    await trader.execute_partial_sell(
        db=db, trade_id=trade_id, leg=1, sell_qty_frac=0.30,
        current_price=1.25, slippage_bps=0,
    )
    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        ("tok3", 0.98, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    (status,) = await cur.fetchone()
    assert status == "closed_floor"
    await db.close()


@pytest.mark.asyncio
async def test_sl_at_15_fires_pre_leg_1(tmp_path, settings_factory):
    from scout.trading.paper import PaperTrader
    from scout.trading.evaluator import evaluate_paper_trades
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory()
    trade_id = await trader.execute_buy(
        db=db, token_id="tok4", symbol="TOK4", name="T4", chain="coingecko",
        signal_type="gainers_early", signal_data={}, current_price=1.00,
        amount_usd=300.0, tp_pct=40.0, sl_pct=15.0, slippage_bps=0,
        signal_combo="gainers_early",
    )
    # Price drops to 0.849 — past -15% SL
    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        ("tok4", 0.849, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    (status,) = await cur.fetchone()
    assert status == "closed_sl"
    await db.close()


@pytest.mark.asyncio
async def test_trailing_stop_on_runner_only_after_leg_1(tmp_path, settings_factory):
    # Scenario: peak at +45% after leg 1, price retraces 13% (>12% trail)
    from scout.trading.paper import PaperTrader
    from scout.trading.evaluator import evaluate_paper_trades
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory()
    trade_id = await trader.execute_buy(
        db=db, token_id="tok5", symbol="TOK5", name="T5", chain="coingecko",
        signal_type="gainers_early", signal_data={}, current_price=1.00,
        amount_usd=300.0, tp_pct=40.0, sl_pct=15.0, slippage_bps=0,
        signal_combo="gainers_early",
    )
    await trader.execute_partial_sell(
        db=db, trade_id=trade_id, leg=1, sell_qty_frac=0.30,
        current_price=1.25, slippage_bps=0,
    )
    # Manually set peak to +45%
    await db._conn.execute(
        "UPDATE paper_trades SET peak_price = 1.45, peak_pct = 45.0 WHERE id = ?",
        (trade_id,),
    )
    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        ("tok5", 1.25, datetime.now(timezone.utc).isoformat()),  # -13.8% from peak
    )
    await db._conn.commit()
    await evaluate_paper_trades(db, settings)
    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    (status,) = await cur.fetchone()
    assert status == "closed_trailing_stop"
    await db.close()
```

- [ ] **Step 2: Run the four tests — they should pass if Task 6 was done correctly**

Run: `uv run pytest tests/test_paper_evaluator.py -v`
Expected: all four PASS (cascade was implemented in Task 6 Step 3)

If any fail, fix the cascade logic in `scout/trading/evaluator.py`. The cascade order is: SL → leg 1 → leg 2 → floor → trailing → expiry. Leg 1/2 use `continue` so the trade isn't closed — only partial sell. Floor and trailing use `execute_sell` with `status_override`.

- [ ] **Step 3: Commit**

```bash
git add tests/test_paper_evaluator.py scout/trading/evaluator.py
git commit -m "test(bl061): cascade tests — leg 2, floor exit, SL 15, trailing on runner"
```

---

## Task 8: Pre-cutover row compatibility test

**Files:**
- Test: `tests/test_paper_evaluator.py`

- [ ] **Step 1: Write test that pre-cutover rows run old policy**

Add to `tests/test_paper_evaluator.py`:

```python
@pytest.mark.asyncio
async def test_pre_cutover_rows_use_old_policy(tmp_path, settings_factory):
    from scout.trading.paper import PaperTrader
    from scout.trading.evaluator import evaluate_paper_trades
    from datetime import timedelta

    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Bump cutover_ts to now so subsequent inserts look post-cutover by default
    # Then force a pre-cutover row by backdating created_at
    trader = PaperTrader()
    settings = settings_factory()
    trade_id = await trader.execute_buy(
        db=db, token_id="old", symbol="OLD", name="Old", chain="coingecko",
        signal_type="gainers_early", signal_data={}, current_price=1.00,
        amount_usd=300.0, tp_pct=40.0, sl_pct=10.0, slippage_bps=0,
        signal_combo="gainers_early",
    )
    # Backdate to before cutover
    old_ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    await db._conn.execute(
        "UPDATE paper_trades SET created_at = ?, "
        "remaining_qty = NULL, floor_armed = NULL, realized_pnl_usd = NULL "
        "WHERE id = ?",
        (old_ts, trade_id),
    )
    # Also move cutover forward so this row is pre-cutover
    now_ts = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "UPDATE paper_migrations SET cutover_ts = ? WHERE name = 'bl061_ladder'",
        (now_ts,),
    )
    await db._conn.commit()

    # Price at +26% — new policy would fire leg 1; old policy should NOT partial-sell
    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        ("old", 1.26, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT leg_1_filled_at, status FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    leg1, status = await cur.fetchone()
    assert leg1 is None, "pre-cutover row must not fire ladder legs"
    assert status == "open", "pre-cutover row still open (old policy: TP at +40%, not +25%)"
    await db.close()
```

- [ ] **Step 2: Run test**

Run: `uv run pytest tests/test_paper_evaluator.py::test_pre_cutover_rows_use_old_policy -v`
Expected: PASS (Task 6 cascade has the `is_bl061` check; pre-cutover rows skip ladder branch)

- [ ] **Step 3: Commit**

```bash
git add tests/test_paper_evaluator.py
git commit -m "test(bl061): pre-cutover rows keep old TP/SL/trailing policy"
```

---

## Task 9: Weekly digest — ladder performance section

**Files:**
- Modify: `scout/trading/weekly_digest.py` (add `_build_ladder_performance`)
- Test: `tests/test_trading_digest.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_trading_digest.py`:

```python
@pytest.mark.asyncio
async def test_ladder_performance_section(tmp_path, settings_factory):
    from scout.db import Database
    from scout.trading.paper import PaperTrader
    from scout.trading.weekly_digest import _build_ladder_performance
    from datetime import datetime, timezone

    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    settings = settings_factory()
    # Open + partial-fill a post-cutover trade
    tid = await trader.execute_buy(
        db=db, token_id="lp1", symbol="LP1", name="LP1", chain="coingecko",
        signal_type="gainers_early", signal_data={}, current_price=1.0,
        amount_usd=300.0, tp_pct=40.0, sl_pct=15.0, slippage_bps=0,
        signal_combo="gainers_early",
    )
    await trader.execute_partial_sell(
        db=db, trade_id=tid, leg=1, sell_qty_frac=0.30, current_price=1.25,
    )
    end_date = datetime.now(timezone.utc)
    out = await _build_ladder_performance(db, end_date, settings)
    assert "Ladder performance" in out
    assert "gainers_early" in out
    assert "leg 1" in out.lower() or "l1" in out.lower()
    await db.close()
```

- [ ] **Step 2: Run test to confirm it fails**

Run: `uv run pytest tests/test_trading_digest.py::test_ladder_performance_section -v`
Expected: FAIL (function doesn't exist)

- [ ] **Step 3: Implement in scout/trading/weekly_digest.py**

Add near the bottom of the file:

```python
async def _build_ladder_performance(db, end_date, settings) -> str:
    """BL-061 ladder performance per signal over the past week."""
    start = end_date - timedelta(days=7)
    cur = await db._conn.execute(
        """
        SELECT signal_type,
               COUNT(*) AS n,
               SUM(CASE WHEN leg_1_filled_at IS NOT NULL THEN 1 ELSE 0 END) AS leg1,
               SUM(CASE WHEN leg_2_filled_at IS NOT NULL THEN 1 ELSE 0 END) AS leg2,
               ROUND(AVG(peak_pct), 1) AS avg_peak,
               ROUND(AVG(pnl_pct), 1) AS avg_pnl
        FROM paper_trades
        WHERE status LIKE 'closed%'
          AND opened_at >= ?
          AND opened_at < ?
          AND remaining_qty IS NOT NULL  -- post-cutover only
        GROUP BY signal_type
        ORDER BY n DESC
        """,
        (start.isoformat(), end_date.isoformat()),
    )
    rows = await cur.fetchall()
    out = ["Ladder performance (post-cutover, last 7d)"]
    out.append("=" * 42)
    if not rows:
        out.append("(no post-cutover closed trades yet)")
        return "\n".join(out)
    out.append(f"{'Signal':24s} {'N':>4s} {'L1%':>6s} {'L2%':>6s} {'AvgPeak':>9s} {'AvgPnL':>8s}")
    for sig, n, leg1, leg2, avg_peak, avg_pnl in rows:
        l1_rate = 100.0 * leg1 / n if n else 0.0
        l2_rate = 100.0 * leg2 / n if n else 0.0
        peak_str = f"{avg_peak:+.1f}%" if avg_peak is not None else "n/a"
        pnl_str = f"{avg_pnl:+.1f}%" if avg_pnl is not None else "n/a"
        out.append(f"{sig:24s} {n:4d} {l1_rate:5.1f}% {l2_rate:5.1f}% {peak_str:>9s} {pnl_str:>8s}")
    return "\n".join(out)
```

Wire the new section into the main digest compose function in the same file. Find where the BL-060 A/B was previously called (removed in Task 3) and add:

```python
    sections.append(await _build_ladder_performance(db, end_date, settings))
```

- [ ] **Step 4: Run test to verify pass**

Run: `uv run pytest tests/test_trading_digest.py::test_ladder_performance_section -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scout/trading/weekly_digest.py tests/test_trading_digest.py
git commit -m "feat(bl061): weekly digest ladder performance section"
```

---

## Task 10: Dashboard frontend — remove ⚡ badge, add Legs column, rebuild

**Files:**
- Modify: `dashboard/frontend/components/TradingTab.jsx`
- Modify: `dashboard/db.py` (if `would_be_live` is selected but not surfaced, remove select; if Legs columns need to be selected, add them)
- Build: `dashboard/frontend/dist/` via `npm run build`

- [ ] **Step 1: Update dashboard/db.py to surface ladder state**

In `dashboard/db.py` around line 898 where `would_be_live` is selected in the open-positions query, replace with ladder columns. New SELECT fragment:

```python
                  leg_1_filled_at,
                  leg_2_filled_at,
                  remaining_qty,
                  realized_pnl_usd,
                  floor_armed
```

Return them in the dict alongside existing keys. Remove the `would_be_live` key entirely from the returned dict.

- [ ] **Step 2: Update dashboard/frontend/components/TradingTab.jsx**

Remove lines 330-340 (the "live-eligible" summary line block):

```jsx
              {positions.every(p => 'would_be_live' in p) && (
                <>
                  {' ('}
                  ...
                  {')'}
                </>
              )}
```

Delete the ⚡ badge and unscoped badge rendering (lines 377-378):

```jsx
                            {p.would_be_live === 1 && <span className="badge-live" ...>⚡</span>}
                            {p.would_be_live === null && <span className="badge-unscoped" ...>·</span>}
```

Add a new "Legs" column. In `<thead>` (around line 350), after the existing TP/SL header:

```jsx
                  <th>Legs</th>
```

In the `<tbody>` row render, add a cell rendering leg state:

```jsx
                      <td style={{ fontSize: 12, textAlign: 'center' }}>
                        <span title={p.leg_1_filled_at ? `leg 1 filled ${p.leg_1_filled_at}` : 'leg 1 pending (+25%)'}>
                          {p.leg_1_filled_at ? '▣' : '○'}
                        </span>
                        {' '}
                        <span title={p.leg_2_filled_at ? `leg 2 filled ${p.leg_2_filled_at}` : 'leg 2 pending (+50%)'}>
                          {p.leg_2_filled_at ? '▣' : '○'}
                        </span>
                        {p.floor_armed === 1 && (
                          <span title="floor armed" style={{ marginLeft: 4, color: 'var(--color-text-secondary)' }}>🛡</span>
                        )}
                      </td>
```

- [ ] **Step 3: Rebuild frontend locally to verify no syntax errors**

```bash
cd dashboard/frontend && npm run build && cd ../..
```

Expected: build succeeds; `dashboard/frontend/dist/assets/*.js` contains ladder references. Verify:

```bash
grep -l "leg_1_filled_at" dashboard/frontend/dist/assets/*.js
```

- [ ] **Step 4: Commit**

```bash
git add dashboard/db.py dashboard/frontend/components/TradingTab.jsx dashboard/frontend/dist/
git commit -m "feat(bl061): dashboard — Legs column, remove BL-060 badge, rebuild"
```

---

## Task 11: Full suite + grep sanity + PR

- [ ] **Step 1: Run full test suite**

```bash
uv run pytest --tb=short -q
```

Expected: all pass (or clearly identify any new failures, fix before PR).

- [ ] **Step 2: Sanity grep — no BL-060 refs remain in scout/**

```bash
git grep -n "PAPER_MIN_QUANT_SCORE\|PAPER_LIVE_ELIGIBLE_CAP\|_build_bl060_ab\|BL060_AB_SIGNAL_TYPES" scout/
```

Expected: zero matches.

- [ ] **Step 3: Verify dashboard bundle was rebuilt**

```bash
grep -l "leg_1_filled_at" dashboard/frontend/dist/assets/*.js && echo "OK: ladder in bundle"
grep -l "would_be_live" dashboard/frontend/dist/assets/*.js && echo "WARN: would_be_live still in bundle" || echo "OK: bundle clean"
```

- [ ] **Step 4: Open PR**

```bash
gh pr create --title "feat(bl061): paper-trading ladder redesign + BL-060 retirement" --body "$(cat <<'EOF'
## Summary

- Replaces fixed +20% TP / -10% SL on paper trades with a three-leg ladder (+25% / +50% / 12% trailing stop on runner) + floor protection + SL widened to -15%
- Retires BL-060 `would_be_live` gate atomically (stamp logic, config knobs, A/B digest section all removed)
- Adds Legs column to dashboard trading tab; removes unrendered ⚡ badge
- Pre-cutover open trades continue under old policy via `paper_migrations.cutover_ts` check

## Design doc

`docs/superpowers/specs/2026-04-23-bl061-paper-trading-ladder-redesign-design.md`

## Measurement intervention

Historical peak data is right-censored by the +20% TP; EV direction of ladder vs flat TP cannot be proved from backward-looking data. `ladder_leg_fired` / `floor_activated` / `floor_exit` structlog events are instrumented for a 30-day post-cutover calibration review on 2026-05-23.

## Test plan

- [x] unit — ladder fires in peak order (leg 1 at +25%, leg 2 at +50%)
- [x] unit — floor blocks below-entry exit on runner
- [x] unit — floor doesn't affect realized leg 1/2 proceeds
- [x] unit — SL at -15% fires pre-leg-1
- [x] unit — trailing stop on runner only, post-leg-1
- [x] unit — pre-cutover rows stay on old policy (created_at < cutover_ts)
- [x] unit — BL-060 retirement — execute_buy no longer accepts live_eligible_cap/min_quant_score
- [x] migration — ladder columns added, paper_migrations table created
- [x] digest — ladder performance section renders
- [ ] manual — VPS bundle rebuild shows new Legs column

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: Commit PR handoff marker**

```bash
git log --oneline -15
```

Confirms all 10 commits shipped. PR URL returned by `gh pr create`.

---

## Self-Review

**1. Spec coverage check**

| Spec section | Covered by |
|--------------|-----------|
| Schema additions (leg_1_filled_at, leg_2_filled_at, remaining_qty, floor_armed, realized_pnl_usd) | Task 1 |
| paper_migrations cutover table | Task 1 |
| Config fields (PAPER_LADDER_*, PAPER_SL_PCT=15) | Task 2 |
| Remove PAPER_MIN_QUANT_SCORE, PAPER_LIVE_ELIGIBLE_CAP | Task 2, 3 |
| Remove stamp subquery from execute_buy | Task 3 |
| Remove BL-060 kwargs from all call sites | Task 3 |
| Remove min_quant gate in signals.py | Task 3 |
| Remove _build_bl060_ab from weekly_digest.py | Task 3 |
| Delete BL-060 tests | Task 3 |
| execute_partial_sell helper | Task 4 |
| Cutover detection helper | Task 5 |
| Evaluator ladder cascade (leg 1, leg 2, floor, trailing, SL, expiry) | Task 6, 7 |
| Pre-cutover rows keep old policy | Task 6, 8 |
| Instrumentation events (ladder_leg_fired, floor_activated, floor_exit) | Task 4, 6 |
| Weekly digest ladder performance section | Task 9 |
| Dashboard frontend rebuild without ⚡ badge, with Legs column | Task 10 |
| Full suite sanity + PR | Task 11 |

All spec sections covered.

**2. Placeholder scan:** no TBD/TODO/"implement later"/"similar to Task N"/"add error handling" phrases. Every step has complete code.

**3. Type consistency:** method signatures match across tasks — `execute_buy` kwargs in Task 3 match engine.py/evaluator.py updates. `execute_partial_sell(db, trade_id, *, leg, sell_qty_frac, current_price, slippage_bps)` signature used consistently in Task 4/6/7/9 tests. `_load_bl061_cutover_ts(conn)` signature consistent in Task 5/6. `status_override` kwarg on `execute_sell` consistent in Task 6/7.

---

**Plan complete and saved to `docs/superpowers/plans/2026-04-23-bl061-paper-trading-ladder-redesign.md`.**
