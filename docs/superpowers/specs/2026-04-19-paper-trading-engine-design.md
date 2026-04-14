# Paper Trading Engine — Design Spec

**Date:** 2026-04-19
**Status:** Approved
**Goal:** Pluggable trading engine that paper-trades every signal, tracks PnL at multi-checkpoints + TP/SL, sends daily digest. Foundation for live DEX trading in Phase B.
**Module:** `scout/trading/` — independent, pluggable from any signal source
**Cost:** $0 (paper mode uses price_cache from existing pipeline)

---

## 1. Architecture Overview

```
Signal Sources (existing)              scout/trading/engine.py
      │                                       │
      ├─ volume_spike ──────────→      engine.open_trade(
      ├─ narrative_prediction ──→        token_id, chain,
      ├─ trending_catch ────────→        signal_type, signal_data,
      ├─ chain_completed ──────→         amount_usd
      ├─ gainers_detected ─────→       )
      └─ category_heating ─────→              │
                                    ┌─────────┴──────────┐
                                    │  mode="paper"       │  mode="live" (Phase B)
                                    │  PaperTrader        │  LiveEVM / LiveSolana
                                    │  Log to DB at       │  Execute on-chain
                                    │  current price      │  via web3/solana-py
                                    └─────────┬──────────┘
                                              │
                                    Evaluator (every 30 min):
                                    ├─ Update 1h/6h/24h/48h checkpoints
                                    ├─ Check TP (+20%) / SL (-10%)
                                    ├─ Track peak price
                                    └─ Auto-expire after 48h
                                              │
                                    Daily Digest (midnight UTC):
                                    └─ Telegram: trades, PnL, by signal type
```

---

## 2. Module Structure

```
scout/trading/
  __init__.py
  engine.py        # TradingEngine — pluggable interface, mode routing
  paper.py         # PaperTrader — simulate fills, log to DB
  evaluator.py     # Checkpoint updates + TP/SL closure
  digest.py        # Daily PnL summary builder for Telegram
  models.py        # PaperTrade, TradeCheckpoint, TradeSummary
```

---

## 3. Models (`scout/trading/models.py`)

```python
from __future__ import annotations
from datetime import datetime
from pydantic import BaseModel, field_validator


class PaperTrade(BaseModel):
    id: int | None = None
    token_id: str
    symbol: str
    name: str
    chain: str
    signal_type: str              # volume_spike, narrative_prediction, etc.
    signal_data: dict             # signal-specific metadata

    entry_price: float
    amount_usd: float             # simulated position size
    quantity: float               # amount_usd / entry_price

    tp_pct: float = 20.0          # take profit %
    sl_pct: float = -10.0         # stop loss %
    tp_price: float               # entry_price * (1 + tp_pct/100)
    sl_price: float               # entry_price * (1 + sl_pct/100)

    status: str = "open"          # open, closed_tp, closed_sl, closed_expired, closed_manual

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


class TradeSummary(BaseModel):
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
    by_signal_type: dict          # {"volume_spike": {"trades": 5, "pnl": 230, "win_rate": 65}}
```

---

## 4. Pluggable Engine Interface (`engine.py`)

```python
class TradingEngine:
    """Pluggable trading engine. Call from any signal source.
    
    Usage:
        engine = TradingEngine(mode="paper", db=db, settings=settings)
        trade_id = await engine.open_trade(
            token_id="bless-2", chain="bsc",
            signal_type="volume_spike",
            signal_data={"spike_ratio": 12.3},
        )
    """

    def __init__(self, mode: str, db, settings):
        self.mode = mode  # "paper" or "live"
        self.db = db
        self.settings = settings

    async def open_trade(
        self, token_id: str, chain: str,
        signal_type: str, signal_data: dict,
        amount_usd: float | None = None,
    ) -> str | None:
        """Open a new trade. Returns trade_id or None if rejected."""
        # Check max exposure
        # Get current price from price_cache
        # If paper mode: delegate to PaperTrader
        # If live mode: delegate to LiveTrader (Phase B)

    async def close_trade(self, trade_id: int, reason: str = "manual") -> None:
        """Force-close a trade."""

    async def get_open_positions(self) -> list[dict]:
        """All open paper trades with current PnL."""

    async def get_pnl_summary(self, days: int = 7) -> dict:
        """Aggregate PnL statistics."""

    async def get_pnl_by_signal_type(self, days: int = 7) -> dict:
        """PnL breakdown by signal type — the key metric for Phase B decisions."""
```

### Exposure Control

Before opening a trade:
1. Check total open exposure: `SUM(amount_usd) FROM paper_trades WHERE status='open'`
2. If exposure + new_amount > `PAPER_MAX_EXPOSURE_USD` → reject, log warning
3. Check if same token already has an open trade → skip duplicate

### Price Lookup

Entry price comes from `price_cache` table (populated by pipeline each cycle):
```python
async def _get_current_price(self, token_id: str) -> float | None:
    """Look up price from price_cache table."""
    row = await self.db._conn.execute(
        "SELECT current_price FROM price_cache WHERE coin_id = ?",
        (token_id,),
    )
    # Fallback: fuzzy match by symbol prefix (same pattern as dashboard)
```

If price unavailable: skip trade, log `"trade_skipped_no_price"`.

---

## 5. Paper Trader (`paper.py`)

```python
class PaperTrader:
    """Simulates trade execution by logging to DB at current price."""

    async def execute_buy(
        self, db, token_id: str, symbol: str, name: str,
        chain: str, signal_type: str, signal_data: dict,
        current_price: float, amount_usd: float,
        tp_pct: float, sl_pct: float,
    ) -> int:
        """Record a paper buy. Returns trade ID."""
        quantity = amount_usd / current_price
        tp_price = current_price * (1 + tp_pct / 100)
        sl_price = current_price * (1 + sl_pct / 100)
        # INSERT into paper_trades
        # Return trade ID

    async def execute_sell(
        self, db, trade_id: int,
        current_price: float, reason: str,
    ) -> None:
        """Close a paper trade."""
        # Compute PnL
        # UPDATE paper_trades SET status, exit_price, pnl_usd, pnl_pct, closed_at
```

---

## 6. Trade Evaluator (`evaluator.py`)

Runs every 30 minutes in the narrative agent loop (same schedule as prediction evaluator).

```python
async def evaluate_paper_trades(db, settings):
    """Check all open paper trades: update checkpoints, check TP/SL, expire old."""
    
    # 1. Get all open trades
    # 2. Batch-fetch current prices from price_cache
    # 3. For each open trade:
    #    a. Update peak_price if current > peak
    #    b. Check TP: current_price >= tp_price → close "closed_tp"
    #    c. Check SL: current_price <= sl_price → close "closed_sl"
    #    d. Check expiry: opened_at + 48h → close "closed_expired"
    #    e. Update checkpoints (1h/6h/24h/48h) when time has passed
```

### Checkpoint Logic

Same pattern as narrative prediction evaluator:
```python
now = datetime.now(timezone.utc)
opened = trade.opened_at

if trade.checkpoint_1h_price is None and now >= opened + timedelta(hours=1):
    trade.checkpoint_1h_price = current_price
    trade.checkpoint_1h_pct = ((current_price - entry_price) / entry_price) * 100

# Same for 6h, 24h, 48h
```

### TP/SL takes priority over checkpoints

If TP/SL triggers at the same evaluation as a checkpoint, close the trade AND record the checkpoint.

---

## 7. Daily Digest (`digest.py`)

Runs at `TRADING_DIGEST_HOUR_UTC` (default midnight). Sent via existing `send_telegram_message()`.

```python
async def build_paper_digest(db, date: str) -> str:
    """Build daily paper trading summary."""
    # Query closed trades for the date
    # Compute: opened, closed, wins (pnl > 0), losses, total PnL
    # Best/worst trade
    # Breakdown by signal_type
    # Open positions count + total exposure
```

Format:
```
Paper Trading — Apr 19

Trades: 12 opened, 8 closed
PnL: +$340 (win rate: 62%)
Best: RAVE +45% (+$450)
Worst: ARIA -12% (-$120)

By signal type:
  volume_spike: 5 trades, +$230 (65% WR)
  narrative_prediction: 3 trades, +$80 (50% WR)
  trending_catch: 2 trades, +$30 (50% WR)

Open positions: 4 ($4,000 exposure)
```

---

## 8. Signal Integration Points

In `scout/main.py`, the engine is initialized at startup and called from every signal detection point:

```python
# At startup
if settings.TRADING_ENABLED:
    trading_engine = TradingEngine(mode=settings.TRADING_MODE, db=db, settings=settings)
else:
    trading_engine = None
```

### Volume Spikes
```python
if trading_engine and spikes:
    for spike in spikes:
        await trading_engine.open_trade(
            token_id=spike["coin_id"], chain="coingecko",
            signal_type="volume_spike",
            signal_data={"spike_ratio": spike["spike_ratio"], "volume": spike["current_volume"]},
        )
```

### Narrative Predictions
```python
if trading_engine and prediction_models:
    for pred in prediction_models:
        if not pred.is_control:
            await trading_engine.open_trade(
                token_id=pred.coin_id, chain="coingecko",
                signal_type="narrative_prediction",
                signal_data={"fit": pred.narrative_fit_score, "category": pred.category_name},
            )
```

### Chain Completions
```python
# In chain tracker, when a chain completes:
if trading_engine:
    await trading_engine.open_trade(
        token_id=match.token_id, chain=match.pipeline,
        signal_type="chain_completed",
        signal_data={"pattern": match.pattern_id, "boost": match.conviction_boost},
    )
```

### Trending Catches (new tokens appearing on trending)
```python
# In trending tracker, when a new token appears:
if trading_engine and is_new_on_trending:
    await trading_engine.open_trade(
        token_id=coin_id, chain="coingecko",
        signal_type="trending_catch",
        signal_data={"trending_rank": rank},
    )
```

### Top Gainers (early detection before gainer list)
```python
# In gainers tracker:
if trading_engine and is_new_gainer:
    await trading_engine.open_trade(
        token_id=coin_id, chain="coingecko",
        signal_type="gainers_early",
        signal_data={"price_change_24h": change},
    )
```

---

## 9. Database Schema

### `paper_trades`
```sql
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    chain TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    signal_data TEXT NOT NULL,          -- JSON

    entry_price REAL NOT NULL,
    amount_usd REAL NOT NULL,
    quantity REAL NOT NULL,

    tp_pct REAL NOT NULL DEFAULT 20.0,
    sl_pct REAL NOT NULL DEFAULT -10.0,
    tp_price REAL NOT NULL,
    sl_price REAL NOT NULL,

    status TEXT NOT NULL DEFAULT 'open',  -- open, closed_tp, closed_sl, closed_expired, closed_manual

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

    UNIQUE(token_id, signal_type, opened_at)  -- prevent duplicate trades
);
CREATE INDEX IF NOT EXISTS idx_paper_trades_status ON paper_trades(status);
CREATE INDEX IF NOT EXISTS idx_paper_trades_opened ON paper_trades(opened_at);
CREATE INDEX IF NOT EXISTS idx_paper_trades_signal ON paper_trades(signal_type);
```

### `paper_daily_summary`
```sql
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
    by_signal_type TEXT,               -- JSON
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

---

## 10. Configuration

```python
# Trading Engine
TRADING_ENABLED: bool = False                  # master switch
TRADING_MODE: str = "paper"                    # "paper" or "live"
PAPER_TRADE_AMOUNT_USD: float = 1000.0         # per trade (paper)
PAPER_MAX_EXPOSURE_USD: float = 10000.0        # max total open (paper)
PAPER_TP_PCT: float = 20.0                     # take profit %
PAPER_SL_PCT: float = -10.0                    # stop loss %
PAPER_MAX_DURATION_HOURS: int = 48             # auto-expire
TRADING_DIGEST_HOUR_UTC: int = 0               # midnight digest
TRADING_EVAL_INTERVAL: int = 1800              # 30 min eval cycle
```

Disabled by default. Enable with `TRADING_ENABLED=true` in `.env`.

---

## 11. Dashboard Integration

### API Endpoints
```
GET /api/trading/positions          — open paper trades with current PnL
GET /api/trading/history            — closed trades, paginated
GET /api/trading/stats              — aggregate PnL, win rate
GET /api/trading/stats/by-signal    — PnL per signal type (THE key metric)
```

### Dashboard Section (on Signals tab)

**Paper Trading Performance** panel:
- Stats cards: Total PnL, Win Rate, Open Positions, Avg PnL per Trade
- By-signal-type breakdown table: signal type, # trades, PnL, win rate
- Open positions table: token, signal, entry price, current price, PnL%, TP/SL status
- Recent closed trades: token, signal, entry→exit, PnL, reason (TP/SL/expired)

---

## 12. Error Handling

- Price unavailable for token → skip trade, log `trade_skipped_no_price`
- Max exposure exceeded → reject trade, log `trade_rejected_max_exposure`
- Duplicate trade (same token + signal_type + hour) → skip via UNIQUE constraint
- DB write failure → log error, continue (don't crash pipeline)
- Evaluator price fetch failure → skip this trade, retry next cycle
- Digest send failure → log error, don't block evaluator

---

## 13. Testing Strategy

| Test File | Coverage |
|-----------|----------|
| `tests/test_trading_engine.py` | Engine interface: open/close/positions/pnl, exposure limit, duplicate rejection, price lookup |
| `tests/test_paper_trader.py` | Buy/sell simulation, PnL calculation, quantity computation |
| `tests/test_trading_evaluator.py` | Checkpoint updates, TP/SL closure, expiry, peak tracking |
| `tests/test_trading_digest.py` | Digest formatting, by-signal-type aggregation, empty state |

---

## 14. Out of Scope (Phase B)

- Wallet connection / private key management
- On-chain DEX execution (web3-ethereum-defi, raydium_py)
- Gas estimation / slippage protection
- MEV protection
- Risk management beyond TP/SL
- Partial position scaling
- Cross-chain arbitrage
