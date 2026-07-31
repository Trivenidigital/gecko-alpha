"""PR-K2: KrakenSpotAdapter order lifecycle (place / cancel / lookup / fills).

Same aioresponses style as tests/test_live_kraken_adapter.py. Payload shapes
follow the AddOrder / CancelOrder / OpenOrders / ClosedOrders / QueryOrders /
QueryTrades response schemas as documented on 2026-07-31 — the txid-keyed
maps, the `error`/`result` envelope, and the numeric-strings convention are
all as the venue sends them.

The load-bearing assertions here are the negative ones: exactly ONE HTTP call
on a failed AddOrder (a retry is a double-order), zero AddOrder POSTs when a
local check rejects, and never inferring "not accepted" from a send failure.
"""

from __future__ import annotations

import re
from decimal import Decimal
from urllib.parse import parse_qs

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.config import Settings
from scout.live.exceptions import VenueTransientError
from scout.live.kraken_adapter import (
    KrakenAmbiguousSubmissionError,
    KrakenAPIError,
    KrakenAuthError,
    KrakenClientOrderIdError,
    KrakenSpotAdapter,
    _format_decimal,
    _validate_cl_ord_id,
)
from scout.live.kraken_signing import reset_nonce_sources_for_tests

_ASSETPAIRS_RE = re.compile(r"https://api\.kraken\.com/0/public/AssetPairs.*")
_ADD_ORDER_URL = "https://api.kraken.com/0/private/AddOrder"
_CANCEL_URL = "https://api.kraken.com/0/private/CancelOrder"
_OPEN_ORDERS_URL = "https://api.kraken.com/0/private/OpenOrders"
_CLOSED_ORDERS_URL = "https://api.kraken.com/0/private/ClosedOrders"
_QUERY_ORDERS_URL = "https://api.kraken.com/0/private/QueryOrders"
_QUERY_TRADES_URL = "https://api.kraken.com/0/private/QueryTrades"

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")

# Base64 so the signing primitive can decode it; not a real credential.
_TEST_SECRET = "dGVzdC1rcmFrZW4tc2VjcmV0LWZvci11bml0LXRlc3Rz"

_CID = "gecko-k-000001"
_TXID = "OQCLML-BW3P3-BUCMWZ"

# Real XXBTZUSD row (trimmed to the fields the order path reads).
_XBTUSD_ROW = {
    "altname": "XBTUSD",
    "base": "XXBT",
    "quote": "ZUSD",
    "pair_decimals": 1,
    "lot_decimals": 8,
    "ordermin": "0.00005",
    "costmin": "0.5",
    "tick_size": "0.1",
    "status": "online",
}


def _settings(**overrides):
    base = dict(
        KRAKEN_API_KEY="testkey",
        KRAKEN_API_SECRET=_TEST_SECRET,
        LIVE_USE_REAL_SIGNED_REQUESTS=True,
        KRAKEN_FILL_POLL_INTERVAL_SEC=0.01,
        # Real settle delay would add 3s to every resolver test; the two-sweep
        # requirement itself is asserted in
        # test_resolve_requires_two_clean_sweeps_before_not_accepted.
        KRAKEN_SUBMISSION_SETTLE_SEC=0.0,
    )
    base.update(overrides)
    return Settings(_env_file=None, **_REQUIRED, **base)


async def _no_sleep(_delay: float) -> None:
    """Swap in for asyncio.sleep so retry tests don't burn real seconds."""
    return None


def _adapter(**overrides) -> KrakenSpotAdapter:
    reset_nonce_sources_for_tests()
    adapter = KrakenSpotAdapter(_settings(**overrides), db=None)
    adapter._retry_sleep = _no_sleep
    return adapter


def _calls(mocked: aioresponses) -> list:
    return [call for calls in mocked.requests.values() for call in calls]


def _calls_to(mocked: aioresponses, url: str) -> list:
    return [
        call
        for key, calls in mocked.requests.items()
        if str(key[1]) == url
        for call in calls
    ]


def _body_of(call) -> dict[str, str]:
    """Form-decode a captured POST body into a flat dict."""
    raw = call.kwargs.get("data")
    return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}


def _mock_assetpairs(m: aioresponses, row: dict | None = None, *, repeat=True) -> None:
    payload = {"error": [], "result": {"XXBTZUSD": row if row else _XBTUSD_ROW}}
    m.get(_ASSETPAIRS_RE, status=200, payload=payload, repeat=repeat)


def _order_row(**overrides) -> dict:
    """A ClosedOrders/OpenOrders order row with our cl_ord_id."""
    row = {
        "cl_ord_id": _CID,
        "status": "open",
        "descr": {"pair": "XBTUSD", "type": "buy", "ordertype": "limit"},
        "vol": "0.00050000",
        "vol_exec": "0.00000000",
        "cost": "0.00000",
        "fee": "0.00000",
        "price": "0.0",
    }
    row.update(overrides)
    return row


# ======================================================================
# _format_decimal — the urlencode/scientific-notation trap
# ======================================================================


@pytest.mark.parametrize(
    ("value", "decimals", "expected"),
    [
        # The trap itself: str(0.00001) is '1e-05' and str(Decimal('1E-5'))
        # is '1E-5'; both are rejected by Kraken.
        (Decimal("0.00001"), 5, "0.00001"),
        (Decimal("1E-5"), 5, "0.00001"),
        (Decimal("0.00001"), 8, "0.00001000"),
        (Decimal("1E-8"), 8, "0.00000001"),
        (Decimal("30000"), 1, "30000.0"),
        (Decimal("30000.567"), 1, "30000.5"),  # ROUND_DOWN, not nearest
        (Decimal("1000000"), 0, "1000000"),
    ],
)
def test_format_decimal_never_emits_scientific_notation(value, decimals, expected):
    formatted = _format_decimal(value, decimals)
    assert formatted == expected
    assert "e" not in formatted.lower()


# ======================================================================
# cl_ord_id validation — Kraken's three documented forms
# ======================================================================


@pytest.mark.parametrize(
    "cid",
    [
        "arb-20240509-00010",  # doc example, 18 chars exactly
        "gecko-k-000001",
        "6d1b345e-2821-40e2-ad83-4ecb18a06876",  # long UUID
        "da8e4ad59b78481c93e589746b0cf91f",  # short UUID (32 hex)
    ],
)
def test_validate_cl_ord_id_accepts_documented_forms(cid):
    _validate_cl_ord_id(cid)


@pytest.mark.parametrize(
    "cid",
    [
        "",
        "a" * 19,  # one over the free-text ceiling
        "has space",
        "tab\there",
    ],
)
def test_validate_cl_ord_id_rejects_bad_forms(cid):
    with pytest.raises(KrakenClientOrderIdError):
        _validate_cl_ord_id(cid)


def test_binance_shaped_client_order_id_does_not_fit_kraken():
    """PIN: make_client_order_id() targets Binance's 28-char ceiling, which
    overflows Kraken's 18-char free-text form. PR-K3's runner must mint a
    Kraken-shaped id; this test fails the day someone wires the Binance
    helper into the Kraken lane."""
    from scout.live.idempotency import make_client_order_id

    cid = make_client_order_id(99999999, "abcd1234-ef56-7890-abcd-ef0123456789")
    assert len(cid) > 18
    with pytest.raises(KrakenClientOrderIdError, match="18"):
        _validate_cl_ord_id(cid)


# ======================================================================
# place_limit_order — happy paths
# ======================================================================


@pytest.mark.asyncio
async def test_place_limit_order_sends_fixed_point_strings():
    adapter = _adapter()
    with aioresponses() as m:
        _mock_assetpairs(m)
        m.post(
            _ADD_ORDER_URL,
            status=200,
            payload={
                "error": [],
                "result": {
                    "descr": {"order": "buy 0.00050000 XBTUSD @ limit 30000.0"},
                    "txid": [_TXID],
                },
            },
        )
        result = await adapter.place_limit_order(
            pair="XBTUSD",
            side="buy",
            price=Decimal("30000.0"),
            volume=Decimal("0.0005"),
            client_order_id=_CID,
        )

        body = _body_of(_calls_to(m, _ADD_ORDER_URL)[0])
        assert body["pair"] == "XBTUSD"
        assert body["type"] == "buy"
        assert body["ordertype"] == "limit"
        assert body["price"] == "30000.0"
        # lot_decimals=8 — and critically NOT '0.0005' rendered as '5e-04'.
        assert body["volume"] == "0.00050000"
        assert body["cl_ord_id"] == _CID
        # A real order must NOT carry the validate flag.
        assert "validate" not in body
        assert "nonce" in body

    assert result["txid"] == [_TXID]
    assert result["validate_only"] is False
    assert result["descr"]["order"].startswith("buy")
    await adapter.close()


@pytest.mark.asyncio
async def test_place_limit_order_tiny_volume_is_not_scientific_notation():
    """The exact trap from the PR-K1 handoff note: 0.00001 must go on the
    wire as '0.00001000', never '1e-05'."""
    adapter = _adapter()
    with aioresponses() as m:
        # ordermin lowered so the value under test clears the minimum and the
        # assertion is about formatting, not validation.
        _mock_assetpairs(m, dict(_XBTUSD_ROW, ordermin="0.00001"))
        m.post(
            _ADD_ORDER_URL,
            status=200,
            payload={"error": [], "result": {"descr": {}, "txid": [_TXID]}},
        )
        await adapter.place_limit_order(
            pair="XBTUSD",
            side="buy",
            price=Decimal("60000"),
            volume=Decimal("0.00001"),
            client_order_id=_CID,
        )
        body = _body_of(_calls_to(m, _ADD_ORDER_URL)[0])
        assert body["volume"] == "0.00001000"
        assert "e" not in body["volume"].lower()
    await adapter.close()


@pytest.mark.asyncio
async def test_place_limit_order_sell_rounds_price_up():
    """Conservative per-side rounding: a sell never offers below the price
    the caller asked for."""
    adapter = _adapter()
    with aioresponses() as m:
        _mock_assetpairs(m)
        m.post(
            _ADD_ORDER_URL,
            status=200,
            payload={"error": [], "result": {"descr": {}, "txid": [_TXID]}},
        )
        await adapter.place_limit_order(
            pair="XBTUSD",
            side="sell",
            price=Decimal("30000.55"),
            volume=Decimal("0.0005"),
            client_order_id=_CID,
        )
        body = _body_of(_calls_to(m, _ADD_ORDER_URL)[0])
        assert body["type"] == "sell"
        assert body["price"] == "30000.6"
    await adapter.close()


@pytest.mark.asyncio
async def test_validate_only_sends_validate_flag_and_needs_no_live_flag():
    """validate=true is a Kraken-side dry run — it cannot trade, so it runs
    with LIVE_USE_REAL_SIGNED_REQUESTS=False. That is what lets the Aug 5
    rehearsal exercise this exact codepath."""
    adapter = _adapter(LIVE_USE_REAL_SIGNED_REQUESTS=False)
    with aioresponses() as m:
        _mock_assetpairs(m)
        m.post(
            _ADD_ORDER_URL,
            status=200,
            payload={
                "error": [],
                "result": {"descr": {"order": "buy 0.00050000 XBTUSD @ limit 30000.0"}},
            },
        )
        result = await adapter.place_limit_order(
            pair="XBTUSD",
            side="buy",
            price=Decimal("30000.0"),
            volume=Decimal("0.0005"),
            client_order_id=_CID,
            validate_only=True,
        )
        body = _body_of(_calls_to(m, _ADD_ORDER_URL)[0])
        assert body["validate"] == "true"

    assert result["validate_only"] is True
    # No order was placed, so Kraken returns descr with no txid.
    assert result["txid"] == []
    assert result["descr"]["order"]
    await adapter.close()


# ======================================================================
# place_limit_order — gating and local rejection (no AddOrder POST)
# ======================================================================


@pytest.mark.asyncio
async def test_real_order_requires_live_signed_flag_and_sends_nothing():
    adapter = _adapter(LIVE_USE_REAL_SIGNED_REQUESTS=False)
    with aioresponses() as m:
        with pytest.raises(NotImplementedError, match="LIVE_USE_REAL_SIGNED_REQUESTS"):
            await adapter.place_limit_order(
                pair="XBTUSD",
                side="buy",
                price=Decimal("30000.0"),
                volume=Decimal("0.0005"),
                client_order_id=_CID,
            )
        assert len(_calls(m)) == 0
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "exc"),
    [
        (dict(side="short"), KrakenAPIError),
        (dict(client_order_id="a" * 30), KrakenClientOrderIdError),
        (dict(price=Decimal("0")), KrakenAPIError),
        (dict(volume=Decimal("-1")), KrakenAPIError),
    ],
)
async def test_metadata_free_rejections_touch_the_network_at_all(kwargs, exc):
    """Checks that need no venue metadata run before any HTTP: a bad side, a
    malformed cl_ord_id or a non-positive amount costs zero requests (and
    zero nonces)."""
    adapter = _adapter()
    call = dict(
        pair="XBTUSD",
        side="buy",
        price=Decimal("30000.0"),
        volume=Decimal("0.0005"),
        client_order_id=_CID,
    )
    call.update(kwargs)
    with aioresponses() as m:
        with pytest.raises(exc):
            await adapter.place_limit_order(**call)
        assert len(_calls(m)) == 0
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("price", "volume", "match"),
    [
        # ordermin is 0.00005 — this clears costmin but not ordermin.
        (Decimal("30000.0"), Decimal("0.00001"), "ordermin"),
        # costmin is 0.5 USD — clears ordermin but the cost is ~0.003 USD.
        (Decimal("60.0"), Decimal("0.00005"), "costmin"),
        # tick_size is 0.1 and pair_decimals is 1, so a sub-tick price
        # quantizes away rather than being sent.
        (Decimal("0.04"), Decimal("1"), "quantized to zero"),
    ],
)
async def test_local_minimum_rejection_sends_no_add_order(price, volume, match):
    """A doomed order is refused locally rather than round-tripped — that
    keeps a guaranteed reject from burning a nonce and an AddOrder slot."""
    adapter = _adapter()
    with aioresponses() as m:
        _mock_assetpairs(m)
        with pytest.raises(KrakenAPIError, match=match):
            await adapter.place_limit_order(
                pair="XBTUSD",
                side="buy",
                price=price,
                volume=volume,
                client_order_id=_CID,
            )
        assert _calls_to(m, _ADD_ORDER_URL) == []
    await adapter.close()


@pytest.mark.asyncio
async def test_price_off_the_tick_grid_is_rejected_locally():
    """A pair whose tick is coarser than its price precision: quantizing to
    pair_decimals is not enough, so divisibility is checked explicitly."""
    adapter = _adapter()
    coarse = dict(_XBTUSD_ROW, pair_decimals=2, tick_size="0.05")
    with aioresponses() as m:
        _mock_assetpairs(m, coarse)
        with pytest.raises(KrakenAPIError, match="tick_size"):
            await adapter.place_limit_order(
                pair="XBTUSD",
                side="buy",
                price=Decimal("30000.03"),
                volume=Decimal("0.0005"),
                client_order_id=_CID,
            )
        assert _calls_to(m, _ADD_ORDER_URL) == []
    await adapter.close()


@pytest.mark.asyncio
async def test_unknown_pair_is_rejected_before_add_order():
    adapter = _adapter()
    with aioresponses() as m:
        m.get(
            _ASSETPAIRS_RE,
            status=200,
            payload={"error": ["EQuery:Unknown asset pair"]},
            repeat=True,
        )
        with pytest.raises(KrakenAPIError, match="unknown pair"):
            await adapter.place_limit_order(
                pair="NOPEUSD",
                side="buy",
                price=Decimal("1"),
                volume=Decimal("1"),
                client_order_id=_CID,
            )
        assert _calls_to(m, _ADD_ORDER_URL) == []
    await adapter.close()


@pytest.mark.asyncio
async def test_non_online_pair_is_rejected_before_add_order():
    adapter = _adapter()
    with aioresponses() as m:
        _mock_assetpairs(m, dict(_XBTUSD_ROW, status="cancel_only"))
        with pytest.raises(KrakenAPIError, match="cancel_only"):
            await adapter.place_limit_order(
                pair="XBTUSD",
                side="buy",
                price=Decimal("30000.0"),
                volume=Decimal("0.0005"),
                client_order_id=_CID,
            )
        assert _calls_to(m, _ADD_ORDER_URL) == []
    await adapter.close()


# ======================================================================
# place_limit_order — venue rejection vs transport ambiguity
# ======================================================================


@pytest.mark.asyncio
async def test_insufficient_funds_is_typed_and_never_retried():
    """A refusal from the matching engine is definitive: the order does NOT
    exist, and exactly one POST must have left the box."""
    adapter = _adapter()
    with aioresponses() as m:
        _mock_assetpairs(m)
        m.post(
            _ADD_ORDER_URL,
            status=200,
            payload={"error": ["EOrder:Insufficient funds"]},
            repeat=True,
        )
        with pytest.raises(KrakenAPIError, match="Insufficient funds") as excinfo:
            await adapter.place_limit_order(
                pair="XBTUSD",
                side="buy",
                price=Decimal("30000.0"),
                volume=Decimal("0.0005"),
                client_order_id=_CID,
            )
        assert not isinstance(excinfo.value, KrakenAmbiguousSubmissionError)
        assert len(_calls_to(m, _ADD_ORDER_URL)) == 1
    await adapter.close()


@pytest.mark.asyncio
async def test_duplicate_client_order_id_response_is_typed_and_not_retried():
    """Kraken publishes no duplicate-cl_ord_id contract, so any error naming
    the field is classified rather than swallowed into the generic bucket."""
    adapter = _adapter()
    with aioresponses() as m:
        _mock_assetpairs(m)
        m.post(
            _ADD_ORDER_URL,
            status=200,
            payload={"error": ["EOrder:Duplicate cl_ord_id"]},
            repeat=True,
        )
        with pytest.raises(KrakenClientOrderIdError, match="cl_ord_id"):
            await adapter.place_limit_order(
                pair="XBTUSD",
                side="buy",
                price=Decimal("30000.0"),
                volume=Decimal("0.0005"),
                client_order_id=_CID,
            )
        assert len(_calls_to(m, _ADD_ORDER_URL)) == 1
    await adapter.close()


@pytest.mark.asyncio
async def test_timeout_raises_ambiguous_with_exactly_one_post():
    """A timeout does NOT prove the order was refused — the POST may have
    landed. One attempt only: a retry here is a double-order."""
    import asyncio

    adapter = _adapter()
    with aioresponses() as m:
        _mock_assetpairs(m)
        m.post(_ADD_ORDER_URL, exception=asyncio.TimeoutError(), repeat=True)
        with pytest.raises(KrakenAmbiguousSubmissionError) as excinfo:
            await adapter.place_limit_order(
                pair="XBTUSD",
                side="buy",
                price=Decimal("30000.0"),
                volume=Decimal("0.0005"),
                client_order_id=_CID,
            )
        assert excinfo.value.client_order_id == _CID
        assert "do NOT resend" in str(excinfo.value)
        assert len(_calls_to(m, _ADD_ORDER_URL)) == 1
    await adapter.close()


@pytest.mark.asyncio
async def test_http_502_after_post_is_ambiguous_not_retriable():
    """A 502 from an edge node says nothing about what the origin did with
    the POST. It is ambiguous, NOT transient-retriable."""
    adapter = _adapter()
    with aioresponses() as m:
        _mock_assetpairs(m)
        m.post(_ADD_ORDER_URL, status=502, body="bad gateway", repeat=True)
        with pytest.raises(KrakenAmbiguousSubmissionError) as excinfo:
            await adapter.place_limit_order(
                pair="XBTUSD",
                side="buy",
                price=Decimal("30000.0"),
                volume=Decimal("0.0005"),
                client_order_id=_CID,
            )
        assert excinfo.value.client_order_id == _CID
        assert len(_calls_to(m, _ADD_ORDER_URL)) == 1
    await adapter.close()


@pytest.mark.asyncio
async def test_service_unavailable_on_add_order_is_ambiguous_and_not_retried():
    """EService:Unavailable is the retryable class everywhere EXCEPT here —
    AddOrder is not idempotent and the nonce is regenerated per attempt, so
    Kraken cannot dedup a replay."""
    adapter = _adapter()
    with aioresponses() as m:
        _mock_assetpairs(m)
        m.post(
            _ADD_ORDER_URL,
            status=200,
            payload={"error": ["EService:Unavailable"]},
            repeat=True,
        )
        with pytest.raises(KrakenAmbiguousSubmissionError):
            await adapter.place_limit_order(
                pair="XBTUSD",
                side="buy",
                price=Decimal("30000.0"),
                volume=Decimal("0.0005"),
                client_order_id=_CID,
            )
        assert len(_calls_to(m, _ADD_ORDER_URL)) == 1
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc_factory",
    [
        # None of these inherit from ClientConnectorError — the connect-phase
        # error that is the ONLY provably-nothing-was-sent case. Every one of
        # them can occur after the POST reached Kraken.
        lambda: aiohttp.ServerDisconnectedError(),
        lambda: aiohttp.ClientOSError(104, "Connection reset by peer"),
        lambda: aiohttp.ClientConnectionResetError("cannot write to closing"),
        lambda: aiohttp.ClientPayloadError("response payload is not completed"),
    ],
)
async def test_midflight_transport_failures_are_ambiguous(exc_factory):
    """ClientPayloadError is the clearest of these: the headers already
    arrived, so the request PROVABLY reached Kraken and only the body read
    failed. Narrow exception handling let all of these escape unclassified."""
    adapter = _adapter()
    with aioresponses() as m:
        _mock_assetpairs(m)
        m.post(_ADD_ORDER_URL, exception=exc_factory(), repeat=True)
        with pytest.raises(KrakenAmbiguousSubmissionError) as excinfo:
            await adapter.place_limit_order(
                pair="XBTUSD",
                side="buy",
                price=Decimal("30000.0"),
                volume=Decimal("0.0005"),
                client_order_id=_CID,
            )
        assert excinfo.value.client_order_id == _CID
        assert len(_calls_to(m, _ADD_ORDER_URL)) == 1
    await adapter.close()


@pytest.mark.asyncio
async def test_validate_only_failure_is_never_ambiguous():
    """validate=true cannot trade, so however it failed, no order exists.
    Calling the rehearsal ambiguous would send an operator hunting for an
    order that cannot have been created."""
    import asyncio

    adapter = _adapter(LIVE_USE_REAL_SIGNED_REQUESTS=False)
    with aioresponses() as m:
        _mock_assetpairs(m)
        m.post(_ADD_ORDER_URL, exception=asyncio.TimeoutError(), repeat=True)
        with pytest.raises(VenueTransientError):
            await adapter.place_limit_order(
                pair="XBTUSD",
                side="buy",
                price=Decimal("30000.0"),
                volume=Decimal("0.0005"),
                client_order_id=_CID,
                validate_only=True,
            )
        assert len(_calls_to(m, _ADD_ORDER_URL)) == 1
    await adapter.close()


@pytest.mark.asyncio
async def test_signed_post_does_not_follow_redirects():
    """aiohttp follows 307/308 by re-sending the same method AND body — a
    second AddOrder transmission inside one attempt, invisible to
    retry_transient=False — and forwards every header it did not strip, which
    does not include API-Key/API-Sign."""
    adapter = _adapter()
    with aioresponses() as m:
        _mock_assetpairs(m)
        m.post(
            _ADD_ORDER_URL,
            status=307,
            headers={"Location": "https://evil.example/0/private/AddOrder"},
            body="",
            repeat=True,
        )
        with pytest.raises(KrakenAmbiguousSubmissionError):
            await adapter.place_limit_order(
                pair="XBTUSD",
                side="buy",
                price=Decimal("30000.0"),
                volume=Decimal("0.0005"),
                client_order_id=_CID,
            )
        add_order_calls = _calls_to(m, _ADD_ORDER_URL)
        assert len(add_order_calls) == 1
        assert add_order_calls[0].kwargs.get("allow_redirects") is False
    await adapter.close()


@pytest.mark.asyncio
async def test_http_4xx_error_does_not_carry_signed_headers():
    """aiohttp's ClientResponseError repr embeds request_info.headers, i.e.
    the live API-Key and API-Sign. _request raises a string it controls
    instead, so a caller logging repr(exc) cannot leak credentials."""
    adapter = _adapter(KRAKEN_API_KEY="LIVEKEY123")
    with aioresponses() as m:
        _mock_assetpairs(m)
        m.post(_ADD_ORDER_URL, status=403, body="forbidden", repeat=True)
        with pytest.raises(Exception) as excinfo:
            await adapter.place_limit_order(
                pair="XBTUSD",
                side="buy",
                price=Decimal("30000.0"),
                volume=Decimal("0.0005"),
                client_order_id=_CID,
            )
        rendered = f"{excinfo.value!r} {excinfo.value!s}"
        assert not isinstance(excinfo.value, aiohttp.ClientResponseError)
        assert "LIVEKEY123" not in rendered
        assert "API-Sign" not in rendered
    await adapter.close()


@pytest.mark.asyncio
async def test_clean_envelope_without_txid_is_ambiguous():
    """No error and no txid violates the documented shape — the order may
    exist, so it must not come back as a plain failure a caller would retry."""
    adapter = _adapter()
    with aioresponses() as m:
        _mock_assetpairs(m)
        m.post(
            _ADD_ORDER_URL,
            status=200,
            payload={"error": [], "result": {"descr": {"order": "buy ..."}}},
        )
        with pytest.raises(KrakenAmbiguousSubmissionError):
            await adapter.place_limit_order(
                pair="XBTUSD",
                side="buy",
                price=Decimal("30000.0"),
                volume=Decimal("0.0005"),
                client_order_id=_CID,
            )
    await adapter.close()


# ======================================================================
# resolve_order_submission
# ======================================================================


def _mock_probe(
    m: aioresponses, url: str, orders: dict, key: str, *, repeat: bool = True
) -> None:
    """Register a probe reply. repeat=True by default because
    resolve_order_submission sweeps every endpoint twice."""
    m.post(
        url,
        status=200,
        payload={"error": [], "result": {key: orders, "count": 0}},
        repeat=repeat,
    )


@pytest.mark.asyncio
async def test_resolve_found_in_open_orders_is_accepted():
    adapter = _adapter()
    with aioresponses() as m:
        _mock_probe(m, _OPEN_ORDERS_URL, {_TXID: _order_row()}, "open")
        _mock_probe(m, _CLOSED_ORDERS_URL, {}, "closed")
        detail = await adapter.resolve_order_submission_detail(client_order_id=_CID)
    assert detail["verdict"] == "accepted"
    assert detail["txid"] == [_TXID]
    # Both probes are recorded even though the first one answered.
    assert [p["outcome"] for p in detail["probes"]] == ["found", "absent"]
    await adapter.close()


@pytest.mark.asyncio
async def test_resolve_found_in_closed_orders_is_accepted():
    adapter = _adapter()
    with aioresponses() as m:
        _mock_probe(m, _OPEN_ORDERS_URL, {}, "open")
        _mock_probe(
            m,
            _CLOSED_ORDERS_URL,
            {_TXID: _order_row(status="closed", vol_exec="0.00050000")},
            "closed",
        )
        verdict = await adapter.resolve_order_submission(client_order_id=_CID)
    assert verdict == "accepted"
    await adapter.close()


@pytest.mark.asyncio
async def test_resolve_absent_from_all_green_probes_is_not_accepted():
    adapter = _adapter()
    with aioresponses() as m:
        _mock_probe(m, _OPEN_ORDERS_URL, {}, "open")
        _mock_probe(m, _CLOSED_ORDERS_URL, {}, "closed")
        detail = await adapter.resolve_order_submission_detail(client_order_id=_CID)
    assert detail["verdict"] == "not_accepted"
    assert detail["txid"] == []
    assert all(p["outcome"] == "absent" for p in detail["probes"])
    await adapter.close()


@pytest.mark.asyncio
async def test_resolve_is_unresolved_when_a_probe_times_out():
    """'We could not look' is never 'we looked and it is not there' — only
    the second makes a resend safe."""
    import asyncio

    adapter = _adapter()
    with aioresponses() as m:
        _mock_probe(m, _OPEN_ORDERS_URL, {}, "open")
        m.post(_CLOSED_ORDERS_URL, exception=asyncio.TimeoutError(), repeat=True)
        detail = await adapter.resolve_order_submission_detail(client_order_id=_CID)
    assert detail["verdict"] == "unresolved"
    outcomes = {p["probe"]: p["outcome"] for p in detail["probes"]}
    assert outcomes["/0/private/OpenOrders"] == "absent"
    assert outcomes["/0/private/ClosedOrders"] == "error"
    await adapter.close()


@pytest.mark.asyncio
async def test_resolve_is_unresolved_on_auth_error_and_never_retries():
    adapter = _adapter()
    with aioresponses() as m:
        m.post(
            _OPEN_ORDERS_URL,
            status=200,
            payload={"error": ["EAPI:Invalid key"]},
            repeat=True,
        )
        m.post(
            _CLOSED_ORDERS_URL,
            status=200,
            payload={"error": ["EAPI:Invalid key"]},
            repeat=True,
        )
        detail = await adapter.resolve_order_submission_detail(client_order_id=_CID)
        assert detail["verdict"] == "unresolved"
        # Auth failures are never retried — retrying burns nonces and risks
        # EGeneral:Temporary lockout on the whole key.
        assert len(_calls_to(m, _OPEN_ORDERS_URL)) == 1
        assert len(_calls_to(m, _CLOSED_ORDERS_URL)) == 1
    assert all(p["error_type"] == "KrakenAuthError" for p in detail["probes"])
    await adapter.close()


@pytest.mark.asyncio
async def test_resolve_ignores_orders_with_a_different_client_id():
    """If Kraken ever drops the cl_ord_id filter it returns the FULL order
    list; trusting the filter would confirm a stranger's order as ours."""
    adapter = _adapter()
    with aioresponses() as m:
        _mock_probe(
            m,
            _OPEN_ORDERS_URL,
            {"OTHER-TXID": _order_row(cl_ord_id="someone-else")},
            "open",
        )
        _mock_probe(m, _CLOSED_ORDERS_URL, {}, "closed")
        detail = await adapter.resolve_order_submission_detail(client_order_id=_CID)
    assert detail["verdict"] == "not_accepted"
    await adapter.close()


@pytest.mark.asyncio
async def test_resolve_requires_two_clean_sweeps_before_not_accepted():
    """A just-accepted order is briefly in NEITHER container (Kraken's
    `pending` state, plus the OpenOrders propagation window). One clean-empty
    sweep cannot tell that apart from a genuine reject, so not_accepted — the
    verdict that licenses a resend — needs a second sweep after a settle
    delay. Here the order appears on the second sweep."""
    adapter = _adapter()
    with aioresponses() as m:
        # Sweep 0: nothing anywhere.
        _mock_probe(m, _OPEN_ORDERS_URL, {}, "open", repeat=False)
        _mock_probe(m, _CLOSED_ORDERS_URL, {}, "closed", repeat=False)
        # Sweep 1: the order has become visible.
        _mock_probe(m, _OPEN_ORDERS_URL, {_TXID: _order_row()}, "open", repeat=False)
        _mock_probe(m, _CLOSED_ORDERS_URL, {}, "closed", repeat=False)
        detail = await adapter.resolve_order_submission_detail(client_order_id=_CID)

    assert detail["verdict"] == "accepted"
    assert detail["txid"] == [_TXID]
    assert len(detail["sweeps"]) == 2
    await adapter.close()


@pytest.mark.asyncio
async def test_resolve_not_accepted_records_both_sweeps():
    adapter = _adapter()
    with aioresponses() as m:
        _mock_probe(m, _OPEN_ORDERS_URL, {}, "open")
        _mock_probe(m, _CLOSED_ORDERS_URL, {}, "closed")
        detail = await adapter.resolve_order_submission_detail(client_order_id=_CID)
    assert detail["verdict"] == "not_accepted"
    assert len(detail["sweeps"]) == 2
    assert all(p["outcome"] == "absent" for p in detail["probes"])
    await adapter.close()


@pytest.mark.asyncio
async def test_resolve_stops_at_the_first_sweep_once_found():
    """No reason to spend a settle delay and four more signed calls proving
    something already proven."""
    adapter = _adapter()
    with aioresponses() as m:
        _mock_probe(m, _OPEN_ORDERS_URL, {_TXID: _order_row()}, "open")
        _mock_probe(m, _CLOSED_ORDERS_URL, {}, "closed")
        detail = await adapter.resolve_order_submission_detail(client_order_id=_CID)
        assert len(_calls_to(m, _OPEN_ORDERS_URL)) == 1
    assert detail["verdict"] == "accepted"
    assert len(detail["sweeps"]) == 1
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        None,  # {"error": [], "result": null}
        {"open": None},  # container present but null
        {"open": {"SOME-TXID": {"status": "open", "vol": "1.0"}}},  # no cl_ord_id echo
    ],
)
async def test_unsearchable_payload_is_never_read_as_absence(result):
    """The sharpest fail-open in the resolver: a payload that cannot answer
    the question must not contribute to not_accepted. The third case is the
    dangerous one — the server-side filter MATCHED (so the order exists) but
    the row omits the optional cl_ord_id echo."""
    adapter = _adapter()
    with aioresponses() as m:
        m.post(
            _OPEN_ORDERS_URL,
            status=200,
            payload={"error": [], "result": result},
            repeat=True,
        )
        _mock_probe(m, _CLOSED_ORDERS_URL, {}, "closed")
        detail = await adapter.resolve_order_submission_detail(client_order_id=_CID)

    assert detail["verdict"] == "unresolved"
    outcomes = {p["probe"]: p["outcome"] for p in detail["probes"]}
    assert outcomes["/0/private/OpenOrders"] == "indeterminate"
    await adapter.close()


@pytest.mark.asyncio
async def test_lookup_raises_rather_than_returning_none_on_unsearchable_payload():
    """fetch_order_by_client_id's None means 'Kraken has no such order'. An
    unreadable payload must not be laundered into that claim."""
    adapter = _adapter()
    with aioresponses() as m:
        m.post(
            _OPEN_ORDERS_URL,
            status=200,
            payload={"error": [], "result": {"open": None}},
            repeat=True,
        )
        with pytest.raises(KrakenAPIError, match="could not be searched"):
            await adapter.fetch_order_by_client_id(pair="XBTUSD", client_order_id=_CID)
    await adapter.close()


@pytest.mark.asyncio
async def test_resolve_sends_cl_ord_id_filter():
    adapter = _adapter()
    with aioresponses() as m:
        _mock_probe(m, _OPEN_ORDERS_URL, {}, "open")
        _mock_probe(m, _CLOSED_ORDERS_URL, {}, "closed")
        await adapter.resolve_order_submission(client_order_id=_CID)
        assert _body_of(_calls_to(m, _OPEN_ORDERS_URL)[0])["cl_ord_id"] == _CID
        assert _body_of(_calls_to(m, _CLOSED_ORDERS_URL)[0])["cl_ord_id"] == _CID
    await adapter.close()


# ======================================================================
# cancel_order
# ======================================================================


@pytest.mark.asyncio
async def test_cancel_by_txid():
    adapter = _adapter()
    with aioresponses() as m:
        m.post(
            _CANCEL_URL,
            status=200,
            payload={"error": [], "result": {"count": 1, "pending": False}},
        )
        result = await adapter.cancel_order(txid=_TXID)
        body = _body_of(_calls_to(m, _CANCEL_URL)[0])
        assert body["txid"] == _TXID
        assert "cl_ord_id" not in body
    assert result == {
        "count": 1,
        "pending": False,
        "already_gone": False,
        "txid": _TXID,
        "client_order_id": None,
        "raw": {"count": 1, "pending": False},
    }
    await adapter.close()


@pytest.mark.asyncio
async def test_cancel_by_client_order_id_uses_the_cl_ord_id_field():
    """CancelOrder's `txid` param accepts a txid or a userref — NOT a client
    order id, which is its own separate field."""
    adapter = _adapter()
    with aioresponses() as m:
        m.post(
            _CANCEL_URL,
            status=200,
            payload={"error": [], "result": {"count": 1, "pending": False}},
        )
        result = await adapter.cancel_order(client_order_id=_CID)
        body = _body_of(_calls_to(m, _CANCEL_URL)[0])
        assert body["cl_ord_id"] == _CID
        assert "txid" not in body
    assert result["count"] == 1
    await adapter.close()


@pytest.mark.asyncio
async def test_cancel_prefers_txid_when_both_identities_are_given():
    adapter = _adapter()
    with aioresponses() as m:
        m.post(
            _CANCEL_URL,
            status=200,
            payload={"error": [], "result": {"count": 1, "pending": False}},
        )
        await adapter.cancel_order(txid=_TXID, client_order_id=_CID)
        body = _body_of(_calls_to(m, _CANCEL_URL)[0])
        assert body["txid"] == _TXID
        assert "cl_ord_id" not in body
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["EOrder:Unknown order", "EOrder:Invalid order"])
async def test_cancel_maps_already_gone_to_a_clean_result(error):
    """The post-condition of a cancel is 'this order is not resting'. An
    order that is already gone satisfies it — that is success, not failure."""
    adapter = _adapter()
    with aioresponses() as m:
        m.post(_CANCEL_URL, status=200, payload={"error": [error]}, repeat=True)
        result = await adapter.cancel_order(txid=_TXID)
    assert result["already_gone"] is True
    assert result["count"] == 0
    await adapter.close()


@pytest.mark.asyncio
async def test_cancel_still_raises_on_an_unmapped_error():
    """Kraken publishes no error table entry for unknown orders, so the
    already-gone mapping is a prefix allowlist — anything else raises."""
    adapter = _adapter()
    with aioresponses() as m:
        m.post(
            _CANCEL_URL,
            status=200,
            payload={"error": ["EGeneral:Invalid arguments"]},
            repeat=True,
        )
        with pytest.raises(KrakenAPIError):
            await adapter.cancel_order(txid=_TXID)
        assert len(_calls_to(m, _CANCEL_URL)) == 1
    await adapter.close()


@pytest.mark.asyncio
async def test_cancel_requires_an_identity():
    adapter = _adapter()
    with aioresponses() as m:
        with pytest.raises(KrakenAPIError, match="txid or client_order_id"):
            await adapter.cancel_order()
        assert len(_calls(m)) == 0
    await adapter.close()


@pytest.mark.asyncio
async def test_cancel_is_not_gated_by_the_live_signed_flag():
    """Cancel REDUCES exposure. A flag flip that leaves a resting live order
    uncancellable disables the safety lever."""
    adapter = _adapter(LIVE_USE_REAL_SIGNED_REQUESTS=False)
    with aioresponses() as m:
        m.post(
            _CANCEL_URL,
            status=200,
            payload={"error": [], "result": {"count": 1, "pending": False}},
        )
        result = await adapter.cancel_order(txid=_TXID)
    assert result["count"] == 1
    await adapter.close()


# ======================================================================
# fetch_order_by_client_id — status mapping
# ======================================================================


@pytest.mark.asyncio
async def test_open_order_maps_to_pending():
    adapter = _adapter()
    with aioresponses() as m:
        _mock_probe(m, _OPEN_ORDERS_URL, {_TXID: _order_row()}, "open")
        conf = await adapter.fetch_order_by_client_id(
            pair="XBTUSD", client_order_id=_CID
        )
    assert conf is not None
    assert conf.status == "pending"
    assert conf.venue_order_id == _TXID
    assert conf.client_order_id == _CID
    assert conf.venue == "kraken"
    await adapter.close()


@pytest.mark.asyncio
async def test_closed_fully_filled_maps_to_filled_with_qty_and_price():
    adapter = _adapter()
    row = _order_row(
        status="closed",
        vol="0.00050000",
        vol_exec="0.00050000",
        cost="15.00000",
        fee="0.02400",
        price="30000.0",
    )
    with aioresponses() as m:
        _mock_probe(m, _OPEN_ORDERS_URL, {}, "open")
        _mock_probe(m, _CLOSED_ORDERS_URL, {_TXID: row}, "closed")
        conf = await adapter.fetch_order_by_client_id(
            pair="XBTUSD", client_order_id=_CID
        )
    assert conf is not None
    assert conf.status == "filled"
    assert conf.filled_qty == pytest.approx(0.0005)
    # VWAP from cost/vol_exec, not the raw `price` field.
    assert conf.fill_price == pytest.approx(30000.0)
    await adapter.close()


@pytest.mark.asyncio
async def test_canceled_with_partial_fill_maps_to_partial_not_rejected():
    """A canceled order that partially filled left real inventory behind.
    Calling it 'rejected' would tell the reconciler nothing happened while
    the account actually holds a position."""
    adapter = _adapter()
    row = _order_row(
        status="canceled",
        vol="0.00050000",
        vol_exec="0.00020000",
        cost="6.00000",
        price="30000.0",
        reason="User requested",
    )
    with aioresponses() as m:
        _mock_probe(m, _OPEN_ORDERS_URL, {}, "open")
        _mock_probe(m, _CLOSED_ORDERS_URL, {_TXID: row}, "closed")
        conf = await adapter.fetch_order_by_client_id(
            pair="XBTUSD", client_order_id=_CID
        )
    assert conf is not None
    assert conf.status == "partial"
    assert conf.filled_qty == pytest.approx(0.0002)
    assert conf.fill_price == pytest.approx(30000.0)
    await adapter.close()


@pytest.mark.asyncio
async def test_canceled_with_zero_fill_maps_to_rejected():
    adapter = _adapter()
    row = _order_row(status="canceled", vol_exec="0.00000000", cost="0.00000")
    with aioresponses() as m:
        _mock_probe(m, _OPEN_ORDERS_URL, {}, "open")
        _mock_probe(m, _CLOSED_ORDERS_URL, {_TXID: row}, "closed")
        conf = await adapter.fetch_order_by_client_id(
            pair="XBTUSD", client_order_id=_CID
        )
    assert conf is not None
    assert conf.status == "rejected"
    assert conf.fill_price is None
    await adapter.close()


@pytest.mark.asyncio
async def test_expired_with_partial_fill_maps_to_partial():
    adapter = _adapter()
    row = _order_row(
        status="expired", vol="1.0", vol_exec="0.4", cost="12000.0", price="30000.0"
    )
    with aioresponses() as m:
        _mock_probe(m, _OPEN_ORDERS_URL, {}, "open")
        _mock_probe(m, _CLOSED_ORDERS_URL, {_TXID: row}, "closed")
        conf = await adapter.fetch_order_by_client_id(
            pair="XBTUSD", client_order_id=_CID
        )
    assert conf is not None
    assert conf.status == "partial"
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("vol_exec", [None, "", "not-a-number"])
async def test_terminal_status_with_unreadable_fill_is_not_called_rejected(vol_exec):
    """'rejected' asserts NO POSITION EXISTS. Defaulting an unreadable
    vol_exec to zero would make the adapter assert that about an order it
    cannot actually read — and Kraken only reaches 'closed' via execution, so
    closed-with-no-readable-fill is self-contradictory. Stay non-terminal so
    the caller keeps looking."""
    row = _order_row(status="closed", vol="0.00050000")
    if vol_exec is None:
        row.pop("vol_exec")
    else:
        row["vol_exec"] = vol_exec
    adapter = _adapter()
    with aioresponses() as m:
        _mock_probe(m, _OPEN_ORDERS_URL, {}, "open")
        _mock_probe(m, _CLOSED_ORDERS_URL, {_TXID: row}, "closed")
        conf = await adapter.fetch_order_by_client_id(
            pair="XBTUSD", client_order_id=_CID
        )
    assert conf is not None
    assert conf.status == "pending"
    await adapter.close()


@pytest.mark.asyncio
async def test_await_fill_keeps_polling_when_the_fill_is_unreadable():
    """The consequence of the mapping above: an unreadable terminal row must
    not stop the poll loop, or polling ends while the account may hold a
    position."""
    adapter = _adapter()
    unreadable = _order_row(status="closed", vol="0.00050000")
    unreadable.pop("vol_exec")
    with aioresponses() as m:
        m.post(_QUERY_ORDERS_URL, status=200, payload=_query_orders_payload(unreadable))
        m.post(
            _QUERY_ORDERS_URL,
            status=200,
            payload=_query_orders_payload(
                _order_row(
                    status="closed",
                    vol_exec="0.00050000",
                    cost="15.00000",
                    price="30000.0",
                )
            ),
        )
        conf = await adapter.await_fill_confirmation(
            venue_order_id=_TXID, client_order_id=_CID, timeout_sec=5.0
        )
        assert len(_calls_to(m, _QUERY_ORDERS_URL)) == 2
    assert conf.status == "filled"
    await adapter.close()


@pytest.mark.asyncio
async def test_absent_order_returns_none():
    adapter = _adapter()
    with aioresponses() as m:
        _mock_probe(m, _OPEN_ORDERS_URL, {}, "open")
        _mock_probe(m, _CLOSED_ORDERS_URL, {}, "closed")
        conf = await adapter.fetch_order_by_client_id(
            pair="XBTUSD", client_order_id=_CID
        )
    assert conf is None
    await adapter.close()


@pytest.mark.asyncio
async def test_lookup_ignores_a_row_with_a_different_client_id():
    adapter = _adapter()
    with aioresponses() as m:
        _mock_probe(
            m, _OPEN_ORDERS_URL, {"OTHER": _order_row(cl_ord_id="not-ours")}, "open"
        )
        _mock_probe(m, _CLOSED_ORDERS_URL, {}, "closed")
        conf = await adapter.fetch_order_by_client_id(
            pair="XBTUSD", client_order_id=_CID
        )
    assert conf is None
    await adapter.close()


# ======================================================================
# await_fill_confirmation
# ======================================================================


def _query_orders_payload(row: dict) -> dict:
    return {"error": [], "result": {_TXID: row}}


@pytest.mark.asyncio
async def test_await_fill_returns_filled_on_the_second_poll():
    adapter = _adapter()
    with aioresponses() as m:
        m.post(
            _QUERY_ORDERS_URL, status=200, payload=_query_orders_payload(_order_row())
        )
        m.post(
            _QUERY_ORDERS_URL,
            status=200,
            payload=_query_orders_payload(
                _order_row(
                    status="closed",
                    vol_exec="0.00050000",
                    cost="15.00000",
                    price="30000.0",
                )
            ),
        )
        conf = await adapter.await_fill_confirmation(
            venue_order_id=_TXID, client_order_id=_CID, timeout_sec=5.0
        )
        assert len(_calls_to(m, _QUERY_ORDERS_URL)) == 2
    assert conf.status == "filled"
    assert conf.filled_qty == pytest.approx(0.0005)
    assert conf.venue_order_id == _TXID
    await adapter.close()


@pytest.mark.asyncio
async def test_await_fill_returns_timeout_without_raising():
    """Never raises on timeout — the caller writes a needs-manual-review row
    rather than handling an exception on a path where money already moved."""
    adapter = _adapter()
    with aioresponses() as m:
        m.post(
            _QUERY_ORDERS_URL,
            status=200,
            payload=_query_orders_payload(_order_row()),
            repeat=True,
        )
        conf = await adapter.await_fill_confirmation(
            venue_order_id=_TXID, client_order_id=_CID, timeout_sec=0.05
        )
    assert conf.status == "timeout"
    assert conf.filled_qty is None
    assert conf.client_order_id == _CID
    await adapter.close()


@pytest.mark.asyncio
async def test_await_fill_returns_timeout_when_polling_fails():
    adapter = _adapter()
    with aioresponses() as m:
        m.post(
            _QUERY_ORDERS_URL,
            status=200,
            payload={"error": ["EAPI:Invalid key"]},
            repeat=True,
        )
        conf = await adapter.await_fill_confirmation(
            venue_order_id=_TXID, client_order_id=_CID, timeout_sec=5.0
        )
        # Broke out of the loop on the first failure rather than burning the
        # whole window against an endpoint that just refused us.
        assert len(_calls_to(m, _QUERY_ORDERS_URL)) == 1
    assert conf.status == "timeout"
    await adapter.close()


@pytest.mark.asyncio
async def test_await_fill_falls_back_to_client_id_lookup_without_a_txid():
    """The state after an ambiguous submission that has not yet been turned
    into a txid."""
    adapter = _adapter()
    with aioresponses() as m:
        _mock_probe(m, _OPEN_ORDERS_URL, {}, "open")
        _mock_probe(
            m,
            _CLOSED_ORDERS_URL,
            {
                _TXID: _order_row(
                    status="closed",
                    vol_exec="0.00050000",
                    cost="15.00000",
                    price="30000.0",
                )
            },
            "closed",
        )
        conf = await adapter.await_fill_confirmation(
            venue_order_id="", client_order_id=_CID, timeout_sec=5.0
        )
    assert conf.status == "filled"
    assert conf.venue_order_id == _TXID
    await adapter.close()


@pytest.mark.asyncio
async def test_await_fill_returns_rejected_immediately_on_a_zero_fill_cancel():
    adapter = _adapter()
    with aioresponses() as m:
        m.post(
            _QUERY_ORDERS_URL,
            status=200,
            payload=_query_orders_payload(_order_row(status="canceled")),
            repeat=True,
        )
        conf = await adapter.await_fill_confirmation(
            venue_order_id=_TXID, client_order_id=_CID, timeout_sec=5.0
        )
        assert len(_calls_to(m, _QUERY_ORDERS_URL)) == 1
    assert conf.status == "rejected"
    await adapter.close()


# ======================================================================
# fetch_order_fills
# ======================================================================


@pytest.mark.asyncio
async def test_fetch_order_fills_returns_decimal_strings():
    adapter = _adapter()
    with aioresponses() as m:
        m.post(
            _QUERY_ORDERS_URL,
            status=200,
            payload=_query_orders_payload(
                _order_row(
                    status="closed",
                    vol_exec="0.00050000",
                    trades=["TZX2WP-XSEOP-FP7WYR", "TJUW2K-FLX2N-AR2FLU"],
                )
            ),
        )
        m.post(
            _QUERY_TRADES_URL,
            status=200,
            payload={
                "error": [],
                "result": {
                    "TZX2WP-XSEOP-FP7WYR": {
                        "ordertxid": _TXID,
                        "pair": "XXBTZUSD",
                        "time": 1688666559.8974,
                        "type": "buy",
                        "ordertype": "limit",
                        "price": "30000.00000",
                        "cost": "9.00000",
                        "fee": "0.01440",
                        "vol": "0.00030000",
                        "maker": True,
                    },
                    "TJUW2K-FLX2N-AR2FLU": {
                        "ordertxid": _TXID,
                        "pair": "XXBTZUSD",
                        "time": 1688666560.1,
                        "type": "buy",
                        "ordertype": "limit",
                        "price": "30000.10000",
                        "cost": "6.00002",
                        "fee": "0.00960",
                        "vol": "0.00020000",
                        "maker": False,
                    },
                },
            },
        )
        fills = await adapter.fetch_order_fills(txid=_TXID)
        assert _body_of(_calls_to(m, _QUERY_ORDERS_URL)[0])["trades"] == "true"
        assert _body_of(_calls_to(m, _QUERY_TRADES_URL)[0])["txid"] == (
            "TZX2WP-XSEOP-FP7WYR,TJUW2K-FLX2N-AR2FLU"
        )

    assert len(fills) == 2
    # Order preserved from the order row's trades array.
    assert [f["trade_id"] for f in fills] == [
        "TZX2WP-XSEOP-FP7WYR",
        "TJUW2K-FLX2N-AR2FLU",
    ]
    first = fills[0]
    # Fees are the money record — strings, never binary floats.
    assert first["fee"] == "0.01440"
    assert isinstance(first["fee"], str)
    assert first["price"] == "30000.00000"
    assert first["vol"] == "0.00030000"
    assert first["cost"] == "9.00000"
    assert first["ordertxid"] == _TXID
    assert first["maker"] is True
    await adapter.close()


@pytest.mark.asyncio
async def test_fetch_order_fills_returns_empty_for_an_unfilled_order():
    adapter = _adapter()
    with aioresponses() as m:
        m.post(
            _QUERY_ORDERS_URL,
            status=200,
            payload=_query_orders_payload(_order_row(trades=[])),
        )
        fills = await adapter.fetch_order_fills(txid=_TXID)
        assert _calls_to(m, _QUERY_TRADES_URL) == []
    assert fills == []
    await adapter.close()


@pytest.mark.asyncio
async def test_fetch_order_fills_chunks_trade_ids_to_the_documented_ceiling():
    """QueryTrades takes at most 20 trade ids per call."""
    adapter = _adapter()
    trade_ids = [f"TRADE-{i:04d}" for i in range(25)]
    with aioresponses() as m:
        m.post(
            _QUERY_ORDERS_URL,
            status=200,
            payload=_query_orders_payload(
                _order_row(status="closed", trades=trade_ids)
            ),
        )
        for start in (0, 20):
            chunk = trade_ids[start : start + 20]
            m.post(
                _QUERY_TRADES_URL,
                status=200,
                payload={
                    "error": [],
                    "result": {
                        tid: {
                            "ordertxid": _TXID,
                            "price": "1.0",
                            "cost": "1.0",
                            "fee": "0.001",
                            "vol": "1.0",
                        }
                        for tid in chunk
                    },
                },
            )
        fills = await adapter.fetch_order_fills(txid=_TXID)
        trade_calls = _calls_to(m, _QUERY_TRADES_URL)
        assert len(trade_calls) == 2
        assert len(_body_of(trade_calls[0])["txid"].split(",")) == 20
        assert len(_body_of(trade_calls[1])["txid"].split(",")) == 5
    assert len(fills) == 25
    await adapter.close()


@pytest.mark.asyncio
async def test_fetch_order_fills_surfaces_auth_failure():
    """Reads are not gated by the live flag, but a real auth failure must
    still reach the caller rather than degrading into an empty list."""
    adapter = _adapter()
    with aioresponses() as m:
        m.post(
            _QUERY_ORDERS_URL,
            status=200,
            payload={"error": ["EAPI:Invalid key"]},
            repeat=True,
        )
        with pytest.raises(KrakenAuthError):
            await adapter.fetch_order_fills(txid=_TXID)
    await adapter.close()


# ======================================================================
# transient-error typing sanity
# ======================================================================


@pytest.mark.asyncio
async def test_ambiguous_submission_is_not_a_transient_error():
    """KrakenAmbiguousSubmissionError must NOT be catchable as
    VenueTransientError — engine-side retry handlers key off that type, and
    an AddOrder that may have landed is the one thing they must not retry."""
    assert not issubclass(KrakenAmbiguousSubmissionError, VenueTransientError)
    assert not issubclass(KrakenAmbiguousSubmissionError, KrakenAPIError)
