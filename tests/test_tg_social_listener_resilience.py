"""Regression tests for tg_social listener crash-resilience: bad-handle
handling, top-level crash watchdog, shared-connection transaction locking,
and end-to-end ordering of catchup → handler-attach → run_until_disconnected.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.errors import (
    AuthKeyError,
    FloodWaitError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)

from scout.db import Database
from scout.social.telegram.listener import (
    _catchup_channel,
    _crash_detail,
    _is_telethon_entity_resolution_error,
    _persist_message_with_watermark,
    run_listener,
)
from scout.social.telegram.models import ParsedMessage


def _make_iter_raising(exc: BaseException):
    """Build a stub for client.iter_messages that raises `exc` on first iteration."""

    def _stub(*args, **kwargs):
        async def _gen():
            raise exc
            yield  # pragma: no cover

        return _gen()

    return _stub


# ---------------------------------------------------------------------------
# Fix #1 — bad channel handle does not kill the catchup loop
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: UsernameInvalidError(request=None),
        lambda: UsernameNotOccupiedError(request=None),
        lambda: ValueError("Cannot find any entity corresponding to '@bogus'"),
        lambda: ValueError("No user has username @bogus"),
    ],
    ids=[
        "UsernameInvalidError",
        "UsernameNotOccupiedError",
        "ValueError-cannot-find",
        "ValueError-no-user-has",
    ],
)
@pytest.mark.asyncio
async def test_bad_handle_marks_removed_at_and_alerts(
    tmp_path, settings_factory, exc_factory
):
    """Each entity-resolution error must mark removed_at, fire the operator
    alert, and return WITHOUT re-raising so the outer catchup loop
    proceeds to the next channel."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(
        TG_SOCIAL_ENABLED=True,
        TG_SOCIAL_API_ID=12345,
        TG_SOCIAL_API_HASH="dummy",
    )
    await db._conn.execute(
        "INSERT INTO tg_social_channels (channel_handle, display_name, "
        "trade_eligible, added_at) VALUES (?, ?, ?, ?)",
        ("@bogus", "Bogus", 1, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()

    client = MagicMock()
    client.iter_messages = _make_iter_raising(exc_factory())
    http_session = MagicMock()
    http_session.post = MagicMock()
    bot_token = "fake-token"
    chat_id = "fake-chat"

    await _catchup_channel(
        client=client,
        db=db,
        settings=settings,
        engine=MagicMock(),
        http_session=http_session,
        telegram_bot_token=bot_token,
        telegram_chat_id=chat_id,
        channel_handle="@bogus",
    )

    cur = await db._conn.execute(
        "SELECT removed_at FROM tg_social_channels WHERE channel_handle = ?",
        ("@bogus",),
    )
    (removed_at,) = await cur.fetchone()
    assert removed_at is not None, "bad handle must be marked removed_at"
    # Operator-visible alert must have been attempted
    assert (
        http_session.post.call_count >= 1
    ), "send_telegram must be invoked with the 'lost access' message"
    await db.close()


@pytest.mark.asyncio
async def test_unrelated_value_error_propagates(tmp_path, settings_factory):
    """A ValueError NOT matching Telethon's entity-resolution prefixes must
    propagate. Otherwise we'd silently mark a perfectly good channel as
    removed because of an unrelated downstream bug (Pydantic validation,
    parameter coercion, etc.)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(
        TG_SOCIAL_ENABLED=True,
        TG_SOCIAL_API_ID=12345,
        TG_SOCIAL_API_HASH="dummy",
    )
    await db._conn.execute(
        "INSERT INTO tg_social_channels (channel_handle, display_name, "
        "trade_eligible, added_at) VALUES (?, ?, ?, ?)",
        ("@good", "Good", 0, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()

    client = MagicMock()
    client.iter_messages = _make_iter_raising(
        ValueError("invalid literal for int() with base 10: 'oops'")
    )

    with pytest.raises(ValueError, match="invalid literal"):
        await _catchup_channel(
            client=client,
            db=db,
            settings=settings,
            engine=MagicMock(),
            http_session=MagicMock(),
            telegram_bot_token="",
            telegram_chat_id="",
            channel_handle="@good",
        )

    cur = await db._conn.execute(
        "SELECT removed_at FROM tg_social_channels WHERE channel_handle = ?",
        ("@good",),
    )
    (removed_at,) = await cur.fetchone()
    assert (
        removed_at is None
    ), "unrelated ValueError must NOT cause a channel to be marked removed"
    await db.close()


@pytest.mark.asyncio
async def test_floodwait_in_catchup_propagates_not_swallowed(
    tmp_path, settings_factory
):
    """FloodWaitError must NOT be caught by the new ValueError clause —
    sanity-check ordering after the refactor. If a future Telethon change
    made FloodWaitError inherit from ValueError, the circuit-break would
    silently break."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(
        TG_SOCIAL_ENABLED=True,
        TG_SOCIAL_API_ID=12345,
        TG_SOCIAL_API_HASH="dummy",
    )
    await db._conn.execute(
        "INSERT INTO tg_social_channels (channel_handle, display_name, "
        "trade_eligible, added_at) VALUES (?, ?, ?, ?)",
        ("@busy", "Busy", 0, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()

    client = MagicMock()
    client.iter_messages = _make_iter_raising(FloodWaitError(request=None, capture=42))

    with pytest.raises(FloodWaitError):
        await _catchup_channel(
            client=client,
            db=db,
            settings=settings,
            engine=MagicMock(),
            http_session=MagicMock(),
            telegram_bot_token="",
            telegram_chat_id="",
            channel_handle="@busy",
        )
    await db.close()


def test_is_telethon_entity_resolution_error_predicate():
    """Predicate must accept Telethon's known prefixes and reject everything
    else. Locks the contract that backs the catchup ValueError branch."""
    assert _is_telethon_entity_resolution_error(
        ValueError("Cannot find any entity corresponding to '@x'")
    )
    assert _is_telethon_entity_resolution_error(ValueError("No user has username @x"))
    assert _is_telethon_entity_resolution_error(
        ValueError("Could not find the input entity for...")
    )
    assert not _is_telethon_entity_resolution_error(
        ValueError("invalid literal for int()")
    )
    assert not _is_telethon_entity_resolution_error(
        ValueError("expected str, got NoneType")
    )


# ---------------------------------------------------------------------------
# Fix #2 — listener crash flips listener_state to 'crashed'
# ---------------------------------------------------------------------------


def _stub_auth(monkeypatch):
    """Patch build_client + connect_and_verify so run_listener can run a
    test body without needing a real Telethon session."""
    fake_client = MagicMock()
    fake_client.run_until_disconnected = AsyncMock()

    async def _fake_build(_settings):
        return fake_client

    async def _fake_verify(_client):
        return {"id": 1, "username": "tester", "first_name": "Tester"}

    monkeypatch.setattr("scout.social.telegram.listener.build_client", _fake_build)
    monkeypatch.setattr(
        "scout.social.telegram.listener.connect_and_verify", _fake_verify
    )
    return fake_client


@pytest.mark.asyncio
async def test_listener_crash_flips_state_and_alerts(
    tmp_path, settings_factory, monkeypatch
):
    """Unhandled exception in the body must:
    (a) flip listener_state to 'crashed',
    (b) include the exception class in detail,
    (c) attempt the operator Telegram alert,
    (d) re-raise so gather() can observe the failure (even if it
        ultimately suppresses via return_exceptions=True).
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(
        TG_SOCIAL_ENABLED=True,
        TG_SOCIAL_API_ID=12345,
        TG_SOCIAL_API_HASH="dummy",
        TELEGRAM_BOT_TOKEN="real-token",
        TELEGRAM_CHAT_ID="real-chat",
    )
    _stub_auth(monkeypatch)

    async def _boom(**_):
        raise RuntimeError("synthetic listener crash")

    monkeypatch.setattr("scout.social.telegram.listener._run_listener_body", _boom)

    http_session = MagicMock()
    http_session.post = MagicMock()

    with pytest.raises(RuntimeError, match="synthetic listener crash"):
        await run_listener(
            db=db, settings=settings, engine=MagicMock(), http_session=http_session
        )

    cur = await db._conn.execute(
        "SELECT listener_state, detail FROM tg_social_health "
        "WHERE component = 'listener'"
    )
    state, detail = await cur.fetchone()
    assert state == "crashed"
    assert "RuntimeError" in (detail or "")
    assert (
        http_session.post.call_count >= 1
    ), "operator Telegram alert must be attempted on crash"
    await db.close()


@pytest.mark.asyncio
async def test_crash_state_flip_failure_preserves_original_exception(
    tmp_path, settings_factory, monkeypatch
):
    """If `_set_listener_state(db, 'crashed')` itself raises (e.g., DB locked
    or closed during shutdown), the ORIGINAL exception must still propagate.
    Otherwise the silent-failure mode this PR is fixing returns: gather()
    sees the secondary DB error, the real crash is lost, and the listener
    dies invisibly."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(
        TG_SOCIAL_ENABLED=True,
        TG_SOCIAL_API_ID=12345,
        TG_SOCIAL_API_HASH="dummy",
    )
    _stub_auth(monkeypatch)

    async def _boom(**_):
        raise RuntimeError("primary crash")

    monkeypatch.setattr("scout.social.telegram.listener._run_listener_body", _boom)

    set_state_calls: list[str] = []
    real_set = None  # not used; we replace entirely

    async def _failing_set_state(_db, state, detail=None):
        set_state_calls.append(state)
        if state == "crashed":
            raise RuntimeError("DB locked during state flip")

    monkeypatch.setattr(
        "scout.social.telegram.listener._set_listener_state", _failing_set_state
    )

    # The ORIGINAL RuntimeError("primary crash") must propagate, NOT the
    # secondary RuntimeError("DB locked during state flip").
    with pytest.raises(RuntimeError, match="primary crash"):
        await run_listener(
            db=db,
            settings=settings,
            engine=MagicMock(),
            http_session=MagicMock(),
        )
    assert (
        "crashed" in set_state_calls
    ), "crash watchdog must attempt the state flip even if it fails"
    await db.close()


@pytest.mark.asyncio
async def test_authkey_error_preserves_auth_lost_state(
    tmp_path, settings_factory, monkeypatch
):
    """AuthKeyError thrown from the body has already stamped 'auth_lost' on
    the inner path. The outer crash watchdog must NOT overwrite that with
    'crashed' — auth_lost is the more-specific, more-actionable state
    (it tells the operator to re-bootstrap the session)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(
        TG_SOCIAL_ENABLED=True,
        TG_SOCIAL_API_ID=12345,
        TG_SOCIAL_API_HASH="dummy",
    )
    _stub_auth(monkeypatch)

    async def _body(**_):
        # Simulate the inner auth_lost path: stamp state + raise.
        from scout.social.telegram.listener import _set_listener_state

        await _set_listener_state(db, "auth_lost", detail="AuthKeyError")
        raise AuthKeyError(request=None, message="auth key revoked")

    monkeypatch.setattr("scout.social.telegram.listener._run_listener_body", _body)

    with pytest.raises(AuthKeyError):
        await run_listener(
            db=db,
            settings=settings,
            engine=MagicMock(),
            http_session=MagicMock(),
        )

    cur = await db._conn.execute(
        "SELECT listener_state FROM tg_social_health WHERE component = 'listener'"
    )
    (state,) = await cur.fetchone()
    assert (
        state == "auth_lost"
    ), f"AuthKeyError must keep state at 'auth_lost', got {state!r}"
    await db.close()


# ---------------------------------------------------------------------------
# Fix #3 — concurrent persists across channels do not collide on BEGIN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_persists_use_shared_txn_lock(tmp_path, monkeypatch):
    """Reproduces the prod 'cannot start a transaction within a transaction'
    OperationalError. The bug only triggers when two persist coroutines
    interleave on the shared aiosqlite connection. This test forces
    interleaving by injecting `await asyncio.sleep(0)` immediately after
    each persist's first INSERT (which is what triggers aiosqlite's
    implicit auto-BEGIN). Without `db._txn_lock` serializing the entire
    INSERT…COMMIT block, two coroutines could both have implicit
    transactions open against the same connection — the trailing
    coroutine's commit would either flush the leading coroutine's writes
    too early, or the conflict would surface as an OperationalError on
    the next execute.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()

    real_execute = db._conn.execute
    saw_first_insert_count = 0

    async def _yielding_execute(sql, *args, **kwargs):
        nonlocal saw_first_insert_count
        result = await real_execute(sql, *args, **kwargs)
        # Hook just the first INSERT in each persist call (messages table)
        # to force scheduler interleaving at exactly the implicit-BEGIN
        # boundary. Without _txn_lock, two coroutines would simultaneously
        # have writes pending on the connection.
        if isinstance(sql, str) and "INSERT INTO tg_social_messages" in sql:
            saw_first_insert_count += 1
            await asyncio.sleep(0)
        return result

    monkeypatch.setattr(db._conn, "execute", _yielding_execute)

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

    for i, pk in enumerate(pks):
        assert not isinstance(
            pk, BaseException
        ), f"persist #{i} raised {type(pk).__name__}: {pk}"
        assert pk is not None

    cur = await db._conn.execute("SELECT COUNT(*) FROM tg_social_messages")
    (count,) = await cur.fetchone()
    assert count == 8
    cur = await db._conn.execute("SELECT COUNT(*) FROM tg_social_watermarks")
    (count,) = await cur.fetchone()
    assert count == 8
    assert (
        saw_first_insert_count >= 8
    ), "test must actually hit the messages-table INSERT at least 8 times"
    await db.close()


# ---------------------------------------------------------------------------
# Body flow ordering — proves catchup runs BEFORE handler attaches.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listener_body_attaches_handler_after_catchup(
    tmp_path, settings_factory, monkeypatch
):
    """Locks the ordering invariant: catchup pass must complete BEFORE the
    NewMessage handler is attached. Otherwise a live event firing during
    catchup could double-process a message OR fire before its watermark
    is initialized.

    Records the order of: catchup() called, client.on() called.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(
        TG_SOCIAL_ENABLED=True,
        TG_SOCIAL_API_ID=12345,
        TG_SOCIAL_API_HASH="dummy",
    )
    await db._conn.execute(
        "INSERT INTO tg_social_channels (channel_handle, display_name, "
        "trade_eligible, added_at) VALUES (?, ?, ?, ?)",
        ("@ordered", "Ordered", 1, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()

    call_order: list[str] = []

    async def _fake_catchup(**_):
        call_order.append("catchup")

    def _fake_client_on(*args, **kwargs):
        call_order.append("client.on")

        # client.on returns a decorator; we return a no-op
        def _decorator(f):
            return f

        return _decorator

    fake_client = MagicMock()
    # run_until_disconnected returns immediately so the listener exits cleanly
    fake_client.run_until_disconnected = AsyncMock()
    fake_client.on = _fake_client_on

    async def _fake_build(_settings):
        return fake_client

    async def _fake_verify(_client):
        return {"id": 1, "username": "tester", "first_name": "Tester"}

    monkeypatch.setattr("scout.social.telegram.listener.build_client", _fake_build)
    monkeypatch.setattr(
        "scout.social.telegram.listener.connect_and_verify", _fake_verify
    )
    monkeypatch.setattr(
        "scout.social.telegram.listener._catchup_channel", _fake_catchup
    )

    await run_listener(
        db=db,
        settings=settings,
        engine=MagicMock(),
        http_session=MagicMock(),
    )

    assert call_order == [
        "catchup",
        "client.on",
    ], f"catchup must precede handler attach, got: {call_order}"

    # And on clean exit, listener_state should be 'stopped' (not 'crashed')
    cur = await db._conn.execute(
        "SELECT listener_state FROM tg_social_health WHERE component = 'listener'"
    )
    (state,) = await cur.fetchone()
    assert state == "stopped"
    await db.close()


def test_crash_detail_helper():
    """One-line truncation invariant: format is `{ClassName}: {msg[:limit]}`."""
    short = _crash_detail(ValueError("boom"))
    assert short == "ValueError: boom"
    longmsg = "x" * 500
    truncated = _crash_detail(RuntimeError(longmsg), limit=50)
    assert truncated.startswith("RuntimeError: ")
    assert len(truncated) == len("RuntimeError: ") + 50
