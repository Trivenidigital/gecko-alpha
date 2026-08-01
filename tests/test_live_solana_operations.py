"""PR-S5: operations — the stuck watchdog and the autonomy preconditions.

Two mechanisms, both about failures that would otherwise be silent.

**The stuck watchdog.** ``solana_executions`` is only written when a trade
runs, so a row-rate SLO would alarm on a quiet week and stay silent through a
process that died mid-submission. The right analogue is STUCK STATE, with a
different threshold per state — ``awaiting_authorization`` is a human reading a
screen, ``submission_attempted`` is a transaction whose fate nobody is
resolving.

**The autonomy preconditions.** The product mandate is that reaching
BOUNDED_AUTONOMOUS must be impossible by flipping one flag. The test that
matters is the negative one: with the mode set, the flag on, and the limits
configured, the lane still refuses until the LEDGER shows completed supervised
executions — because that is the one precondition configuration cannot fake.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.live import solana_lane as lane
from scout.live.solana import execution_watchdog as watchdog

_SIGNATURE = "4Rf81Nd6uXrp39hFFr1WMFnGjzxyGXrvwb8VSRUnotH1uTbzLEsatgthdk1mR2yUq2P1Bz5HXoccfQX7YTUgffwy"


@pytest.fixture(autouse=True)
def _clear_watchdog_dedup():
    watchdog._reset_for_tests()
    yield
    watchdog._reset_for_tests()


async def _db(tmp_path) -> Database:
    db = Database(tmp_path / "ops.db")
    await db.initialize()
    return db


async def _seed(
    db, decision_id, state, *, age_seconds, mode="SUPERVISED_LIVE", sig=None
):
    """Insert one execution row that last moved ``age_seconds`` ago."""
    stamped = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    async with db._txn_lock:
        await db._conn.execute(
            "INSERT INTO solana_executions "
            "(decision_id, state, mode, expected_signature, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (decision_id, state, mode, sig, stamped, stamped),
        )
        await db._conn.commit()


class _AlerterSpy:
    """Captures what would have gone to Telegram, including parse_mode."""

    def __init__(self, *, fail=False):
        self.calls: list[dict] = []
        self._fail = fail

    async def send_telegram_message(self, text, session, settings, **kwargs):
        self.calls.append({"text": text, **kwargs})
        if self._fail:
            raise RuntimeError("telegram unreachable")


@pytest.fixture
def alerter_spy(monkeypatch):
    def _install(*, fail=False):
        spy = _AlerterSpy(fail=fail)
        import scout.alerter as real

        monkeypatch.setattr(real, "send_telegram_message", spy.send_telegram_message)
        return spy

    return _install


# ======================================================================
# Threshold mapping: every non-terminal state is watched
# ======================================================================
def test_every_non_terminal_state_has_a_threshold():
    """*** The monitoring hole this test exists to prevent. ***

    Adding a state to the lifecycle without giving it a threshold would make
    that state permanently unwatched, and nothing else would notice.
    """
    assert watchdog.unmonitored_states(lane.ALL_STATES, lane.TERMINAL_STATES) == []


def test_terminal_states_are_not_watched():
    """A finished run is not stuck; alerting on one is noise that trains an
    operator to ignore the alert."""
    for state in lane.TERMINAL_STATES:
        assert state not in watchdog.STATE_THRESHOLD_KEYS


def test_the_submission_threshold_is_the_tightest(settings_factory):
    """The states mean different things, so one threshold cannot serve both.

    ``awaiting_authorization`` is a human reading a screen;
    ``submission_attempted`` is a transaction whose fate nobody is resolving.
    """
    settings = settings_factory()
    submission = watchdog.threshold_seconds("submission_attempted", settings)
    approval = watchdog.threshold_seconds("awaiting_authorization", settings)
    assert submission < approval
    assert submission <= 300, "a submission must not sit unresolved for long"


def test_thresholds_come_from_settings(settings_factory):
    settings = settings_factory(SOLANA_STUCK_SUBMISSION_SEC=42.0)
    assert watchdog.threshold_seconds("submission_attempted", settings) == 42.0
    assert watchdog.threshold_seconds("reconciled", settings) is None


def test_a_zero_threshold_is_refused_at_config_time(settings_factory):
    """Zero reports every row as stuck the instant it is written."""
    with pytest.raises(ValueError, match="SOLANA_STUCK_SUBMISSION_SEC"):
        settings_factory(SOLANA_STUCK_SUBMISSION_SEC=0)


# ======================================================================
# Finding stuck rows
# ======================================================================
async def test_a_fresh_execution_is_not_stuck(tmp_path, settings_factory):
    db = await _db(tmp_path)
    await _seed(db, "fresh", "submission_attempted", age_seconds=1)
    assert await watchdog.find_stuck_executions(db, settings_factory()) == []
    await db.close()


async def test_a_submission_past_its_threshold_is_stuck(tmp_path, settings_factory):
    """*** The row that costs money to leave alone. ***"""
    db = await _db(tmp_path)
    await _seed(
        db, "stuck-sub", "submission_attempted", age_seconds=600, sig=_SIGNATURE
    )
    stuck = await watchdog.find_stuck_executions(
        db, settings_factory(SOLANA_STUCK_SUBMISSION_SEC=180.0)
    )
    assert [s.decision_id for s in stuck] == ["stuck-sub"]
    assert stuck[0].blocks_the_lane is True
    assert stuck[0].expected_signature == _SIGNATURE
    assert stuck[0].age_seconds >= 600
    await db.close()


async def test_a_terminal_execution_is_never_stuck(tmp_path, settings_factory):
    db = await _db(tmp_path)
    await _seed(db, "done", "reconciled", age_seconds=999_999)
    await _seed(db, "dead", "failed", age_seconds=999_999)
    assert await watchdog.find_stuck_executions(db, settings_factory()) == []
    await db.close()


async def test_an_approval_inside_its_generous_threshold_is_not_stuck(
    tmp_path, settings_factory
):
    """A slow human is not an incident. Paging on one is how alerts get muted."""
    db = await _db(tmp_path)
    await _seed(db, "thinking", "awaiting_authorization", age_seconds=600)
    settings = settings_factory(
        SOLANA_STUCK_AWAITING_AUTHORIZATION_SEC=3600.0,
        SOLANA_STUCK_PRE_SUBMISSION_SEC=60.0,
    )
    assert await watchdog.find_stuck_executions(db, settings) == []
    await db.close()


async def test_blocking_rows_are_reported_first(tmp_path, settings_factory):
    """The order an operator should read: what may have moved money, first."""
    db = await _db(tmp_path)
    await _seed(db, "old-quote", "quote_created", age_seconds=100_000)
    await _seed(db, "submitted", "submission_attempted", age_seconds=600)
    stuck = await watchdog.find_stuck_executions(db, settings_factory())
    assert [s.decision_id for s in stuck] == ["submitted", "old-quote"]
    await db.close()


# ======================================================================
# Alerting (CLAUDE.md §12b)
# ======================================================================
async def test_a_stuck_execution_alerts_the_operator_in_plain_text(
    tmp_path, settings_factory, alerter_spy
):
    """*** parse_mode=None is load-bearing. ***

    Every state name in the body contains an underscore. Under MarkdownV1
    Telegram eats them as italics markers, mangles the message and still
    returns HTTP 200 — the documented worked example for this exact failure.
    """
    spy = alerter_spy()
    db = await _db(tmp_path)
    await _seed(db, "stuck-1", "submission_attempted", age_seconds=600, sig=_SIGNATURE)

    summary = await watchdog.check_stuck_executions(db, None, settings_factory())

    assert summary["alerted"] == 1
    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["parse_mode"] is None
    assert "submission_attempted" in call["text"]
    assert "DO NOT rebuild" in call["text"]
    assert call["source"] == "solana_execution_watchdog"
    await db.close()


async def test_the_alert_is_deduped_per_state_and_re_fires_on_a_new_state(
    tmp_path, settings_factory, alerter_spy
):
    """A row sitting still must not page every run; a row that MOVES to a new
    stuck state is new information and must."""
    spy = alerter_spy()
    db = await _db(tmp_path)
    settings = settings_factory()
    await _seed(db, "d1", "submission_attempted", age_seconds=600)

    await watchdog.check_stuck_executions(db, None, settings)
    await watchdog.check_stuck_executions(db, None, settings)
    assert len(spy.calls) == 1, "the same stuck state paged twice"

    async with db._txn_lock:
        await db._conn.execute(
            "UPDATE solana_executions SET state = 'submission_unknown', "
            "updated_at = ? WHERE decision_id = 'd1'",
            ((datetime.now(timezone.utc) - timedelta(seconds=99_999)).isoformat(),),
        )
        await db._conn.commit()
    await watchdog.check_stuck_executions(db, None, settings)
    assert len(spy.calls) == 2
    assert "submission_unknown" in spy.calls[1]["text"]
    await db.close()


async def test_a_failed_delivery_is_retried_rather_than_marked_sent(
    tmp_path, settings_factory, alerter_spy, monkeypatch
):
    """Marking a failed alert as delivered loses the one alert that mattered."""
    failing = alerter_spy(fail=True)
    db = await _db(tmp_path)
    settings = settings_factory()
    await _seed(db, "d1", "submission_attempted", age_seconds=600)

    summary = await watchdog.check_stuck_executions(db, None, settings)
    assert summary["alerted"] == 1
    assert len(failing.calls) == 1

    # Next run: still stuck, still not marked as alerted, so it tries again.
    working = alerter_spy()
    await watchdog.check_stuck_executions(db, None, settings)
    assert len(working.calls) == 1
    await db.close()


async def test_the_watchdog_can_be_disabled(tmp_path, settings_factory, alerter_spy):
    spy = alerter_spy()
    db = await _db(tmp_path)
    await _seed(db, "d1", "submission_attempted", age_seconds=600)
    summary = await watchdog.check_stuck_executions(
        db, None, settings_factory(SOLANA_EXECUTION_WATCHDOG_ENABLED=False)
    )
    assert summary == {"enabled": False, "stuck": [], "alerted": 0}
    assert spy.calls == []
    await db.close()


async def test_a_clean_lane_alerts_nothing(tmp_path, settings_factory, alerter_spy):
    spy = alerter_spy()
    db = await _db(tmp_path)
    summary = await watchdog.check_stuck_executions(db, None, settings_factory())
    assert summary["stuck"] == []
    assert spy.calls == []
    await db.close()


# ======================================================================
# Bounded-autonomy preconditions
# ======================================================================
async def test_the_supervised_count_only_counts_reconciled_supervised_runs(
    tmp_path,
):
    """``finalized`` is not enough.

    A swap that landed but whose accounting could not be explained is exactly
    the history that must NOT count toward promoting the lane — and the state
    machine already refuses to advance such a run past ``finalized``.
    """
    db = await _db(tmp_path)
    await _seed(db, "a", "reconciled", age_seconds=0, mode="SUPERVISED_LIVE")
    await _seed(db, "b", "reconciled", age_seconds=0, mode="SUPERVISED_LIVE")
    await _seed(db, "c", "finalized", age_seconds=0, mode="SUPERVISED_LIVE")
    await _seed(db, "d", "reconciled", age_seconds=0, mode="BOUNDED_AUTONOMOUS")
    await _seed(db, "e", "failed", age_seconds=0, mode="SUPERVISED_LIVE")

    assert await lane.count_supervised_reconciled(db) == 2
    await db.close()


def test_a_zero_supervised_requirement_is_refused_at_config_time(settings_factory):
    """Zero would let the lane go autonomous having never executed anything
    under supervision, which is the precondition's whole point."""
    with pytest.raises(ValueError, match="SOLANA_AUTONOMY_MIN_SUPERVISED_EXECUTIONS"):
        settings_factory(SOLANA_AUTONOMY_MIN_SUPERVISED_EXECUTIONS=0)
