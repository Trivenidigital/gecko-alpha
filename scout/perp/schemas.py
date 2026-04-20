"""Pydantic models for perp WebSocket ticks and anomaly events."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, field_validator

from scout.perp import TICKER_PATTERN

AnomalyKind = Literal["funding_flip", "oi_spike"]
Exchange = Literal["binance", "bybit"]


class PerpTick(BaseModel):
    exchange: Exchange
    symbol: str
    ticker: str
    funding_rate: float | None = None
    mark_price: float | None = None
    open_interest: float | None = None
    open_interest_usd: float | None = None
    timestamp: datetime

    @field_validator("ticker")
    @classmethod
    def _ticker_charset(cls, v: str) -> str:
        if not TICKER_PATTERN.match(v):
            raise ValueError(f"invalid ticker: {v!r}")
        return v

    @field_validator("symbol")
    @classmethod
    def _symbol_len(cls, v: str) -> str:
        if not (1 <= len(v) <= 32):
            raise ValueError(f"symbol {v!r} length {len(v)} out of bounds (max 32)")
        return v


class PerpAnomaly(BaseModel):
    exchange: Exchange
    symbol: str
    ticker: str
    kind: AnomalyKind
    magnitude: float
    baseline: float | None = None
    observed_at: datetime
