"""Tests for fix/bl-064-listener-resilience.

Three regressions discovered in production on 2026-04-28 after the BL-064
listener silently sat dead for ~24h:

1. **Bad channel handle kills entire listener.** `iter_messages` raises
   `UsernameInvalidError` (or `UsernameNotOccupiedError`) for handles that
   don't exist on Telegram. The original `_catchup_channel` only caught
   `ChannelPrivateError` / `ChatAdminRequiredError`, so the unhandled
   exception escaped `run_listener`, hit `asyncio.gather(return_exceptions=
   True)` in main.py, and was silently swallowed. The first bad handle in
   the catchup loop killed the whole listener task before reaching channel
   #2. Fix: extend the except-tuple to also catch `UsernameInvalidError`,
   `UsernameNotOccupiedError`, and bare `ValueError` (Telethon emits these
   for "no entity found" depending on version).

2. **listener_state lies after crash.** `_set_listener_state(db, "running")`
   was called BEFORE the catchup loop, then any unhandled exception
   propagated out of `run_listener` without ever flipping state to a
   terminal value. tg_social_health stayed `running` forever. Fix: wrap
   the entire post-auth body in a top-level try/except that flips state
   to `crashed` and emits a Telegram alert before re-raising.

3. **OperationalError "cannot start a transaction within a transaction".**
   The previous `_persist_message_with_watermark` used a per-channel
   `asyncio.Lock` instead of the project-wide `db._txn_lock`. Two channels'
   coroutines could interleave on the shared aiosqlite connection: A's
   `BEGIN IMMEDIATE` await yielded control, B grabbed the connection
   queue and tried its own BEGIN, sqlite refused. 66% of catchup persists
   DLQ'd until manual diagnosis. Fix: switch to `db._txn_lock` to match
   engine.py / kill_switch.py / metrics.py / etc.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.errors import UsernameInvalidError

from scout.db import Database
from scout.social.telegram.listener import (
    _catchup_channel,
    _persist_message_with_watermark,
    run_listener,
)
from scout.social.telegram.models import ParsedMessage


# ---------------------------------------------------------------------------
# Fix #1 — bad channel handle does not kill the catchup loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_username_invalid_marks_removed_at_and_continues(
    tmp_path, settings_factory
):
    """When iter_messages raises UsernameInvalidError, the catchup must:
      - mark removed_at on the channel row,
      - emit a warning log,
      - return without re-raising,
    so the outer for-channels loop in run_listener proceeds to the next handle.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(
        TG_SOCIAL_ENABLED=True,
        TG_SOCIAL_API_ID=12345,
        TG_SOCIAL_API_HASH="dummy_hash_for_test",
    )
    await db._conn.execute(
        "INSERT INTO tg_social_channels (channel_handle, display_name, "
        "trade_eligible, added_at) VALUES (?, ?, ?, ?)",
        ("@nonexistent", "Bogus", 1, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()

    client = MagicMock()
    # Telethon's iter_messages: invalid handles bubble up
    # UsernameInvalidError on first iteration.
    def _raising_iter(*args, **kwargs):
        async def _gen():
            raise UsernameInvalidError(request=None)
            yield  # pragma: no cover — never reached
        return _gen()
    client.iter_messages = _raising_iter
    http_session = MagicMock()
    http_session.post = MagicMock()  # Won't be invoked — bot creds blanked below

    # No raise from _catchup_channel — that's the contract.
    await _catchup_channel(
        client=client,
        db=db,
        settings=settings,
        engine=MagicMock(),
        http_session=http_session,
        telegram_bot_token="",
        telegram_chat_id="",
        channel_handle="@nonexistent",
    )

    cur = await db._conn.execute(
        "SELECT removed_at FROM tg_social_channels WHERE channel_handle = ?",
        ("@nonexistent",),
    )
    (removed_at,) = await cur.fetchone()
    assert removed_at is not None, "bad handle must be marked removed_at"
    await db.close()


@pytest.mark.asyncio
async def test_value_error_from_telethon_marks_removed(tmp_path, settings_factory):
    """Some Telethon paths surface 'no entity found' as bare ValueError —
    e.g. `ValueError: Cannot find any entity corresponding to "@foo"`.
    Catchup must treat ValueError the same as UsernameInvalidError."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(
        TG_SOCIAL_ENABLED=True,
        TG_SOCIAL_API_ID=12345,
        TG_SOCIAL_API_HASH="dummy_hash_for_test",
    )
    await db._conn.execute(
        "INSERT INTO tg_social_channels (channel_handle, display_name, "
        "trade_eligible, added_at) VALUES (?, ?, ?, ?)",
        ("@bogus2", "Bogus2", 0, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()

    client = MagicMock()
    def _raising_iter(*args, **kwargs):
        async def _gen():
            raise ValueError("Cannot find any entity corresponding to '@bogus2'")
            yield  # pragma: no cover
        return _gen()
    client.iter_messages = _raising_iter

    await _catchup_channel(
        client=client,
        db=db,
        settings=settings,
        engine=MagicMock(),
        http_session=MagicMock(),
        telegram_bot_token="",
        telegram_chat_id="",
        channel_handle="@bogus2",
    )

    cur = await db._conn.execute(
        "SELECT removed_at FROM tg_social_channels WHERE channel_handle = ?",
        ("@bogus2",),
    )
    (removed_at,) = await cur.fetchone()
    assert removed_at is not None
    await db.close()


# ---------------------------------------------------------------------------
# Fix #2 — listener crash flips listener_state to 'crashed'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listener_crash_flips_state_to_crashed(
    tmp_path, settings_factory, monkeypatch
):
    """Any unhandled exception inside the listener body (after auth has
    succeeded) must flip tg_social_health.listener_state from 'running' to
    'crashed' before re-raising. Without this, gather(return_exceptions=
    True) swallows the exception and health lies indefinitely."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(
        TG_SOCIAL_ENABLED=True,
        TG_SOCIAL_API_ID=12345,
        TG_SOCIAL_API_HASH="dummy_hash_for_test",
    )

    # Stub out the auth path — return a fake info dict so run_listener
    # proceeds past _set_listener_state(db, "running").
    fake_client = MagicMock()
    fake_client.run_until_disconnected = AsyncMock()

    async def _fake_build(_settings):
        return fake_client

    async def _fake_verify(_client):
        return {"id": 1, "username": "tester", "first_name": "Tester"}

    monkeypatch.setattr(
        "scout.social.telegram.listener.build_client", _fake_build
    )
    monkeypatch.setattr(
        "scout.social.telegram.listener.connect_and_verify", _fake_verify
    )

    # Force the inner body to explode with a non-FloodWait, non-AuthKey
    # error so we exercise the new top-level catch.
    async def _boom(**_):
        raise RuntimeError("synthetic listener crash")

    monkeypatch.setattr(
        "scout.social.telegram.listener._run_listener_body", _boom
    )

    http_session = MagicMock()
    http_session.post = MagicMock()  # not actually invoked by send_telegram
    # send_telegram swallows aiohttp errors; we expect best-effort alert
    # send to fail silently (no bot creds in test) — that's fine.

    with pytest.raises(RuntimeError, match="synthetic listener crash"):
        await run_listener(
            db=db, settings=settings, engine=MagicMock(), http_session=http_session
        )

    cur = await db._conn.execute(
        "SELECT listener_state, detail FROM tg_social_health WHERE component = 'listener'"
    )
    state, detail = await cur.fetchone()
    assert state == "crashed", f"expected crashed, got {state}"
    assert "RuntimeError" in (detail or ""), (
        f"crash detail must include exception class, got: {detail!r}"
    )
    await db.close()


# ---------------------------------------------------------------------------
# Fix #3 — concurrent persists across channels do not collide on BEGIN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_persists_dont_nest_transactions(tmp_path):
    """Reproduces the prod 'cannot start a transaction within a transaction'
    OperationalError. With the per-channel lock, two channels' coroutines
    could interleave on the shared aiosqlite connection: A starts BEGIN
    IMMEDIATE, awaits to issue INSERT, B grabs the queue and tries its own
    BEGIN — sqlite refuses. With db._txn_lock, B waits until A commits.

    This test fires N persist coroutines concurrently across N distinct
    channels. With the old per-channel lock + nested BEGIN, the test would
    flake with at least one OperationalError. With db._txn_lock all of
    them succeed.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()

    async def _persist_one(channel: str, msg_id: int):
        return await _persist_message_with_watermark(
            db=db,
            channel_handle=channel,
            msg_id=msg_id,
            posted_at=datetime.now(timezone.utc),
            sender="t",
            text="hello",
            parsed=ParsedMessage(cashtags=[], contracts=[], urls=[]),
        )

    coros = [_persist_one(f"@ch{i}", i + 100) for i in range(8)]
    pks = await asyncio.gather(*coros, return_exceptions=True)

    # Every coroutine must succeed — no OperationalError, no exception.
    for i, pk in enumerate(pks):
        assert not isinstance(pk, BaseException), (
            f"persist #{i} raised {type(pk).__name__}: {pk}"
        )
        assert pk is not None, f"persist #{i} returned None unexpectedly"

    # All 8 messages persisted
    cur = await db._conn.execute("SELECT COUNT(*) FROM tg_social_messages")
    (count,) = await cur.fetchone()
    assert count == 8

    # All 8 watermarks
    cur = await db._conn.execute("SELECT COUNT(*) FROM tg_social_watermarks")
    (count,) = await cur.fetchone()
    assert count == 8
    await db.close()
