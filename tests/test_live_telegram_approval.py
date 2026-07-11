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


@pytest.mark.asyncio
async def test_request_operator_approval_sends_telegram_alert(tmp_path, monkeypatch):
    """LIVE-07 (S2-1): request_operator_approval must actually notify the
    operator via Telegram — not just poll silently. Asserts the send fires
    with parse_mode=None + source='live_approval' and a body naming the
    trade/venue/gate. A fake scout.alerter is injected via sys.modules so the
    test never imports real aiohttp (Windows-runnable)."""
    import sys
    import types

    from scout.live.telegram_approval import request_operator_approval

    sent: dict = {}

    async def _fake_send(
        text, session, settings, *, parse_mode="Markdown", source="unattributed", **kw
    ):
        sent.update(text=text, parse_mode=parse_mode, source=source)

    fake_alerter = types.ModuleType("scout.alerter")
    fake_alerter.send_telegram_message = _fake_send
    monkeypatch.setitem(sys.modules, "scout.alerter", fake_alerter)

    db = Database(tmp_path / "t.db")
    await db.initialize()

    class _PaperTrade:
        id = 7
        signal_type = "first_signal"

    class _Candidate:
        venue = "binance"

    # No override seeded → polls to timeout → False, but the alert must have
    # been dispatched exactly once regardless of the poll outcome.
    approved = await request_operator_approval(
        db,
        paper_trade=_PaperTrade(),
        candidate=_Candidate(),
        gate="new_venue_gate",
        timeout_sec=0.05,
        session=object(),
        settings=object(),
    )
    assert approved is False
    assert sent, "request_operator_approval sent no Telegram alert"
    assert sent["parse_mode"] is None, "must be plain text (underscore-safe)"
    assert sent["source"] == "live_approval"
    assert "binance" in sent["text"]
    assert "new_venue_gate" in sent["text"]
    assert "#7" in sent["text"]
    await db.close()


@pytest.mark.asyncio
async def test_request_operator_approval_skips_send_without_session(tmp_path):
    """Backward-compat: with no session/settings injected the function
    degrades to log-and-poll (no crash, no send) — preserving the prior
    contract for callers that cannot supply an aiohttp session."""
    from scout.live.telegram_approval import request_operator_approval

    db = Database(tmp_path / "t.db")
    await db.initialize()

    class _PaperTrade:
        id = 3
        signal_type = "first_signal"

    class _Candidate:
        venue = "binance"

    approved = await request_operator_approval(
        db,
        paper_trade=_PaperTrade(),
        candidate=_Candidate(),
        gate="new_venue_gate",
        timeout_sec=0.05,
    )
    assert approved is False
    await db.close()
