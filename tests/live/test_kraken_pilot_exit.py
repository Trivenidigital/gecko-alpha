"""The operator-invoked supervised Kraken EXIT command and its recovery paths.

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
from pathlib import Path
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
    EXIT_BLOCKED,
    EXIT_ESCALATE,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_REVIEW,
    PilotRunner,
    build_exit_snapshot,
    exit_authorization_token,
    open_order_fingerprint,
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
_QUOTE = "USD"
_CID = "gecko-x-1"

_ENTRY_PRICE = Decimal("100.0")
_HELD_QTY = Decimal("0.15")
_EXIT_PRICE = Decimal("110.0")

# This account's real tier, from the live TradeVolume read on 2026-08-02.
_MAKER_PCT = "0.4000"
_TAKER_PCT = "0.8000"
_LOT_DECIMALS = 8

# Real XXBTZUSD row, trimmed to what the exit path reads.
_XBTUSD_ROW = {
    "altname": "XBTUSD",
    "base": "XXBT",
    "quote": "ZUSD",
    "pair_decimals": 1,
    "lot_decimals": _LOT_DECIMALS,
    "ordermin": "0.00005",
    "costmin": "0.5",
    "tick_size": "0.1",
    "status": "online",
}

# Entry: 0.15 @ 100.0 = 15.00 cost, 0.12 fee (0.8% taker).
_ENTRY_FILLS = [
    {
        "trade_id": "TENTRY-1",
        "ordertxid": _ENTRY_TXID,
        "pair": "XXBTZUSD",
        "type": "buy",
        "ordertype": "limit",
        "price": "100.0",
        "cost": "15.00000",
        "fee": "0.12000",
        "vol": "0.15000000",
        "maker": False,
    }
]
# Exit: 0.15 @ 110.0 = 16.50 gross, 0.132 fee (0.8% taker).
_EXIT_FILLS = [
    {
        "trade_id": "TEXIT-1",
        "ordertxid": _EXIT_TXID,
        "pair": "XXBTZUSD",
        "type": "sell",
        "ordertype": "limit",
        "price": "110.0",
        "cost": "16.50000",
        "fee": "0.13200",
        "vol": "0.15000000",
        "maker": False,
    }
]

# proceeds 16.50 - 0.132 = 16.368 ; basis 15.00 + 0.12 = 15.12
_EXPECTED_PNL = Decimal("1.248")


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


class _Balances:
    """Per-asset ``fetch_account_balance`` side effect.

    Keyed by asset because the exit path now samples BOTH sides — the base to
    prove the coins left and the quote to prove the money arrived — so a
    single call-ordered sequence would silently hand a BTC figure to a USD
    question. The last value in a sequence repeats.
    """

    def __init__(
        self,
        base: tuple[str, ...] = ("1.0", "1.0", "0.85"),
        quote: tuple[str, ...] = ("500.0", "516.368"),
    ) -> None:
        self.seqs = {_SYMBOL: list(base), _QUOTE: list(quote)}

    def __call__(self, *, asset: str) -> float:
        seq = self.seqs.get(asset.upper())
        if not seq:
            return 0.0
        value = seq.pop(0) if len(seq) > 1 else seq[0]
        return float(Decimal(value))

    def set(self, asset: str, value: str) -> None:
        self.seqs[asset.upper()] = [value]


def _fills_by_txid(mapping: dict[str, list[dict]]):
    def _fetch(*, txid: str) -> list[dict]:
        return list(mapping.get(txid, []))

    return _fetch


def _confirmation(status: str, *, filled_qty=0.15, fill_price=110.0, limit="110.0"):
    return OrderConfirmation(
        venue="kraken",
        venue_order_id=_EXIT_TXID,
        client_order_id=_CID,
        status=status,
        filled_qty=filled_qty,
        fill_price=fill_price,
        raw_response={"status": "closed", "descr": {"price": limit, "pair": _PAIR}},
    )


def _fee_tier(**overrides) -> dict:
    tier = {
        "pair": _PAIR,
        "maker_pct": _MAKER_PCT,
        "taker_pct": _TAKER_PCT,
        "volume": "2039.93616",
        "currency": "ZUSD",
        "next_maker_pct": "0.3000",
        "next_taker_pct": "0.6000",
        "next_volume": "2500.00000",
        "raw": {},
    }
    tier.update(overrides)
    return tier


def _adapter() -> MagicMock:
    """A happy-path adapter double. Tests override one method at a time."""
    adapter = MagicMock(spec=KrakenSpotAdapter)
    adapter.balances = _Balances()
    adapter.fetch_account_balance.side_effect = adapter.balances
    adapter.fetch_open_orders.return_value = []
    adapter.fetch_exchange_info_row.return_value = dict(_XBTUSD_ROW)
    adapter.fetch_fee_tier.return_value = _fee_tier()
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
    adapter.fetch_order_by_client_id.return_value = None
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


class _Tty:
    """A stdin stand-in that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


def _authorize(monkeypatch, typed: str | None, *, tty: bool = True) -> None:
    """Feed the approval prompt. ``None`` raises EOF (a closed stdin)."""
    if tty:
        monkeypatch.setattr(kraken_pilot.sys, "stdin", _Tty())

    def _fake_input(_prompt: str = "") -> str:
        if typed is None:
            raise EOFError
        return typed

    monkeypatch.setattr("builtins.input", _fake_input)


def _snapshot(
    *,
    price: Decimal = _EXIT_PRICE,
    volume: Decimal = _HELD_QTY,
    row_id: int = 1,
    balance: Decimal = Decimal("1.0"),
    open_orders: list[dict] | None = None,
    maker_ceiling: str = _MAKER_PCT,
    taker_ceiling: str = _TAKER_PCT,
) -> dict:
    return build_exit_snapshot(
        live_trade_id=row_id,
        pair=_PAIR,
        side="sell",
        volume=volume,
        price=price,
        client_order_id=f"gecko-x-{row_id}",
        row_status="open",
        row_venue="kraken",
        position_qty=volume,
        base_balance=balance,
        open_orders=open_orders or [],
        maker_ceiling_pct=Decimal(maker_ceiling),
        taker_ceiling_pct=Decimal(taker_ceiling),
    )


def _token(**kwargs) -> str:
    return exit_authorization_token(_snapshot(**kwargs))


def _evidence_dir(tmp_path) -> Path:
    return tmp_path / "evidence"


def _evidence_path(tmp_path, row_id: int = 1, command: str | None = None) -> Path:
    name = (
        f"kraken_pilot_exit_{row_id}_{_DECISION}.json"
        if command is None
        else f"kraken_pilot_exit_{row_id}_{command}_{_DECISION}.json"
    )
    return _evidence_dir(tmp_path) / name


def _steps(tmp_path, row_id: int = 1, command: str | None = None) -> list[dict]:
    text = _evidence_path(tmp_path, row_id, command).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _step(steps: list[dict], name: str) -> dict | None:
    return next((s for s in steps if s["step"] == name), None)


def _write_dangling_intent(tmp_path, row_id: int = 1, decision_id: str = "dead-run"):
    """An evidence file from a run that died between authorizing and finishing."""
    directory = _evidence_dir(tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"kraken_pilot_exit_{row_id}_{decision_id}.json"
    path.write_text(
        json.dumps(
            {
                "step": "exit_intent_persisted",
                "at": "2026-08-02T00:00:00+00:00",
                "decision_id": decision_id,
                "live_trade_id": row_id,
                "pair": _PAIR,
                "side": "sell",
                "volume": "0.15",
                "price": "110.0",
                "client_order_id": f"gecko-x-{row_id}",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


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
# Row selection and eligibility
# ======================================================================
async def test_exit_refuses_when_the_row_does_not_exist(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        assert (
            await runner.exit_position(live_trade_id=1, price=_EXIT_PRICE)
            == EXIT_REFUSED
        )
        adapter.place_limit_order.assert_not_called()
        abort = _step(_steps(tmp_path), "aborted")
        assert abort["stage"] == "row_selection"
        assert "no live_trades row" in abort["reason"]
    finally:
        await db.close()


@pytest.mark.parametrize("status", ["closed_tp", "needs_manual_review", "rejected"])
async def test_exit_refuses_a_row_that_is_not_open(tmp_path, monkeypatch, status):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        row_id = await _seed_row(db, status=status)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REFUSED
        )
        adapter.place_limit_order.assert_not_called()
        assert f"'{status}'" in _step(_steps(tmp_path, row_id), "aborted")["reason"]
    finally:
        await db.close()


@pytest.mark.parametrize(
    ("venue", "pair"), [("binance", "BTCUSDT"), ("solana", "SOL/USDC")]
)
async def test_exit_refuses_a_row_from_another_venue(
    tmp_path, monkeypatch, venue, pair
):
    """A Solana or Binance position is closed through its own lane. The refusal
    must name the venue, not report the row as missing."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        row_id = await _seed_row(db, venue=venue, pair=pair)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REFUSED
        )
        adapter.place_limit_order.assert_not_called()
        reason = _step(_steps(tmp_path, row_id), "aborted")["reason"]
        assert venue in reason and "only closes kraken" in reason
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
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REFUSED
        )
        adapter.place_limit_order.assert_not_called()
        assert (
            "no recorded entry fill"
            in _step(_steps(tmp_path, row_id), "aborted")["reason"]
        )
    finally:
        await db.close()


async def test_exit_refuses_when_another_kraken_row_is_in_flight(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        row_id = await _seed_row(db)
        await _seed_row(db, status="needs_manual_review", client_order_id="other-cid")
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REFUSED
        )
        adapter.place_limit_order.assert_not_called()
        assert _step(_steps(tmp_path, row_id), "aborted")["stage"] == "lane_state"
    finally:
        await db.close()


# ======================================================================
# Venue state
# ======================================================================
async def test_exit_refuses_when_the_venue_balance_is_short(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.fetch_account_balance.side_effect = _Balances(base=("0.05",))
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REFUSED
        )
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
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REFUSED
        )
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
            "client_order_id": _CID,
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
        assert row_id == 1
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REFUSED
        )
        adapter.place_limit_order.assert_not_called()
        assert _CID in _step(_steps(tmp_path, row_id), "aborted")["reason"]
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
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REFUSED
        )
        adapter.place_limit_order.assert_not_called()
        abort = _step(_steps(tmp_path, row_id), "aborted")
        assert abort["stage"] == "venue_open_orders"
        assert "unreadable listing" in abort["reason"]
    finally:
        await db.close()


async def test_exit_refuses_when_the_pair_has_no_lot_precision(tmp_path, monkeypatch):
    """Without lot_decimals there is no honest reconciliation tolerance."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.fetch_exchange_info_row.return_value = {"altname": _PAIR}
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REFUSED
        )
        adapter.place_limit_order.assert_not_called()
        assert _step(_steps(tmp_path, row_id), "aborted")["stage"] == "market_rules"
    finally:
        await db.close()


# ======================================================================
# Fee tier
# ======================================================================
async def test_exit_refuses_when_the_fee_lookup_fails(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.fetch_fee_tier.side_effect = KrakenAPIError("TradeVolume exploded")
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REFUSED
        )
        adapter.place_limit_order.assert_not_called()
        abort = _step(_steps(tmp_path, row_id), "aborted")
        assert abort["stage"] == "fee_tier"
        assert "no hard-coded rate to fall back to" in abort["reason"]
    finally:
        await db.close()


@pytest.mark.parametrize(
    "tier",
    [
        {"maker_pct": None, "taker_pct": None},
        {"maker_pct": _MAKER_PCT, "taker_pct": None},
        {"maker_pct": "not-a-number", "taker_pct": _TAKER_PCT},
        {"maker_pct": "-0.4", "taker_pct": _TAKER_PCT},
    ],
    ids=["both-missing", "taker-missing", "malformed", "negative"],
)
async def test_exit_refuses_unusable_fee_rates(tmp_path, monkeypatch, tier):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.fetch_fee_tier.return_value = _fee_tier(**tier)
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
# The cost screen shows BOTH assumptions
# ======================================================================
async def test_both_fee_scenarios_are_shown_with_the_real_entry_fee(
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
        assert Decimal(scenarios["entry_fee"]) == Decimal("0.12")
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
        # The next tier is decision-relevant on a small account.
        assert "next fee tier" in out and "2500.00000" in out
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
        assert Decimal(scenarios["entry_fee"]) == Decimal("0.12")
    finally:
        await db.close()


# ======================================================================
# The typed authorization is bound to the order AND the state
# ======================================================================
async def test_exit_refuses_a_wrong_token(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, "DEADBEEF")
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REFUSED
        )
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


async def test_exit_refuses_a_piped_authorization(tmp_path, monkeypatch, capsys):
    """A non-TTY stdin carrying the RIGHT token is still a refusal. EOF alone
    would not catch `echo <token> | kraken_pilot exit`, which is exactly the
    automation this approval boundary exists to exclude."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token(), tty=False)
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REFUSED
        )
        adapter.place_limit_order.assert_not_called()
        assert (
            _step(_steps(tmp_path, row_id), "authorization")["detail"]
            == "not_interactive"
        )
        assert "not a terminal" in capsys.readouterr().out
    finally:
        await db.close()


async def test_a_token_for_one_price_does_not_authorize_another(tmp_path, monkeypatch):
    """An operator who read and approved a 110.0 sell must not have that
    approval carried onto a 105.0 one."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token(price=Decimal("110.0")))
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=Decimal("105.0"))
            == EXIT_REFUSED
        )
        adapter.place_limit_order.assert_not_called()
        assert _step(_steps(tmp_path, row_id), "authorization")["detail"] == "mismatch"
    finally:
        await db.close()


def test_the_token_changes_with_every_bound_field():
    baseline = _token()
    variants = [
        _token(price=Decimal("110.1")),
        _token(volume=Decimal("0.16")),
        _token(row_id=2),
        _token(balance=Decimal("2.0")),
        _token(maker_ceiling="0.5000"),
        _token(taker_ceiling="0.9000"),
        _token(open_orders=[{"txid": "OTHER", "pair": "ETHUSD", "vol": "1"}]),
    ]
    for variant in variants:
        assert variant != baseline
    # Same economic order, differently-spelled Decimals — one approval.
    assert _token(volume=Decimal("0.1500")) == baseline


def test_the_open_order_fingerprint_is_order_insensitive_and_change_sensitive():
    a = {"txid": "A", "client_order_id": None, "pair": "XBTUSD", "vol": "1"}
    b = {"txid": "B", "client_order_id": "x", "pair": "ETHUSD", "vol": "2"}
    assert open_order_fingerprint([a, b]) == open_order_fingerprint([b, a])
    assert open_order_fingerprint([]) == "none"
    assert open_order_fingerprint([a]) != open_order_fingerprint([a, b])
    assert open_order_fingerprint([a]) != open_order_fingerprint(
        [{**a, "vol_exec": "0.5"}]
    )


# ======================================================================
# Pre-submit verification — the approval is void if the world moved
# ======================================================================
async def test_a_balance_drop_after_approval_voids_the_authorization(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    adapter = _adapter()
    balances = adapter.balances

    def _typed(_prompt: str = "") -> str:
        # The operator types the token; the world moves while they do.
        balances.set(_SYMBOL, "0.02")
        return _token()

    monkeypatch.setattr(kraken_pilot.sys, "stdin", _Tty())
    monkeypatch.setattr("builtins.input", _typed)
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REFUSED
        )
        adapter.place_limit_order.assert_not_called()
        abort = _step(_steps(tmp_path, row_id), "aborted")
        assert abort["stage"] == "pre_submit_recheck_balance"
        assert "AFTER the authorization" in abort["reason"]
    finally:
        await db.close()


async def test_an_order_appearing_after_approval_voids_the_authorization(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    adapter = _adapter()

    def _typed(_prompt: str = "") -> str:
        adapter.fetch_open_orders.return_value = [
            {
                "txid": "OLATE-1",
                "client_order_id": None,
                "pair": _PAIR,
                "status": "open",
                "vol": "0.15",
                "vol_exec": "0",
                "price": "120.0",
            }
        ]
        return _token()

    monkeypatch.setattr(kraken_pilot.sys, "stdin", _Tty())
    monkeypatch.setattr("builtins.input", _typed)
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REFUSED
        )
        adapter.place_limit_order.assert_not_called()
        assert (
            _step(_steps(tmp_path, row_id), "aborted")["stage"]
            == "pre_submit_recheck_open_orders"
        )
    finally:
        await db.close()


async def test_a_position_quantity_change_after_approval_voids_the_authorization(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    holder: dict = {}

    def _typed(_prompt: str = "") -> str:
        holder["ran"] = True
        return _token()

    monkeypatch.setattr(kraken_pilot.sys, "stdin", _Tty())
    monkeypatch.setattr("builtins.input", _typed)
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        row_id = await _seed_row(db)

        original = kraken_pilot.fetch_live_trade_row
        calls = {"n": 0}

        async def _mutating(database, live_trade_id):
            calls["n"] += 1
            if calls["n"] == 2:
                # Between the approval screen and the pre-submit read.
                await kraken_pilot.update_live_trade(
                    database, live_trade_id, entry_fill_qty="0.10"
                )
            return await original(database, live_trade_id)

        monkeypatch.setattr(kraken_pilot, "fetch_live_trade_row", _mutating)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REFUSED
        )
        assert holder["ran"] is True
        adapter.place_limit_order.assert_not_called()
        abort = _step(_steps(tmp_path, row_id), "aborted")
        assert abort["stage"] == "pre_submit_recheck"
        assert "position quantity changed" in abort["reason"]
    finally:
        await db.close()


# ----------------------------------------------------------------------
# The fee contract, in full. ONE rule:
#
#     current fee <= approved ceiling  -> remains authorized
#     current fee >  approved ceiling  -> authorization invalidated
#
# An earlier revision digested the exact current rates, which made the
# contract self-contradictory: an IMPROVED rate also changed the digest and
# voided an authorization the operator would obviously still give. These six
# cases pin the single coherent rule so that cannot come back.
# ----------------------------------------------------------------------
async def _run_exit_with_fee_change_at_prompt(tmp_path, monkeypatch, tier_override):
    """Approve, then move the fee schedule while the operator types."""
    _fix_decision_id(monkeypatch)
    adapter = _adapter()

    def _typed(_prompt: str = "") -> str:
        if tier_override is not None:
            adapter.fetch_fee_tier.return_value = _fee_tier(**tier_override)
        return _token()

    monkeypatch.setattr(kraken_pilot.sys, "stdin", _Tty())
    monkeypatch.setattr("builtins.input", _typed)
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        code = await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
        return code, adapter, _steps(tmp_path, row_id)
    finally:
        await db.close()


@pytest.mark.parametrize(
    ("label", "override"),
    [
        ("unchanged", None),
        ("improved-maker", {"maker_pct": "0.2000"}),
        ("improved-taker", {"taker_pct": "0.5000"}),
        ("improved-both", {"maker_pct": "0.2000", "taker_pct": "0.5000"}),
    ],
)
async def test_a_fee_at_or_below_the_ceiling_remains_authorized(
    tmp_path, monkeypatch, label, override
):
    """A rate the operator would be HAPPIER with must not void their approval."""
    code, adapter, steps = await _run_exit_with_fee_change_at_prompt(
        tmp_path, monkeypatch, override
    )
    assert code == EXIT_OK, label
    assert adapter.place_limit_order.call_count == 1, label
    recheck = _step(steps, "pre_submit_recheck")
    assert recheck["outcome"] == "clear", label
    # The ceiling is what was bound, so the digest is unmoved by a better rate.
    assert recheck["digest"] == _step(steps, "authorization_bound")["digest"], label


@pytest.mark.parametrize(
    ("label", "override"),
    [
        ("worsened-maker", {"maker_pct": "0.9000"}),
        ("worsened-taker", {"taker_pct": "1.2000"}),
        ("worsened-both", {"maker_pct": "0.9000", "taker_pct": "1.2000"}),
    ],
)
async def test_a_fee_above_the_ceiling_invalidates_the_authorization(
    tmp_path, monkeypatch, label, override
):
    code, adapter, steps = await _run_exit_with_fee_change_at_prompt(
        tmp_path, monkeypatch, override
    )
    assert code == EXIT_REFUSED, label
    adapter.place_limit_order.assert_not_called()
    abort = _step(steps, "aborted")
    assert abort["stage"] == "pre_submit_recheck", label
    assert "above the authorized ceiling" in abort["reason"].lower(), label


async def test_a_fee_schedule_that_breaks_at_the_recheck_is_refused(
    tmp_path, monkeypatch
):
    """Malformed or missing rates refuse — never a guess, never a stale reuse."""
    _fix_decision_id(monkeypatch)
    adapter = _adapter()

    def _typed(_prompt: str = "") -> str:
        adapter.fetch_fee_tier.side_effect = KrakenAPIError("TradeVolume down")
        return _token()

    monkeypatch.setattr(kraken_pilot.sys, "stdin", _Tty())
    monkeypatch.setattr("builtins.input", _typed)
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


def test_the_digest_binds_ceilings_and_not_current_rates():
    """Structural: the bound field set names ceilings, and only ceilings."""
    snapshot = _snapshot()
    assert "maker_ceiling" in snapshot and "taker_ceiling" in snapshot
    assert "maker_pct" not in snapshot and "taker_pct" not in snapshot
    # Two runs whose CURRENT rates differ but whose ceilings match are one
    # approval; two whose ceilings differ are not.
    assert _token(maker_ceiling=_MAKER_PCT) == _token()
    assert _token(maker_ceiling="0.9000") != _token()


async def test_the_recheck_records_a_matching_digest_on_the_clean_path(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_OK
        )
        steps = _steps(tmp_path, row_id)
        bound = _step(steps, "authorization_bound")
        recheck = _step(steps, "pre_submit_recheck")
        assert recheck["outcome"] == "clear"
        assert recheck["digest"] == bound["digest"]
    finally:
        await db.close()


# ======================================================================
# Durable intent, exactly one submission, ambiguity resolution
# ======================================================================
async def test_the_intent_is_persisted_before_the_order_is_submitted(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    seen: dict = {}

    def _place(**kwargs):
        seen["steps_at_submit"] = [s["step"] for s in _steps(tmp_path, 1)]
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
        order = seen["steps_at_submit"]
        assert "exit_intent_persisted" in order
        assert "submitted" not in order
        # The recheck runs before the intent, so an approval voided by a state
        # change never leaves an intent record behind.
        assert order.index("pre_submit_recheck") < order.index("exit_intent_persisted")
        intent = _step(_steps(tmp_path, row_id), "exit_intent_persisted")
        assert intent["live_trade_id"] == row_id
        assert intent["pair"] == _PAIR
        assert intent["side"] == "sell"
        assert intent["volume"] == "0.15"
        assert intent["price"] == "110.0"
        assert intent["client_order_id"] == _CID
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
        assert kwargs["client_order_id"] == _CID
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
        "timeout", client_order_id=_CID
    )
    adapter.resolve_order_submission_detail.return_value = {
        "verdict": "accepted",
        "client_order_id": _CID,
        "txid": [_EXIT_TXID],
        "probes": [],
    }
    adapter.fetch_order_by_client_id.return_value = _confirmation("filled")
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_OK
        )
        assert adapter.place_limit_order.call_count == 1
        adapter.resolve_order_submission_detail.assert_awaited_once()
        assert (
            _step(_steps(tmp_path, row_id), "ambiguity_adopted")["txid"] == _EXIT_TXID
        )
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
        "socket died", client_order_id=_CID
    )
    adapter.resolve_order_submission_detail.return_value = {
        "verdict": "not_accepted",
        "client_order_id": _CID,
        "txid": [],
        "probes": [],
    }
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REFUSED
        )
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
        "502", client_order_id=_CID
    )
    adapter.resolve_order_submission_detail.return_value = {
        "verdict": "unresolved",
        "client_order_id": _CID,
        "txid": [],
        "probes": [],
    }
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_ESCALATE
        )
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
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REFUSED
        )
        row = await _row(db, row_id)
        assert row["status"] == "open"
        assert row["exit_order_id"] is None
        adapter.resolve_order_submission_detail.assert_not_called()
    finally:
        await db.close()


async def test_an_interruption_after_submission_leaves_the_txid_recoverable(
    tmp_path, monkeypatch
):
    """The order landed and the process then failed before reconciling. The
    txid must already be on the row, and the row must stay non-terminal."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.await_fill_confirmation.side_effect = RuntimeError("process interrupted")
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_ESCALATE
        )
        row = await _row(db, row_id)
        assert row["exit_order_id"] == _EXIT_TXID
        assert row["status"] == "open"
        assert row["closed_at"] is None
        steps = _steps(tmp_path, row_id)
        assert _step(steps, "exit_txid_persisted")["txid"] == _EXIT_TXID
        # Completed, so it is not a dangling intent — the run accounted for
        # itself even though it failed.
        assert _step(steps, "run_completed")["exit_code"] == EXIT_ESCALATE
        assert runner.dangling_exit_intents(row_id) == []
    finally:
        await db.close()


# ======================================================================
# Partial, close, review
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
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REVIEW
        )
        row = await _row(db, row_id)
        assert row["status"] == "open"
        assert Decimal(row["entry_fill_qty"]) == Decimal("0.10")
        assert row["exit_order_id"] == _EXIT_TXID
        assert Decimal(row["exit_fill_price"]) == Decimal("110.0")
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
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_OK
        )
        row = await _row(db, row_id)
        assert row["status"] == "closed_via_reconciliation"
        assert row["exit_order_id"] == _EXIT_TXID
        assert Decimal(row["exit_fill_price"]) == Decimal("110.0")
        # 16.50 gross - 0.132 exit fee - (15.00 + 0.12 entry) = 1.248
        assert Decimal(row["realized_pnl_usd"]) == _EXPECTED_PNL
        assert Decimal(row["realized_pnl_pct"]).quantize(Decimal("0.0001")) == Decimal(
            "8.2540"
        )
        assert row["closed_at"] is not None

        recon = _step(_steps(tmp_path, row_id), "exit_reconciliation")
        assert recon["verdict"] == "pass"
        assert recon["mismatches"] == []
        assert recon["base_mismatches"] == [] and recon["quote_mismatches"] == []
        assert recon["entry_fee_source"] == "actual_entry_fills"
        # One lot tick at 8 decimals — not a percentage of position size.
        assert Decimal(recon["base"]["lot_tolerance"]) == Decimal("0.00000001")
    finally:
        await db.close()


async def test_a_balance_delta_one_lot_tick_out_still_closes(tmp_path, monkeypatch):
    """The tolerance is a representation unit, so a last-place difference must
    not block a genuine close."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    # 1.0 -> 0.85000001 : one lot tick short of the exact 0.15 move.
    adapter.fetch_account_balance.side_effect = _Balances(
        base=("1.0", "1.0", "0.85000001")
    )
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_OK
        )
        assert (await _row(db, row_id))["status"] == "closed_via_reconciliation"
    finally:
        await db.close()


async def test_a_materially_short_balance_move_does_not_close_the_row(
    tmp_path, monkeypatch
):
    """The old percentage band would have accepted this: 0.0015 BTC is 1% of
    the position, and the sale is short by exactly that."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    # Only 0.1485 actually left the account — 1% short of the 0.15 sold.
    adapter.fetch_account_balance.side_effect = _Balances(base=("1.0", "1.0", "0.8515"))
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REVIEW
        )
        row = await _row(db, row_id)
        assert row["status"] == "needs_manual_review"
        assert row["exit_order_id"] == _EXIT_TXID
        assert row["realized_pnl_usd"] is None
        assert row["closed_at"] is None
        recon = _step(_steps(tmp_path, row_id), "exit_reconciliation")
        assert recon["verdict"] == "review"
        assert any("cumulative executed volume" in m for m in recon["base_mismatches"])
    finally:
        await db.close()


async def test_a_high_exit_fee_does_not_widen_the_base_comparison(
    tmp_path, monkeypatch
):
    """Quote-side variability must never loosen the base-asset check: the fee
    lands in quote_mismatches and the base comparison is unaffected."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.fetch_order_fills.side_effect = _fills_by_txid(
        {
            _ENTRY_TXID: _ENTRY_FILLS,
            # A fee far above the account's own taker rate on this notional.
            _EXIT_TXID: [{**_EXIT_FILLS[0], "fee": "5.00000"}],
        }
    )
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REVIEW
        )
        recon = _step(_steps(tmp_path, row_id), "exit_reconciliation")
        assert recon["base_mismatches"] == []
        assert any("taker rate" in m for m in recon["quote_mismatches"])
        assert (await _row(db, row_id))["status"] == "needs_manual_review"
    finally:
        await db.close()


async def test_an_execution_below_the_limit_does_not_close_the_row(
    tmp_path, monkeypatch
):
    """A limit SELL cannot execute below its limit, so a VWAP that did is not
    the order we placed."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.await_fill_confirmation.return_value = _confirmation(
        "filled", fill_price=90.0
    )
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REVIEW
        )
        recon = _step(_steps(tmp_path, row_id), "exit_reconciliation")
        assert any("below the" in m for m in recon["quote_mismatches"])
        assert (await _row(db, row_id))["status"] == "needs_manual_review"
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
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REVIEW
        )
        assert (await _row(db, row_id))["status"] == "needs_manual_review"
        recon = _step(_steps(tmp_path, row_id), "exit_reconciliation")
        assert any("no per-fill records" in m for m in recon["base_mismatches"])
    finally:
        await db.close()


async def test_a_resting_exit_order_leaves_the_row_open(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    adapter = _adapter()
    adapter.await_fill_confirmation.return_value = OrderConfirmation(
        venue="kraken",
        venue_order_id=_EXIT_TXID,
        client_order_id=_CID,
        status="timeout",
        filled_qty=None,
        fill_price=None,
        raw_response=None,
    )
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_OK
        )
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
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REFUSED
        )
        row = await _row(db, row_id)
        assert row["status"] == "open"
        assert Decimal(row["entry_fill_qty"]) == _HELD_QTY
    finally:
        await db.close()


# ======================================================================
# Interrupted runs block the lane; exit-status clears them
# ======================================================================
async def test_a_dangling_intent_blocks_a_fresh_exit(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        row_id = await _seed_row(db)
        _write_dangling_intent(tmp_path, row_id)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_REFUSED
        )
        adapter.place_limit_order.assert_not_called()
        abort = _step(_steps(tmp_path, row_id), "aborted")
        assert abort["stage"] == "lane_state"
        assert "exit-status" in abort["reason"]
    finally:
        await db.close()


async def test_a_completed_run_is_not_dangling(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_OK
        )
        assert runner.dangling_exit_intents(row_id) == []
    finally:
        await db.close()


async def test_exit_status_clears_a_dangling_intent_only_on_proof(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    adapter = _adapter()
    adapter.resolve_order_submission_detail.return_value = {
        "verdict": "not_accepted",
        "client_order_id": _CID,
        "txid": [],
        "probes": [],
    }
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        _write_dangling_intent(tmp_path, row_id)
        assert runner.dangling_exit_intents(row_id) != []
        assert await runner.exit_status(live_trade_id=row_id) == EXIT_OK
        # Read-only at the venue: nothing was cancelled and nothing was placed.
        adapter.place_limit_order.assert_not_called()
        adapter.cancel_order.assert_not_called()
        assert runner.dangling_exit_intents(row_id) == []
    finally:
        await db.close()


async def test_exit_status_leaves_the_block_in_place_when_unresolved(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    adapter = _adapter()
    adapter.resolve_order_submission_detail.return_value = {
        "verdict": "unresolved",
        "client_order_id": _CID,
        "txid": [],
        "probes": [],
    }
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        _write_dangling_intent(tmp_path, row_id)
        assert await runner.exit_status(live_trade_id=row_id) == EXIT_BLOCKED
        assert runner.dangling_exit_intents(row_id) != []
        adapter.cancel_order.assert_not_called()
    finally:
        await db.close()


async def test_exit_status_reports_a_working_order_without_touching_it(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    adapter = _adapter()
    adapter.fetch_order_by_client_id.return_value = _confirmation(
        "pending", filled_qty=0.0, fill_price=None
    )
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        assert await runner.exit_status(live_trade_id=row_id) == EXIT_BLOCKED
        adapter.cancel_order.assert_not_called()
        adapter.place_limit_order.assert_not_called()
        status = _step(_steps(tmp_path, row_id, "status"), "exit_status")
        assert status["venue_order_status"] == "pending"
    finally:
        await db.close()


async def test_exit_after_a_cleared_intent_proceeds(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    adapter = _adapter()
    adapter.resolve_order_submission_detail.return_value = {
        "verdict": "not_accepted",
        "client_order_id": _CID,
        "txid": [],
        "probes": [],
    }
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        _write_dangling_intent(tmp_path, row_id)
        assert await runner.exit_status(live_trade_id=row_id) == EXIT_OK
        _authorize(monkeypatch, _token())
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_OK
        )
    finally:
        await db.close()


# ======================================================================
# exit-cancel
# ======================================================================
async def test_exit_cancel_refuses_when_no_exit_order_exists(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    adapter = _adapter()
    adapter.fetch_order_by_client_id.return_value = None
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        assert await runner.exit_cancel(live_trade_id=row_id) == EXIT_REFUSED
        adapter.cancel_order.assert_not_called()
        abort = _step(_steps(tmp_path, row_id, "cancel"), "aborted")
        assert "nothing to cancel" in abort["reason"]
    finally:
        await db.close()


async def test_exit_cancel_targets_the_exit_client_id(tmp_path, monkeypatch):
    """`cancel --decision-id` keys off the ENTRY client id and structurally
    cannot reach an exit order. This one is derived from the row."""
    _fix_decision_id(monkeypatch)
    adapter = _adapter()
    working = _confirmation("pending", filled_qty=0.0, fill_price=None)
    cancelled = _confirmation("rejected", filled_qty=0.0, fill_price=None)
    adapter.fetch_order_by_client_id.side_effect = [working, cancelled]
    adapter.cancel_order.return_value = {
        "count": 1,
        "pending": False,
        "already_gone": False,
    }
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        assert await runner.exit_cancel(live_trade_id=row_id) == EXIT_REFUSED
        # The pre-cancel lookup used the exit cid, not the entry one.
        lookup = adapter.fetch_order_by_client_id.call_args_list[0].kwargs
        assert lookup["client_order_id"] == _CID
        assert adapter.cancel_order.call_count == 1
        assert adapter.cancel_order.call_args.kwargs["txid"] == _EXIT_TXID
        row = await _row(db, row_id)
        assert row["status"] == "open"
        assert Decimal(row["entry_fill_qty"]) == _HELD_QTY
    finally:
        await db.close()


async def test_exit_cancel_reconciles_a_partial_fill_and_reduces_the_position(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    adapter = _adapter()
    working = _confirmation("pending", filled_qty=0.0, fill_price=None)
    partial = _confirmation("partial", filled_qty=0.05, fill_price=110.0)
    adapter.fetch_order_by_client_id.side_effect = [working, partial]
    adapter.cancel_order.return_value = {
        "count": 1,
        "pending": False,
        "already_gone": False,
    }
    adapter.fetch_order_fills.side_effect = _fills_by_txid(
        {
            _ENTRY_TXID: _ENTRY_FILLS,
            _EXIT_TXID: [{**_EXIT_FILLS[0], "vol": "0.05", "cost": "5.5"}],
        }
    )
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        assert await runner.exit_cancel(live_trade_id=row_id) == EXIT_REVIEW
        row = await _row(db, row_id)
        assert row["status"] == "open"
        assert Decimal(row["entry_fill_qty"]) == Decimal("0.10")
        assert row["exit_order_id"] == _EXIT_TXID
        assert row["realized_pnl_usd"] is None
        partial_step = _step(_steps(tmp_path, row_id, "cancel"), "exit_partial_fill")
        assert partial_step["origin"] == "exit-cancel"
        assert partial_step["remaining_qty"] == "0.10"
    finally:
        await db.close()


async def test_exit_cancel_never_restores_a_reduced_position(tmp_path, monkeypatch):
    """A held quantity only ever falls. Re-running the cancel against an order
    whose fills were already folded in escalates rather than double-counting."""
    _fix_decision_id(monkeypatch)
    adapter = _adapter()
    partial = _confirmation("partial", filled_qty=0.05, fill_price=110.0)
    adapter.fetch_order_by_client_id.return_value = partial
    adapter.fetch_order_fills.side_effect = _fills_by_txid(
        {
            _ENTRY_TXID: _ENTRY_FILLS,
            _EXIT_TXID: [{**_EXIT_FILLS[0], "vol": "0.05", "cost": "5.5"}],
        }
    )
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        # First pass folds the partial in.
        assert await runner.exit_cancel(live_trade_id=row_id) == EXIT_REVIEW
        assert Decimal((await _row(db, row_id))["entry_fill_qty"]) == Decimal("0.10")

        # Second pass on the SAME order must not subtract the same coins again.
        monkeypatch.setattr(kraken_pilot, "uuid4", lambda: UUID(int=2))
        assert await runner.exit_cancel(live_trade_id=row_id) == EXIT_ESCALATE
        row = await _row(db, row_id)
        assert Decimal(row["entry_fill_qty"]) == Decimal("0.10")
        assert row["status"] == "needs_manual_review"
    finally:
        await db.close()


async def test_exit_cancel_after_the_order_already_filled_does_not_close_the_row(
    tmp_path, monkeypatch
):
    """Cancelling after a full fill must account for the sale without sending a
    pointless CancelOrder — and must NOT close the row.

    The coins left before this command sampled anything, so there is no
    pre-trade balance to measure the sale against. The runner says so instead
    of inventing a baseline: a balance read after the fact produces a ~0 delta,
    which would read as "nothing sold" for a sale that did happen. The numbers
    are all recorded; a human sets the outcome."""
    _fix_decision_id(monkeypatch)
    adapter = _adapter()
    adapter.fetch_order_by_client_id.return_value = _confirmation("filled")
    # Already reduced when the command starts — the sale predates this run.
    adapter.fetch_account_balance.side_effect = _Balances(base=("0.85",))
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        assert await runner.exit_cancel(live_trade_id=row_id) == EXIT_REVIEW
        adapter.cancel_order.assert_not_called()
        row = await _row(db, row_id)
        assert row["status"] == "needs_manual_review"
        assert row["exit_order_id"] == _EXIT_TXID
        assert row["realized_pnl_usd"] is None
        assert row["closed_at"] is None
        recon = _step(_steps(tmp_path, row_id, "cancel"), "exit_reconciliation")
        assert recon["origin"] == "exit-cancel"
        assert recon["verdict"] == "review"
        assert any("no pre-trade balance sample" in m for m in recon["base_mismatches"])
        # The fill IS recorded, in full — it is unconfirmed, not denied.
        assert Decimal(recon["filled_qty"]) == _HELD_QTY
        assert Decimal(recon["exit_fee"]) == Decimal("0.132")
    finally:
        await db.close()


async def test_an_ambiguous_cancel_is_resolved_by_reading_not_resending(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    adapter = _adapter()
    working = _confirmation("pending", filled_qty=0.0, fill_price=None)
    settled = _confirmation("rejected", filled_qty=0.0, fill_price=None)
    adapter.fetch_order_by_client_id.side_effect = [working, settled]
    adapter.cancel_order.side_effect = KrakenAPIError("EService:Unavailable")
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        assert await runner.exit_cancel(live_trade_id=row_id) == EXIT_REFUSED
        # Exactly one cancel attempt, then a READ to settle it.
        assert adapter.cancel_order.call_count == 1
        steps = _steps(tmp_path, row_id, "cancel")
        assert _step(steps, "cancel_ambiguous") is not None
        assert _step(steps, "cancel_ambiguity_resolution")["status"] == "rejected"
        assert (await _row(db, row_id))["status"] == "open"
    finally:
        await db.close()


async def test_an_unresolvable_cancel_escalates(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    adapter = _adapter()
    working = _confirmation("pending", filled_qty=0.0, fill_price=None)
    adapter.fetch_order_by_client_id.side_effect = [working, None]
    adapter.cancel_order.side_effect = KrakenAPIError("EService:Unavailable")
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        assert await runner.exit_cancel(live_trade_id=row_id) == EXIT_ESCALATE
        assert adapter.cancel_order.call_count == 1
    finally:
        await db.close()


async def test_exit_cancel_refuses_a_non_kraken_row(tmp_path, monkeypatch):
    _fix_decision_id(monkeypatch)
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        row_id = await _seed_row(db, venue="solana", pair="SOL/USDC")
        assert await runner.exit_cancel(live_trade_id=row_id) == EXIT_REFUSED
        adapter.cancel_order.assert_not_called()
    finally:
        await db.close()


# ======================================================================
# Rehearsal and envelope
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
        assert (
            await runner.exit_position(
                live_trade_id=row_id, price=_EXIT_PRICE, validate_only=True
            )
            == EXIT_OK
        )
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
# CLI
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
    for argv in (
        ["exit", "--price", "110.0"],
        ["exit", "--live-trade-id", "1"],
        ["exit", "--live-trade-id", "0", "--price", "110.0"],
        ["exit", "--live-trade-id", "1", "--price", "0"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)
    args = parser.parse_args(["exit", "--live-trade-id", "7", "--price", "110.0"])
    assert args.live_trade_id == 7
    assert args.price == Decimal("110.0")


@pytest.mark.parametrize("command", ["exit-cancel", "exit-status"])
def test_the_recovery_subcommands_need_exactly_one_target(command):
    parser = kraken_pilot.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([command])
    with pytest.raises(SystemExit):
        parser.parse_args([command, "--live-trade-id", "1", "--decision-id", _DECISION])
    assert parser.parse_args([command, "--live-trade-id", "7"]).live_trade_id == 7
    assert (
        parser.parse_args([command, "--decision-id", _DECISION]).decision_id
        == _DECISION
    )


def test_a_decision_id_resolves_to_its_live_trade_id(tmp_path):
    directory = _evidence_dir(tmp_path)
    directory.mkdir(parents=True)
    (directory / f"kraken_pilot_exit_42_{_DECISION}.json").write_text("", "utf-8")
    assert kraken_pilot.resolve_exit_decision_id(directory, _DECISION) == 42
    assert kraken_pilot.resolve_exit_decision_id(directory, "no-such-run") is None


# ======================================================================
# Crash recovery — a landed exit must be RETIRABLE without its process
#
# The originating run fsyncs its baseline (both balances, lot precision, fee
# ceilings, limit price, entry economics) immediately before submitting,
# precisely so a later process that shares none of its memory can reconcile
# the trade. needs_manual_review is for contradictory or unavailable exchange
# evidence — never for "the process exited".
# ======================================================================
def _write_intent(
    tmp_path,
    row_id: int = 1,
    decision_id: str = "dead-run",
    *,
    submitted_txid: str | None = None,
    base_before: str | None = "1.0",
    quote_before: str | None = "500.0",
    completed: bool = False,
):
    """An evidence file exactly as a run that died would have left it."""
    directory = _evidence_dir(tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"kraken_pilot_exit_{row_id}_{decision_id}.json"
    records = [
        {
            "step": "exit_intent_persisted",
            "at": "2026-08-02T00:00:00+00:00",
            "decision_id": decision_id,
            "live_trade_id": row_id,
            "pair": _PAIR,
            "side": "sell",
            "volume": "0.15",
            "price": "110.0",
            "client_order_id": f"gecko-x-{row_id}",
            "base_asset": _SYMBOL,
            "quote_asset": _QUOTE,
            "base_balance_before": base_before,
            "quote_balance_before": quote_before,
            "maker_ceiling_pct": _MAKER_PCT,
            "taker_ceiling_pct": _TAKER_PCT,
            "lot_decimals": _LOT_DECIMALS,
            "base_tolerance": "0.00000001",
            "entry_fill_price": "100.0",
            "entry_fee": "0.12",
            "entry_fee_source": "actual_entry_fills",
            "submission_state": "about_to_submit",
        }
    ]
    if submitted_txid:
        records.append(
            {
                "step": "submitted",
                "at": "2026-08-02T00:00:01+00:00",
                "txid": submitted_txid,
                "submission_state": "submitted",
            }
        )
    if completed:
        records.append(
            {
                "step": "run_completed",
                "at": "2026-08-02T00:00:02+00:00",
                "decision_id": decision_id,
                "exit_code": 0,
            }
        )
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


async def test_the_persisted_intent_carries_the_whole_recovery_baseline(
    tmp_path, monkeypatch
):
    """Everything a foreign process needs to reconcile, in one fsynced record."""
    _fix_decision_id(monkeypatch)
    _authorize(monkeypatch, _token())
    runner, db, adapter = await _make_runner(tmp_path)
    try:
        row_id = await _seed_row(db)
        assert (
            await runner.exit_position(live_trade_id=row_id, price=_EXIT_PRICE)
            == EXIT_OK
        )
        intent = _step(_steps(tmp_path, row_id), "exit_intent_persisted")
        for field in (
            "live_trade_id",
            "decision_id",
            "pair",
            "side",
            "volume",
            "price",
            "client_order_id",
            "digest",
            "base_asset",
            "quote_asset",
            "base_balance_before",
            "quote_balance_before",
            "open_order_fingerprint",
            "maker_ceiling_pct",
            "taker_ceiling_pct",
            "lot_decimals",
            "base_tolerance",
            "entry_fill_price",
            "entry_fee",
            "authorized_at",
            "submission_state",
        ):
            assert intent.get(field) is not None, field
        assert Decimal(intent["base_balance_before"]) == Decimal("1.0")
        assert Decimal(intent["quote_balance_before"]) == Decimal("500.0")
        assert intent["submission_state"] == "about_to_submit"
        # Written BEFORE the order existed.
        assert intent["at"] < _step(_steps(tmp_path, row_id), "submitted")["at"]
    finally:
        await db.close()


async def test_restart_after_intent_but_before_submission_retains_the_position(
    tmp_path, monkeypatch
):
    """Interruption point 1. No order was created, so the coins are still ours
    and the lane must unblock — not escalate."""
    _fix_decision_id(monkeypatch)
    adapter = _adapter()
    adapter.fetch_order_by_client_id.return_value = None
    adapter.resolve_order_submission_detail.return_value = {
        "verdict": "not_accepted",
        "client_order_id": _CID,
        "txid": [],
        "probes": [],
    }
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        _write_intent(tmp_path, row_id)
        assert await runner.exit_status(live_trade_id=row_id) == EXIT_OK
        row = await _row(db, row_id)
        assert row["status"] == "open"
        assert Decimal(row["entry_fill_qty"]) == _HELD_QTY
        assert row["realized_pnl_usd"] is None
        assert runner.dangling_exit_intents(row_id) == []
        adapter.place_limit_order.assert_not_called()
        adapter.cancel_order.assert_not_called()
    finally:
        await db.close()


async def test_restart_after_submission_closes_a_fully_filled_exit(
    tmp_path, monkeypatch
):
    """Interruption point 2/3, the one the previous revision could not do.

    The process that submitted is gone. exit-status resolves the order, reads
    the fills, compares CURRENT balances against the PERSISTED baseline, and
    RETIRES the row with realised P&L — no needs_manual_review."""
    _fix_decision_id(monkeypatch)
    adapter = _adapter()
    adapter.fetch_order_by_client_id.return_value = _confirmation("filled")
    # Post-restart balances: the sale already happened.
    adapter.fetch_account_balance.side_effect = _Balances(
        base=("0.85",), quote=("516.368",)
    )
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        _write_intent(tmp_path, row_id, submitted_txid=_EXIT_TXID)
        assert await runner.exit_status(live_trade_id=row_id) == EXIT_OK
        row = await _row(db, row_id)
        assert row["status"] == "closed_via_reconciliation"
        assert row["exit_order_id"] == _EXIT_TXID
        assert Decimal(row["realized_pnl_usd"]) == _EXPECTED_PNL
        assert row["closed_at"] is not None
        recon = _step(_steps(tmp_path, row_id, "status"), "exit_reconciliation")
        assert recon["origin"] == "exit-status"
        assert recon["verdict"] == "pass"
        assert Decimal(recon["base"]["before"]) == Decimal("1.0")
        assert recon["quote"]["checked"] is True
        # The interrupted run is accounted for, so the lane unblocks.
        assert runner.dangling_exit_intents(row_id) == []
        adapter.place_limit_order.assert_not_called()
        adapter.cancel_order.assert_not_called()
    finally:
        await db.close()


async def test_restart_after_submission_reduces_a_partially_filled_exit(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    adapter = _adapter()
    adapter.fetch_order_by_client_id.return_value = _confirmation(
        "partial", filled_qty=0.05, fill_price=110.0
    )
    adapter.fetch_order_fills.side_effect = _fills_by_txid(
        {
            _ENTRY_TXID: _ENTRY_FILLS,
            _EXIT_TXID: [{**_EXIT_FILLS[0], "vol": "0.05", "cost": "5.5"}],
        }
    )
    adapter.fetch_account_balance.side_effect = _Balances(
        base=("0.95",), quote=("505.456",)
    )
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        _write_intent(tmp_path, row_id, submitted_txid=_EXIT_TXID)
        assert await runner.exit_status(live_trade_id=row_id) == EXIT_REVIEW
        row = await _row(db, row_id)
        assert row["status"] == "open"
        assert Decimal(row["entry_fill_qty"]) == Decimal("0.10")
        assert row["exit_order_id"] == _EXIT_TXID
        assert row["realized_pnl_usd"] is None
        partial = _step(_steps(tmp_path, row_id, "status"), "exit_partial_fill")
        assert partial["origin"] == "exit-status"
        assert partial["remaining_qty"] == "0.10"
    finally:
        await db.close()


async def test_restart_with_the_order_still_working_changes_nothing(
    tmp_path, monkeypatch
):
    _fix_decision_id(monkeypatch)
    adapter = _adapter()
    adapter.fetch_order_by_client_id.return_value = _confirmation(
        "pending", filled_qty=0.0, fill_price=None
    )
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        _write_intent(tmp_path, row_id, submitted_txid=_EXIT_TXID)
        assert await runner.exit_status(live_trade_id=row_id) == EXIT_BLOCKED
        row = await _row(db, row_id)
        assert row["status"] == "open"
        assert Decimal(row["entry_fill_qty"]) == _HELD_QTY
        adapter.cancel_order.assert_not_called()
        adapter.place_limit_order.assert_not_called()
        # Still working, so the intent stays dangling and `exit` stays blocked.
        assert runner.dangling_exit_intents(row_id) != []
    finally:
        await db.close()


async def test_recovery_uses_the_same_predicate_and_will_not_close_on_a_short_move(
    tmp_path, monkeypatch
):
    """Recovery must not have a looser standard than the path that submitted.
    A materially short base move fails the SAME one-lot-tick check."""
    _fix_decision_id(monkeypatch)
    adapter = _adapter()
    adapter.fetch_order_by_client_id.return_value = _confirmation("filled")
    # 1.0 -> 0.8515 : only 0.1485 left, 1% short of the 0.15 the venue claims.
    adapter.fetch_account_balance.side_effect = _Balances(
        base=("0.8515",), quote=("516.368",)
    )
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        _write_intent(tmp_path, row_id, submitted_txid=_EXIT_TXID)
        assert await runner.exit_status(live_trade_id=row_id) == EXIT_REVIEW
        row = await _row(db, row_id)
        assert row["status"] == "needs_manual_review"
        assert row["realized_pnl_usd"] is None
        recon = _step(_steps(tmp_path, row_id, "status"), "exit_reconciliation")
        assert any("cumulative executed volume" in m for m in recon["base_mismatches"])
        assert Decimal(recon["base"]["lot_tolerance"]) == Decimal("0.00000001")
    finally:
        await db.close()


async def test_recovery_notices_when_the_proceeds_never_arrived(tmp_path, monkeypatch):
    """The base check proves the coins left; only the quote check proves they
    were paid for. That is why the intent persists BOTH balances."""
    _fix_decision_id(monkeypatch)
    adapter = _adapter()
    adapter.fetch_order_by_client_id.return_value = _confirmation("filled")
    adapter.fetch_account_balance.side_effect = _Balances(
        base=("0.85",),
        quote=("500.0",),  # unchanged: no money arrived
    )
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        _write_intent(tmp_path, row_id, submitted_txid=_EXIT_TXID)
        assert await runner.exit_status(live_trade_id=row_id) == EXIT_REVIEW
        assert (await _row(db, row_id))["status"] == "needs_manual_review"
        recon = _step(_steps(tmp_path, row_id, "status"), "exit_reconciliation")
        assert recon["base_mismatches"] == []
        assert any("did not arrive" in m for m in recon["quote_mismatches"])
    finally:
        await db.close()


async def test_recovery_without_a_persisted_baseline_is_the_only_review_case(
    tmp_path, monkeypatch
):
    """needs_manual_review for UNAVAILABLE evidence — and the message says the
    baseline is what is missing, not that a process exited."""
    _fix_decision_id(monkeypatch)
    adapter = _adapter()
    adapter.fetch_order_by_client_id.return_value = _confirmation("filled")
    adapter.fetch_account_balance.side_effect = _Balances(
        base=("0.85",), quote=("516.368",)
    )
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        _write_intent(tmp_path, row_id, submitted_txid=_EXIT_TXID, base_before=None)
        assert await runner.exit_status(live_trade_id=row_id) == EXIT_REVIEW
        assert (await _row(db, row_id))["status"] == "needs_manual_review"
        recon = _step(_steps(tmp_path, row_id, "status"), "exit_reconciliation")
        assert any("no pre-trade balance sample" in m for m in recon["base_mismatches"])
    finally:
        await db.close()


async def test_a_completed_intent_is_not_replayed_by_recovery(tmp_path, monkeypatch):
    """A run that finished is not dangling, and its row is left alone."""
    _fix_decision_id(monkeypatch)
    adapter = _adapter()
    adapter.fetch_order_by_client_id.return_value = None
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        _write_intent(tmp_path, row_id, submitted_txid=_EXIT_TXID, completed=True)
        assert runner.dangling_exit_intents(row_id) == []
        assert await runner.exit_status(live_trade_id=row_id) == EXIT_OK
        assert (await _row(db, row_id))["status"] == "open"
    finally:
        await db.close()


async def test_exit_cancel_settles_a_terminal_order_from_the_persisted_baseline(
    tmp_path, monkeypatch
):
    """The gap the previous revision left open: an already-filled order found
    by exit-cancel now closes, because the baseline was persisted."""
    _fix_decision_id(monkeypatch)
    adapter = _adapter()
    adapter.fetch_order_by_client_id.return_value = _confirmation("filled")
    adapter.fetch_account_balance.side_effect = _Balances(
        base=("0.85",), quote=("516.368",)
    )
    runner, db, adapter = await _make_runner(tmp_path, adapter)
    try:
        row_id = await _seed_row(db)
        _write_intent(tmp_path, row_id, submitted_txid=_EXIT_TXID)
        assert await runner.exit_cancel(live_trade_id=row_id) == EXIT_OK
        adapter.cancel_order.assert_not_called()
        row = await _row(db, row_id)
        assert row["status"] == "closed_via_reconciliation"
        assert Decimal(row["realized_pnl_usd"]) == _EXPECTED_PNL
        baseline = _step(_steps(tmp_path, row_id, "cancel"), "recovery_baseline")
        assert baseline["source"] == "persisted_exit_intent"
    finally:
        await db.close()
