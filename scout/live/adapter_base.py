"""Minimal ExchangeAdapter ABC (spec §2.1). Binance is v1's only impl."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from scout.live.types import Depth


@dataclass(frozen=True)
class VenueMetadata:
    """Generic venue metadata — Tier-1/2/3a/3b adapters all populate this
    from their venue-specific source (REST exchangeInfo, CCXT markets,
    CLI subprocess output, aggregator skill response). Routing layer
    consumes this without knowing the source."""

    venue: str
    canonical: str  # e.g. "BTC"
    venue_pair: str  # e.g. "BTCUSDT" / "PF_XBTUSD" / "BTC-USD"
    quote: str  # e.g. "USDT"
    asset_class: str  # 'spot' | 'perp' | 'option' | 'equity' | 'forex'
    min_size: float | None
    tick_size: float | None
    lot_size: float | None


@dataclass(frozen=True)
class OrderRequest:
    paper_trade_id: int
    canonical: str
    venue_pair: str
    side: str  # 'buy' | 'sell'
    size_usd: float
    intent_uuid: str  # gecko-side; populates client_order_id


@dataclass(frozen=True)
class OrderConfirmation:
    venue: str
    venue_order_id: str | None
    client_order_id: str | None
    status: str  # 'filled' | 'partial' | 'rejected' | 'pending' | 'timeout'
    filled_qty: float | None
    fill_price: float | None
    raw_response: dict[str, Any] | None


class ExchangeAdapter(ABC):
    venue_name: str

    @abstractmethod
    async def fetch_exchange_info_row(self, pair: str) -> dict | None:
        """Return parsed exchangeInfo row for `pair` or None on 404/delisted."""

    @abstractmethod
    async def resolve_pair_for_symbol(self, symbol: str) -> str | None:
        """Search exchangeInfo for symbol with quote=USDT, status=TRADING."""

    @abstractmethod
    async def fetch_depth(self, pair: str, limit: int = 100) -> Depth:
        """Return a Depth snapshot. Raises for transient failures."""

    @abstractmethod
    async def fetch_price(self, pair: str) -> Decimal:
        """Spot mid price via /ticker/price (weight=1)."""

    @abstractmethod
    async def send_order(self, *, pair: str, side: str, size_usd: Decimal) -> dict:
        """Live-mode real order. BL-055 implementations may raise
        NotImplementedError since live mode itself is gated at startup."""

    # ------------------------------------------------------------------
    # Multi-tier routing surface (BL-NEW-LIVE-HYBRID M1, Task 5).
    # Additive — new abstract methods alongside the legacy 5 above.
    # ------------------------------------------------------------------
    @abstractmethod
    async def fetch_venue_metadata(self, canonical: str) -> VenueMetadata | None:
        """Return VenueMetadata for canonical symbol or None if not listed
        on this venue. Generalizes the old fetch_exchange_info_row which
        was Binance-REST-shaped — the new shape returns a typed dataclass
        usable by the multi-tier routing layer."""
        ...

    @abstractmethod
    async def place_order_request(self, request: OrderRequest) -> str:
        """Submit order; return venue_order_id immediately. For two-step
        venues (Minara quote-then-confirm), this submits the request
        portion. For single-call venues (Binance REST), this submits +
        returns the order ID synchronously."""
        ...

    @abstractmethod
    async def await_fill_confirmation(
        self, *, venue_order_id: str, client_order_id: str, timeout_sec: float
    ) -> OrderConfirmation:
        """Wait for order to reach terminal state (filled/partial/rejected)
        or timeout."""
        ...

    @abstractmethod
    async def fetch_account_balance(self, asset: str = "USDT") -> float:
        """Return free balance in `asset`. Used by balance_gate."""
        ...
