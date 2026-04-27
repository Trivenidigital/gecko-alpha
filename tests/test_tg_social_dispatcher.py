"""BL-064 dispatcher gate tests — TG-only admission."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scout.db import Database
from scout.social.telegram.dispatcher import evaluate
from scout.social.telegram.models import ResolvedToken


def _resolved(
    *,
    ca: str | None = "0xabc",
    chain: str | None = "ethereum",
    safe: bool = True,
    completed: bool = True,
    mcap: float = 1_000_000.0,
) -> ResolvedToken:
    return ResolvedToken(
        token_id="tok",
        symbol="TOK",
        chain=chain,
        contract_address=ca,
        mcap=mcap,
        price_usd=1.0,
        volume_24h_usd=100.0,
        safety_pass=safe,
        safety_check_completed=completed,
    )


async def _add_channel(db: Database, handle: str = "@gem", trade_eligible: int = 1):
    await db._conn.execute(
        "INSERT OR REPLACE INTO tg_social_channels "
        "(channel_handle, display_name, trade_eligible, added_at) VALUES (?, ?, ?, ?)",
        (handle, "Gem", trade_eligible, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()


@pytest.mark.asyncio
async def test_gate_no_ca_blocks(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _add_channel(db)
    decision = await evaluate(
        db=db,
        settings=settings_factory(),
        token=_resolved(ca=None),
        channel_handle="@gem",
    )
    assert not decision.dispatch_trade
    assert decision.blocked_gate == "no_ca"
    await db.close()


@pytest.mark.asyncio
async def test_gate_safety_unknown_blocks(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _add_channel(db)
    decision = await evaluate(
        db=db,
        settings=settings_factory(),
        token=_resolved(safe=True, completed=False),  # 5xx case
        channel_handle="@gem",
    )
    assert not decision.dispatch_trade
    assert decision.blocked_gate == "safety_unknown"
    await db.close()


@pytest.mark.asyncio
async def test_gate_safety_failed_blocks(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _add_channel(db)
    decision = await evaluate(
        db=db,
        settings=settings_factory(),
        token=_resolved(safe=False, completed=True),  # honeypot/blacklist
        channel_handle="@gem",
    )
    assert not decision.dispatch_trade
    assert decision.blocked_gate == "safety_failed"
    await db.close()


@pytest.mark.asyncio
async def test_gate_channel_disabled_blocks(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _add_channel(db, trade_eligible=0)
    decision = await evaluate(
        db=db,
        settings=settings_factory(),
        token=_resolved(),
        channel_handle="@gem",
    )
    assert not decision.dispatch_trade
    assert decision.blocked_gate == "channel_disabled"
    await db.close()


@pytest.mark.asyncio
async def test_gate_unknown_channel_blocks(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    decision = await evaluate(
        db=db,
        settings=settings_factory(),
        token=_resolved(),
        channel_handle="@nope",
    )
    assert not decision.dispatch_trade
    assert decision.blocked_gate == "channel_disabled"
    await db.close()


@pytest.mark.asyncio
async def test_gate_quota_blocks(tmp_path, settings_factory):
    """5 open tg_social trades + new attempt → quota gate rejects."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _add_channel(db)
    # Insert 5 fake open paper_trades with signal_type='tg_social'
    now_iso = datetime.now(timezone.utc).isoformat()
    for i in range(5):
        await db._conn.execute(
            """INSERT INTO paper_trades
               (token_id, symbol, name, chain, signal_type, signal_data,
                entry_price, amount_usd, quantity, tp_pct, sl_pct,
                tp_price, sl_price, status, opened_at, signal_combo,
                remaining_qty, floor_armed, realized_pnl_usd)
               VALUES (?, ?, ?, ?, 'tg_social', '{}', 1.0, 300.0, 300.0,
                       20.0, 10.0, 1.2, 0.9, 'open', ?, 'tg_social',
                       300.0, 0, 0.0)""",
            (f"t{i}", f"T{i}", f"T{i}", "ethereum", now_iso),
        )
    await db._conn.commit()
    decision = await evaluate(
        db=db,
        settings=settings_factory(TG_SOCIAL_MAX_OPEN_TRADES=5),
        token=_resolved(),
        channel_handle="@gem",
    )
    assert not decision.dispatch_trade
    assert decision.blocked_gate == "tg_social_quota"
    await db.close()


@pytest.mark.asyncio
async def test_gate_dedup_open_blocks(tmp_path, settings_factory):
    """Same token has open paper_trade_id from tg_social → dedup_open."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _add_channel(db)
    # Open trade on token 'tok' from prior tg_social signal
    now_iso = datetime.now(timezone.utc).isoformat()
    cur = await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct,
            tp_price, sl_price, status, opened_at, signal_combo,
            remaining_qty, floor_armed, realized_pnl_usd)
           VALUES ('tok', 'TOK', 'TOK', 'ethereum', 'tg_social', '{}',
                   1.0, 300.0, 300.0, 20.0, 10.0, 1.2, 0.9, 'open', ?,
                   'tg_social', 300.0, 0, 0.0)""",
        (now_iso,),
    )
    pt_id = cur.lastrowid
    # Persist matching tg_social_signals row
    await db._conn.execute(
        """INSERT INTO tg_social_messages
           (channel_handle, msg_id, posted_at, parsed_at)
           VALUES ('@gem', 1, ?, ?)""",
        (now_iso, now_iso),
    )
    await db._conn.execute(
        """INSERT INTO tg_social_signals
           (message_pk, token_id, symbol, resolution_state,
            source_channel_handle, paper_trade_id, created_at)
           VALUES (1, 'tok', 'TOK', 'RESOLVED', '@gem', ?, ?)""",
        (pt_id, now_iso),
    )
    await db._conn.commit()
    decision = await evaluate(
        db=db,
        settings=settings_factory(),
        token=_resolved(),
        channel_handle="@gem",
    )
    assert not decision.dispatch_trade
    assert decision.blocked_gate == "dedup_open"
    await db.close()


@pytest.mark.asyncio
async def test_gate_all_pass_dispatches(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _add_channel(db)
    decision = await evaluate(
        db=db,
        settings=settings_factory(),
        token=_resolved(),
        channel_handle="@gem",
    )
    assert decision.dispatch_trade
    assert decision.blocked_gate is None
    await db.close()
