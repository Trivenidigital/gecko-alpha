"""BL-NEW-LIVE-HYBRID M1 v2.1: Task 12 client_order_id idempotency tests."""

from __future__ import annotations

import pytest

from scout.db import Database
from scout.live.idempotency import (
    CLIENT_ORDER_ID_BINANCE_MAX_LEN,
    lookup_existing_order_id,
    make_client_order_id,
    record_pending_order,
)


def test_client_order_id_format():
    cid = make_client_order_id(1, "abcd1234-ef56-7890-abcd-ef0123456789")
    assert cid == "gecko-1-abcd1234"


def test_client_order_id_fits_binance_limit():
    """Long paper_trade_id + 8-char uuid stays under Binance 28-char limit."""
    cid = make_client_order_id(99999999, "abcd1234-ef56-7890-abcd-ef0123456789")
    assert len(cid) <= CLIENT_ORDER_ID_BINANCE_MAX_LEN


def test_client_order_id_deterministic():
    """Same inputs always produce the same client_order_id (idempotency
    contract at the construction layer)."""
    a = make_client_order_id(42, "abcd1234-ef56-7890-abcd-ef0123456789")
    b = make_client_order_id(42, "abcd1234-ef56-7890-abcd-ef0123456789")
    assert a == b


@pytest.mark.asyncio
async def test_live_trades_has_client_order_id_column(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(live_trades)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "client_order_id" in cols
    await db.close()


@pytest.mark.asyncio
async def test_unique_index_on_client_order_id(tmp_path):
    """UNIQUE INDEX (partial, ignores NULLs) backstops dedup."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='idx_live_trades_client_order_id'"
    )
    assert (await cur.fetchone()) is not None
    await db.close()


async def _seed_paper(db: Database, token_id: str = "tok") -> int:
    cur = await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_price, sl_price,
            status, opened_at)
           VALUES (?, 'X', 'x', 'ethereum', 'first_signal', '{}',
                   100, 50, 0.5, 120, 80, 'open',
                   '2026-05-08T00:00:00+00:00')""",
        (token_id,),
    )
    return cur.lastrowid


@pytest.mark.asyncio
async def test_record_pending_order_inserts_row(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    paper_id = await _seed_paper(db)
    cid = make_client_order_id(paper_id, "abcd1234-ef56-7890-abcd-ef0123456789")
    live_id = await record_pending_order(
        db,
        client_order_id=cid,
        paper_trade_id=paper_id,
        coin_id="x",
        symbol="X",
        venue="binance",
        pair="XUSDT",
        signal_type="first_signal",
        size_usd="50.0",
    )
    assert live_id > 0
    cur = await db._conn.execute(
        "SELECT client_order_id, status FROM live_trades WHERE id = ?",
        (live_id,),
    )
    row = await cur.fetchone()
    assert row[0] == cid
    assert row[1] == "open"
    await db.close()


@pytest.mark.asyncio
async def test_lookup_existing_order_id_returns_none_when_absent(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    result = await lookup_existing_order_id(db, "gecko-99-deadbeef")
    assert result is None
    await db.close()


@pytest.mark.asyncio
async def test_lookup_existing_order_id_returns_recorded(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    paper_id = await _seed_paper(db)
    cid = make_client_order_id(paper_id, "abcd1234-ef56-7890-abcd-ef0123456789")
    await record_pending_order(
        db,
        client_order_id=cid,
        paper_trade_id=paper_id,
        coin_id="x",
        symbol="X",
        venue="binance",
        pair="XUSDT",
        signal_type="first_signal",
        size_usd="50.0",
    )
    # Update entry_order_id to simulate a venue confirmation
    await db._conn.execute(
        "UPDATE live_trades SET entry_order_id = ? WHERE client_order_id = ?",
        ("BNX-12345", cid),
    )
    await db._conn.commit()
    result = await lookup_existing_order_id(db, cid)
    assert result == "BNX-12345"
    await db.close()


@pytest.mark.asyncio
async def test_unique_constraint_rejects_duplicate_client_order_id(tmp_path):
    """DB-layer dedup backstop: race-condition retry that bypasses the
    application-layer lookup will still fail at INSERT."""
    import sqlite3

    db = Database(tmp_path / "t.db")
    await db.initialize()
    paper_id = await _seed_paper(db)
    cid = make_client_order_id(paper_id, "abcd1234-ef56-7890-abcd-ef0123456789")
    await record_pending_order(
        db,
        client_order_id=cid,
        paper_trade_id=paper_id,
        coin_id="x",
        symbol="X",
        venue="binance",
        pair="XUSDT",
        signal_type="first_signal",
        size_usd="50.0",
    )
    paper_id_2 = await _seed_paper(db, token_id="tok2")
    with pytest.raises(sqlite3.IntegrityError):
        await record_pending_order(
            db,
            client_order_id=cid,  # duplicate!
            paper_trade_id=paper_id_2,
            coin_id="x",
            symbol="X",
            venue="binance",
            pair="XUSDT",
            signal_type="first_signal",
            size_usd="50.0",
        )
    await db.close()


@pytest.mark.asyncio
async def test_telemetry_migration_idempotent_on_rerun(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._migrate_live_client_order_id()  # second call
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_migrations WHERE name = ?",
        ("bl_live_client_order_id_v1",),
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()
