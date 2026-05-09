"""BL-NEW-LIVE-HYBRID M1 v2.1: Telegram approval gateway tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.live.telegram_approval import (
    has_active_override,
    set_operator_override,
)


@pytest.mark.asyncio
async def test_set_operator_override_writes_row(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    rowid = await set_operator_override(
        db,
        override_type="auto_approve",
        venue="binance",
        set_by="LowCapHunt",
    )
    assert rowid > 0
    cur = await db._conn.execute(
        "SELECT override_type, venue, set_by FROM live_operator_overrides "
        "WHERE id = ?",
        (rowid,),
    )
    row = await cur.fetchone()
    assert tuple(row) == ("auto_approve", "binance", "LowCapHunt")
    await db.close()


@pytest.mark.asyncio
async def test_has_active_override_returns_true_when_unexpired(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await set_operator_override(db, override_type="approval_required", venue="binance")
    active = await has_active_override(
        db, override_type="approval_required", venue="binance"
    )
    assert active is True
    await db.close()


@pytest.mark.asyncio
async def test_has_active_override_returns_false_when_expired(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO live_operator_overrides
           (override_type, venue, set_at, expires_at)
           VALUES ('approval_required', 'binance', ?, ?)""",
        (now_iso, past),
    )
    await db._conn.commit()
    active = await has_active_override(
        db, override_type="approval_required", venue="binance"
    )
    assert active is False
    await db.close()


@pytest.mark.asyncio
async def test_has_active_override_matches_null_venue_globally(tmp_path):
    """venue IS NULL means 'all venues'."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await set_operator_override(db, override_type="approval_required", venue=None)
    active = await has_active_override(
        db, override_type="approval_required", venue="binance"
    )
    assert active is True
    await db.close()


@pytest.mark.asyncio
async def test_request_operator_approval_returns_false_on_timeout(tmp_path):
    """No approval row written → polls until timeout → returns False."""
    from scout.live.telegram_approval import request_operator_approval

    db = Database(tmp_path / "t.db")
    await db.initialize()

    class _PaperTrade:
        id = 1
        signal_type = "first_signal"

    class _Candidate:
        venue = "binance"

    approved = await request_operator_approval(
        db,
        paper_trade=_PaperTrade(),
        candidate=_Candidate(),
        gate="new_venue_gate",
        timeout_sec=0.1,  # tight to keep test fast
    )
    assert approved is False
    await db.close()


@pytest.mark.asyncio
async def test_request_operator_approval_returns_true_when_pre_approved(tmp_path):
    """auto_approve override already set → returns True on first poll."""
    from scout.live.telegram_approval import request_operator_approval

    db = Database(tmp_path / "t.db")
    await db.initialize()
    await set_operator_override(db, override_type="auto_approve", venue="binance")

    class _PaperTrade:
        id = 1
        signal_type = "first_signal"

    class _Candidate:
        venue = "binance"

    approved = await request_operator_approval(
        db,
        paper_trade=_PaperTrade(),
        candidate=_Candidate(),
        gate="new_venue_gate",
        timeout_sec=5.0,
    )
    assert approved is True
    await db.close()
