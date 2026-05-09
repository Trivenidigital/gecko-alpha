from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class DepthLevel:
    price: Decimal
    qty: Decimal


@dataclass(frozen=True)
class Depth:
    pair: str
    bids: tuple[DepthLevel, ...]  # descending
    asks: tuple[DepthLevel, ...]  # ascending
    mid: Decimal
    fetched_at: datetime


@dataclass(frozen=True)
class WalkResult:
    vwap: Decimal | None  # None if insufficient_liquidity
    filled_qty: Decimal
    filled_notional: Decimal
    slippage_bps: int | None
    insufficient_liquidity: bool


@dataclass(frozen=True)
class ResolvedVenue:
    symbol: str
    venue: str
    pair: str
    source: Literal["cache", "override_table", "binance_exchangeinfo"]


@dataclass(frozen=True)
class KillState:
    kill_event_id: int
    killed_until: datetime
    reason: str
    triggered_by: Literal["daily_loss_cap", "manual", "ops_maintenance"]


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reject_reason: str | None = None  # matches §3.1 CHECK enum when non-None
    detail: str | None = None
