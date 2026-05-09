"""CCXTAdapter — Tier 3b for the long tail (Bybit, OKX, Coinbase, MEXC,
Gate, etc.). Parameterized by venue name. Delegates to ccxt.<venue>.
Per design v2.1 — scaffolded at M1, NOT wired to any venue. M1.5 wires
the first CCXT venue."""

from __future__ import annotations

from typing import Any

import ccxt.async_support as ccxt_async  # async variant for asyncio
import structlog

from scout.live.adapter_base import (
    ExchangeAdapter,
    OrderConfirmation,
    OrderRequest,
    VenueMetadata,
)

log = structlog.get_logger(__name__)


class CCXTAdapter(ExchangeAdapter):
    """Generic CCXT-backed adapter. Constructor: CCXTAdapter('bybit', api_key=..., secret=...)."""

    def __init__(
        self,
        venue_name: str,
        *,
        api_key: str | None = None,
        secret: str | None = None,
        **ccxt_options: Any,
    ) -> None:
        self.venue_name = venue_name
        ccxt_class = getattr(ccxt_async, venue_name)
        self._client = ccxt_class(
            {
                "apiKey": api_key,
                "secret": secret,
                **ccxt_options,
            }
        )

    async def fetch_venue_metadata(self, canonical: str) -> VenueMetadata | None:
        # Load markets if not yet loaded; CCXT caches this internally.
        await self._client.load_markets()
        # Try common variations: BTC/USDT, BTC/USD, BTC/USDT:USDT (perp)
        for symbol in [
            f"{canonical}/USDT",
            f"{canonical}/USD",
            f"{canonical}/USDT:USDT",
        ]:
            if symbol in self._client.markets:
                m = self._client.markets[symbol]
                return VenueMetadata(
                    venue=self.venue_name,
                    canonical=canonical,
                    venue_pair=m["id"],
                    quote=m["quote"],
                    asset_class="perp" if m.get("contract") else "spot",
                    min_size=m.get("limits", {}).get("amount", {}).get("min"),
                    tick_size=m.get("precision", {}).get("price"),
                    lot_size=m.get("precision", {}).get("amount"),
                )
        return None

    async def resolve_pair_for_symbol(self, canonical: str) -> str | None:
        meta = await self.fetch_venue_metadata(canonical)
        return meta.venue_pair if meta is not None else None

    async def fetch_depth(self, pair: str) -> dict[str, Any]:
        return await self._client.fetch_l2_order_book(pair)

    async def place_order_request(self, request: OrderRequest) -> str:
        client_order_id = f"gecko-{request.paper_trade_id}-{request.intent_uuid}"
        # Convert size_usd → quantity using current price; this is venue-specific.
        # M1 scaffold returns NotImplementedError; M1.5 wires it.
        raise NotImplementedError(
            "CCXTAdapter is M1 scaffold; first wired venue is M1.5."
        )

    async def await_fill_confirmation(
        self, *, venue_order_id: str, client_order_id: str, timeout_sec: float
    ) -> OrderConfirmation:
        raise NotImplementedError(
            "CCXTAdapter is M1 scaffold; first wired venue is M1.5."
        )

    async def fetch_account_balance(self, asset: str = "USDT") -> float:
        balance = await self._client.fetch_balance()
        return float(balance.get(asset, {}).get("free", 0.0))

    async def close(self) -> None:
        await self._client.close()
