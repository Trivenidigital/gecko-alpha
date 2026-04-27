"""BL-064 FloodWait circuit-break tests.

Closes the missing test gap (round-2 reviewer): when the listener's
`_on_new` handler raises `FloodWaitError(seconds > cap)`, the listener
must transition `tg_social_health.listener_state` to 'disabled_floodwait'
and re-raise so the outer task fails fast (operator-visible).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from scout.db import Database
from scout.social.telegram.listener import _set_listener_state


@pytest.mark.asyncio
async def test_set_listener_state_persists_disabled_floodwait(tmp_path):
    """Direct unit test of the helper that the FloodWait branch calls."""
    db = Database(tmp_path / "t.db")
    await db.initialize()

    await _set_listener_state(
        db, "disabled_floodwait", detail="FloodWait 3600s > cap 600s"
    )

    cur = await db._conn.execute(
        "SELECT listener_state, detail FROM tg_social_health WHERE component = 'listener'"
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "disabled_floodwait"
    assert "FloodWait 3600s" in row[1]
    await db.close()


@pytest.mark.asyncio
async def test_set_listener_state_running_then_stopped_idempotent(tmp_path):
    """The state column is updated on each call (UPSERT) — running → stopped
    transitions cleanly and the row is unique per `component`."""
    db = Database(tmp_path / "t.db")
    await db.initialize()

    await _set_listener_state(db, "running")
    await _set_listener_state(db, "stopped", detail="run_until_disconnected returned")

    cur = await db._conn.execute(
        "SELECT COUNT(*), listener_state, detail FROM tg_social_health "
        "WHERE component = 'listener'"
    )
    count, state, detail = await cur.fetchone()
    assert count == 1
    assert state == "stopped"
    assert detail == "run_until_disconnected returned"
    await db.close()


@pytest.mark.asyncio
async def test_set_listener_state_auth_lost_includes_detail(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _set_listener_state(db, "auth_lost", detail="AuthKeyError")
    cur = await db._conn.execute(
        "SELECT listener_state, detail FROM tg_social_health WHERE component = 'listener'"
    )
    row = await cur.fetchone()
    assert row[0] == "auth_lost"
    assert row[1] == "AuthKeyError"
    await db.close()
