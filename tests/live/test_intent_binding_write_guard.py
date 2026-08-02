"""A row must not be able to assert a binding it does not have.

``live_trades.intent_hash`` claims that ``client_order_id`` was DERIVED from it at
that venue. The three binding fields on ``OrderRequest`` are independently
optional, so a caller can set the hash and let the adapter fall back to the legacy
uuid-derived id — writing a row that is broken at birth.

Boot reconciliation then terminalizes that row to ``needs_manual_review`` without
ever asking the venue about it. So the same caller mistake is either a refused
write or an orphaned real position, depending only on where it is caught. It is
caught at the write.
"""

from __future__ import annotations

import pytest

from scout.db import Database
from scout.live.idempotency import record_pending_order
from scout.live.order_id import VENUE_ORDER_ID_FORMS

_HASH = "a1b2c3d4e5f6" + "0" * 52  # 64 hex


async def _db(tmp_path) -> Database:
    db = Database(str(tmp_path / "w.db"))
    await db.initialize()
    await db._conn.execute(
        "INSERT INTO paper_trades (token_id, symbol, name, chain, signal_type, "
        "signal_data, entry_price, amount_usd, quantity, tp_pct, sl_pct, "
        "tp_price, sl_price, status, opened_at) "
        "VALUES ('c','SYM','N','eth','first_signal','{}',1.0,100.0,100.0,"
        "20.0,10.0,1.2,0.9,'open','2026-08-01T00:00:00')"
    )
    await db._conn.commit()
    return db


async def _pt_id(db) -> int:
    cur = await db._conn.execute("SELECT id FROM paper_trades LIMIT 1")
    return (await cur.fetchone())[0]


async def _write(db, **overrides):
    kw = dict(
        client_order_id=VENUE_ORDER_ID_FORMS["binance"].render(_HASH),
        paper_trade_id=await _pt_id(db),
        coin_id="c",
        symbol="SYM",
        venue="binance",
        pair="SYMUSDT",
        signal_type="first_signal",
        size_usd="100",
        intent_hash=_HASH,
        mandate_mode="SUPERVISED_LIVE",
    )
    kw.update(overrides)
    return await record_pending_order(db, **kw)


async def test_a_correctly_bound_row_is_written(tmp_path):
    """Guard on the guard: the write path still works."""
    db = await _db(tmp_path)
    try:
        row_id = await _write(db)
        assert row_id > 0
        cur = await db._conn.execute(
            "SELECT intent_hash, mandate_mode FROM live_trades WHERE id=?", (row_id,)
        )
        assert tuple(await cur.fetchone()) == (_HASH, "SUPERVISED_LIVE")
    finally:
        await db.close()


async def test_an_intent_hash_with_a_legacy_id_is_refused(tmp_path):
    """The exact shape the adapter's `or` fallback produces: the caller set the
    hash, the adapter minted the uuid-derived id, and the two do not agree."""
    db = await _db(tmp_path)
    try:
        with pytest.raises(ValueError, match="is not the id"):
            await _write(db, client_order_id="gecko-1-abcd1234")
        cur = await db._conn.execute("SELECT COUNT(*) FROM live_trades")
        assert (await cur.fetchone())[0] == 0, "a broken row was written anyway"
    finally:
        await db.close()


async def test_an_intent_hash_on_a_venue_with_no_declared_form_is_refused(tmp_path):
    """A binding that could never be verified must not be claimed."""
    db = await _db(tmp_path)
    try:
        with pytest.raises(ValueError, match="declares no"):
            await _write(db, venue="coinbase", client_order_id="whatever")
    finally:
        await db.close()


async def test_the_kraken_form_is_accepted_on_the_kraken_venue(tmp_path):
    db = await _db(tmp_path)
    try:
        row_id = await _write(
            db,
            venue="kraken",
            pair="XBTUSD",
            client_order_id=VENUE_ORDER_ID_FORMS["kraken"].render(_HASH),
        )
        assert row_id > 0
    finally:
        await db.close()


async def test_a_row_with_no_intent_hash_is_unconstrained(tmp_path):
    """Legacy callers claim nothing and are checked for nothing. Absence of a
    claim is not a broken claim — the guard must not break every writer that has
    not been wired to intents yet."""
    db = await _db(tmp_path)
    try:
        row_id = await _write(
            db, intent_hash=None, mandate_mode=None, client_order_id="gecko-1-abcd1234"
        )
        assert row_id > 0
    finally:
        await db.close()
