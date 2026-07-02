"""Pydantic models for the paper trading engine."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

from scout.price_sources import REGISTERED_PRICE_SOURCES

# Canonical set of trade status strings accepted by the evaluator + dashboard.
# Mirrors `scout.trading.paper.CLOSED_COUNTABLE_STATUSES` for the closed set
# but adds the runtime-only `open` and `closed_manual` / `closed_floor` /
# `closed_peak_fade` variants that aren't counted in standard rollups.
TradeStatus = Literal[
    "open",
    "closed_tp",
    "closed_sl",
    "closed_expired",
    "closed_trailing_stop",
    "closed_moonshot_trail",  # BL-063
    "closed_floor",
    "closed_peak_fade",
    "closed_manual",
    "closed_stale_onset",  # Phase 6 slice 3 — stale-onset exit at last-good mark
]


class PaperTradeOpen(BaseModel):
    """App-boundary contract for opening a paper trade (Phase 6 slice 2).

    Validated at the top of ``PaperTrader.execute_buy`` — the ONE funnel
    every open passes through — so the invariant "a position cannot be
    opened without a registered price source" holds even for callers
    that bypass the TradingEngine gate (belt and suspenders with the
    GA-01 dispatch gate in scout/trading/engine.py step 0c).

    ``price_source`` must be one of
    :data:`scout.price_sources.REGISTERED_PRICE_SOURCES`. ``'legacy'``
    is a migration-only backfill label and is rejected here by design.
    """

    token_id: str
    signal_type: str
    signal_combo: str
    price_source: str | None = None

    @model_validator(mode="after")
    def _require_registered_price_source(self) -> "PaperTradeOpen":
        if self.price_source not in REGISTERED_PRICE_SOURCES:
            raise ValueError(
                f"price_source {self.price_source!r} is not a registered price "
                f"source (registered: {sorted(REGISTERED_PRICE_SOURCES)}). "
                "A paper trade cannot open without a resolvable price source "
                "— see scout.price_sources.resolve_price_source."
            )
        return self


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

    status: TradeStatus = "open"

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

    # BL-063 moonshot exit upgrade — NULL until peak_pct crosses the
    # moonshot threshold; original_trail snapshot is preserved at arm
    # time for post-mortem analysis. Stored as an ISO-string TEXT in
    # SQLite and read back as a string by the evaluator (it never
    # constructs a PaperTrade from the row), so the type matches the
    # wire shape rather than promising a parsed datetime.
    moonshot_armed_at: str | None = None
    original_trail_drawdown_pct: float | None = None

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
