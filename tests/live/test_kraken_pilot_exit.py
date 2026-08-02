"""The operator-invoked supervised Kraken EXIT command.

The load-bearing assertions here are negative, exactly as in the entry lane's
suite: no ``AddOrder`` when a gate refuses, none when the operator does not
type the bound token, never a second one after an ambiguous submission, and no
row closed on evidence the reconciliation could not corroborate. Where a test
asserts a refusal it also asserts the venue was never asked to sell, because a
"refusal" that still sold the position is the failure this command exists to
prevent.

The adapter is mocked with ``spec=KrakenSpotAdapter`` rather than stubbed with
a hand-rolled double: a money API that silently grows a method the tests do not
model is how a real call slips past a suite that looks green.
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from scout.config import Settings
from scout.db import Database
from scout.live import kraken_pilot
from scout.live.adapter_base import OrderConfirmation
from scout.live.kill_switch import KillSwitch
from scout.live.kraken_adapter import (
    KrakenAmbiguousSubmissionError,
    KrakenAPIError,
    KrakenSpotAdapter,
)
from scout.live.kraken_pilot import (
    EXIT_ESCALATE,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_REVIEW,
    PilotRunner,
    exit_authorization_token,
)

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")

# Base64 so the signing primitive could decode it; not a real credential.
_TEST_SECRET = "dGVzdC1rcmFrZW4tc2VjcmV0LWZvci11bml0LXRlc3Rz"

# Fixed so the evidence filename is known here.
_DECISION = "6d1b345e-2821-40e2-ad83-4ecb18a06876"

_ENTRY_TXID = "OENTRY-BW3P3-BUCMWZ"
_EXIT_TXID = "OEXIT1-BW3P3-BUCMWZ"
_PAIR = "XBTUSD"
_SYMBOL = "BTC"

_ENTRY_PRICE = Decimal("100.0")
_HELD_QTY = Decimal("0.15")
_EXIT_PRICE = Decimal("110.0")

_MAKER_PCT = "0.16"
_TAKER_PCT = "0.26"

# Entry: 0.15 @ 100.0 = 15.00 cost, 0.039 fee (0.26% taker).
_ENTRY_FILLS = [
    {
        "trade_id": "TENTRY-1",
        "ordertxid": _ENTRY_TXID,
        "pair": "XXBTZUSD",
        "type": "buy",
        "ordertype": "limit",
        "price": "100.0",
        "cost": "15.00000",
        "fee": "0.03900",
        "vol": "0.15000000",
        "maker": False,
    }
]
# Exit: 0.15 @ 110.0 = 16.50 gross, 0.0429 fee (0.26% taker).
_EXIT_FILLS = [
    {
        "trade_id": "TEXIT-1",
        "ordertxid": _EXIT_TXID,
        "pair": "XXBTZUSD",
        "type": "sell",
        "ordertype": "limit",
        "price": "110.0",
        "cost": "16.50000",
        "fee": "0.04290",
        "vol": "0.15000000",
        "maker": False,
    }
]

# proceeds 16.50 - 0.0429 = 16.4571 ; basis 15.00 + 0.039 = 15.039
_EXPECTED_PNL = Decimal("1.4181")


# ----------------------------------------------------------------------
# Wiring
# ----------------------------------------------------------------------
def _settings(tmp_path, **overrides) -> Settings:
    base = dict(
        KRAKEN_API_KEY="planted-kraken-key-DO-NOT-LEAK",
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


def _balance_sequence(*values: str):
    """``fetch_account_balance`` side effect: walk ``values``, then hold."""
    remaining = [float(v) for v in values]

    def _fetch(*, asset: str) -> float:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return _fetch


def _fills_by_txid(mapping: dict[str, list[dict]]):
    def _fetch(*, txid: str) -> list[dict]:
        return list(mapping.get(txid, []))

    return _fetch


def _confirmation(status: str, *, filled_qty=0.15, fill_price=110.0):
    return OrderConfirmation(
        venue="kraken",
        venue_order_id=_EXIT_TXID,
        client_order_id="gecko-x-1",
        status=status,
        filled_qty=filled_qty,
        fill_price=fill_price,
        raw_response={"status": "closed"},
    )


def _adapter(**overrides) -> MagicMock:
    """A happy-path adapter double. Tests override one method at a time."""
    adapter = MagicMock(spec=KrakenSpotAdapter)
    adapter.fetch_account_balance.side_effect = _balance_sequence("1.0", "0.85")
    adapter.fetch_open_orders.return_value = []
    adapter.fetch_fee_tier.return_value = {
        "pair": _PAIR,
        "maker_pct": _MAKER_PCT,
        "taker_pct": _TAKER_PCT,
        "volume": "12345.0",
        "currency": "ZUSD",
        "raw": {},
    }
    adapter.fetch_order_fills.side_effect = _fills_by_txid(
        {_ENTRY_TXID: _ENTRY_FILLS, _EXIT_TXID: _EXIT_FILLS}
    )
    adapter.fetch_price.return_value = Decimal("109.0")
    adapter.place_limit_order.return_value = {
        "txid": [_EXIT_TXID],
        "descr": {"order": "sell 0.15000000 XBTUSD @ limit 110.0"},
        "price": "110.0",
        "volume": "0.15000000",
    }
    adapter.await_fill_confirmation.return_value = _confirmation("filled")
    for name, value in overrides.items():
        getattr(adapter, name).return_value = value
    return adapter


async def _make_runner(tmp_path, adapter=None, **overrides):
    """Runner + db + adapter against a tmp DB. Caller closes the db."""
    settings = _settings(tmp_path, **overrides)
    db = Database(tmp_path / "pilot.db")
    await db.initialize()
    adapter = adapter or _adapter()
    runner = PilotRunner(
        settings=settings, db=db, adapter=adapter, kill_switch=KillSwitch(db)
    )
    return runner, db, adapter


def _fix_decision_id(monkeypatch) -> None:
    monkeypatch.setattr(kraken_pilot, "uuid4", lambda: UUID(_DECISION))


def _authorize(monkeypatch, typed: str | None) -> None:
    """Feed the approval prompt. ``None`` raises EOF (non-interactive stdin)."""

    def _fake_input(_prompt: str = "") -> str:
        if typed is None:
            raise EOFError
        return typed

    monkeypatch.setattr("builtins.input", _fake_input)


def _token(price: Decimal = _EXIT_PRICE, volume: Decimal = _HELD_QTY, row_id: int = 1):
    return exit_authorization_token(
        pair=_PAIR,
        side="sell",
        volume=volume,
        price=price,
        client_order_id=f"gecko-x-{row_id}",
    )


def _evidence_path(tmp_path, row_id: int = 1):
    return tmp_path / "evidence" / f"kraken_pilot_exit_{row_id}_{_DECISION}.json"


def _steps(tmp_path, row_id: int = 1) -> list[dict]:
    text = _evidence_path(tmp_path, row_id).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _step(steps: list[dict], name: str) -> dict | None:
    return next((s for s in steps if s["step"] == name), None)


# ----------------------------------------------------------------------
# Ledger seeding
# ----------------------------------------------------------------------
async def _seed_row(
    db: Database,
    *,
    status: str = "open",
    venue: str = "kraken",
    pair: str = _PAIR,
    symbol: str = _SYMBOL,
    size_usd: str = "15.0",
    entry_order_id: str | None = _ENTRY_TXID,
    entry_fill_price: str | None = "100.0",
    entry_fill_qty: str | None = "0.15",
    client_order_id: str | None = None,
) -> int:
    from datetime import datetime, timezone

    anchor = await kraken_pilot.ensure_pilot_anchor(db)
    now_iso = datetime.now(timezone.utc).isoformat()
    async with db._txn_lock:
        cur = await db._conn.execute(
            """INSERT INTO live_trades
               (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
                size_usd, status, client_order_id, entry_order_id,
                entry_fill_price, entry_fill_qty, created_at)
               VALUES (?, 'kraken-pilot-btc', ?, ?, ?, 'kraken_pilot',
                       ?, ?, ?, ?, ?, ?, ?)""",
            (
                anchor,
                symbol,
                venue,
                pair,
                size_usd,
                status,
                client_order_id,
                entry_order_id,
                entry_fill_price,
                entry_fill_qty,
                now_iso,
            ),
        )
        await db._conn.commit()
        return int(cur.lastrowid)


async def _row(db: Database, row_id: int) -> dict:
    fetched = await kraken_pilot.fetch_live_trade_row(db, row_id)
    assert fetched is not None
    return fetched


# ======================================================================
# Step 1 — row selection and eligibility
# ======================================================================
async def test_exit_refuses_when_the_row_does_not_exist(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        code = await runner.exit_position(live_trade_id=1, price=_EXIT_PRICE)
        assert code == EXIT_REFUSED
        adapter.place_limit_order.assert_not_called()
        abort = _step(_steps(tmp_path), "aborted")
        assert abort["stage"] == "row_selection"
        assert "no live_trades row" in abort["reason"]
    finally:
        await db.close()


async def test_exit_refuses_when_the_row_is_not_open(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        row_id = await _seed_row(db, status="closed_tp")
        code = await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
        assert code == EXIT_REFUSED
        adapter.place_limit_order.assert_not_called()
        assert "'closed_tp'" in _step(_steps(tmp_path, row_id), "aborted")["reason"]
    finally:
        await db.close()


async def test_exit_refuses_a_needs_manual_review_row(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        row_id = await _seed_row(db, status="needs_manual_review")
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REFUSED
        )
        adapter.place_limit_order.assert_not_called()
    finally:
        await db.close()


async def test_exit_refuses_a_non_kraken_row(tmp_path, monkeypatch):
    """A binance row must not be sold through the Kraken lane, and the operator
    must be told WHY rather than being told the row does not exist."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        row_id = await _seed_row(db, venue="binance", pair="BTCUSDT")
        code = await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
        assert code == EXIT_REFUSED
        adapter.place_limit_order.assert_not_called()
        reason = _step(_steps(tmp_path, row_id), "aborted")["reason"]
        assert "binance" in reason and "only closes kraken" in reason
    finally:
        await db.close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"entry_fill_price": None},
        {"entry_fill_qty": None},
        {"entry_fill_price": None, "entry_fill_qty": None},
    ],
    ids=["no-price", "no-qty", "neither"],
)
async def test_exit_refuses_when_the_entry_fill_is_not_recorded(
    tmp_path, monkeypatch, overrides
):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        row_id = await _seed_row(db, **overrides)
        code = await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
        assert code == EXIT_REFUSED
        adapter.place_limit_order.assert_not_called()
        assert (
            "no recorded entry fill"
            in _step(_steps(tmp_path, row_id), "aborted")["reason"]
        )
    finally:
        await db.close()


async def test_exit_refuses_when_another_kraken_row_is_in_flight(tmp_path, monkeypatch):
    """This row being open is the precondition. A SECOND non-terminal row means
    the one-order-at-a-time invariant is already broken."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        row_id = await _seed_row(db)
        await _seed_row(db, status="needs_manual_review", client_order_id="other-cid")
        code = await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
        assert code == EXIT_REFUSED
        adapter.place_limit_order.assert_not_called()
        assert _step(_steps(tmp_path, row_id), "aborted")["stage"] == "lane_state"
    finally:
        await db.close()


# ======================================================================
# Step 2 — venue state
# ======================================================================
async def test_exit_refuses_when_the_venue_balance_is_short(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.fetch_account_balance.side_effect = _balance_sequence("0.05")
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        code = await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
        assert code == EXIT_REFUSED
        adapter.place_limit_order.assert_not_called()
        abort = _step(_steps(tmp_path, row_id), "aborted")
        assert abort["stage"] == "venue_balance"
        assert "does not cover" in abort["reason"]
    finally:
        await db.close()


async def test_exit_refuses_when_an_order_already_rests_for_the_pair(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.fetch_open_orders.return_value = [
        {
            "txid": "ORESTING-1",
            "client_order_id": None,
            "pair": _PAIR,
            "status": "open",
            "vol": "0.15",
            "vol_exec": "0",
            "price": "120.0",
        }
    ]
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        code = await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
        assert code == EXIT_REFUSED
        adapter.place_limit_order.assert_not_called()
        abort = _step(_steps(tmp_path, row_id), "aborted")
        assert abort["stage"] == "venue_open_orders"
        assert "never place a second one" in abort["reason"]
    finally:
        await db.close()


async def test_exit_refuses_when_the_exit_client_id_is_already_resting(
    tmp_path, monkeypatch
):
    """The deterministic exit id IS the crash-recovery mechanism: a rerun after
    an interrupted exit finds its own order and refuses instead of selling the
    same coins twice."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.fetch_open_orders.return_value = [
        {
            "txid": _EXIT_TXID,
            "client_order_id": "gecko-x-1",
            # A different pair string, so only the client id can match.
            "pair": "ETHUSD",
            "status": "open",
            "vol": "0.15",
            "vol_exec": "0",
            "price": "110.0",
        }
    ]
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        assert row_id == 1  # the cid in the fixture above assumes it
        code = await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
        assert code == EXIT_REFUSED
        adapter.place_limit_order.assert_not_called()
        assert "gecko-x-1" in _step(_steps(tmp_path, row_id), "aborted")["reason"]
    finally:
        await db.close()


async def test_exit_refuses_when_open_orders_cannot_be_read(tmp_path, monkeypatch):
    """Fails CLOSED — 'no resting order' is the fact that licenses selling."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.fetch_open_orders.side_effect = KrakenAPIError("unreadable payload")
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        code = await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
        assert code == EXIT_REFUSED
        adapter.place_limit_order.assert_not_called()
        abort = _step(_steps(tmp_path, row_id), "aborted")
        assert abort["stage"] == "venue_open_orders"
        assert "unreadable listing" in abort["reason"]
    finally:
        await db.close()


# ======================================================================
# Step 3 — fee tier
# ======================================================================
async def test_exit_refuses_when_the_fee_tier_cannot_be_read(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.fetch_fee_tier.side_effect = KrakenAPIError("TradeVolume exploded")
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        code = await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
        assert code == EXIT_REFUSED
        adapter.place_limit_order.assert_not_called()
        abort = _step(_steps(tmp_path, row_id), "aborted")
        assert abort["stage"] == "fee_tier"
        assert "rather than guessing" in abort["reason"]
    finally:
        await db.close()


async def test_exit_refuses_when_trade_volume_returns_unusable_rates(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.fetch_fee_tier.return_value = {
        "pair": _PAIR,
        "maker_pct": None,
        "taker_pct": None,
    }
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REFUSED
        )
        adapter.place_limit_order.assert_not_called()
        assert _step(_steps(tmp_path, row_id), "aborted")["stage"] == "fee_tier"
    finally:
        await db.close()


# ======================================================================
# Step 4 — the cost screen shows BOTH assumptions
# ======================================================================
async def test_both_fee_scenarios_are_recorded_with_the_real_entry_fee(
    tmp_path, monkeypatch, capsys
):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, None)  # abort at the prompt; the screen is the subject
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        row_id = await _seed_row(db)
        await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
        scenarios = _step(_steps(tmp_path, row_id), "cost_scenarios")
        assert scenarios["entry_fee_source"] == "actual_entry_fills"
        assert scenarios["entry_fee"] == "0.03900"
        assert scenarios["maker"]["fee_pct"] == _MAKER_PCT
        assert scenarios["taker"]["fee_pct"] == _TAKER_PCT
        # Maker is the cheaper exit, so it breaks even lower and nets more.
        assert Decimal(scenarios["maker"]["exit_fee"]) < Decimal(
            scenarios["taker"]["exit_fee"]
        )
        assert Decimal(scenarios["maker"]["break_even_price"]) < Decimal(
            scenarios["taker"]["break_even_price"]
        )
        assert Decimal(scenarios["taker"]["realized_pnl_usd"]) == _EXPECTED_PNL
        out = capsys.readouterr().out
        assert "IF THIS FILLS AS MAKER" in out
        assert "IF THIS FILLS AS TAKER" in out
        assert "not a MAKER guarantee" in out
        assert "round-trip fee" in out
    finally:
        await db.close()


async def test_entry_fee_falls_back_to_a_labelled_estimate(tmp_path, monkeypatch):
    """An unreadable entry fee is derived at the TAKER rate — the higher one —
    so P&L is understated rather than overstated, and the screen says so."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, None)
    adapter = _adapter()
    adapter.fetch_order_fills.side_effect = _fills_by_txid({_EXIT_TXID: _EXIT_FILLS})
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
        scenarios = _step(_steps(tmp_path, row_id), "cost_scenarios")
        assert scenarios["entry_fee_source"] == "estimated_at_taker_rate"
        # 15.00 * 0.26% = 0.039 — same number, different provenance.
        assert Decimal(scenarios["entry_fee"]) == Decimal("0.039")
    finally:
        await db.close()


# ======================================================================
# Step 5 — the typed authorization is bound to the ORDER
# ======================================================================
async def test_exit_refuses_a_wrong_token(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, "DEADBEEF")
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        row_id = await _seed_row(db)
        code = await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
        assert code == EXIT_REFUSED
        adapter.place_limit_order.assert_not_called()
        steps = _steps(tmp_path, row_id)
        assert _step(steps, "authorization")["detail"] == "mismatch"
        assert _step(steps, "exit_intent_persisted") is None
    finally:
        await db.close()


async def test_exit_refuses_when_stdin_is_closed(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, None)
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REFUSED
        )
        adapter.place_limit_order.assert_not_called()
        assert _step(_steps(tmp_path, row_id), "authorization")["detail"] == "no_input"
    finally:
        await db.close()


async def test_a_token_for_one_price_does_not_authorize_another(tmp_path, monkeypatch):
    """The core of the binding. An operator who read and approved a 110.0 sell
    must not have that approval carried onto a 105.0 one."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token(price=Decimal("110.0")))
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        row_id = await _seed_row(db)
        code = await runner.exit_position(live_trade_id=row_id, price=Decimal("105.0"))
        assert code == EXIT_REFUSED
        adapter.place_limit_order.assert_not_called()
        assert _step(_steps(tmp_path, row_id), "authorization")["detail"] == "mismatch"
    finally:
        await db.close()


def test_the_token_changes_with_every_bound_field():
    base = dict(
        pair=_PAIR,
        side="sell",
        volume=_HELD_QTY,
        price=_EXIT_PRICE,
        client_order_id="gecko-x-1",
    )
    baseline = exit_authorization_token(**base)
    for field, value in (
        ("pair", "ETHUSD"),
        ("side", "buy"),
        ("volume", Decimal("0.16")),
        ("price", Decimal("110.1")),
        ("client_order_id", "gecko-x-2"),
    ):
        assert exit_authorization_token(**{**base, field: value}) != baseline, field
    # Same economic order, differently-spelled Decimals — one approval.
    assert exit_authorization_token(**{**base, "volume": Decimal("0.1500")}) == baseline


# ======================================================================
# Steps 6-8 — durable intent, exactly one submission, ambiguity resolution
# ======================================================================
async def test_the_intent_is_persisted_before_the_order_is_submitted(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    seen: dict[str, list[str]] = {}

    def _place(**kwargs):
        seen["steps_at_submit"] = [s["step"] for s in _steps(tmp_path, 1)]
        seen["kwargs"] = kwargs
        return {
            "txid": [_EXIT_TXID],
            "descr": {},
            "price": "110.0",
            "volume": "0.15000000",
        }

    adapter.place_limit_order.side_effect = _place
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
        assert "exit_intent_persisted" in seen["steps_at_submit"]
        assert "submitted" not in seen["steps_at_submit"]
        intent = _step(_steps(tmp_path, row_id), "exit_intent_persisted")
        assert intent["live_trade_id"] == row_id
        assert intent["pair"] == _PAIR
        assert intent["side"] == "sell"
        assert intent["volume"] == "0.15"
        assert intent["price"] == "110.0"
        assert intent["client_order_id"] == "gecko-x-1"
    finally:
        await db.close()


async def test_exit_submits_exactly_one_sell_order(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_OK
        )
        assert adapter.place_limit_order.call_count == 1
        kwargs = adapter.place_limit_order.call_args.kwargs
        assert kwargs["side"] == "sell"
        assert kwargs["pair"] == _PAIR
        assert kwargs["volume"] == _HELD_QTY
        assert kwargs["price"] == _EXIT_PRICE
        assert kwargs["client_order_id"] == "gecko-x-1"
        assert kwargs["validate_only"] is False
    finally:
        await db.close()


async def test_ambiguous_submission_adopts_a_landed_order_and_never_resends(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.place_limit_order.side_effect = KrakenAmbiguousSubmissionError(
        "timeout", client_order_id="gecko-x-1"
    )
    adapter.resolve_order_submission_detail.return_value = {
        "verdict": "accepted",
        "client_order_id": "gecko-x-1",
        "txid": [_EXIT_TXID],
        "probes": [],
    }
    adapter.fetch_order_by_client_id.return_value = _confirmation("filled")
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        code = await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
        assert code == EXIT_OK
        assert adapter.place_limit_order.call_count == 1
        adapter.resolve_order_submission_detail.assert_awaited_once()
        steps = _steps(tmp_path, row_id)
        assert _step(steps, "ambiguity_adopted")["txid"] == _EXIT_TXID
        assert (await _row(db, row_id))["status"] == "closed_via_reconciliation"
    finally:
        await db.close()


async def test_ambiguous_submission_that_did_not_land_leaves_the_row_untouched(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.place_limit_order.side_effect = KrakenAmbiguousSubmissionError(
        "socket died", client_order_id="gecko-x-1"
    )
    adapter.resolve_order_submission_detail.return_value = {
        "verdict": "not_accepted",
        "client_order_id": "gecko-x-1",
        "txid": [],
        "probes": [],
    }
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        code = await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
        assert code == EXIT_REFUSED
        assert adapter.place_limit_order.call_count == 1
        row = await _row(db, row_id)
        assert row["status"] == "open"
        assert row["exit_order_id"] is None
        assert row["entry_fill_qty"] == "0.15"
    finally:
        await db.close()


async def test_unresolved_ambiguity_escalates_and_never_resends(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.place_limit_order.side_effect = KrakenAmbiguousSubmissionError(
        "502", client_order_id="gecko-x-1"
    )
    adapter.resolve_order_submission_detail.return_value = {
        "verdict": "unresolved",
        "client_order_id": "gecko-x-1",
        "txid": [],
        "probes": [],
    }
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        code = await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
        assert code == EXIT_ESCALATE
        assert adapter.place_limit_order.call_count == 1
        assert (await _row(db, row_id))["status"] == "needs_manual_review"
    finally:
        await db.close()


async def test_a_definitive_venue_refusal_leaves_the_position_held(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.place_limit_order.side_effect = KrakenAPIError("EOrder:Invalid price")
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        code = await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
        assert code == EXIT_REFUSED
        row = await _row(db, row_id)
        assert row["status"] == "open"
        assert row["exit_order_id"] is None
        adapter.resolve_order_submission_detail.assert_not_called()
    finally:
        await db.close()


# ======================================================================
# Steps 9-10 — partial, close, review
# ======================================================================
async def test_a_partial_fill_leaves_the_row_open_with_the_remainder(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.await_fill_confirmation.return_value = _confirmation(
        "partial", filled_qty=0.05, fill_price=110.0
    )
    adapter.fetch_order_fills.side_effect = _fills_by_txid(
        {
            _ENTRY_TXID: _ENTRY_FILLS,
            _EXIT_TXID: [{**_EXIT_FILLS[0], "vol": "0.05", "cost": "5.5"}],
        }
    )
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        code = await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
        assert code == EXIT_REVIEW
        row = await _row(db, row_id)
        assert row["status"] == "open"
        assert Decimal(row["entry_fill_qty"]) == Decimal("0.10")
        assert row["exit_order_id"] == _EXIT_TXID
        assert Decimal(row["exit_fill_price"]) == Decimal("110.0")
        # An unfinished exit has no realised P&L and is not closed.
        assert row["realized_pnl_usd"] is None
        assert row["realized_pnl_pct"] is None
        assert row["closed_at"] is None
        partial = _step(_steps(tmp_path, row_id), "exit_partial_fill")
        assert partial["remaining_qty"] == "0.10"
        assert partial["ledger_status"] == "open"
    finally:
        await db.close()


async def test_a_clean_fill_reconciles_and_closes_the_row(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        row_id = await _seed_row(db)
        code = await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
        assert code == EXIT_OK
        row = await _row(db, row_id)
        assert row["status"] == "closed_via_reconciliation"
        assert row["exit_order_id"] == _EXIT_TXID
        assert Decimal(row["exit_fill_price"]) == Decimal("110.0")

        cur = await db._conn.execute(
            "SELECT realized_pnl_usd, realized_pnl_pct, closed_at "
            "FROM live_trades WHERE id = ?",
            (row_id,),
        )
        pnl_usd, pnl_pct, closed_at = await cur.fetchone()
        # 16.50 gross - 0.0429 exit fee - (15.00 + 0.039 entry) = 1.4181
        assert Decimal(pnl_usd) == _EXPECTED_PNL
        assert Decimal(pnl_pct).quantize(Decimal("0.0001")) == Decimal("9.4295")
        assert closed_at is not None

        recon = _step(_steps(tmp_path, row_id), "exit_reconciliation")
        assert recon["verdict"] == "pass"
        assert recon["mismatches"] == []
        assert recon["entry_fee_source"] == "actual_entry_fills"
    finally:
        await db.close()


async def test_a_balance_that_did_not_move_does_not_close_the_row(
    tmp_path, monkeypatch
):
    """The venue says it sold; the account says otherwise. A close asserts the
    position is gone, and that assertion is not available here."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.fetch_account_balance.side_effect = _balance_sequence("1.0", "1.0")
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        code = await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
        assert code == EXIT_REVIEW
        row = await _row(db, row_id)
        assert row["status"] == "needs_manual_review"
        # The txid and fill price ARE recorded — the sale is not denied, only
        # unconfirmed — but nothing terminal is written.
        assert row["exit_order_id"] == _EXIT_TXID
        cur = await db._conn.execute(
            "SELECT realized_pnl_usd, closed_at FROM live_trades WHERE id = ?",
            (row_id,),
        )
        pnl_usd, closed_at = await cur.fetchone()
        assert pnl_usd is None
        assert closed_at is None
        recon = _step(_steps(tmp_path, row_id), "exit_reconciliation")
        assert recon["verdict"] == "review"
        assert any("balance delta" in m for m in recon["mismatches"])
    finally:
        await db.close()


async def test_a_fill_with_no_per_fill_records_does_not_close_the_row(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.fetch_order_fills.side_effect = _fills_by_txid({_ENTRY_TXID: _ENTRY_FILLS})
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        code = await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
        assert code == EXIT_REVIEW
        assert (await _row(db, row_id))["status"] == "needs_manual_review"
        recon = _step(_steps(tmp_path, row_id), "exit_reconciliation")
        assert any("no per-fill records" in m for m in recon["mismatches"])
    finally:
        await db.close()


async def test_a_resting_exit_order_leaves_the_row_open(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.await_fill_confirmation.return_value = OrderConfirmation(
        venue="kraken",
        venue_order_id=_EXIT_TXID,
        client_order_id="gecko-x-1",
        status="timeout",
        filled_qty=None,
        fill_price=None,
        raw_response=None,
    )
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        code = await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
        assert code == EXIT_OK
        row = await _row(db, row_id)
        assert row["status"] == "open"
        assert row["exit_order_id"] == _EXIT_TXID
        assert Decimal(row["entry_fill_qty"]) == _HELD_QTY
        assert _step(_steps(tmp_path, row_id), "exit_order_resting") is not None
    finally:
        await db.close()


async def test_a_terminal_zero_fill_leaves_the_position_held(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.await_fill_confirmation.return_value = _confirmation(
        "rejected", filled_qty=0.0, fill_price=None
    )
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        code = await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
        assert code == EXIT_REFUSED
        row = await _row(db, row_id)
        assert row["status"] == "open"
        assert Decimal(row["entry_fill_qty"]) == _HELD_QTY
    finally:
        await db.close()


# ======================================================================
# Rehearsal
# ======================================================================
async def test_validate_only_places_nothing_and_leaves_the_row_open(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.place_limit_order.return_value = {
        "txid": [],
        "descr": {"order": "sell 0.15000000 XBTUSD @ limit 110.0"},
        "price": "110.0",
        "volume": "0.15000000",
    }
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        code = await runner.exit_position(
            live_trade_id=row_id, price=_EXIT_PRICE, validate_only=True
        )
        assert code == EXIT_OK
        assert adapter.place_limit_order.call_args.kwargs["validate_only"] is True
        adapter.await_fill_confirmation.assert_not_called()
        row = await _row(db, row_id)
        assert row["status"] == "open"
        assert row["exit_order_id"] is None
    finally:
        await db.close()


async def test_validate_only_is_allowed_under_the_emergency_revert_posture(
    tmp_path, monkeypatch
):
    """A rehearsal cannot trade, so the flag that blocks real orders must not
    block it — otherwise the rehearsal exercises a different code path."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.place_limit_order.return_value = {"txid": [], "descr": {"order": "x"}}
    runner, db, adapter = await _make_runner(
        tmp_path, adapter, LIVE_USE_REAL_SIGNED_REQUESTS=False
    )
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(
                live_trade_id=row_id, price=_EXIT_PRICE, validate_only=True
            )
            == EXIT_OK
        )
    finally:
        await db.close()


async def test_a_real_exit_is_refused_under_the_emergency_revert_posture(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    runner, db, adapter = await _make_runner(
        tmp_path, LIVE_USE_REAL_SIGNED_REQUESTS=False
    )
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REFUSED
        )
        adapter.place_limit_order.assert_not_called()
        assert _step(_steps(tmp_path, row_id), "aborted")["stage"] == "envelope_gate"
    finally:
        await db.close()


async def test_exit_is_not_gated_on_the_master_pilot_switch(tmp_path, monkeypatch):
    """KRAKEN_PILOT_ENABLED stops NEW exposure. It must never be able to strand
    an open position — the same reasoning that leaves `cancel` ungated."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    runner, db, adapter = await _make_runner(tmp_path, KRAKEN_PILOT_ENABLED=False)
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_OK
        )
        assert adapter.place_limit_order.call_count == 1
    finally:
        await db.close()


# ======================================================================
# CLI flag pairing
# ======================================================================
async def test_validate_only_without_the_rehearsal_flag_is_refused(capsys):
    code = await kraken_pilot.main(
        ["exit", "--live-trade-id", "1", "--price", "110.0", "--validate-only"]
    )
    assert code == EXIT_REFUSED
    assert "--yes-i-am-rehearsing" in capsys.readouterr().out


async def test_the_rehearsal_flag_without_validate_only_is_refused(capsys):
    code = await kraken_pilot.main(
        ["exit", "--live-trade-id", "1", "--price", "110.0", "--yes-i-am-rehearsing"]
    )
    assert code == EXIT_REFUSED
    assert "REAL order" in capsys.readouterr().out


def test_the_exit_subcommand_requires_a_row_id_and_a_price():
    parser = kraken_pilot.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["exit", "--price", "110.0"])
    with pytest.raises(SystemExit):
        parser.parse_args(["exit", "--live-trade-id", "1"])
    with pytest.raises(SystemExit):
        parser.parse_args(["exit", "--live-trade-id", "0", "--price", "110.0"])
    with pytest.raises(SystemExit):
        parser.parse_args(["exit", "--live-trade-id", "1", "--price", "0"])
    args = parser.parse_args(["exit", "--live-trade-id", "7", "--price", "110.0"])
    assert args.live_trade_id == 7
    assert args.price == Decimal("110.0")
