"""Venue isolation for the autonomous closer.

The autonomous exit loop (``evaluate_open_live_trades``) historically selected
``WHERE status='open'`` with NO venue predicate, while ``scout/main.py`` hands
it exactly ONE adapter. With two venues live (Kraken row 1, Solana row 5) that
is a cross-venue mis-dispatch waiting to happen: a Solana position handed to the
Kraken adapter, which would try to sell ``SOL/USDC`` on a Bitcoin exchange.

Nothing had executed it — shadow mode, unset triggers, and a refusal stub each
independently blocked it — but those are accidental layers, not a contract.
These tests make the isolation a contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from scout.config import Settings
from scout.db import Database
from scout.live.adapter_base import OrderConfirmation
from scout.live.config import LiveConfig
from scout.live.kill_switch import KillSwitch
from scout.live.live_evaluator import evaluate_open_live_trades


def _settings(**overrides):
    base = dict(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        LIVE_MODE="live",
        LIVE_TRADING_ENABLED=True,
        LIVE_SIGNAL_ALLOWLIST="first_signal",
        # Triggers deliberately ARMED so that any row reaching the evaluation
        # body would immediately attempt a sell. That is what makes these tests
        # meaningful: if the venue filter regresses, the mis-dispatch fires.
        LIVE_TP_PCT=Decimal("1"),
        LIVE_SL_PCT=Decimal("1"),
        LIVE_MAX_DURATION_HOURS=1,
        LIVE_TRADE_AMOUNT_USD=Decimal("100"),
        LIVE_DAILY_LOSS_CAP_USD=Decimal("1000"),
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


async def _seed_open(db, *, venue: str, pair: str, cid: str):
    assert db._conn is not None
    await db._conn.execute(
        "INSERT INTO paper_trades (token_id, symbol, name, chain, signal_type, "
        "signal_data, entry_price, amount_usd, quantity, tp_price, sl_price, "
        "status, opened_at) "
        "VALUES (?,'L','N','eth','first_signal','{}',1.0,100.0,100.0,1.2,0.9,"
        "'open','2026-07-11T00:00:00+00:00')",
        # token_id varies per row: (token_id, signal_type, opened_at) is UNIQUE.
        (cid,),
    )
    await db._conn.commit()
    cur = await db._conn.execute("SELECT last_insert_rowid()")
    paper_id = (await cur.fetchone())[0]

    await db._conn.execute(
        "INSERT INTO live_trades "
        "(paper_trade_id, coin_id, symbol, venue, pair, signal_type, size_usd, "
        " entry_fill_price, entry_fill_qty, mid_at_entry, status, "
        " client_order_id, created_at) "
        "VALUES (?,'c','L',?,?,'first_signal','100','100','1','100','open',?,?)",
        (paper_id, venue, pair, cid, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()
    cur = await db._conn.execute("SELECT last_insert_rowid()")
    return (await cur.fetchone())[0]


def _adapter(venue_name: str):
    """An adapter that would happily complete a sell if the evaluator reached it.

    Deliberately fully functional: these tests must fail because the row was
    FILTERED OUT, never because the mock was too thin to complete a sale.
    """
    adapter = MagicMock()
    adapter.venue_name = venue_name
    adapter.fetch_price = AsyncMock(return_value=Decimal("1000"))
    adapter.place_exit_order = AsyncMock(
        return_value=OrderConfirmation(
            venue=venue_name or "?",
            venue_order_id="EXIT-1",
            client_order_id="gecko-x-1",
            status="filled",
            filled_qty=1.0,
            fill_price=1000.0,
            raw_response=None,
        )
    )
    return adapter


async def _run(db, adapter, settings):
    return await evaluate_open_live_trades(
        db=db,
        adapter=adapter,
        config=LiveConfig(settings),
        ks=KillSwitch(db),
        settings=settings,
    )


async def test_solana_row_never_reaches_the_kraken_adapter(tmp_path):
    """The headline contract: a Solana position is invisible to Kraken's closer."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        solana_id = await _seed_open(db, venue="solana", pair="SOL/USDC", cid="sol-1")
        adapter = _adapter("kraken")
        settings = _settings()

        await _run(db, adapter, settings)

        # Not priced, not sold, not touched.
        adapter.fetch_price.assert_not_awaited()
        adapter.place_exit_order.assert_not_awaited()

        cur = await db._conn.execute(
            "SELECT status FROM live_trades WHERE id=?", (solana_id,)
        )
        assert (await cur.fetchone())[0] == "open"
    finally:
        await db.close()


async def test_kraken_adapter_still_sees_its_own_rows(tmp_path):
    """The filter must not be so tight that it disables the closer entirely."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        await _seed_open(db, venue="kraken", pair="XBTUSD", cid="krk-1")
        adapter = _adapter("kraken")

        await _run(db, adapter, _settings())

        adapter.fetch_price.assert_awaited()
    finally:
        await db.close()


async def test_mixed_venues_only_the_matching_row_is_evaluated(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        solana_id = await _seed_open(db, venue="solana", pair="SOL/USDC", cid="sol-2")
        await _seed_open(db, venue="kraken", pair="XBTUSD", cid="krk-2")
        adapter = _adapter("kraken")

        await _run(db, adapter, _settings())

        # Exactly one row priced — the Kraken one.
        assert adapter.fetch_price.await_count == 1
        assert adapter.fetch_price.await_args.args[0] == "XBTUSD"

        cur = await db._conn.execute(
            "SELECT status FROM live_trades WHERE id=?", (solana_id,)
        )
        assert (await cur.fetchone())[0] == "open"
    finally:
        await db.close()


@pytest.mark.parametrize("bad_venue", [None, "", "   "])
async def test_adapter_without_a_venue_identity_is_refused(tmp_path, bad_venue):
    """Fail closed. An adapter that cannot say who it is must NOT fall back to
    scanning every venue's rows — that is the exact bug being removed."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        await _seed_open(db, venue="kraken", pair="XBTUSD", cid="krk-3")
        adapter = _adapter("kraken")
        adapter.venue_name = bad_venue

        with pytest.raises(ValueError, match="venue_name"):
            await _run(db, adapter, _settings())

        adapter.place_exit_order.assert_not_awaited()
    finally:
        await db.close()


async def test_venue_match_is_exact_not_a_prefix(tmp_path):
    """'kraken' must not match 'kraken_futures'."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        await _seed_open(db, venue="kraken_futures", pair="PF_XBTUSD", cid="krf-1")
        adapter = _adapter("kraken")

        await _run(db, adapter, _settings())

        adapter.fetch_price.assert_not_awaited()
        adapter.place_exit_order.assert_not_awaited()
    finally:
        await db.close()
