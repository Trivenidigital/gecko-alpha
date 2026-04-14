# Paper Trading Engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pluggable paper trading engine that opens simulated trades on every signal, tracks PnL at multi-checkpoints with TP/SL, sends daily digest. Foundation for live DEX trading in Phase B. $0 cost -- paper mode uses price_cache from existing pipeline.

**Architecture:** New `scout/trading/` package. TradingEngine is called from every signal detection point in `scout/main.py`. PaperTrader simulates fills with slippage. Evaluator runs every 30 minutes on the EVALUATE interval. Digest fires at midnight UTC. Dashboard gets 4 new API endpoints.

**Tech Stack:** Python 3.12, aiosqlite, Pydantic v2, structlog, pytest (asyncio_mode=auto)

**Spec:** `docs/superpowers/specs/2026-04-19-paper-trading-engine-design.md`

**Reviewer feedback incorporated:**
1. Price staleness check: skip trade if `price_cache.updated_at` > 300s old
2. `sl_pct` positive convention: `PAPER_SL_PCT=10.0` means 10% loss, validator rejects negative
3. Slippage simulation: `PAPER_SLIPPAGE_BPS=50` (0.5%), applied at entry and exit
4. Batch price lookup: evaluator uses single `SELECT ... WHERE coin_id IN (...)` query

---

## File Map

### New files (create)

| File | Responsibility |
|------|---------------|
| `scout/trading/__init__.py` | Package init |
| `scout/trading/models.py` | PaperTrade, TradeSummary Pydantic models |
| `scout/trading/paper.py` | PaperTrader -- simulate fills with slippage, log to DB |
| `scout/trading/engine.py` | TradingEngine -- pluggable interface, mode routing, exposure control |
| `scout/trading/evaluator.py` | Checkpoint updates + TP/SL closure + peak tracking |
| `scout/trading/digest.py` | Daily PnL summary builder for Telegram |
| `tests/test_trading_models.py` | Model validation + sl_pct validator |
| `tests/test_trading_db.py` | DB schema creation & constraints |
| `tests/test_paper_trader.py` | Buy/sell simulation, PnL calculation, slippage |
| `tests/test_trading_engine.py` | Engine interface: open/close/positions, exposure, staleness, dedup |
| `tests/test_trading_evaluator.py` | Checkpoint updates, TP/SL closure, expiry, batch query |
| `tests/test_trading_digest.py` | Digest formatting, by-signal-type aggregation |
| `tests/test_trading_dashboard.py` | Dashboard API endpoints for trading |

### Modified files

| File | Changes |
|------|---------|
| `scout/config.py` | Add 10 `PAPER_*` / `TRADING_*` config fields + sl_pct validator |
| `scout/db.py` | Add `paper_trades` + `paper_daily_summary` tables to `_create_tables()` |
| `scout/main.py` | Wire engine at signal points, evaluator on EVALUATE interval, digest at midnight |
| `dashboard/api.py` | Add 4 trading endpoints |
| `dashboard/db.py` | Add trading query functions |
| `.env.example` | Add `TRADING_*` / `PAPER_*` env vars |

---

## Task 1: Models + Config

**Files:**
- Create: `scout/trading/__init__.py`
- Create: `scout/trading/models.py`
- Modify: `scout/config.py`
- Modify: `.env.example`
- Test: `tests/test_trading_models.py`

- [ ] **Step 1: Write failing tests for models**

```python
# tests/test_trading_models.py
"""Tests for paper trading Pydantic models."""
from datetime import datetime, timezone

import pytest

from scout.trading.models import PaperTrade, TradeSummary


def test_paper_trade_required_fields():
    now = datetime.now(timezone.utc)
    trade = PaperTrade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={"spike_ratio": 12.3},
        entry_price=50000.0,
        amount_usd=1000.0,
        quantity=0.02,
        tp_pct=20.0,
        sl_pct=10.0,
        tp_price=60000.0,
        sl_price=45000.0,
        opened_at=now,
    )
    assert trade.id is None
    assert trade.status == "open"
    assert trade.exit_price is None
    assert trade.pnl_usd is None
    assert trade.peak_price is None


def test_paper_trade_all_checkpoints_nullable():
    now = datetime.now(timezone.utc)
    trade = PaperTrade(
        token_id="ethereum",
        symbol="ETH",
        name="Ethereum",
        chain="coingecko",
        signal_type="narrative_prediction",
        signal_data={"fit": 85},
        entry_price=3000.0,
        amount_usd=1000.0,
        quantity=0.333,
        tp_pct=20.0,
        sl_pct=10.0,
        tp_price=3600.0,
        sl_price=2700.0,
        opened_at=now,
    )
    assert trade.checkpoint_1h_price is None
    assert trade.checkpoint_6h_price is None
    assert trade.checkpoint_24h_price is None
    assert trade.checkpoint_48h_price is None


def test_paper_trade_closed_state():
    now = datetime.now(timezone.utc)
    trade = PaperTrade(
        token_id="solana",
        symbol="SOL",
        name="Solana",
        chain="coingecko",
        signal_type="trending_catch",
        signal_data={"trending_rank": 3},
        entry_price=100.0,
        amount_usd=1000.0,
        quantity=10.0,
        tp_pct=20.0,
        sl_pct=10.0,
        tp_price=120.0,
        sl_price=90.0,
        status="closed_tp",
        exit_price=121.0,
        exit_reason="take_profit",
        pnl_usd=210.0,
        pnl_pct=21.0,
        opened_at=now,
        closed_at=now,
    )
    assert trade.status == "closed_tp"
    assert trade.pnl_usd == 210.0


def test_sl_pct_must_be_positive():
    """sl_pct uses positive convention: 10.0 means 10% stop loss."""
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="sl_pct must be positive"):
        PaperTrade(
            token_id="bitcoin",
            symbol="BTC",
            name="Bitcoin",
            chain="coingecko",
            signal_type="volume_spike",
            signal_data={},
            entry_price=50000.0,
            amount_usd=1000.0,
            quantity=0.02,
            tp_pct=20.0,
            sl_pct=-10.0,
            tp_price=60000.0,
            sl_price=45000.0,
            opened_at=now,
        )


def test_trade_summary_required_fields():
    summary = TradeSummary(
        date="2026-04-19",
        trades_opened=12,
        trades_closed=8,
        wins=5,
        losses=3,
        total_pnl_usd=340.0,
        best_trade_pnl=450.0,
        worst_trade_pnl=-120.0,
        avg_pnl_pct=4.25,
        win_rate_pct=62.5,
        by_signal_type={
            "volume_spike": {"trades": 5, "pnl": 230, "win_rate": 65},
        },
    )
    assert summary.trades_opened == 12
    assert summary.by_signal_type["volume_spike"]["pnl"] == 230
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_trading_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scout.trading'`

- [ ] **Step 3: Create package and models**

```python
# scout/trading/__init__.py
"""Paper Trading Engine -- simulated trade execution and PnL tracking."""
```

```python
# scout/trading/models.py
"""Pydantic models for the paper trading engine."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, field_validator


class PaperTrade(BaseModel):
    """A single paper trade with checkpoint tracking."""

    id: int | None = None
    token_id: str
    symbol: str
    name: str
    chain: str
    signal_type: str
    signal_data: dict

    entry_price: float
    amount_usd: float
    quantity: float

    tp_pct: float = 20.0
    sl_pct: float = 10.0  # positive: 10.0 means 10% stop loss
    tp_price: float
    sl_price: float

    status: str = "open"  # open, closed_tp, closed_sl, closed_expired, closed_manual

    exit_price: float | None = None
    exit_reason: str | None = None
    pnl_usd: float | None = None
    pnl_pct: float | None = None

    checkpoint_1h_price: float | None = None
    checkpoint_1h_pct: float | None = None
    checkpoint_6h_price: float | None = None
    checkpoint_6h_pct: float | None = None
    checkpoint_24h_price: float | None = None
    checkpoint_24h_pct: float | None = None
    checkpoint_48h_price: float | None = None
    checkpoint_48h_pct: float | None = None

    peak_price: float | None = None
    peak_pct: float | None = None

    opened_at: datetime
    closed_at: datetime | None = None

    @field_validator("sl_pct")
    @classmethod
    def _validate_sl_pct_positive(cls, v: float) -> float:
        if v < 0:
            raise ValueError(
                "sl_pct must be positive, e.g. 10.0 for 10% stop loss"
            )
        return v


class TradeSummary(BaseModel):
    """Daily paper trading summary."""

    date: str
    trades_opened: int
    trades_closed: int
    wins: int
    losses: int
    total_pnl_usd: float
    best_trade_pnl: float
    worst_trade_pnl: float
    avg_pnl_pct: float
    win_rate_pct: float
    by_signal_type: dict  # {"volume_spike": {"trades": 5, "pnl": 230, "win_rate": 65}}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_trading_models.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Add config fields to Settings**

Add to `scout/config.py` inside the `Settings` class, after the Second-Wave Detection section:

```python
    # -------- Paper Trading Engine --------
    TRADING_ENABLED: bool = False                  # master switch
    TRADING_MODE: str = "paper"                    # "paper" or "live"
    PAPER_TRADE_AMOUNT_USD: float = 1000.0         # per trade (paper)
    PAPER_MAX_EXPOSURE_USD: float = 10000.0        # max total open (paper)
    PAPER_TP_PCT: float = 20.0                     # take profit %
    PAPER_SL_PCT: float = 10.0                     # stop loss % (positive: 10.0 = 10%)
    PAPER_MAX_DURATION_HOURS: int = 48             # auto-expire
    PAPER_SLIPPAGE_BPS: int = 50                   # 0.5% slippage simulation
    TRADING_DIGEST_HOUR_UTC: int = 0               # midnight digest
    TRADING_EVAL_INTERVAL: int = 1800              # 30 min eval cycle
```

Add validator inside the `Settings` class:

```python
    @field_validator("PAPER_SL_PCT")
    @classmethod
    def _validate_paper_sl_pct(cls, v: float) -> float:
        if v < 0:
            raise ValueError(
                "sl_pct must be positive, e.g. 10.0 for 10% stop loss"
            )
        return v
```

Add to `.env.example` at the bottom:

```
# === Paper Trading Engine ===
TRADING_ENABLED=false
TRADING_MODE=paper
PAPER_TRADE_AMOUNT_USD=1000.0
PAPER_MAX_EXPOSURE_USD=10000.0
PAPER_TP_PCT=20.0
PAPER_SL_PCT=10.0
PAPER_MAX_DURATION_HOURS=48
PAPER_SLIPPAGE_BPS=50
TRADING_DIGEST_HOUR_UTC=0
TRADING_EVAL_INTERVAL=1800
```

- [ ] **Step 6: Run full suite**

Run: `uv run pytest --tb=short -q`
Expected: All existing tests + new model tests PASS

- [ ] **Step 7: Commit**

```bash
git add scout/trading/__init__.py scout/trading/models.py scout/config.py .env.example tests/test_trading_models.py
git commit -m "feat(trading): add Pydantic models and config for paper trading engine"
```

---

## Task 2: DB Schema

**Files:**
- Modify: `scout/db.py`
- Test: `tests/test_trading_db.py`

- [ ] **Step 1: Write failing tests for tables + constraints**

```python
# tests/test_trading_db.py
"""Tests for paper trading database tables."""
import json
from datetime import datetime, timezone

import pytest

from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


async def test_paper_trades_table_exists(db):
    """paper_trades table is created on initialize."""
    cursor = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='paper_trades'"
    )
    row = await cursor.fetchone()
    assert row is not None


async def test_paper_daily_summary_table_exists(db):
    """paper_daily_summary table is created on initialize."""
    cursor = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='paper_daily_summary'"
    )
    row = await cursor.fetchone()
    assert row is not None


async def test_paper_trades_insert_and_read(db):
    """Can insert and read a paper trade."""
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
            status, opened_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("bitcoin", "BTC", "Bitcoin", "coingecko", "volume_spike",
         json.dumps({"spike_ratio": 12.3}),
         50000.0, 1000.0, 0.02, 20.0, 10.0, 60000.0, 45000.0, "open", now),
    )
    await db._conn.commit()
    cursor = await db._conn.execute("SELECT * FROM paper_trades WHERE token_id='bitcoin'")
    row = await cursor.fetchone()
    assert row is not None
    assert dict(row)["entry_price"] == 50000.0


async def test_paper_trades_unique_constraint(db):
    """Duplicate (token_id, signal_type, opened_at) is rejected."""
    now = datetime.now(timezone.utc).isoformat()
    args = ("bitcoin", "BTC", "Bitcoin", "coingecko", "volume_spike",
            json.dumps({}), 50000.0, 1000.0, 0.02, 20.0, 10.0,
            60000.0, 45000.0, "open", now)
    sql = """INSERT INTO paper_trades
             (token_id, symbol, name, chain, signal_type, signal_data,
              entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
              status, opened_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    await db._conn.execute(sql, args)
    await db._conn.commit()
    with pytest.raises(Exception):  # IntegrityError
        await db._conn.execute(sql, args)
        await db._conn.commit()


async def test_paper_trades_status_index(db):
    """Status index exists for efficient open-trade queries."""
    cursor = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_paper_trades_status'"
    )
    row = await cursor.fetchone()
    assert row is not None


async def test_paper_daily_summary_unique_date(db):
    """paper_daily_summary enforces unique date."""
    await db._conn.execute(
        """INSERT INTO paper_daily_summary (date, trades_opened, trades_closed,
           wins, losses, total_pnl_usd) VALUES (?, ?, ?, ?, ?, ?)""",
        ("2026-04-19", 10, 8, 5, 3, 340.0),
    )
    await db._conn.commit()
    with pytest.raises(Exception):
        await db._conn.execute(
            """INSERT INTO paper_daily_summary (date, trades_opened, trades_closed,
               wins, losses, total_pnl_usd) VALUES (?, ?, ?, ?, ?, ?)""",
            ("2026-04-19", 5, 3, 2, 1, 100.0),
        )
        await db._conn.commit()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_trading_db.py -v`
Expected: FAIL -- tables do not exist yet

- [ ] **Step 3: Add tables to `_create_tables()` in `scout/db.py`**

Add the following SQL at the end of the `_create_tables()` executescript, before the closing `"""`:

```sql
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                chain TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                signal_data TEXT NOT NULL,

                entry_price REAL NOT NULL,
                amount_usd REAL NOT NULL,
                quantity REAL NOT NULL,

                tp_pct REAL NOT NULL DEFAULT 20.0,
                sl_pct REAL NOT NULL DEFAULT 10.0,
                tp_price REAL NOT NULL,
                sl_price REAL NOT NULL,

                status TEXT NOT NULL DEFAULT 'open',

                exit_price REAL,
                exit_reason TEXT,
                pnl_usd REAL,
                pnl_pct REAL,

                checkpoint_1h_price REAL,
                checkpoint_1h_pct REAL,
                checkpoint_6h_price REAL,
                checkpoint_6h_pct REAL,
                checkpoint_24h_price REAL,
                checkpoint_24h_pct REAL,
                checkpoint_48h_price REAL,
                checkpoint_48h_pct REAL,

                peak_price REAL,
                peak_pct REAL,

                opened_at TEXT NOT NULL,
                closed_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),

                UNIQUE(token_id, signal_type, opened_at)
            );
            CREATE INDEX IF NOT EXISTS idx_paper_trades_status ON paper_trades(status);
            CREATE INDEX IF NOT EXISTS idx_paper_trades_opened ON paper_trades(opened_at);
            CREATE INDEX IF NOT EXISTS idx_paper_trades_signal ON paper_trades(signal_type);

            CREATE TABLE IF NOT EXISTS paper_daily_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                trades_opened INTEGER NOT NULL DEFAULT 0,
                trades_closed INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                total_pnl_usd REAL NOT NULL DEFAULT 0,
                best_trade_pnl REAL,
                worst_trade_pnl REAL,
                avg_pnl_pct REAL,
                win_rate_pct REAL,
                by_signal_type TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_trading_db.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Run full suite**

Run: `uv run pytest --tb=short -q`
Expected: All existing tests still PASS

- [ ] **Step 6: Commit**

```bash
git add scout/db.py tests/test_trading_db.py
git commit -m "feat(trading): add paper_trades and paper_daily_summary DB tables"
```

---

## Task 3: Paper Trader

**Files:**
- Create: `scout/trading/paper.py`
- Test: `tests/test_paper_trader.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_paper_trader.py
"""Tests for PaperTrader -- simulated trade execution with slippage."""
import json
from datetime import datetime, timezone

import pytest

from scout.db import Database
from scout.trading.paper import PaperTrader


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
def trader():
    return PaperTrader()


async def test_execute_buy_inserts_trade(db, trader):
    """execute_buy creates a paper trade row in the DB."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={"spike_ratio": 12.3},
        current_price=50000.0,
        amount_usd=1000.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=50,
    )
    assert trade_id is not None
    cursor = await db._conn.execute(
        "SELECT * FROM paper_trades WHERE id = ?", (trade_id,)
    )
    row = dict(await cursor.fetchone())
    assert row["token_id"] == "bitcoin"
    assert row["status"] == "open"


async def test_execute_buy_applies_slippage(db, trader):
    """Entry price includes slippage: effective_entry = price * (1 + bps/10000)."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        current_price=10000.0,
        amount_usd=1000.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=100,  # 1%
    )
    cursor = await db._conn.execute(
        "SELECT entry_price, quantity FROM paper_trades WHERE id = ?", (trade_id,)
    )
    row = dict(await cursor.fetchone())
    # effective_entry = 10000 * (1 + 100/10000) = 10100
    assert row["entry_price"] == pytest.approx(10100.0)
    # quantity = 1000 / 10100
    assert row["quantity"] == pytest.approx(1000.0 / 10100.0)


async def test_execute_buy_computes_tp_sl_prices(db, trader):
    """TP and SL prices are computed from effective entry price."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id="ethereum",
        symbol="ETH",
        name="Ethereum",
        chain="coingecko",
        signal_type="narrative_prediction",
        signal_data={"fit": 85},
        current_price=3000.0,
        amount_usd=1000.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=0,  # no slippage
    )
    cursor = await db._conn.execute(
        "SELECT tp_price, sl_price FROM paper_trades WHERE id = ?", (trade_id,)
    )
    row = dict(await cursor.fetchone())
    # tp_price = 3000 * (1 + 20/100) = 3600
    assert row["tp_price"] == pytest.approx(3600.0)
    # sl_price = 3000 * (1 - 10/100) = 2700
    assert row["sl_price"] == pytest.approx(2700.0)


async def test_execute_sell_closes_trade(db, trader):
    """execute_sell closes a trade and computes PnL."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        current_price=50000.0,
        amount_usd=1000.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=0,
    )
    await trader.execute_sell(
        db=db,
        trade_id=trade_id,
        current_price=60000.0,
        reason="take_profit",
        slippage_bps=0,
    )
    cursor = await db._conn.execute(
        "SELECT status, exit_price, pnl_usd, pnl_pct FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = dict(await cursor.fetchone())
    assert row["status"] == "closed_tp"
    assert row["exit_price"] == pytest.approx(60000.0)
    assert row["pnl_pct"] == pytest.approx(20.0)
    assert row["pnl_usd"] == pytest.approx(200.0)


async def test_execute_sell_applies_exit_slippage(db, trader):
    """Exit price includes slippage: effective_exit = price * (1 - bps/10000)."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        current_price=10000.0,
        amount_usd=1000.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=0,
    )
    await trader.execute_sell(
        db=db,
        trade_id=trade_id,
        current_price=12000.0,
        reason="take_profit",
        slippage_bps=100,  # 1% exit slippage
    )
    cursor = await db._conn.execute(
        "SELECT exit_price FROM paper_trades WHERE id = ?", (trade_id,),
    )
    row = dict(await cursor.fetchone())
    # effective_exit = 12000 * (1 - 100/10000) = 11880
    assert row["exit_price"] == pytest.approx(11880.0)


async def test_execute_sell_stop_loss_pnl(db, trader):
    """PnL is negative on a stop loss."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        current_price=50000.0,
        amount_usd=1000.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=0,
    )
    await trader.execute_sell(
        db=db,
        trade_id=trade_id,
        current_price=45000.0,
        reason="stop_loss",
        slippage_bps=0,
    )
    cursor = await db._conn.execute(
        "SELECT pnl_usd, pnl_pct FROM paper_trades WHERE id = ?", (trade_id,),
    )
    row = dict(await cursor.fetchone())
    assert row["pnl_pct"] == pytest.approx(-10.0)
    assert row["pnl_usd"] == pytest.approx(-100.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_paper_trader.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scout.trading.paper'`

- [ ] **Step 3: Implement PaperTrader**

```python
# scout/trading/paper.py
"""PaperTrader -- simulates trade execution by logging to DB at current price."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import structlog

from scout.db import Database

log = structlog.get_logger()


class PaperTrader:
    """Simulates trade execution with slippage simulation."""

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
    ) -> int:
        """Record a paper buy. Returns trade ID.

        Applies slippage to entry price: effective_entry = price * (1 + bps/10000).
        sl_pct is positive: sl_price = entry * (1 - sl_pct/100).
        """
        conn = db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")

        effective_entry = current_price * (1 + slippage_bps / 10000)
        quantity = amount_usd / effective_entry
        tp_price = effective_entry * (1 + tp_pct / 100)
        sl_price = effective_entry * (1 - sl_pct / 100)
        now = datetime.now(timezone.utc).isoformat()

        cursor = await conn.execute(
            """INSERT INTO paper_trades
               (token_id, symbol, name, chain, signal_type, signal_data,
                entry_price, amount_usd, quantity,
                tp_pct, sl_pct, tp_price, sl_price,
                status, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
            (
                token_id, symbol, name, chain, signal_type,
                json.dumps(signal_data),
                effective_entry, amount_usd, quantity,
                tp_pct, sl_pct, tp_price, sl_price,
                now,
            ),
        )
        await conn.commit()
        trade_id = cursor.lastrowid

        log.info(
            "paper_trade_opened",
            trade_id=trade_id,
            token_id=token_id,
            symbol=symbol,
            signal_type=signal_type,
            entry_price=effective_entry,
            amount_usd=amount_usd,
            tp_price=tp_price,
            sl_price=sl_price,
        )
        return trade_id

    async def execute_sell(
        self,
        db: Database,
        trade_id: int,
        current_price: float,
        reason: str,
        slippage_bps: int = 0,
    ) -> None:
        """Close a paper trade. Applies exit slippage.

        effective_exit = price * (1 - bps/10000).
        """
        conn = db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")

        cursor = await conn.execute(
            "SELECT entry_price, amount_usd, quantity FROM paper_trades WHERE id = ?",
            (trade_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            log.warning("paper_trade_not_found", trade_id=trade_id)
            return

        entry_price = float(row[0])
        amount_usd = float(row[1])
        quantity = float(row[2])

        effective_exit = current_price * (1 - slippage_bps / 10000)
        pnl_pct = ((effective_exit - entry_price) / entry_price) * 100
        pnl_usd = quantity * (effective_exit - entry_price)
        now = datetime.now(timezone.utc).isoformat()

        # Map reason to status
        status_map = {
            "take_profit": "closed_tp",
            "stop_loss": "closed_sl",
            "expired": "closed_expired",
            "manual": "closed_manual",
        }
        status = status_map.get(reason, "closed_manual")

        await conn.execute(
            """UPDATE paper_trades
               SET status = ?, exit_price = ?, exit_reason = ?,
                   pnl_usd = ?, pnl_pct = ?, closed_at = ?
               WHERE id = ?""",
            (status, effective_exit, reason, pnl_usd, round(pnl_pct, 4), now, trade_id),
        )
        await conn.commit()

        log.info(
            "paper_trade_closed",
            trade_id=trade_id,
            reason=reason,
            exit_price=effective_exit,
            pnl_usd=round(pnl_usd, 2),
            pnl_pct=round(pnl_pct, 2),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_paper_trader.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Run full suite**

Run: `uv run pytest --tb=short -q`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add scout/trading/paper.py tests/test_paper_trader.py
git commit -m "feat(trading): implement PaperTrader with slippage simulation"
```

---

## Task 4: Trading Engine

**Files:**
- Create: `scout/trading/engine.py`
- Test: `tests/test_trading_engine.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_trading_engine.py
"""Tests for TradingEngine -- pluggable interface with exposure and staleness checks."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from scout.config import Settings
from scout.db import Database
from scout.trading.engine import TradingEngine


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_CHAT_ID="test",
        ANTHROPIC_API_KEY="test",
        DB_PATH=tmp_path / "test.db",
        TRADING_ENABLED=True,
        TRADING_MODE="paper",
        PAPER_TRADE_AMOUNT_USD=1000.0,
        PAPER_MAX_EXPOSURE_USD=5000.0,
        PAPER_TP_PCT=20.0,
        PAPER_SL_PCT=10.0,
        PAPER_SLIPPAGE_BPS=50,
        PAPER_MAX_DURATION_HOURS=48,
    )


@pytest.fixture
def engine(db, settings):
    return TradingEngine(mode="paper", db=db, settings=settings)


async def _seed_price_cache(db, coin_id, price, age_seconds=0):
    """Helper: insert a price_cache row with a given age."""
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    await db._conn.execute(
        """INSERT OR REPLACE INTO price_cache
           (coin_id, current_price, price_change_24h, price_change_7d, market_cap, updated_at)
           VALUES (?, ?, 0, 0, 0, ?)""",
        (coin_id, price, ts.isoformat()),
    )
    await db._conn.commit()


async def test_open_trade_success(engine, db):
    """Engine opens a paper trade when price is available and fresh."""
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=60)
    trade_id = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={"spike_ratio": 12.3},
    )
    assert trade_id is not None


async def test_open_trade_skips_no_price(engine, db):
    """Engine skips trade when price is not in cache."""
    trade_id = await engine.open_trade(
        token_id="unknown-coin",
        symbol="UNK",
        name="Unknown",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
    )
    assert trade_id is None


async def test_open_trade_skips_stale_price(engine, db):
    """Engine skips trade when price_cache.updated_at is older than 300 seconds."""
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=400)
    trade_id = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
    )
    assert trade_id is None


async def test_open_trade_rejects_max_exposure(engine, db, settings):
    """Engine rejects trade when total exposure would exceed max."""
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=0)
    # Open 5 trades at $1000 each = $5000 (max)
    for i in range(5):
        ts = (datetime.now(timezone.utc) + timedelta(seconds=i)).isoformat()
        await db._conn.execute(
            """INSERT INTO paper_trades
               (token_id, symbol, name, chain, signal_type, signal_data,
                entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
                status, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
            (f"coin-{i}", "X", "X", "coingecko", "test", "{}",
             100.0, 1000.0, 10.0, 20.0, 10.0, 120.0, 90.0, ts),
        )
    await db._conn.commit()

    trade_id = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
    )
    assert trade_id is None


async def test_open_trade_rejects_duplicate(engine, db):
    """Engine skips if same token already has an open trade."""
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=0)
    trade_id_1 = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
    )
    assert trade_id_1 is not None

    trade_id_2 = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
    )
    assert trade_id_2 is None


async def test_close_trade(engine, db):
    """Engine can force-close a trade."""
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=0)
    trade_id = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
    )
    await engine.close_trade(trade_id, reason="manual")
    cursor = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == "closed_manual"


async def test_get_open_positions(engine, db):
    """get_open_positions returns all open trades."""
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=0)
    await _seed_price_cache(db, "ethereum", 3000.0, age_seconds=0)
    await engine.open_trade(
        token_id="bitcoin", symbol="BTC", name="Bitcoin",
        chain="coingecko", signal_type="volume_spike", signal_data={},
    )
    await engine.open_trade(
        token_id="ethereum", symbol="ETH", name="Ethereum",
        chain="coingecko", signal_type="narrative_prediction", signal_data={},
    )
    positions = await engine.get_open_positions()
    assert len(positions) == 2


async def test_uses_custom_amount(engine, db):
    """Engine uses custom amount_usd if provided."""
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=0)
    trade_id = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        amount_usd=2000.0,
    )
    cursor = await db._conn.execute(
        "SELECT amount_usd FROM paper_trades WHERE id = ?", (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == pytest.approx(2000.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_trading_engine.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scout.trading.engine'`

- [ ] **Step 3: Implement TradingEngine**

```python
# scout/trading/engine.py
"""TradingEngine -- pluggable interface for paper and live trading."""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from scout.db import Database
from scout.trading.paper import PaperTrader

log = structlog.get_logger()

# Maximum age (seconds) for a price_cache entry to be considered fresh.
_MAX_PRICE_AGE_SECONDS = 300


class TradingEngine:
    """Pluggable trading engine. Call from any signal source.

    Usage:
        engine = TradingEngine(mode="paper", db=db, settings=settings)
        trade_id = await engine.open_trade(
            token_id="bitcoin", chain="coingecko",
            signal_type="volume_spike",
            signal_data={"spike_ratio": 12.3},
        )
    """

    def __init__(self, mode: str, db: Database, settings) -> None:
        self.mode = mode
        self.db = db
        self.settings = settings
        self._paper_trader = PaperTrader()

    async def open_trade(
        self,
        token_id: str,
        symbol: str = "",
        name: str = "",
        chain: str = "coingecko",
        signal_type: str = "",
        signal_data: dict | None = None,
        amount_usd: float | None = None,
    ) -> int | None:
        """Open a new trade. Returns trade_id or None if rejected."""
        if signal_data is None:
            signal_data = {}

        conn = self.db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")

        # 1. Get current price from price_cache with staleness check
        price_row = await self._get_current_price_with_age(token_id)
        if price_row is None:
            log.info("trade_skipped_no_price", token_id=token_id)
            return None

        current_price, price_age_seconds = price_row
        if price_age_seconds > _MAX_PRICE_AGE_SECONDS:
            log.info(
                "trade_skipped_stale_price",
                token_id=token_id,
                price_age_seconds=round(price_age_seconds, 1),
            )
            return None

        # 2. Check duplicate open position
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE token_id = ? AND status = 'open'",
            (token_id,),
        )
        row = await cursor.fetchone()
        if row[0] > 0:
            log.info("trade_skipped_duplicate", token_id=token_id)
            return None

        # 3. Check max exposure
        trade_amount = amount_usd or self.settings.PAPER_TRADE_AMOUNT_USD
        cursor = await conn.execute(
            "SELECT COALESCE(SUM(amount_usd), 0) FROM paper_trades WHERE status = 'open'"
        )
        row = await cursor.fetchone()
        current_exposure = float(row[0])
        if current_exposure + trade_amount > self.settings.PAPER_MAX_EXPOSURE_USD:
            log.warning(
                "trade_rejected_max_exposure",
                token_id=token_id,
                current_exposure=current_exposure,
                new_amount=trade_amount,
                max_exposure=self.settings.PAPER_MAX_EXPOSURE_USD,
            )
            return None

        # 4. Execute via paper trader
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
            )
            return trade_id

        log.warning("trade_mode_not_supported", mode=self.mode)
        return None

    async def close_trade(self, trade_id: int, reason: str = "manual") -> None:
        """Force-close a trade."""
        conn = self.db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")

        # Get current price for PnL calculation
        cursor = await conn.execute(
            "SELECT token_id FROM paper_trades WHERE id = ?", (trade_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return

        token_id = row[0]
        price_row = await self._get_current_price_with_age(token_id)
        current_price = price_row[0] if price_row else 0.0

        await self._paper_trader.execute_sell(
            db=self.db,
            trade_id=trade_id,
            current_price=current_price,
            reason=reason,
            slippage_bps=self.settings.PAPER_SLIPPAGE_BPS,
        )

    async def get_open_positions(self) -> list[dict]:
        """All open paper trades."""
        conn = self.db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")
        cursor = await conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'open' ORDER BY opened_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_pnl_summary(self, days: int = 7) -> dict:
        """Aggregate PnL statistics over the last N days."""
        conn = self.db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")
        cursor = await conn.execute(
            """SELECT
                 COUNT(*) as total_trades,
                 SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                 SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) as losses,
                 COALESCE(SUM(pnl_usd), 0) as total_pnl_usd,
                 COALESCE(AVG(pnl_pct), 0) as avg_pnl_pct,
                 MAX(pnl_usd) as best_trade,
                 MIN(pnl_usd) as worst_trade
               FROM paper_trades
               WHERE status != 'open'
                 AND closed_at >= datetime('now', ?)""",
            (f"-{days} days",),
        )
        row = await cursor.fetchone()
        total = row[0] or 0
        wins = row[1] or 0
        return {
            "total_trades": total,
            "wins": wins,
            "losses": row[2] or 0,
            "total_pnl_usd": row[3] or 0,
            "avg_pnl_pct": round(row[4] or 0, 2),
            "best_trade": row[5],
            "worst_trade": row[6],
            "win_rate_pct": round((wins / total) * 100, 1) if total > 0 else 0,
        }

    async def get_pnl_by_signal_type(self, days: int = 7) -> dict:
        """PnL breakdown by signal type."""
        conn = self.db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")
        cursor = await conn.execute(
            """SELECT signal_type,
                 COUNT(*) as trades,
                 COALESCE(SUM(pnl_usd), 0) as pnl,
                 SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins
               FROM paper_trades
               WHERE status != 'open'
                 AND closed_at >= datetime('now', ?)
               GROUP BY signal_type""",
            (f"-{days} days",),
        )
        rows = await cursor.fetchall()
        result = {}
        for row in rows:
            total = row[1]
            wins = row[3] or 0
            result[row[0]] = {
                "trades": total,
                "pnl": round(row[2], 2),
                "win_rate": round((wins / total) * 100, 1) if total > 0 else 0,
            }
        return result

    async def _get_current_price_with_age(
        self, token_id: str
    ) -> tuple[float, float] | None:
        """Look up price from price_cache table. Returns (price, age_seconds) or None."""
        conn = self.db._conn
        if conn is None:
            return None
        cursor = await conn.execute(
            "SELECT current_price, updated_at FROM price_cache WHERE coin_id = ?",
            (token_id,),
        )
        row = await cursor.fetchone()
        if row is None or row[0] is None:
            return None

        price = float(row[0])
        updated_at = datetime.fromisoformat(str(row[1])).replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - updated_at).total_seconds()
        return (price, age_seconds)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_trading_engine.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Run full suite**

Run: `uv run pytest --tb=short -q`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add scout/trading/engine.py tests/test_trading_engine.py
git commit -m "feat(trading): implement TradingEngine with exposure, staleness, and dedup checks"
```

---

## Task 5: Evaluator

**Files:**
- Create: `scout/trading/evaluator.py`
- Test: `tests/test_trading_evaluator.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_trading_evaluator.py
"""Tests for paper trade evaluator -- checkpoints, TP/SL, expiry, batch lookup."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading.evaluator import evaluate_paper_trades


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


def _settings_factory(tmp_path, **overrides):
    from scout.config import Settings
    defaults = dict(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_CHAT_ID="test",
        ANTHROPIC_API_KEY="test",
        DB_PATH=tmp_path / "test.db",
        PAPER_TP_PCT=20.0,
        PAPER_SL_PCT=10.0,
        PAPER_SLIPPAGE_BPS=0,
        PAPER_MAX_DURATION_HOURS=48,
    )
    defaults.update(overrides)
    return Settings(**defaults)


async def _insert_trade(db, token_id, entry_price, opened_at, **kwargs):
    """Helper: insert a paper trade for testing."""
    defaults = {
        "symbol": token_id.upper()[:3],
        "name": token_id.title(),
        "chain": "coingecko",
        "signal_type": "volume_spike",
        "signal_data": json.dumps({}),
        "amount_usd": 1000.0,
        "quantity": 1000.0 / entry_price,
        "tp_pct": 20.0,
        "sl_pct": 10.0,
        "tp_price": entry_price * 1.2,
        "sl_price": entry_price * 0.9,
        "status": "open",
    }
    defaults.update(kwargs)
    cursor = await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
            status, opened_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            token_id, defaults["symbol"], defaults["name"], defaults["chain"],
            defaults["signal_type"], defaults["signal_data"],
            entry_price, defaults["amount_usd"], defaults["quantity"],
            defaults["tp_pct"], defaults["sl_pct"],
            defaults["tp_price"], defaults["sl_price"],
            defaults["status"], opened_at.isoformat(),
        ),
    )
    await db._conn.commit()
    return cursor.lastrowid


async def _seed_price(db, coin_id, price):
    """Helper: insert fresh price into cache."""
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT OR REPLACE INTO price_cache
           (coin_id, current_price, price_change_24h, price_change_7d, market_cap, updated_at)
           VALUES (?, ?, 0, 0, 0, ?)""",
        (coin_id, price, now),
    )
    await db._conn.commit()


async def test_checkpoint_1h_update(db, tmp_path):
    """Evaluator updates 1h checkpoint when 1h has elapsed."""
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(hours=1, minutes=5)
    trade_id = await _insert_trade(db, "bitcoin", 50000.0, opened)
    await _seed_price(db, "bitcoin", 55000.0)

    await evaluate_paper_trades(db, settings)

    cursor = await db._conn.execute(
        "SELECT checkpoint_1h_price, checkpoint_1h_pct FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == pytest.approx(55000.0)
    assert row[1] == pytest.approx(10.0)


async def test_tp_closure(db, tmp_path):
    """Evaluator closes trade when price >= tp_price."""
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    trade_id = await _insert_trade(db, "bitcoin", 50000.0, opened)
    # TP at 60000, current at 61000
    await _seed_price(db, "bitcoin", 61000.0)

    await evaluate_paper_trades(db, settings)

    cursor = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?", (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == "closed_tp"
    assert row[1] == "take_profit"


async def test_sl_closure(db, tmp_path):
    """Evaluator closes trade when price <= sl_price."""
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    trade_id = await _insert_trade(db, "bitcoin", 50000.0, opened)
    # SL at 45000, current at 44000
    await _seed_price(db, "bitcoin", 44000.0)

    await evaluate_paper_trades(db, settings)

    cursor = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?", (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == "closed_sl"
    assert row[1] == "stop_loss"


async def test_expiry_closure(db, tmp_path):
    """Evaluator closes trade after PAPER_MAX_DURATION_HOURS."""
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(hours=49)
    trade_id = await _insert_trade(db, "bitcoin", 50000.0, opened)
    await _seed_price(db, "bitcoin", 51000.0)

    await evaluate_paper_trades(db, settings)

    cursor = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?", (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == "closed_expired"
    assert row[1] == "expired"


async def test_peak_tracking(db, tmp_path):
    """Evaluator updates peak_price when current > previous peak."""
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    trade_id = await _insert_trade(db, "bitcoin", 50000.0, opened)
    # Price is up but not at TP
    await _seed_price(db, "bitcoin", 55000.0)

    await evaluate_paper_trades(db, settings)

    cursor = await db._conn.execute(
        "SELECT peak_price, peak_pct FROM paper_trades WHERE id = ?", (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == pytest.approx(55000.0)
    assert row[1] == pytest.approx(10.0)


async def test_batch_price_lookup(db, tmp_path):
    """Evaluator uses a single batch query for all open trades."""
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    await _insert_trade(db, "bitcoin", 50000.0, opened)
    await _insert_trade(db, "ethereum", 3000.0, opened)
    await _seed_price(db, "bitcoin", 52000.0)
    await _seed_price(db, "ethereum", 3100.0)

    # This should not raise -- batch query handles multiple coins
    await evaluate_paper_trades(db, settings)

    # Both trades should have peak tracking updated
    cursor = await db._conn.execute(
        "SELECT token_id, peak_price FROM paper_trades WHERE peak_price IS NOT NULL"
    )
    rows = await cursor.fetchall()
    assert len(rows) == 2


async def test_tp_with_checkpoint(db, tmp_path):
    """TP/SL takes priority but checkpoint is also recorded."""
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(hours=1, minutes=5)
    trade_id = await _insert_trade(db, "bitcoin", 50000.0, opened)
    # Price at TP level -- should close AND record 1h checkpoint
    await _seed_price(db, "bitcoin", 62000.0)

    await evaluate_paper_trades(db, settings)

    cursor = await db._conn.execute(
        "SELECT status, checkpoint_1h_price FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == "closed_tp"
    assert row[1] is not None  # 1h checkpoint was recorded


async def test_skips_trade_with_no_price(db, tmp_path):
    """Evaluator skips trades where price is not available in cache."""
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    trade_id = await _insert_trade(db, "unknown-coin", 100.0, opened)
    # No price in cache for unknown-coin

    await evaluate_paper_trades(db, settings)

    # Trade should remain open, unchanged
    cursor = await db._conn.execute(
        "SELECT status, peak_price FROM paper_trades WHERE id = ?", (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == "open"
    assert row[1] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_trading_evaluator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scout.trading.evaluator'`

- [ ] **Step 3: Implement evaluator**

```python
# scout/trading/evaluator.py
"""EVALUATE phase -- paper trade checkpoint tracking with TP/SL/expiry.

Runs every 30 minutes. Uses batch price lookup from price_cache
(single SELECT ... WHERE coin_id IN (...) query).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

from scout.db import Database
from scout.trading.paper import PaperTrader

log = structlog.get_logger()

_trader = PaperTrader()


async def evaluate_paper_trades(db: Database, settings) -> None:
    """Check all open paper trades: update checkpoints, check TP/SL, expire old.

    Uses a single batch query to fetch prices for all open trades.
    Logs price_age_seconds alongside the price for each trade.
    """
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized.")

    # 1. Get all open trades
    cursor = await conn.execute(
        """SELECT id, token_id, entry_price, opened_at,
                  tp_price, sl_price, tp_pct, sl_pct,
                  checkpoint_1h_price, checkpoint_6h_price,
                  checkpoint_24h_price, checkpoint_48h_price,
                  peak_price, peak_pct, amount_usd, quantity
           FROM paper_trades
           WHERE status = 'open'"""
    )
    rows = await cursor.fetchall()
    if not rows:
        return

    # 2. Batch-fetch current prices from price_cache (single IN query)
    unique_ids = list({row[1] for row in rows})
    placeholders = ",".join("?" * len(unique_ids))
    price_cursor = await conn.execute(
        f"""SELECT coin_id, current_price, updated_at
            FROM price_cache
            WHERE coin_id IN ({placeholders})""",
        unique_ids,
    )
    price_rows = await price_cursor.fetchall()
    price_map: dict[str, tuple[float, str]] = {}
    for pr in price_rows:
        if pr[1] is not None:
            price_map[pr[0]] = (float(pr[1]), str(pr[2]))

    now = datetime.now(timezone.utc)
    max_duration = timedelta(hours=settings.PAPER_MAX_DURATION_HOURS)
    slippage_bps = settings.PAPER_SLIPPAGE_BPS

    for row in rows:
        trade_id = row[0]
        token_id = row[1]
        entry_price = float(row[2])
        opened_at = datetime.fromisoformat(str(row[3])).replace(tzinfo=timezone.utc)
        tp_price = float(row[4])
        sl_price = float(row[5])
        cp_1h = row[8]
        cp_6h = row[9]
        cp_24h = row[10]
        cp_48h = row[11]
        peak_price = float(row[12]) if row[12] is not None else None
        peak_pct = float(row[13]) if row[13] is not None else None

        # Price lookup
        price_data = price_map.get(token_id)
        if price_data is None:
            log.debug("trade_eval_no_price", trade_id=trade_id, token_id=token_id)
            continue

        current_price, updated_at_str = price_data
        updated_at = datetime.fromisoformat(updated_at_str).replace(tzinfo=timezone.utc)
        price_age_seconds = (now - updated_at).total_seconds()

        if entry_price <= 0:
            continue

        elapsed = now - opened_at
        change_pct = ((current_price - entry_price) / entry_price) * 100

        # --- Peak tracking ---
        reference = peak_price if peak_price is not None else entry_price
        if current_price > reference:
            peak_price = current_price
            peak_pct = ((current_price - entry_price) / entry_price) * 100
            await conn.execute(
                "UPDATE paper_trades SET peak_price = ?, peak_pct = ? WHERE id = ?",
                (peak_price, round(peak_pct, 4), trade_id),
            )

        # --- Checkpoint updates ---
        updates: dict[str, object] = {}

        if cp_1h is None and elapsed >= timedelta(hours=1):
            updates["checkpoint_1h_price"] = current_price
            updates["checkpoint_1h_pct"] = round(change_pct, 4)

        if cp_6h is None and elapsed >= timedelta(hours=6):
            updates["checkpoint_6h_price"] = current_price
            updates["checkpoint_6h_pct"] = round(change_pct, 4)

        if cp_24h is None and elapsed >= timedelta(hours=24):
            updates["checkpoint_24h_price"] = current_price
            updates["checkpoint_24h_pct"] = round(change_pct, 4)

        if cp_48h is None and elapsed >= timedelta(hours=48):
            updates["checkpoint_48h_price"] = current_price
            updates["checkpoint_48h_pct"] = round(change_pct, 4)

        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [trade_id]
            await conn.execute(
                f"UPDATE paper_trades SET {set_clause} WHERE id = ?",
                values,
            )

        # --- TP/SL/Expiry checks (takes priority, but checkpoints still recorded above) ---
        close_reason = None
        if current_price >= tp_price:
            close_reason = "take_profit"
        elif current_price <= sl_price:
            close_reason = "stop_loss"
        elif elapsed >= max_duration:
            close_reason = "expired"

        if close_reason is not None:
            await _trader.execute_sell(
                db=db,
                trade_id=trade_id,
                current_price=current_price,
                reason=close_reason,
                slippage_bps=slippage_bps,
            )
            log.info(
                "paper_trade_eval_closed",
                trade_id=trade_id,
                token_id=token_id,
                reason=close_reason,
                price_age_seconds=round(price_age_seconds, 1),
                current_price=current_price,
                change_pct=round(change_pct, 2),
            )
        else:
            log.debug(
                "paper_trade_eval_ok",
                trade_id=trade_id,
                token_id=token_id,
                price_age_seconds=round(price_age_seconds, 1),
                current_price=current_price,
                change_pct=round(change_pct, 2),
            )

    await conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_trading_evaluator.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Run full suite**

Run: `uv run pytest --tb=short -q`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add scout/trading/evaluator.py tests/test_trading_evaluator.py
git commit -m "feat(trading): implement paper trade evaluator with batch price lookup and checkpoints"
```

---

## Task 6: Daily Digest + Main Loop Integration

**Files:**
- Create: `scout/trading/digest.py`
- Modify: `scout/main.py`
- Test: `tests/test_trading_digest.py`

- [ ] **Step 1: Write failing tests for digest**

```python
# tests/test_trading_digest.py
"""Tests for paper trading daily digest builder."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading.digest import build_paper_digest


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


async def _insert_closed_trade(db, token_id, symbol, signal_type, pnl_usd, pnl_pct, closed_at):
    """Helper: insert a closed paper trade."""
    opened_at = (closed_at - timedelta(hours=2)).isoformat()
    await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
            status, exit_price, exit_reason, pnl_usd, pnl_pct, opened_at, closed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            token_id, symbol, token_id.title(), "coingecko", signal_type,
            json.dumps({}),
            100.0, 1000.0, 10.0, 20.0, 10.0, 120.0, 90.0,
            "closed_tp" if pnl_usd > 0 else "closed_sl",
            110.0 if pnl_usd > 0 else 90.0,
            "take_profit" if pnl_usd > 0 else "stop_loss",
            pnl_usd, pnl_pct,
            opened_at, closed_at.isoformat(),
        ),
    )
    await db._conn.commit()


async def test_digest_with_trades(db):
    """Digest includes trades, PnL, and signal breakdown."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc)
    await _insert_closed_trade(db, "bitcoin", "BTC", "volume_spike", 200.0, 20.0, now)
    await _insert_closed_trade(db, "ethereum", "ETH", "narrative_prediction", -50.0, -5.0, now)

    text = await build_paper_digest(db, today)
    assert "Paper Trading" in text
    assert "BTC" in text or "bitcoin" in text.lower()
    assert "volume_spike" in text


async def test_digest_empty_day(db):
    """Digest handles days with no trades gracefully."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    text = await build_paper_digest(db, today)
    assert "Paper Trading" in text
    assert "0" in text  # 0 trades


async def test_digest_signal_breakdown(db):
    """Digest includes per-signal-type stats."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    await _insert_closed_trade(db, "coin-a", "A", "volume_spike", 100.0, 10.0, now)
    await _insert_closed_trade(db, "coin-b", "B", "volume_spike", 50.0, 5.0, now)
    await _insert_closed_trade(db, "coin-c", "C", "trending_catch", -30.0, -3.0, now)

    text = await build_paper_digest(db, today)
    assert "volume_spike" in text
    assert "trending_catch" in text


async def test_digest_includes_open_positions(db):
    """Digest shows open position count and exposure."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    # Insert open trade
    await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
            status, opened_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
        ("bitcoin", "BTC", "Bitcoin", "coingecko", "volume_spike",
         json.dumps({}), 50000.0, 1000.0, 0.02, 20.0, 10.0, 60000.0, 45000.0,
         now.isoformat()),
    )
    await db._conn.commit()

    text = await build_paper_digest(db, today)
    assert "Open positions" in text or "open" in text.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_trading_digest.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scout.trading.digest'`

- [ ] **Step 3: Implement digest builder**

```python
# scout/trading/digest.py
"""Daily paper trading digest builder for Telegram."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import structlog

from scout.db import Database

log = structlog.get_logger()


async def build_paper_digest(db: Database, date: str) -> str:
    """Build daily paper trading summary text.

    Args:
        db: Initialised Database instance.
        date: Date string in YYYY-MM-DD format.

    Returns:
        Formatted digest text for Telegram.
    """
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized.")

    # Closed trades for the date
    cursor = await conn.execute(
        """SELECT token_id, symbol, signal_type, pnl_usd, pnl_pct
           FROM paper_trades
           WHERE status != 'open'
             AND DATE(closed_at) = ?
           ORDER BY pnl_usd DESC""",
        (date,),
    )
    closed_rows = await cursor.fetchall()

    # Opened trades for the date
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE DATE(opened_at) = ?",
        (date,),
    )
    opened_count = (await cursor.fetchone())[0] or 0

    # Open positions
    cursor = await conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(amount_usd), 0) FROM paper_trades WHERE status = 'open'"
    )
    open_row = await cursor.fetchone()
    open_count = open_row[0] or 0
    open_exposure = open_row[1] or 0

    # Compute stats
    closed_count = len(closed_rows)
    wins = sum(1 for r in closed_rows if r[3] and r[3] > 0)
    losses = closed_count - wins
    total_pnl = sum(r[3] or 0 for r in closed_rows)
    win_rate = round((wins / closed_count) * 100, 1) if closed_count > 0 else 0

    best_trade = max(closed_rows, key=lambda r: r[3] or 0) if closed_rows else None
    worst_trade = min(closed_rows, key=lambda r: r[3] or 0) if closed_rows else None

    # By signal type
    signal_stats: dict[str, dict] = {}
    for row in closed_rows:
        sig = row[2]
        if sig not in signal_stats:
            signal_stats[sig] = {"trades": 0, "pnl": 0.0, "wins": 0}
        signal_stats[sig]["trades"] += 1
        signal_stats[sig]["pnl"] += row[3] or 0
        if row[3] and row[3] > 0:
            signal_stats[sig]["wins"] += 1

    # Format date header
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
        date_display = dt.strftime("%b %d")
    except ValueError:
        date_display = date

    # Build text
    pnl_sign = "+" if total_pnl >= 0 else ""
    lines = [
        f"Paper Trading -- {date_display}",
        "",
        f"Trades: {opened_count} opened, {closed_count} closed",
        f"PnL: {pnl_sign}${total_pnl:.0f} (win rate: {win_rate}%)",
    ]

    if best_trade:
        lines.append(
            f"Best: {best_trade[1]} {'+' if best_trade[4] >= 0 else ''}{best_trade[4]:.0f}% "
            f"({'+' if best_trade[3] >= 0 else ''}${best_trade[3]:.0f})"
        )
    if worst_trade and closed_count > 1:
        lines.append(
            f"Worst: {worst_trade[1]} {worst_trade[4]:.0f}% "
            f"(${worst_trade[3]:.0f})"
        )

    if signal_stats:
        lines.append("")
        lines.append("By signal type:")
        for sig, stats in sorted(signal_stats.items()):
            sig_wr = round((stats["wins"] / stats["trades"]) * 100) if stats["trades"] > 0 else 0
            pnl_s = "+" if stats["pnl"] >= 0 else ""
            lines.append(
                f"  {sig}: {stats['trades']} trades, {pnl_s}${stats['pnl']:.0f} ({sig_wr}% WR)"
            )

    lines.append("")
    lines.append(f"Open positions: {open_count} (${open_exposure:,.0f} exposure)")

    digest_text = "\n".join(lines)

    # Store daily summary in DB
    avg_pnl = (
        sum(r[4] or 0 for r in closed_rows) / closed_count
        if closed_count > 0
        else 0
    )
    try:
        await conn.execute(
            """INSERT OR REPLACE INTO paper_daily_summary
               (date, trades_opened, trades_closed, wins, losses,
                total_pnl_usd, best_trade_pnl, worst_trade_pnl,
                avg_pnl_pct, win_rate_pct, by_signal_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                date, opened_count, closed_count, wins, losses,
                total_pnl,
                best_trade[3] if best_trade else None,
                worst_trade[3] if worst_trade else None,
                round(avg_pnl, 2), win_rate,
                json.dumps(signal_stats) if signal_stats else None,
            ),
        )
        await conn.commit()
    except Exception:
        log.exception("paper_digest_db_error")

    return digest_text
```

- [ ] **Step 4: Run digest tests**

Run: `uv run pytest tests/test_trading_digest.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Wire into `scout/main.py`**

Make the following modifications to `scout/main.py`:

**5a. Add imports** at the top of the file (after existing imports):

```python
from scout.trading.engine import TradingEngine
from scout.trading.evaluator import evaluate_paper_trades
from scout.trading.digest import build_paper_digest
```

**5b. Initialize engine in `main()` function**, after `db.initialize()` and before `shutdown_event`:

```python
    # Paper trading engine (disabled by default)
    trading_engine: TradingEngine | None = None
    if settings.TRADING_ENABLED:
        trading_engine = TradingEngine(
            mode=settings.TRADING_MODE, db=db, settings=settings
        )
        logger.info("trading_engine_initialized", mode=settings.TRADING_MODE)
```

**5c. Wire into volume spikes** in `run_cycle()`, after `logger.info("volume_spikes_detected", ...)`:

Add `trading_engine` parameter to `run_cycle` signature:

```python
async def run_cycle(
    settings: Settings,
    db: Database,
    session: aiohttp.ClientSession,
    dry_run: bool = False,
    trading_engine: TradingEngine | None = None,
) -> dict:
```

After the `if spikes:` block in the volume spike section:

```python
                if trading_engine and spikes:
                    for spike in spikes:
                        await trading_engine.open_trade(
                            token_id=spike["coin_id"],
                            symbol=spike.get("symbol", ""),
                            name=spike.get("name", ""),
                            chain="coingecko",
                            signal_type="volume_spike",
                            signal_data={
                                "spike_ratio": spike.get("spike_ratio", 0),
                                "volume": spike.get("current_volume", 0),
                            },
                        )
```

**5d. Wire into narrative predictions** in `narrative_agent_loop()`, after `prediction_models` is populated and stored (after `store_predictions` call):

```python
                    # Paper trading: open trades for narrative predictions
                    if trading_engine and prediction_models:
                        for pred in prediction_models:
                            if not pred.is_control:
                                await trading_engine.open_trade(
                                    token_id=pred.coin_id,
                                    symbol=pred.symbol,
                                    name=pred.name,
                                    chain="coingecko",
                                    signal_type="narrative_prediction",
                                    signal_data={
                                        "fit": pred.narrative_fit_score,
                                        "category": pred.category_name,
                                    },
                                )
```

Note: `narrative_agent_loop` needs `trading_engine` passed as parameter:

```python
async def narrative_agent_loop(
    session: aiohttp.ClientSession,
    settings: Settings,
    db: Database,
    trading_engine: TradingEngine | None = None,
) -> None:
```

**5e. Wire evaluator into EVALUATE phase** in `narrative_agent_loop()`, right after the `evaluate_pending(...)` call in the EVALUATE section:

```python
                    # Paper trade evaluation (piggybacks on EVALUATE interval)
                    if settings.TRADING_ENABLED:
                        try:
                            await evaluate_paper_trades(db, settings)
                            logger.info("paper_trade_eval_complete")
                        except Exception:
                            logger.exception("paper_trade_eval_error")
```

**5f. Wire digest into daily schedule** in `_pipeline_loop()`, right after the existing daily summary block (after `last_summary_date = current_date`):

```python
                        # Paper trading daily digest
                        if settings.TRADING_ENABLED:
                            try:
                                yesterday = (
                                    datetime.now(timezone.utc) - timedelta(days=1)
                                ).strftime("%Y-%m-%d")
                                digest_text = await build_paper_digest(db, yesterday)
                                if not args.dry_run:
                                    await send_telegram_message(
                                        digest_text, session, settings
                                    )
                                logger.info("paper_trading_digest_sent")
                            except Exception as e:
                                logger.warning(
                                    "paper_trading_digest_error", error=str(e)
                                )
```

**5g. Update `_pipeline_loop` call** to pass `trading_engine`:

```python
                        stats = await run_cycle(
                            settings, db, session, dry_run=args.dry_run,
                            trading_engine=trading_engine,
                        )
```

**5h. Update `narrative_agent_loop` call** in the tasks section:

```python
            if settings.NARRATIVE_ENABLED:
                tasks.append(
                    asyncio.create_task(
                        narrative_agent_loop(session, settings, db, trading_engine)
                    )
                )
```

- [ ] **Step 6: Run full suite**

Run: `uv run pytest --tb=short -q`
Expected: All tests PASS (trading is disabled by default, existing tests unaffected)

- [ ] **Step 7: Commit**

```bash
git add scout/trading/digest.py scout/main.py tests/test_trading_digest.py
git commit -m "feat(trading): add daily digest and wire engine into main pipeline loop"
```

---

## Task 7: Dashboard API

**Files:**
- Modify: `dashboard/api.py`
- Modify: `dashboard/db.py`
- Test: `tests/test_trading_dashboard.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_trading_dashboard.py
"""Tests for paper trading dashboard API endpoints."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient, ASGITransport

from dashboard.api import create_app
from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "test.db"
    d = Database(db_path)
    await d.initialize()
    yield d, str(db_path)
    await d.close()


@pytest.fixture
async def client(db):
    d, db_path = db
    app = create_app(db_path=db_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, d


async def _insert_trade(conn, token_id, symbol, signal_type, status, pnl_usd=None, pnl_pct=None):
    now = datetime.now(timezone.utc)
    opened = (now - timedelta(hours=2)).isoformat()
    closed = now.isoformat() if status != "open" else None
    await conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
            status, pnl_usd, pnl_pct, opened_at, closed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            token_id, symbol, token_id.title(), "coingecko", signal_type,
            json.dumps({}),
            100.0, 1000.0, 10.0, 20.0, 10.0, 120.0, 90.0,
            status, pnl_usd, pnl_pct, opened, closed,
        ),
    )
    await conn.commit()


async def test_get_positions(client):
    c, db = client
    await _insert_trade(db._conn, "bitcoin", "BTC", "volume_spike", "open")
    resp = await c.get("/api/trading/positions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["symbol"] == "BTC"


async def test_get_history(client):
    c, db = client
    await _insert_trade(db._conn, "bitcoin", "BTC", "volume_spike", "closed_tp", 200.0, 20.0)
    resp = await c.get("/api/trading/history")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1


async def test_get_stats(client):
    c, db = client
    await _insert_trade(db._conn, "bitcoin", "BTC", "volume_spike", "closed_tp", 200.0, 20.0)
    await _insert_trade(db._conn, "ethereum", "ETH", "narrative_prediction", "closed_sl", -50.0, -5.0)
    resp = await c.get("/api/trading/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_pnl_usd" in data
    assert "win_rate_pct" in data


async def test_get_stats_by_signal(client):
    c, db = client
    await _insert_trade(db._conn, "bitcoin", "BTC", "volume_spike", "closed_tp", 200.0, 20.0)
    await _insert_trade(db._conn, "ethereum", "ETH", "volume_spike", "closed_sl", -50.0, -5.0)
    resp = await c.get("/api/trading/stats/by-signal")
    assert resp.status_code == 200
    data = resp.json()
    assert "volume_spike" in data


async def test_positions_empty(client):
    c, _ = client
    resp = await c.get("/api/trading/positions")
    assert resp.status_code == 200
    assert resp.json() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_trading_dashboard.py -v`
Expected: FAIL -- endpoints do not exist yet

- [ ] **Step 3: Add query functions to `dashboard/db.py`**

Add the following functions at the end of `dashboard/db.py`:

```python
async def get_trading_positions(db_path: str) -> list[dict]:
    """Open paper trades."""
    async with _ro_db(db_path) as db:
        cursor = await db.execute(
            """SELECT id, token_id, symbol, name, chain, signal_type,
                      entry_price, amount_usd, quantity,
                      tp_price, sl_price, tp_pct, sl_pct,
                      peak_price, peak_pct,
                      checkpoint_1h_pct, checkpoint_6h_pct,
                      checkpoint_24h_pct, checkpoint_48h_pct,
                      opened_at
               FROM paper_trades
               WHERE status = 'open'
               ORDER BY opened_at DESC"""
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_trading_history(
    db_path: str, limit: int = 50, offset: int = 0
) -> list[dict]:
    """Closed paper trades, paginated."""
    async with _ro_db(db_path) as db:
        cursor = await db.execute(
            """SELECT id, token_id, symbol, name, chain, signal_type,
                      entry_price, exit_price, amount_usd,
                      pnl_usd, pnl_pct, exit_reason, status,
                      peak_price, peak_pct,
                      checkpoint_1h_pct, checkpoint_6h_pct,
                      checkpoint_24h_pct, checkpoint_48h_pct,
                      opened_at, closed_at
               FROM paper_trades
               WHERE status != 'open'
               ORDER BY closed_at DESC
               LIMIT ? OFFSET ?""",
            (limit, offset),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_trading_stats(db_path: str, days: int = 7) -> dict:
    """Aggregate paper trading PnL stats."""
    async with _ro_db(db_path) as db:
        cursor = await db.execute(
            """SELECT
                 COUNT(*) as total_trades,
                 SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                 SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) as losses,
                 COALESCE(SUM(pnl_usd), 0) as total_pnl_usd,
                 COALESCE(AVG(pnl_pct), 0) as avg_pnl_pct,
                 MAX(pnl_usd) as best_trade,
                 MIN(pnl_usd) as worst_trade
               FROM paper_trades
               WHERE status != 'open'
                 AND closed_at >= datetime('now', ?)""",
            (f"-{days} days",),
        )
        row = await cursor.fetchone()
        total = row[0] or 0
        wins = row[1] or 0

        # Open positions count
        cursor2 = await db.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount_usd), 0) FROM paper_trades WHERE status = 'open'"
        )
        open_row = await cursor2.fetchone()

        return {
            "total_trades": total,
            "wins": wins,
            "losses": row[2] or 0,
            "total_pnl_usd": round(row[3] or 0, 2),
            "avg_pnl_pct": round(row[4] or 0, 2),
            "best_trade": row[5],
            "worst_trade": row[6],
            "win_rate_pct": round((wins / total) * 100, 1) if total > 0 else 0,
            "open_positions": open_row[0] or 0,
            "open_exposure": round(open_row[1] or 0, 2),
        }


async def get_trading_stats_by_signal(db_path: str, days: int = 7) -> dict:
    """Paper trading PnL breakdown by signal type."""
    async with _ro_db(db_path) as db:
        cursor = await db.execute(
            """SELECT signal_type,
                 COUNT(*) as trades,
                 COALESCE(SUM(pnl_usd), 0) as pnl,
                 SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins
               FROM paper_trades
               WHERE status != 'open'
                 AND closed_at >= datetime('now', ?)
               GROUP BY signal_type""",
            (f"-{days} days",),
        )
        rows = await cursor.fetchall()
        result = {}
        for row in rows:
            total = row[1]
            w = row[3] or 0
            result[row[0]] = {
                "trades": total,
                "pnl": round(row[2], 2),
                "win_rate": round((w / total) * 100, 1) if total > 0 else 0,
            }
        return result
```

- [ ] **Step 4: Add endpoints to `dashboard/api.py`**

Add the following endpoints inside `create_app()`, after the existing narrative endpoints:

```python
    # --- Paper trading endpoints ---

    @app.get("/api/trading/positions")
    async def get_trading_positions_endpoint():
        return await db.get_trading_positions(_db_path)

    @app.get("/api/trading/history")
    async def get_trading_history_endpoint(
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        return await db.get_trading_history(_db_path, limit=limit, offset=offset)

    @app.get("/api/trading/stats")
    async def get_trading_stats_endpoint(
        days: int = Query(7, ge=1, le=365),
    ):
        return await db.get_trading_stats(_db_path, days=days)

    @app.get("/api/trading/stats/by-signal")
    async def get_trading_stats_by_signal_endpoint(
        days: int = Query(7, ge=1, le=365),
    ):
        return await db.get_trading_stats_by_signal(_db_path, days=days)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_trading_dashboard.py -v`
Expected: All 6 tests PASS

- [ ] **Step 6: Run full suite**

Run: `uv run pytest --tb=short -q`
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add dashboard/api.py dashboard/db.py tests/test_trading_dashboard.py
git commit -m "feat(trading): add dashboard API endpoints for paper trading positions, history, and stats"
```

---

## Summary

| Task | Files Created | Files Modified | Tests |
|------|--------------|----------------|-------|
| 1. Models + Config | `scout/trading/__init__.py`, `scout/trading/models.py` | `scout/config.py`, `.env.example` | 6 |
| 2. DB Schema | -- | `scout/db.py` | 6 |
| 3. Paper Trader | `scout/trading/paper.py` | -- | 7 |
| 4. Trading Engine | `scout/trading/engine.py` | -- | 8 |
| 5. Evaluator | `scout/trading/evaluator.py` | -- | 8 |
| 6. Digest + Integration | `scout/trading/digest.py` | `scout/main.py` | 4 |
| 7. Dashboard API | -- | `dashboard/api.py`, `dashboard/db.py` | 6 |
| **Total** | **6 new files** | **6 modified files** | **45 tests** |

### Key reviewer feedback addressed

- **Price staleness**: `_get_current_price_with_age()` checks `updated_at` is within 300s before opening; evaluator logs `price_age_seconds` with every checkpoint
- **sl_pct positive**: `PAPER_SL_PCT=10.0` means 10% loss; `sl_price = entry * (1 - sl_pct/100)`; validator on both model and Settings rejects negative values
- **Slippage**: `PAPER_SLIPPAGE_BPS=50` applied as `price * (1 + bps/10000)` on entry, `price * (1 - bps/10000)` on exit
- **Batch price lookup**: Evaluator fetches all prices in a single `SELECT ... WHERE coin_id IN (...)` query, not N queries per trade
