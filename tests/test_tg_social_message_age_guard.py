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
        # Raise so nothing downstream (alerting, dispatch) executes, and so a
        # test that EXPECTS resolution can assert on it positively.
        raise RuntimeError("resolve-reached")

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


async def test_skipped_replay_still_persists_its_watermark_row(
    tmp_path, settings_factory, resolve_spy
):
    """A skipped replay still writes its watermark row.

    Deliberately NOT named "advances the watermark": that framing was
    retracted. `_catchup_channel` iterates newest-first and the watermark
    UPSERT is last-write-wins with no MAX(), so a multi-message batch ends
    on its OLDEST id and IS re-fetched next restart. What this pins is the
    narrower true property -- the guard runs after persist, so a skip leaves
    a row behind rather than a hole -- which is what kills a
    guard-before-persist mutant.
    """
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
        # A FRESH replay sharing the same dict. Without this the test proves
        # only that the counter increments per call, so hoisting the
        # increment out of the skip branch -- counting every replayed
        # message, stale or not -- would survive.
        with pytest.raises(RuntimeError, match="resolve-reached"):
            await _run(db, settings, _event(605, datetime.now(timezone.utc)), stats=stats)
        assert stats == {"skipped_stale": 2}
    finally:
        await db.close()


# -- _catchup_channel wiring ---------------------------------------------
#
# Everything above calls handle_new_message DIRECTLY and supplies is_replay
# itself, which pins the GUARD but not the WIRING. Four reviewers independently
# found that deleting `is_replay=True` at the _catchup_channel call site
# reverts this entire feature to a no-op with the whole suite green. These two
# tests are the falsifier for that.


class _FakeMsg:
    def __init__(self, msg_id: int, posted_at):
        self.id = msg_id
        self.message = TEXT
        self.text = TEXT
        self.date = posted_at

    async def get_chat(self):
        return SimpleNamespace(username=CHANNEL.lstrip("@"), id=-100123)

    async def get_sender(self):
        return SimpleNamespace(username="curator", first_name="Curator")


class _FakeClient:
    """Yields messages instead of raising.

    Every pre-existing _catchup_channel test drives iter_messages with a
    RAISING fake, which is why no message has ever reached the handler
    through this path.
    """

    def __init__(self, messages, raise_at_end=None):
        self._messages = messages
        self._raise_at_end = raise_at_end
        # Recorded so a test can pin that catchup passes the WATERMARK as
        # min_id. A fake that ignores it lets a mutant replay the channel's
        # entire history on every restart -- the exact hazard this guard
        # exists to bound -- while the suite stays green.
        self.seen_min_id = None
        self.seen_limit = None

    def iter_messages(self, channel, min_id=0, limit=None):
        self.seen_min_id = min_id
        self.seen_limit = limit
        messages = self._messages
        raise_at_end = self._raise_at_end

        async def _gen():
            for m in messages:
                yield m
            if raise_at_end is not None:
                raise raise_at_end

        return _gen()


async def test_catchup_applies_the_guard_and_reports_the_pass(
    tmp_path, settings_factory, monkeypatch
):
    """Drives a real _catchup_channel pass: 1 fresh + 2 stale, newest-first.

    Kills three mutants that the rest of the suite cannot see:
      * delete `is_replay=True`   -> replays ungated, feature is a no-op
      * delete `stats=pass_stats` -> counter never increments
      * delete the aggregate log  -> per-pass summary disappears

    NOTE the spy RECORDS rather than raises. `_catchup_channel` funnels
    per-message exceptions into the DLQ and keeps iterating, so a raising
    spy is swallowed and the assertion passes against the mutant -- the
    reviewer who wrote this falsifier first hit exactly that.
    """
    from structlog.testing import capture_logs

    from scout.social.telegram.listener import _catchup_channel
    from scout.social.telegram.models import ResolutionResult, ResolutionState

    resolved: list[int] = []

    async def _resolve(*args, **kwargs):
        resolved.append(1)
        return ResolutionResult(state=ResolutionState.UNRESOLVED_TERMINAL)

    async def _replay(*args, **kwargs):
        return None

    monkeypatch.setattr(listener_mod, "resolve_and_enrich", _resolve)
    monkeypatch.setattr(listener_mod, "_replay_post_resolution", _replay)

    now = datetime.now(timezone.utc)
    client = _FakeClient(
        [
            _FakeMsg(903, now),                          # fresh  -> resolves
            _FakeMsg(902, now - timedelta(days=3)),      # stale  -> skipped
            _FakeMsg(901, now - timedelta(days=40)),     # stale  -> skipped
        ]
    )

    settings = settings_factory(
        TG_SOCIAL_MAX_MESSAGE_AGE_MIN=60, TG_SOCIAL_CATCHUP_LIMIT=200
    )
    db = await _mkdb(tmp_path)
    try:
        with capture_logs() as logs:
            await _catchup_channel(
                client=client,
                db=db,
                settings=settings,
                engine=AsyncMock(),
                http_session=AsyncMock(),
                telegram_bot_token="tok",
                telegram_chat_id="chat",
                channel_handle=CHANNEL,
            )

        # Only the fresh message may reach the alert/trade path.
        assert resolved == [1], f"expected exactly the fresh message, got {resolved}"

        drops = [e for e in logs if e.get("event") == "tg_social_message_too_old"]
        assert {d["msg_id"] for d in drops} == {902, 901}

        agg = [e for e in logs if e.get("event") == "tg_social_catchup_pass"]
        assert len(agg) == 1, f"expected one per-pass summary, got {agg}"
        assert agg[0]["skipped_stale"] == 2
        assert agg[0]["fetched"] == 3
        assert agg[0]["max_age_min"] == 60
        assert agg[0]["log_level"] == "warning"

        # All three persisted regardless of age -- a skip is evidence, not a hole.
        assert await _row_count(db, "tg_social_messages") == 3
    finally:
        await db.close()


async def test_second_pass_counts_duplicates_and_no_signal(
    tmp_path, settings_factory, monkeypatch
):
    """Repeat-pass reconciliation. Kills four mutants at once:
      * duplicates tally removed / double-counted
      * `duplicates` dropped from the summary
      * the emit condition narrowed back to `if _skipped:` -- a second pass
        has skipped_stale == 0, so a narrowed condition goes SILENT exactly
        when the operator needs to know the pass did nothing new

    Also pins that a no-signal message that is already known counts as a
    DUPLICATE, not a fresh no_signal: without capturing the persist return
    in that branch, a repeat pass over a chatty channel reads almost
    identically to a healthy pass processing new messages.
    """
    from structlog.testing import capture_logs

    from scout.social.telegram.listener import _catchup_channel
    from scout.social.telegram.models import ResolutionResult, ResolutionState

    async def _resolve(*args, **kwargs):
        return ResolutionResult(state=ResolutionState.UNRESOLVED_TERMINAL)

    async def _replay(*args, **kwargs):
        return None

    monkeypatch.setattr(listener_mod, "resolve_and_enrich", _resolve)
    monkeypatch.setattr(listener_mod, "_replay_post_resolution", _replay)

    now = datetime.now(timezone.utc)
    chatter = _FakeMsg(802, now)
    chatter.message = chatter.text = "gm everyone no ticker here"

    def _client():
        return _FakeClient([_FakeMsg(803, now), chatter])

    settings = settings_factory(
        TG_SOCIAL_MAX_MESSAGE_AGE_MIN=60, TG_SOCIAL_CATCHUP_LIMIT=200
    )
    db = await _mkdb(tmp_path)
    kw = dict(
        db=db,
        settings=settings,
        engine=AsyncMock(),
        http_session=AsyncMock(),
        telegram_bot_token="tok",
        telegram_chat_id="chat",
        channel_handle=CHANNEL,
    )
    try:
        with capture_logs() as first:
            await _catchup_channel(client=_client(), **kw)
        a1 = [e for e in first if e.get("event") == "tg_social_catchup_pass"][0]
        assert (a1["duplicates"], a1["no_signal"], a1["fetched"]) == (0, 1, 2)
        # Clean pass -> INFO, not WARNING.
        assert a1["log_level"] == "info"

        # Same batch again: both are now known.
        second_client = _client()
        with capture_logs() as second:
            await _catchup_channel(client=second_client, **kw)
        a2 = [e for e in second if e.get("event") == "tg_social_catchup_pass"][0]
        assert a2["duplicates"] == 2, f"repeat pass must report duplicates: {a2}"
        assert a2["no_signal"] == 0
        assert a2["skipped_stale"] == 0
        assert a2["fetched"] == 2
        # Something WAS dropped, so this one escalates.
        assert a2["log_level"] == "warning"

        # Catchup must resume from the watermark, not from 0. A fake that
        # ignored min_id would let a full-history-replay mutant survive.
        assert second_client.seen_min_id == 802
        assert await _row_count(db, "tg_social_messages") == 2
    finally:
        await db.close()


async def test_partial_duplicate_pass_stays_at_info(
    tmp_path, settings_factory, monkeypatch
):
    """Routine restarts must not warn.

    The watermark is non-monotonic (newest-first iteration, last-write-wins
    UPSERT), so a pass ends on its OLDEST id and the next restart re-fetches
    the tail. `duplicates > 0` is therefore the NORMAL case on any channel
    that received more than one message -- promoting on it would warn on
    every restart and train the operator to ignore the level that `dlq` and
    `skipped_stale` need. Only a pass that made ZERO forward progress
    (all-duplicate) escalates; that case is covered by
    test_second_pass_counts_duplicates_and_no_signal.
    """
    from structlog.testing import capture_logs

    from scout.social.telegram.listener import _catchup_channel
    from scout.social.telegram.models import ResolutionResult, ResolutionState

    async def _resolve(*args, **kwargs):
        return ResolutionResult(state=ResolutionState.UNRESOLVED_TERMINAL)

    async def _replay(*args, **kwargs):
        return None

    monkeypatch.setattr(listener_mod, "resolve_and_enrich", _resolve)
    monkeypatch.setattr(listener_mod, "_replay_post_resolution", _replay)

    now = datetime.now(timezone.utc)
    settings = settings_factory(
        TG_SOCIAL_MAX_MESSAGE_AGE_MIN=60, TG_SOCIAL_CATCHUP_LIMIT=200
    )
    db = await _mkdb(tmp_path)
    kw = dict(
        db=db,
        settings=settings,
        engine=AsyncMock(),
        http_session=AsyncMock(),
        telegram_bot_token="tok",
        telegram_chat_id="chat",
        channel_handle=CHANNEL,
    )
    try:
        await _catchup_channel(client=_FakeClient([_FakeMsg(901, now)]), **kw)
        # Second pass: one already-seen message + one genuinely new one.
        with capture_logs() as logs:
            await _catchup_channel(
                client=_FakeClient([_FakeMsg(902, now), _FakeMsg(901, now)]), **kw
            )
        agg = [e for e in logs if e.get("event") == "tg_social_catchup_pass"][0]
        assert agg["duplicates"] == 1 and agg["fetched"] == 2
        assert agg["log_level"] == "info", (
            "a pass that made forward progress must not warn merely because "
            f"it re-read the tail: {agg}"
        )
    finally:
        await db.close()


async def test_persist_failure_is_counted_as_dlq_and_warns(
    tmp_path, settings_factory, monkeypatch
):
    """Pins the whole `dlq` dimension.

    Kills three mutants that survive otherwise, all sharing one cause --
    no test anywhere makes a persist raise:
      * the dlq leg of the three-way selection mislabelled as no_signal
      * the signal-path `stats["dlq"]` counter deleted
      * `_dlq` dropped from the WARNING trigger

    A pass that is quietly DLQ-ing messages must not report INFO.
    """
    from structlog.testing import capture_logs

    from scout.social.telegram.listener import _catchup_channel

    async def _boom(*args, **kwargs):
        raise RuntimeError("persist exploded")

    monkeypatch.setattr(listener_mod, "_persist_message_with_watermark", _boom)

    settings = settings_factory(
        TG_SOCIAL_MAX_MESSAGE_AGE_MIN=60, TG_SOCIAL_CATCHUP_LIMIT=200
    )
    db = await _mkdb(tmp_path)
    try:
        now = datetime.now(timezone.utc)
        # BOTH shapes: a signal-bearing message exercises the counter on the
        # signal path, and a chatter message exercises the dlq LEG of the
        # three-way selection in the parsed.is_empty branch. With only the
        # first, mislabelling that leg as no_signal survives.
        chatter = _FakeMsg(911, now)
        chatter.message = chatter.text = "gm no ticker"
        with capture_logs() as logs:
            await _catchup_channel(
                client=_FakeClient([_FakeMsg(910, now), chatter]),
                db=db,
                settings=settings,
                engine=AsyncMock(),
                http_session=AsyncMock(),
                telegram_bot_token="tok",
                telegram_chat_id="chat",
                channel_handle=CHANNEL,
            )
        agg = [e for e in logs if e.get("event") == "tg_social_catchup_pass"][0]
        assert agg["dlq"] == 2, f"both DLQ'd shapes must be tallied: {agg}"
        assert agg["no_signal"] == 0, f"a DLQ'd chatter message is not no_signal: {agg}"
        assert agg["fetched"] == 2
        assert agg["log_level"] == "warning", "a DLQ-ing pass must not read as clean"
        assert await _row_count(db, "tg_social_dlq") == 2
    finally:
        await db.close()


async def test_exception_escaping_the_handler_is_counted_as_dlq(
    tmp_path, settings_factory, monkeypatch
):
    """An exception that escapes `handle_new_message` into
    `_catchup_channel`'s own per-message except writes a DLQ row and
    increments `fetched`. Without a tally there it lands in the `resolved`
    residual, so a pass that DLQ'd everything reported INFO and read as
    fully resolved -- the reachable leak in the identity claim.
    """
    from structlog.testing import capture_logs

    from scout.social.telegram.listener import _catchup_channel

    def _boom(_text):
        raise RuntimeError("parse exploded")

    # parse_message runs before handle_new_message's own try, so this
    # propagates out to the loop rather than being handled internally.
    monkeypatch.setattr(listener_mod, "parse_message", _boom)

    settings = settings_factory(
        TG_SOCIAL_MAX_MESSAGE_AGE_MIN=60, TG_SOCIAL_CATCHUP_LIMIT=200
    )
    db = await _mkdb(tmp_path)
    try:
        with capture_logs() as logs:
            await _catchup_channel(
                client=_FakeClient([_FakeMsg(920, datetime.now(timezone.utc))]),
                db=db,
                settings=settings,
                engine=AsyncMock(),
                http_session=AsyncMock(),
                telegram_bot_token="tok",
                telegram_chat_id="chat",
                channel_handle=CHANNEL,
            )
        agg = [e for e in logs if e.get("event") == "tg_social_catchup_pass"][0]
        assert agg["dlq"] == 1, f"an escaped exception must be tallied: {agg}"
        assert agg["fetched"] == 1
        assert agg["log_level"] == "warning"
        assert await _row_count(db, "tg_social_dlq") == 1
    finally:
        await db.close()


async def test_malformed_event_is_counted_as_dlq(
    tmp_path, settings_factory, monkeypatch
):
    """A message whose chat metadata is unusable DLQs and must be tallied.

    These early returns pre-date this PR, but the claim that the tallies
    partition every yielded message is new -- so an uncounted exit here
    inflates the `resolved` residual exactly like any other leak. Pins the
    `_bump` helper, whose body is otherwise a no-op no test would notice.
    """
    from structlog.testing import capture_logs

    from scout.social.telegram.listener import _catchup_channel

    class _NoChatMsg(_FakeMsg):
        async def get_chat(self):
            return None

    settings = settings_factory(
        TG_SOCIAL_MAX_MESSAGE_AGE_MIN=60, TG_SOCIAL_CATCHUP_LIMIT=200
    )
    db = await _mkdb(tmp_path)
    try:
        with capture_logs() as logs:
            await _catchup_channel(
                client=_FakeClient([_NoChatMsg(930, datetime.now(timezone.utc))]),
                db=db,
                settings=settings,
                engine=AsyncMock(),
                http_session=AsyncMock(),
                telegram_bot_token="tok",
                telegram_chat_id="chat",
                channel_handle=CHANNEL,
            )
        agg = [e for e in logs if e.get("event") == "tg_social_catchup_pass"][0]
        assert agg["dlq"] == 1, f"a malformed event must be tallied: {agg}"
        assert agg["log_level"] == "warning"
    finally:
        await db.close()


async def test_summary_survives_an_aborted_pass(tmp_path, settings_factory, monkeypatch):
    """FloodWait mid-catchup: the summary must still emit AND the original
    exception must still propagate.

    A long historical catchup is both the pass most likely to skip many
    messages and the one most likely to trip FloodWait, so emitting the
    summary after the `try` lost it in exactly the case it exists for.
    """
    from structlog.testing import capture_logs
    from telethon.errors import FloodWaitError

    from scout.social.telegram.listener import _catchup_channel

    settings = settings_factory(
        TG_SOCIAL_MAX_MESSAGE_AGE_MIN=60, TG_SOCIAL_CATCHUP_LIMIT=200
    )
    db = await _mkdb(tmp_path)
    try:
        old = datetime.now(timezone.utc) - timedelta(days=9)
        client = _FakeClient(
            [_FakeMsg(701, old), _FakeMsg(702, old)],
            raise_at_end=FloodWaitError(request=None, capture=7),
        )
        with capture_logs() as logs:
            with pytest.raises(FloodWaitError):
                await _catchup_channel(
                    client=client,
                    db=db,
                    settings=settings,
                    engine=AsyncMock(),
                    http_session=AsyncMock(),
                    telegram_bot_token="tok",
                    telegram_chat_id="chat",
                    channel_handle=CHANNEL,
                )
        agg = [e for e in logs if e.get("event") == "tg_social_catchup_pass"]
        assert len(agg) == 1, f"aborted pass must still summarise: {agg}"
        assert agg[0]["skipped_stale"] == 2
    finally:
        await db.close()


async def test_empty_pass_emits_no_summary(tmp_path, settings_factory, monkeypatch):
    """A pass that touched nothing stays silent -- otherwise every restart
    at TG_SOCIAL_CATCHUP_LIMIT=0 would emit a summary per channel."""
    from structlog.testing import capture_logs

    from scout.social.telegram.listener import _catchup_channel

    settings = settings_factory(TG_SOCIAL_MAX_MESSAGE_AGE_MIN=60)
    db = await _mkdb(tmp_path)
    try:
        with capture_logs() as logs:
            await _catchup_channel(
                client=_FakeClient([]),
                db=db,
                settings=settings,
                engine=AsyncMock(),
                http_session=AsyncMock(),
                telegram_bot_token="tok",
                telegram_chat_id="chat",
                channel_handle=CHANNEL,
            )
        assert [e for e in logs if e.get("event") == "tg_social_catchup_pass"] == []
    finally:
        await db.close()


async def test_the_production_default_leaves_live_ungated(
    tmp_path, settings_factory, monkeypatch
):
    """Calls handle_new_message with is_replay OMITTED, as `_on_new` does.

    The live lane is protected solely by the `is_replay: bool = False`
    default. Every other test passes the flag explicitly, so flipping that
    default to True survives them all. This is the only test that exercises
    the production call shape.
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
            # No is_replay kwarg -- exactly how _on_new invokes it.
            await handle_new_message(
                _event(606, ancient),
                db=db,
                settings=settings,
                engine=AsyncMock(),
                http_session=AsyncMock(),
                telegram_bot_token="tok",
                telegram_chat_id="chat",
            )
        assert reached == ["yes"], "the default must leave the live path ungated"
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
