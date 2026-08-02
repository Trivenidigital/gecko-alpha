"""Kraken spot adapter — PR-K1 core + PR-K2 order lifecycle.

Surface for the supervised Kraken spot pilot: auth, preflight, balances and
market rules (PR-K1) plus limit-order placement, cancellation, lookup, fill
reconciliation and ambiguous-submission resolution (PR-K2).

The pilot lane is LIMIT-ONLY. There is deliberately no market-order path:
``send_order`` / ``place_order_request`` / ``place_exit_order`` all raise, and
``place_limit_order`` is the one method that can reach ``AddOrder``.

Endpoints used
--------------
- ``GET  /0/public/AssetPairs`` — market rules + pair resolution
- ``GET  /0/public/Ticker``     — spot mid
- ``GET  /0/public/Depth``      — L2 orderbook snapshot
- ``POST /0/private/BalanceEx`` — available balance, net of order holds
- ``POST /0/private/Balance``   — auth liveness check (preflight step 1)
- ``POST /0/private/TradeVolume`` — THIS account's maker/taker fee tier
  (PR-K4; the public AssetPairs schedule is tier-zero and not what a
  volume-carrying account actually pays)
- ``POST /0/private/WithdrawMethods`` + ``POST /0/private/WithdrawStatus``
  — withdrawal-capability probes (preflight step 2; a SUCCESS on either is
  a hard reject)
- ``POST /0/private/AddOrder``     — limit-order placement (PR-K2)
- ``POST /0/private/CancelOrder``  — cancel by txid or cl_ord_id (PR-K2)
- ``POST /0/private/OpenOrders`` + ``POST /0/private/ClosedOrders``
  — cl_ord_id lookup + ambiguous-submission resolution (PR-K2)
- ``POST /0/private/QueryOrders``  — order state by txid (PR-K2)
- ``POST /0/private/QueryTrades``  — per-fill price/qty/fee (PR-K2)

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
   ``preflight_credentials_check`` probes withdrawal-scoped endpoints and
   requires an explicit permission denial, rather than reading a scope list.
7. BalanceEx (https://docs.kraken.com/api/docs/rest-api/get-extended-balance)
   returns per asset ``balance`` / ``hold_trade`` (+ ``credit`` /
   ``credit_used`` on credit-line accounts) and documents
   ``available = balance + credit - credit_used - hold_trade``. Plain
   ``Balance`` is NOT net of open-order holds, so ``fetch_account_balance``
   uses BalanceEx. Note BalanceEx sends these as JSON numbers whereas most
   Kraken endpoints send numeric strings; ``_to_float`` accepts both.
8. WithdrawStatus
   (https://docs.kraken.com/api/docs/rest-api/get-status-recent-withdrawals)
   requires "Funds permissions - Withdraw" **OR** "Data - Query ledger
   entries" — the two withdrawal probes are NOT gated identically. See
   ``preflight_credentials_check`` for the false-reject this can cause.

PR-K2 order-lifecycle facts, verified against current docs on 2026-07-31
--------------------------------------------------------------------------
9.  AddOrder (https://docs.kraken.com/api/docs/rest-api/add-order) request
    params are ``pair`` / ``type`` (buy|sell) / ``ordertype`` (limit) /
    ``price`` / ``volume``, all sent as **strings** in the form-encoded
    body. Response is ``result.descr`` (``{order, close}``) plus
    ``result.txid`` (an ARRAY of transaction ids).
10. ``cl_ord_id`` is the client-order-id field: "alphanumeric client order
    identifier which uniquely identifies an open order for each client". It
    is **mutually exclusive with ``userref``** and the docs accept exactly
    three forms — long UUID (``6d1b345e-2821-40e2-ad83-4ecb18a06876``),
    short UUID (32 hex, no dashes), or **free text of at most 18 ASCII
    characters** (``arb-20240509-00010``). ``_validate_cl_ord_id`` enforces
    that set locally, BEFORE any HTTP, because a rejected order id is a
    round trip we can avoid.

    *** CONSEQUENCE FOR CALLERS: ``scout.live.idempotency.make_client_order_id``
    emits up to 28 characters (Binance's limit), which does NOT fit Kraken's
    18-character free-text form. The Kraken pilot runner must mint its own
    Kraken-shaped client order id. ``_validate_cl_ord_id`` rejects an
    over-long id rather than letting Kraken decide. ***
11. **NOT VERIFIABLE — duplicate ``cl_ord_id`` semantics.** The AddOrder docs
    describe the field as uniquely identifying an open order but say NOTHING
    about whether a duplicate is rejected, deduplicated, or silently
    accepted as a second order, and no ``EOrder:*`` entry in the published
    error table (https://support.kraken.com/articles/360001491786-api-error-messages)
    covers it. **The safety story therefore does NOT rest on Kraken
    deduplicating a resent cl_ord_id.** A resend must never be attempted on
    an ambiguous submission — ``place_limit_order`` raises
    ``KrakenAmbiguousSubmissionError`` and the caller resolves via
    ``resolve_order_submission``. ``_raise_for_errors`` maps any error
    string mentioning ``cl_ord_id`` to ``KrakenClientOrderIdError`` as a
    best-effort classification; since malformed ids are already rejected
    locally, such an error in practice means "duplicate".
12. ``validate``: "If set to ``true`` the order will be validated only, it
    will not trade" (default false). This is a Kraken-side dry run — the
    order is parsed and checked but never placed, and the reply carries
    ``result.descr`` with NO ``result.txid``. It is the mechanism behind
    ``place_limit_order(validate_only=True)`` and is therefore allowed to
    run with ``LIVE_USE_REAL_SIGNED_REQUESTS=False`` (see that method).
13. Lookup asymmetry — this one shapes the whole reconciliation design:
    - ``OpenOrders`` (https://docs.kraken.com/api/docs/rest-api/get-open-orders)
      and ``ClosedOrders`` (.../get-closed-orders) BOTH accept ``cl_ord_id``
      as a filter ("Restrict results to given client order id") and return
      ``result.open`` / ``result.closed`` as maps keyed by txid, with
      ``cl_ord_id`` echoed on each order row.
    - ``QueryOrders`` (.../get-orders-info) does **NOT**. Its required
      params are ``nonce`` and ``txid``; ``cl_ord_id`` appears only in the
      RESPONSE. An order can therefore only be found by client id through
      OpenOrders/ClosedOrders, which is why ``resolve_order_submission``
      probes exactly those two and not three.
    Order ``status`` is one of ``pending`` / ``open`` / ``closed`` /
    ``canceled`` / ``expired``, alongside ``vol`` / ``vol_exec`` / ``price``
    / ``cost`` / ``fee`` / ``reason`` / ``trades``.
14. ``CancelOrder`` (.../cancel-order) documents ``txid`` as "Kraken order
    identifier (txid) or user reference (userref)" and carries ``cl_ord_id``
    as a SEPARATE optional param — so a cancel keyed by client id must send
    ``cl_ord_id=``, not ``txid=``. Response is ``result.count`` (orders
    cancelled) + ``result.pending``. **NOT VERIFIABLE:** the docs specify no
    error for an unknown or already-closed order, so
    ``_CANCEL_ALREADY_GONE_ERROR_PREFIXES`` is a best-effort mapping of the
    observed strings; anything unmatched still raises.
15. ``QueryTrades`` (.../get-trades-info) takes a comma-delimited ``txid``
    list of **at most 20** trade ids and returns a map keyed by trade id
    with ``ordertxid`` / ``pair`` / ``time`` / ``type`` / ``ordertype`` /
    ``price`` ("Average price order was executed at") / ``cost`` / ``fee``
    ("Total fee (quote currency)") / ``vol`` / ``maker``. Fill evidence is
    assembled by reading the order's ``trades`` id array from
    ``QueryOrders(trades=true)`` and then querying those ids in chunks.

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
import re
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation
from typing import Any, Literal
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
from scout.live.kraken_signing import (
    KrakenNonce,
    KrakenSigningError,
    get_nonce_source,
    key_fingerprint,
    sign_kraken_request,
)
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


class KrakenClientOrderIdError(KrakenAPIError):
    """Kraken refused the ``cl_ord_id`` we sent.

    Terminal. Because ``_validate_cl_ord_id`` rejects malformed ids locally
    before any HTTP, a server-side cl_ord_id error in practice means the id
    is already in use — but the docs publish no error string for either
    case (module docstring, fact 11), so the class is named for what is
    actually known rather than for the inference.
    """


class KrakenAmbiguousSubmissionError(Exception):
    """An ``AddOrder`` POST may or may not have reached the matching engine.

    Raised when the submission fails in a way that does NOT prove the order
    was refused: a network drop, a timeout, an HTTP 5xx, a CDN interstitial.
    All of those can occur AFTER Kraken accepted the order — a 502 from an
    edge node says nothing about what the origin did with the POST.

    **Never resend on this.** Kraken publishes no duplicate-``cl_ord_id``
    contract (module docstring, fact 11), so a resend is a live double-order
    risk. Resolve with ``resolve_order_submission`` instead; the
    ``client_order_id`` needed to do that is carried on the exception.
    """

    def __init__(self, message: str, *, client_order_id: str) -> None:
        super().__init__(message)
        self.client_order_id = client_order_id


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

# Withdrawal-scoped endpoints probed by preflight_credentials_check, as
# (path, post_data). EVERY one must answer EGeneral:Permission denied for
# the key to count as withdrawal-excluded — one endpoint is not enough
# because Kraken need not gate them all on the same permission bit.
# WithdrawStatus's `asset` filter is optional; ZUSD is passed so the probe
# is a well-formed query rather than an unfiltered account-wide one.
_WITHDRAWAL_PROBES: tuple[tuple[str, dict[str, Any] | None], ...] = (
    ("/0/private/WithdrawMethods", None),
    ("/0/private/WithdrawStatus", {"asset": "ZUSD"}),
)

_BACKOFFS: tuple[float, ...] = (1.0, 2.0, 4.0)

# Kraken caps Depth's `count` at 500.
_MAX_DEPTH_COUNT = 500

# QueryTrades takes "a comma delimited list of transaction IDs to query info
# about (20 maximum)" — fill reads are chunked to this.
_MAX_QUERY_TRADES_IDS = 20

# CancelOrder outcomes that mean "the order is already gone", which is the
# SUCCESS case for a cancel, not a failure. NOT in Kraken's published error
# table (module docstring, fact 14) — a best-effort mapping of the observed
# strings. Anything unmatched still raises.
_CANCEL_ALREADY_GONE_ERROR_PREFIXES: tuple[str, ...] = (
    "EOrder:Unknown order",
    "EOrder:Invalid order",
)

# The two endpoints that accept a cl_ord_id filter, as (path, result key).
# QueryOrders is deliberately absent: it requires txid and cannot be queried
# by client id at all (module docstring, fact 13).
_CL_ORD_ID_LOOKUPS: tuple[tuple[str, str], ...] = (
    ("/0/private/OpenOrders", "open"),
    ("/0/private/ClosedOrders", "closed"),
)

# Consecutive clean-empty sweeps required before resolve_order_submission
# will say not_accepted. Two, because a just-accepted order is briefly in
# neither container and a single sweep cannot tell that apart from a reject.
_SUBMISSION_CONFIRM_SWEEPS = 2

# Kraken's three documented cl_ord_id forms (module docstring, fact 10).
_CL_ORD_ID_LONG_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-" r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_CL_ORD_ID_SHORT_UUID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_CL_ORD_ID_FREE_TEXT_MAX_LEN = 18
# Printable ASCII excluding space. The docs say "ascii text"; whitespace and
# control bytes are excluded here because they survive form-encoding as
# escapes and would make the id we send differ visibly from the id we logged.
_CL_ORD_ID_FREE_TEXT_RE = re.compile(r"^[\x21-\x7e]+$")


class _Indeterminate:
    """Sentinel type: "the payload could not answer the question".

    Distinct from ``None`` (a real, well-formed absence) because only a real
    absence may contribute to a ``not_accepted`` verdict — the one verdict
    that tells a caller a resend is safe.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "INDETERMINATE"


INDETERMINATE = _Indeterminate()


def _validate_cl_ord_id(client_order_id: str) -> None:
    """Raise unless ``client_order_id`` matches a form Kraken documents.

    Checked locally, before any HTTP: a malformed id is a guaranteed reject,
    and spending a signed round trip (and a nonce) to learn that is waste.

    The 18-character free-text ceiling is the sharp edge —
    ``scout.live.idempotency.make_client_order_id`` targets Binance's 28 and
    does NOT fit here (module docstring, fact 10).
    """
    if not isinstance(client_order_id, str) or not client_order_id:
        raise KrakenClientOrderIdError("cl_ord_id must be a non-empty string")
    if _CL_ORD_ID_LONG_UUID_RE.match(client_order_id):
        return
    if _CL_ORD_ID_SHORT_UUID_RE.match(client_order_id):
        return
    if not _CL_ORD_ID_FREE_TEXT_RE.match(client_order_id):
        raise KrakenClientOrderIdError(
            f"cl_ord_id {client_order_id!r} contains characters outside printable "
            "ASCII; Kraken accepts a UUID (long or short form) or free ASCII text"
        )
    if len(client_order_id) > _CL_ORD_ID_FREE_TEXT_MAX_LEN:
        raise KrakenClientOrderIdError(
            f"cl_ord_id {client_order_id!r} is {len(client_order_id)} chars; Kraken "
            f"free-text ids are capped at {_CL_ORD_ID_FREE_TEXT_MAX_LEN} (a 32-hex "
            "or dashed-UUID id is the alternative). Note make_client_order_id() "
            "targets Binance's 28-char limit and does not fit Kraken."
        )


def _format_decimal(
    value: Decimal, decimals: int, *, rounding: str = ROUND_DOWN
) -> str:
    """Quantize ``value`` to ``decimals`` places as a FIXED-POINT string.

    Every number that reaches an order body goes through here. ``urlencode``
    renders values with ``str()``, and ``str()`` of a small quantity yields
    scientific notation — ``str(0.00001)`` is ``'1e-05'`` and even
    ``str(Decimal('1E-5'))`` is ``'1E-5'`` — which Kraken rejects. The ``f``
    format spec is what forces ``'0.00001'``.
    """
    quantum = Decimal(1).scaleb(-int(decimals))
    return format(value.quantize(quantum, rounding=rounding), "f")


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

        # Swappable in tests to skip real backoff delays.
        self._retry_sleep = asyncio.sleep

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------
    def _api_key(self) -> str:
        """Extract the API key from SecretStr; raise if unconfigured."""
        return self._secret_value("KRAKEN_API_KEY")

    def _nonce_source(self, api_key: str) -> KrakenNonce:
        """The process-wide nonce counter for this adapter's API key.

        Resolved per request rather than cached on the instance: Kraken
        counts nonces per KEY, so every adapter sharing a key must draw
        from one counter. Looking it up lazily also means a public-only
        adapter (no credentials) never touches the registry.
        """
        return get_nonce_source(key_fingerprint(api_key))

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

        Retry taxonomy. The split that matters is DEFINITIVE vs AMBIGUOUS,
        because ``place_limit_order`` treats a ``KrakenAPIError`` as proof the
        order was refused and anything else as "it may exist". Only Kraken's
        own error envelope (HTTP 200 + a non-empty ``error`` list) is a venue
        decision; every other anomaly is transport of unknown provenance and
        must be transient so the order path can classify it ambiguous.

        - network error / timeout      → retry, then ``VenueTransientError``
        - HTTP 5xx (incl. Cloudflare 520-529) → retry, then ``VenueTransientError``
        - HTTP 429                     → ``KrakenRateLimitError`` (no retry)
        - HTTP 3xx (signed POST only)  → ``VenueTransientError``, raised
          immediately (never followed — see the signed-POST branch)
        - other HTTP 4xx               → ``VenueTransientError``, raised
          immediately. NOT a Kraken refusal: refusals arrive as HTTP 200.
        - HTML body on a 200           → retry, then ``VenueTransientError``
        - non-object JSON on a 200     → ``VenueTransientError``, raised
          immediately. Every Kraken reply is an object.
        - HTTP 200 + empty error list  → return ``result``
        - ``EService:*`` / internal    → retry, then ``VenueTransientError``
        - auth-class errors            → ``KrakenAuthError`` (NEVER retried)
        - ``EGeneral:Permission denied``→ ``KrakenPermissionError`` (no retry)
        - rate-limit errors            → ``KrakenRateLimitError`` (no retry)
        - errors naming ``cl_ord_id``  → ``KrakenClientOrderIdError`` (no retry)
        - any other non-empty error    → ``KrakenAPIError`` (no retry)

        Return-shape caveat: a few Kraken endpoints send ``result`` as a JSON
        ARRAY rather than an object (``WithdrawMethods`` is one). Those return
        ``{}`` here — PR-K1 only needs the raise/don't-raise signal from them,
        and the declared ``dict`` return type is what every current caller
        consumes. PR-K2 must widen this return type before it can read an
        array payload, rather than assuming ``{}`` means "empty result".
        """
        url = f"{self._base_url}{path}"
        session = self._get_session()
        last_exc: Exception | None = None

        for attempt in range(len(_BACKOFFS) + 1):
            try:
                if signed:
                    body = dict(data or {})
                    api_key = self._api_key()
                    # Fresh nonce per attempt: Kraken requires strictly
                    # increasing nonces, so a replayed one is rejected.
                    nonce = await self._nonce_source(api_key).next()
                    body["nonce"] = nonce
                    post_data = urlencode(body)
                    headers = {
                        "API-Key": api_key,
                        "API-Sign": sign_kraken_request(
                            path, post_data, nonce, self._api_secret()
                        ),
                        "Content-Type": "application/x-www-form-urlencoded",
                    }
                    # allow_redirects=False is load-bearing on a SIGNED POST.
                    # aiohttp follows 307/308 by re-sending the same method
                    # AND body — a second AddOrder transmission inside one
                    # attempt, invisible to retry_transient=False. It also
                    # forwards every header it did not explicitly strip, and
                    # it strips only Authorization, so API-Key / API-Sign
                    # would be handed to the redirect target.
                    cm = session.post(
                        url, data=post_data, headers=headers, allow_redirects=False
                    )
                else:
                    cm = session.get(url, params=params)

                async with cm as resp:
                    if resp.status == 429:
                        raise KrakenRateLimitError(f"kraken 429 on {path}")

                    if 300 <= resp.status < 400:
                        # Only reachable on the signed POST, which no longer
                        # follows redirects. api.kraken.com never redirects a
                        # private endpoint, so this is a hijack or a
                        # misconfigured base URL — never something to follow.
                        #
                        # Raised as transient, not as a Kraken error, so the
                        # order path classifies it AMBIGUOUS: a redirect reply
                        # is not an order rejection, and whatever produced it
                        # may have relayed the POST onward. Raised immediately
                        # rather than retried — re-sending a signed POST into
                        # something that redirects is the double-order shape
                        # this whole branch exists to avoid.
                        raise VenueTransientError(
                            f"kraken {path}: unexpected redirect (HTTP "
                            f"{resp.status}) on a signed request"
                        )

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
                        # Non-429 4xx. NOT a Kraken refusal: Kraken signals
                        # application-level rejection as HTTP 200 + a non-empty
                        # `error` list, so a 4xx status line did not come from
                        # the matching engine deciding anything. A proxy 400,
                        # a 408 Request Timeout or a 499 is fully compatible
                        # with the POST having been relayed onward, which is
                        # why this is transient (hence AMBIGUOUS on the order
                        # path) rather than definitive.
                        #
                        # Raised immediately, never retried: re-sending a
                        # signed non-idempotent POST at something answering
                        # 4xx is the double-order shape.
                        #
                        # Also NOT resp.raise_for_status(): its
                        # ClientResponseError carries request_info, whose repr
                        # renders the request headers — including this call's
                        # live API-Key and API-Sign. Any caller logging
                        # repr(exc) would write credentials to disk. The
                        # message here is a string we control.
                        raise VenueTransientError(f"kraken {path}: HTTP {resp.status}")

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
                    # Well-formed JSON that is not an object. EVERY Kraken
                    # reply, success or failure, is the object
                    # {"error": [...], "result": {...}} — so a bare array or
                    # scalar is not Kraken deciding anything, it is an
                    # injected or rewritten body. Transient (hence AMBIGUOUS
                    # on the order path) rather than definitive, and raised
                    # immediately rather than retried, for the same reason as
                    # the 4xx branch above.
                    raise VenueTransientError(
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

            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                # Deliberately the WHOLE aiohttp.ClientError tree, not just
                # ClientConnectorError. ClientConnectorError is the
                # connect-phase failure — the one case that PROVES nothing was
                # sent. Its siblings are the ambiguous ones and none of them
                # inherit from it: ServerDisconnectedError and
                # ClientConnectionResetError are peers under
                # ClientConnectionError, ClientOSError is a PARENT, and
                # ClientPayloadError means the headers already arrived (so the
                # request definitely reached Kraken) and only the body read
                # failed. Catching the narrow class let every genuinely
                # ambiguous transport failure escape _request unclassified,
                # which on the order path meant it never became a
                # KrakenAmbiguousSubmissionError.
                #
                # ValueError covers json.JSONDecodeError from a non-JSON,
                # non-HTML 200 body. KrakenSigningError is NOT in this tree
                # (it derives from Exception) and still fails fast without a
                # retry, which is what keeps a mis-pasted secret from burning
                # nonces.
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

        # Substring, not prefix: Kraken publishes no error code for a
        # rejected client order id (module docstring, fact 11), so the field
        # name in the message body is the only signal available.
        cl_ord_id_error = next((e for e in errors if "cl_ord_id" in e.lower()), None)
        if cl_ord_id_error is not None:
            raise KrakenClientOrderIdError(f"kraken {path}: {cl_ord_id_error}")

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

    def _base_matches(self, row: dict, symbol: str) -> bool:
        """Verify the returned pair's BASE really is ``symbol``.

        Kraken does curated alias resolution server-side on the ``pair``
        query — ``BTCUSD`` answers with ``XXBTZUSD``, which is what we want,
        but it means we asked a question and got an answer about a
        *different string than we sent*. Without checking the base we would
        trust whatever Kraken decided our ticker meant, and a wrong-but-
        online pair resolves silently into an order for the wrong asset.
        Validating the quote alone does not catch this: a mis-resolved pair
        is still USD-quoted.
        """
        expected = symbol.upper()
        actual = normalize_kraken_asset(str(row.get("base", "")))
        if actual == expected:
            return True
        log.warning(
            "kraken_pair_base_mismatch",
            symbol=expected,
            resolved_base=actual,
            altname=row.get("altname"),
        )
        return False

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
                if not self._base_matches(row, symbol):
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
        # Re-validate on this row too: resolve_pair_for_symbol checked a
        # different response, and a metadata row that disagrees about the
        # base would hand the sizing layer another asset's tick and minimums.
        if not self._base_matches(row, canonical):
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
            if bid <= ask:
                return (bid + ask) / Decimal(2)
            # Crossed book (bid > ask) — a stale or corrupt ticker. Averaging
            # the two produces a plausible-looking number that is arbitrarily
            # far from the real price, which would silently mis-size an order.
            # Fall through to the last trade instead.
            log.warning(
                "kraken_ticker_crossed_book",
                pair=pair,
                bid=str(bid),
                ask=str(ask),
            )

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
        """AVAILABLE spot balance in ``asset`` via ``POST /0/private/BalanceEx``.

        Uses BalanceEx, not Balance. ``/0/private/Balance`` reports a total
        that is net of pending withdrawals but NOT of funds held by open
        orders, so on an account with resting orders it overstates what can
        actually be spent — and ``balance_gate`` consumes this number as
        "available". BalanceEx breaks the total out and lets us subtract the
        hold.

        Computed with Kraken's documented formula
        (https://docs.kraken.com/api/docs/rest-api/get-extended-balance):
        ``available = balance + credit - credit_used - hold_trade``.
        ``credit``/``credit_used`` appear only on accounts with a credit
        line and default to 0 — including them costs nothing and keeps the
        result correct if the pilot account ever gets one.

        Defaults to USD, not the ABC's USDT: this is a USD-quoted Kraken
        pilot. Every caller in-tree passes the asset by keyword, so the
        narrowed default cannot silently change a Binance call.

        Keys are normalized (``ZUSD``→USD, ``XXBT``→BTC). Suffixed keys
        (``XBT.S`` staked, ``.F``/``.B``/``.M``/``.T`` earn and tokenized
        variants) are SKIPPED — they are not spot-tradable, and counting
        them would let a balance gate approve an order the account cannot
        fund. Returns ``0.0`` when the asset is absent.
        """
        body = await self._private_post("/0/private/BalanceEx")
        target = asset.upper()
        for code, entry in body.items():
            if "." in code:
                continue
            if normalize_kraken_asset(code) != target:
                continue
            if not isinstance(entry, dict):
                log.warning(
                    "kraken_balance_unexpected_entry_shape",
                    asset=target,
                    entry_type=type(entry).__name__,
                )
                return 0.0
            balance = _to_float(entry.get("balance")) or 0.0
            hold_trade = _to_float(entry.get("hold_trade")) or 0.0
            credit = _to_float(entry.get("credit")) or 0.0
            credit_used = _to_float(entry.get("credit_used")) or 0.0
            available = balance + credit - credit_used - hold_trade
            if available < 0.0:
                # Nonsensical; treat as unfundable rather than pass a
                # negative into a gate that may only test an upper bound.
                log.warning(
                    "kraken_balance_negative_available",
                    asset=target,
                    balance=balance,
                    hold_trade=hold_trade,
                )
                return 0.0
            return available
        return 0.0

    async def fetch_fee_tier(self, *, pair: str) -> dict[str, Any]:
        """This ACCOUNT's maker and taker rates for ``pair``.

        ``POST /0/private/TradeVolume`` with a ``pair`` filter returns
        ``fees`` (taker) and ``fees_maker`` (maker) maps, each keyed by the
        AssetPairs key and carrying a ``fee`` percent for the volume tier the
        account is actually in.

        Why this and not ``AssetPairs.fees`` / ``fees_maker``: those are the
        PUBLIC schedule's tier-zero rates, i.e. what a brand-new account
        pays. An account with traded volume, a fee-tier promotion or a
        different fee currency pays something else, and a cost screen built
        on the public table quietly shows the wrong number — which is worse
        than showing none, because it looks authoritative.

        Fails CLOSED: an unreadable rate raises rather than defaulting.
        A caller that puts a guessed fee in front of an operator asking them
        to authorize a trade has made the approval screen a liability.

        The NEXT tier is returned alongside the current one because it is
        decision-relevant and free to read: each fee row carries ``nextfee``
        and ``nextvolume``, so an operator sizing a trade can see how far the
        account is from a cheaper rate. Absent on the top tier, hence
        optional — a missing next tier is not an error.

        Returns:
            ``{"pair", "maker_pct", "taker_pct", "volume", "currency",
            "next_maker_pct", "next_taker_pct", "next_volume", "raw"}`` —
            every rate a fixed-point percent STRING (``"0.16"`` means 0.16%),
            matching how the rest of this adapter hands Kraken numerics to its
            callers. The three ``next_*`` fields are ``None`` when Kraken
            publishes no further tier.
        """
        body = await self._private_post("/0/private/TradeVolume", {"pair": pair})
        taker = _fee_row_from_map(body.get("fees"), pair)
        maker = _fee_row_from_map(body.get("fees_maker"), pair)
        taker_pct = _decimal_str(taker.get("fee")) if taker else None
        maker_pct = _decimal_str(maker.get("fee")) if maker else None
        if taker_pct is None or maker_pct is None:
            raise KrakenAPIError(
                f"kraken /0/private/TradeVolume: no readable fee schedule for "
                f"pair={pair!r} (maker={maker_pct!r} taker={taker_pct!r})"
            )
        log.info(
            "kraken_fee_tier",
            pair=pair,
            maker_pct=maker_pct,
            taker_pct=taker_pct,
        )
        return {
            "pair": pair,
            "maker_pct": maker_pct,
            "taker_pct": taker_pct,
            "volume": _decimal_str(body.get("volume")),
            "currency": str(body.get("currency") or "") or None,
            "next_maker_pct": _decimal_str((maker or {}).get("nextfee")),
            "next_taker_pct": _decimal_str((taker or {}).get("nextfee")),
            "next_volume": _decimal_str((taker or {}).get("nextvolume"))
            or _decimal_str((maker or {}).get("nextvolume")),
            "raw": body,
        }

    async def preflight_credentials_check(self) -> dict[str, Any]:
        """Verify the API key authenticates AND cannot withdraw funds.

        Checks, ALL of which must pass before the adapter is considered
        usable for the supervised pilot:

        1. ``POST /0/private/Balance`` succeeds → the key/secret/nonce chain
           works and has query permission.
        2. BOTH withdrawal-scoped probes — ``/0/private/WithdrawMethods``
           and ``/0/private/WithdrawStatus`` — are REFUSED with
           ``EGeneral:Permission denied`` → the key demonstrably lacks
           withdrawal scope. Two endpoints rather than one because they need
           not be gated by the same permission bit.

        Check 2 fails closed. Kraken exposes no endpoint that reports a
        key's permission scopes (see module docstring, fact 6), so an
        explicit denial is the only positive evidence available. A success
        means the key reached a withdrawal-scoped endpoint; any other
        outcome — a different error, a transient failure, a network drop —
        means we did not PROVE it cannot withdraw. Both raise
        ``KrakenWithdrawalCapabilityError``.

        CAVEAT for the operator, verified in the docs 2026-07-31:
        ``WithdrawStatus`` is documented as requiring "Funds permissions -
        Withdraw" **OR** "Data - Query ledger entries". A key that lacks
        withdraw but HAS ledger-query will therefore SUCCEED on this probe
        and be rejected here even though it is genuinely safe. That is the
        fail-closed direction, but it is a false reject: if preflight fails
        with ``probe=/0/private/WithdrawStatus outcome=permitted`` while
        ``WithdrawMethods`` denied, reissue the key without ledger-query
        rather than assuming it can withdraw.

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

        for probe_path, probe_data in _WITHDRAWAL_PROBES:
            await self._probe_withdrawal_denied(probe_path, probe_data)
        withdrawal_excluded = True

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

    async def _probe_withdrawal_denied(
        self, path: str, data: dict[str, Any] | None
    ) -> None:
        """Assert ``path`` refuses this key with ``EGeneral:Permission denied``.

        Returns normally ONLY on that exact denial. Success (the key reached
        a withdrawal-scoped endpoint) and every other outcome (different
        error, transient failure, network drop) raise
        ``KrakenWithdrawalCapabilityError`` — "we did not prove it cannot
        withdraw" and "it can withdraw" are both disqualifying.
        """
        try:
            await self._private_post(path, data)
        except KrakenPermissionError:
            return
        except Exception as exc:
            log.warning(
                "kraken_preflight_failed",
                stage="withdrawal_probe",
                probe=path,
                outcome="inconclusive",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise KrakenWithdrawalCapabilityError(
                f"withdrawal-capability probe {path} was inconclusive "
                f"({type(exc).__name__}: {exc}) — refusing to proceed; only an "
                "explicit EGeneral:Permission denied proves the key cannot withdraw"
            ) from exc

        log.warning(
            "kraken_preflight_failed",
            stage="withdrawal_probe",
            probe=path,
            outcome="permitted",
        )
        raise KrakenWithdrawalCapabilityError(
            f"API key reached withdrawal-scoped endpoint {path} — reissue the key "
            "without 'Funds permissions - Withdraw' before running the pilot. "
            "Note: WithdrawStatus also accepts 'Data - Query ledger entries', so a "
            "success there alone may mean ledger-query, not withdraw, is enabled."
        )

    # ------------------------------------------------------------------
    # Order lifecycle — PR-K2.
    #
    # Two traps that shaped this section:
    #  - Format every numeric order param as a pre-formatted Decimal string.
    #    urlencode() renders a float via str(), and str(0.00001) is '1e-05',
    #    which Kraken rejects. Everything numeric goes through
    #    _format_decimal; nothing here passes a float to a body.
    #  - NEVER log repr(exc) or exc.request_info for a signed call.
    #    aiohttp's ClientResponseError repr embeds request_info, which
    #    carries the API-Key and API-Sign headers — that would write live
    #    credentials into the log. Log type(exc).__name__ and str(exc).
    #
    # The market-order surface stays closed. The three ABC entry points that
    # imply a market order raise; place_limit_order is the only path to
    # AddOrder.
    # ------------------------------------------------------------------
    async def send_order(self, *, pair: str, side: str, size_usd: Decimal) -> dict:
        raise NotImplementedError("kraken lane is limit-only; use place_limit_order")

    async def place_order_request(self, request: OrderRequest) -> str:
        """Not the Kraken entry point — ``OrderRequest`` carries no price.

        The ABC's request shape is quote-sized and market-shaped
        (``size_usd``, no limit price), which cannot express the only order
        this lane places. The supervised pilot runner calls
        ``place_limit_order`` directly; this override exists so a generic
        routing caller fails loudly instead of silently reaching a venue it
        was never wired for.
        """
        raise KrakenAPIError("kraken lane is limit-only; use place_limit_order")

    async def place_exit_order(
        self,
        *,
        pair: str,
        base_qty: Decimal,
        client_order_id: str,
        timeout_sec: float,
    ) -> OrderConfirmation:
        raise NotImplementedError(
            "kraken pilot exits via place_limit_order/cancel + manual supervision"
        )

    # ---------------- placement ----------------
    def _prepare_order_amounts(
        self,
        *,
        row: dict[str, Any],
        pair: str,
        side: str,
        price: Decimal,
        volume: Decimal,
    ) -> tuple[str, str]:
        """Quantize price/volume to the pair's precision and enforce Kraken's
        minimums LOCALLY, returning the exact strings to put on the wire.

        Rounding is conservative per field rather than nearest, so a
        quantization can never enlarge the caller's exposure:
        - volume always rounds DOWN — never trade more than asked;
        - price rounds DOWN on a buy (never bid above the asked price) and
          UP on a sell (never offer below it). Both directions move the
          order at most one tick away from filling, which is the safe
          direction to be wrong in.

        Validation runs AFTER quantization, because quantization is what can
        push a marginal order under ``ordermin``/``costmin``. Rejecting here
        rather than round-tripping a doomed order also keeps a guaranteed
        failure from consuming a nonce and an AddOrder rate-limit slot.
        """
        pair_decimals = row.get("pair_decimals")
        lot_decimals = row.get("lot_decimals")
        if pair_decimals is None or lot_decimals is None:
            raise KrakenAPIError(
                f"kraken AddOrder: pair {pair!r} is missing precision metadata "
                f"(pair_decimals={pair_decimals!r} lot_decimals={lot_decimals!r})"
            )

        try:
            price_str = _format_decimal(
                price,
                int(pair_decimals),
                rounding=ROUND_DOWN if side == "buy" else ROUND_UP,
            )
            volume_str = _format_decimal(volume, int(lot_decimals))
        except (TypeError, ValueError, InvalidOperation) as exc:
            raise KrakenAPIError(
                f"kraken AddOrder: cannot format price/volume for {pair!r}: {exc}"
            ) from exc

        price_q = Decimal(price_str)
        volume_q = Decimal(volume_str)
        if price_q != price or volume_q != volume:
            log.info(
                "kraken_order_precision_adjusted",
                pair=pair,
                price_requested=format(price, "f"),
                price_sent=price_str,
                volume_requested=format(volume, "f"),
                volume_sent=volume_str,
            )
        if price_q <= 0 or volume_q <= 0:
            raise KrakenAPIError(
                f"kraken AddOrder: price/volume quantized to zero for {pair!r} "
                f"(price={price_str} volume={volume_str}) — the request is below "
                "the pair's published precision"
            )

        # Tick check is separate from quantization on purpose. Quantizing to
        # pair_decimals satisfies the tick for every pair Kraken currently
        # lists (PR-K1 measured tick_size == 10**-pair_decimals across all
        # 1428), but the two fields are independent, and a pair whose tick is
        # coarser than its price precision would otherwise let an off-grid
        # price through. Rejecting rather than re-quantizing to the tick keeps
        # the price the caller chose intact — a supervised operator should be
        # told their price is off-grid, not have it silently moved.
        tick = _to_decimal(row.get("tick_size"))
        if tick is not None and tick > 0:
            try:
                off_grid = price_q % tick != 0
            except (InvalidOperation, ArithmeticError) as exc:
                # Decimal.__mod__ raises past 28 quotient digits, e.g.
                # Decimal('1E30') % Decimal('0.00000001').
                raise KrakenAPIError(
                    f"kraken AddOrder: cannot check price {price_str} against "
                    f"{pair!r} tick_size {format(tick, 'f')}: {exc}"
                ) from exc
            if off_grid:
                raise KrakenAPIError(
                    f"kraken AddOrder: price {price_str} is not a multiple of the "
                    f"{pair!r} tick_size {format(tick, 'f')}"
                )

        ordermin = _to_decimal(row.get("ordermin"))
        if ordermin is not None and volume_q < ordermin:
            raise KrakenAPIError(
                f"kraken AddOrder: volume {volume_str} is below {pair!r} ordermin "
                f"{format(ordermin, 'f')}"
            )

        costmin = _to_decimal(row.get("costmin"))
        if costmin is not None and price_q * volume_q < costmin:
            raise KrakenAPIError(
                f"kraken AddOrder: cost {format(price_q * volume_q, 'f')} is below "
                f"{pair!r} costmin {format(costmin, 'f')} — Kraken enforces costmin "
                "independently of ordermin"
            )

        return price_str, volume_str

    async def place_limit_order(
        self,
        *,
        pair: str,
        side: str,
        price: Decimal,
        volume: Decimal,
        client_order_id: str,
        validate_only: bool = False,
    ) -> dict[str, Any]:
        """Place ONE spot LIMIT order via ``POST /0/private/AddOrder``.

        The only method in this adapter that can reach an order endpoint.

        Args:
            pair: Kraken altname (``XBTUSD``) — what AddOrder documents.
            side: ``buy`` or ``sell``.
            price: Limit price, quantized to the pair's ``pair_decimals``.
            volume: Base-asset quantity, quantized to ``lot_decimals``.
            client_order_id: Sent as ``cl_ord_id``; validated locally against
                Kraken's documented forms first (see ``_validate_cl_ord_id``).
            validate_only: Send ``validate=true`` — Kraken parses and checks
                the order but never places it.

        Gating: a REAL order requires ``LIVE_USE_REAL_SIGNED_REQUESTS``, the
        same emergency-revert lever the Binance order path uses.
        ``validate_only=True`` is exempt: it is a venue-side dry run that
        cannot trade, and it is how the pre-trade rehearsal exercises this
        exact code path (signing, encoding, precision, minimums) without an
        order existing. Gating the dry run would leave the rehearsal testing
        a different codepath than the live run, which is the failure the
        rehearsal exists to prevent.

        NOT retried. ``retry_transient=False`` is load-bearing: AddOrder is
        not idempotent, the nonce is regenerated per attempt so Kraken cannot
        dedup a replay, and Kraken publishes no duplicate-cl_ord_id contract.
        A retry here is a double-order.

        Returns:
            ``{"txid": [...], "descr": {...}, "client_order_id", "pair",
            "side", "price", "volume", "validate_only", "raw"}``.
            ``txid`` is empty on a ``validate_only`` run — Kraken returns
            ``descr`` alone when nothing was placed.

        Raises:
            KrakenClientOrderIdError: id malformed (local) or refused (venue).
            KrakenAPIError: unknown/untradable pair, or a local precision /
                ordermin / costmin / tick rejection — no HTTP POST is sent.
            KrakenAmbiguousSubmissionError: the POST may have landed. Resolve
                with ``resolve_order_submission``; never resend.
        """
        normalized_side = side.strip().lower()
        if normalized_side not in ("buy", "sell"):
            raise KrakenAPIError(
                f"kraken AddOrder: side must be 'buy' or 'sell', got {side!r}"
            )
        _validate_cl_ord_id(client_order_id)
        if not validate_only and not getattr(
            self._settings, "LIVE_USE_REAL_SIGNED_REQUESTS", False
        ):
            raise NotImplementedError(
                "LIVE_USE_REAL_SIGNED_REQUESTS=False — emergency-revert posture"
            )
        if price <= 0 or volume <= 0:
            raise KrakenAPIError(
                f"kraken AddOrder: price and volume must be positive, got "
                f"price={format(price, 'f')} volume={format(volume, 'f')}"
            )

        row = await self.fetch_exchange_info_row(pair)
        if row is None:
            raise KrakenAPIError(f"kraken AddOrder: unknown pair {pair!r}")
        if row.get("status") != _TRADABLE_STATUS:
            # Fail closed, matching resolve_pair_for_symbol: cancel_only and
            # reduce_only cannot take a new order at all, and post_only would
            # reject this plain limit order at the matching engine.
            raise KrakenAPIError(
                f"kraken AddOrder: pair {pair!r} status is {row.get('status')!r}, "
                f"not {_TRADABLE_STATUS!r}"
            )
        price_str, volume_str = self._prepare_order_amounts(
            row=row,
            pair=pair,
            side=normalized_side,
            price=price,
            volume=volume,
        )

        data: dict[str, Any] = {
            "pair": pair,
            "type": normalized_side,
            "ordertype": "limit",
            "price": price_str,
            "volume": volume_str,
            "cl_ord_id": client_order_id,
        }
        if validate_only:
            data["validate"] = "true"

        log.info(
            "kraken_order_submitting",
            pair=pair,
            side=normalized_side,
            price=price_str,
            volume=volume_str,
            client_order_id=client_order_id,
            validate_only=validate_only,
        )
        try:
            body = await self._private_post(
                "/0/private/AddOrder", data, retry_transient=False
            )
        except (KrakenAPIError, KrakenSigningError):
            # DEFINITIVE. Kraken parsed the request and refused it (its error
            # envelope is a decision, so the order does not exist), or signing
            # failed before anything was sent. Propagate unchanged — wrapping
            # these as ambiguous would send the caller off to resolve an order
            # we know was never created.
            raise
        except asyncio.CancelledError:
            # Cancellation must keep propagating — swallowing it would break
            # task teardown — but the order may still have been placed, so
            # leave a record an operator can find.
            log.warning(
                "kraken_order_submission_cancelled",
                pair=pair,
                client_order_id=client_order_id,
                note="AddOrder was cancelled mid-flight; the order may exist. "
                "Resolve with resolve_order_submission before any resend.",
            )
            raise
        except Exception as exc:
            # AMBIGUOUS — everything else, deliberately including exception
            # types not anticipated here. The POST may have reached the
            # matching engine: timeouts, mid-flight disconnects, payload
            # errors, 5xx, HTML interstitials and rate-limit refusals are all
            # compatible with the order existing. Kraken publishes no
            # duplicate-cl_ord_id contract (module docstring, fact 11), so
            # "unknown" must fail closed to "may exist" — the alternative is a
            # caller that resends and double-orders.
            #
            # KrakenRateLimitError lands here rather than in the definitive
            # branch, which is a deliberate over-caution: a rate limiter
            # almost certainly refuses before placing, but that is not
            # documented, and the cost of being wrong the safe way is two
            # extra read probes.
            if validate_only:
                # validate=true cannot trade, so however this failed, no order
                # exists. Reporting the rehearsal as ambiguous would send an
                # operator hunting for an order that cannot have been created.
                log.warning(
                    "kraken_order_validate_failed",
                    pair=pair,
                    client_order_id=client_order_id,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                raise
            log.warning(
                "kraken_order_submission_ambiguous",
                pair=pair,
                client_order_id=client_order_id,
                validate_only=validate_only,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise KrakenAmbiguousSubmissionError(
                f"kraken AddOrder for cl_ord_id={client_order_id} did not return a "
                f"verdict ({type(exc).__name__}: {exc}) — the order may exist. "
                "Resolve with resolve_order_submission; do NOT resend.",
                client_order_id=client_order_id,
            ) from exc

        txids = [str(t) for t in (body.get("txid") or []) if str(t)]
        descr = body.get("descr") if isinstance(body.get("descr"), dict) else {}
        if validate_only:
            if not descr:
                raise KrakenAPIError(
                    f"kraken AddOrder validate=true returned no descr: {body!r}"
                )
        elif not txids:
            # A clean envelope with no txid violates the documented response
            # shape, and we cannot tell whether the order exists. Treat it as
            # ambiguous rather than as a failure a caller might retry.
            log.warning(
                "kraken_order_missing_txid",
                pair=pair,
                client_order_id=client_order_id,
            )
            raise KrakenAmbiguousSubmissionError(
                f"kraken AddOrder for cl_ord_id={client_order_id} returned no error "
                "and no txid — the order may exist. Resolve with "
                "resolve_order_submission; do NOT resend.",
                client_order_id=client_order_id,
            )

        log.info(
            "kraken_order_submitted",
            pair=pair,
            side=normalized_side,
            price=price_str,
            volume=volume_str,
            client_order_id=client_order_id,
            validate_only=validate_only,
            txid=txids,
        )
        return {
            "txid": txids,
            "descr": descr,
            "client_order_id": client_order_id,
            "pair": pair,
            "side": normalized_side,
            "price": price_str,
            "volume": volume_str,
            "validate_only": validate_only,
            "raw": body,
        }

    # ---------------- cancel ----------------
    async def cancel_order(
        self,
        *,
        txid: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Cancel one resting order via ``POST /0/private/CancelOrder``.

        Exactly one identity is required; ``txid`` wins when both are given
        because it names a single order unambiguously, whereas ``cl_ord_id``
        is only unique across a client's OPEN orders.

        NOT gated by ``LIVE_USE_REAL_SIGNED_REQUESTS``. Cancel is the lever
        that REDUCES exposure — a flag flip that leaves a resting live order
        uncancellable disables the safety mechanism, which is the same
        reasoning that keeps the withdrawal preflight ungated.

        Not retried on a transient failure either, but for a different reason
        than AddOrder: a cancel keyed by a fixed identity is safe to repeat,
        so the transient error is surfaced to the caller (who can simply call
        again) rather than being absorbed by a loop that hides how many
        attempts happened during a supervised run.

        An unknown or already-closed order is a SUCCESS, not a failure — the
        post-condition ("this order is not resting") holds either way. It
        comes back as ``already_gone=True`` with ``count=0``. The error
        strings for that case are not in Kraken's published table (module
        docstring, fact 14), so anything unmatched still raises.

        Returns:
            ``{"count": int, "pending": bool, "already_gone": bool,
            "txid", "client_order_id", "raw"}``.
        """
        if not txid and not client_order_id:
            raise KrakenAPIError("kraken CancelOrder: pass txid or client_order_id")

        if txid:
            data: dict[str, Any] = {"txid": txid}
        else:
            # CancelOrder's `txid` param accepts a txid or a userref — NOT a
            # client order id, which is its own separate field.
            assert client_order_id is not None
            _validate_cl_ord_id(client_order_id)
            data = {"cl_ord_id": client_order_id}

        body = await self._private_post(
            "/0/private/CancelOrder",
            data,
            tolerate_errors=_CANCEL_ALREADY_GONE_ERROR_PREFIXES,
            retry_transient=False,
        )

        already_gone = _TOLERATED_KEY in body
        if already_gone:
            log.info(
                "kraken_cancel_already_gone",
                txid=txid,
                client_order_id=client_order_id,
                error=body[_TOLERATED_KEY],
            )
            return {
                "count": 0,
                "pending": False,
                "already_gone": True,
                "txid": txid,
                "client_order_id": client_order_id,
                "raw": body,
            }

        count = int(_to_float(body.get("count")) or 0)
        log.info(
            "kraken_cancel_ok",
            txid=txid,
            client_order_id=client_order_id,
            count=count,
            pending=bool(body.get("pending")),
        )
        return {
            "count": count,
            "pending": bool(body.get("pending")),
            "already_gone": False,
            "txid": txid,
            "client_order_id": client_order_id,
            "raw": body,
        }

    # ---------------- lookup ----------------
    @staticmethod
    def _map_order_status(
        kraken_status: str, *, volume: Decimal | None, filled: Decimal | None
    ) -> str:
        """Kraken order status → the ``OrderConfirmation`` vocabulary.

        Kraken's states are ``pending`` / ``open`` / ``closed`` / ``canceled``
        / ``expired``. The mapping that matters is the terminal one: a
        canceled or expired order that PARTIALLY FILLED left real inventory
        behind, so it must surface as ``partial`` carrying the filled
        quantity — calling it ``rejected`` (Binance's mapping for CANCELED,
        which only ever cancels unfilled remainders after reporting the fill)
        would tell the reconciler nothing happened while the account holds a
        position.

        An unreadable ``vol_exec`` on a terminal status is NOT treated as a
        zero fill. ``rejected`` asserts "no position exists", and defaulting a
        missing or non-numeric field to zero would make the adapter assert
        that about an order it cannot actually read — the dangerous direction.
        Such a row stays ``pending`` so the caller keeps looking rather than
        stopping on a fabricated terminal verdict.
        """
        if kraken_status not in ("closed", "canceled", "expired"):
            # pending / open — and anything unrecognised, which must not be
            # mistaken for terminal.
            return "pending"
        if filled is None:
            log.warning(
                "kraken_order_unreadable_fill",
                kraken_status=kraken_status,
                note="terminal status with no parseable vol_exec",
            )
            return "pending"
        if kraken_status == "closed":
            if volume is not None and 0 < filled < volume:
                return "partial"
            return "filled" if filled > 0 else "rejected"
        return "partial" if filled > 0 else "rejected"

    @staticmethod
    def _avg_fill_price(row: dict[str, Any]) -> float | None:
        """Volume-weighted fill price for an order row.

        ``cost / vol_exec`` is the definitive VWAP (both are execution
        fields). The top-level ``price`` — Kraken's own average executed
        price, distinct from ``descr.price``, the limit price — is the
        fallback for rows where cost is absent.
        """
        cost = _to_decimal(row.get("cost"))
        filled = _to_decimal(row.get("vol_exec"))
        if cost is not None and filled is not None and cost > 0 and filled > 0:
            return float(cost / filled)
        price = _to_decimal(row.get("price"))
        if price is not None and price > 0:
            return float(price)
        return None

    def _confirmation_from_order(
        self, txid: str | None, row: dict[str, Any], *, client_order_id: str
    ) -> OrderConfirmation:
        """Build an ``OrderConfirmation`` from a Kraken order row."""
        volume = _to_decimal(row.get("vol"))
        filled = _to_decimal(row.get("vol_exec"))
        status = self._map_order_status(
            str(row.get("status", "")), volume=volume, filled=filled
        )
        fill_price = (
            self._avg_fill_price(row) if status in ("filled", "partial") else None
        )
        return OrderConfirmation(
            venue=self.venue_name,
            venue_order_id=str(txid) if txid else None,
            client_order_id=client_order_id,
            status=status,
            filled_qty=float(filled) if filled is not None else None,
            fill_price=fill_price,
            raw_response=row,
        )

    @staticmethod
    def _find_order_by_cl_ord_id(
        body: dict[str, Any], container: str, client_order_id: str
    ) -> tuple[str, dict[str, Any]] | None | _Indeterminate:
        """Find ``client_order_id`` in an OpenOrders/ClosedOrders payload.

        THREE outcomes, not two — the distinction is the whole point:

        - ``(txid, row)``  the order is there;
        - ``None``         the container was present, well-formed, and held no
                           order carrying our client id — a real absence;
        - ``INDETERMINATE`` the payload could not answer the question.

        The server-side ``cl_ord_id`` filter is re-checked client-side ON
        PURPOSE. Kraken ignores unrecognised parameters rather than erroring,
        so if the filter were ever dropped the reply would be the account's
        FULL order list — and trusting the filter would hand back a stranger's
        order as ours, which on a reconciliation path means confirming a fill
        that never happened.

        But that re-check has a fail-open edge, which is why INDETERMINATE
        exists. ``cl_ord_id`` is an OPTIONAL response field: if the filter
        MATCHED (so the order does exist) yet the row omits the echo, a
        two-valued function reports "absent" — and absent-from-every-probe is
        the verdict that licenses a resend. Same for a ``result`` of ``null``
        or a non-dict container, which ``_request`` flattens to ``{}``.
        A non-empty container in which NOTHING carries a readable client id is
        therefore treated as unanswerable, never as absence.
        """
        orders = body.get(container)
        if not isinstance(orders, dict):
            # Container missing or malformed — includes the `result: null`
            # shape that _request returns as {}. We did not get to look.
            return INDETERMINATE
        saw_readable_id = False
        for txid, row in orders.items():
            if not isinstance(row, dict):
                continue
            row_cid = row.get("cl_ord_id")
            if row_cid is not None and str(row_cid) != "":
                saw_readable_id = True
            if str(row_cid or "") != client_order_id:
                continue
            return str(txid), row
        if orders and not saw_readable_id:
            # Rows came back but none carried a client id to match against —
            # the filter may well have matched ours. Cannot claim absence.
            return INDETERMINATE
        return None

    async def fetch_order_by_client_id(
        self, *, pair: str, client_order_id: str
    ) -> OrderConfirmation | None:
        """Find an order by ``cl_ord_id``, or ``None`` if Kraken has no such order.

        Checks OpenOrders then ClosedOrders — the only two endpoints that
        accept a client-id filter (module docstring, fact 13). ``pair`` is
        accepted for ABC compatibility and is NOT used to filter: Kraken's
        lookup is account-wide by client id, and narrowing on the pair string
        could only turn a real match into a false ``None``.

        NOT gated by ``LIVE_USE_REAL_SIGNED_REQUESTS`` (unlike the Binance
        implementation). This is a read, and it is the read a supervised
        operator needs most precisely when the flag has been flipped off
        mid-incident to stop new orders.
        """
        for path, container in _CL_ORD_ID_LOOKUPS:
            body = await self._private_post(path, {"cl_ord_id": client_order_id})
            found = self._find_order_by_cl_ord_id(body, container, client_order_id)
            if isinstance(found, _Indeterminate):
                # An unanswerable payload is not an absence. Raising keeps a
                # poller looking instead of concluding the order is gone.
                raise KrakenAPIError(
                    f"kraken {path}: payload could not be searched for "
                    f"cl_ord_id={client_order_id}"
                )
            if found is not None:
                txid, row = found
                return self._confirmation_from_order(
                    txid, row, client_order_id=client_order_id
                )
        return None

    async def fetch_open_orders(self) -> list[dict[str, Any]]:
        """EVERY resting order on the account — unfiltered ``OpenOrders``.

        The account-wide counterpart to ``fetch_order_by_client_id``. That
        method can only answer "is MY order there?"; this one answers "what is
        there?", which is the question a one-order-at-a-time pilot actually
        needs — an order placed from the Kraken web UI, or left behind by a
        run whose ledger row was lost, is invisible to a client-id lookup and
        would let the pilot add a second live order believing it was the
        first.

        Read-only, so it is neither gated by ``LIVE_USE_REAL_SIGNED_REQUESTS``
        nor barred from retrying: unlike AddOrder there is nothing
        non-idempotent to duplicate, and this read is most needed exactly when
        the flag has been flipped off mid-incident.

        Fails CLOSED. An unreadable payload raises rather than returning an
        empty list, because the caller uses "no open orders" as a licence to
        place one, and "we could not look" must never render as "there are
        none" (same reasoning as ``_find_order_by_cl_ord_id``'s INDETERMINATE).

        Returns:
            One normalized dict per resting order: ``txid`` /
            ``client_order_id`` (``None`` when the venue does not echo one) /
            ``pair`` / ``status`` / ``vol`` / ``vol_exec`` / ``price``.
            Numeric fields stay strings, as Kraken sends them.
        """
        body = await self._private_post("/0/private/OpenOrders")
        orders = body.get("open")
        if not isinstance(orders, dict):
            raise KrakenAPIError(
                "kraken /0/private/OpenOrders: payload has no readable 'open' "
                f"container (got {type(orders).__name__}) — cannot conclude the "
                "account has no resting orders"
            )

        open_orders: list[dict[str, Any]] = []
        for txid, row in orders.items():
            if not isinstance(row, dict):
                raise KrakenAPIError(
                    f"kraken /0/private/OpenOrders: entry {txid!r} is "
                    f"{type(row).__name__}, not an order row"
                )
            descr = row.get("descr") if isinstance(row.get("descr"), dict) else {}
            cl_ord_id = row.get("cl_ord_id")
            open_orders.append(
                {
                    "txid": str(txid),
                    "client_order_id": str(cl_ord_id) if cl_ord_id else None,
                    "pair": str(descr.get("pair") or "") or None,
                    "status": str(row.get("status") or "") or None,
                    "vol": _decimal_str(row.get("vol")),
                    "vol_exec": _decimal_str(row.get("vol_exec")),
                    # descr.price is the LIMIT price; the top-level `price` is
                    # the average executed price, which is 0 on a resting order.
                    "price": _decimal_str(descr.get("price"))
                    or _decimal_str(row.get("price")),
                }
            )
        log.info("kraken_open_orders", count=len(open_orders))
        return open_orders

    # ---------------- ambiguous-submission resolution ----------------
    async def resolve_order_submission_detail(
        self, *, client_order_id: str
    ) -> dict[str, Any]:
        """Probe every client-id-addressable endpoint and record what each said.

        The structured companion to ``resolve_order_submission``: same probes,
        but returns the full evidence record for the pilot's authorization log
        rather than just the verdict.

        Verdict rules:
        - found in ANY probe of ANY sweep      → ``accepted``
        - TWO consecutive sweeps in which every probe answered cleanly and
          none found it                        → ``not_accepted``
        - anything else                        → ``unresolved``

        ``not_accepted`` is the only verdict that tells a caller a resend is
        safe, so it is the one deliberately made hard to reach. It requires
        every probe to have SUCCEEDED and returned a searchable payload that
        did not contain the order — a failed probe and an unsearchable
        payload both count as "we could not look", which is a different fact
        from "we looked and it is not there".

        It also requires that to hold TWICE, separated by
        ``KRAKEN_SUBMISSION_SETTLE_SEC``. A freshly-accepted order is briefly
        in neither container — Kraken's ``pending`` state and the OpenOrders
        propagation window both produce a clean-empty sweep for an order that
        does exist. One sweep cannot distinguish that from a genuine reject;
        two sweeps either side of a settle delay can.

        Every probe runs even after a hit, so the record shows what each
        endpoint said. An order found in OpenOrders being absent from
        ClosedOrders is the normal, expected shape.
        """
        sweeps: list[list[dict[str, Any]]] = []
        txids: list[str] = []

        for sweep_index in range(_SUBMISSION_CONFIRM_SWEEPS):
            if sweep_index:
                # Settle delay before the confirming sweep — its whole job is
                # to give a just-accepted order time to become visible.
                await asyncio.sleep(
                    float(getattr(self._settings, "KRAKEN_SUBMISSION_SETTLE_SEC", 3.0))
                )
            probes = await self._submission_sweep(
                client_order_id=client_order_id, sweep=sweep_index
            )
            sweeps.append(probes)
            if not probes:
                # A sweep that asked nothing proves nothing. Only reachable if
                # _CL_ORD_ID_LOOKUPS is emptied, but zero evidence must never
                # reach the verdict that licenses a resend.
                verdict = "unresolved"
                break
            txids.extend(str(p["txid"]) for p in probes if p["outcome"] == "found")
            if any(p["outcome"] == "found" for p in probes):
                verdict = "accepted"
                break
            if any(p["outcome"] != "absent" for p in probes):
                # A probe errored or came back unsearchable. A later sweep
                # cannot repair that into an absence claim.
                verdict = "unresolved"
                break
        else:
            # Every sweep completed with every probe cleanly absent.
            verdict = "not_accepted"

        detail = {
            "verdict": verdict,
            "client_order_id": client_order_id,
            "txid": list(dict.fromkeys(txids)),
            "probes": [p for sweep in sweeps for p in sweep],
            "sweeps": sweeps,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        log.info(
            "kraken_submission_resolved",
            client_order_id=client_order_id,
            verdict=verdict,
            txid=detail["txid"],
            sweep_count=len(sweeps),
        )
        return detail

    async def _submission_sweep(
        self, *, client_order_id: str, sweep: int
    ) -> list[dict[str, Any]]:
        """One pass over every client-id-addressable endpoint.

        Outcomes per probe: ``found`` / ``absent`` / ``indeterminate`` /
        ``error``. Only ``absent`` is evidence the order does not exist.
        """
        probes: list[dict[str, Any]] = []
        for path, container in _CL_ORD_ID_LOOKUPS:
            try:
                body = await self._private_post(path, {"cl_ord_id": client_order_id})
            except Exception as exc:
                probes.append(
                    {
                        "probe": path,
                        "sweep": sweep,
                        "outcome": "error",
                        "txid": None,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
                log.warning(
                    "kraken_submission_probe",
                    probe=path,
                    sweep=sweep,
                    client_order_id=client_order_id,
                    outcome="error",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                continue

            found = self._find_order_by_cl_ord_id(body, container, client_order_id)
            if isinstance(found, _Indeterminate):
                outcome, txid = "indeterminate", None
            elif found is None:
                outcome, txid = "absent", None
            else:
                outcome, txid = "found", found[0]

            probes.append(
                {"probe": path, "sweep": sweep, "outcome": outcome, "txid": txid}
            )
            log.info(
                "kraken_submission_probe",
                probe=path,
                sweep=sweep,
                client_order_id=client_order_id,
                outcome=outcome,
                txid=txid,
            )
        return probes

    async def resolve_order_submission(
        self, *, client_order_id: str
    ) -> Literal["accepted", "not_accepted", "unresolved"]:
        """Decide whether an ambiguous ``AddOrder`` actually landed.

        The mandatory follow-up to ``KrakenAmbiguousSubmissionError``.

        The verdict is derived ONLY from what Kraken reports now — the
        original send's failure is never evidence. A timeout, a 502 and a
        dropped connection are all compatible with the order having been
        accepted, so inferring ``not_accepted`` from the send failure is
        exactly the reasoning that produces a duplicate live order.

        See ``resolve_order_submission_detail`` for the per-probe record.
        """
        detail = await self.resolve_order_submission_detail(
            client_order_id=client_order_id
        )
        verdict: Literal["accepted", "not_accepted", "unresolved"] = detail["verdict"]
        return verdict

    # ---------------- fills ----------------
    async def fetch_order_fills(self, *, txid: str) -> list[dict[str, Any]]:
        """Per-fill price / quantity / fee for one order, for the evidence pack.

        Two hops, because Kraken splits them: ``QueryOrders(trades=true)``
        returns the order's trade-id array, then ``QueryTrades`` resolves
        those ids into executions. Trade ids are chunked to Kraken's
        documented 20-per-call ceiling.

        Every numeric is returned as a fixed-point Decimal STRING, not a
        float — these rows are the fee/price record for a real trade, and
        binary floats round them.

        Returns ``[]`` when the order has no executions. Fills come back in
        the order Kraken listed them on the order row.
        """
        body = await self._private_post(
            "/0/private/QueryOrders", {"txid": txid, "trades": "true"}
        )
        row = body.get(txid)
        if not isinstance(row, dict):
            row = next((v for v in body.values() if isinstance(v, dict)), None)
        if row is None:
            log.info("kraken_order_fills_no_order", txid=txid)
            return []

        trade_ids = [str(t) for t in (row.get("trades") or []) if str(t)]
        if not trade_ids:
            return []

        by_id: dict[str, Any] = {}
        for start in range(0, len(trade_ids), _MAX_QUERY_TRADES_IDS):
            chunk = trade_ids[start : start + _MAX_QUERY_TRADES_IDS]
            chunk_body = await self._private_post(
                "/0/private/QueryTrades", {"txid": ",".join(chunk)}
            )
            by_id.update(chunk_body)

        fills: list[dict[str, Any]] = []
        for trade_id in trade_ids:
            trade = by_id.get(trade_id)
            if not isinstance(trade, dict):
                log.warning("kraken_order_fill_missing", txid=txid, trade_id=trade_id)
                continue
            fills.append(
                {
                    "trade_id": trade_id,
                    "ordertxid": str(trade.get("ordertxid") or "") or None,
                    "pair": str(trade.get("pair") or "") or None,
                    "time": trade.get("time"),
                    "type": str(trade.get("type") or "") or None,
                    "ordertype": str(trade.get("ordertype") or "") or None,
                    "price": _decimal_str(trade.get("price")),
                    "cost": _decimal_str(trade.get("cost")),
                    "fee": _decimal_str(trade.get("fee")),
                    "vol": _decimal_str(trade.get("vol")),
                    "maker": trade.get("maker"),
                }
            )
        log.info("kraken_order_fills", txid=txid, fill_count=len(fills))
        return fills

    # ---------------- fill confirmation ----------------
    async def await_fill_confirmation(
        self, *, venue_order_id: str, client_order_id: str, timeout_sec: float
    ) -> OrderConfirmation:
        """Poll until the order is terminal or ``timeout_sec`` elapses.

        Never raises on timeout — returns ``status='timeout'``, matching the
        Binance implementation, so the caller writes a needs-manual-review
        row rather than handling an exception on a path where the money has
        already moved.

        Polls ``QueryOrders`` by txid when ``venue_order_id`` is known (the
        precise, single-order lookup) and falls back to the OpenOrders /
        ClosedOrders client-id path when it is not — which is the state after
        an ambiguous submission that ``resolve_order_submission`` has not yet
        turned into a txid.

        Poll interval is ``KRAKEN_FILL_POLL_INTERVAL_SEC``. A failed poll ends
        the loop rather than burning the remaining window on an endpoint that
        just refused us; the result is ``timeout``, i.e. unresolved, which is
        the honest answer.
        """
        interval = float(getattr(self._settings, "KRAKEN_FILL_POLL_INTERVAL_SEC", 2.0))
        loop = asyncio.get_event_loop()
        deadline = loop.time() + float(timeout_sec)
        last_row: dict[str, Any] | None = None

        while loop.time() < deadline:
            try:
                conf = await self._poll_order_state(
                    venue_order_id=venue_order_id, client_order_id=client_order_id
                )
            except (KrakenAPIError, KrakenRateLimitError, VenueTransientError) as exc:
                log.warning(
                    "kraken_await_fill_poll_failed",
                    client_order_id=client_order_id,
                    venue_order_id=venue_order_id,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                break

            if conf is not None:
                last_row = conf.raw_response
                if conf.status in ("filled", "partial", "rejected"):
                    return conf

            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(interval, remaining))

        log.warning(
            "kraken_await_fill_timeout",
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            timeout_sec=timeout_sec,
        )
        return OrderConfirmation(
            venue=self.venue_name,
            venue_order_id=venue_order_id or None,
            client_order_id=client_order_id,
            status="timeout",
            filled_qty=None,
            fill_price=None,
            raw_response=last_row,
        )

    async def _poll_order_state(
        self, *, venue_order_id: str, client_order_id: str
    ) -> OrderConfirmation | None:
        """One order-state read, by txid when available else by client id."""
        if not venue_order_id:
            return await self.fetch_order_by_client_id(
                pair="", client_order_id=client_order_id
            )
        body = await self._private_post(
            "/0/private/QueryOrders", {"txid": venue_order_id, "trades": "true"}
        )
        row = body.get(venue_order_id)
        if not isinstance(row, dict):
            row = next((v for v in body.values() if isinstance(v, dict)), None)
        if not isinstance(row, dict):
            return None
        return self._confirmation_from_order(
            venue_order_id, row, client_order_id=client_order_id
        )

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


def _to_decimal(value: Any) -> Decimal | None:
    """Best-effort Decimal; ``None`` on missing or malformed input.

    The exact-arithmetic counterpart to ``_to_float`` — used wherever a
    Kraken number feeds an order decision (minimums, tick multiples, VWAP)
    rather than a display or gate threshold.
    """
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation):
        return None


def _decimal_str(value: Any) -> str | None:
    """Kraken number → fixed-point string, or ``None`` if unparseable."""
    parsed = _to_decimal(value)
    return format(parsed, "f") if parsed is not None else None


def _fee_row_from_map(fees: Any, pair: str) -> dict[str, Any] | None:
    """Pull one pair's row out of a TradeVolume ``fees``/``fees_maker`` map.

    The map is keyed by the AssetPairs KEY (``XXBTZUSD``) while callers hold
    the altname (``XBTUSD``), so an exact-key hit is tried first and a
    single-entry map is then accepted on its own — we asked about one pair,
    so one entry is unambiguous. A multi-entry map with no exact hit returns
    ``None`` rather than guessing which pair's rate to charge the operator.

    The whole row is returned rather than just the rate: ``fee`` is the
    current tier and ``nextfee`` / ``nextvolume`` describe the next one, and
    both come from the same row.
    """
    if not isinstance(fees, dict) or not fees:
        return None
    entry = fees.get(pair)
    if not isinstance(entry, dict):
        if len(fees) != 1:
            return None
        entry = next(iter(fees.values()))
        if not isinstance(entry, dict):
            return None
    return entry


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
