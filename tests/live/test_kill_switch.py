"""Tests for KillSwitch (spec §6). compute_kill_duration G2 math tested first."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.live.kill_switch import KillSwitch, compute_kill_duration


@pytest.mark.parametrize(
    "trigger_hour,trigger_minute,expected_hours",
    [
        (0, 15, 23.75),  # trigger right after midnight → hold until NEXT midnight
        (12, 0, 12.0),  # noon → next midnight = 12h, min is 4h, max() = 12h
        (23, 55, 4.0),  # late-night → 4h minimum wins over 5-min-to-midnight
        (20, 0, 4.0),  # 20:00 → 4h min wins over 4h-to-midnight
    ],
)
def test_compute_kill_duration_maxes_midnight_vs_4h(
    trigger_hour, trigger_minute, expected_hours
):
    trig = datetime(2026, 4, 23, trigger_hour, trigger_minute, tzinfo=timezone.utc)
    dur = compute_kill_duration(trig)
    assert abs(dur.total_seconds() / 3600 - expected_hours) < 0.01


async def test_trigger_inserts_row_and_sets_control(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)
    kid, won = await ks.trigger(
        triggered_by="manual",
        reason="test",
        duration=timedelta(hours=1),
    )
    assert won is True
    cur = await db._conn.execute("SELECT id, triggered_by, cleared_at FROM kill_events")
    rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == kid
    assert rows[0][1] == "manual"
    assert rows[0][2] is None

    cur = await db._conn.execute(
        "SELECT active_kill_event_id FROM live_control WHERE id=1"
    )
    assert (await cur.fetchone())[0] == kid
    await db.close()


async def test_is_active_returns_none_when_cleared(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)
    assert await ks.is_active() is None
    kid, won = await ks.trigger(
        triggered_by="manual", reason="x", duration=timedelta(hours=1)
    )
    assert won is True
    assert (await ks.is_active()).kill_event_id == kid
    await ks.clear(cleared_by="manual")
    assert await ks.is_active() is None
    await db.close()


async def test_auto_clear_if_expired_fires_when_past_killed_until(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)
    await ks.trigger(
        triggered_by="manual", reason="x", duration=timedelta(seconds=-1)
    )  # already expired
    did_clear = await ks.auto_clear_if_expired()
    assert did_clear is True
    assert await ks.is_active() is None
    cur = await db._conn.execute(
        "SELECT cleared_by FROM kill_events ORDER BY id DESC LIMIT 1"
    )
    assert (await cur.fetchone())[0] == "auto_expired"
    await db.close()


async def test_two_concurrent_closes_trigger_exactly_once(tmp_path):
    """Spec §11.5 TOCTOU: two concurrent trigger() calls (as would happen if
    two close paths each detect the daily loss cap breach simultaneously) must
    produce exactly one active kill_event. The loser's speculative row is
    cleaned up; live_control.active_kill_event_id points to the winner."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)
    results = await asyncio.gather(
        ks.trigger(
            triggered_by="daily_loss_cap", reason="A", duration=timedelta(hours=4)
        ),
        ks.trigger(
            triggered_by="daily_loss_cap", reason="B", duration=timedelta(hours=4)
        ),
    )
    # Both calls return the SAME winner id, but only one has i_am_winner=True.
    ids = [r[0] for r in results]
    winners = [r[1] for r in results]
    assert ids[0] == ids[1]
    assert sum(winners) == 1
    # Exactly one kill_events row exists.
    cur = await db._conn.execute("SELECT COUNT(*) FROM kill_events")
    assert (await cur.fetchone())[0] == 1
    # live_control points to that row.
    cur = await db._conn.execute(
        "SELECT active_kill_event_id FROM live_control WHERE id = 1"
    )
    assert (await cur.fetchone())[0] == ids[0]
    await db.close()


# --------------------------------------------------------------------------
# §12b: automated kill-switch state changes must alert the operator.
# The kill switch halts/resumes LIVE trading — the highest-stakes automated
# state reversal in the system. A hooked KillSwitch (the automated main.py
# instance) must notify; a hookless one (CLI manual ops, tests) must not.
# --------------------------------------------------------------------------


async def test_trigger_winner_emits_operator_alert(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    sent: list[str] = []

    async def hook(message: str) -> None:
        sent.append(message)

    ks = KillSwitch(db, alert_hook=hook)
    # 'ops_maintenance' is the engine's venue-fatal trigger value (post PR-1
    # fail-safe fix; the prior 'live_engine' violated the CHECK constraint).
    kid, won = await ks.trigger(
        triggered_by="ops_maintenance",
        reason="binance_auth_revoked_mid_session",
        duration=timedelta(hours=4),
    )
    assert won is True
    assert len(sent) == 1
    assert "TRIGGERED" in sent[0]
    # Underscored reason/actor must survive verbatim (plain-text, no MarkdownV1).
    assert "binance_auth_revoked_mid_session" in sent[0]
    assert "ops_maintenance" in sent[0]
    await db.close()


async def test_clear_emits_operator_alert(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    sent: list[str] = []

    async def hook(message: str) -> None:
        sent.append(message)

    ks = KillSwitch(db, alert_hook=hook)
    await ks.trigger(
        triggered_by="daily_loss_cap", reason="x", duration=timedelta(hours=4)
    )
    sent.clear()
    await ks.clear(cleared_by="auto_expired")
    assert len(sent) == 1
    assert "CLEARED" in sent[0]
    assert "auto_expired" in sent[0]
    await db.close()


async def test_no_hook_means_no_alert_and_no_error(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)  # hookless — CLI / test path
    kid, won = await ks.trigger(
        triggered_by="manual", reason="x", duration=timedelta(hours=1)
    )
    assert won is True
    await ks.clear(cleared_by="manual")
    assert await ks.is_active() is None
    await db.close()


async def test_alert_hook_failure_does_not_break_trigger(tmp_path):
    """§12b resilience: the DB write commits before the alert, so a hook failure
    is logged but must NOT corrupt the kill-switch contract."""
    db = Database(tmp_path / "t.db")
    await db.initialize()

    async def failing_hook(message: str) -> None:
        raise RuntimeError("telegram down")

    ks = KillSwitch(db, alert_hook=failing_hook)
    kid, won = await ks.trigger(
        triggered_by="ops_maintenance",
        reason="binance_ip_banned",
        duration=timedelta(hours=4),
    )
    assert won is True
    assert (await ks.is_active()).kill_event_id == kid
    await db.close()


@pytest.mark.parametrize(
    "triggered_by", ["daily_loss_cap", "manual", "ops_maintenance"]
)
async def test_every_code_used_triggered_by_is_constraint_valid(tmp_path, triggered_by):
    """Regression guard for the PR-1 fail-safe bug: every triggered_by value the
    codebase passes (cli=manual, daily-loss=daily_loss_cap, engine=ops_maintenance)
    MUST satisfy the kill_events CHECK constraint. The prior engine value
    'live_engine' did not, so the venue-fatal kill raised IntegrityError instead
    of halting trading. If someone re-introduces an unlisted value, this fails."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)
    _id, won = await ks.trigger(
        triggered_by=triggered_by, reason="contract-check", duration=timedelta(hours=1)
    )
    assert won is True
    await db.close()


async def test_trigger_loser_does_not_alert(tmp_path):
    """The lost-race trigger must NOT alert — the winner already did."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    sent: list[str] = []

    async def hook(message: str) -> None:
        sent.append(message)

    ks = KillSwitch(db, alert_hook=hook)
    await asyncio.gather(
        ks.trigger(
            triggered_by="daily_loss_cap", reason="A", duration=timedelta(hours=4)
        ),
        ks.trigger(
            triggered_by="daily_loss_cap", reason="B", duration=timedelta(hours=4)
        ),
    )
    assert len(sent) == 1
    await db.close()
