"""PR-K1: KrakenSpotAdapter core tests (public rules, private auth, preflight).

Follows tests/test_live_binance_adapter_signed.py's aioresponses style.
Payloads are trimmed copies of real Kraken responses captured from the live
public API on 2026-07-31 — the XXBTZUSD/XDGUSD shapes, the ZUSD/XXBT asset
codes, and the HTTP-200-with-error envelope are all as the venue sends them.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest
from aioresponses import aioresponses

from scout.config import Settings
from scout.live.adapter_base import ExchangeAdapter, OrderRequest
from scout.live.exceptions import VenueTransientError
from scout.live.kraken_adapter import (
    KrakenAPIError,
    KrakenAuthError,
    KrakenPermissionError,
    KrakenRateLimitError,
    KrakenSpotAdapter,
    KrakenWithdrawalCapabilityError,
    normalize_kraken_asset,
)

_ASSETPAIRS_RE = re.compile(r"https://api\.kraken\.com/0/public/AssetPairs.*")
_TICKER_RE = re.compile(r"https://api\.kraken\.com/0/public/Ticker.*")
_DEPTH_RE = re.compile(r"https://api\.kraken\.com/0/public/Depth.*")
_BALANCE_URL = "https://api.kraken.com/0/private/Balance"
_WITHDRAW_URL = "https://api.kraken.com/0/private/WithdrawMethods"

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")

# Base64 so the signing primitive can decode it; not a real credential.
_TEST_SECRET = "dGVzdC1rcmFrZW4tc2VjcmV0LWZvci11bml0LXRlc3Rz"

# Real XXBTZUSD row (trimmed to the fields the adapter reads).
_XBTUSD_ROW = {
    "altname": "XBTUSD",
    "wsname": "XBT/USD",
    "base": "XXBT",
    "quote": "ZUSD",
    "pair_decimals": 1,
    "cost_decimals": 5,
    "lot_decimals": 8,
    "ordermin": "0.00005",
    "costmin": "0.5",
    "tick_size": "0.1",
    "status": "online",
}
_XBTUSDT_ROW = {
    "altname": "XBTUSDT",
    "wsname": "XBT/USDT",
    "base": "XXBT",
    "quote": "USDT",
    "pair_decimals": 1,
    "lot_decimals": 8,
    "ordermin": "0.00005",
    "costmin": "0.5",
    "tick_size": "0.1",
    "status": "online",
}
_UNKNOWN_PAIR = {"error": ["EQuery:Unknown asset pair"]}


def _settings(**overrides):
    base = dict(KRAKEN_API_KEY="testkey", KRAKEN_API_SECRET=_TEST_SECRET)
    base.update(overrides)
    return Settings(_env_file=None, **_REQUIRED, **base)


def _adapter(**overrides) -> KrakenSpotAdapter:
    adapter = KrakenSpotAdapter(_settings(**overrides), db=None)
    adapter._retry_sleep = _no_sleep
    return adapter


async def _no_sleep(_delay: float) -> None:
    """Swap in for asyncio.sleep so retry tests don't burn 7 real seconds."""
    return None


def _calls(mocked: aioresponses) -> list:
    return [call for calls in mocked.requests.values() for call in calls]


# ---------- construction / ABC conformance ----------


def test_adapter_instantiates_against_abc():
    """No abstract-method holes — every ExchangeAdapter method is concrete."""
    adapter = KrakenSpotAdapter(_settings(), db=None)
    assert isinstance(adapter, ExchangeAdapter)
    assert adapter.venue_name == "kraken"


def test_adapter_defers_session_creation():
    """Constructing outside a running loop must not build a ClientSession."""
    adapter = KrakenSpotAdapter(_settings(), db=None)
    assert adapter._session is None


# ---------- asset-code normalization ----------


@pytest.mark.parametrize(
    ("kraken_code", "expected"),
    [
        ("ZUSD", "USD"),
        ("XXBT", "BTC"),
        ("XETH", "ETH"),
        ("XXDG", "DOGE"),
        ("XXRP", "XRP"),
        ("USDT", "USDT"),
        ("SOL", "SOL"),
        # 4-char codes that START with X/Z but are their own altname — a
        # strip-the-prefix rule would corrupt every one of these.
        ("XAUT", "XAUT"),
        ("ZORA", "ZORA"),
        ("ZEUS", "ZEUS"),
        ("XION", "XION"),
    ],
)
def test_normalize_kraken_asset(kraken_code, expected):
    assert normalize_kraken_asset(kraken_code) == expected


# ---------- public: AssetPairs / metadata / resolution ----------


@pytest.mark.asyncio
async def test_fetch_exchange_info_row_returns_row():
    adapter = _adapter()
    with aioresponses() as m:
        m.get(
            _ASSETPAIRS_RE, payload={"error": [], "result": {"XXBTZUSD": _XBTUSD_ROW}}
        )
        row = await adapter.fetch_exchange_info_row("XBTUSD")
        assert row is not None
        assert row["altname"] == "XBTUSD"
    await adapter.close()


@pytest.mark.asyncio
async def test_fetch_exchange_info_row_returns_none_for_unknown_pair():
    """HTTP 200 + EQuery:Unknown asset pair is terminal-not-listed, not an
    error — the resolver needs None so it can try the next candidate."""
    adapter = _adapter()
    with aioresponses() as m:
        m.get(_ASSETPAIRS_RE, payload=_UNKNOWN_PAIR)
        assert await adapter.fetch_exchange_info_row("BOGUSPAIR") is None
    await adapter.close()


@pytest.mark.asyncio
async def test_resolve_pair_for_symbol_maps_btc_to_kraken_xbt_altname():
    """Canonical BTC must reach Kraken's XBT naming, and the returned pair
    must be the altname (XBTUSD) that AddOrder accepts — not the AssetPairs
    key (XXBTZUSD) and not the wsname (XBT/USD)."""
    adapter = _adapter()
    with aioresponses() as m:
        m.get(
            _ASSETPAIRS_RE, payload={"error": [], "result": {"XXBTZUSD": _XBTUSD_ROW}}
        )
        assert await adapter.resolve_pair_for_symbol("BTC") == "XBTUSD"
        # First candidate hit — the XBT alias is tried before plain BTCUSD.
        assert _calls(m)[0].kwargs["params"] == {"pair": "XBTUSD"}
    await adapter.close()


@pytest.mark.asyncio
async def test_resolve_pair_for_symbol_prefers_usd_over_usdt():
    """A USD book short-circuits resolution: USDT is never queried."""
    adapter = _adapter()
    with aioresponses() as m:
        m.get(
            _ASSETPAIRS_RE, payload={"error": [], "result": {"XXBTZUSD": _XBTUSD_ROW}}
        )
        assert await adapter.resolve_pair_for_symbol("BTC") == "XBTUSD"
        assert len(_calls(m)) == 1
    await adapter.close()


@pytest.mark.asyncio
async def test_resolve_pair_for_symbol_falls_back_to_usdt():
    """No USD book → both USD candidates miss, then USDT resolves."""
    adapter = _adapter()
    with aioresponses() as m:
        m.get(_ASSETPAIRS_RE, payload=_UNKNOWN_PAIR)  # XBTUSD
        m.get(_ASSETPAIRS_RE, payload=_UNKNOWN_PAIR)  # BTCUSD
        m.get(
            _ASSETPAIRS_RE, payload={"error": [], "result": {"XBTUSDT": _XBTUSDT_ROW}}
        )
        assert await adapter.resolve_pair_for_symbol("BTC") == "XBTUSDT"
    await adapter.close()


@pytest.mark.asyncio
async def test_resolve_pair_for_symbol_rejects_non_online_status():
    """post_only can't be entered or exited with a market order — resolution
    must fail closed rather than hand back an untradable book."""
    adapter = _adapter()
    post_only = dict(_XBTUSD_ROW, status="post_only")
    with aioresponses() as m:
        m.get(
            _ASSETPAIRS_RE,
            payload={"error": [], "result": {"XXBTZUSD": post_only}},
            repeat=True,
        )
        assert await adapter.resolve_pair_for_symbol("BTC") is None
    await adapter.close()


@pytest.mark.asyncio
async def test_resolve_pair_for_symbol_returns_none_when_unlisted():
    adapter = _adapter()
    with aioresponses() as m:
        m.get(_ASSETPAIRS_RE, payload=_UNKNOWN_PAIR, repeat=True)
        assert await adapter.resolve_pair_for_symbol("NOTACOIN") is None
    await adapter.close()


@pytest.mark.asyncio
async def test_fetch_venue_metadata_maps_kraken_market_rules():
    adapter = _adapter()
    with aioresponses() as m:
        m.get(
            _ASSETPAIRS_RE,
            payload={"error": [], "result": {"XXBTZUSD": _XBTUSD_ROW}},
            repeat=True,
        )
        meta = await adapter.fetch_venue_metadata("BTC")
    assert meta is not None
    assert meta.venue == "kraken"
    assert meta.canonical == "BTC"
    assert meta.venue_pair == "XBTUSD"
    assert meta.quote == "USD"  # ZUSD normalized
    assert meta.asset_class == "spot"
    assert meta.min_size == pytest.approx(0.00005)  # ordermin
    assert meta.min_cost == pytest.approx(0.5)  # costmin
    assert meta.tick_size == pytest.approx(0.1)  # pair_decimals=1
    assert meta.lot_size == pytest.approx(1e-8)  # lot_decimals=8
    await adapter.close()


@pytest.mark.asyncio
async def test_fetch_venue_metadata_derives_tick_from_pair_decimals():
    """tick_size absent (older payload shape) → fall back to 10**-pair_decimals."""
    adapter = _adapter()
    row = {k: v for k, v in _XBTUSD_ROW.items() if k != "tick_size"}
    with aioresponses() as m:
        m.get(
            _ASSETPAIRS_RE,
            payload={"error": [], "result": {"XXBTZUSD": row}},
            repeat=True,
        )
        meta = await adapter.fetch_venue_metadata("BTC")
    assert meta is not None
    assert meta.tick_size == pytest.approx(0.1)
    await adapter.close()


@pytest.mark.asyncio
async def test_fetch_venue_metadata_returns_none_when_unlisted():
    adapter = _adapter()
    with aioresponses() as m:
        m.get(_ASSETPAIRS_RE, payload=_UNKNOWN_PAIR, repeat=True)
        assert await adapter.fetch_venue_metadata("NOTACOIN") is None
    await adapter.close()


# ---------- public: Ticker / Depth ----------


@pytest.mark.asyncio
async def test_fetch_price_returns_bid_ask_mid():
    adapter = _adapter()
    with aioresponses() as m:
        m.get(
            _TICKER_RE,
            payload={
                "error": [],
                "result": {
                    "XXBTZUSD": {
                        "a": ["30300.10000", "1", "1.000"],
                        "b": ["30300.00000", "1", "1.000"],
                        "c": ["30303.20000", "0.00067643"],
                    }
                },
            },
        )
        price = await adapter.fetch_price("XBTUSD")
        assert price == Decimal("30300.05")
    await adapter.close()


@pytest.mark.asyncio
async def test_fetch_price_falls_back_to_last_trade():
    """One-sided book → use c[0] rather than failing the price lookup."""
    adapter = _adapter()
    with aioresponses() as m:
        m.get(
            _TICKER_RE,
            payload={
                "error": [],
                "result": {
                    "XXBTZUSD": {"a": [], "b": [], "c": ["30303.20000", "0.00067643"]}
                },
            },
        )
        assert await adapter.fetch_price("XBTUSD") == Decimal("30303.20000")
    await adapter.close()


@pytest.mark.asyncio
async def test_fetch_depth_orders_sides_and_computes_mid():
    adapter = _adapter()
    with aioresponses() as m:
        m.get(
            _DEPTH_RE,
            payload={
                "error": [],
                "result": {
                    "XXBTZUSD": {
                        # Deliberately shuffled — the adapter must sort.
                        "asks": [
                            ["30387.90000", "0.500", 1690000001],
                            ["30384.10000", "2.000", 1690000000],
                        ],
                        "bids": [
                            ["30296.70000", "1.000", 1690000003],
                            ["30297.00000", "3.000", 1690000002],
                        ],
                    }
                },
            },
        )
        depth = await adapter.fetch_depth("XBTUSD", limit=2)
    assert [lv.price for lv in depth.bids] == [
        Decimal("30297.00000"),
        Decimal("30296.70000"),
    ]
    assert [lv.price for lv in depth.asks] == [
        Decimal("30384.10000"),
        Decimal("30387.90000"),
    ]
    assert depth.bids[0].qty == Decimal("3.000")
    assert depth.mid == (Decimal("30297.00000") + Decimal("30384.10000")) / Decimal(2)
    assert depth.fetched_at.tzinfo is not None
    await adapter.close()


@pytest.mark.asyncio
async def test_fetch_depth_caps_count_at_kraken_maximum():
    adapter = _adapter()
    with aioresponses() as m:
        m.get(
            _DEPTH_RE,
            payload={"error": [], "result": {"XXBTZUSD": {"asks": [], "bids": []}}},
        )
        await adapter.fetch_depth("XBTUSD", limit=5000)
        assert _calls(m)[0].kwargs["params"]["count"] == 500
    await adapter.close()


# ---------- private: signing, headers, balances ----------


@pytest.mark.asyncio
async def test_private_post_sends_signed_headers_and_form_body():
    adapter = _adapter()
    with aioresponses() as m:
        m.post(_BALANCE_URL, payload={"error": [], "result": {"ZUSD": "100.0"}})
        await adapter.fetch_account_balance(asset="USD")
        recorded = _calls(m)[0]
        headers = recorded.kwargs["headers"]
        assert headers["API-Key"] == "testkey"
        assert headers["API-Sign"]  # non-empty base64 signature
        assert headers["Content-Type"] == "application/x-www-form-urlencoded"
        body = recorded.kwargs["data"]
        assert isinstance(body, str)
        assert body.startswith("nonce=")
        # The nonce in the signed body must be the one that was hashed.
        assert body.split("nonce=")[1].split("&")[0].isdigit()
    await adapter.close()


@pytest.mark.asyncio
async def test_fetch_account_balance_normalizes_kraken_asset_codes():
    adapter = _adapter()
    with aioresponses() as m:
        m.post(
            _BALANCE_URL,
            payload={
                "error": [],
                "result": {
                    "ZUSD": "171288.6158",
                    "XXBT": "1011.19088779",
                    "USDT": "500000.0",
                },
            },
        )
        assert await adapter.fetch_account_balance(asset="USD") == pytest.approx(
            171288.6158
        )
    await adapter.close()


@pytest.mark.asyncio
async def test_fetch_account_balance_defaults_to_usd():
    """ABC default is USDT; the Kraken override defaults to USD because the
    pilot trades a USD-quoted book."""
    adapter = _adapter()
    with aioresponses() as m:
        m.post(_BALANCE_URL, payload={"error": [], "result": {"ZUSD": "42.5"}})
        assert await adapter.fetch_account_balance() == pytest.approx(42.5)
    await adapter.close()


@pytest.mark.asyncio
async def test_fetch_account_balance_skips_staked_variants():
    """XBT.S is staked, not spot-tradable. Counting it would let a balance
    gate approve an order the account cannot fund."""
    adapter = _adapter()
    with aioresponses() as m:
        m.post(
            _BALANCE_URL,
            payload={"error": [], "result": {"XXBT.S": "5.0", "XXBT": "0.25"}},
        )
        assert await adapter.fetch_account_balance(asset="BTC") == pytest.approx(0.25)
    await adapter.close()


@pytest.mark.asyncio
async def test_fetch_account_balance_returns_zero_when_asset_absent():
    adapter = _adapter()
    with aioresponses() as m:
        m.post(_BALANCE_URL, payload={"error": [], "result": {"ZEUR": "10.0"}})
        assert await adapter.fetch_account_balance(asset="USD") == 0.0
    await adapter.close()


@pytest.mark.asyncio
async def test_private_call_without_credentials_raises_config_error():
    """No keys configured → a clear config error before any HTTP happens."""
    adapter = KrakenSpotAdapter(Settings(_env_file=None, **_REQUIRED), db=None)
    with aioresponses() as m:
        with pytest.raises(KrakenAuthError, match="KRAKEN_API_KEY not configured"):
            await adapter.fetch_account_balance(asset="USD")
        assert len(_calls(m)) == 0
    await adapter.close()


@pytest.mark.asyncio
async def test_private_call_without_secret_raises_config_error():
    adapter = KrakenSpotAdapter(
        Settings(_env_file=None, **_REQUIRED, KRAKEN_API_KEY="testkey"), db=None
    )
    with aioresponses() as m:
        with pytest.raises(KrakenAuthError, match="KRAKEN_API_SECRET not configured"):
            await adapter.fetch_account_balance(asset="USD")
        assert len(_calls(m)) == 0
    await adapter.close()


# ---------- error envelope + retry taxonomy ----------


@pytest.mark.asyncio
async def test_http_200_with_error_list_is_a_failure():
    """The core Kraken trap: application failures ship as HTTP 200."""
    adapter = _adapter()
    with aioresponses() as m:
        m.post(
            _BALANCE_URL,
            status=200,
            payload={"error": ["EGeneral:Invalid arguments"]},
            repeat=True,
        )
        with pytest.raises(KrakenAPIError):
            await adapter.fetch_account_balance(asset="USD")
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    ["EAPI:Invalid key", "EAPI:Invalid signature", "EAPI:Invalid nonce"],
)
async def test_auth_class_errors_raise_and_are_never_retried(error_code):
    """Retrying an auth failure burns nonces and risks EGeneral:Temporary
    lockout — exactly one request must leave the box."""
    adapter = _adapter()
    with aioresponses() as m:
        m.post(_BALANCE_URL, status=200, payload={"error": [error_code]}, repeat=True)
        with pytest.raises(KrakenAuthError, match=re.escape(error_code)):
            await adapter.fetch_account_balance(asset="USD")
        assert len(_calls(m)) == 1
    await adapter.close()


@pytest.mark.asyncio
async def test_permission_denied_raises_permission_error_without_retry():
    adapter = _adapter()
    with aioresponses() as m:
        m.post(
            _BALANCE_URL,
            status=200,
            payload={"error": ["EGeneral:Permission denied"]},
            repeat=True,
        )
        with pytest.raises(KrakenPermissionError):
            await adapter.fetch_account_balance(asset="USD")
        assert len(_calls(m)) == 1
    await adapter.close()


@pytest.mark.asyncio
async def test_rate_limit_error_raises_without_retry():
    adapter = _adapter()
    with aioresponses() as m:
        m.post(
            _BALANCE_URL,
            status=200,
            payload={"error": ["EAPI:Rate limit exceeded"]},
            repeat=True,
        )
        with pytest.raises(KrakenRateLimitError):
            await adapter.fetch_account_balance(asset="USD")
        assert len(_calls(m)) == 1
    await adapter.close()


@pytest.mark.asyncio
async def test_http_429_raises_rate_limit_error_without_retry():
    adapter = _adapter()
    with aioresponses() as m:
        m.get(_TICKER_RE, status=429, body="", repeat=True)
        with pytest.raises(KrakenRateLimitError):
            await adapter.fetch_price("XBTUSD")
        assert len(_calls(m)) == 1
    await adapter.close()


@pytest.mark.asyncio
async def test_service_unavailable_is_retried_then_raises_transient():
    """EService:Unavailable is the retryable class: 1 attempt + 3 backoffs."""
    adapter = _adapter()
    with aioresponses() as m:
        m.get(
            _TICKER_RE,
            status=200,
            payload={"error": ["EService:Unavailable"]},
            repeat=True,
        )
        with pytest.raises(VenueTransientError):
            await adapter.fetch_price("XBTUSD")
        assert len(_calls(m)) == 4
    await adapter.close()


@pytest.mark.asyncio
async def test_service_busy_recovers_on_retry():
    adapter = _adapter()
    with aioresponses() as m:
        m.get(_TICKER_RE, status=200, payload={"error": ["EService:Busy"]})
        m.get(
            _TICKER_RE,
            payload={
                "error": [],
                "result": {"XXBTZUSD": {"a": ["101"], "b": ["99"], "c": ["100"]}},
            },
        )
        assert await adapter.fetch_price("XBTUSD") == Decimal(100)
        assert len(_calls(m)) == 2
    await adapter.close()


@pytest.mark.asyncio
async def test_http_5xx_is_retried_then_raises_transient():
    adapter = _adapter()
    with aioresponses() as m:
        m.get(_TICKER_RE, status=503, body="", repeat=True)
        with pytest.raises(VenueTransientError):
            await adapter.fetch_price("XBTUSD")
        assert len(_calls(m)) == 4
    await adapter.close()


@pytest.mark.asyncio
async def test_cloudflare_html_body_is_treated_as_transient():
    """A CDN interstitial served as HTTP 200 must not reach the JSON parser."""
    adapter = _adapter()
    with aioresponses() as m:
        m.get(
            _TICKER_RE,
            status=200,
            body="<html>attention required</html>",
            headers={"Content-Type": "text/html"},
            repeat=True,
        )
        with pytest.raises(VenueTransientError):
            await adapter.fetch_price("XBTUSD")
    await adapter.close()


# ---------- preflight ----------


@pytest.mark.asyncio
async def test_preflight_passes_when_withdrawal_is_permission_denied():
    adapter = _adapter()
    with aioresponses() as m:
        m.post(_BALANCE_URL, payload={"error": [], "result": {"ZUSD": "100.0"}})
        m.post(_WITHDRAW_URL, payload={"error": ["EGeneral:Permission denied"]})
        result = await adapter.preflight_credentials_check()
    assert result["auth_ok"] is True
    assert result["withdrawal_excluded"] is True
    assert result["checked_at"].endswith("+00:00")
    await adapter.close()


@pytest.mark.asyncio
async def test_preflight_hard_rejects_when_withdrawal_probe_succeeds():
    """The key CAN reach a withdrawal-scoped endpoint — hard reject."""
    adapter = _adapter()
    with aioresponses() as m:
        m.post(_BALANCE_URL, payload={"error": [], "result": {"ZUSD": "100.0"}})
        m.post(
            _WITHDRAW_URL,
            payload={"error": [], "result": [{"asset": "XBT", "method": "Bitcoin"}]},
        )
        with pytest.raises(KrakenWithdrawalCapabilityError, match="CAN reach"):
            await adapter.preflight_credentials_check()
    await adapter.close()


@pytest.mark.asyncio
async def test_preflight_fails_closed_on_unrelated_withdrawal_error():
    """Anything that isn't an explicit permission denial leaves withdrawal
    exclusion UNPROVEN — refuse to proceed."""
    adapter = _adapter()
    with aioresponses() as m:
        m.post(_BALANCE_URL, payload={"error": [], "result": {"ZUSD": "100.0"}})
        m.post(_WITHDRAW_URL, payload={"error": ["EFunding:No funding method"]})
        with pytest.raises(KrakenWithdrawalCapabilityError, match="inconclusive"):
            await adapter.preflight_credentials_check()
    await adapter.close()


@pytest.mark.asyncio
async def test_preflight_fails_closed_when_withdrawal_probe_is_transient():
    adapter = _adapter()
    with aioresponses() as m:
        m.post(_BALANCE_URL, payload={"error": [], "result": {"ZUSD": "100.0"}})
        m.post(_WITHDRAW_URL, status=503, body="", repeat=True)
        with pytest.raises(KrakenWithdrawalCapabilityError, match="inconclusive"):
            await adapter.preflight_credentials_check()
    await adapter.close()


@pytest.mark.asyncio
async def test_preflight_surfaces_auth_failure_from_balance_step():
    """A bad key must surface as KrakenAuthError, not be laundered into the
    withdrawal-capability class."""
    adapter = _adapter()
    with aioresponses() as m:
        m.post(_BALANCE_URL, payload={"error": ["EAPI:Invalid key"]}, repeat=True)
        with pytest.raises(KrakenAuthError):
            await adapter.preflight_credentials_check()
        # Withdrawal probe never fires once auth is known-bad.
        assert len(_calls(m)) == 1
    await adapter.close()


# ---------- order lifecycle is PR-K2 ----------


@pytest.mark.asyncio
async def test_order_lifecycle_methods_raise_pr_k2():
    adapter = _adapter()
    request = OrderRequest(
        paper_trade_id=1,
        canonical="BTC",
        venue_pair="XBTUSD",
        side="buy",
        size_usd=10.0,
        intent_uuid="abcd1234-ef56-7890-abcd-ef0123456789",
    )
    with pytest.raises(NotImplementedError, match="PR-K2"):
        await adapter.send_order(pair="XBTUSD", side="buy", size_usd=Decimal("10"))
    with pytest.raises(NotImplementedError, match="PR-K2"):
        await adapter.place_order_request(request)
    with pytest.raises(NotImplementedError, match="PR-K2"):
        await adapter.await_fill_confirmation(
            venue_order_id="x", client_order_id="y", timeout_sec=1.0
        )
    with pytest.raises(NotImplementedError, match="PR-K2"):
        await adapter.place_exit_order(
            pair="XBTUSD",
            base_qty=Decimal("1"),
            client_order_id="y",
            timeout_sec=1.0,
        )
    with pytest.raises(NotImplementedError, match="PR-K2"):
        await adapter.fetch_order_by_client_id(pair="XBTUSD", client_order_id="y")
    await adapter.close()
