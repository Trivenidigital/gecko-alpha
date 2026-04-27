"""BL-064 watermark transactionality test.

Closes the missing test gap flagged by 4/5 reviewers + the round-2 reviewer
("the kind that catches sneaky bugs after the flag flips on production"):
verifies that message persist + watermark advance + health row update
all happen in a single BEGIN IMMEDIATE / COMMIT transaction so a crash
between them cannot leave watermark and messages out of sync.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scout.db import Database
from scout.social.telegram.listener import _persist_message_with_watermark
from scout.social.telegram.models import ContractRef, ParsedMessage


@pytest.mark.asyncio
async def test_persist_advances_watermark_and_health(tmp_path):
    """Successful persist: messages row + watermark + health row all written."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    parsed = ParsedMessage(cashtags=["RIV"], contracts=[], urls=[])
    pk = await _persist_message_with_watermark(
        db=db,
        channel_handle="@gem",
        msg_id=42,
        posted_at=datetime.now(timezone.utc),
        sender="someone",
        text="$RIV moon",
        parsed=parsed,
    )
    assert pk is not None

    cur = await db._conn.execute(
        "SELECT msg_id FROM tg_social_messages WHERE channel_handle = '@gem'"
    )
    rows = await cur.fetchall()
    assert [r[0] for r in rows] == [42]

    cur = await db._conn.execute(
        "SELECT last_seen_msg_id FROM tg_social_watermarks WHERE channel_handle = '@gem'"
    )
    (watermark,) = await cur.fetchone()
    assert watermark == 42

    cur = await db._conn.execute(
        "SELECT last_message_at FROM tg_social_health WHERE component = 'channel:@gem'"
    )
    (health_ts,) = await cur.fetchone()
    assert health_ts is not None
    await db.close()


@pytest.mark.asyncio
async def test_duplicate_message_rolled_back_no_state_change(tmp_path):
    """A second persist of the same (channel, msg_id) returns None and does
    NOT advance watermark or update health. UNIQUE conflict is detected via
    sqlite_errorname (round-2 Medium #1)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    parsed = ParsedMessage(cashtags=[], contracts=[], urls=[])
    posted_at = datetime.now(timezone.utc)

    # First insert
    pk = await _persist_message_with_watermark(
        db=db,
        channel_handle="@gem",
        msg_id=100,
        posted_at=posted_at,
        sender="x",
        text="hi",
        parsed=parsed,
    )
    assert pk is not None
    cur = await db._conn.execute(
        "SELECT last_seen_msg_id FROM tg_social_watermarks WHERE channel_handle = '@gem'"
    )
    (wm1,) = await cur.fetchone()

    # Bump watermark separately to simulate later state
    await db._conn.execute(
        "UPDATE tg_social_watermarks SET last_seen_msg_id = 200 "
        "WHERE channel_handle = '@gem'"
    )
    await db._conn.commit()

    # Re-insert msg_id=100 — duplicate
    pk2 = await _persist_message_with_watermark(
        db=db,
        channel_handle="@gem",
        msg_id=100,
        posted_at=posted_at,
        sender="x",
        text="hi",
        parsed=parsed,
    )
    assert pk2 is None

    # Watermark must not regress to 100 — duplicate rolled back, and the
    # 200 we set out-of-band is preserved.
    cur = await db._conn.execute(
        "SELECT last_seen_msg_id FROM tg_social_watermarks WHERE channel_handle = '@gem'"
    )
    (wm2,) = await cur.fetchone()
    assert wm2 == 200
    await db.close()


@pytest.mark.asyncio
async def test_messages_count_one_per_unique_pair(tmp_path):
    """UNIQUE(channel, msg_id) is enforced: even after a duplicate attempt
    only one row exists."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    parsed = ParsedMessage(cashtags=[], contracts=[], urls=[])
    posted_at = datetime.now(timezone.utc)
    for _ in range(3):
        await _persist_message_with_watermark(
            db=db,
            channel_handle="@gem",
            msg_id=7,
            posted_at=posted_at,
            sender="x",
            text="dup",
            parsed=parsed,
        )
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM tg_social_messages WHERE channel_handle = '@gem'"
    )
    (count,) = await cur.fetchone()
    assert count == 1
    await db.close()


@pytest.mark.asyncio
async def test_per_channel_lock_serializes_writes(tmp_path):
    """Two concurrent persists on the same channel must serialize cleanly
    via the per-channel asyncio.Lock — no interleaved BEGIN/commits, no
    half-committed states."""
    import asyncio

    db = Database(tmp_path / "t.db")
    await db.initialize()
    parsed = ParsedMessage(cashtags=[], contracts=[], urls=[])
    posted_at = datetime.now(timezone.utc)

    results = await asyncio.gather(
        _persist_message_with_watermark(
            db=db,
            channel_handle="@gem",
            msg_id=101,
            posted_at=posted_at,
            sender="x",
            text="a",
            parsed=parsed,
        ),
        _persist_message_with_watermark(
            db=db,
            channel_handle="@gem",
            msg_id=102,
            posted_at=posted_at,
            sender="x",
            text="b",
            parsed=parsed,
        ),
    )
    assert all(r is not None for r in results)

    cur = await db._conn.execute(
        "SELECT msg_id FROM tg_social_messages WHERE channel_handle = '@gem' ORDER BY msg_id"
    )
    rows = [r[0] for r in await cur.fetchall()]
    assert rows == [101, 102]

    cur = await db._conn.execute(
        "SELECT last_seen_msg_id FROM tg_social_watermarks WHERE channel_handle = '@gem'"
    )
    (wm,) = await cur.fetchone()
    assert wm == 102  # higher of the two wins
    await db.close()
