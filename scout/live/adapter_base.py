"""Minimal ExchangeAdapter ABC (spec §2.1). Binance is v1's only impl."""
from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from scout.live.types import Depth


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
