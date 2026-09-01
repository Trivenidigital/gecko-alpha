"""Catchup-replay age guard for tg_social messages.

`_catchup_channel` replays historical messages through `handle_new_message`,
the same entry point live messages use, and nothing downstream inspects
message age. Before TG_SOCIAL_MAX_MESSAGE_AGE_MIN existed, restoring
TG_SOCIAL_CATCHUP_LIMIT to a non-zero value would therefore resolve, alert
and (on trade_eligible channels) paper-trade months-old calls as if they had
just fired.

The contract under test:
  * stale message  -> persisted, watermark advanced, NOT resolved
  * fresh message  -> resolved as normal
  * threshold 0    -> guard disabled entirely (pre-guard behaviour)
  * bad timestamp  -> fail OPEN (never drop a live message on an age error)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scout.db import Database
from scout.social.telegram import listener as listener_mod
from scout.social.telegram.listener import _message_age_minutes, handle_new_message

CHANNEL = "@ageguard"
# A contract-bearing body, so parse_message() yields a non-empty ParsedMessage
# and execution reaches the persist -> guard -> resolve path. A body with no
# signal would short-circuit at `parsed.is_empty` and prove nothing.
TEXT = "buy 0x1111111111111111111111111111111111111111 now"


def _event(msg_id: int, posted_at) -> SimpleNamespace:
    return SimpleNamespace(
        message=SimpleNamespace(id=msg_id, message=TEXT, text=TEXT, date=posted_at),
        chat=SimpleNamespace(username=CHANNEL.lstrip("@"), id=-100123),
        sender=SimpleNamespace(username="curator", first_name="Curator"),
    )


async def _run(db, settings, event) -> None:
    await handle_new_message(
        event,
        db=db,
        settings=settings,
        engine=AsyncMock(),
        http_session=AsyncMock(),
        telegram_bot_token="tok",
        telegram_chat_id="chat",
    )


@pytest.fixture
def resolve_spy(monkeypatch):
    """Records whether execution reached resolution (the alert/trade path).

    Resolution is the first thing past the guard, so 'was it called' is the
    exact observable that distinguishes recorded-only from acted-upon.
    """
    calls: list[tuple] = []

    async def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError(
            "resolve_and_enrich reached — guard should have returned first"
        )

    monkeypatch.setattr(listener_mod, "resolve_and_enrich", _spy)
    return calls


async def _mkdb(tmp_path) -> Database:
    db = Database(tmp_path / "age.db")
    await db.initialize()
    return db


async def _row_count(db, table: str) -> int:
    cur = await db._conn.execute(f"SELECT COUNT(*) FROM {table}")
    (n,) = await cur.fetchone()
    return n


async def test_stale_message_is_persisted_but_not_resolved(
    tmp_path, settings_factory, resolve_spy
):
    """The core contract: recorded, but not alerted/traded."""
    settings = settings_factory(TG_SOCIAL_MAX_MESSAGE_AGE_MIN=60)
    db = await _mkdb(tmp_path)
    try:
        old = datetime.now(timezone.utc) - timedelta(days=70)
        # Does not raise: the guard returns before resolve_spy's AssertionError.
        await _run(db, settings, _event(501, old))

        assert resolve_spy == [], "stale message must not reach resolution"
        # Persisted anyway — the row is evidence, only the ACTION is suppressed.
        assert await _row_count(db, "tg_social_messages") == 1
        # No signal row: signals are what drive alerts/trades downstream.
        assert await _row_count(db, "tg_social_signals") == 0
    finally:
        await db.close()


async def test_stale_message_still_advances_the_watermark(
    tmp_path, settings_factory, resolve_spy
):
    """Forward progress. If a skipped replay left the watermark behind, every
    restart would re-fetch and re-skip the same message forever, and catchup
    could never reach the newer messages sitting behind it."""
    settings = settings_factory(TG_SOCIAL_MAX_MESSAGE_AGE_MIN=60)
    db = await _mkdb(tmp_path)
    try:
        await _run(db, settings, _event(777, datetime.now(timezone.utc) - timedelta(days=5)))

        cur = await db._conn.execute(
            "SELECT last_seen_msg_id FROM tg_social_watermarks WHERE channel_handle = ?",
            (CHANNEL,),
        )
        row = await cur.fetchone()
        assert row is not None, "watermark row must exist for a skipped replay"
        assert row[0] == 777
    finally:
        await db.close()


async def test_fresh_message_is_not_gated(tmp_path, settings_factory, monkeypatch):
    """A live message (age ~0) must pass the guard untouched."""
    reached: list[str] = []

    async def _spy(*args, **kwargs):
        reached.append("yes")
        raise RuntimeError("stop-after-guard")

    monkeypatch.setattr(listener_mod, "resolve_and_enrich", _spy)

    settings = settings_factory(TG_SOCIAL_MAX_MESSAGE_AGE_MIN=60)
    db = await _mkdb(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="stop-after-guard"):
            await _run(db, settings, _event(502, datetime.now(timezone.utc)))
        assert reached == ["yes"], "fresh message must reach resolution"
    finally:
        await db.close()


async def test_threshold_zero_disables_the_guard(
    tmp_path, settings_factory, monkeypatch
):
    """0 is the documented escape hatch = pre-guard behaviour."""
    reached: list[str] = []

    async def _spy(*args, **kwargs):
        reached.append("yes")
        raise RuntimeError("stop-after-guard")

    monkeypatch.setattr(listener_mod, "resolve_and_enrich", _spy)

    settings = settings_factory(TG_SOCIAL_MAX_MESSAGE_AGE_MIN=0)
    db = await _mkdb(tmp_path)
    try:
        ancient = datetime.now(timezone.utc) - timedelta(days=400)
        with pytest.raises(RuntimeError, match="stop-after-guard"):
            await _run(db, settings, _event(503, ancient))
        assert reached == ["yes"], "threshold 0 must not gate anything"
    finally:
        await db.close()


async def test_uncomputable_age_fails_open(tmp_path, settings_factory, monkeypatch):
    """Never drop a message because its age could not be computed.

    Driven by patching `_message_age_minutes` rather than by feeding a
    non-datetime `date`: that input never reaches the guard, because
    `_persist_message_with_watermark` runs first and raises on it (the
    message is DLQ'd well before any age check). Patching the helper
    exercises the None branch that actually exists in the guard, instead
    of asserting on a path the caller cannot reach.
    """
    reached: list[str] = []

    async def _spy(*args, **kwargs):
        reached.append("yes")
        raise RuntimeError("stop-after-guard")

    monkeypatch.setattr(listener_mod, "resolve_and_enrich", _spy)
    monkeypatch.setattr(listener_mod, "_message_age_minutes", lambda _: None)

    settings = settings_factory(TG_SOCIAL_MAX_MESSAGE_AGE_MIN=60)
    db = await _mkdb(tmp_path)
    try:
        ancient = datetime.now(timezone.utc) - timedelta(days=400)
        with pytest.raises(RuntimeError, match="stop-after-guard"):
            await _run(db, settings, _event(504, ancient))
        assert reached == ["yes"], "uncomputable age must fail open"
    finally:
        await db.close()


# -- _message_age_minutes ------------------------------------------------


def test_age_helper_handles_naive_datetime_as_utc():
    """A naive datetime must not raise; the listener would drop the message."""
    naive = (datetime.now(timezone.utc) - timedelta(minutes=30)).replace(tzinfo=None)
    age = _message_age_minutes(naive)
    assert age is not None
    assert 29 <= age <= 31


def test_age_helper_returns_none_for_non_datetime():
    assert _message_age_minutes("2026-01-01") is None
    assert _message_age_minutes(None) is None
    assert _message_age_minutes(1735689600) is None


def test_age_helper_future_timestamp_is_negative_not_stale():
    """Clock skew must only ever make a message look newer, never older."""
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    age = _message_age_minutes(future)
    assert age is not None and age < 0
