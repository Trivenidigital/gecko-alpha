"""Pydantic models for perp WebSocket ticks and anomaly events."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from scout.perp import TICKER_PATTERN

AnomalyKind = Literal["funding_flip", "oi_spike"]
Exchange = Literal["binance", "bybit"]


class PerpTick(BaseModel):
    model_config = ConfigDict(extra="ignore")

    exchange: Exchange
    symbol: str
    ticker: str
    # allow_inf_nan=False rejects NaN/Inf at schema level (defense layer 1).
    # BaselineStore.update has a second math.isfinite guard (defense layer 2).
    funding_rate: float | None = Field(default=None, allow_inf_nan=False)
    mark_price: float | None = Field(default=None, allow_inf_nan=False)
    open_interest: float | None = Field(default=None, allow_inf_nan=False)
    open_interest_usd: float | None = Field(default=None, allow_inf_nan=False)
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

    @field_validator("timestamp")
    @classmethod
    def _timestamp_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return v


class PerpAnomaly(BaseModel):
    model_config = ConfigDict(extra="ignore")

    exchange: Exchange
    symbol: str
    ticker: str
    kind: AnomalyKind
    magnitude: float
    baseline: float | None = None
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def _observed_at_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return v
