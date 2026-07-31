"""PR-K3: the supervised Kraken pilot runner.

The load-bearing assertions here are negative: no ``AddOrder`` POST when a
gate refuses, none when the operator does not type the authorization, none
when the kill switch flips after approval, and no resend after an ambiguous
submission. Where a test asserts a refusal it also asserts the venue was
never asked, because "refused" that still placed an order is the failure this
whole module exists to prevent.

Venue payload shapes follow the same AssetPairs / Ticker / BalanceEx /
AddOrder / CancelOrder / OpenOrders / ClosedOrders / QueryOrders /
QueryTrades schemas as tests/test_live_kraken_orders.py.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qs
from uuid import UUID

import pytest
from aioresponses import aioresponses

from scout.config import Settings
from scout.db import Database
from scout.live import kraken_pilot
from scout.live.kill_switch import KillSwitch
from scout.live.kraken_adapter import KrakenSpotAdapter
from scout.live.kraken_pilot import (
    EXIT_BLOCKED,
    build_parser,
    EXIT_ESCALATE,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_REVIEW,
    PilotLockHeld,
    PilotRunner,
)
from scout.live.kraken_signing import reset_nonce_sources_for_tests

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")

# Base64 so the signing primitive can decode it; not a real credential.
_TEST_SECRET = "dGVzdC1rcmFrZW4tc2VjcmV0LWZvci11bml0LXRlc3Rz"
_PLANTED_KEY = "planted-kraken-key-DO-NOT-LEAK"

# Fixed so the evidence filename and the authorization prefix are known here.
_DECISION = "6d1b345e-2821-40e2-ad83-4ecb18a06876"
_TXID = "OQCLML-BW3P3-BUCMWZ"

_ASSETPAIRS_RE = re.compile(r"https://api\.kraken\.com/0/public/AssetPairs.*")
_TICKER_RE = re.compile(r"https://api\.kraken\.com/0/public/Ticker.*")
_BALANCE_URL = "https://api.kraken.com/0/private/Balance"
_BALANCE_EX_URL = "https://api.kraken.com/0/private/BalanceEx"
_WITHDRAW_METHODS_URL = "https://api.kraken.com/0/private/WithdrawMethods"
_WITHDRAW_STATUS_URL = "https://api.kraken.com/0/private/WithdrawStatus"
_ADD_ORDER_URL = "https://api.kraken.com/0/private/AddOrder"
_CANCEL_URL = "https://api.kraken.com/0/private/CancelOrder"
_OPEN_ORDERS_URL = "https://api.kraken.com/0/private/OpenOrders"
_CLOSED_ORDERS_URL = "https://api.kraken.com/0/private/ClosedOrders"
_QUERY_ORDERS_URL = "https://api.kraken.com/0/private/QueryOrders"
_QUERY_TRADES_URL = "https://api.kraken.com/0/private/QueryTrades"

# Real XXBTZUSD row, trimmed to what the pilot and the order path read.
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
    "fees": [[0, 0.26], [50000, 0.24]],
    "fees_maker": [[0, 0.16], [50000, 0.14]],
}

# price * volume = 15.00 USD — inside the default [10, 25] per-order band.
_PRICE = Decimal("100.0")
_VOLUME = Decimal("0.15")


# ----------------------------------------------------------------------
# Wiring
# ----------------------------------------------------------------------
async def _no_sleep(_delay: float) -> None:
    return None


def _settings(tmp_path, **overrides) -> Settings:
    base = dict(
        KRAKEN_API_KEY=_PLANTED_KEY,
        KRAKEN_API_SECRET=_TEST_SECRET,
        LIVE_USE_REAL_SIGNED_REQUESTS=True,
        KRAKEN_PILOT_ENABLED=True,
        KRAKEN_PILOT_PAIR="BTC",
        KRAKEN_PILOT_QUOTE="USD",
        KRAKEN_PILOT_EVIDENCE_DIR=str(tmp_path / "evidence"),
        KRAKEN_PILOT_FILL_TIMEOUT_SEC=0.5,
        KRAKEN_FILL_POLL_INTERVAL_SEC=0.01,
        KRAKEN_SUBMISSION_SETTLE_SEC=0.0,
    )
    base.update(overrides)
    return Settings(_env_file=None, **_REQUIRED, **base)


async def _make_runner(tmp_path, **overrides):
    """Runner + db + adapter against a tmp DB. Caller closes both."""
    reset_nonce_sources_for_tests()
    settings = _settings(tmp_path, **overrides)
    db = Database(tmp_path / "pilot.db")
    await db.initialize()
    adapter = KrakenSpotAdapter(settings, db=db)
    adapter._retry_sleep = _no_sleep
    runner = PilotRunner(
        settings=settings, db=db, adapter=adapter, kill_switch=KillSwitch(db)
    )
    return runner, db, adapter


def _fix_decision_id(monkeypatch) -> None:
    monkeypatch.setattr(kraken_pilot, "uuid4", lambda: UUID(_DECISION))


def _authorize(monkeypatch, typed: str | None = None) -> None:
    """Feed the approval prompt. ``None`` raises EOF (non-interactive stdin)."""

    def _fake_input(_prompt: str = "") -> str:
        if typed is None:
            raise EOFError
        return typed

    monkeypatch.setattr("builtins.input", _fake_input)


def _evidence_path(tmp_path, decision_id: str = _DECISION):
    return tmp_path / "evidence" / f"kraken_pilot_{decision_id}.json"


def _evidence_steps(tmp_path, decision_id: str = _DECISION) -> list[dict]:
    text = _evidence_path(tmp_path, decision_id).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _step(steps: list[dict], name: str) -> dict | None:
    return next((s for s in steps if s["step"] == name), None)


def _calls(mocked: aioresponses) -> list:
    return [call for calls in mocked.requests.values() for call in calls]


def _posts_to(mocked: aioresponses, url: str) -> list:
    return [
        call
        for key, calls in mocked.requests.items()
        for call in calls
        if str(key[1]) == url
    ]


def _body(call) -> dict:
    raw = call.kwargs.get("data")
    return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}


# ----------------------------------------------------------------------
# Venue mocks
# ----------------------------------------------------------------------
def _balance_payload(usd: str = "1000.0", xbt: str = "1.0") -> dict:
    return {
        "error": [],
        "result": {
            "ZUSD": {"balance": usd, "hold_trade": "0.0"},
            "XXBT": {"balance": xbt, "hold_trade": "0.0"},
        },
    }


def _mock_preflight(m: aioresponses) -> None:
    m.post(_BALANCE_URL, payload={"error": [], "result": {}}, repeat=True)
    denied = {"error": ["EGeneral:Permission denied"], "result": {}}
    m.post(_WITHDRAW_METHODS_URL, payload=denied, repeat=True)
    m.post(_WITHDRAW_STATUS_URL, payload=denied, repeat=True)


def _mock_market(m: aioresponses, row: dict | None = None) -> None:
    m.get(
        _ASSETPAIRS_RE,
        payload={"error": [], "result": {"XXBTZUSD": row or _XBTUSD_ROW}},
        repeat=True,
    )
    # bid 99.9 / ask 100.1 → mid exactly 100.0, so the pilot's limit neither
    # crosses nor deviates.
    m.get(
        _TICKER_RE,
        payload={
            "error": [],
            "result": {
                "XXBTZUSD": {
                    "a": ["100.1", "1", "1"],
                    "b": ["99.9", "1", "1"],
                    "c": ["100.0", "1"],
                }
            },
        },
        repeat=True,
    )


def _mock_empty_order_lookups(m: aioresponses) -> None:
    m.post(_OPEN_ORDERS_URL, payload={"error": [], "result": {"open": {}}}, repeat=True)
    m.post(
        _CLOSED_ORDERS_URL, payload={"error": [], "result": {"closed": {}}}, repeat=True
    )


def _filled_order_row(**overrides) -> dict:
    row = {
        "cl_ord_id": _DECISION,
        "status": "closed",
        "descr": {"pair": "XBTUSD", "type": "buy", "ordertype": "limit"},
        "vol": "0.15000000",
        "vol_exec": "0.15000000",
        "cost": "15.00000",
        "fee": "0.03900",
        "price": "100.0",
        "trades": ["TRADE-1"],
    }
    row.update(overrides)
    return row


def _mock_happy_fill(m: aioresponses) -> None:
    m.post(
        _QUERY_ORDERS_URL,
        payload={"error": [], "result": {_TXID: _filled_order_row()}},
        repeat=True,
    )
    m.post(
        _QUERY_TRADES_URL,
        payload={
            "error": [],
            "result": {
                "TRADE-1": {
                    "ordertxid": _TXID,
                    "pair": "XXBTZUSD",
                    "time": 1785000000.0,
                    "type": "buy",
                    "ordertype": "limit",
                    "price": "100.0",
                    "cost": "15.00000",
                    "fee": "0.03900",
                    "vol": "0.15000000",
                    "maker": False,
                }
            },
        },
        repeat=True,
    )


def _mock_full_happy_path(m: aioresponses) -> None:
    _mock_preflight(m)
    _mock_market(m)
    _mock_empty_order_lookups(m)
    # Sampled twice: the pre-trade balance gate, then the reconciliation.
    # 1000.00 - (15.00 cost + 0.039 fee) = 984.961.
    m.post(_BALANCE_EX_URL, payload=_balance_payload("1000.0"))
    m.post(_BALANCE_EX_URL, payload=_balance_payload("984.961"))
    m.post(
        _ADD_ORDER_URL,
        payload={
            "error": [],
            "result": {
                "txid": [_TXID],
                "descr": {"order": "buy 0.15000000 XBTUSD @ limit 100.0"},
            },
        },
    )
    _mock_happy_fill(m)


# ----------------------------------------------------------------------
# Ledger seeding
# ----------------------------------------------------------------------
async def _seed_kraken_row(
    db: Database,
    *,
    size_usd: str,
    status: str = "open",
    client_order_id: str | None = None,
    entry_order_id: str | None = None,
    created_at: str | None = None,
) -> int:
    anchor = await kraken_pilot.ensure_pilot_anchor(db)
    now_iso = created_at or datetime.now(timezone.utc).isoformat()
    async with db._txn_lock:
        cur = await db._conn.execute(
            """INSERT INTO live_trades
               (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
                size_usd, status, client_order_id, entry_order_id, created_at)
               VALUES (?, 'kraken-pilot-btc', 'BTC', 'kraken', 'XBTUSD',
                       'kraken_pilot', ?, ?, ?, ?, ?)""",
            (anchor, size_usd, status, client_order_id, entry_order_id, now_iso),
        )
        await db._conn.commit()
        return int(cur.lastrowid)


async def _row_by_cid(db: Database, cid: str) -> dict | None:
    return await kraken_pilot.fetch_row_by_decision_id(db, cid)


async def _count(db: Database, table: str) -> int:
    cur = await db._conn.execute(f"SELECT COUNT(*) FROM {table}")
    return (await cur.fetchone())[0]


# ======================================================================
# Envelope gates
# ======================================================================
async def test_place_refuses_when_pilot_disabled(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    runner, db, adapter = await _make_runner(tmp_path, KRAKEN_PILOT_ENABLED=False)
    with aioresponses() as m:
        code = await runner.place(side="buy", price=_PRICE, volume=_VOLUME)
        assert code == EXIT_REFUSED
        assert _calls(m) == []
    steps = _evidence_steps(tmp_path)
    assert _step(steps, "aborted")["stage"] == "envelope_gate"
    await adapter.close()
    await db.close()


async def test_place_refuses_without_configured_pair(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    runner, db, adapter = await _make_runner(tmp_path, KRAKEN_PILOT_PAIR="")
    with aioresponses() as m:
        code = await runner.place(side="buy", price=_PRICE, volume=_VOLUME)
        assert code == EXIT_REFUSED
        assert _calls(m) == []
    assert "KRAKEN_PILOT_PAIR" in _step(_evidence_steps(tmp_path), "aborted")["reason"]
    await adapter.close()
    await db.close()


async def test_place_refuses_without_credentials(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    runner, db, adapter = await _make_runner(tmp_path, KRAKEN_API_SECRET=None)
    with aioresponses() as m:
        assert await runner.place(side="buy", price=_PRICE, volume=_VOLUME) == (
            EXIT_REFUSED
        )
        assert _calls(m) == []
    await adapter.close()
    await db.close()


async def test_place_refuses_when_real_signed_requests_disabled(tmp_path, monkeypatch):
    """A real order under the emergency-revert posture is refused BEFORE the
    operator is asked to authorize something that could not be placed."""
    _fix_decision_id(monkeypatch)
    runner, db, adapter = await _make_runner(
        tmp_path, LIVE_USE_REAL_SIGNED_REQUESTS=False
    )
    with aioresponses() as m:
        assert await runner.place(side="buy", price=_PRICE, volume=_VOLUME) == (
            EXIT_REFUSED
        )
        assert _calls(m) == []
    await adapter.close()
    await db.close()


# ======================================================================
# Caps
# ======================================================================
@pytest.mark.parametrize(
    "volume",
    [Decimal("0.0999"), Decimal("0.2501")],  # 9.99 USD and 25.01 USD
    ids=["below-min", "above-max"],
)
async def test_place_refuses_notional_outside_band(tmp_path, monkeypatch, volume):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_market(m)
        _mock_empty_order_lookups(m)
        code = await runner.place(side="buy", price=_PRICE, volume=volume)
        assert code == EXIT_REFUSED
        assert _posts_to(m, _ADD_ORDER_URL) == []
    abort = _step(_evidence_steps(tmp_path), "aborted")
    assert abort["stage"] == "caps"
    assert "per-order band" in abort["reason"]
    await adapter.close()
    await db.close()


async def test_place_refuses_when_daily_gross_would_exceed_cap(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)
    # 90 already today (terminal, so it does not block the lane) + 15 > 100.
    await _seed_kraken_row(db, size_usd="90.0", status="closed_tp")
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_market(m)
        _mock_empty_order_lookups(m)
        code = await runner.place(side="buy", price=_PRICE, volume=_VOLUME)
        assert code == EXIT_REFUSED
        assert _posts_to(m, _ADD_ORDER_URL) == []
    abort = _step(_evidence_steps(tmp_path), "aborted")
    assert abort["stage"] == "caps"
    assert "daily gross" in abort["reason"]
    await adapter.close()
    await db.close()


async def test_daily_gross_ignores_rejected_and_other_days(tmp_path):
    runner, db, adapter = await _make_runner(tmp_path)
    today = datetime.now(timezone.utc).date().isoformat()
    await _seed_kraken_row(db, size_usd="20.0", status="closed_tp")
    await _seed_kraken_row(db, size_usd="99.0", status="rejected")
    await _seed_kraken_row(
        db,
        size_usd="77.0",
        status="closed_tp",
        created_at="2020-01-01T00:00:00+00:00",
    )
    assert await kraken_pilot.daily_gross_usd(db, today) == Decimal("20.0")
    await adapter.close()
    await db.close()


async def test_place_refuses_when_price_violates_tick(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_market(m)
        _mock_empty_order_lookups(m)
        # 100.05 is not a multiple of the 0.1 tick.
        code = await runner.place(
            side="buy", price=Decimal("100.05"), volume=Decimal("0.15")
        )
        assert code == EXIT_REFUSED
        assert _posts_to(m, _ADD_ORDER_URL) == []
    abort = _step(_evidence_steps(tmp_path), "aborted")
    assert abort["stage"] == "market_rules"
    assert "tick_size" in abort["reason"]
    await adapter.close()
    await db.close()


async def test_place_refuses_when_balance_does_not_cover(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_market(m)
        _mock_empty_order_lookups(m)
        m.post(_BALANCE_EX_URL, payload=_balance_payload("5.0"), repeat=True)
        code = await runner.place(side="buy", price=_PRICE, volume=_VOLUME)
        assert code == EXIT_REFUSED
        assert _posts_to(m, _ADD_ORDER_URL) == []
    assert _step(_evidence_steps(tmp_path), "aborted")["stage"] == "balance"
    await adapter.close()
    await db.close()


# ======================================================================
# One live order at a time / startup reconciliation
# ======================================================================
async def test_existing_open_kraken_row_blocks_the_lane(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)
    await _seed_kraken_row(
        db, size_usd="12.0", status="open", client_order_id="prior-cid-0001"
    )
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_empty_order_lookups(m)
        code = await runner.place(side="buy", price=_PRICE, volume=_VOLUME)
        assert code == EXIT_BLOCKED
        assert _posts_to(m, _ADD_ORDER_URL) == []
    recon = _step(_evidence_steps(tmp_path), "startup_reconciliation")
    assert recon["blocker_count"] == 1
    assert recon["blocking_rows"][0]["venue"]["outcome"] == "absent"
    await adapter.close()
    await db.close()


async def test_needs_manual_review_row_blocks_the_lane(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)
    await _seed_kraken_row(
        db,
        size_usd="12.0",
        status="needs_manual_review",
        client_order_id="stuck-cid-0001",
    )
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_empty_order_lookups(m)
        assert await runner.place(side="buy", price=_PRICE, volume=_VOLUME) == (
            EXIT_BLOCKED
        )
        assert _posts_to(m, _ADD_ORDER_URL) == []
    await adapter.close()
    await db.close()


# ======================================================================
# Approval boundary
# ======================================================================
@pytest.mark.parametrize(
    "typed, detail",
    [("deadbeef", "mismatch"), ("", "empty"), (None, "no_input")],
    ids=["wrong-echo", "empty-line", "eof"],
)
async def test_place_aborts_without_correct_authorization(
    tmp_path, monkeypatch, typed, detail
):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, typed)
    runner, db, adapter = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_market(m)
        _mock_empty_order_lookups(m)
        m.post(_BALANCE_EX_URL, payload=_balance_payload(), repeat=True)
        code = await runner.place(side="buy", price=_PRICE, volume=_VOLUME)
        assert code == EXIT_REFUSED
        assert _posts_to(m, _ADD_ORDER_URL) == []
    steps = _evidence_steps(tmp_path)
    authorization = _step(steps, "authorization")
    assert authorization["outcome"] == "authorization_refused"
    assert authorization["detail"] == detail
    # Nothing was persisted — the intent row is only written to cover a POST.
    assert await _count(db, "live_trades") == 0
    await adapter.close()
    await db.close()


async def test_authorization_prefix_is_the_first_eight_chars(tmp_path, monkeypatch):
    """A 7- or 9-char echo is a refusal: the prompt asks for exactly 8."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:7])
    runner, db, adapter = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_market(m)
        _mock_empty_order_lookups(m)
        m.post(_BALANCE_EX_URL, payload=_balance_payload(), repeat=True)
        assert await runner.place(side="buy", price=_PRICE, volume=_VOLUME) == (
            EXIT_REFUSED
        )
        assert _posts_to(m, _ADD_ORDER_URL) == []
    await adapter.close()
    await db.close()


# ======================================================================
# Kill switch
# ======================================================================
async def test_kill_switch_active_refuses_before_anything_else(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)
    await runner._ks.trigger(
        triggered_by="manual", reason="ops halt", duration=timedelta(hours=4)
    )
    with aioresponses() as m:
        code = await runner.place(side="buy", price=_PRICE, volume=_VOLUME)
        assert code == EXIT_REFUSED
        assert _calls(m) == []
    assert _step(_evidence_steps(tmp_path), "aborted")["stage"] == "kill_switch_check"
    assert await _count(db, "live_trades") == 0
    await adapter.close()
    await db.close()


async def test_kill_switch_engaged_after_approval_aborts_before_submit(
    tmp_path, monkeypatch
):
    """The post-approval re-check is the one that matters: the intent row
    already exists, so the abort must also retire it."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)

    real_is_active = runner._ks.is_active
    seen = {"n": 0}

    async def flipping_is_active():
        seen["n"] += 1
        if seen["n"] == 2:
            # Genuinely engage the kill in the DB between approval and submit.
            await runner._ks.trigger(
                triggered_by="manual",
                reason="mid-flight halt",
                duration=timedelta(hours=4),
            )
        return await real_is_active()

    runner._ks.is_active = flipping_is_active

    with aioresponses() as m:
        _mock_preflight(m)
        _mock_market(m)
        _mock_empty_order_lookups(m)
        m.post(_BALANCE_EX_URL, payload=_balance_payload(), repeat=True)
        code = await runner.place(side="buy", price=_PRICE, volume=_VOLUME)
        assert code == EXIT_REFUSED
        assert _posts_to(m, _ADD_ORDER_URL) == []

    row = await _row_by_cid(db, _DECISION)
    assert row["status"] == "rejected"
    steps = _evidence_steps(tmp_path)
    assert _step(steps, "intent_persisted") is not None
    assert _step(steps, "kill_switch_recheck")["kill_active"] is True
    assert _step(steps, "aborted")["stage"] == "kill_switch_recheck"

    cur = await db._conn.execute(
        "SELECT reject_reason FROM live_trades WHERE client_order_id = ?", (_DECISION,)
    )
    assert (await cur.fetchone())[0] == "kill_switch"
    await adapter.close()
    await db.close()


# ======================================================================
# validate-only rehearsal
# ======================================================================
async def test_validate_only_requires_the_rehearsal_flag():
    """The guard runs before any settings or DB wiring, so this touches
    neither the real .env nor scout.db."""
    code = await kraken_pilot.main(
        [
            "place",
            "--side",
            "buy",
            "--price",
            "100.0",
            "--volume",
            "0.15",
            "--validate-only",
        ]
    )
    assert code == EXIT_REFUSED


async def test_rehearsal_flag_without_validate_only_is_refused():
    code = await kraken_pilot.main(
        [
            "place",
            "--side",
            "buy",
            "--price",
            "100.0",
            "--volume",
            "0.15",
            "--yes-i-am-rehearsing",
        ]
    )
    assert code == EXIT_REFUSED


async def test_validate_only_sends_validate_true_and_writes_no_ledger_row(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    # Deliberately under the emergency-revert posture: a venue-side dry run is
    # exempt, which is what makes the rehearsal exercise the real code path.
    runner, db, adapter = await _make_runner(
        tmp_path, LIVE_USE_REAL_SIGNED_REQUESTS=False
    )
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_market(m)
        _mock_empty_order_lookups(m)
        m.post(_BALANCE_EX_URL, payload=_balance_payload(), repeat=True)
        m.post(
            _ADD_ORDER_URL,
            payload={
                "error": [],
                "result": {"descr": {"order": "buy 0.15000000 XBTUSD @ limit 100.0"}},
            },
        )
        code = await runner.place(
            side="buy", price=_PRICE, volume=_VOLUME, validate_only=True
        )
        assert code == EXIT_OK
        body = _body(_posts_to(m, _ADD_ORDER_URL)[0])
        assert body["validate"] == "true"
        assert body["cl_ord_id"] == _DECISION

    assert await _count(db, "live_trades") == 0
    # No anchor either — a rehearsal leaves nothing behind at all.
    assert await _count(db, "paper_trades") == 0
    steps = _evidence_steps(tmp_path)
    assert _step(steps, "intent_skipped") is not None
    assert _step(steps, "validate_only_accepted") is not None
    await adapter.close()
    await db.close()


# ======================================================================
# Happy path
# ======================================================================
async def test_place_happy_path_persists_txid_fill_and_reconciles(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_full_happy_path(m)
        code = await runner.place(side="buy", price=_PRICE, volume=_VOLUME)
        assert code == EXIT_OK
        body = _body(_posts_to(m, _ADD_ORDER_URL)[0])
        assert body["pair"] == "XBTUSD"
        assert body["type"] == "buy"
        assert body["ordertype"] == "limit"
        assert body["price"] == "100.0"
        assert body["volume"] == "0.15000000"
        assert body["cl_ord_id"] == _DECISION
        assert "validate" not in body

    row = await _row_by_cid(db, _DECISION)
    assert row["status"] == "open"  # a filled ENTRY leaves an open position
    assert row["entry_order_id"] == _TXID
    assert Decimal(row["entry_fill_price"]) == Decimal("100.0")
    assert Decimal(row["entry_fill_qty"]) == Decimal("0.15")
    assert Decimal(row["size_usd"]) == Decimal("15.00")

    steps = _evidence_steps(tmp_path)
    assert [s["step"] for s in steps][:3] == [
        "run_started",
        "envelope_gate",
        "kill_switch_check",
    ]
    # live_trades has no fee column, so the per-fill fee record lives in the
    # evidence file — assert it is actually there rather than assumed.
    fills = _step(steps, "fills")
    assert fills["fill_count"] == 1
    assert fills["fills"][0]["fee"] == "0.03900"
    assert fills["fills"][0]["price"] == "100.0"
    reconciliation = _step(steps, "reconciliation")
    assert reconciliation["verdict"] == "pass"
    assert reconciliation["mismatches"] == []
    assert reconciliation["venue"]["fees_paid"] == "0.03900"
    assert reconciliation["balance"]["observed_delta"] == "-15.039"
    await adapter.close()
    await db.close()


async def test_place_reports_review_when_balance_delta_is_unexplained(
    tmp_path, monkeypatch
):
    """A balance that moved by something other than cost+fee is surfaced,
    never silently accepted."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_market(m)
        _mock_empty_order_lookups(m)
        m.post(_BALANCE_EX_URL, payload=_balance_payload("1000.0"))
        m.post(_BALANCE_EX_URL, payload=_balance_payload("900.0"))  # -100, not -15.04
        m.post(
            _ADD_ORDER_URL,
            payload={"error": [], "result": {"txid": [_TXID], "descr": {}}},
        )
        _mock_happy_fill(m)
        # Exit 4, not 0: the order completed but the money move is unexplained.
        assert (
            await runner.place(side="buy", price=_PRICE, volume=_VOLUME) == EXIT_REVIEW
        )

    reconciliation = _step(_evidence_steps(tmp_path), "reconciliation")
    assert reconciliation["verdict"] == "review"
    assert any("balance delta" in msg for msg in reconciliation["mismatches"])
    await adapter.close()
    await db.close()


async def test_place_leaves_row_open_when_limit_order_is_still_resting(
    tmp_path, monkeypatch
):
    """A limit order that has not filled inside the window is FINE — the row
    stays open and the operator is told it is still working."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_market(m)
        _mock_empty_order_lookups(m)
        m.post(_BALANCE_EX_URL, payload=_balance_payload("1000.0"))
        m.post(_BALANCE_EX_URL, payload=_balance_payload("985.0"))
        m.post(
            _ADD_ORDER_URL,
            payload={"error": [], "result": {"txid": [_TXID], "descr": {}}},
        )
        m.post(
            _QUERY_ORDERS_URL,
            payload={
                "error": [],
                "result": {
                    _TXID: _filled_order_row(
                        status="open", vol_exec="0.00000000", cost="0.00000", trades=[]
                    )
                },
            },
            repeat=True,
        )
        assert await runner.place(side="buy", price=_PRICE, volume=_VOLUME) == EXIT_OK

    row = await _row_by_cid(db, _DECISION)
    assert row["status"] == "open"
    assert row["entry_order_id"] == _TXID
    steps = _evidence_steps(tmp_path)
    assert _step(steps, "fill_confirmation")["status"] == "timeout"
    assert _step(steps, "order_resting") is not None
    await adapter.close()
    await db.close()


# ======================================================================
# Ambiguous submission
# ======================================================================
async def test_ambiguous_submission_resolved_accepted_is_adopted(tmp_path, monkeypatch):
    """The order DID land. Adopt it — never resend."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_market(m)
        m.post(_BALANCE_EX_URL, payload=_balance_payload("1000.0"))
        m.post(_BALANCE_EX_URL, payload=_balance_payload("984.961"))
        m.post(_ADD_ORDER_URL, exception=TimeoutError("connection dropped"))
        # Step 4 runs BEFORE the order exists, so the first OpenOrders read is
        # an empty book; every later read (resolver probes, adoption lookup,
        # post-trade listing) finds the order that landed.
        m.post(_OPEN_ORDERS_URL, payload={"error": [], "result": {"open": {}}})
        m.post(
            _OPEN_ORDERS_URL,
            payload={"error": [], "result": {"open": {_TXID: _filled_order_row()}}},
            repeat=True,
        )
        m.post(
            _CLOSED_ORDERS_URL,
            payload={"error": [], "result": {"closed": {}}},
            repeat=True,
        )
        _mock_happy_fill(m)
        code = await runner.place(side="buy", price=_PRICE, volume=_VOLUME)
        assert code == EXIT_OK
        # Exactly ONE AddOrder POST. A resend here is a double order.
        assert len(_posts_to(m, _ADD_ORDER_URL)) == 1

    row = await _row_by_cid(db, _DECISION)
    assert row["entry_order_id"] == _TXID
    steps = _evidence_steps(tmp_path)
    assert _step(steps, "ambiguity_resolution")["verdict"] == "accepted"
    assert _step(steps, "ambiguity_adopted")["txid"] == _TXID
    await adapter.close()
    await db.close()


async def test_ambiguous_submission_resolved_not_accepted_rejects_the_row(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_market(m)
        _mock_empty_order_lookups(m)
        m.post(_BALANCE_EX_URL, payload=_balance_payload(), repeat=True)
        m.post(_ADD_ORDER_URL, exception=TimeoutError("connection dropped"))
        code = await runner.place(side="buy", price=_PRICE, volume=_VOLUME)
        assert code == EXIT_REFUSED
        assert len(_posts_to(m, _ADD_ORDER_URL)) == 1

    row = await _row_by_cid(db, _DECISION)
    assert row["status"] == "rejected"
    cur = await db._conn.execute(
        "SELECT reject_reason FROM live_trades WHERE client_order_id = ?", (_DECISION,)
    )
    assert (await cur.fetchone())[0] == "venue_unavailable"
    assert (
        _step(_evidence_steps(tmp_path), "ambiguity_resolution")["verdict"]
        == "not_accepted"
    )
    await adapter.close()
    await db.close()


async def test_unresolved_submission_escalates_and_blocks_the_next_run(
    tmp_path, monkeypatch
):
    """Restart-cannot-forget: the block lives in the DB, not the process."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_market(m)
        m.post(_BALANCE_EX_URL, payload=_balance_payload(), repeat=True)
        m.post(_ADD_ORDER_URL, exception=TimeoutError("connection dropped"))
        # Step 4's venue listing succeeds (empty book, lane clear); it is the
        # resolver's probes that fail. A probe that FAILED is not evidence of
        # absence, so no number of sweeps reaches the not_accepted verdict
        # that would license a resend.
        m.post(_OPEN_ORDERS_URL, payload={"error": [], "result": {"open": {}}})
        m.post(
            _OPEN_ORDERS_URL,
            payload={"error": ["EGeneral:Invalid arguments"], "result": {}},
            repeat=True,
        )
        m.post(
            _CLOSED_ORDERS_URL,
            payload={"error": [], "result": {"closed": {}}},
            repeat=True,
        )
        code = await runner.place(side="buy", price=_PRICE, volume=_VOLUME)
        assert code == EXIT_ESCALATE

    row = await _row_by_cid(db, _DECISION)
    assert row["status"] == "needs_manual_review"

    # A brand-new runner over the same DB refuses to place anything.
    second = PilotRunner(
        settings=_settings(tmp_path),
        db=db,
        adapter=adapter,
        kill_switch=KillSwitch(db),
    )
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_empty_order_lookups(m)
        code = await second.place(side="buy", price=_PRICE, volume=_VOLUME)
        assert code == EXIT_BLOCKED
        assert _posts_to(m, _ADD_ORDER_URL) == []
    await adapter.close()
    await db.close()


# ======================================================================
# Cancel
# ======================================================================
async def test_cancel_zero_fill_rejects_the_row(tmp_path, monkeypatch):
    runner, db, adapter = await _make_runner(tmp_path)
    live_id = await _seed_kraken_row(
        db,
        size_usd="15.00",
        status="open",
        client_order_id=_DECISION,
        entry_order_id=_TXID,
    )
    with aioresponses() as m:
        m.post(_CANCEL_URL, payload={"error": [], "result": {"count": 1}})
        m.post(
            _OPEN_ORDERS_URL, payload={"error": [], "result": {"open": {}}}, repeat=True
        )
        m.post(
            _CLOSED_ORDERS_URL,
            payload={
                "error": [],
                "result": {
                    "closed": {
                        _TXID: _filled_order_row(
                            status="canceled", vol_exec="0.00000000", cost="0.00000"
                        )
                    }
                },
            },
            repeat=True,
        )
        m.post(_BALANCE_EX_URL, payload=_balance_payload(), repeat=True)
        assert await runner.cancel(decision_id=_DECISION) == EXIT_OK
        assert _body(_posts_to(m, _CANCEL_URL)[0])["txid"] == _TXID

    row = await _row_by_cid(db, _DECISION)
    assert row["live_trade_id"] == live_id
    assert row["status"] == "rejected"
    cur = await db._conn.execute(
        "SELECT reject_reason FROM live_trades WHERE client_order_id = ?", (_DECISION,)
    )
    assert (await cur.fetchone())[0] is None
    assert _step(_evidence_steps(tmp_path), "cancel_zero_fill") is not None
    await adapter.close()
    await db.close()


async def test_cancel_partial_fill_keeps_the_row_open(tmp_path, monkeypatch):
    """A partial fill left real inventory — 'rejected' would assert nothing
    happened while the account holds coins."""
    runner, db, adapter = await _make_runner(tmp_path)
    await _seed_kraken_row(
        db,
        size_usd="15.00",
        status="open",
        client_order_id=_DECISION,
        entry_order_id=_TXID,
    )
    with aioresponses() as m:
        m.post(_CANCEL_URL, payload={"error": [], "result": {"count": 1}})
        m.post(
            _OPEN_ORDERS_URL, payload={"error": [], "result": {"open": {}}}, repeat=True
        )
        m.post(
            _CLOSED_ORDERS_URL,
            payload={
                "error": [],
                "result": {
                    "closed": {
                        _TXID: _filled_order_row(
                            status="canceled", vol_exec="0.05000000", cost="5.00000"
                        )
                    }
                },
            },
            repeat=True,
        )
        m.post(_QUERY_ORDERS_URL, payload={"error": [], "result": {}}, repeat=True)
        m.post(_BALANCE_EX_URL, payload=_balance_payload(), repeat=True)
        assert await runner.cancel(decision_id=_DECISION) == EXIT_OK

    row = await _row_by_cid(db, _DECISION)
    assert row["status"] == "open"
    assert Decimal(row["entry_fill_qty"]) == Decimal("0.05")
    partial = _step(_evidence_steps(tmp_path), "cancel_partial_fill")
    assert partial["ledger_status"] == "open"
    await adapter.close()
    await db.close()


async def test_cancel_works_with_the_master_gate_off(tmp_path):
    """Turning KRAKEN_PILOT_ENABLED off must not strand a resting order —
    the gate cannot be allowed to disable the exposure-reducing lever."""
    runner, db, adapter = await _make_runner(tmp_path, KRAKEN_PILOT_ENABLED=False)
    await _seed_kraken_row(
        db,
        size_usd="15.00",
        status="open",
        client_order_id=_DECISION,
        entry_order_id=_TXID,
    )
    with aioresponses() as m:
        m.post(_CANCEL_URL, payload={"error": [], "result": {"count": 1}})
        m.post(
            _OPEN_ORDERS_URL, payload={"error": [], "result": {"open": {}}}, repeat=True
        )
        m.post(
            _CLOSED_ORDERS_URL,
            payload={
                "error": [],
                "result": {
                    "closed": {
                        _TXID: _filled_order_row(
                            status="canceled", vol_exec="0.00000000", cost="0.00000"
                        )
                    }
                },
            },
            repeat=True,
        )
        m.post(_BALANCE_EX_URL, payload=_balance_payload(), repeat=True)
        assert await runner.cancel(decision_id=_DECISION) == EXIT_OK

    assert (await _row_by_cid(db, _DECISION))["status"] == "rejected"
    envelope = _step(_evidence_steps(tmp_path), "envelope_gate")
    assert envelope["pilot_enabled"] is False
    await adapter.close()
    await db.close()


async def test_cancel_refuses_unknown_decision_id(tmp_path):
    runner, db, adapter = await _make_runner(tmp_path)
    with aioresponses() as m:
        code = await runner.cancel(decision_id=_DECISION)
        assert code == EXIT_REFUSED
        assert _posts_to(m, _CANCEL_URL) == []
    assert _step(_evidence_steps(tmp_path), "aborted")["stage"] == "lookup"
    await adapter.close()
    await db.close()


# ======================================================================
# Evidence
# ======================================================================
async def test_evidence_is_jsonlines_with_an_authorization_record_and_no_secrets(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_full_happy_path(m)
        assert await runner.place(side="buy", price=_PRICE, volume=_VOLUME) == EXIT_OK

    path = _evidence_path(tmp_path)
    assert path.exists()
    raw = path.read_text(encoding="utf-8")
    steps = [json.loads(line) for line in raw.splitlines() if line.strip()]
    assert len(steps) > 8
    assert all("step" in s and "at" in s for s in steps)
    # Every timestamp parses as ISO-8601 UTC.
    for step in steps:
        assert datetime.fromisoformat(step["at"]).tzinfo is not None

    authorization = _step(steps, "authorization")
    assert authorization["outcome"] == "authorized"

    assert _PLANTED_KEY not in raw
    assert _TEST_SECRET not in raw
    await adapter.close()
    await db.close()


async def test_evidence_survives_a_crash_mid_run(tmp_path, monkeypatch):
    """Every completed step is on disk before the next one starts, so an
    unexpected failure cannot cost the record of what already happened."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated crash after the caps step")

    with aioresponses() as m:
        _mock_preflight(m)
        _mock_market(m)
        _mock_empty_order_lookups(m)
        monkeypatch.setattr(adapter, "fetch_account_balance", _boom)
        code = await runner.place(side="buy", price=_PRICE, volume=_VOLUME)
        assert code == EXIT_ESCALATE

    steps = _evidence_steps(tmp_path)
    names = [s["step"] for s in steps]
    assert "envelope_gate" in names and "caps" in names
    assert _step(steps, "unexpected_error")["error_type"] == "RuntimeError"
    await adapter.close()
    await db.close()


async def test_evidence_scrubs_secret_shaped_keys():
    scrubbed = kraken_pilot._scrub(
        {
            "api_key": "abc",
            "API-Sign": "xyz",
            "nested": {"kraken_api_secret": "s3cr3t"},
            "signal_type": "kraken_pilot",
            "client_order_id": _DECISION,
        }
    )
    assert scrubbed["api_key"] == "[REDACTED]"
    assert scrubbed["API-Sign"] == "[REDACTED]"
    assert scrubbed["nested"]["kraken_api_secret"] == "[REDACTED]"
    # Fields whose names merely contain 'sign' must survive intact.
    assert scrubbed["signal_type"] == "kraken_pilot"
    assert scrubbed["client_order_id"] == _DECISION


# ======================================================================
# Ledger anchor
# ======================================================================
async def test_pilot_anchor_is_created_once_and_hidden_from_paper_readers(tmp_path):
    runner, db, adapter = await _make_runner(tmp_path)
    first = await kraken_pilot.ensure_pilot_anchor(db)
    second = await kraken_pilot.ensure_pilot_anchor(db)
    assert first == second
    assert await _count(db, "paper_trades") == 1

    # Not 'open', so no open-position loop scans it.
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE status = 'open'"
    )
    assert (await cur.fetchone())[0] == 0
    # Epoch opened_at keeps it out of every "today" / recent-window query.
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE date(opened_at) = date('now')"
    )
    assert (await cur.fetchone())[0] == 0
    cur = await db._conn.execute(
        "SELECT closed_at FROM paper_trades WHERE id = ?", (first,)
    )
    assert (await cur.fetchone())[0] is None
    await adapter.close()
    await db.close()


# ======================================================================
# Status
# ======================================================================
async def test_status_is_read_only_and_reports_blockers(tmp_path, capsys):
    runner, db, adapter = await _make_runner(tmp_path)
    await _seed_kraken_row(
        db, size_usd="12.0", status="open", client_order_id="prior-cid-0001"
    )
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_empty_order_lookups(m)
        m.post(_BALANCE_EX_URL, payload=_balance_payload(), repeat=True)
        assert await runner.status() == EXIT_OK
        assert _posts_to(m, _ADD_ORDER_URL) == []
        assert _posts_to(m, _CANCEL_URL) == []
    out = capsys.readouterr().out
    assert "KRAKEN PILOT STATUS" in out
    assert "blocking ledger rows     : 1" in out
    assert "BLOCKED" in out
    await adapter.close()
    await db.close()


# ======================================================================
# Config
# ======================================================================
def test_pilot_cap_defaults_are_safe():
    s = Settings(_env_file=None, **_REQUIRED)
    assert s.KRAKEN_PILOT_ENABLED is False
    assert s.KRAKEN_PILOT_PAIR == ""
    assert s.KRAKEN_PILOT_MIN_ORDER_USD == 10.0
    assert s.KRAKEN_PILOT_MAX_ORDER_USD == 25.0
    assert s.KRAKEN_PILOT_MAX_DAILY_GROSS_USD == 100.0


def test_pilot_caps_reject_inverted_min_max():
    with pytest.raises(ValueError, match="KRAKEN_PILOT_MIN_ORDER_USD"):
        Settings(
            _env_file=None,
            **_REQUIRED,
            KRAKEN_PILOT_MIN_ORDER_USD=30.0,
            KRAKEN_PILOT_MAX_ORDER_USD=25.0,
        )


def test_pilot_caps_reject_order_cap_above_daily_gross():
    with pytest.raises(ValueError, match="KRAKEN_PILOT_MAX_DAILY_GROSS_USD"):
        Settings(
            _env_file=None,
            **_REQUIRED,
            KRAKEN_PILOT_MAX_ORDER_USD=150.0,
            KRAKEN_PILOT_MAX_DAILY_GROSS_USD=100.0,
        )


# ======================================================================
# F1 — a limit price bounds the price, not the size
# ======================================================================
def _mock_venue_open(m: aioresponses, orders: dict, *, repeat: bool = True) -> None:
    """Register the account-wide OpenOrders listing step 4 reads."""
    m.post(
        _OPEN_ORDERS_URL,
        payload={"error": [], "result": {"open": orders}},
        repeat=repeat,
    )


def _resting_order(**overrides) -> dict:
    row = {
        "status": "open",
        "descr": {"pair": "XBTUSD", "type": "buy", "price": "95.0"},
        "vol": "0.20000000",
        "vol_exec": "0.00000000",
    }
    row.update(overrides)
    return row


async def test_marketable_sell_is_capped_on_what_it_transacts_not_its_limit(
    tmp_path, monkeypatch
):
    """A sell priced far below the mid does not sell for its limit — it sells
    into the book at ~mid. Bounding price*volume bounds nothing here: 1.5 units
    at a $10 limit reads as $15 while disposing of $150 at a $100 mid."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_market(m)  # mid = 100.0
        _mock_empty_order_lookups(m)
        m.post(_BALANCE_EX_URL, payload=_balance_payload(xbt="10.0"), repeat=True)
        code = await runner.place(
            side="sell", price=Decimal("10.0"), volume=Decimal("1.5")
        )
        assert code == EXIT_REFUSED
        assert _posts_to(m, _ADD_ORDER_URL) == []

    abort = _step(_evidence_steps(tmp_path), "aborted")
    assert abort["stage"] == "caps"
    assert "150" in abort["reason"]
    assert "bounds the price, not the size" in abort["reason"]
    await adapter.close()
    await db.close()


async def test_marketable_buy_is_capped_on_what_it_transacts(tmp_path, monkeypatch):
    """The same cap applies on the buy side — the check does not assume the
    fill price is the one field of an order that cannot surprise us."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_market(m)  # mid = 100.0
        _mock_empty_order_lookups(m)
        m.post(_BALANCE_EX_URL, payload=_balance_payload("100000.0"), repeat=True)
        # 0.30 units at limit 60 = 18.0 (inside the band) but 30.0 at the mid,
        # which is over the 25.0 per-order cap.
        code = await runner.place(
            side="buy", price=Decimal("60.0"), volume=Decimal("0.30")
        )
        assert code == EXIT_REFUSED
        assert _posts_to(m, _ADD_ORDER_URL) == []

    abort = _step(_evidence_steps(tmp_path), "aborted")
    assert abort["stage"] == "caps"
    assert "30.00" in abort["reason"]
    await adapter.close()
    await db.close()


async def test_ledger_records_exposure_taken_not_exposure_requested(
    tmp_path, monkeypatch
):
    """size_usd is the larger of the limit and market notionals, so the daily
    gross counts what the order can actually transact."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_market(m)  # mid = 100.0
        _mock_empty_order_lookups(m)
        # A resting limit BUY holds price*volume (18.0), not the mid-priced
        # figure — the hold follows the order, the cap follows the risk.
        m.post(_BALANCE_EX_URL, payload=_balance_payload("1000.0"))
        m.post(_BALANCE_EX_URL, payload=_balance_payload("982.0"))
        m.post(
            _ADD_ORDER_URL,
            payload={"error": [], "result": {"txid": [_TXID], "descr": {}}},
        )
        m.post(
            _QUERY_ORDERS_URL,
            payload={
                "error": [],
                "result": {
                    _TXID: _filled_order_row(
                        status="open",
                        vol="0.20000000",
                        vol_exec="0.00000000",
                        trades=[],
                    )
                },
            },
            repeat=True,
        )
        # limit 90 * 0.2 = 18.0 requested; 100 * 0.2 = 20.0 at the mid.
        code = await runner.place(
            side="buy", price=Decimal("90.0"), volume=Decimal("0.20")
        )
        assert code == EXIT_OK

    caps = _step(_evidence_steps(tmp_path), "caps")
    assert Decimal(caps["notional"]) == Decimal("18.0")
    assert Decimal(caps["market_notional"]) == Decimal("20.0")
    assert Decimal(caps["effective_notional"]) == Decimal("20.0")
    row = await _row_by_cid(db, _DECISION)
    assert Decimal(row["size_usd"]) == Decimal("20.00")
    # And that larger figure is what the day's cap accounting sees.
    today = datetime.now(timezone.utc).date().isoformat()
    assert await kraken_pilot.daily_gross_usd(db, today) == Decimal("20.00")
    await adapter.close()
    await db.close()


async def test_sell_checks_the_base_asset_balance(tmp_path, monkeypatch):
    """A sell is funded in BASE units, not quote — a full USD balance does not
    make an unfunded sell affordable."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_market(m)
        _mock_empty_order_lookups(m)
        # Plenty of USD, almost no BTC.
        m.post(
            _BALANCE_EX_URL,
            payload=_balance_payload("1000000.0", xbt="0.01"),
            repeat=True,
        )
        code = await runner.place(
            side="sell", price=Decimal("100.0"), volume=Decimal("0.15")
        )
        assert code == EXIT_REFUSED
        assert _posts_to(m, _ADD_ORDER_URL) == []

    steps = _evidence_steps(tmp_path)
    abort = _step(steps, "aborted")
    assert abort["stage"] == "balance"
    assert "BTC" in abort["reason"]
    await adapter.close()
    await db.close()


async def test_sell_below_the_mid_warns_that_it_crosses(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    runner, db, adapter = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_market(m)  # mid = 100.0
        _mock_empty_order_lookups(m)
        m.post(_BALANCE_EX_URL, payload=_balance_payload(xbt="10.0"), repeat=True)
        # 0.2 units at 99 = 19.8 limit, 20.0 at the mid: both inside the band,
        # so the run reaches the approval block and the crossing is a warning.
        _authorize(monkeypatch, "nope")
        await runner.place(side="sell", price=Decimal("99.0"), volume=Decimal("0.20"))

    caps = _step(_evidence_steps(tmp_path), "caps")
    assert caps["crossing"] is True
    assert any("CROSSES" in w for w in caps["warnings"])
    await adapter.close()
    await db.close()


async def test_sell_reconciliation_measures_the_base_asset(tmp_path, monkeypatch):
    """The expected balance move on a sell is in BASE units — the tolerance
    band must be scaled to the sold quantity, not to a quote notional."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_market(m)
        _mock_empty_order_lookups(m)
        # BTC balance before / after: 10.0 -> 9.85 (the 0.15 sold).
        m.post(_BALANCE_EX_URL, payload=_balance_payload(xbt="10.0"))
        m.post(_BALANCE_EX_URL, payload=_balance_payload(xbt="9.85"))
        m.post(
            _ADD_ORDER_URL,
            payload={"error": [], "result": {"txid": [_TXID], "descr": {}}},
        )
        m.post(
            _QUERY_ORDERS_URL,
            payload={"error": [], "result": {_TXID: _filled_order_row()}},
            repeat=True,
        )
        m.post(
            _QUERY_TRADES_URL,
            payload={
                "error": [],
                "result": {
                    "TRADE-1": {
                        "ordertxid": _TXID,
                        "pair": "XXBTZUSD",
                        "type": "sell",
                        "price": "100.0",
                        "cost": "15.00000",
                        "fee": "0.03900",
                        "vol": "0.15000000",
                    }
                },
            },
            repeat=True,
        )
        code = await runner.place(side="sell", price=_PRICE, volume=_VOLUME)
        assert code == EXIT_OK

    reconciliation = _step(_evidence_steps(tmp_path), "reconciliation")
    assert reconciliation["verdict"] == "pass"
    assert reconciliation["balance"]["asset"] == "BTC"
    assert Decimal(reconciliation["balance"]["expected_delta"]) == Decimal("-0.15")
    assert Decimal(reconciliation["balance"]["observed_delta"]) == Decimal("-0.15")
    await adapter.close()
    await db.close()


# ======================================================================
# C2 — the venue's own open-order listing
# ======================================================================
async def test_resting_order_the_ledger_cannot_name_blocks_the_lane(
    tmp_path, monkeypatch
):
    """The direction a client-id lookup cannot cover: an order placed from the
    web UI has no cl_ord_id we would think to ask about, so a ledger-driven
    check reports a clear lane while the account holds a live order."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_venue_open(m, {"MANUAL-TXID-01": _resting_order()})
        m.post(
            _CLOSED_ORDERS_URL,
            payload={"error": [], "result": {"closed": {}}},
            repeat=True,
        )
        code = await runner.place(side="buy", price=_PRICE, volume=_VOLUME)
        assert code == EXIT_BLOCKED
        assert _posts_to(m, _ADD_ORDER_URL) == []

    recon = _step(_evidence_steps(tmp_path), "startup_reconciliation")
    assert recon["blocking_rows"] == []  # the ledger is clean
    assert recon["venue_open_count"] == 1
    assert len(recon["unknown_orders"]) == 1
    assert recon["unknown_orders"][0]["txid"] == "MANUAL-TXID-01"
    await adapter.close()
    await db.close()


async def test_venue_order_matching_a_blocking_row_is_not_counted_twice(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)
    await _seed_kraken_row(
        db,
        size_usd="15.00",
        status="open",
        client_order_id="known-cid-0001",
        entry_order_id=_TXID,
    )
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_venue_open(m, {_TXID: _resting_order(cl_ord_id="known-cid-0001")})
        m.post(
            _CLOSED_ORDERS_URL,
            payload={"error": [], "result": {"closed": {}}},
            repeat=True,
        )
        assert await runner.place(side="buy", price=_PRICE, volume=_VOLUME) == (
            EXIT_BLOCKED
        )

    recon = _step(_evidence_steps(tmp_path), "startup_reconciliation")
    assert len(recon["blocking_rows"]) == 1
    assert recon["unknown_orders"] == []
    assert recon["blocker_count"] == 1  # the row, not the row + its own order
    await adapter.close()
    await db.close()


async def test_unreadable_open_order_listing_blocks_rather_than_reads_as_empty(
    tmp_path, monkeypatch
):
    """FAIL CLOSED: "no open orders" is what licenses a placement, so an
    unreadable listing must never stand in for an empty one."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_preflight(m)
        m.post(
            _OPEN_ORDERS_URL,
            payload={"error": ["EGeneral:Invalid arguments"], "result": {}},
            repeat=True,
        )
        code = await runner.place(side="buy", price=_PRICE, volume=_VOLUME)
        assert code == EXIT_BLOCKED
        assert _posts_to(m, _ADD_ORDER_URL) == []

    recon = _step(_evidence_steps(tmp_path), "startup_reconciliation")
    assert recon["listing_error"]["error_type"] == "KrakenAPIError"
    assert recon["blocker_count"] == 1
    await adapter.close()
    await db.close()


async def test_post_trade_evidence_carries_the_venue_open_order_listing(
    tmp_path, monkeypatch, capsys
):
    """One-order-at-a-time is checkable from the evidence pack alone, without
    trusting the same ledger the run was writing."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_full_happy_path(m)
        assert await runner.place(side="buy", price=_PRICE, volume=_VOLUME) == EXIT_OK

    reconciliation = _step(_evidence_steps(tmp_path), "reconciliation")
    assert reconciliation["venue"]["open_orders_after"] == []
    # C1: the approval block's count is venue-derived, not a ledger fact
    # printed as if it were one.
    out = capsys.readouterr().out
    assert "open kraken orders  : 0 (from the venue's own OpenOrders listing)" in out
    await adapter.close()
    await db.close()


# ======================================================================
# C3 — the window between the check and the write
# ======================================================================
async def test_row_appearing_during_the_approval_prompt_blocks_the_write(
    tmp_path, monkeypatch
):
    """Step 4 runs before an unbounded operator pause. A row that lands while
    the prompt is open must stop the write, not be overwritten by it."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _DECISION[:8])
    runner, db, adapter = await _make_runner(tmp_path)

    real_fetch = kraken_pilot.fetch_blocking_rows
    seen = {"n": 0}

    async def late_arrival(db_arg):
        seen["n"] += 1
        if seen["n"] == 1:
            return []  # step 4: lane clear
        return await real_fetch(db_arg)  # pre-write: whatever is really there

    monkeypatch.setattr(kraken_pilot, "fetch_blocking_rows", late_arrival)
    await _seed_kraken_row(
        db, size_usd="12.0", status="open", client_order_id="raced-cid-0001"
    )

    with aioresponses() as m:
        _mock_preflight(m)
        _mock_market(m)
        _mock_empty_order_lookups(m)
        m.post(_BALANCE_EX_URL, payload=_balance_payload(), repeat=True)
        code = await runner.place(side="buy", price=_PRICE, volume=_VOLUME)
        assert code == EXIT_BLOCKED
        assert _posts_to(m, _ADD_ORDER_URL) == []

    steps = _evidence_steps(tmp_path)
    assert _step(steps, "pre_write_recheck")["outcome"] == "blocked"
    assert _step(steps, "intent_persisted") is None
    assert await _row_by_cid(db, _DECISION) is None
    await adapter.close()
    await db.close()


# ======================================================================
# J1 — one pilot process at a time
# ======================================================================
def test_pilot_lock_is_exclusive(tmp_path):
    db_path = tmp_path / "pilot.db"
    fd, lock_path = kraken_pilot.acquire_pilot_lock(db_path)
    try:
        assert lock_path.exists()
        with pytest.raises(PilotLockHeld) as excinfo:
            kraken_pilot.acquire_pilot_lock(db_path)
        assert str(os.getpid()) in excinfo.value.holder
    finally:
        kraken_pilot.release_pilot_lock(fd, lock_path)
    assert not lock_path.exists()
    # Released cleanly, so the next run takes it.
    fd2, lock_path2 = kraken_pilot.acquire_pilot_lock(db_path)
    kraken_pilot.release_pilot_lock(fd2, lock_path2)


def test_pilot_lock_is_never_broken_automatically(tmp_path):
    """A stale lock means an earlier run did not reach its cleanup — exactly
    the state where an order may be resting unrecorded."""
    db_path = tmp_path / "pilot.db"
    kraken_pilot.pilot_lock_path(db_path).write_text("pid=999999 (long gone)\n")
    with pytest.raises(PilotLockHeld):
        kraken_pilot.acquire_pilot_lock(db_path)


# ======================================================================
# F2 / J1 — main() wiring
# ======================================================================
def _patch_settings(monkeypatch, settings) -> None:
    monkeypatch.setattr(kraken_pilot, "load_settings", lambda: settings)


async def test_main_refuses_when_the_database_does_not_exist(
    tmp_path, monkeypatch, capsys
):
    """sqlite3 creates a DB for any path it is handed, so the wrong working
    directory silently yields an empty one: no kill switch, no rows to
    reconcile, zero daily gross."""
    missing = tmp_path / "nowhere" / "scout.db"
    _patch_settings(monkeypatch, _settings(tmp_path, DB_PATH=missing))
    code = await kraken_pilot.main(["status"])
    assert code == EXIT_REFUSED
    out = capsys.readouterr().out
    assert str(missing.resolve()) in out
    assert not missing.exists()  # and it did NOT create one


async def test_main_refuses_while_another_run_holds_the_lock(
    tmp_path, monkeypatch, capsys
):
    db_path = tmp_path / "pilot.db"
    db = Database(db_path)
    await db.initialize()
    await db.close()
    kraken_pilot.pilot_lock_path(db_path).write_text("pid=4242 acquired_at=now\n")
    _patch_settings(monkeypatch, _settings(tmp_path, DB_PATH=db_path))

    code = await kraken_pilot.main(["status"])
    assert code == EXIT_BLOCKED
    out = capsys.readouterr().out
    assert "4242" in out
    assert "Two runs are two live orders" in out


async def test_main_releases_the_lock_after_a_run(tmp_path, monkeypatch):
    db_path = tmp_path / "pilot.db"
    db = Database(db_path)
    await db.initialize()
    await db.close()
    _patch_settings(monkeypatch, _settings(tmp_path, DB_PATH=db_path))
    reset_nonce_sources_for_tests()

    with aioresponses() as m:
        _mock_preflight(m)
        _mock_empty_order_lookups(m)
        m.post(_BALANCE_EX_URL, payload=_balance_payload(), repeat=True)
        assert await kraken_pilot.main(["status"]) == EXIT_OK

    assert not kraken_pilot.pilot_lock_path(db_path).exists()


# ======================================================================
# F3 / F4 / J4 — operator-facing input and output
# ======================================================================
async def test_numeric_operator_lines_are_ascii(tmp_path, monkeypatch, capsys):
    """Money figures are read on whatever console the operator has; a cp1252
    terminal renders a stray arrow as mojibake between two numbers."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, "nope")  # stop at the approval boundary
    runner, db, adapter = await _make_runner(tmp_path)
    with aioresponses() as m:
        _mock_preflight(m)
        _mock_market(m)
        _mock_empty_order_lookups(m)
        m.post(_BALANCE_EX_URL, payload=_balance_payload(), repeat=True)
        await runner.place(side="buy", price=_PRICE, volume=_VOLUME)

    out = capsys.readouterr().out
    assert "→" not in out  # no rightwards arrow anywhere
    numeric = [
        line
        for line in out.splitlines()
        if any(
            token in line
            for token in ("notional", "daily gross", "available balance", "current mid")
        )
    ]
    assert numeric
    for line in numeric:
        assert line.isascii(), f"non-ascii in a numeric line: {line!r}"
    assert "daily gross now     : 0 -> 15.000 of 100.0" in out
    await adapter.close()
    await db.close()


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-1", "0", "nan", "-Infinity"])
@pytest.mark.parametrize("flag", ["--price", "--volume"])
def test_place_rejects_non_finite_and_non_positive_numbers(flag, bad):
    """NaN and Infinity PARSE as Decimals. NaN then compares False against
    every cap it is tested against, so it must die at the boundary."""
    other = "--volume" if flag == "--price" else "--price"
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["place", "--side", "buy", flag, bad, other, "0.15"])
    assert excinfo.value.code == 2


def test_place_accepts_ordinary_decimals():
    args = build_parser().parse_args(
        ["place", "--side", "buy", "--price", "100.0", "--volume", "0.15"]
    )
    assert args.price == Decimal("100.0")
    assert args.volume == Decimal("0.15")


def test_decision_id_is_stripped_and_shape_checked():
    args = build_parser().parse_args(["cancel", "--decision-id", f"  {_DECISION}  "])
    assert args.decision_id == _DECISION
    for bad in ("not-a-uuid", "", _DECISION[:-1], _DECISION.replace("-", "")):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["cancel", "--decision-id", bad])
