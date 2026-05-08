"""Binance spot adapter (spec §7, §8, §9, §10.1).

Shadow-mode HTTP client for Binance public endpoints:
- ``/api/v3/exchangeInfo`` — venue resolution
- ``/api/v3/depth`` — L2 orderbook snapshots
- ``/api/v3/ticker/price`` — spot price

Implements the weight-header governor (§9.1) and retry taxonomy (§10.1).
``send_order`` is intentionally not implemented — BL-055 is shadow only.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import aiohttp
import structlog

from scout.config import Settings
from scout.live.adapter_base import (
    ExchangeAdapter,
    OrderConfirmation,
    OrderRequest,
    VenueMetadata,
)
from scout.live.exceptions import RateLimitError, VenueTransientError
from scout.live.types import Depth, DepthLevel

log = structlog.get_logger(__name__)


_BASE_URL = "https://api.binance.com"
_BACKOFFS: tuple[float, ...] = (1.0, 2.0, 4.0)

# Spec §9.1 weight thresholds (max 1200/min).
_WEIGHT_SHRINK = 960  # 80% — shrink concurrency to 3
_WEIGHT_GATE_CLOSE = 1140  # 95% — close gate, pause for 10s

# Optional metric increment — the ``scout.live.metrics.inc`` helper is
# scheduled for BL-055 Task 11. Until then, guard the import so the adapter
# still works (metrics just no-op).
try:  # pragma: no cover - trivial import guard
    from scout.live.metrics import inc as _metric_inc  # type: ignore
except ImportError:  # pragma: no cover
    _metric_inc = None  # type: ignore[assignment]


class BinanceSpotAdapter(ExchangeAdapter):
    """Spot-venue adapter for Binance.

    Parameters
    ----------
    settings:
        Project ``Settings``. Reserved for future config (e.g. per-venue
        HTTP timeout overrides) — currently unused at the field level.
    db:
        Optional ``scout.db.Database`` for metric counters. When provided,
        429 responses bump ``live_metrics_daily.binance_rate_limit_hits``.
    """

    venue_name: str = "binance"

    # Class-level default — overridable per-instance (tests set 0.05s).
    _RATE_LIMIT_PAUSE_SEC: float = 10.0

    def __init__(self, settings: Settings, db: Any | None = None) -> None:
        self._settings = settings
        self._db = db

        timeout = aiohttp.ClientTimeout(total=10.0)
        self._session = aiohttp.ClientSession(timeout=timeout)

        # Gate is set (open) by default — requests are allowed.
        self._rate_limit_gate = asyncio.Event()
        self._rate_limit_gate.set()

        # Swappable in tests to skip real backoff delays.
        self._retry_sleep = asyncio.sleep

        # Current concurrency cap (consumed by the resolver / engine).
        # Shrinks to 3 at 80% weight usage (spec §9.1).
        self._current_semaphore_cap = 10

    # ------------------------------------------------------------------
    # HTTP core
    # ------------------------------------------------------------------
    async def _http_get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Central Binance GET with retry taxonomy (spec §10.1).

        Returns the parsed JSON body, or the sentinel ``{"__code": -1121}``
        when Binance answers 400 + code=-1121 (unknown symbol). Callers
        such as :meth:`fetch_exchange_info_row` translate that to ``None``.
        """
        await self._rate_limit_gate.wait()

        url = f"{_BASE_URL}{path}"
        last_exc: Exception | None = None

        for attempt in range(len(_BACKOFFS) + 1):
            try:
                async with self._session.get(url, params=params) as resp:
                    weight = int(resp.headers.get("X-MBX-USED-WEIGHT-1M", 0))
                    await self._update_weight_governor(weight)

                    if resp.status == 429:
                        if self._db is not None and _metric_inc is not None:
                            try:
                                await _metric_inc(self._db, "binance_rate_limit_hits")
                            except Exception:  # noqa: BLE001
                                # Metric write is best-effort; never mask
                                # the RateLimitError that follows.
                                log.warning(
                                    "binance_rate_limit_metric_failed",
                                    exc_info=True,
                                )
                        raise RateLimitError(f"binance 429 weight={weight}")

                    if 500 <= resp.status < 600:
                        last_exc = VenueTransientError(
                            f"binance {resp.status} attempt={attempt + 1}"
                        )
                        if attempt < len(_BACKOFFS):
                            await self._retry_sleep(_BACKOFFS[attempt])
                            continue
                        raise last_exc

                    if resp.status == 400:
                        body = await resp.json(content_type=None)
                        if isinstance(body, dict) and body.get("code") == -1121:
                            # Sentinel — caller translates to None.
                            return {"__code": -1121}
                        # Other 400s — raise immediately (no retry).
                        resp.raise_for_status()

                    if resp.status >= 400:
                        # 401/403/404/etc — raise immediately.
                        resp.raise_for_status()

                    return await resp.json()

            except (aiohttp.ClientConnectorError, asyncio.TimeoutError) as exc:
                # NOTE: some aiohttp versions raise from ``str(exc)`` when
                # the exception was constructed with a null connection key
                # (seen in tests). Render the type name instead — it is
                # both safe and sufficient for logging.
                last_exc = VenueTransientError(f"network error: {type(exc).__name__}")
                if attempt < len(_BACKOFFS):
                    await self._retry_sleep(_BACKOFFS[attempt])
                    continue
                raise last_exc

        # Shouldn't be reached — the loop either returns or raises.
        assert last_exc is not None
        raise last_exc

    async def _update_weight_governor(self, weight: int) -> None:
        """Adjust concurrency + gate state based on used-weight header."""
        if weight >= _WEIGHT_GATE_CLOSE:
            # 95%+ — close gate to throttle all future callers.
            if self._rate_limit_gate.is_set():
                self._rate_limit_gate.clear()
                pause = self._RATE_LIMIT_PAUSE_SEC  # read instance value

                async def _reopen_gate_later() -> None:
                    await asyncio.sleep(pause)
                    self._rate_limit_gate.set()

                asyncio.create_task(_reopen_gate_later())
            self._current_semaphore_cap = 3
        elif weight >= _WEIGHT_SHRINK:
            # 80%+ — shrink concurrency but keep gate open.
            self._current_semaphore_cap = 3
        else:
            self._current_semaphore_cap = 10

    # ------------------------------------------------------------------
    # Public API (ExchangeAdapter)
    # ------------------------------------------------------------------
    async def fetch_exchange_info_row(self, pair: str) -> dict | None:
        """Fetch the single-symbol exchangeInfo row.

        Returns ``None`` on Binance ``-1121`` ("Invalid symbol") — the
        terminal-unknown-symbol path. Other failures propagate.
        """
        body = await self._http_get("/api/v3/exchangeInfo", params={"symbol": pair})
        if body.get("__code") == -1121:
            return None
        symbols = body.get("symbols") or []
        if not symbols:
            return None
        return symbols[0]

    async def resolve_pair_for_symbol(self, symbol: str) -> str | None:
        """Probe ``{SYMBOL}USDT`` on Binance spot; return pair if TRADING."""
        pair = f"{symbol.upper()}USDT"
        row = await self.fetch_exchange_info_row(pair)
        if row is None:
            return None
        if row.get("status") != "TRADING":
            return None
        if row.get("quoteAsset") != "USDT":
            return None
        return pair

    async def fetch_depth(self, pair: str, limit: int = 100) -> Depth:
        """Fetch L2 depth for ``pair`` and compute the mid price."""
        body = await self._http_get(
            "/api/v3/depth", params={"symbol": pair, "limit": limit}
        )
        bids = tuple(
            DepthLevel(price=Decimal(str(p)), qty=Decimal(str(q)))
            for p, q in body.get("bids", [])
        )
        asks = tuple(
            DepthLevel(price=Decimal(str(p)), qty=Decimal(str(q)))
            for p, q in body.get("asks", [])
        )
        if not bids or not asks:
            # No mid computable — caller treats as DepthInsufficient upstream.
            mid = Decimal(0)
        else:
            mid = (bids[0].price + asks[0].price) / Decimal(2)
        return Depth(
            pair=pair,
            bids=bids,
            asks=asks,
            mid=mid,
            fetched_at=datetime.now(timezone.utc),
        )

    async def fetch_price(self, pair: str) -> Decimal:
        """Fetch spot price via ``/ticker/price`` (weight=1)."""
        body = await self._http_get("/api/v3/ticker/price", params={"symbol": pair})
        return Decimal(str(body["price"]))

    async def send_order(self, *, pair: str, side: str, size_usd: Decimal) -> dict:
        """Hard-block real order submission — BL-055 is shadow only (§1.3).

        The primary gate is ``scout/main.py`` (blocks on ``LIVE_MODE``); this
        raise is defense-in-depth so an accidental call path cannot reach
        Binance even if the startup guard is bypassed. Live orders land in
        BL-058.
        """
        raise NotImplementedError(
            "BL-055 shadow mode — send_order blocked; wire in BL-058"
        )

    # ------------------------------------------------------------------
    # Multi-tier routing surface (BL-NEW-LIVE-HYBRID M1, Task 5).
    # ------------------------------------------------------------------
    async def fetch_venue_metadata(self, canonical: str) -> VenueMetadata | None:
        """Resolve `canonical` on Binance spot and wrap as VenueMetadata.

        Reuses the legacy ``resolve_pair_for_symbol`` + ``fetch_exchange_info_row``
        path — keeps a single source of truth for Binance pair shape.
        Returns ``None`` if the canonical symbol is not listed as a
        TRADING USDT pair.

        Filter extraction (LOT_SIZE.minQty/stepSize, PRICE_FILTER.tickSize)
        is best-effort: if a filter is missing/malformed we set the field
        to ``None``. M1 routing layer treats ``None`` as "size validation
        deferred downstream" — see plan §Task 5.
        """
        pair = await self.resolve_pair_for_symbol(canonical)
        if pair is None:
            return None
        row = await self.fetch_exchange_info_row(pair)
        if row is None:
            return None

        min_size: float | None = None
        tick_size: float | None = None
        lot_size: float | None = None
        for f in row.get("filters", []) or []:
            ftype = f.get("filterType")
            try:
                if ftype == "LOT_SIZE":
                    min_size = float(f.get("minQty")) if f.get("minQty") else None
                    lot_size = float(f.get("stepSize")) if f.get("stepSize") else None
                elif ftype == "PRICE_FILTER":
                    tick_size = float(f.get("tickSize")) if f.get("tickSize") else None
            except (TypeError, ValueError):
                # Malformed filter — leave field as None.
                continue

        return VenueMetadata(
            venue="binance",
            canonical=canonical.upper(),
            venue_pair=row.get("symbol", pair),
            quote=row.get("quoteAsset", "USDT"),
            asset_class="spot",
            min_size=min_size,
            tick_size=tick_size,
            lot_size=lot_size,
        )

    async def place_order_request(self, request: OrderRequest) -> str:
        """Hard-block order placement — BL-055 is shadow only (§1.3).

        Defense-in-depth alongside ``send_order``; live wiring lands in
        Task 12 (client_order_id idempotency).
        """
        raise NotImplementedError(
            "BL-055 shadow mode — place_order_request blocked; "
            "wire in Task 12 (client_order_id idempotency)"
        )

    async def await_fill_confirmation(
        self,
        *,
        venue_order_id: str,
        client_order_id: str,
        timeout_sec: float,
    ) -> OrderConfirmation:
        """Hard-block fill confirmation — BL-055 is shadow only (§1.3).

        Wired in Task 12 once ``place_order_request`` is unblocked.
        """
        raise NotImplementedError(
            "BL-055 shadow mode — await_fill_confirmation blocked; " "wire in Task 12"
        )

    async def fetch_account_balance(self, asset: str = "USDT") -> float:
        """Return free balance in `asset`.

        Stubbed for M1 — Task 8 (balance_gate.py) will implement the
        signed ``/api/v3/account`` request with HMAC + timestamp + recvWindow.
        Stubbing avoids landing partial signed-request code on the
        adapter while balance_gate's full requirements are still being
        fleshed out.
        """
        raise NotImplementedError(
            "fetch_account_balance: implement in Task 8 (balance_gate.py)"
        )

    async def close(self) -> None:
        """Close the underlying aiohttp session."""
        await self._session.close()
