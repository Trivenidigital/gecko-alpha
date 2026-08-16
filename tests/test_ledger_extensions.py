"""C3 — ledger extensions: failure sites, parole denials, refresh failures.

Debts 17-19. Each site below previously left its evidence only in a log line,
on a box where journald retention collapses to minutes during the nightly
backup window — which is the whole reason the ledger exists.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from scout.db import Database
from scout.trading import combo_refresh, suppression

_counter = itertools.count()


class _StubSender:
    def __init__(self):
        self.calls = 0
        self.messages: list[str] = []

    async def __call__(self, *args):
        self.calls += 1
        texts = [a for a in args if isinstance(a, str)]
        assert len(texts) == 1, f"cannot identify the message body in {args!r}"
        self.messages.append(texts[0])


def _stub_all_senders(monkeypatch):
    perm, rev, retest = _StubSender(), _StubSender(), _StubSender()
    monkeypatch.setattr(combo_refresh, "_send_permanent_suppression_alert", perm)
    monkeypatch.setattr(combo_refresh, "_send_suppression_reversal_alert", rev)
    monkeypatch.setattr(combo_refresh, "_send_retest_incomplete_alert", retest)
    return perm, rev, retest


async def _events(db, **where):
    clause = " WHERE event_type != 'ledger_installed'"
    params: tuple = ()
    if where:
        clause += " AND " + " AND ".join(f"{k} = ?" for k in where)
        params = tuple(where.values())
    cur = await db._conn.execute(
        "SELECT event_type, combo_key, transition, delivery_result, state_json, "
        f"detail FROM alert_events{clause} ORDER BY id",
        params,
    )
    cols = "event_type combo_key transition delivery_result state_json detail".split()
    return [dict(zip(cols, r)) for r in await cur.fetchall()]


async def _seed_30d(db, combo_key, **cols):
    now = datetime.now(timezone.utc)
    base = dict(
        trades=25,
        wins=4,
        losses=21,
        total_pnl_usd=-100.0,
        avg_pnl_pct=-2.0,
        win_rate_pct=16.0,
        suppressed=1,
        suppressed_at=(now - timedelta(days=20)).isoformat(),
        parole_at=(now - timedelta(days=6)).isoformat(),
        parole_trades_remaining=0,
        refresh_failures=0,
        last_refreshed=(now - timedelta(hours=1)).isoformat(),
    )
    base.update(cols)
    names = ", ".join(base)
    marks = ", ".join("?" for _ in base)
    await db._conn.execute(
        f"INSERT INTO combo_performance (combo_key, window, {names}) "
        f"VALUES (?, '30d', {marks})",
        (combo_key, *base.values()),
    )
    await db._conn.commit()


# --- debt#18: parole admission denials ------------------------------------


async def test_parole_exhausted_denial_is_recorded(tmp_path, settings_factory):
    """The denial that explains a stalled retest after the fact. It left NO
    trace at all before this — not even a log line."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        await _seed_30d(db, "combo_a", parole_trades_remaining=0)
        allow, reason = await suppression.should_open(db, "combo_a", settings=s)
        assert (allow, reason) == (False, "parole_exhausted")

        rows = await _events(db, event_type="parole_denied")
        assert len(rows) == 1
        assert rows[0]["transition"] == "parole_exhausted"
        assert rows[0]["combo_key"] == "combo_a"
    finally:
        await db.close()


async def test_generation_change_denial_is_recorded(tmp_path, settings_factory):
    """The other denial: suppression was re-latched between the lock-free read
    and the locked reservation, so the validated window no longer exists."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        now = datetime.now(timezone.utc)
        await _seed_30d(db, "combo_a", parole_trades_remaining=5)

        # Move the window into the FUTURE between the fast-path read and the
        # locked re-read — the exact TOCTOU the branch exists for.
        real_execute = db._conn.execute
        moved = {"done": False}

        async def _relatch_on_begin(sql, *a, **k):
            if not moved["done"] and str(sql).strip().upper().startswith(
                "BEGIN IMMEDIATE"
            ):
                moved["done"] = True
                cur = await real_execute(sql, *a, **k)
                await real_execute(
                    "UPDATE combo_performance SET parole_at = ? "
                    "WHERE combo_key='combo_a' AND window='30d'",
                    ((now + timedelta(days=9)).isoformat(),),
                )
                return cur
            return await real_execute(sql, *a, **k)

        db._conn.execute = _relatch_on_begin
        try:
            allow, reason = await suppression.should_open(db, "combo_a", settings=s)
        finally:
            db._conn.execute = real_execute

        assert moved["done"], "the fixture never re-latched"
        assert (allow, reason) == (False, "suppressed")
        rows = await _events(db, event_type="parole_denied")
        assert len(rows) == 1
        assert rows[0]["transition"] == "generation_changed_before_reservation"
    finally:
        await db.close()


async def test_routine_suppressed_denial_writes_nothing(tmp_path, settings_factory):
    """The routine `suppressed` denial deliberately stays out of the ledger: it
    fires per candidate on every cycle for every suppressed combo, and it is
    already durable in signal_outcome_ledger.

    Excluded for VOLUME — not because the denials that DO land here are rare.
    That was the original premise and it was wrong: `parole_exhausted` is a
    LATCHED steady-state denial, not an occasional one. Once a combo latches,
    every dispatch attempt re-enters that branch indefinitely, which produced
    20,094 byte-identical rows in 17h across 3 combos in prod. It is now
    deduped on denial-state identity (combo + reason + generation) rather than
    written per attempt — see tests/test_parole_denied_dedup.py.

    So the contrast this test pins is volume-vs-volume, settled by counting:
    the routine denial is excluded at the writer, and the latched denial is
    collapsed at the key."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        now = datetime.now(timezone.utc)
        # Window still CLOSED -> the routine `suppressed` denial on the
        # lock-free fast path.
        await _seed_30d(
            db,
            "combo_a",
            parole_at=(now + timedelta(days=5)).isoformat(),
            parole_trades_remaining=5,
        )
        for _ in range(5):
            allow, reason = await suppression.should_open(db, "combo_a", settings=s)
            assert (allow, reason) == (False, "suppressed")
        assert await _events(db, event_type="parole_denied") == []
    finally:
        await db.close()


# --- debt#19: refresh failures --------------------------------------------


async def test_per_combo_refresh_failure_is_recorded(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        await _seed_30d(db, "combo_a", refresh_failures=0)

        real_commit = db._conn.commit
        calls = {"n": 0}

        async def _fail_first_commit():
            calls["n"] += 1
            if calls["n"] == 1:
                await db._conn.rollback()
                raise aiosqlite.OperationalError("disk I/O error")
            return await real_commit()

        db._conn.commit = _fail_first_commit
        try:
            ok = await combo_refresh.refresh_combo(db, "combo_a", s)
        finally:
            db._conn.commit = real_commit
        assert ok is False

        rows = await _events(db, transition="refresh_failed")
        assert len(rows) == 1
        assert rows[0]["event_type"] == "marker_anomaly"
        assert rows[0]["delivery_result"] == "error:OperationalError"
        assert "disk I/O error" in rows[0]["detail"]
    finally:
        await db.close()


async def test_chronic_failure_is_recorded_once_per_crossing(
    tmp_path, settings_factory
):
    """Gated on EQUALITY with the threshold. The chronic log in `refresh_all`
    fires every run for as long as the combo stays above it; a ledger row per
    run would be exactly the steady-state noise this ledger avoids."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        threshold = s.FEEDBACK_CHRONIC_FAILURE_THRESHOLD
        # One short of the threshold, so the next failure is the crossing.
        await _seed_30d(db, "combo_a", refresh_failures=threshold - 1)

        real_commit = db._conn.commit

        async def _always_fail_refresh_commit():
            # Fail the refresh's own commit; the counter commit that follows
            # is a separate call and must succeed.
            if getattr(_always_fail_refresh_commit, "armed", True):
                _always_fail_refresh_commit.armed = False
                await db._conn.rollback()
                raise aiosqlite.OperationalError("disk I/O error")
            return await real_commit()

        for run in range(3):
            _always_fail_refresh_commit.armed = True
            db._conn.commit = _always_fail_refresh_commit
            try:
                await combo_refresh.refresh_combo(db, "combo_a", s)
            finally:
                db._conn.commit = real_commit

        crossings = await _events(db, transition="chronic_failure")
        assert len(crossings) == 1, (
            f"expected ONE crossing row across 3 failing runs, got " f"{len(crossings)}"
        )
        state = crossings[0]["state_json"]
        assert f'"refresh_failures": {threshold}' in state
        # And the per-run failure rows are still one per run.
        assert len(await _events(db, transition="refresh_failed")) == 3
    finally:
        await db.close()


# --- debt#17: the four marker/write failure sites -------------------------


async def test_perm_marker_update_failure_is_recorded(
    tmp_path, settings_factory, monkeypatch
):
    """The page went out and the dedup marker did not land, so the next run
    pages again. Without this row that duplicate is unexplainable after the
    fact — the row itself carries no trace of the failed write."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        _stub_all_senders(monkeypatch)
        await _seed_30d(db, "combo_a")

        real_execute = db._conn.execute

        async def _fail_marker_update(sql, *a, **k):
            if "perm_suppression_alerted_at = ?" in str(sql):
                raise aiosqlite.OperationalError("disk I/O error")
            return await real_execute(sql, *a, **k)

        db._conn.execute = _fail_marker_update
        try:
            window_cutoff = (
                datetime.now(timezone.utc) - timedelta(days=30)
            ).isoformat()
            await combo_refresh._process_permanent_suppression(db, s, window_cutoff)
        finally:
            db._conn.execute = real_execute

        rows = await _events(
            db, transition="permanent_suppression_marker_update_failed"
        )
        assert len(rows) == 1
        assert rows[0]["event_type"] == "marker_anomaly"
        assert rows[0]["delivery_result"] == "error:OperationalError"
    finally:
        await db.close()


async def test_retest_marker_update_failure_is_recorded(
    tmp_path, settings_factory, monkeypatch
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        _stub_all_senders(monkeypatch)
        await _seed_30d(db, "combo_a", parole_trades_remaining=5)

        real_execute = db._conn.execute

        async def _fail_marker_update(sql, *a, **k):
            if "retest_incomplete_alerted_at = ?" in str(sql):
                raise aiosqlite.OperationalError("disk I/O error")
            return await real_execute(sql, *a, **k)

        db._conn.execute = _fail_marker_update
        try:
            await combo_refresh._process_retest_terminal_incomplete(db, s)
        finally:
            db._conn.execute = real_execute

        rows = await _events(db, transition="retest_incomplete_marker_update_failed")
        assert len(rows) == 1
        assert rows[0]["delivery_result"] == "error:OperationalError"
    finally:
        await db.close()
