"""BL-064 channel-list hot-reload tests.

Pins the periodic re-load task added to scout/social/telegram/listener.py:
_run_listener_body alongside _silence_heartbeat. Mocks Telethon
TelegramClient — does NOT exercise real network I/O.

Tests gated by SKIP_AIOHTTP_TESTS=1 on Windows because the listener
module imports aiohttp + alerter (transitive scout.alerter chain triggers
Windows OpenSSL DLL conflict). Full suite runs on VPS Linux.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

_SKIP_AIOHTTP = pytest.mark.skipif(
    sys.platform == "win32" and os.environ.get("SKIP_AIOHTTP_TESTS") == "1",
    reason="Windows + SKIP_AIOHTTP_TESTS=1: skip aiohttp tests",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    from scout.db import Database
    d = Database(tmp_path / "t.db")
    await d.initialize()
    yield d
    await d.close()


def _make_mock_client():
    """Telethon client stub.

    Tracks `_handlers` as a list of (event_type, callback) tuples.
    `client.on(event_type)` returns a decorator; `remove_event_handler`
    matches by `is` (real Telethon uses `==` which falls back to `is`
    for named functions/lambdas — behavior identical for our test cases).
    """
    client = MagicMock()
    client._handlers = []

    def _on(event_type):
        def _decorator(func):
            client._handlers.append((event_type, func))
            return func

        return _decorator

    def _remove_event_handler(func):
        client._handlers = [
            (et, f) for et, f in client._handlers if f is not func
        ]

    client.on = _on
    client.remove_event_handler = _remove_event_handler
    return client


async def _seed_channels(db, *handles):
    """Insert active channel rows."""
    now = datetime.now(timezone.utc).isoformat()
    for h in handles:
        await db._conn.execute(
            "INSERT INTO tg_social_channels "
            "(channel_handle, display_name, trade_eligible, safety_required, added_at) "
            "VALUES (?, ?, 1, 1, ?)",
            (h, h.lstrip("@"), now),
        )
    await db._conn.commit()


async def _remove_channel(db, handle):
    """Soft-remove via removed_at stamp."""
    await db._conn.execute(
        "UPDATE tg_social_channels SET removed_at = ? WHERE channel_handle = ?",
        (datetime.now(timezone.utc).isoformat(), handle),
    )
    await db._conn.commit()


# ---------------------------------------------------------------------------
# T1-T3: _channel_reload_once (no-op / add / remove)
# ---------------------------------------------------------------------------


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_reload_no_op_when_channels_unchanged(db):
    """T1 — re-query returns same set → no log, no handler swap."""
    from scout.social.telegram.listener import _channel_reload_once
    await _seed_channels(db, "@a", "@b")
    client = _make_mock_client()
    initial_handler = lambda evt: None
    client._handlers.append(("NewMessage", initial_handler))
    in_memory = ["@a", "@b"]
    with capture_logs() as logs:
        new_list = await _channel_reload_once(
            db, client, in_memory, initial_handler
        )
    events = [e.get("event") for e in logs]
    assert "tg_social_channel_list_reloaded" not in events
    assert sorted(new_list) == ["@a", "@b"]
    # Handler list still contains initial handler
    assert any(h is initial_handler for _, h in client._handlers)


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_reload_detects_added_channel_and_re_binds(db):
    """T2 — new row in tg_social_channels → tg_social_channel_list_reloaded
    event with added=[@c], total=N+1; handler re-bound."""
    from scout.social.telegram.listener import _channel_reload_once
    await _seed_channels(db, "@a", "@b")
    client = _make_mock_client()
    initial_handler = lambda evt: None
    client._handlers.append(("NewMessage", initial_handler))
    in_memory = ["@a", "@b"]
    # Operator adds @c
    await _seed_channels(db, "@c")
    with capture_logs() as logs:
        new_list = await _channel_reload_once(
            db, client, in_memory, initial_handler
        )
    armed = [e for e in logs if e.get("event") == "tg_social_channel_list_reloaded"]
    assert len(armed) == 1
    assert "@c" in armed[0]["added"]
    assert armed[0]["removed"] == []
    assert armed[0]["total"] == 3
    assert sorted(new_list) == ["@a", "@b", "@c"]


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_reload_detects_removed_channel_and_re_binds(db):
    """T3 — removed_at stamped → tg_social_channel_list_reloaded event
    with removed=[@b], total=N-1."""
    from scout.social.telegram.listener import _channel_reload_once
    await _seed_channels(db, "@a", "@b", "@c")
    client = _make_mock_client()
    initial_handler = lambda evt: None
    client._handlers.append(("NewMessage", initial_handler))
    in_memory = ["@a", "@b", "@c"]
    await _remove_channel(db, "@b")
    with capture_logs() as logs:
        new_list = await _channel_reload_once(
            db, client, in_memory, initial_handler
        )
    armed = [e for e in logs if e.get("event") == "tg_social_channel_list_reloaded"]
    assert len(armed) == 1
    assert armed[0]["added"] == []
    assert "@b" in armed[0]["removed"]
    assert armed[0]["total"] == 2
    assert sorted(new_list) == ["@a", "@c"]


# ---------------------------------------------------------------------------
# T4: heartbeat disable path (interval=0)
# ---------------------------------------------------------------------------


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_reload_disabled_when_interval_is_zero(db, settings_factory):
    """T4 (rewritten v2) — settings.TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC=0
    → heartbeat returns immediately + emits tg_social_channel_reload_disabled.
    This now WORKS because the validator was amended to allow 0 as the
    explicit opt-out (PR-review adv-M1)."""
    from scout.social.telegram.listener import _make_channel_reload_heartbeat
    settings = settings_factory(TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC=0)
    client = _make_mock_client()
    initial_handler = lambda evt: None
    channels_holder = {"channels": ["@a", "@b"]}

    heartbeat = _make_channel_reload_heartbeat(
        db, client, settings, channels_holder, initial_handler
    )
    with capture_logs() as logs:
        # Heartbeat returns immediately when interval=0 (no infinite loop)
        await asyncio.wait_for(heartbeat(), timeout=2.0)
    events = [e.get("event") for e in logs]
    assert "tg_social_channel_reload_disabled" in events


# ---------------------------------------------------------------------------
# T5: error handling (DB raises → tg_social_channel_reload_error)
# ---------------------------------------------------------------------------


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_reload_error_is_caught_in_heartbeat_not_propagated(
    db, settings_factory, monkeypatch
):
    """T5 (rewritten v2 per arch-S1) — DB raises during reload query →
    heartbeat catches via tg_social_channel_reload_error log, does NOT
    crash the listener loop. Tests the heartbeat-level catch (not the
    once-level — `_channel_reload_once` propagates by design)."""
    from scout.social.telegram.listener import _make_channel_reload_heartbeat
    # 60s interval but we'll use a fast monkeypatch on asyncio.sleep
    settings = settings_factory(TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC=60)
    client = _make_mock_client()
    initial_handler = lambda evt: None
    channels_holder = {"channels": ["@a"]}

    # Monkeypatch DB execute to raise on the channel-reload SELECT
    real_execute = db._conn.execute

    async def _broken_execute(sql, params=()):
        if "tg_social_channels" in sql:
            raise RuntimeError("simulated DB lost")
        return await real_execute(sql, params)

    monkeypatch.setattr(db._conn, "execute", _broken_execute)

    # Replace asyncio.sleep so the heartbeat ticks fast.
    # We want to run ONE iteration then cancel — emulate this by
    # monkeypatching sleep to immediately raise CancelledError on 2nd call.
    sleep_count = {"n": 0}
    real_sleep = asyncio.sleep

    async def _fast_sleep(_seconds):
        sleep_count["n"] += 1
        if sleep_count["n"] >= 2:
            raise asyncio.CancelledError()
        await real_sleep(0)  # quick yield

    monkeypatch.setattr(
        "scout.social.telegram.listener.asyncio.sleep", _fast_sleep
    )

    heartbeat = _make_channel_reload_heartbeat(
        db, client, settings, channels_holder, initial_handler
    )

    with capture_logs() as logs:
        with pytest.raises(asyncio.CancelledError):
            await heartbeat()
    events = [e.get("event") for e in logs]
    assert "tg_social_channel_reload_error" in events, (
        f"heartbeat must catch and log error; got {events}"
    )


# ---------------------------------------------------------------------------
# arch-S2 — handler swap rollback on client.on(...) failure
# ---------------------------------------------------------------------------


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_reload_swap_failure_rolls_back_to_old_handler(db):
    """arch-S2 PR-review fix — if client.on(new_list) raises after
    remove_event_handler, the OLD handler must be re-attached so the
    listener doesn't go silent."""
    from scout.social.telegram.listener import _channel_reload_once
    await _seed_channels(db, "@a", "@b")
    client = _make_mock_client()
    initial_handler = lambda evt: None
    client._handlers.append(("NewMessage", initial_handler))

    # Monkey: client.on raises on the FIRST call (new-list path);
    # second call (rollback path) succeeds.
    real_on = client.on
    call_count = {"n": 0}

    def _broken_on(event_type):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated entity-resolution failure")
        return real_on(event_type)

    client.on = _broken_on
    in_memory = ["@a", "@b"]
    await _seed_channels(db, "@c")  # trigger a swap

    with capture_logs() as logs:
        with pytest.raises(RuntimeError):
            await _channel_reload_once(
                db, client, in_memory, initial_handler
            )
    events = [e.get("event") for e in logs]
    assert "tg_social_channel_reload_swap_failed" in events
    # Old handler re-attached — listener still has a handler
    assert any(h is initial_handler for _, h in client._handlers)
