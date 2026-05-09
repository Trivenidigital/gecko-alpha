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
import sqlite3
import time
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


# M1.5a — typed exceptions for signed-endpoint failures (Task 1.5).
class BinanceAuthError(Exception):
    """-2014 (signature) / -2015 (auth) / -1021 (timestamp). Never retry."""


class BinanceIPBanError(Exception):
    """HTTP 418 — distinct from 429; back off MINUTES (operator action)."""


class BinanceDuplicateOrderError(Exception):
    """-2010 duplicate newClientOrderId. Caller recovers via origClientOrderId GET."""


class BinanceInsufficientFundsError(Exception):
    """-2018 Balance is insufficient / -2019 Margin is insufficient. PR #86 V1-C1.

    Distinct from BinanceAuthError (different operator action — fund the
    account vs rotate the key) and from VenueTransientError (NEVER retry —
    funds aren't going to grow on retry). Engine layer maps to
    reject_reason='insufficient_balance'.
    """


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
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        signed: bool = False,
    ) -> dict[str, Any]:
        """Central Binance HTTP — core method shared by signed + unsigned callers.

        Retry taxonomy (spec §10.1):
        - 200 → return JSON
        - 400 + code=-1121 (unknown symbol) → return ``{"__code": -1121}`` sentinel
        - 400 + code=-2010 (duplicate cid) → raise ``BinanceDuplicateOrderError`` (signed=True only)
        - 4xx + auth codes -2014/-2015/-1021 → raise ``BinanceAuthError`` (signed=True only)
        - 418 (IP ban) → raise ``BinanceIPBanError``
        - 429 (rate limit) → raise ``RateLimitError``
        - 5xx → retry up to 3 times with backoff, then raise ``VenueTransientError``
        - Other 4xx → ``resp.raise_for_status()``
        - Network/timeout → retry up to 3 times, then raise ``VenueTransientError``

        Added in M1.5a (Task 1.5) to share retry+weight+429+418 across signed
        and unsigned codepaths (R1-C1 plan-stage finding).
        """
        await self._rate_limit_gate.wait()

        url = f"{_BASE_URL}{path}"
        last_exc: Exception | None = None

        for attempt in range(len(_BACKOFFS) + 1):
            try:
                if method == "GET":
                    cm = self._session.get(url, params=params, headers=headers)
                elif method == "POST":
                    cm = self._session.post(url, params=params, headers=headers)
                else:
                    raise ValueError(f"Unsupported method: {method!r}")

                async with cm as resp:
                    weight = int(resp.headers.get("X-MBX-USED-WEIGHT-1M", 0))
                    await self._update_weight_governor(weight)

                    if resp.status == 418:
                        # IP-ban — distinct from 429 transient (R1-I1)
                        body = await resp.json(content_type=None)
                        raise BinanceIPBanError(
                            f"{method} {path}: 418 IP-banned: {body}"
                        )

                    if resp.status == 429:
                        if self._db is not None and _metric_inc is not None:
                            try:
                                await _metric_inc(self._db, "binance_rate_limit_hits")
                            except Exception:  # noqa: BLE001
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
                        # R2-M1: content-type sniff before json() — CDN HTML 5xx
                        # masquerading as 400 raises clearer error.
                        ctype = resp.headers.get("Content-Type", "")
                        if "text/html" in ctype:
                            raise VenueTransientError(
                                f"binance 400: CDN error (HTML response)"
                            )
                        body = await resp.json(content_type=None)
                        if isinstance(body, dict):
                            code = body.get("code")
                            if code == -1121:
                                # Unknown symbol — sentinel preserved
                                return {"__code": -1121}
                            if signed and code in (-2014, -2015, -1021):
                                # Auth-class — never retry (R1-I1)
                                raise BinanceAuthError(
                                    f"{method} {path} failed code={code} "
                                    f"msg={body.get('msg')!r}"
                                )
                            if signed and code == -2010:
                                # Duplicate clientOrderId (R2-I2 dedup race)
                                raise BinanceDuplicateOrderError(
                                    f"duplicate newClientOrderId: {body.get('msg')!r}"
                                )
                            if signed and code in (-2018, -2019):
                                # PR #86 V1-C1: Balance/margin insufficient.
                                # Distinct from generic transient error so
                                # engine layer maps to insufficient_balance.
                                raise BinanceInsufficientFundsError(
                                    f"{method} {path} code={code} "
                                    f"msg={body.get('msg')!r}"
                                )
                        # Other 400s — raise (no retry).
                        resp.raise_for_status()

                    if resp.status >= 400:
                        # Signed callers: also map auth codes from non-400 responses
                        if signed:
                            try:
                                body = await resp.json(content_type=None)
                                code = (
                                    body.get("code") if isinstance(body, dict) else None
                                )
                                if code in (-2014, -2015, -1021):
                                    raise BinanceAuthError(
                                        f"{method} {path} failed code={code} "
                                        f"msg={body.get('msg')!r}"
                                    )
                            except (aiohttp.ContentTypeError, ValueError):
                                pass
                        resp.raise_for_status()

                    return await resp.json()

            except (aiohttp.ClientConnectorError, asyncio.TimeoutError) as exc:
                last_exc = VenueTransientError(f"network error: {type(exc).__name__}")
                if attempt < len(_BACKOFFS):
                    await self._retry_sleep(_BACKOFFS[attempt])
                    continue
                raise last_exc

        assert last_exc is not None
        raise last_exc

    async def _http_get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Public unsigned GET — back-compat wrapper over `_request`."""
        return await self._request("GET", path, params=params, signed=False)

    def _api_secret(self) -> str:
        """Extract API secret from SecretStr, raise if missing."""
        s = self._settings.BINANCE_API_SECRET
        if s is None:
            raise BinanceAuthError("BINANCE_API_SECRET not configured")
        # SecretStr.get_secret_value() returns raw string
        return s.get_secret_value() if hasattr(s, "get_secret_value") else str(s)

    def _api_key(self) -> str:
        """Extract API key from SecretStr, raise if missing."""
        s = self._settings.BINANCE_API_KEY
        if s is None:
            raise BinanceAuthError("BINANCE_API_KEY not configured")
        return s.get_secret_value() if hasattr(s, "get_secret_value") else str(s)

    async def _signed_get(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Signed GET — adds HMAC-SHA256 + timestamp + recvWindow + X-MBX-APIKEY.
        All retry/weight/auth-error logic lives in `_request`."""
        from scout.live.binance_signing import sign_request

        body_params: dict[str, Any] = dict(params or {})
        body_params["timestamp"] = int(time.time() * 1000)
        body_params["recvWindow"] = 10000  # V1-M3: jitter tolerance for EU-VPS NTP
        signed_params, _sig = sign_request(self._api_secret(), body_params)
        headers = {"X-MBX-APIKEY": self._api_key()}
        return await self._request(
            "GET", path, params=signed_params, headers=headers, signed=True
        )

    async def _signed_post(
        self, path: str, *, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Signed POST — same signature scheme as _signed_get; full retry +
        5xx handling inherited from `_request` (R1-C2 fix — original plan
        had no retry for POST, leaking 503s as fatal)."""
        from scout.live.binance_signing import sign_request

        body_params: dict[str, Any] = dict(params)
        body_params["timestamp"] = int(time.time() * 1000)
        body_params["recvWindow"] = 10000  # V1-M3: jitter tolerance for EU-VPS NTP
        signed_params, _sig = sign_request(self._api_secret(), body_params)
        headers = {"X-MBX-APIKEY": self._api_key()}
        return await self._request(
            "POST", path, params=signed_params, headers=headers, signed=True
        )

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
        """Submit market order via signed POST /api/v3/order. Returns venue_order_id.

        M1.5a runtime body (Task 4). Idempotency-aware:
        - Pre-checks live_trades for the client_order_id (M1's idempotency.py
          + UNIQUE INDEX backstop)
        - Captures mid_at_entry from a quick depth fetch — used by
          await_fill_confirmation to compute fill_slippage_bps
        - Records pending row before Binance submit; UNIQUE INDEX rejects
          concurrent INSERT race (R1-C3 fix — re-call lookup on collision)
        - Catches BinanceDuplicateOrderError (-2010 from prior retry's
          successful Binance submit; R2-I2 fix) — recovers via
          origClientOrderId GET to retrieve the existing orderId
        - Rejects empty/missing orderId from Binance response (R1-C6 fix)
        - Acquires _txn_lock around UPDATE entry_order_id (R1-I2 fix)
        - Gated by LIVE_USE_REAL_SIGNED_REQUESTS (R2-I4 emergency-revert)
        """
        if not getattr(self._settings, "LIVE_USE_REAL_SIGNED_REQUESTS", False):
            raise NotImplementedError(
                "LIVE_USE_REAL_SIGNED_REQUESTS=False — emergency-revert posture"
            )

        from scout.live.idempotency import (
            lookup_existing_order_id,
            make_client_order_id,
            record_pending_order,
        )

        if self._db is None:
            raise RuntimeError(
                "place_order_request requires db wired into BinanceSpotAdapter"
            )

        cid = make_client_order_id(request.paper_trade_id, request.intent_uuid)

        # Step 1: cheap dedup
        existing = await lookup_existing_order_id(self._db, cid)
        if existing is not None:
            log.info(
                "place_order_dedup_hit",
                client_order_id=cid,
                venue_order_id=existing,
            )
            return existing

        # Step 2: capture mid_at_entry for slippage compute
        mid_str: str | None = None
        try:
            depth = await self.fetch_depth(request.venue_pair)
            mid_str = str(depth.mid)
        except Exception:
            log.exception("place_order_mid_fetch_failed", canonical=request.canonical)

        # PR #86 V2-C1: look up CoinGecko slug from paper_trades to keep
        # live_trades.coin_id semantically consistent with engine.py path
        # (which writes paper_trade.coin_id slug, e.g. "bitcoin"). The
        # naive lower() ("btc") would silently corrupt analytics.
        coin_id_slug = request.canonical.lower()  # safe fallback
        try:
            cur_pt = await self._db._conn.execute(
                "SELECT token_id FROM paper_trades WHERE id = ?",
                (request.paper_trade_id,),
            )
            row = await cur_pt.fetchone()
            if row is not None and row[0]:
                coin_id_slug = row[0]
        except Exception:
            log.exception(
                "place_order_coin_id_lookup_failed",
                paper_trade_id=request.paper_trade_id,
            )

        # Step 3: record pending row, handle concurrent-INSERT race (R1-C3)
        # PR #86 V2-I1: narrow except to sqlite3.IntegrityError so other
        # exceptions (RuntimeError, TypeError) propagate cleanly.
        try:
            await record_pending_order(
                self._db,
                client_order_id=cid,
                paper_trade_id=request.paper_trade_id,
                coin_id=coin_id_slug,
                symbol=request.canonical,
                venue=self.venue_name,
                pair=request.venue_pair,
                signal_type="",  # filled by engine layer
                size_usd=str(request.size_usd),
                mid_at_entry=mid_str,
            )
        except sqlite3.IntegrityError as exc:
            # UNIQUE constraint on client_order_id — another retry beat us.
            # Re-lookup; if still NULL entry_order_id, fall through to submit
            # — Binance's -2010 catches if our prior retry already POSTed.
            existing = await lookup_existing_order_id(self._db, cid)
            if existing is not None:
                log.info(
                    "place_order_dedup_hit_post_race",
                    client_order_id=cid,
                    venue_order_id=existing,
                )
                return existing
            log.warning(
                "place_order_record_pending_race",
                client_order_id=cid,
                err=str(exc),
            )

        # Step 4: submit to Binance, handle -2010 (R2-I2)
        try:
            body = await self._signed_post(
                "/api/v3/order",
                params={
                    "symbol": request.venue_pair,
                    "side": request.side.upper(),
                    "type": "MARKET",
                    "quoteOrderQty": str(request.size_usd),
                    "newClientOrderId": cid,
                },
            )
        except BinanceDuplicateOrderError:
            # Prior retry's POST succeeded but response was lost (network drop).
            # Recover by reading the existing order via origClientOrderId.
            log.info("place_order_recovered_from_duplicate", client_order_id=cid)
            body = await self._signed_get(
                "/api/v3/order",
                params={
                    "symbol": request.venue_pair,
                    "origClientOrderId": cid,
                },
            )

        # Step 5: validate orderId, persist, return (R1-C6 fix)
        order_id_raw = body.get("orderId")
        if order_id_raw in (None, "", 0):
            raise VenueTransientError(f"Binance response missing orderId: {body!r}")
        venue_order_id = str(order_id_raw)

        # Step 6: persist entry_order_id under txn lock (R1-I2)
        async with self._db._txn_lock:
            await self._db._conn.execute(
                "UPDATE live_trades SET entry_order_id = ? WHERE client_order_id = ?",
                (venue_order_id, cid),
            )
            await self._db._conn.commit()

        log.info(
            "place_order_submitted",
            client_order_id=cid,
            venue_order_id=venue_order_id,
            venue_pair=request.venue_pair,
        )
        return venue_order_id

    async def await_fill_confirmation(
        self,
        *,
        venue_order_id: str,
        client_order_id: str,
        timeout_sec: float,
    ) -> OrderConfirmation:
        """Poll signed GET /api/v3/order until terminal state or timeout.

        M1.5a runtime body (Task 5). Adaptive backoff (200ms→500ms→1s→2s).

        - Pre-loop SELECT pair FROM live_trades WHERE client_order_id=? (R1-C4
          fix; cached for the entire poll; no per-iteration DB hit)
        - On terminal FILLED/PARTIAL: compute avg fill price (sync helper,
          R1-C5 fix), write fill_slippage_bps under _txn_lock (R1-I2)
        - Slippage formula: (fill_price/mid_at_entry - 1) * 10000
          — drift-inclusive proxy per design §9 (median-of-30 averages drift
          to ~0 in V1 approval-removal gate)
        - Gated by LIVE_USE_REAL_SIGNED_REQUESTS (R2-I4)
        """
        if not getattr(self._settings, "LIVE_USE_REAL_SIGNED_REQUESTS", False):
            raise NotImplementedError(
                "LIVE_USE_REAL_SIGNED_REQUESTS=False — emergency-revert posture"
            )
        if self._db is None:
            raise RuntimeError(
                "await_fill_confirmation requires db wired into BinanceSpotAdapter"
            )

        # R1-C4: resolve symbol ONCE before poll loop, cache, reuse
        cur = await self._db._conn.execute(
            "SELECT pair FROM live_trades WHERE client_order_id = ?",
            (client_order_id,),
        )
        pair_row = await cur.fetchone()
        if pair_row is None:
            raise RuntimeError(
                f"await_fill_confirmation: no live_trades row for cid={client_order_id}"
            )
        symbol = pair_row[0]

        backoff_schedule = [0.2, 0.5, 1.0, 2.0]
        deadline = asyncio.get_event_loop().time() + timeout_sec
        attempt = 0
        last_body: dict[str, Any] = {}

        while asyncio.get_event_loop().time() < deadline:
            try:
                body = await self._signed_get(
                    "/api/v3/order",
                    params={
                        "symbol": symbol,
                        "origClientOrderId": client_order_id,
                    },
                )
                last_body = body
                status_str = body.get("status", "")
                if status_str == "FILLED":
                    fill_price = self._extract_avg_fill_price(body)
                    # V1-I2: skip slippage write if we couldn't compute a
                    # reliable fill price; otherwise write under txn_lock.
                    if fill_price is not None:
                        await self._write_slippage_bps(client_order_id, fill_price)
                    return OrderConfirmation(
                        venue=self.venue_name,
                        venue_order_id=venue_order_id,
                        client_order_id=client_order_id,
                        status="filled",
                        filled_qty=float(body.get("executedQty", "0")),
                        fill_price=fill_price,
                        raw_response=body,
                    )
                if status_str == "PARTIALLY_FILLED":
                    fill_price = self._extract_avg_fill_price(body)
                    return OrderConfirmation(
                        venue=self.venue_name,
                        venue_order_id=venue_order_id,
                        client_order_id=client_order_id,
                        status="partial",
                        filled_qty=float(body.get("executedQty", "0")),
                        fill_price=fill_price,
                        raw_response=body,
                    )
                if status_str in ("CANCELED", "EXPIRED", "REJECTED"):
                    return OrderConfirmation(
                        venue=self.venue_name,
                        venue_order_id=venue_order_id,
                        client_order_id=client_order_id,
                        status="rejected",
                        filled_qty=None,
                        fill_price=None,
                        raw_response=body,
                    )
                # NEW / PENDING_CANCEL — keep polling
                wait = backoff_schedule[min(attempt, len(backoff_schedule) - 1)]
                attempt += 1
                await asyncio.sleep(wait)
            except (BinanceAuthError, VenueTransientError):
                # Surface to caller as timeout w/ raw_response for log inspection.
                # Engine layer handles by writing needs_manual_review (M1.5b).
                break

        return OrderConfirmation(
            venue=self.venue_name,
            venue_order_id=venue_order_id,
            client_order_id=client_order_id,
            status="timeout",
            filled_qty=None,
            fill_price=None,
            raw_response=last_body or None,
        )

    @staticmethod
    def _extract_avg_fill_price(body: dict[str, Any]) -> float | None:
        """Volume-weighted avg fill price from Binance fills array.

        SYNC helper (R1-C5 fix — original plan had async no-await mismatch).

        PR #86 V1-I2 fix: original fallback to body.avgPrice was wrong
        (avgPrice is FUTURES-only; spot /api/v3/order returns
        cummulativeQuoteQty + executedQty). New fallback computes
        cummulativeQuoteQty/executedQty when both > 0; returns None when
        we can't compute a reliable avg (caller skips slippage write).
        """
        fills = body.get("fills", []) or []
        total_qty = 0.0
        total_quote = 0.0
        for fill in fills:
            qty = float(fill.get("qty", "0"))
            price = float(fill.get("price", "0"))
            total_qty += qty
            total_quote += qty * price
        if total_qty > 0:
            return total_quote / total_qty
        # Fallback: cummulativeQuoteQty / executedQty (spot endpoint shape)
        try:
            cum_quote = float(body.get("cummulativeQuoteQty", "0") or "0")
            executed = float(body.get("executedQty", "0") or "0")
        except (TypeError, ValueError):
            return None
        if executed > 0 and cum_quote > 0:
            return cum_quote / executed
        return None

    async def _write_slippage_bps(
        self, client_order_id: str, fill_price: float
    ) -> None:
        """Compute (fill_price/mid_at_entry - 1) * 10000 and write to live_trades.

        Skips silently when mid_at_entry is NULL (place_order_request's
        depth fetch failed). Acquires _txn_lock per R1-I2.
        """
        if self._db is None or self._db._conn is None:
            return
        cur = await self._db._conn.execute(
            "SELECT mid_at_entry FROM live_trades WHERE client_order_id = ?",
            (client_order_id,),
        )
        row = await cur.fetchone()
        if row is None or row[0] is None:
            return
        try:
            mid = float(row[0])
        except (TypeError, ValueError):
            return
        if mid <= 0:
            return
        slippage_bps = round((fill_price / mid - 1.0) * 10000.0, 2)
        async with self._db._txn_lock:
            await self._db._conn.execute(
                "UPDATE live_trades SET fill_slippage_bps = ? "
                "WHERE client_order_id = ?",
                (slippage_bps, client_order_id),
            )
            await self._db._conn.commit()

    async def fetch_account_balance(self, asset: str = "USDT") -> float:
        """Return free balance in `asset` via signed GET /api/v3/account.

        M1.5a runtime body. Gated by `LIVE_USE_REAL_SIGNED_REQUESTS`
        Settings flag (R2-I4) — when False (default), raises
        NotImplementedError so balance_gate maps it to Gate 10
        `live_signed_disabled` reject_reason (R1-I1).
        """
        if not getattr(self._settings, "LIVE_USE_REAL_SIGNED_REQUESTS", False):
            raise NotImplementedError(
                "LIVE_USE_REAL_SIGNED_REQUESTS=False — emergency-revert posture"
            )
        body = await self._signed_get("/api/v3/account", params={})
        balances = body.get("balances", []) or []
        for entry in balances:
            if entry.get("asset", "").upper() == asset.upper():
                return float(entry.get("free", "0"))
        return 0.0

    async def close(self) -> None:
        """Close the underlying aiohttp session."""
        await self._session.close()
