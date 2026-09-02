"""Catchup-replay age guard for tg_social messages.

`_catchup_channel` replays historical messages through `handle_new_message`,
the same entry point live messages use, and nothing downstream inspects
message age. Without a bound, a catchup pass resolves, alerts and (on
trade_eligible channels) paper-trades months-old calls as if they had just
fired -- a channel whose watermark is 0 would replay its entire history.
TG_SOCIAL_CATCHUP_LIMIT defaults to 200 in code, so this is the default
posture, not a hazard that only appears if an operator opts in.

The contract under test:
  * stale REPLAY   -> persisted, NOT resolved, warned, counted
  * live message   -> never gated, at any age (the load-bearing property:
                      age spans two clocks, so gating live traffic would let
                      local clock skew silently kill the whole signal lane)
  * fresh message  -> resolved as normal
  * threshold 0    -> guard disabled entirely (documented escape hatch)
  * bad timestamp  -> fail OPEN (never drop on an age-computation error)
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


async def _run(db, settings, event, *, is_replay: bool = True, stats=None) -> None:
    """Defaults to is_replay=True: the guard only applies to catchup replay,
    so a test that forgot the flag would silently exercise the ungated live
    path and pass for the wrong reason. The live path is covered explicitly
    by test_live_message_is_never_gated."""
    await handle_new_message(
        event,
        db=db,
        settings=settings,
        engine=AsyncMock(),
        http_session=AsyncMock(),
        telegram_bot_token="tok",
        telegram_chat_id="chat",
        is_replay=is_replay,
        stats=stats,
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


async def test_live_message_is_never_gated(tmp_path, settings_factory, monkeypatch):
    """THE load-bearing property. Age is a difference between OUR clock and
    Telegram's, so a fast local clock inflates it. If the guard applied to
    live traffic, skew would silently suppress the entire signal lane while
    tg_social_health.last_message_at kept refreshing (it is stamped during
    persist, before the guard) and every alarm stayed green.

    An ancient LIVE message must still be processed.
    """
    reached: list[str] = []

    async def _spy(*args, **kwargs):
        reached.append("yes")
        raise RuntimeError("stop-after-guard")

    monkeypatch.setattr(listener_mod, "resolve_and_enrich", _spy)

    settings = settings_factory(TG_SOCIAL_MAX_MESSAGE_AGE_MIN=60)
    db = await _mkdb(tmp_path)
    try:
        ancient = datetime.now(timezone.utc) - timedelta(days=400)
        with pytest.raises(RuntimeError, match="stop-after-guard"):
            await _run(db, settings, _event(601, ancient), is_replay=False)
        assert reached == ["yes"], "live message must never be age-gated"
    finally:
        await db.close()


async def test_skip_emits_a_warning_with_the_diagnostic_fields(
    tmp_path, settings_factory, resolve_spy
):
    """Pins the guard's only per-message observable.

    A prior revision of this suite let the entire log call be deleted with
    every test still green (mutation M7). That log is the sole evidence a
    call was discarded, so it is asserted here by event name AND by the
    fields needed to diagnose -- an age_min in the low hundreds means live
    traffic is being eaten, one in the thousands means a genuine historical
    replay, and that distinction is unavailable without the values.
    """
    from structlog.testing import capture_logs

    settings = settings_factory(TG_SOCIAL_MAX_MESSAGE_AGE_MIN=60)
    db = await _mkdb(tmp_path)
    try:
        old = datetime.now(timezone.utc) - timedelta(days=3)
        with capture_logs() as logs:
            await _run(db, settings, _event(602, old))

        drops = [e for e in logs if e.get("event") == "tg_social_message_too_old"]
        assert len(drops) == 1, f"expected exactly one drop log, got {logs}"
        entry = drops[0]
        assert entry["log_level"] == "warning"
        assert entry["channel_handle"] == CHANNEL
        assert entry["msg_id"] == 602
        assert entry["max_age_min"] == 60
        # 3 days == 4320 min; assert the real value, not merely presence.
        assert 4300 <= entry["age_min"] <= 4340
    finally:
        await db.close()


async def test_skip_is_counted_in_the_pass_stats(
    tmp_path, settings_factory, resolve_spy
):
    """The per-pass aggregate _catchup_channel logs. Without this, noticing
    that a whole catchup pass was discarded means reading every message-level
    warning individually."""
    settings = settings_factory(TG_SOCIAL_MAX_MESSAGE_AGE_MIN=60)
    db = await _mkdb(tmp_path)
    try:
        stats: dict[str, int] = {}
        old = datetime.now(timezone.utc) - timedelta(days=2)
        await _run(db, settings, _event(603, old), stats=stats)
        await _run(db, settings, _event(604, old), stats=stats)
        assert stats == {"skipped_stale": 2}
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
