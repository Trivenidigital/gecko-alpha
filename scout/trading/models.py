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
    sl_pct: float = (
        10.0  # positive: 10.0 means 10% stop loss; 0 means no stop loss (used for long_hold trades)
    )
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
            raise ValueError("sl_pct must be positive, e.g. 10.0 for 10% stop loss")
        return v

    @field_validator("tp_pct")
    @classmethod
    def _validate_tp_pct_positive(cls, v: float) -> float:
        if v < 0:
            raise ValueError("tp_pct must be positive, e.g. 20.0 for 20% take profit")
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
