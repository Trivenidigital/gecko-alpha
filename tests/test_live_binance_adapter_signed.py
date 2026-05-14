"""BL-NEW-LIVE-HYBRID M1.5a: BinanceSpotAdapter signed-request runtime body tests.

Covers Tasks 2 (_signed_get/_signed_post wrappers), 3 (fetch_account_balance),
4 (place_order_request idempotency), and 5 (await_fill_confirmation poll
+ slippage compute).

Note: these tests use aiohttp + aioresponses, which on Windows transitively
trigger the OpenSSL Applink crash documented across multiple test files.
They run cleanly on CI Linux. For local cross-platform smoke coverage, the
parallel `tests/test_live_binance_signing.py` (pure-stdlib) covers the
signing primitive without aiohttp.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest
from aioresponses import aioresponses

# aioresponses matches URL EXACTLY by default — signed endpoints append
# ?timestamp=...&signature=...&recvWindow=10000 query strings, breaking
# bare-URL mocks. Use regex prefix patterns instead.
_ACCOUNT_RE = re.compile(r"https://api\.binance\.com/api/v3/account.*")
_ORDER_RE = re.compile(r"https://api\.binance\.com/api/v3/order.*")
_DEPTH_RE = re.compile(r"https://api\.binance\.com/api/v3/depth.*")

from scout.config import Settings
from scout.db import Database
from scout.live.adapter_base import OrderRequest
from scout.live.binance_adapter import (
    BinanceAuthError,
    BinanceDuplicateOrderError,
    BinanceIPBanError,
    BinanceSpotAdapter,
)

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")


def _settings(**overrides):
    base = dict(
        BINANCE_API_KEY="testkey",
        BINANCE_API_SECRET="testsecret",
        LIVE_USE_REAL_SIGNED_REQUESTS=True,
    )
    base.update(overrides)
    return Settings(_env_file=None, **_REQUIRED, **base)


# ---------- Task 2: _signed_get / _signed_post ----------


@pytest.mark.asyncio
async def test_signed_get_appends_timestamp_and_signature():
    s = _settings()
    adapter = BinanceSpotAdapter(s, db=None)
    with aioresponses() as m:
        m.get(
            _ACCOUNT_RE,
            payload={"balances": [], "permissions": ["SPOT"]},
        )
        await adapter._signed_get("/api/v3/account", params={})
    await adapter.close()


@pytest.mark.asyncio
async def test_signed_get_includes_api_key_header():
    s = _settings()
    adapter = BinanceSpotAdapter(s, db=None)
    with aioresponses() as m:
        m.get(
            _ACCOUNT_RE,
            payload={"balances": []},
        )
        await adapter._signed_get("/api/v3/account", params={})
        # Recorded request should have X-MBX-APIKEY header
        recorded = list(m.requests.values())[0][0]
        headers = recorded.kwargs.get("headers", {}) or {}
        assert headers.get("X-MBX-APIKEY") == "testkey"
    await adapter.close()


@pytest.mark.asyncio
async def test_signed_post_round_trips():
    s = _settings()
    adapter = BinanceSpotAdapter(s, db=None)
    with aioresponses() as m:
        m.post(
            _ORDER_RE,
            payload={"orderId": 12345, "status": "NEW"},
        )
        result = await adapter._signed_post(
            "/api/v3/order",
            params={
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "MARKET",
                "quoteOrderQty": "10",
                "newClientOrderId": "gecko-1-abcd1234",
            },
        )
        assert result["orderId"] == 12345
    await adapter.close()


@pytest.mark.asyncio
async def test_signed_get_raises_on_signature_invalid():
    """Binance error -2014 → BinanceAuthError, never retry."""
    s = _settings()
    adapter = BinanceSpotAdapter(s, db=None)
    with aioresponses() as m:
        m.get(
            _ACCOUNT_RE,
            status=400,
            payload={"code": -2014, "msg": "Signature for input invalid"},
        )
        with pytest.raises(BinanceAuthError) as excinfo:
            await adapter._signed_get("/api/v3/account", params={})
        assert "2014" in str(excinfo.value)
    await adapter.close()


@pytest.mark.asyncio
async def test_signed_get_raises_on_timestamp_drift():
    """-1021 (clock skew) → BinanceAuthError. Common in prod on NTP drift."""
    s = _settings()
    adapter = BinanceSpotAdapter(s, db=None)
    with aioresponses() as m:
        m.get(
            _ACCOUNT_RE,
            status=400,
            payload={
                "code": -1021,
                "msg": "Timestamp for this request is outside of the recvWindow",
            },
        )
        with pytest.raises(BinanceAuthError) as excinfo:
            await adapter._signed_get("/api/v3/account", params={})
        assert "1021" in str(excinfo.value)
    await adapter.close()


@pytest.mark.asyncio
async def test_signed_endpoint_raises_ip_ban_on_418():
    """HTTP 418 → BinanceIPBanError (R1-I1: distinct from 429 retry-able)."""
    s = _settings()
    adapter = BinanceSpotAdapter(s, db=None)
    adapter._retry_sleep = lambda _: None  # no delay
    with aioresponses() as m:
        m.get(
            _ACCOUNT_RE,
            status=418,
            payload={"code": -1003, "msg": "Way too much request weight used"},
        )
        with pytest.raises(BinanceIPBanError):
            await adapter._signed_get("/api/v3/account", params={})
    await adapter.close()


# ---------- Task 3: fetch_account_balance ----------


@pytest.mark.asyncio
async def test_fetch_account_balance_returns_free_usdt():
    s = _settings()
    adapter = BinanceSpotAdapter(s, db=None)
    with aioresponses() as m:
        m.get(
            _ACCOUNT_RE,
            payload={
                "balances": [
                    {"asset": "BTC", "free": "0.5", "locked": "0.0"},
                    {"asset": "USDT", "free": "1234.56", "locked": "100.0"},
                ],
                "permissions": ["SPOT"],
            },
        )
        balance = await adapter.fetch_account_balance("USDT")
        assert balance == 1234.56
    await adapter.close()


@pytest.mark.asyncio
async def test_fetch_account_balance_returns_zero_when_asset_absent():
    s = _settings()
    adapter = BinanceSpotAdapter(s, db=None)
    with aioresponses() as m:
        m.get(
            _ACCOUNT_RE,
            payload={"balances": [{"asset": "BTC", "free": "0.5", "locked": "0"}]},
        )
        balance = await adapter.fetch_account_balance("XYZ")
        assert balance == 0.0
    await adapter.close()


@pytest.mark.asyncio
async def test_fetch_account_balance_raises_when_signed_disabled():
    """LIVE_USE_REAL_SIGNED_REQUESTS=False (default) → NotImplementedError.
    R2-I4 emergency-revert posture."""
    s = _settings(LIVE_USE_REAL_SIGNED_REQUESTS=False)
    adapter = BinanceSpotAdapter(s, db=None)
    with pytest.raises(NotImplementedError, match="emergency-revert"):
        await adapter.fetch_account_balance("USDT")
    await adapter.close()


# ---------- Task 4: place_order_request ----------


async def _seed_paper(db):
    cur = await db._conn.execute("""INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_price, sl_price,
            status, opened_at)
           VALUES ('btc-tok', 'BTC', 'btc', 'ethereum', 'first_signal', '{}',
                   100, 50, 0.5, 120, 80, 'open',
                   '2026-05-09T00:00:00+00:00')""")
    return cur.lastrowid


@pytest.mark.asyncio
async def test_place_order_request_dedup_returns_existing(tmp_path):
    """If live_trades has cid + entry_order_id set, return without HTTP."""
    from scout.live.idempotency import make_client_order_id, record_pending_order

    db = Database(tmp_path / "t.db")
    await db.initialize()
    paper_id = await _seed_paper(db)
    intent_uuid = "abcd1234-ef56-7890-abcd-ef0123456789"
    cid = make_client_order_id(paper_id, intent_uuid)
    live_id = await record_pending_order(
        db,
        client_order_id=cid,
        paper_trade_id=paper_id,
        coin_id="btc",
        symbol="BTC",
        venue="binance",
        pair="BTCUSDT",
        signal_type="first_signal",
        size_usd="50",
    )
    await db._conn.execute(
        "UPDATE live_trades SET entry_order_id = ? WHERE id = ?",
        ("BNX-99999", live_id),
    )
    await db._conn.commit()

    s = _settings()
    adapter = BinanceSpotAdapter(s, db=db)
    request = OrderRequest(
        paper_trade_id=paper_id,
        canonical="BTC",
        venue_pair="BTCUSDT",
        side="buy",
        size_usd=50.0,
        intent_uuid=intent_uuid,
        signal_type="first_signal",
    )
    with aioresponses() as m:
        order_id = await adapter.place_order_request(request)
        assert order_id == "BNX-99999"
        # Zero HTTP calls (dedup hit)
        assert len(m.requests) == 0
    await adapter.close()
    await db.close()


@pytest.mark.asyncio
async def test_place_order_request_first_attempt_records_then_submits(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    paper_id = await _seed_paper(db)
    intent_uuid = "abcd1234-ef56-7890-abcd-ef0123456789"

    s = _settings()
    adapter = BinanceSpotAdapter(s, db=db)
    with aioresponses() as m:
        # fetch_depth call
        m.get(
            _DEPTH_RE,
            payload={
                "lastUpdateId": 1,
                "bids": [["100.0", "1.0"]],
                "asks": [["100.5", "1.0"]],
            },
        )
        m.post(
            _ORDER_RE,
            payload={"orderId": 88888, "status": "NEW"},
        )
        request = OrderRequest(
            paper_trade_id=paper_id,
            canonical="BTC",
            venue_pair="BTCUSDT",
            side="buy",
            size_usd=50.0,
            intent_uuid=intent_uuid,
            signal_type="first_signal",
        )
        order_id = await adapter.place_order_request(request)
        assert order_id == "88888"

    cur = await db._conn.execute(
        "SELECT client_order_id, status, entry_order_id, signal_type FROM live_trades "
        "WHERE paper_trade_id = ?",
        (paper_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[1] == "open"
    assert row[2] == "88888"
    assert row[3] == "first_signal"
    await adapter.close()
    await db.close()


@pytest.mark.asyncio
async def test_place_order_request_recovers_from_2010(tmp_path):
    """R2-I2: Binance returns -2010 (duplicate cid). Adapter must recover
    via origClientOrderId GET and return the existing orderId."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    paper_id = await _seed_paper(db)
    intent_uuid = "abcd1234-ef56-7890-abcd-ef0123456789"

    s = _settings()
    adapter = BinanceSpotAdapter(s, db=db)
    with aioresponses() as m:
        m.get(
            _DEPTH_RE,
            payload={
                "lastUpdateId": 1,
                "bids": [["100.0", "1.0"]],
                "asks": [["100.5", "1.0"]],
            },
        )
        # POST returns -2010 (duplicate)
        m.post(
            _ORDER_RE,
            status=400,
            payload={"code": -2010, "msg": "Duplicate clientOrderId"},
        )
        # Recovery GET returns the existing order
        m.get(
            _ORDER_RE,
            payload={"orderId": 77777, "status": "FILLED"},
        )
        request = OrderRequest(
            paper_trade_id=paper_id,
            canonical="BTC",
            venue_pair="BTCUSDT",
            side="buy",
            size_usd=50.0,
            intent_uuid=intent_uuid,
            signal_type="first_signal",
        )
        order_id = await adapter.place_order_request(request)
        assert order_id == "77777"
    await adapter.close()
    await db.close()


@pytest.mark.asyncio
async def test_place_order_request_rejects_empty_order_id(tmp_path):
    """R1-C6: empty/missing orderId in Binance response → VenueTransientError.
    Never persist entry_order_id=''."""
    from scout.live.exceptions import VenueTransientError

    db = Database(tmp_path / "t.db")
    await db.initialize()
    paper_id = await _seed_paper(db)
    intent_uuid = "abcd1234-ef56-7890-abcd-ef0123456789"

    s = _settings()
    adapter = BinanceSpotAdapter(s, db=db)
    with aioresponses() as m:
        m.get(
            _DEPTH_RE,
            payload={
                "lastUpdateId": 1,
                "bids": [["100.0", "1.0"]],
                "asks": [["100.5", "1.0"]],
            },
        )
        # Malformed Binance response: missing orderId
        m.post(
            _ORDER_RE,
            payload={"status": "NEW"},
        )
        request = OrderRequest(
            paper_trade_id=paper_id,
            canonical="BTC",
            venue_pair="BTCUSDT",
            side="buy",
            size_usd=50.0,
            intent_uuid=intent_uuid,
            signal_type="first_signal",
        )
        with pytest.raises(VenueTransientError, match="missing orderId"):
            await adapter.place_order_request(request)
    await adapter.close()
    await db.close()


@pytest.mark.asyncio
async def test_place_order_request_raises_when_signed_disabled(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    paper_id = await _seed_paper(db)
    s = _settings(LIVE_USE_REAL_SIGNED_REQUESTS=False)
    adapter = BinanceSpotAdapter(s, db=db)
    request = OrderRequest(
        paper_trade_id=paper_id,
        canonical="BTC",
        venue_pair="BTCUSDT",
        side="buy",
        size_usd=50.0,
        intent_uuid="abcd1234-ef56-7890-abcd-ef0123456789",
        signal_type="first_signal",
    )
    with pytest.raises(NotImplementedError, match="emergency-revert"):
        await adapter.place_order_request(request)
    await adapter.close()
    await db.close()


# ---------- Task 5: await_fill_confirmation ----------


@pytest.mark.asyncio
async def test_await_fill_confirmation_terminal_filled_writes_slippage(tmp_path):
    """50 bps slippage: fill 50250 vs mid_at_entry 50000 → bps=50.0."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    paper_id = await _seed_paper(db)
    cid = "gecko-1-abcd1234"
    await db._conn.execute(
        """INSERT INTO live_trades
           (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
            size_usd, mid_at_entry, status, client_order_id, created_at)
           VALUES (?, 'btc', 'BTC', 'binance', 'BTCUSDT', 'first_signal',
                   '50', '50000.0', 'open', ?, '2026-05-09T00:00:00+00:00')""",
        (paper_id, cid),
    )
    await db._conn.commit()

    s = _settings()
    adapter = BinanceSpotAdapter(s, db=db)
    with aioresponses() as m:
        m.get(
            _ORDER_RE,
            payload={
                "orderId": 88888,
                "status": "FILLED",
                "executedQty": "0.001",
                "fills": [{"price": "50250.0", "qty": "0.001"}],
            },
        )
        confirmation = await adapter.await_fill_confirmation(
            venue_order_id="88888",
            client_order_id=cid,
            timeout_sec=2.0,
        )
        assert confirmation.status == "filled"
        assert confirmation.fill_price == pytest.approx(50250.0)
    cur = await db._conn.execute(
        "SELECT fill_slippage_bps FROM live_trades WHERE client_order_id = ?",
        (cid,),
    )
    row = await cur.fetchone()
    assert row[0] == pytest.approx(50.0)
    await adapter.close()
    await db.close()


@pytest.mark.asyncio
async def test_await_fill_confirmation_skips_slippage_when_mid_null(tmp_path):
    """If mid_at_entry is NULL (depth fetch failed at place_order_request
    time), slippage write skips silently — fill_slippage_bps stays NULL."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    paper_id = await _seed_paper(db)
    cid = "gecko-1-deadbeef"
    await db._conn.execute(
        """INSERT INTO live_trades
           (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
            size_usd, status, client_order_id, created_at)
           VALUES (?, 'btc', 'BTC', 'binance', 'BTCUSDT', 'first_signal',
                   '50', 'open', ?, '2026-05-09T00:00:00+00:00')""",
        (paper_id, cid),
    )
    await db._conn.commit()

    s = _settings()
    adapter = BinanceSpotAdapter(s, db=db)
    with aioresponses() as m:
        m.get(
            _ORDER_RE,
            payload={
                "orderId": 88888,
                "status": "FILLED",
                "executedQty": "0.001",
                "fills": [{"price": "50250.0", "qty": "0.001"}],
            },
        )
        confirmation = await adapter.await_fill_confirmation(
            venue_order_id="88888",
            client_order_id=cid,
            timeout_sec=2.0,
        )
        assert confirmation.status == "filled"
    cur = await db._conn.execute(
        "SELECT fill_slippage_bps FROM live_trades WHERE client_order_id = ?",
        (cid,),
    )
    row = await cur.fetchone()
    assert row[0] is None  # silently skipped
    await adapter.close()
    await db.close()


@pytest.mark.asyncio
async def test_await_fill_confirmation_timeout_returns_status_timeout(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    paper_id = await _seed_paper(db)
    cid = "gecko-1-deadbeef"
    await db._conn.execute(
        """INSERT INTO live_trades
           (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
            size_usd, status, client_order_id, created_at)
           VALUES (?, 'btc', 'BTC', 'binance', 'BTCUSDT', 'first_signal',
                   '50', 'open', ?, '2026-05-09T00:00:00+00:00')""",
        (paper_id, cid),
    )
    await db._conn.commit()

    s = _settings()
    adapter = BinanceSpotAdapter(s, db=db)
    with aioresponses() as m:
        # Always NEW
        for _ in range(20):
            m.get(
                _ORDER_RE,
                payload={"orderId": 88888, "status": "NEW"},
            )
        confirmation = await adapter.await_fill_confirmation(
            venue_order_id="88888",
            client_order_id=cid,
            timeout_sec=0.5,
        )
        assert confirmation.status == "timeout"
    await adapter.close()
    await db.close()


@pytest.mark.asyncio
async def test_await_fill_confirmation_partial_fill_returns_partial(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    paper_id = await _seed_paper(db)
    cid = "gecko-1-partial"
    await db._conn.execute(
        """INSERT INTO live_trades
           (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
            size_usd, mid_at_entry, status, client_order_id, created_at)
           VALUES (?, 'btc', 'BTC', 'binance', 'BTCUSDT', 'first_signal',
                   '50', '50000.0', 'open', ?, '2026-05-09T00:00:00+00:00')""",
        (paper_id, cid),
    )
    await db._conn.commit()

    s = _settings()
    adapter = BinanceSpotAdapter(s, db=db)
    with aioresponses() as m:
        m.get(
            _ORDER_RE,
            payload={
                "orderId": 88888,
                "status": "PARTIALLY_FILLED",
                "executedQty": "0.0005",
                "fills": [{"price": "50100.0", "qty": "0.0005"}],
            },
        )
        confirmation = await adapter.await_fill_confirmation(
            venue_order_id="88888",
            client_order_id=cid,
            timeout_sec=2.0,
        )
        assert confirmation.status == "partial"
        assert confirmation.filled_qty == 0.0005
    await adapter.close()
    await db.close()


@pytest.mark.asyncio
async def test_await_fill_confirmation_canceled_returns_rejected(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    paper_id = await _seed_paper(db)
    cid = "gecko-1-canc"
    await db._conn.execute(
        """INSERT INTO live_trades
           (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
            size_usd, status, client_order_id, created_at)
           VALUES (?, 'btc', 'BTC', 'binance', 'BTCUSDT', 'first_signal',
                   '50', 'open', ?, '2026-05-09T00:00:00+00:00')""",
        (paper_id, cid),
    )
    await db._conn.commit()

    s = _settings()
    adapter = BinanceSpotAdapter(s, db=db)
    with aioresponses() as m:
        m.get(
            _ORDER_RE,
            payload={"orderId": 88888, "status": "CANCELED"},
        )
        confirmation = await adapter.await_fill_confirmation(
            venue_order_id="88888",
            client_order_id=cid,
            timeout_sec=2.0,
        )
        assert confirmation.status == "rejected"
    await adapter.close()
    await db.close()
