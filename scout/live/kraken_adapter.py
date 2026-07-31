"""Kraken spot adapter — PR-K1 core (auth, preflight, balances, market rules).

Read-only surface for the supervised Kraken spot pilot. Order placement,
cancellation and fill reconciliation are deliberately NOT here — every
order-lifecycle method raises ``NotImplementedError("PR-K2 ...")`` so the
ABC stays satisfied without any codepath that can reach a Kraken order
endpoint.

Endpoints used
--------------
- ``GET  /0/public/AssetPairs`` — market rules + pair resolution
- ``GET  /0/public/Ticker``     — spot mid
- ``GET  /0/public/Depth``      — L2 orderbook snapshot
- ``POST /0/private/Balance``   — account balances (preflight step 1)
- ``POST /0/private/WithdrawMethods`` — withdrawal-capability probe
  (preflight step 2; a SUCCESS here is a hard reject)

Kraken API facts verified against current docs on 2026-07-31
------------------------------------------------------------
1. Auth (https://docs.kraken.com/api/docs/guides/spot-rest-auth):
   ``API-Sign = base64(HMAC-SHA512(base64decode(secret),
   uri_path + SHA256(nonce + post_data)))``, sent with the ``API-Key`` /
   ``API-Sign`` headers and a form-encoded POST body carrying ``nonce``.
   Kraken's published worked example is reproduced byte-for-byte in
   ``tests/test_live_kraken_signing.py``; the implementation lives in
   ``scout/live/kraken_signing.py``.
2. Envelope: EVERY response — success or failure — is
   ``{"error": [...], "result": {...}}`` and, critically, Kraken returns
   **HTTP 200 for application-level failures**. Empirically confirmed
   2026-07-31: an unauthenticated ``POST /0/private/Balance`` returns
   HTTP 200 with ``{"error":["EAPI:Invalid key"]}``, and an unknown pair
   returns HTTP 200 with ``{"error":["EQuery:Unknown asset pair"]}``.
   ``_request`` therefore treats a non-empty ``error`` list as failure and
   never infers success from the status line.
3. Error taxonomy (https://support.kraken.com/articles/360001491786-api-error-messages):
   errors are ``ECategory:Type[:extra]`` strings. Auth-class
   (``EAPI:Invalid key`` / ``Invalid signature`` / ``Invalid nonce``,
   ``EGeneral:Temporary lockout``) and ``EGeneral:Permission denied`` are
   never retried; ``EService:Unavailable`` / ``EService:Busy`` /
   ``EGeneral:Internal error`` are the retryable classes;
   ``EAPI:Rate limit exceeded`` gets its own class and is NOT retried in
   the request loop.
4. AssetPairs (https://docs.kraken.com/api/docs/rest-api/get-tradable-asset-pairs)
   returns per pair: ``altname``, ``wsname``, ``base``, ``quote``,
   ``pair_decimals``, ``lot_decimals``, ``cost_decimals``, ``ordermin``,
   ``costmin``, ``tick_size``, ``status`` (``online`` / ``cancel_only`` /
   ``post_only`` / ``limit_only`` / ``reduce_only``) and margin/fee fields.
5. AddOrder (https://docs.kraken.com/api/docs/rest-api/add-order) documents
   its ``pair`` argument as "Asset pair id or altname" and its example uses
   the altname (``XBTUSD``). ``venue_pair`` is therefore the **altname**,
   not the AssetPairs dict key (``XXBTZUSD``) and not the wsname.
6. NOT VERIFIABLE: no Kraken REST endpoint reports the permission scopes
   granted to an API key — the docs index, the auth guide and the private
   endpoint list contain no key-introspection method. That absence is why
   ``preflight_credentials_check`` probes a withdrawal-scoped endpoint and
   requires an explicit permission denial, rather than reading a scope list.

Empirical checks run against the live public API on 2026-07-31 (all 1428
tradable pairs): ``tick_size`` equalled ``10**-pair_decimals`` for every
pair, and only ``online`` / ``post_only`` statuses were present. The
legacy X/Z asset-code prefixes are NOT strippable by rule — 9 live assets
(``XAUT``, ``XION``, ``XNAP``, ``XTER``, ``ZAMA``, ``ZBCN``, ``ZETA``,
``ZEUS``, ``ZORA``) start with X or Z, are 4 characters long, and are their
own altname, so a naive strip would corrupt them. ``_KRAKEN_LEGACY_ASSETS``
below is the exact closed set from ``GET /0/public/Assets``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

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
from scout.live.kraken_signing import KrakenNonce, sign_kraken_request
from scout.live.types import Depth, DepthLevel

log = structlog.get_logger(__name__)


# ----------------------------------------------------------------------
# Typed exceptions — mirrors binance_adapter.py's flat, in-module shape so
# callers discriminate on type rather than on the error string.
# ----------------------------------------------------------------------
class KrakenAPIError(Exception):
    """Non-empty Kraken ``error`` list that isn't a more specific class.

    Terminal — the request was understood and refused. Never retried.
    """


class KrakenAuthError(KrakenAPIError):
    """Credential-class failure. NEVER retry — retrying burns nonces and
    can trip ``EGeneral:Temporary lockout``.

    Covers ``EAPI:Invalid key`` / ``Invalid signature`` / ``Invalid nonce``,
    ``EGeneral:Temporary lockout``, and a missing/blank KRAKEN_API_* config.
    """


class KrakenPermissionError(KrakenAPIError):
    """``EGeneral:Permission denied`` — the key lacks the endpoint's scope.

    Distinct from ``KrakenAuthError`` because the operator action differs
    (widen the key's permissions vs rotate the key), and because the
    withdrawal preflight REQUIRES this exact outcome to pass.
    """


class KrakenRateLimitError(RateLimitError):
    """``EAPI:Rate limit exceeded`` / ``EOrder:Rate limit exceeded`` / HTTP 429.

    Subclasses the shared ``RateLimitError`` so existing engine-side
    handling keeps working. Raised immediately — the request loop does NOT
    retry it, because Kraken's counter decays on a timer and an in-loop
    retry just deepens the penalty.
    """


class KrakenWithdrawalCapabilityError(Exception):
    """The API key was not PROVEN to lack withdrawal capability.

    Hard reject — the adapter refuses to proceed. Raised when the
    withdrawal-scoped probe succeeds (key CAN withdraw) and, fail-closed,
    whenever the probe's outcome is anything other than an explicit
    ``EGeneral:Permission denied``.
    """


# ----------------------------------------------------------------------
# Kraken naming
# ----------------------------------------------------------------------
# Exact legacy-prefixed asset codes, from GET /0/public/Assets on
# 2026-07-31 (the complete set where key != altname). An explicit map, NOT
# a strip-the-X/Z rule — see the module docstring for the 9 assets a rule
# would corrupt.
_KRAKEN_LEGACY_ASSETS: dict[str, str] = {
    "KFEE": "FEE",
    "XETC": "ETC",
    "XETH": "ETH",
    "XLTC": "LTC",
    "XMLN": "MLN",
    "XREP": "REP",
    "XXBT": "XBT",
    "XXDG": "XDG",
    "XXLM": "XLM",
    "XXMR": "XMR",
    "XXRP": "XRP",
    "XZEC": "ZEC",
    "ZARS": "ARS",
    "ZAUD": "AUD",
    "ZCAD": "CAD",
    "ZCLP": "CLP",
    "ZCOP": "COP",
    "ZDKK": "DKK",
    "ZEUR": "EUR",
    "ZGBP": "GBP",
    "ZGEL": "GEL",
    "ZGHS": "GHS",
    "ZJPY": "JPY",
    "ZLKR": "LKR",
    "ZMXN": "MXN",
    "ZPLN": "PLN",
    "ZSEK": "SEK",
    "ZUGX": "UGX",
    "ZUSD": "USD",
    "ZVND": "VND",
    "ZXOF": "XOF",
}

# Kraken's own tickers that differ from the canonical ticker the rest of
# the pipeline uses. Both directions kept explicit rather than inverted at
# import time so a future one-way-only alias stays expressible.
_VENUE_TO_CANONICAL: dict[str, str] = {"XBT": "BTC", "XDG": "DOGE"}
_CANONICAL_TO_VENUE: dict[str, str] = {"BTC": "XBT", "DOGE": "XDG"}

# Quote preference for the pilot. USD first — this is a USD-quoted spot
# pilot on Kraken; USDT only as a fallback for coins Kraken lists without a
# USD book. Order is load-bearing: resolve_pair_for_symbol returns the FIRST
# match, so a coin with both books always resolves to the USD one.
_QUOTE_PREFERENCE: tuple[str, ...] = ("USD", "USDT")

# Only a fully-open book is tradable for a market-order pilot. post_only /
# limit_only / cancel_only / reduce_only all break market entry or exit, so
# resolution fails closed rather than resolving into a book we cannot exit.
_TRADABLE_STATUS = "online"


def normalize_kraken_asset(code: str) -> str:
    """Kraken asset code → canonical ticker (``ZUSD``→USD, ``XXBT``→BTC).

    Two hops: legacy-prefix code → Kraken altname → canonical ticker.
    Unknown codes pass through upper-cased, which is correct for the ~793
    Kraken assets whose code already IS their altname.

    Suffixed balance keys (``XBT.S`` staked, ``.F``/``.B``/``.M``/``.T``)
    are NOT unwrapped — they are separate, non-spot-tradable balances and
    ``fetch_account_balance`` skips them outright.
    """
    upper = code.upper()
    altname = _KRAKEN_LEGACY_ASSETS.get(upper, upper)
    return _VENUE_TO_CANONICAL.get(altname, altname)


# Error-string prefixes, matched with startswith because Kraken appends
# free-form detail (e.g. "EGeneral:Invalid arguments:display-volume").
_AUTH_ERROR_PREFIXES: tuple[str, ...] = (
    "EAPI:Invalid key",
    "EAPI:Invalid signature",
    "EAPI:Invalid nonce",
    "EGeneral:Temporary lockout",
    "ESession:Invalid session",
)
_PERMISSION_ERROR_PREFIXES: tuple[str, ...] = ("EGeneral:Permission denied",)
_RATE_LIMIT_ERROR_PREFIXES: tuple[str, ...] = (
    "EAPI:Rate limit exceeded",
    "EOrder:Rate limit exceeded",
)
_TRANSIENT_ERROR_PREFIXES: tuple[str, ...] = (
    "EService:Unavailable",
    "EService:Busy",
    "EGeneral:Internal error",
)

# Sentinel key for a tolerated application error — mirrors the Binance
# adapter's ``{"__code": -1121}`` unknown-symbol sentinel.
_TOLERATED_KEY = "__kraken_error"

_BACKOFFS: tuple[float, ...] = (1.0, 2.0, 4.0)

# Kraken caps Depth's `count` at 500.
_MAX_DEPTH_COUNT = 500


def _select_error(errors: list[str], prefixes: tuple[str, ...]) -> str | None:
    """Return the first error in ``errors`` matching any of ``prefixes``."""
    for err in errors:
        for prefix in prefixes:
            if err.startswith(prefix):
                return err
    return None


class KrakenSpotAdapter(ExchangeAdapter):
    """Spot-venue adapter for Kraken — PR-K1 read-only surface.

    Parameters
    ----------
    settings:
        Project ``Settings``. Supplies ``KRAKEN_API_KEY`` /
        ``KRAKEN_API_SECRET`` / ``KRAKEN_API_BASE_URL`` /
        ``KRAKEN_HTTP_TIMEOUT_SEC``.
    db:
        Optional ``scout.db.Database``. Unused in PR-K1 (no ledger writes on
        the read-only surface) — accepted so the constructor signature
        matches ``BinanceSpotAdapter`` and PR-K2 can wire idempotency without
        changing every call site.

    Note on ``LIVE_USE_REAL_SIGNED_REQUESTS``: that flag gates Binance's
    *order* runtime bodies as an emergency-revert lever. PR-K1 exposes no
    order path, and its two private calls (Balance, WithdrawMethods) are
    read-only — gating them would disable the withdrawal preflight, i.e.
    disable the safety check itself. PR-K2 gates the order methods it adds.
    """

    venue_name: str = "kraken"

    def __init__(self, settings: Settings, db: Any | None = None) -> None:
        self._settings = settings
        self._db = db
        self._base_url = settings.KRAKEN_API_BASE_URL.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._nonce = KrakenNonce()

        # Swappable in tests to skip real backoff delays.
        self._retry_sleep = asyncio.sleep

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------
    def _api_key(self) -> str:
        """Extract the API key from SecretStr; raise if unconfigured."""
        return self._secret_value("KRAKEN_API_KEY")

    def _api_secret(self) -> str:
        """Extract the API secret from SecretStr; raise if unconfigured."""
        return self._secret_value("KRAKEN_API_SECRET")

    def _secret_value(self, field: str) -> str:
        raw = getattr(self._settings, field, None)
        if raw is None:
            raise KrakenAuthError(f"{field} not configured")
        value = raw.get_secret_value() if hasattr(raw, "get_secret_value") else str(raw)
        if not value:
            raise KrakenAuthError(f"{field} not configured")
        return value

    # ------------------------------------------------------------------
    # HTTP core
    # ------------------------------------------------------------------
    def _get_session(self) -> aiohttp.ClientSession:
        """Lazily create the aiohttp session on the running event loop.

        Deferred (unlike Binance's eager constructor) so constructing an
        adapter outside a loop — CLI wiring, ABC-conformance tests — does
        not bind a session to the wrong loop or leak one when the object is
        never used.
        """
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(
                total=float(self._settings.KRAKEN_HTTP_TIMEOUT_SEC)
            )
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _request(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        signed: bool = False,
        tolerate_errors: tuple[str, ...] = (),
        retry_transient: bool = True,
    ) -> dict[str, Any]:
        """Central Kraken HTTP — shared by public GET and private POST.

        Args:
            path: Full URI path including the version prefix, e.g.
                ``/0/public/AssetPairs``. Also the string that gets signed.
            params: Query params (public GET only).
            data: Form fields (private POST only). ``nonce`` is added here,
                per attempt.
            signed: Sign + send ``API-Key``/``API-Sign``.
            tolerate_errors: Error prefixes to return as
                ``{"__kraken_error": "<full error string>"}`` instead of
                raising — the caller's "expected, non-exceptional failure"
                set (e.g. ``EQuery:Unknown asset pair``).
            retry_transient: Retry network errors / 5xx / ``EService:*``.
                PR-K2 MUST pass ``False`` for order submission — a retried
                non-idempotent POST is a double-order risk, and the nonce is
                regenerated per attempt so Kraken cannot dedup it.

        Retry taxonomy:
        - network error / timeout      → retry, then ``VenueTransientError``
        - HTTP 5xx (incl. Cloudflare 520-529) → retry, then ``VenueTransientError``
        - HTTP 429                     → ``KrakenRateLimitError`` (no retry)
        - HTTP 200 + empty error list  → return ``result``
        - ``EService:*`` / internal    → retry, then ``VenueTransientError``
        - auth-class errors            → ``KrakenAuthError`` (NEVER retried)
        - ``EGeneral:Permission denied``→ ``KrakenPermissionError`` (no retry)
        - rate-limit errors            → ``KrakenRateLimitError`` (no retry)
        - any other non-empty error    → ``KrakenAPIError`` (no retry)
        """
        url = f"{self._base_url}{path}"
        session = self._get_session()
        last_exc: Exception | None = None

        for attempt in range(len(_BACKOFFS) + 1):
            try:
                if signed:
                    body = dict(data or {})
                    # Fresh nonce per attempt: Kraken requires strictly
                    # increasing nonces, so a replayed one is rejected.
                    nonce = await self._nonce.next()
                    body["nonce"] = nonce
                    post_data = urlencode(body)
                    headers = {
                        "API-Key": self._api_key(),
                        "API-Sign": sign_kraken_request(
                            path, post_data, nonce, self._api_secret()
                        ),
                        "Content-Type": "application/x-www-form-urlencoded",
                    }
                    cm = session.post(url, data=post_data, headers=headers)
                else:
                    cm = session.get(url, params=params)

                async with cm as resp:
                    if resp.status == 429:
                        raise KrakenRateLimitError(f"kraken 429 on {path}")

                    if 500 <= resp.status < 600:
                        # Includes Cloudflare's 520-529 origin-error range.
                        last_exc = VenueTransientError(
                            f"kraken {resp.status} on {path} attempt={attempt + 1}"
                        )
                        if retry_transient and attempt < len(_BACKOFFS):
                            await self._retry_sleep(_BACKOFFS[attempt])
                            continue
                        raise last_exc

                    if resp.status >= 400:
                        resp.raise_for_status()

                    ctype = resp.headers.get("Content-Type", "")
                    if "html" in ctype.lower():
                        # A CDN interstitial served as HTTP 200 — transient,
                        # and json() would raise something far less legible.
                        last_exc = VenueTransientError(
                            f"kraken {path}: non-JSON (HTML) response"
                        )
                        if retry_transient and attempt < len(_BACKOFFS):
                            await self._retry_sleep(_BACKOFFS[attempt])
                            continue
                        raise last_exc

                    payload = await resp.json(content_type=None)

                if not isinstance(payload, dict):
                    raise KrakenAPIError(
                        f"kraken {path}: unexpected response type "
                        f"{type(payload).__name__}"
                    )

                errors = [str(e) for e in (payload.get("error") or [])]
                if not errors:
                    result = payload.get("result")
                    # Some endpoints legitimately return an empty result
                    # (e.g. Balance on a funded-but-empty account).
                    return result if isinstance(result, dict) else {}

                # HTTP 200 with a non-empty error list IS a failure.
                tolerated = _select_error(errors, tolerate_errors)
                if tolerated is not None:
                    return {_TOLERATED_KEY: tolerated}

                transient = _select_error(errors, _TRANSIENT_ERROR_PREFIXES)
                if transient is not None:
                    last_exc = VenueTransientError(
                        f"kraken {path}: {transient} attempt={attempt + 1}"
                    )
                    if retry_transient and attempt < len(_BACKOFFS):
                        await self._retry_sleep(_BACKOFFS[attempt])
                        continue
                    raise last_exc

                self._raise_for_errors(path, errors)

            except (aiohttp.ClientConnectorError, asyncio.TimeoutError) as exc:
                last_exc = VenueTransientError(f"network error: {type(exc).__name__}")
                if retry_transient and attempt < len(_BACKOFFS):
                    await self._retry_sleep(_BACKOFFS[attempt])
                    continue
                raise last_exc

        # Load-bearing guard, mirroring binance_adapter._request: an `assert`
        # is stripped under `python -O`, and `raise None` would mask the
        # control-flow bug behind a TypeError.
        if last_exc is None:
            raise RuntimeError(
                "kraken_adapter._request: retry loop exited without raising "
                "or returning — _BACKOFFS likely empty"
            )
        raise last_exc

    @staticmethod
    def _raise_for_errors(path: str, errors: list[str]) -> None:
        """Map a non-empty, non-tolerated, non-transient error list to a
        typed exception. Always raises."""
        auth = _select_error(errors, _AUTH_ERROR_PREFIXES)
        if auth is not None:
            raise KrakenAuthError(f"kraken {path}: {auth}")

        permission = _select_error(errors, _PERMISSION_ERROR_PREFIXES)
        if permission is not None:
            raise KrakenPermissionError(f"kraken {path}: {permission}")

        rate_limited = _select_error(errors, _RATE_LIMIT_ERROR_PREFIXES)
        if rate_limited is not None:
            raise KrakenRateLimitError(f"kraken {path}: {rate_limited}")

        raise KrakenAPIError(f"kraken {path}: {'; '.join(errors)}")

    async def _public_get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        tolerate_errors: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Unsigned GET against ``/0/public/*``."""
        return await self._request(
            path, params=params, signed=False, tolerate_errors=tolerate_errors
        )

    async def _private_post(
        self,
        path: str,
        data: dict[str, Any] | None = None,
        *,
        tolerate_errors: tuple[str, ...] = (),
        retry_transient: bool = True,
    ) -> dict[str, Any]:
        """Signed, form-encoded POST against ``/0/private/*``."""
        return await self._request(
            path,
            data=data,
            signed=True,
            tolerate_errors=tolerate_errors,
            retry_transient=retry_transient,
        )

    # ------------------------------------------------------------------
    # Market rules / resolution
    # ------------------------------------------------------------------
    async def fetch_exchange_info_row(self, pair: str) -> dict | None:
        """Return the raw ``AssetPairs`` row for ``pair``, or ``None``.

        ``pair`` may be an altname (``XBTUSD``) or an AssetPairs key
        (``XXBTZUSD``); Kraken resolves either. ``None`` means Kraken
        answered ``EQuery:Unknown asset pair`` — terminal for this symbol,
        not an error.
        """
        body = await self._public_get(
            "/0/public/AssetPairs",
            params={"pair": pair},
            tolerate_errors=("EQuery:Unknown asset pair",),
        )
        if _TOLERATED_KEY in body:
            return None
        for row in body.values():
            if isinstance(row, dict):
                return row
        return None

    def _pair_candidates(self, symbol: str, quote: str) -> list[str]:
        """Altname candidates for ``symbol`` against ``quote``, best first.

        Kraken's own ticker leads (``XBTUSD``) because that is the published
        altname; the canonical spelling (``BTCUSD``) follows as a fallback
        for symbols outside the alias table.
        """
        canonical = symbol.upper()
        venue_base = _CANONICAL_TO_VENUE.get(canonical, canonical)
        candidates = [f"{venue_base}{quote}", f"{canonical}{quote}"]
        # dict.fromkeys: dedupe while preserving preference order.
        return list(dict.fromkeys(candidates))

    async def resolve_pair_for_symbol(self, symbol: str) -> str | None:
        """Resolve a canonical ticker to a tradable Kraken altname.

        Preference order is USD then USDT (see ``_QUOTE_PREFERENCE``): the
        pilot trades a USD-quoted book, and USDT is accepted only when
        Kraken lists no USD book for the symbol.

        Fails closed on anything but ``status == "online"`` — a
        ``post_only`` / ``limit_only`` / ``cancel_only`` / ``reduce_only``
        book cannot be entered or exited with a market order, and resolving
        into one would produce a position we could not close.
        """
        for quote in _QUOTE_PREFERENCE:
            for candidate in self._pair_candidates(symbol, quote):
                row = await self.fetch_exchange_info_row(candidate)
                if row is None:
                    continue
                if row.get("status") != _TRADABLE_STATUS:
                    log.info(
                        "kraken_pair_not_tradable",
                        symbol=symbol.upper(),
                        candidate=candidate,
                        status=row.get("status"),
                    )
                    continue
                if normalize_kraken_asset(str(row.get("quote", ""))) != quote:
                    continue
                altname = row.get("altname")
                if not altname:
                    continue
                # AddOrder takes "asset pair id or altname" — altname is the
                # documented form and the one its example uses.
                return str(altname)
        return None

    async def fetch_venue_metadata(self, canonical: str) -> VenueMetadata | None:
        """Resolve ``canonical`` on Kraken spot and wrap as VenueMetadata.

        - ``venue_pair`` is the altname (what AddOrder accepts).
        - ``tick_size`` prefers the explicit ``tick_size`` field, falling
          back to ``10**-pair_decimals``. Both agreed for all 1428 live
          pairs on 2026-07-31; the explicit field is authoritative if Kraken
          ever widens a tick beyond the price precision.
        - ``lot_size`` is ``10**-lot_decimals`` (Kraken publishes volume
          precision as a decimal count, not a step).
        - ``min_size`` is ``ordermin`` (base units) and ``min_cost`` is
          ``costmin`` (quote units). Kraken enforces BOTH — an order over
          ordermin but under costmin is rejected — so the sizing layer in
          PR-K2/K3 must honour min_cost as well as min_size.

        Malformed numeric fields degrade to ``None`` per field rather than
        failing resolution, matching the Binance adapter's best-effort
        filter extraction.
        """
        pair = await self.resolve_pair_for_symbol(canonical)
        if pair is None:
            return None
        row = await self.fetch_exchange_info_row(pair)
        if row is None:
            return None

        tick_size = _to_float(row.get("tick_size"))
        if tick_size is None:
            tick_size = _decimals_to_step(row.get("pair_decimals"))

        return VenueMetadata(
            venue=self.venue_name,
            canonical=canonical.upper(),
            venue_pair=str(row.get("altname", pair)),
            quote=normalize_kraken_asset(str(row.get("quote", ""))),
            asset_class="spot",
            min_size=_to_float(row.get("ordermin")),
            tick_size=tick_size,
            lot_size=_decimals_to_step(row.get("lot_decimals")),
            min_cost=_to_float(row.get("costmin")),
        )

    # ------------------------------------------------------------------
    # Prices / depth
    # ------------------------------------------------------------------
    async def fetch_price(self, pair: str) -> Decimal:
        """Spot mid for ``pair`` via ``/0/public/Ticker``.

        Mid is ``(best bid + best ask) / 2`` from the ticker's ``b``/``a``
        arrays (element 0 is the price). Falls back to the last trade price
        (``c[0]``) when either side is missing — a one-sided book still
        gives a usable reference price for sizing.
        """
        body = await self._public_get("/0/public/Ticker", params={"pair": pair})
        row = next((v for v in body.values() if isinstance(v, dict)), None)
        if row is None:
            raise KrakenAPIError(f"kraken Ticker returned no row for pair={pair!r}")

        bid = _first_decimal(row.get("b"))
        ask = _first_decimal(row.get("a"))
        if bid is not None and ask is not None:
            return (bid + ask) / Decimal(2)

        last = _first_decimal(row.get("c"))
        if last is not None:
            return last
        raise KrakenAPIError(f"kraken Ticker row for pair={pair!r} has no usable price")

    async def fetch_depth(self, pair: str, limit: int = 100) -> Depth:
        """L2 orderbook snapshot via ``/0/public/Depth``.

        Kraken returns entries as ``[price, volume, timestamp]`` with bids
        already descending and asks ascending; both are re-sorted here so
        the ``Depth`` contract holds regardless of what the venue sends.
        """
        count = max(1, min(int(limit), _MAX_DEPTH_COUNT))
        body = await self._public_get(
            "/0/public/Depth", params={"pair": pair, "count": count}
        )
        row = next((v for v in body.values() if isinstance(v, dict)), None)
        if row is None:
            raise KrakenAPIError(f"kraken Depth returned no row for pair={pair!r}")

        bids = tuple(
            sorted(
                _depth_levels(row.get("bids")), key=lambda lv: lv.price, reverse=True
            )
        )
        asks = tuple(sorted(_depth_levels(row.get("asks")), key=lambda lv: lv.price))
        mid = (
            (bids[0].price + asks[0].price) / Decimal(2)
            if bids and asks
            else Decimal(0)
        )
        return Depth(
            pair=pair,
            bids=bids,
            asks=asks,
            mid=mid,
            fetched_at=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------
    async def fetch_account_balance(self, asset: str = "USD") -> float:
        """Free spot balance in ``asset`` via ``POST /0/private/Balance``.

        Defaults to USD, not the ABC's USDT: this is a USD-quoted Kraken
        pilot. Every caller in-tree passes the asset by keyword, so the
        narrowed default cannot silently change a Binance call.

        Balance keys are normalized (``ZUSD``→USD, ``XXBT``→BTC). Suffixed
        keys (``XBT.S`` staked, ``.F``/``.B``/``.M``/``.T`` earn and
        tokenized variants) are SKIPPED — they are not spot-tradable, and
        counting them would let a balance gate approve an order the account
        cannot actually fund. Returns ``0.0`` when the asset is absent.
        """
        body = await self._private_post("/0/private/Balance")
        target = asset.upper()
        for code, amount in body.items():
            if "." in code:
                continue
            if normalize_kraken_asset(code) != target:
                continue
            value = _to_float(amount)
            return value if value is not None else 0.0
        return 0.0

    async def preflight_credentials_check(self) -> dict[str, Any]:
        """Verify the API key authenticates AND cannot withdraw funds.

        Two checks, both of which must pass before the adapter is considered
        usable for the supervised pilot:

        1. ``POST /0/private/Balance`` succeeds → the key/secret/nonce chain
           works and has query permission.
        2. ``POST /0/private/WithdrawMethods`` is REFUSED with
           ``EGeneral:Permission denied`` → the key demonstrably lacks
           withdrawal scope.

        Check 2 fails closed. Kraken exposes no endpoint that reports a
        key's permission scopes (see module docstring, fact 6), so an
        explicit denial is the only positive evidence available. A success
        means the key CAN withdraw; any other outcome — a different error, a
        transient failure, a network drop — means we did not PROVE it
        cannot. Both raise ``KrakenWithdrawalCapabilityError``.

        Returns:
            ``{"auth_ok": True, "withdrawal_excluded": True,
            "checked_at": "<ISO-8601 UTC>"}`` — a structured evidence record
            for the pilot's authorization log.

        Raises:
            KrakenAuthError: credentials missing or rejected (check 1).
            KrakenWithdrawalCapabilityError: withdrawal exclusion not proven.
        """
        try:
            await self._private_post("/0/private/Balance")
        except Exception as exc:
            log.warning(
                "kraken_preflight_failed",
                stage="auth",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise

        try:
            await self._private_post("/0/private/WithdrawMethods")
        except KrakenPermissionError:
            # The one passing outcome: the key is refused withdrawal scope.
            withdrawal_excluded = True
        except Exception as exc:
            log.warning(
                "kraken_preflight_failed",
                stage="withdrawal_probe",
                outcome="inconclusive",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise KrakenWithdrawalCapabilityError(
                "withdrawal-capability probe was inconclusive "
                f"({type(exc).__name__}: {exc}) — refusing to proceed; only an "
                "explicit EGeneral:Permission denied proves the key cannot withdraw"
            ) from exc
        else:
            log.warning(
                "kraken_preflight_failed",
                stage="withdrawal_probe",
                outcome="permitted",
            )
            raise KrakenWithdrawalCapabilityError(
                "API key CAN reach withdrawal-scoped endpoints — reissue the key "
                "without 'Funds permissions - Withdraw' before running the pilot"
            )

        checked_at = datetime.now(timezone.utc).isoformat()
        log.info(
            "kraken_preflight_ok",
            venue=self.venue_name,
            auth_ok=True,
            withdrawal_excluded=withdrawal_excluded,
            checked_at=checked_at,
        )
        return {
            "auth_ok": True,
            "withdrawal_excluded": withdrawal_excluded,
            "checked_at": checked_at,
        }

    # ------------------------------------------------------------------
    # Order lifecycle — PR-K2. Concrete raising overrides keep the ABC
    # satisfied without creating any path that can reach a Kraken order
    # endpoint from this PR.
    # ------------------------------------------------------------------
    async def send_order(self, *, pair: str, side: str, size_usd: Decimal) -> dict:
        raise NotImplementedError("PR-K2 — Kraken order lifecycle not wired in PR-K1")

    async def place_order_request(self, request: OrderRequest) -> str:
        raise NotImplementedError("PR-K2 — Kraken order lifecycle not wired in PR-K1")

    async def await_fill_confirmation(
        self, *, venue_order_id: str, client_order_id: str, timeout_sec: float
    ) -> OrderConfirmation:
        raise NotImplementedError("PR-K2 — Kraken order lifecycle not wired in PR-K1")

    async def place_exit_order(
        self,
        *,
        pair: str,
        base_qty: Decimal,
        client_order_id: str,
        timeout_sec: float,
    ) -> OrderConfirmation:
        raise NotImplementedError("PR-K2 — Kraken order lifecycle not wired in PR-K1")

    async def fetch_order_by_client_id(
        self, *, pair: str, client_order_id: str
    ) -> OrderConfirmation | None:
        raise NotImplementedError("PR-K2 — Kraken order lifecycle not wired in PR-K1")

    async def close(self) -> None:
        """Close the underlying aiohttp session, if one was created."""
        if self._session is not None and not self._session.closed:
            await self._session.close()


# ----------------------------------------------------------------------
# Parsing helpers — Kraken sends every number as a string.
# ----------------------------------------------------------------------
def _to_float(value: Any) -> float | None:
    """Best-effort float; ``None`` on missing or malformed input."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _decimals_to_step(decimals: Any) -> float | None:
    """Convert a Kraken decimal-count to a step size (``8`` → ``1e-8``)."""
    if decimals is None:
        return None
    try:
        return float(Decimal(1).scaleb(-int(decimals)))
    except (TypeError, ValueError, InvalidOperation):
        return None


def _first_decimal(field: Any) -> Decimal | None:
    """First element of a Kraken price array (``["30300.1", "1", "1.0"]``)."""
    if not isinstance(field, (list, tuple)) or not field:
        return None
    try:
        return Decimal(str(field[0]))
    except (TypeError, ValueError, InvalidOperation):
        return None


def _depth_levels(entries: Any) -> list[DepthLevel]:
    """Parse ``[[price, volume, timestamp], ...]`` into DepthLevels.

    Malformed entries are dropped rather than aborting the snapshot — a
    single bad level shouldn't blind the depth gate to the rest of the book.
    """
    levels: list[DepthLevel] = []
    for entry in entries or []:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        try:
            levels.append(
                DepthLevel(price=Decimal(str(entry[0])), qty=Decimal(str(entry[1])))
            )
        except (TypeError, ValueError, InvalidOperation):
            continue
    return levels
