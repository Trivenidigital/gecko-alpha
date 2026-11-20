"""Tests for KillSwitch (spec §6). compute_kill_duration G2 math tested first."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import structlog

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


# --------------------------------------------------------------------------
# LIVE-01: an expired-but-uncleared kill must NOT latch (belt-and-braces in
# is_active), and a latched auto-clear must alert the operator that the shadow
# soak was frozen (§12b). kill_events #1 latched 33 days in prod because
# auto_clear_if_expired had zero callers and is_active ignored killed_until.
# --------------------------------------------------------------------------


async def test_is_active_false_for_expired_uncleared_kill(tmp_path):
    """A kill whose killed_until is in the past reads as INACTIVE even when
    cleared_at is still NULL, so a missed auto_clear tick cannot latch it
    forever. The guard logs kill_switch_expired_uncleared (§12b visibility)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)
    # Negative duration → killed_until already in the past; never cleared.
    await ks.trigger(
        triggered_by="daily_loss_cap", reason="x", duration=timedelta(hours=-2)
    )
    with structlog.testing.capture_logs() as logs:
        state = await ks.is_active()
    assert state is None
    assert "kill_switch_expired_uncleared" in [le.get("event") for le in logs]
    # is_active is a pure read — the row stays uncleared (only clear/auto_clear
    # stamp cleared_at).
    cur = await db._conn.execute(
        "SELECT cleared_at FROM kill_events ORDER BY id DESC LIMIT 1"
    )
    assert (await cur.fetchone())[0] is None
    await db.close()


async def test_fresh_kill_still_blocks(tmp_path):
    """A not-yet-expired kill still reports active; auto_clear leaves it."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)
    kid, _ = await ks.trigger(
        triggered_by="daily_loss_cap", reason="x", duration=timedelta(hours=4)
    )
    assert (await ks.is_active()).kill_event_id == kid
    assert await ks.auto_clear_if_expired() is False
    assert (await ks.is_active()).kill_event_id == kid
    await db.close()


async def test_latched_auto_clear_emits_frozen_alert(tmp_path):
    """A kill latched >1h past expiry, when auto-cleared, sends the §12b
    plain-text 'soak was frozen' operator alert with dispatched/delivered
    logs (distinct from the standard CLEARED notification)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    sent: list[str] = []

    async def hook(message: str) -> None:
        sent.append(message)

    ks = KillSwitch(db, alert_hook=hook)
    kid, _ = await ks.trigger(
        triggered_by="daily_loss_cap", reason="x", duration=timedelta(hours=-5)
    )
    with structlog.testing.capture_logs() as logs:
        did_clear = await ks.auto_clear_if_expired()
    assert did_clear is True
    latched = [m for m in sent if "soak was frozen" in m]
    assert len(latched) == 1
    assert f"#{kid}" in latched[0]
    assert "auto-cleared" in latched[0]
    assert "latched" in latched[0]
    events = [le.get("event") for le in logs]
    assert "kill_switch_latched_auto_cleared" in events
    # §12b dispatched/delivered pair fired (traceable in journalctl).
    assert "kill_switch_alert_dispatched" in events
    assert "kill_switch_alert_delivered" in events
    await db.close()


async def test_recent_expiry_auto_clear_no_frozen_alert(tmp_path):
    """A kill that expired <1h ago (normal 4h kill picked up next tick) auto-
    clears WITHOUT the latched 'soak was frozen' alert."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    sent: list[str] = []

    async def hook(message: str) -> None:
        sent.append(message)

    ks = KillSwitch(db, alert_hook=hook)
    await ks.trigger(
        triggered_by="daily_loss_cap", reason="x", duration=timedelta(seconds=-1)
    )
    assert await ks.auto_clear_if_expired() is True
    assert not any("soak was frozen" in m for m in sent)
    await db.close()


# --- a dead pointer must not block the next kill -----------------------------
#
# live_control.active_kill_event_id is released only by clear(). Between a
# kill's expiry and the next auto-clear tick is_active() already answers
# "inactive" (orders flow), yet trigger()'s compare-and-swap still saw a
# non-NULL pointer and lost — so a fresh loss-cap breach could not halt
# trading. A pointer left on an already-cleared event was worse: nothing ever
# released it.


async def _point_control_at(db, *, killed_until, cleared_at=None):
    cur = await db._conn.execute(
        "INSERT INTO kill_events (triggered_by, reason, triggered_at, "
        "killed_until, cleared_at, cleared_by) VALUES (?, ?, ?, ?, ?, ?)",
        (
            "manual",
            "old",
            "2026-01-01T00:00:00+00:00",
            killed_until.isoformat(),
            cleared_at.isoformat() if cleared_at else None,
            "manual" if cleared_at else None,
        ),
    )
    old_id = cur.lastrowid
    await db._conn.execute(
        "UPDATE live_control SET active_kill_event_id = ? WHERE id = 1", (old_id,)
    )
    await db._conn.commit()
    return old_id


async def test_trigger_engages_over_an_expired_uncleared_kill(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)
    now = datetime.now(timezone.utc)
    old_id = await _point_control_at(db, killed_until=now - timedelta(minutes=5))
    assert await ks.is_active() is None  # orders are already flowing again

    new_id, won = await ks.trigger(
        triggered_by="daily_loss_cap", reason="breach", duration=timedelta(hours=4)
    )

    assert won is True
    assert new_id != old_id
    state = await ks.is_active()
    assert state is not None and state.kill_event_id == new_id
    # The expired event is closed out, not left dangling.
    cur = await db._conn.execute(
        "SELECT cleared_at, cleared_by FROM kill_events WHERE id = ?", (old_id,)
    )
    cleared_at, cleared_by = await cur.fetchone()
    assert cleared_at is not None
    assert cleared_by == "auto_expired"
    await db.close()


async def test_trigger_engages_over_a_pointer_to_a_cleared_event(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)
    now = datetime.now(timezone.utc)
    cleared = now - timedelta(hours=2)
    old_id = await _point_control_at(
        db, killed_until=now + timedelta(hours=6), cleared_at=cleared
    )
    assert await ks.is_active() is None

    new_id, won = await ks.trigger(
        triggered_by="manual", reason="again", duration=timedelta(hours=1)
    )

    assert won is True
    state = await ks.is_active()
    assert state is not None and state.kill_event_id == new_id
    # The already-cleared event keeps its original clear stamp.
    cur = await db._conn.execute(
        "SELECT cleared_at, cleared_by FROM kill_events WHERE id = ?", (old_id,)
    )
    assert tuple(await cur.fetchone()) == (cleared.isoformat(), "manual")
    await db.close()


async def test_trigger_still_loses_to_a_live_kill(tmp_path):
    """The release must not weaken the compare-and-swap for a kill that is
    genuinely in force."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)
    first_id, won = await ks.trigger(
        triggered_by="manual", reason="first", duration=timedelta(hours=1)
    )
    assert won is True

    second_id, won_again = await ks.trigger(
        triggered_by="daily_loss_cap", reason="second", duration=timedelta(hours=4)
    )

    assert won_again is False
    assert second_id == first_id
    cur = await db._conn.execute("SELECT COUNT(*) FROM kill_events")
    assert (await cur.fetchone())[0] == 1
    cur = await db._conn.execute(
        "SELECT cleared_at FROM kill_events WHERE id = ?", (first_id,)
    )
    assert (await cur.fetchone())[0] is None
    await db.close()


async def test_daily_loss_breach_engages_over_an_expired_uncleared_kill(tmp_path):
    """End to end through the loss-cap path that the stale pointer used to
    defeat silently (it returned False as if a kill were already in force)."""
    from scout.live.kill_switch import maybe_trigger_from_daily_loss

    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)
    now = datetime.now(timezone.utc)
    await _point_control_at(db, killed_until=now - timedelta(minutes=5))
    await db._conn.execute(
        "INSERT INTO paper_trades (token_id, symbol, name, chain, signal_type, "
        "signal_data, entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, "
        "sl_price, status, opened_at) VALUES ('t', 'T', 'T', 'eth', 'x', '{}', "
        "1, 1, 1, 1, 1, 1, 1, 'open', ?)",
        (now.isoformat(),),
    )
    cur = await db._conn.execute("SELECT last_insert_rowid()")
    paper_id = (await cur.fetchone())[0]
    cur = await db._conn.execute("PRAGMA table_info(shadow_trades)")
    cols = {r[1]: r for r in await cur.fetchall()}
    values = {
        "paper_trade_id": paper_id,
        "status": "closed_sl",
        "closed_at": now.isoformat(),
        "realized_pnl_usd": "-100000",
    }
    for name, (_cid, _n, ctype, notnull, default, pk) in cols.items():
        if name in values or default is not None or pk or not notnull:
            continue
        values[name] = 0 if "INT" in (ctype or "").upper() else "x"
    await db._conn.execute(
        f"INSERT INTO shadow_trades ({', '.join(values)}) "
        f"VALUES ({', '.join('?' for _ in values)})",
        tuple(values.values()),
    )
    await db._conn.commit()

    class _S:
        LIVE_DAILY_LOSS_CAP_USD = 50
        LIVE_TRADING_ENABLED = False

    assert await maybe_trigger_from_daily_loss(db, ks, _S()) is True
    assert await ks.is_active() is not None
    await db.close()


# --- an auto-clear must only clear the kill it judged expired ----------------
#
# auto_clear_if_expired reads "event E has expired" without the lock and then
# clears. Now that trigger() can replace an expired E with a fresh N, a tick
# that read E just before must not go on to clear N.


async def test_auto_clear_does_not_clear_a_kill_that_replaced_the_expired_one(
    tmp_path,
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    alerts: list[str] = []

    async def _hook(text):
        alerts.append(text)

    ks = KillSwitch(db, alert_hook=_hook)
    now = datetime.now(timezone.utc)
    old_id = await _point_control_at(db, killed_until=now - timedelta(minutes=5))
    stale = await ks._active_kill_state()  # what the tick read: E, expired
    assert stale.kill_event_id == old_id

    new_id, won = await ks.trigger(
        triggered_by="daily_loss_cap", reason="breach", duration=timedelta(hours=4)
    )
    assert won is True

    # The tick resumes with its stale read.
    async def _stale_read():
        return stale

    ks._active_kill_state = _stale_read
    cleared = await ks.auto_clear_if_expired()
    del ks._active_kill_state

    assert cleared is False
    state = await ks.is_active()
    assert state is not None and state.kill_event_id == new_id
    cur = await db._conn.execute(
        "SELECT cleared_at FROM kill_events WHERE id = ?", (new_id,)
    )
    assert (await cur.fetchone())[0] is None
    assert not any("CLEARED" in a.upper() for a in alerts)
    await db.close()


async def test_trigger_racing_an_auto_clear_tick_always_leaves_the_kill_engaged(
    tmp_path,
):
    for attempt in range(5):
        db = Database(tmp_path / f"t{attempt}.db")
        await db.initialize()
        ks = KillSwitch(db)
        now = datetime.now(timezone.utc)
        await _point_control_at(db, killed_until=now - timedelta(minutes=5))

        (new_id, won), _cleared = await asyncio.gather(
            ks.trigger(triggered_by="manual", reason="r", duration=timedelta(hours=4)),
            ks.auto_clear_if_expired(),
        )

        assert won is True
        state = await ks.is_active()
        assert state is not None and state.kill_event_id == new_id
        await db.close()


async def test_operator_clear_does_not_restamp_an_already_cleared_event(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)
    now = datetime.now(timezone.utc)
    cleared = now - timedelta(hours=2)
    old_id = await _point_control_at(
        db, killed_until=now + timedelta(hours=6), cleared_at=cleared
    )

    await ks.clear(cleared_by="manual")

    cur = await db._conn.execute(
        "SELECT active_kill_event_id FROM live_control WHERE id = 1"
    )
    assert (await cur.fetchone())[0] is None  # the dangling pointer is released
    cur = await db._conn.execute(
        "SELECT cleared_at FROM kill_events WHERE id = ?", (old_id,)
    )
    assert (await cur.fetchone())[0] == cleared.isoformat()  # stamp untouched
    await db.close()


async def test_a_failed_trigger_insert_releases_nothing(tmp_path):
    """An invalid triggered_by fails the INSERT's CHECK. The dead pointer must
    not have been released by then, or the release would ride out on some
    unrelated later commit with no kill and no log line that matches."""
    import sqlite3

    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)
    now = datetime.now(timezone.utc)
    old_id = await _point_control_at(db, killed_until=now - timedelta(minutes=5))

    with pytest.raises(sqlite3.IntegrityError):
        await ks.trigger(
            triggered_by="not_a_valid_source", reason="x", duration=timedelta(hours=1)
        )
    await db._conn.commit()  # an unrelated writer commits

    cur = await db._conn.execute(
        "SELECT active_kill_event_id FROM live_control WHERE id = 1"
    )
    assert (await cur.fetchone())[0] == old_id
    cur = await db._conn.execute(
        "SELECT cleared_at FROM kill_events WHERE id = ?", (old_id,)
    )
    assert (await cur.fetchone())[0] is None
    await db.close()


async def test_clear_reports_nothing_cleared_when_another_process_swapped_the_kill(
    tmp_path,
):
    """Another process (the kill CLI has its own connection) can engage a new
    kill between _clear's read of the pointer and its write. The write then
    matches nothing — and _clear must say so, not announce "cleared, live
    trading re-enabled" while the new kill is in force."""
    import sqlite3

    dbp = tmp_path / "t.db"
    db = Database(dbp)
    await db.initialize()
    alerts: list[str] = []

    async def _hook(text):
        alerts.append(text)

    ks = KillSwitch(db, alert_hook=_hook)
    now = datetime.now(timezone.utc)
    old_id = await _point_control_at(db, killed_until=now - timedelta(minutes=5))

    real_execute = db._conn.execute
    swapped: dict = {}

    async def _execute(sql, *args, **kwargs):
        cur = await real_execute(sql, *args, **kwargs)
        if "SELECT active_kill_event_id FROM live_control" in sql and not swapped:
            other = sqlite3.connect(dbp)
            c = other.execute(
                "INSERT INTO kill_events (triggered_by, reason, triggered_at, "
                "killed_until) VALUES ('manual', 'other process', ?, ?)",
                (now.isoformat(), (now + timedelta(hours=4)).isoformat()),
            )
            swapped["id"] = c.lastrowid
            other.execute(
                "UPDATE live_control SET active_kill_event_id = ? WHERE id = 1",
                (c.lastrowid,),
            )
            other.commit()
            other.close()
        return cur

    db._conn.execute = _execute
    try:
        cleared = await ks.auto_clear_if_expired()
    finally:
        db._conn.execute = real_execute

    assert cleared is False
    assert alerts == []
    # The write that matched nothing must not leave a write transaction open
    # on the shared connection: it would hold the database lock against every
    # other process until some unrelated commit came along.
    assert db._conn.in_transaction is False
    cur = await db._conn.execute(
        "SELECT active_kill_event_id FROM live_control WHERE id = 1"
    )
    assert (await cur.fetchone())[0] == swapped["id"]
    cur = await db._conn.execute(
        "SELECT cleared_at FROM kill_events WHERE id = ?", (old_id,)
    )
    assert (await cur.fetchone())[0] is None
    await db.close()


async def test_clear_returns_the_cleared_event_id_or_none(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)
    assert await ks.clear(cleared_by="manual") is None  # nothing active
    kid, _ = await ks.trigger(
        triggered_by="manual", reason="x", duration=timedelta(hours=1)
    )
    assert await ks.clear(cleared_by="manual") == kid
    assert await ks.is_active() is None
    await db.close()


# --- vector-review findings (PR #602) ----------------------------------------
# Some writers on the shared connection commit or roll back without
# db._txn_lock. trigger() awaits several statements while holding the lock;
# a foreign ROLLBACK landing between them undoes trigger's own pending writes.


def _rollback_once_after(db, needle: str):
    """Make the shared connection roll back once, right after the first
    statement containing ``needle`` — as an unlocked writer elsewhere would."""
    real_execute = db._conn.execute
    fired: dict = {}

    async def _execute(sql, *args, **kwargs):
        cur = await real_execute(sql, *args, **kwargs)
        if needle in sql and not fired:
            fired["at"] = sql
            await db._conn.rollback()
        return cur

    db._conn.execute = _execute
    return fired, real_execute


async def test_trigger_still_engages_when_a_foreign_rollback_undoes_the_release(
    tmp_path,
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)
    now = datetime.now(timezone.utc)
    await _point_control_at(db, killed_until=now - timedelta(minutes=5))

    fired, real_execute = _rollback_once_after(
        db, "UPDATE live_control SET active_kill_event_id = NULL"
    )
    try:
        new_id, won = await ks.trigger(
            triggered_by="daily_loss_cap", reason="breach", duration=timedelta(hours=4)
        )
    finally:
        db._conn.execute = real_execute

    assert fired, "the interference was not injected"
    assert won is True
    state = await ks.is_active()
    assert state is not None and state.kill_event_id == new_id
    assert db._conn.in_transaction is False
    await db.close()


async def test_trigger_still_engages_when_a_foreign_rollback_undoes_the_insert(
    tmp_path,
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)

    fired, real_execute = _rollback_once_after(db, "INSERT INTO kill_events")
    try:
        new_id, won = await ks.trigger(
            triggered_by="manual", reason="op", duration=timedelta(hours=1)
        )
    finally:
        db._conn.execute = real_execute

    assert fired
    assert won is True
    state = await ks.is_active()
    assert state is not None and state.kill_event_id == new_id
    assert db._conn.in_transaction is False
    # No orphan speculative rows: exactly the one live event.
    cur = await db._conn.execute("SELECT COUNT(*) FROM kill_events")
    assert (await cur.fetchone())[0] == 1
    await db.close()


async def test_trigger_judges_expiry_after_waiting_for_the_lock(tmp_path):
    """A trigger that queued behind the lock while the old kill expired must
    judge the old kill by the time it gets the lock, not the time it asked."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)
    now = datetime.now(timezone.utc)
    await _point_control_at(db, killed_until=now + timedelta(milliseconds=300))

    await db._txn_lock.acquire()
    try:
        pending = asyncio.ensure_future(
            ks.trigger(
                triggered_by="daily_loss_cap",
                reason="breach",
                duration=timedelta(hours=4),
            )
        )
        await asyncio.sleep(0.6)  # the old kill expires while trigger waits
    finally:
        db._txn_lock.release()
    new_id, won = await pending

    assert won is True
    state = await ks.is_active()
    assert state is not None and state.kill_event_id == new_id
    await db.close()


async def test_an_unreadable_killed_until_is_logged_when_it_blocks_a_trigger(
    tmp_path,
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)
    old_id = await _point_control_at(
        db, killed_until=datetime.now(timezone.utc) + timedelta(hours=1)
    )
    await db._conn.execute(
        "UPDATE kill_events SET killed_until = 'not-a-time' WHERE id = ?", (old_id,)
    )
    await db._conn.commit()

    with structlog.testing.capture_logs() as logs:
        winner, won = await ks.trigger(
            triggered_by="daily_loss_cap", reason="breach", duration=timedelta(hours=4)
        )

    assert won is False and winner == old_id  # left alone: cannot judge it
    unreadable = [
        e for e in logs if e.get("event") == "live_kill_dead_pointer_unreadable"
    ]
    assert len(unreadable) == 1
    assert unreadable[0]["log_level"] == "error"
    assert unreadable[0]["kill_event_id"] == old_id
    await db.close()


async def test_releasing_a_long_latched_kill_sends_the_latched_alert(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    alerts: list[str] = []

    async def _hook(text):
        alerts.append(text)

    ks = KillSwitch(db, alert_hook=_hook)
    old_id = await _point_control_at(
        db, killed_until=datetime.now(timezone.utc) - timedelta(hours=3)
    )

    with structlog.testing.capture_logs() as logs:
        await ks.trigger(
            triggered_by="daily_loss_cap", reason="breach", duration=timedelta(hours=4)
        )

    released = [e for e in logs if e.get("event") == "live_kill_dead_pointer_released"]
    assert released and released[0]["triggered_by"] == "manual"
    assert released[0]["latched_hours"] >= 2.9
    assert any(f"#{old_id}" in a and "latched" in a for a in alerts)
    assert any("TRIGGERED" in a for a in alerts)
    await db.close()


# --- vector re-review findings at 68417141 -----------------------------------


def _patch_commit(db, behaviour):
    """Replace the shared connection's commit. ``behaviour(n, real_commit)``
    is awaited for the n-th call (1-based)."""
    real_commit = db._conn.commit
    calls = {"n": 0}

    async def _commit():
        calls["n"] += 1
        await behaviour(calls["n"], real_commit)

    db._conn.commit = _commit
    return calls, real_commit


async def test_a_rollback_just_before_the_commit_does_not_yield_a_phantom_win(
    tmp_path,
):
    """A foreign rollback landing between the claim and the commit leaves
    nothing to commit. The call must not report a win for a kill that never
    landed; it must retry and engage for real."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    alerts: list[str] = []

    async def _hook(text):
        alerts.append(text)

    ks = KillSwitch(db, alert_hook=_hook)
    await _point_control_at(
        db, killed_until=datetime.now(timezone.utc) - timedelta(minutes=5)
    )

    async def _rollback_then_commit_once(n, real_commit):
        if n == 1:
            await db._conn.rollback()  # an unlocked writer elsewhere
        await real_commit()

    _calls, real_commit = _patch_commit(db, _rollback_then_commit_once)
    try:
        new_id, won = await ks.trigger(
            triggered_by="daily_loss_cap", reason="breach", duration=timedelta(hours=4)
        )
    finally:
        db._conn.commit = real_commit

    assert won is True
    state = await ks.is_active()
    assert state is not None and state.kill_event_id == new_id
    assert sum("TRIGGERED" in a for a in alerts) == 1
    await db.close()


async def test_a_winner_whose_commit_raises_is_still_reported_as_the_winner(
    tmp_path,
):
    """If the winning commit raises but the kill ends up engaged anyway, the
    caller must hear that it won and the operator must get the alert."""
    import sqlite3

    db = Database(tmp_path / "t.db")
    await db.initialize()
    alerts: list[str] = []

    async def _hook(text):
        alerts.append(text)

    ks = KillSwitch(db, alert_hook=_hook)

    async def _fail_first_commit(n, real_commit):
        if n == 1:
            raise sqlite3.OperationalError("database is locked")
        await real_commit()

    _calls, real_commit = _patch_commit(db, _fail_first_commit)
    try:
        new_id, won = await ks.trigger(
            triggered_by="manual", reason="op", duration=timedelta(hours=1)
        )
    finally:
        db._conn.commit = real_commit

    assert won is True
    state = await ks.is_active()
    assert state is not None and state.kill_event_id == new_id
    assert sum("TRIGGERED" in a for a in alerts) == 1
    await db.close()


async def test_a_kill_that_cannot_be_engaged_says_so_loudly(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    alerts: list[str] = []

    async def _hook(text):
        alerts.append(text)

    ks = KillSwitch(db, alert_hook=_hook)

    async def _always_rolled_back(n, real_commit):
        await db._conn.rollback()
        await real_commit()

    _calls, real_commit = _patch_commit(db, _always_rolled_back)
    try:
        with structlog.testing.capture_logs() as logs:
            active_id, won = await ks.trigger(
                triggered_by="daily_loss_cap",
                reason="breach",
                duration=timedelta(hours=4),
            )
    finally:
        db._conn.commit = real_commit

    assert won is False
    assert active_id is None  # nothing is in force; do not name a dead kill
    assert await ks.is_active() is None
    assert any("NOT ENGAGED" in a for a in alerts)
    unconfirmed = [e for e in logs if e.get("event") == "live_kill_trigger_unconfirmed"]
    assert unconfirmed and unconfirmed[0]["log_level"] == "error"
    await db.close()


async def test_the_latched_alert_survives_a_retried_attempt(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    alerts: list[str] = []

    async def _hook(text):
        alerts.append(text)

    ks = KillSwitch(db, alert_hook=_hook)
    old_id = await _point_control_at(
        db, killed_until=datetime.now(timezone.utc) - timedelta(hours=3)
    )
    fired, real_execute = _rollback_once_after(db, "INSERT INTO kill_events")
    try:
        _new_id, won = await ks.trigger(
            triggered_by="daily_loss_cap", reason="breach", duration=timedelta(hours=4)
        )
    finally:
        db._conn.execute = real_execute

    assert fired and won is True
    assert any(f"#{old_id}" in a and "latched" in a for a in alerts)
    cur = await db._conn.execute(
        "SELECT cleared_at, cleared_by FROM kill_events WHERE id = ?", (old_id,)
    )
    cleared_at, cleared_by = await cur.fetchone()
    assert cleared_at is not None and cleared_by == "auto_expired"
    await db.close()


# --- vector review findings at 896b65c8 --------------------------------------


async def test_a_twice_failed_commit_is_never_reported_as_a_win_on_trust(tmp_path):
    """If the commit and its retry both fail, the transaction is still open
    and the read-back sees the call's own UNCOMMITTED claim. That must not be
    reported as a win; the next attempt must make it durable, and the call
    must then report the kill it actually engaged."""
    import sqlite3

    db = Database(tmp_path / "t.db")
    await db.initialize()
    alerts: list[str] = []

    async def _hook(text):
        alerts.append(text)

    ks = KillSwitch(db, alert_hook=_hook)

    async def _fail_twice(n, real_commit):
        if n <= 2:
            raise sqlite3.OperationalError("database is locked")
        await real_commit()

    _calls, real_commit = _patch_commit(db, _fail_twice)
    try:
        kid, won = await ks.trigger(
            triggered_by="manual", reason="op", duration=timedelta(hours=1)
        )
    finally:
        db._conn.commit = real_commit

    assert db._conn.in_transaction is False
    assert won is True
    # Durable: survives a foreign rollback.
    await db._conn.rollback()
    state = await ks.is_active()
    assert state is not None and state.kill_event_id == kid
    assert sum("TRIGGERED" in a for a in alerts) == 1
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM kill_events WHERE cleared_at IS NULL"
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()


async def test_a_caller_error_is_not_hidden_when_another_kill_is_in_force(tmp_path):
    """A CHECK-violating triggered_by while a live kill exists: trading is
    halted either way, but the bad caller must surface at ERROR."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)
    live_id, _ = await ks.trigger(
        triggered_by="manual", reason="first", duration=timedelta(hours=1)
    )

    with structlog.testing.capture_logs() as logs:
        winner, won = await ks.trigger(
            triggered_by="not_a_valid_source", reason="x", duration=timedelta(hours=1)
        )

    assert (winner, won) == (live_id, False)
    errors = [e for e in logs if e.get("event") == "live_kill_trigger_error_while_lost"]
    assert errors and errors[0]["log_level"] == "error"
    await db.close()


# --- vector review findings at 78666322 --------------------------------------


async def _metric(db, name):
    cur = await db._conn.execute(
        "SELECT COALESCE(SUM(value), 0) FROM live_metrics_daily WHERE metric = ?",
        (name,),
    )
    return (await cur.fetchone())[0]


async def test_an_attempt_whose_commits_all_fail_is_retried_and_its_claim_kept(
    tmp_path,
):
    """Three failed commits close no transaction in attempt 1 (main commit +
    two tries in _commit_or_log_locked). Attempt 1 must not judge anything;
    attempt 2's commit makes attempt 1's claim durable, and that is this
    call's win."""
    import sqlite3

    db = Database(tmp_path / "t.db")
    await db.initialize()
    alerts: list[str] = []

    async def _hook(text):
        alerts.append(text)

    ks = KillSwitch(db, alert_hook=_hook)

    async def _fail_three(n, real_commit):
        if n <= 3:
            raise sqlite3.OperationalError("database is locked")
        await real_commit()

    _calls, real_commit = _patch_commit(db, _fail_three)
    try:
        kid, won = await ks.trigger(
            triggered_by="manual", reason="op", duration=timedelta(hours=1)
        )
    finally:
        db._conn.commit = real_commit

    assert won is True
    assert db._conn.in_transaction is False
    await db._conn.rollback()  # durable: survives a foreign rollback
    state = await ks.is_active()
    assert state is not None and state.kill_event_id == kid
    assert sum("TRIGGERED" in a for a in alerts) == 1
    await db.close()


async def test_when_every_commit_fails_the_operator_is_told_a_claim_is_pending(
    tmp_path,
):
    import sqlite3

    db = Database(tmp_path / "t.db")
    await db.initialize()
    alerts: list[str] = []

    async def _hook(text):
        alerts.append(text)

    ks = KillSwitch(db, alert_hook=_hook)

    async def _always_fail(n, real_commit):
        raise sqlite3.OperationalError("database is locked")

    _calls, real_commit = _patch_commit(db, _always_fail)
    try:
        with structlog.testing.capture_logs() as logs:
            with pytest.raises(sqlite3.OperationalError):
                await ks.trigger(
                    triggered_by="daily_loss_cap",
                    reason="breach",
                    duration=timedelta(hours=4),
                )
    finally:
        db._conn.commit = real_commit

    unconfirmed = [e for e in logs if e.get("event") == "live_kill_trigger_unconfirmed"]
    assert unconfirmed and unconfirmed[0]["pending_kill_event_ids"]
    not_engaged = [a for a in alerts if "NOT ENGAGED" in a]
    assert not_engaged and "pending" in not_engaged[0]
    assert "--status" in not_engaged[0]
    await db.close()


async def test_a_raised_trigger_failure_is_counted_exactly_once(tmp_path):
    """Counted inside trigger, so callers that do not count (the engine's
    venue-fatal kills, the CLI) are covered; the daily-loss helper must not
    count it again."""
    import sqlite3

    from scout.live.kill_switch import maybe_trigger_from_daily_loss

    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)
    with pytest.raises(sqlite3.IntegrityError):
        await ks.trigger(
            triggered_by="not_a_valid_source", reason="x", duration=timedelta(hours=1)
        )
    assert await _metric(db, "kill_trigger_errors") == 1

    class _Boom(KillSwitch):
        async def trigger(self, **kwargs):
            return await KillSwitch.trigger(
                self, **{**kwargs, "triggered_by": "not_a_valid_source"}
            )

    now = datetime.now(timezone.utc)
    await db._conn.execute(
        "INSERT INTO paper_trades (token_id, symbol, name, chain, signal_type, "
        "signal_data, entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, "
        "sl_price, status, opened_at) VALUES ('t', 'T', 'T', 'eth', 'x', '{}', "
        "1, 1, 1, 1, 1, 1, 1, 'open', ?)",
        (now.isoformat(),),
    )
    cur = await db._conn.execute("SELECT last_insert_rowid()")
    paper_id = (await cur.fetchone())[0]
    cur = await db._conn.execute("PRAGMA table_info(shadow_trades)")
    cols = {r[1]: r for r in await cur.fetchall()}
    values = {
        "paper_trade_id": paper_id,
        "status": "closed_sl",
        "closed_at": now.isoformat(),
        "realized_pnl_usd": "-100000",
    }
    for name, (_cid, _n, ctype, notnull, default, pk) in cols.items():
        if name in values or default is not None or pk or not notnull:
            continue
        values[name] = 0 if "INT" in (ctype or "").upper() else "x"
    await db._conn.execute(
        f"INSERT INTO shadow_trades ({', '.join(values)}) "
        f"VALUES ({', '.join('?' for _ in values)})",
        tuple(values.values()),
    )
    await db._conn.commit()

    class _S:
        LIVE_DAILY_LOSS_CAP_USD = 50
        LIVE_TRADING_ENABLED = False

    with pytest.raises(sqlite3.IntegrityError):
        await maybe_trigger_from_daily_loss(db, _Boom(db), _S())
    assert await _metric(db, "kill_trigger_errors") == 2  # one more, not two
    await db.close()


async def test_a_reused_event_id_is_not_mistaken_for_this_calls_kill(tmp_path):
    """A foreign rollback also rolls back AUTOINCREMENT's counter, so an id
    this call inserted (then lost) can be handed to someone else's row. That
    row is not this call's kill."""
    import sqlite3

    dbp = tmp_path / "t.db"
    db = Database(dbp)
    await db.initialize()
    alerts: list[str] = []

    async def _hook(text):
        alerts.append(text)

    ks = KillSwitch(db, alert_hook=_hook)
    real_execute = db._conn.execute
    state = {"done": False}

    async def _execute(sql, *args, **kwargs):
        cur = await real_execute(sql, *args, **kwargs)
        if "INSERT INTO kill_events" in sql and not state["done"]:
            state["done"] = True
            await db._conn.rollback()  # our row and the counter are undone
            other = sqlite3.connect(dbp)
            now = datetime.now(timezone.utc)
            c = other.execute(
                "INSERT INTO kill_events (triggered_by, reason, triggered_at, "
                "killed_until) VALUES ('manual', 'someone else', ?, ?)",
                ("2020-01-01T00:00:00+00:00", (now + timedelta(hours=2)).isoformat()),
            )
            state["other_id"] = c.lastrowid
            other.execute(
                "UPDATE live_control SET active_kill_event_id = ? WHERE id = 1",
                (c.lastrowid,),
            )
            other.commit()
            other.close()
        return cur

    db._conn.execute = _execute
    try:
        winner, won = await ks.trigger(
            triggered_by="daily_loss_cap", reason="breach", duration=timedelta(hours=4)
        )
    finally:
        db._conn.execute = real_execute

    assert won is False
    assert winner == state["other_id"]
    assert not any("TRIGGERED" in a for a in alerts)
    await db.close()


async def test_two_concurrent_triggers_never_both_win_when_an_id_is_reused(tmp_path):
    """Reviewer's sequence: A inserts id N, a foreign rollback undoes it (and
    AUTOINCREMENT's counter), B inserts and gets the same N, claims and wins.
    A's next attempt sees pointer N — which is B's kill, not A's."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    alerts: list[str] = []

    async def _hook(text):
        alerts.append(text)

    ks = KillSwitch(db, alert_hook=_hook)
    real_execute = db._conn.execute
    state = {"done": False}

    async def _execute(sql, *args, **kwargs):
        cur = await real_execute(sql, *args, **kwargs)
        if "INSERT INTO kill_events" in sql and not state["done"]:
            state["done"] = True
            await db._conn.rollback()  # an unlocked writer elsewhere
        return cur

    db._conn.execute = _execute
    try:
        results = await asyncio.gather(
            ks.trigger(triggered_by="manual", reason="A", duration=timedelta(hours=1)),
            ks.trigger(triggered_by="manual", reason="B", duration=timedelta(hours=1)),
        )
    finally:
        db._conn.execute = real_execute

    winners = [r for r in results if r[1]]
    assert len(winners) == 1, results
    assert results[0][0] == results[1][0]  # both name the one kill in force
    assert sum("TRIGGERED" in a for a in alerts) == 1
    state_now = await ks.is_active()
    assert state_now is not None and state_now.kill_event_id == winners[0][0]
    await db.close()


def test_attempt_timestamps_are_strictly_increasing():
    from scout.live.kill_switch import _unique_utc_now

    stamps = [_unique_utc_now() for _ in range(2000)]
    assert all(b > a for a, b in zip(stamps, stamps[1:]))


# --- vector review findings at 57a93fc0 --------------------------------------


async def test_a_pending_claim_made_durable_at_the_end_is_reported_as_the_win(
    tmp_path,
):
    """Every attempt's commits fail; then the database frees up. The claim
    left pending must be settled by trigger itself and reported as the win
    it is (TRIGGERED, won=True) — not engaged silently by a later commit
    after NOT ENGAGED went out."""
    import sqlite3

    db = Database(tmp_path / "t.db")
    await db.initialize()
    alerts: list[str] = []

    async def _hook(text):
        alerts.append(text)

    ks = KillSwitch(db, alert_hook=_hook)
    attempt_commits = 3 * 4  # comfortably more than all three attempts use

    async def _fail_then_work(n, real_commit):
        if n <= attempt_commits and _in_trigger_attempts["on"]:
            raise sqlite3.OperationalError("database is locked")
        await real_commit()

    _in_trigger_attempts = {"on": True}
    real_trigger_attempt = ks._trigger_attempt_locked
    calls = {"n": 0}

    async def _attempt(**kwargs):
        calls["n"] += 1
        result = await real_trigger_attempt(**kwargs)
        if calls["n"] == 3:  # the database frees up after the last attempt
            _in_trigger_attempts["on"] = False
        return result

    ks._trigger_attempt_locked = _attempt
    _calls, real_commit = _patch_commit(db, _fail_then_work)
    try:
        kid, won = await ks.trigger(
            triggered_by="daily_loss_cap", reason="breach", duration=timedelta(hours=4)
        )
    finally:
        db._conn.commit = real_commit

    assert won is True
    assert db._conn.in_transaction is False
    await db._conn.rollback()
    state = await ks.is_active()
    assert state is not None and state.kill_event_id == kid
    assert sum("TRIGGERED" in a for a in alerts) == 1
    assert not any("NOT ENGAGED" in a for a in alerts)
    cur = await db._conn.execute(
        "SELECT killed_until FROM kill_events WHERE id = ?", (kid,)
    )
    stored = datetime.fromisoformat((await cur.fetchone())[0]).isoformat()
    assert stored in [a for a in alerts if "TRIGGERED" in a][0]
    await db.close()


async def test_a_database_error_in_the_read_back_does_not_escape(tmp_path):
    """The read-back that judges an attempt fails during every attempt (a
    transient disk error), then recovers. The error must not escape
    uncounted; the call must settle and report the kill it engaged."""
    import sqlite3

    db = Database(tmp_path / "t.db")
    await db.initialize()
    alerts: list[str] = []

    async def _hook(text):
        alerts.append(text)

    ks = KillSwitch(db, alert_hook=_hook)
    real_execute = db._conn.execute
    failures = {"left": 3}

    async def _execute(sql, *args, **kwargs):
        if (
            sql.startswith("SELECT lc.active_kill_event_id, ke.id, ke.triggered_at")
            and failures["left"] > 0
        ):
            failures["left"] -= 1
            raise sqlite3.OperationalError("disk I/O error")
        return await real_execute(sql, *args, **kwargs)

    db._conn.execute = _execute
    try:
        kid, won = await ks.trigger(
            triggered_by="manual", reason="op", duration=timedelta(hours=1)
        )
    finally:
        db._conn.execute = real_execute

    assert failures["left"] == 0
    assert won is True
    state = await ks.is_active()
    assert state is not None and state.kill_event_id == kid
    assert sum("TRIGGERED" in a for a in alerts) == 1
    assert not any("NOT ENGAGED" in a for a in alerts)
    await db.close()


async def test_orphan_cleanup_never_deletes_another_writers_row_with_a_reused_id(
    tmp_path,
):
    """A foreign rollback right after our claim undoes our insert AND claim,
    and rewinds AUTOINCREMENT; another writer then inserts a row (a
    historical, cleared event - not the pointer) that gets our old id. Our
    cleanup of "our" leftover row by id alone would delete theirs."""
    import sqlite3

    dbp = tmp_path / "t.db"
    db = Database(dbp)
    await db.initialize()
    ks = KillSwitch(db)
    real_execute = db._conn.execute
    state = {"done": False}

    async def _execute(sql, *args, **kwargs):
        cur = await real_execute(sql, *args, **kwargs)
        if (
            "SET active_kill_event_id = ? WHERE id = 1 AND active_kill_event_id IS NULL"
            in sql
            and not state["done"]
        ):
            state["done"] = True
            await db._conn.rollback()
            other = sqlite3.connect(dbp)
            c = other.execute(
                "INSERT INTO kill_events (triggered_by, reason, triggered_at, "
                "killed_until, cleared_at, cleared_by) VALUES ('manual', "
                "'history', '2020-01-01T00:00:00+00:00', "
                "'2020-01-02T00:00:00+00:00', '2020-01-02T00:00:00+00:00', "
                "'manual')"
            )
            state["other_id"] = c.lastrowid
            other.commit()
            other.close()
        return cur

    db._conn.execute = _execute
    try:
        _kid, won = await ks.trigger(
            triggered_by="manual", reason="op", duration=timedelta(hours=1)
        )
    finally:
        db._conn.execute = real_execute

    assert state["done"] and won is True
    cur = await db._conn.execute(
        "SELECT reason FROM kill_events WHERE id = ?", (state["other_id"],)
    )
    row = await cur.fetchone()
    assert row is not None and row[0] == "history"
    await db.close()


async def test_trigger_stays_correct_on_a_clock_that_repeats_itself(
    tmp_path, monkeypatch
):
    """With a clock that returns the same instant every time, two concurrent
    triggers' rows would share triggered_at and ownership could collide.
    trigger must stamp attempts with _unique_utc_now, not the raw clock."""
    from scout.live import kill_switch as ks_module

    fixed = datetime(2030, 1, 1, tzinfo=timezone.utc)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(ks_module, "datetime", _FrozenDatetime)
    monkeypatch.setattr(ks_module, "_last_attempt_at", None)

    db = Database(tmp_path / "t.db")
    await db.initialize()
    alerts: list[str] = []

    async def _hook(text):
        alerts.append(text)

    ks = KillSwitch(db, alert_hook=_hook)
    real_execute = db._conn.execute
    state = {"done": False}

    async def _execute(sql, *args, **kwargs):
        cur = await real_execute(sql, *args, **kwargs)
        if "INSERT INTO kill_events" in sql and not state["done"]:
            state["done"] = True
            await db._conn.rollback()
        return cur

    db._conn.execute = _execute
    try:
        results = await asyncio.gather(
            ks.trigger(triggered_by="manual", reason="A", duration=timedelta(hours=1)),
            ks.trigger(triggered_by="manual", reason="B", duration=timedelta(hours=1)),
        )
    finally:
        db._conn.execute = real_execute

    assert sum(1 for r in results if r[1]) == 1, results
    assert sum("TRIGGERED" in a for a in alerts) == 1
    await db.close()


# --- vector review findings at fd0ced55 --------------------------------------


def _fail_sql(db, needle: str, *, times: int | None = None):
    import sqlite3

    real_execute = db._conn.execute
    left = {"n": times}

    async def _execute(sql, *args, **kwargs):
        if needle in sql and (left["n"] is None or left["n"] > 0):
            if left["n"] is not None:
                left["n"] -= 1
            raise sqlite3.OperationalError("disk I/O error")
        return await real_execute(sql, *args, **kwargs)

    db._conn.execute = _execute
    return real_execute


async def test_a_persistently_failing_read_back_still_reports_this_calls_kill(
    tmp_path,
):
    """The judging read fails every time, but the kill was committed and
    is_active() can see it. It is this call's kill: report the win."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    alerts: list[str] = []

    async def _hook(text):
        alerts.append(text)

    ks = KillSwitch(db, alert_hook=_hook)
    real_execute = _fail_sql(db, "ke.id, ke.triggered_at")
    try:
        kid, won = await ks.trigger(
            triggered_by="manual", reason="op", duration=timedelta(hours=1)
        )
    finally:
        db._conn.execute = real_execute

    assert won is True
    state = await ks.is_active()
    assert state is not None and state.kill_event_id == kid
    assert sum("TRIGGERED" in a for a in alerts) == 1
    await db.close()


async def test_when_no_read_of_the_switch_works_the_operator_is_told(tmp_path):
    import sqlite3

    db = Database(tmp_path / "t.db")
    await db.initialize()
    alerts: list[str] = []

    async def _hook(text):
        alerts.append(text)

    ks = KillSwitch(db, alert_hook=_hook)
    real_execute = _fail_sql(db, "FROM live_control")
    try:
        with structlog.testing.capture_logs() as logs:
            with pytest.raises(sqlite3.OperationalError):
                await ks.trigger(
                    triggered_by="manual", reason="op", duration=timedelta(hours=1)
                )
    finally:
        db._conn.execute = real_execute

    assert any("NOT ENGAGED" in a for a in alerts)
    assert any(e.get("event") == "live_kill_trigger_unconfirmed" for e in logs)
    await db.close()


async def test_the_announced_expiry_is_the_winning_claims_own(tmp_path):
    """Attempt 1's claim lands only later; the alert must give attempt 1's
    killed_until, not that of a later attempt."""
    import sqlite3

    db = Database(tmp_path / "t.db")
    await db.initialize()
    alerts: list[str] = []

    async def _hook(text):
        alerts.append(text)

    ks = KillSwitch(db, alert_hook=_hook)

    async def _fail_three(n, real_commit):
        if n <= 3:
            raise sqlite3.OperationalError("database is locked")
        await real_commit()

    _calls, real_commit = _patch_commit(db, _fail_three)
    try:
        kid, won = await ks.trigger(
            triggered_by="manual", reason="op", duration=timedelta(hours=1)
        )
    finally:
        db._conn.commit = real_commit

    assert won is True
    cur = await db._conn.execute(
        "SELECT killed_until FROM kill_events WHERE id = ?", (kid,)
    )
    stored = datetime.fromisoformat((await cur.fetchone())[0]).isoformat()
    triggered = [a for a in alerts if "TRIGGERED" in a]
    assert triggered and stored in triggered[0]
    await db.close()


async def test_the_metric_is_skipped_while_a_claim_is_pending(tmp_path):
    """Commits fail through every attempt and the final settle, then the
    database frees up just before the metric write. That write's commit would
    land the pending claim behind the NOT ENGAGED alert's back."""
    import sqlite3

    db = Database(tmp_path / "t.db")
    await db.initialize()
    alerts: list[str] = []

    async def _hook(text):
        alerts.append(text)

    ks = KillSwitch(db, alert_hook=_hook)
    locked = {"on": True}

    async def _locked_until_flag(n, real_commit):
        if locked["on"]:
            raise sqlite3.OperationalError("database is locked")
        await real_commit()

    real_present = ks._our_rows_present_locked

    async def _present(our_ids):
        result = await real_present(our_ids)
        locked["on"] = False  # the database frees up right after the settle
        return result

    ks._our_rows_present_locked = _present
    _calls, real_commit = _patch_commit(db, _locked_until_flag)
    try:
        with structlog.testing.capture_logs() as logs:
            with pytest.raises(sqlite3.OperationalError):
                await ks.trigger(
                    triggered_by="daily_loss_cap",
                    reason="breach",
                    duration=timedelta(hours=4),
                )
        still_open = db._conn.in_transaction
    finally:
        db._conn.commit = real_commit

    assert still_open is True  # nobody committed the pending claim
    assert any(e.get("event") == "live_kill_trigger_metric_skipped" for e in logs)
    assert any("NOT ENGAGED" in a and "pending" in a for a in alerts)
    await db.close()


async def test_a_failing_dead_pointer_check_is_a_retry_not_an_exception(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)
    live_id, _ = await ks.trigger(
        triggered_by="manual", reason="first", duration=timedelta(hours=1)
    )
    real_execute = _fail_sql(db, "ke.killed_until FROM live_control", times=1)
    try:
        winner, won = await ks.trigger(
            triggered_by="manual", reason="second", duration=timedelta(hours=1)
        )
    finally:
        db._conn.execute = real_execute
    assert (winner, won) == (live_id, False)
    await db.close()


async def test_a_foreign_transaction_after_the_settle_commit_does_not_hide_the_win(
    tmp_path,
):
    """The final settle's commit lands this call's claim; then a writer that
    does not take the lock opens a transaction on the shared connection. The
    call must judge by whether ITS commit succeeded, not by in_transaction."""
    import sqlite3

    db = Database(tmp_path / "t.db")
    await db.initialize()
    alerts: list[str] = []

    async def _hook(text):
        alerts.append(text)

    ks = KillSwitch(db, alert_hook=_hook)
    attempt_commits = 9  # three attempts x (commit + two tries) all fail

    async def _fail_then_foreign(n, real_commit):
        if n <= attempt_commits:
            raise sqlite3.OperationalError("database is locked")
        await real_commit()
        if n == attempt_commits + 1:
            # an unlocked writer starts a transaction right after our commit
            await db._conn.execute(
                "UPDATE live_metrics_daily SET value = value WHERE 0"
            )

    _calls, real_commit = _patch_commit(db, _fail_then_foreign)
    try:
        kid, won = await ks.trigger(
            triggered_by="manual", reason="op", duration=timedelta(hours=1)
        )
    finally:
        db._conn.commit = real_commit
    await db._conn.commit()  # end the foreign transaction

    assert won is True
    state = await ks.is_active()
    assert state is not None and state.kill_event_id == kid
    assert sum("TRIGGERED" in a for a in alerts) == 1
    assert not any("NOT ENGAGED" in a for a in alerts)
    await db.close()


# --- vector review findings at d45ad7db --------------------------------------


async def test_same_reason_triggers_with_failing_reads_never_both_win(tmp_path):
    """A's insert is undone and B reuses its id and wins; A's judging read
    keeps failing. A must not claim B's kill just because the reason and
    source match."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    alerts: list[str] = []

    async def _hook(text):
        alerts.append(text)

    ks_a = KillSwitch(db, alert_hook=_hook)
    ks_b = KillSwitch(db, alert_hook=_hook)
    real_execute = db._conn.execute
    state = {"rolled": False}

    async def _execute(sql, *args, **kwargs):
        import sqlite3

        if "ke.id, ke.triggered_at" in sql or "SELECT triggered_at FROM" in sql:
            raise sqlite3.OperationalError("disk I/O error")
        cur = await real_execute(sql, *args, **kwargs)
        if "INSERT INTO kill_events" in sql and not state["rolled"]:
            state["rolled"] = True
            await db._conn.rollback()
        return cur

    db._conn.execute = _execute
    try:
        results = await asyncio.gather(
            ks_a.trigger(
                triggered_by="daily_loss_cap",
                reason="breach",
                duration=timedelta(hours=4),
            ),
            ks_b.trigger(
                triggered_by="daily_loss_cap",
                reason="breach",
                duration=timedelta(hours=4),
            ),
            return_exceptions=True,
        )
    finally:
        db._conn.execute = real_execute

    wins = [r for r in results if isinstance(r, tuple) and r[1]]
    assert len(wins) <= 1, results
    assert sum("TRIGGERED" in a for a in alerts) == len(wins)
    await db.close()


async def test_daily_loss_caller_does_not_commit_a_pending_claim(tmp_path):
    """Every commit inside trigger fails, so the claim is pending in an open
    transaction and trigger deliberately skips the metric. The daily-loss
    caller must not then commit it with its own metric write."""
    import sqlite3

    from scout.live.kill_switch import maybe_trigger_from_daily_loss

    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)
    now = datetime.now(timezone.utc)
    await db._conn.execute(
        "INSERT INTO paper_trades (token_id, symbol, name, chain, signal_type, "
        "signal_data, entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, "
        "sl_price, status, opened_at) VALUES ('t', 'T', 'T', 'eth', 'x', '{}', "
        "1, 1, 1, 1, 1, 1, 1, 'open', ?)",
        (now.isoformat(),),
    )
    cur = await db._conn.execute("SELECT last_insert_rowid()")
    paper_id = (await cur.fetchone())[0]
    cur = await db._conn.execute("PRAGMA table_info(shadow_trades)")
    cols = {r[1]: r for r in await cur.fetchall()}
    values = {
        "paper_trade_id": paper_id,
        "status": "closed_sl",
        "closed_at": now.isoformat(),
        "realized_pnl_usd": "-100000",
    }
    for name, (_cid, _n, ctype, notnull, default, pk) in cols.items():
        if name in values or default is not None or pk or not notnull:
            continue
        values[name] = 0 if "INT" in (ctype or "").upper() else "x"
    await db._conn.execute(
        f"INSERT INTO shadow_trades ({', '.join(values)}) "
        f"VALUES ({', '.join('?' for _ in values)})",
        tuple(values.values()),
    )
    await db._conn.commit()

    locked = {"on": True}

    async def _locked(n, real_commit):
        if locked["on"]:
            raise sqlite3.OperationalError("database is locked")
        await real_commit()

    real_present = ks._our_rows_present_locked

    async def _present(our_ids):
        result = await real_present(our_ids)
        locked["on"] = False  # frees up once trigger has given up
        return result

    ks._our_rows_present_locked = _present
    _calls, real_commit = _patch_commit(db, _locked)

    class _S:
        LIVE_DAILY_LOSS_CAP_USD = 50
        LIVE_TRADING_ENABLED = False

    try:
        with pytest.raises(sqlite3.OperationalError):
            await maybe_trigger_from_daily_loss(db, ks, _S())
        still_open = db._conn.in_transaction
    finally:
        db._conn.commit = real_commit
    assert still_open is True
    await db.close()


async def test_a_win_judged_after_a_foreign_transaction_opens_needs_no_retry(
    tmp_path,
):
    """The attempt's own commit succeeded; an unlocked writer then opens a
    transaction. The attempt must still judge itself (one attempt), not
    retry because in_transaction reads True."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ks = KillSwitch(db)

    async def _commit_then_foreign(n, real_commit):
        await real_commit()
        if n == 1:
            await db._conn.execute(
                "UPDATE live_metrics_daily SET value = value WHERE 0"
            )

    _calls, real_commit = _patch_commit(db, _commit_then_foreign)
    try:
        with structlog.testing.capture_logs() as logs:
            _kid, won = await ks.trigger(
                triggered_by="manual", reason="op", duration=timedelta(hours=1)
            )
    finally:
        db._conn.commit = real_commit
    await db._conn.commit()

    assert won is True
    triggered = [e for e in logs if e.get("event") == "live_kill_event_triggered"]
    assert triggered and triggered[0]["attempts"] == 1
    await db.close()


async def test_a_reused_id_in_a_later_attempt_announces_that_attempts_expiry(
    tmp_path,
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    alerts: list[str] = []

    async def _hook(text):
        alerts.append(text)

    ks = KillSwitch(db, alert_hook=_hook)
    fired, real_execute = _rollback_once_after(db, "INSERT INTO kill_events")
    try:
        kid, won = await ks.trigger(
            triggered_by="manual", reason="op", duration=timedelta(hours=1)
        )
    finally:
        db._conn.execute = real_execute

    assert fired and won is True
    cur = await db._conn.execute(
        "SELECT killed_until FROM kill_events WHERE id = ?", (kid,)
    )
    stored = datetime.fromisoformat((await cur.fetchone())[0]).isoformat()
    triggered = [a for a in alerts if "TRIGGERED" in a]
    assert triggered and stored in triggered[0]
    await db.close()


# --- vector review findings at 46ff9af1 --------------------------------------


def _frozen_clock(monkeypatch):
    """Every datetime.now() in kill_switch returns one instant, so the only
    thing keeping attempt timestamps apart is _unique_utc_now."""
    from scout.live import kill_switch as ks_module

    fixed = datetime(2030, 1, 1, tzinfo=timezone.utc)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(ks_module, "datetime", _FrozenDatetime)
    monkeypatch.setattr(ks_module, "_last_attempt_at", None)


async def _colliding_expiry_race(tmp_path, *, fail_owned_read: bool):
    """A's insert is undone; B reuses the id and wins. The durations are
    chosen so that, by attempt time, A's and B's killed_until are equal —
    as two callers computing "until midnight" from their own clocks can."""
    import sqlite3

    db = Database(tmp_path / "t.db")
    await db.initialize()
    alerts: list[str] = []

    async def _hook(text):
        alerts.append(text)

    ks_a = KillSwitch(db, alert_hook=_hook)
    ks_b = KillSwitch(db, alert_hook=_hook)
    real_execute = db._conn.execute
    state = {"rolled": False}

    async def _execute(sql, *args, **kwargs):
        if fail_owned_read and "ke.id, ke.triggered_at" in sql:
            raise sqlite3.OperationalError("disk I/O error")
        cur = await real_execute(sql, *args, **kwargs)
        if "INSERT INTO kill_events" in sql and not state["rolled"]:
            state["rolled"] = True
            await db._conn.rollback()
        return cur

    db._conn.execute = _execute
    try:
        results = await asyncio.gather(
            ks_a.trigger(
                triggered_by="daily_loss_cap",
                reason="breach",
                duration=timedelta(hours=4),
            ),
            ks_b.trigger(
                triggered_by="daily_loss_cap",
                reason="breach",
                # B's first attempt is stamped 1us after A's: same expiry.
                duration=timedelta(hours=4) - timedelta(microseconds=1),
            ),
            return_exceptions=True,
        )
    finally:
        db._conn.execute = real_execute
    wins = [r for r in results if isinstance(r, tuple) and r[1]]
    await db.close()
    return results, wins, alerts


async def test_equal_expiries_from_different_callers_never_give_two_winners(
    tmp_path, monkeypatch
):
    _frozen_clock(monkeypatch)
    results, wins, alerts = await _colliding_expiry_race(tmp_path, fail_owned_read=True)
    assert len(wins) <= 1, results
    assert sum("TRIGGERED" in a for a in alerts) == len(wins)


async def test_a_successful_not_ours_read_is_not_overridden(tmp_path, monkeypatch):
    """When the committed-state ownership read works and says "not ours",
    the weaker is_active-based check must not run and override it — even if
    expiries happen to collide (uniqueness disabled here to isolate this)."""
    from scout.live import kill_switch as ks_module

    _frozen_clock(monkeypatch)
    monkeypatch.setattr(ks_module, "_unique_kill_until", lambda value: value)
    results, wins, alerts = await _colliding_expiry_race(
        tmp_path, fail_owned_read=False
    )
    assert len(wins) <= 1, results
    assert sum("TRIGGERED" in a for a in alerts) == len(wins)


def test_kill_expiries_issued_in_one_process_are_unique():
    from scout.live.kill_switch import _unique_kill_until

    base = datetime(2030, 1, 1, tzinfo=timezone.utc)
    issued = [_unique_kill_until(base) for _ in range(50)]
    assert len(set(issued)) == 50


async def test_a_naive_killed_until_in_the_final_step_still_alerts(tmp_path):
    """The attempts all end in retry (the judging read keeps failing) and the
    final is_active() raises TypeError, as a hand-edited killed_until with no
    timezone makes it do. That must be handled like any unreadable state."""
    import sqlite3

    db = Database(tmp_path / "t.db")
    await db.initialize()
    alerts: list[str] = []

    async def _hook(text):
        alerts.append(text)

    ks = KillSwitch(db, alert_hook=_hook)

    async def _naive_compare():
        raise TypeError("can't compare offset-naive and offset-aware datetimes")

    ks.is_active = _naive_compare
    real_execute = _fail_sql(db, "ke.id, ke.triggered_at")
    try:
        with structlog.testing.capture_logs() as logs:
            with pytest.raises(sqlite3.OperationalError):
                await ks.trigger(
                    triggered_by="manual", reason="op", duration=timedelta(hours=1)
                )
    finally:
        db._conn.execute = real_execute
    assert any(e.get("event") == "live_kill_trigger_state_unreadable" for e in logs)
    assert any("NOT ENGAGED" in a for a in alerts)
    await db.close()
