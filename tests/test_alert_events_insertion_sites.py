"""F3 — one test per `alert_events` insertion site.

Each test asserts BOTH that the row exists AND how many there are. A bare
"something was written" assertion passes when the writer fires twice, or fires
on a branch that should have stayed silent, and both of those are the failure
modes a control-plane ledger cannot tolerate: an event that did not happen is
indistinguishable from one that did once it is durable.

The Telegram senders are monkeypatched at the module boundary (the same
`_StubSender` shape the existing combo-refresh tests use), so no test here
touches the network.
"""

from __future__ import annotations

import itertools
import json
import time
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from scout.db import Database
from scout.trading import alert_events, combo_refresh, suppression

_counter = itertools.count()


# --- helpers ---------------------------------------------------------------


class _StubSender:
    """Records sends without importing aiohttp (Windows OpenSSL Applink).

    The three senders do not agree on argument order —
    `_send_permanent_suppression_alert(settings, message)` and
    `_send_retest_incomplete_alert(message, settings)` are mirror images — so
    the message is picked out by type rather than by position. Guessing
    positionally records the Settings object and every payload-hash assertion
    then compares two wrong values."""

    def __init__(self):
        self.calls = 0
        self.messages: list[str] = []

    async def __call__(self, *args):
        self.calls += 1
        texts = [a for a in args if isinstance(a, str)]
        assert len(texts) == 1, f"cannot identify the message body in {args!r}"
        self.messages.append(texts[0])


class _BoomSender:
    def __init__(self, exc=None):
        self.exc = exc or RuntimeError("telegram send failed status=400")

    async def __call__(self, *args):
        raise self.exc


def _stub_all_senders(monkeypatch):
    """Silence every combo_refresh sender at once. Returns the three stubs."""
    perm, rev, retest = _StubSender(), _StubSender(), _StubSender()
    monkeypatch.setattr(combo_refresh, "_send_permanent_suppression_alert", perm)
    monkeypatch.setattr(combo_refresh, "_send_suppression_reversal_alert", rev)
    monkeypatch.setattr(combo_refresh, "_send_retest_incomplete_alert", retest)
    return perm, rev, retest


async def _events(db, **where) -> list[dict]:
    clause = ""
    params: tuple = ()
    if where:
        clause = " WHERE " + " AND ".join(f"{k} = ?" for k in where)
        params = tuple(where.values())
    cur = await db._conn.execute(
        "SELECT event_type, combo_key, signal_type, alert_source, transition, "
        "detected_at, delivery_result, retry, payload_hash, state_json, detail "
        f"FROM alert_events{clause} ORDER BY id",
        params,
    )
    cols = (
        "event_type combo_key signal_type alert_source transition detected_at "
        "delivery_result retry payload_hash state_json detail"
    ).split()
    return [dict(zip(cols, r)) for r in await cur.fetchall()]


async def _insert_trade(
    db,
    combo_key: str,
    pnl_usd: float,
    closed_at: datetime,
    *,
    status: str = "closed_tp",
    opened_at: datetime | None = None,
):
    opened = (opened_at or closed_at - timedelta(hours=1)).isoformat()
    await db._conn.execute(
        "INSERT INTO paper_trades "
        "(token_id, symbol, name, chain, signal_type, signal_data, "
        " entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price, "
        " status, pnl_usd, pnl_pct, opened_at, closed_at, signal_combo) "
        "VALUES (?, 'S', 'N', 'coingecko', 'volume_spike', '{}', "
        " 1.0, 100.0, 100.0, 20.0, 10.0, 1.2, 0.9, ?, ?, ?, ?, ?, ?)",
        (
            f"tok_{combo_key}_{next(_counter)}",
            status,
            pnl_usd,
            5.0 if pnl_usd > 0 else -3.0,
            opened,
            closed_at.isoformat(),
            combo_key,
        ),
    )
    await db._conn.commit()


async def _seed_30d_row(db, combo_key, **cols):
    base = dict(
        trades=25,
        wins=4,
        losses=21,
        total_pnl_usd=-100.0,
        avg_pnl_pct=-2.0,
        win_rate_pct=16.0,
        suppressed=1,
        suppressed_at=(datetime.now(timezone.utc) - timedelta(days=20)).isoformat(),
        parole_at=(datetime.now(timezone.utc) - timedelta(days=6)).isoformat(),
        parole_trades_remaining=0,
        last_refreshed=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
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


# --- suppression_transition (combo_refresh._refresh_combo_locked) ----------


async def test_first_write_latch_is_recorded(tmp_path, settings_factory):
    """A combo suppressed on its very first 30d write. Before F3 this branch
    left nothing behind but a mutated column."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        now = datetime.now(timezone.utc)
        for _ in range(20):
            await _insert_trade(db, "combo_a", -5, now - timedelta(days=2))
        assert await combo_refresh.refresh_combo(db, "combo_a", s)

        rows = await _events(db, event_type="suppression_transition")
        assert len(rows) == 1
        assert rows[0]["transition"] == "first_write_latch"
        assert rows[0]["combo_key"] == "combo_a"
        state = json.loads(rows[0]["state_json"])
        assert state["before_suppressed"] is None, "no prior row -> explicit null"
        assert state["after_suppressed"] == 1
        assert state["after_parole_trades_remaining"] == s.FEEDBACK_PAROLE_RETEST_TRADES
    finally:
        await db.close()


async def test_initial_latch_from_an_unsuppressed_row_is_recorded(
    tmp_path, settings_factory
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        now = datetime.now(timezone.utc)
        await _seed_30d_row(
            db,
            "combo_a",
            suppressed=0,
            suppressed_at=None,
            parole_at=None,
            parole_trades_remaining=None,
            trades=1,
            wins=1,
            losses=0,
            win_rate_pct=100.0,
        )
        for _ in range(20):
            await _insert_trade(db, "combo_a", -5, now - timedelta(days=2))
        assert await combo_refresh.refresh_combo(db, "combo_a", s)

        rows = await _events(db, event_type="suppression_transition")
        assert len(rows) == 1
        assert rows[0]["transition"] == "initial_latch"
        state = json.loads(rows[0]["state_json"])
        assert state["before_suppressed"] == 0
        assert state["after_suppressed"] == 1
    finally:
        await db.close()


async def test_no_transition_writes_no_row(tmp_path, settings_factory):
    """An unsuppressed combo that stays unsuppressed is NOT a transition. If
    this ever writes a row, every refresh of every healthy combo does too."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        now = datetime.now(timezone.utc)
        for _ in range(20):
            await _insert_trade(db, "combo_a", +5, now - timedelta(days=2))
        assert await combo_refresh.refresh_combo(db, "combo_a", s)
        assert await combo_refresh.refresh_combo(db, "combo_a", s)
        assert await _events(db, event_type="suppression_transition") == []
    finally:
        await db.close()


async def test_rearm_with_no_stamped_marker_writes_no_row(tmp_path, settings_factory):
    """The re-arm UPDATE fires for EVERY unsuppressed combo on EVERY refresh.
    Keying the ledger row on that UPDATE would write one row per healthy combo
    per run and bury the real transitions. Only a re-arm that actually cleared
    a stamped marker counts."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        now = datetime.now(timezone.utc)
        await _seed_30d_row(
            db,
            "combo_a",
            suppressed=0,
            suppressed_at=None,
            parole_at=None,
            parole_trades_remaining=None,
            trades=1,
            wins=1,
            losses=0,
            win_rate_pct=100.0,
            retest_incomplete_alerted_at=None,
        )
        for _ in range(20):
            await _insert_trade(db, "combo_a", +5, now - timedelta(days=2))
        for _ in range(3):
            assert await combo_refresh.refresh_combo(db, "combo_a", s)
        assert await _events(db, event_type="suppression_transition") == []
    finally:
        await db.close()


async def test_clear_and_page_rearm_are_recorded_as_separate_rows(
    tmp_path, settings_factory
):
    """A completed, recovered retest clears suppression AND re-arms the stuck
    page. Two independent facts -> two rows; collapsing them would make "did
    this generation get its page back?" unanswerable.

    The seeded `retest_incomplete_alerted_at` is what makes the re-arm a real
    event: without a stamped marker there is nothing to re-arm, and the ledger
    correctly stays silent (see the sibling test below)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        now = datetime.now(timezone.utc)
        parole_at = (now - timedelta(days=3)).isoformat()
        await _seed_30d_row(
            db,
            "combo_a",
            parole_at=parole_at,
            retest_incomplete_alerted_at=(now - timedelta(days=1)).isoformat(),
        )
        # 5 valid closes admitted under this parole, all winners.
        for _ in range(5):
            await _insert_trade(
                db,
                "combo_a",
                +10,
                now - timedelta(days=1),
                opened_at=now - timedelta(days=2),
            )
        assert await combo_refresh.refresh_combo(db, "combo_a", s)

        rows = await _events(db, event_type="suppression_transition")
        assert len(rows) == 2
        assert [r["transition"] for r in rows] == ["clear", "page_rearm"]
        state = json.loads(rows[0]["state_json"])
        assert state["before_suppressed"] == 1
        assert state["after_suppressed"] == 0
    finally:
        await db.close()


async def test_relatch_is_recorded(tmp_path, settings_factory):
    """Completed retest that FAILED -> re-suppress with a fresh parole."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        now = datetime.now(timezone.utc)
        parole_at = (now - timedelta(days=3)).isoformat()
        await _seed_30d_row(
            db,
            "combo_a",
            parole_at=parole_at,
            retest_incomplete_alerted_at=(now - timedelta(days=1)).isoformat(),
        )
        for _ in range(5):
            await _insert_trade(
                db,
                "combo_a",
                -10,
                now - timedelta(days=1),
                opened_at=now - timedelta(days=2),
            )
        assert await combo_refresh.refresh_combo(db, "combo_a", s)

        rows = await _events(db, event_type="suppression_transition")
        transitions = [r["transition"] for r in rows]
        assert transitions.count("relatch") == 1
        assert transitions.count("page_rearm") == 1
        assert len(rows) == 2
    finally:
        await db.close()


async def test_terminal_incomplete_hold_is_recorded_with_the_held_label(
    tmp_path, settings_factory
):
    """The classifier names the STATE (`terminal_incomplete`); the ledger names
    what the refresh DID about it (`terminal_incomplete_held`)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        now = datetime.now(timezone.utc)
        parole_at = (now - timedelta(days=3)).isoformat()
        await _seed_30d_row(db, "combo_a", parole_at=parole_at)
        # Slots exhausted (remaining=0), nothing open, fewer than 5 valid closes.
        for _ in range(2):
            await _insert_trade(
                db,
                "combo_a",
                -10,
                now - timedelta(days=1),
                opened_at=now - timedelta(days=2),
            )
        assert await combo_refresh.refresh_combo(db, "combo_a", s)

        rows = await _events(db, event_type="suppression_transition")
        assert len(rows) == 1
        assert rows[0]["transition"] == "terminal_incomplete_held"
    finally:
        await db.close()


async def test_transition_row_is_discarded_when_the_refresh_rolls_back(
    tmp_path, settings_factory, monkeypatch
):
    """`managed_txn=True` means the event and the state change share a fate. A
    ledger row that survived a rolled-back refresh would assert a suppression
    that never happened."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        now = datetime.now(timezone.utc)
        for _ in range(20):
            await _insert_trade(db, "combo_a", -5, now - timedelta(days=2))

        real_commit = db._conn.commit
        real_rollback = db._conn.rollback
        calls = {"n": 0}

        async def _fail_first_commit():
            calls["n"] += 1
            if calls["n"] == 1:
                # Discard the transaction, then report the failure — the shape
                # a real commit failure leaves behind. Without the rollback the
                # error handler's own commit would land the work this test says
                # was thrown away, and the assertion below would be vacuous.
                await real_rollback()
                raise aiosqlite.OperationalError("disk I/O error")
            return await real_commit()

        monkeypatch.setattr(db._conn, "commit", _fail_first_commit)
        ok = await combo_refresh.refresh_combo(db, "combo_a", s)
        monkeypatch.undo()
        assert ok is False

        assert await _events(db, event_type="suppression_transition") == []
        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM combo_performance WHERE combo_key='combo_a'"
        )
        (rows,) = await cur.fetchone()
        assert rows == 0, "fixture did not actually roll the refresh back"
    finally:
        await db.close()


# --- refresh_completed heartbeat ------------------------------------------


async def test_refresh_all_writes_exactly_one_heartbeat(
    tmp_path, settings_factory, monkeypatch
):
    """Exactly one per run, unconditionally — including a run with nothing to
    do. That unconditionality is what makes the §12a watchdog meaningful."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        _stub_all_senders(monkeypatch)
        await combo_refresh.refresh_all(db, s)
        rows = await _events(db, event_type="refresh_completed")
        assert len(rows) == 1
        state = json.loads(rows[0]["state_json"])
        assert state["refreshed"] == 0
        assert state["combos_enumerated"] == 0

        await combo_refresh.refresh_all(db, s)
        assert len(await _events(db, event_type="refresh_completed")) == 2
    finally:
        await db.close()


async def test_heartbeat_carries_the_summary_counts(
    tmp_path, settings_factory, monkeypatch
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        _stub_all_senders(monkeypatch)
        now = datetime.now(timezone.utc)
        for _ in range(20):
            await _insert_trade(db, "combo_a", -5, now - timedelta(days=2))
        await combo_refresh.refresh_all(db, s)

        rows = await _events(db, event_type="refresh_completed")
        assert len(rows) == 1
        state = json.loads(rows[0]["state_json"])
        assert state["refreshed"] == 1
        assert state["failed"] == 0
        assert state["combos_enumerated"] == 1
    finally:
        await db.close()


# --- parole slot spend / refund (suppression.py) ---------------------------


async def test_parole_slot_spend_is_recorded_in_the_same_txn(
    tmp_path, settings_factory
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        parole_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        await _seed_30d_row(
            db, "combo_a", parole_at=parole_at, parole_trades_remaining=5
        )

        allow, reason = await suppression.should_open(db, "combo_a", settings=s)
        assert (allow, reason) == (True, suppression.PAROLE_RETEST_REASON)

        rows = await _events(db, event_type="parole_slot_spent")
        assert len(rows) == 1
        assert rows[0]["combo_key"] == "combo_a"
        assert rows[0]["transition"] == "parole_retest"
        state = json.loads(rows[0]["state_json"])
        assert state["parole_trades_remaining_before"] == 5
        assert state["parole_trades_remaining_after"] == 4
    finally:
        await db.close()


async def test_denied_admission_writes_no_spend_row(tmp_path, settings_factory):
    """A denial spends no slot, so it must record no spend. Otherwise the
    ledger over-counts admissions against a parole generation."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        parole_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        await _seed_30d_row(
            db, "combo_a", parole_at=parole_at, parole_trades_remaining=0
        )
        allow, reason = await suppression.should_open(db, "combo_a", settings=s)
        assert (allow, reason) == (False, "parole_exhausted")
        assert await _events(db, event_type="parole_slot_spent") == []
    finally:
        await db.close()


async def test_refund_records_ok_and_credits_the_slot(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        parole_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        await _seed_30d_row(
            db, "combo_a", parole_at=parole_at, parole_trades_remaining=5
        )

        async with suppression.parole_reservation(db, "combo_a", settings=s) as res:
            assert res.slot_taken is True
            # No confirm() -> the slot comes back on exit.

        rows = await _events(db, event_type="parole_slot_refunded")
        assert len(rows) == 1
        assert rows[0]["delivery_result"] == "ok"
        state = json.loads(rows[0]["state_json"])
        assert state["parole_trades_remaining_before"] == 4
        assert state["rows_changed"] == 1

        cur = await db._conn.execute(
            "SELECT parole_trades_remaining FROM combo_performance "
            "WHERE combo_key='combo_a' AND window='30d'"
        )
        (remaining,) = await cur.fetchone()
        assert remaining == 5
    finally:
        await db.close()


async def test_refund_into_a_replaced_generation_records_stale_generation(
    tmp_path, settings_factory
):
    """The generation guard is the whole point of the refund path — a stale
    refund would grant an extra admission against a FRESH lock period."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        parole_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        await _seed_30d_row(
            db, "combo_a", parole_at=parole_at, parole_trades_remaining=5
        )

        async with suppression.parole_reservation(db, "combo_a", settings=s) as res:
            assert res.slot_taken is True
            # combo_refresh re-latches under us: a NEW generation.
            await db._conn.execute(
                "UPDATE combo_performance SET suppressed_at = ?, parole_at = ? "
                "WHERE combo_key='combo_a' AND window='30d'",
                ("2099-01-01T00:00:00+00:00", "2099-02-01T00:00:00+00:00"),
            )
            await db._conn.commit()

        rows = await _events(db, event_type="parole_slot_refunded")
        assert len(rows) == 1
        assert rows[0]["delivery_result"] == "stale_generation"
        assert json.loads(rows[0]["state_json"])["rows_changed"] == 0

        cur = await db._conn.execute(
            "SELECT parole_trades_remaining FROM combo_performance "
            "WHERE combo_key='combo_a' AND window='30d'"
        )
        (remaining,) = await cur.fetchone()
        assert remaining == 4, "the slot was credited to the replacement generation"
    finally:
        await db.close()


async def test_refund_uses_statement_rowcount_not_connection_total_changes(
    tmp_path, settings_factory, monkeypatch
):
    """The refund now reads THIS statement's affected-row count.
    `Connection.total_changes` is connection-wide and cumulative, so ANY other
    write landing between the before/after reads made a generation-bound UPDATE
    that matched ZERO rows report success — crediting a slot to a replacement
    generation, which over-admits against a fresh lock period.

    The interference is injected in the exact window the old formulation was
    exposed to: immediately AFTER the generation-bound UPDATE returns and before
    its result is read. Under `total_changes` the delta would be 1 and this
    refund would report success; under `cur.rowcount` it stays 0."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        parole_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        await _seed_30d_row(
            db, "combo_a", parole_at=parole_at, parole_trades_remaining=5
        )
        await _seed_30d_row(
            db, "combo_b", parole_at=parole_at, parole_trades_remaining=5
        )

        async with suppression.parole_reservation(db, "combo_a", settings=s) as res:
            assert res.slot_taken is True
            # combo_refresh moves the generation under us -> the refund's
            # generation-bound UPDATE will match zero rows.
            await db._conn.execute(
                "UPDATE combo_performance SET parole_at = ? "
                "WHERE combo_key='combo_a' AND window='30d'",
                ("2099-02-01T00:00:00+00:00",),
            )
            await db._conn.commit()

            real_execute = db._conn.execute
            fired = {"n": 0}

            async def _execute_then_interfere(sql, *a, **k):
                cur = await real_execute(sql, *a, **k)
                if "SET parole_trades_remaining =" in str(sql) and "MIN(" in str(sql):
                    fired["n"] += 1
                    await real_execute(
                        "UPDATE combo_performance SET trades = trades + 1 "
                        "WHERE combo_key='combo_b' AND window='30d'"
                    )
                return cur

            monkeypatch.setattr(db._conn, "execute", _execute_then_interfere)

        monkeypatch.undo()
        assert fired["n"] == 1, "the interfering write never ran"
        rows = await _events(db, event_type="parole_slot_refunded")
        assert len(rows) == 1
        assert rows[0]["delivery_result"] == "stale_generation", (
            "an unrelated concurrent write made a zero-row refund read as "
            "success — this is the total_changes bug"
        )
        cur = await db._conn.execute(
            "SELECT parole_trades_remaining FROM combo_performance "
            "WHERE combo_key='combo_a' AND window='30d'"
        )
        (remaining,) = await cur.fetchone()
        assert remaining == 4, "slot credited to the replacement generation"
    finally:
        await db.close()


# --- reversal pending + delivery axis --------------------------------------


async def _drive_newly_suppressed(db, s):
    now = datetime.now(timezone.utc)
    for _ in range(20):
        await _insert_trade(db, "combo_a", -5, now - timedelta(days=2))


async def test_reversal_pending_recorded_on_the_durable_write(
    tmp_path, settings_factory, monkeypatch
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        _stub_all_senders(monkeypatch)
        await _drive_newly_suppressed(db, s)
        await combo_refresh.refresh_all(db, s)

        rows = await _events(db, event_type="reversal_pending_recorded")
        assert len(rows) == 1
        assert rows[0]["combo_key"] == "combo_a"
        assert rows[0]["transition"] == "newly_suppressed"
        assert rows[0]["delivery_result"] == "ok"
        assert rows[0]["payload_hash"] is not None
        assert rows[0]["detail"] is None
    finally:
        await db.close()


async def test_reversal_delivery_bracket_records_dispatched_and_delivered(
    tmp_path, settings_factory, monkeypatch
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        _, rev, _ = _stub_all_senders(monkeypatch)
        await _drive_newly_suppressed(db, s)
        await combo_refresh.refresh_all(db, s)

        assert rev.calls == 1
        dispatched = await _events(
            db,
            event_type="alert_dispatched",
            alert_source="combo_refresh_suppression_reversal",
        )
        delivered = await _events(
            db,
            event_type="alert_delivered",
            alert_source="combo_refresh_suppression_reversal",
        )
        assert len(dispatched) == 1
        assert len(delivered) == 1
        assert delivered[0]["delivery_result"] == "ok"
        assert delivered[0]["retry"] == 0
        assert delivered[0]["transition"] == "newly_suppressed"
        # The digest must be over the body actually handed to the sender.
        assert delivered[0]["payload_hash"] == alert_events.payload_digest(
            rev.messages[0]
        )
        assert len(await _events(db, event_type="marker_cleared")) == 1
    finally:
        await db.close()


async def test_reversal_delivery_failure_records_alert_failed_not_delivered(
    tmp_path, settings_factory, monkeypatch
):
    """A rejected page must leave `alert_failed` and NO `alert_delivered`. The
    marker also stays, so the next refresh re-attempts."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        _stub_all_senders(monkeypatch)
        monkeypatch.setattr(
            combo_refresh, "_send_suppression_reversal_alert", _BoomSender()
        )
        await _drive_newly_suppressed(db, s)
        await combo_refresh.refresh_all(db, s)

        failed = await _events(
            db,
            event_type="alert_failed",
            alert_source="combo_refresh_suppression_reversal",
        )
        assert len(failed) == 1
        assert failed[0]["delivery_result"] == "error:RuntimeError"
        assert (
            await _events(
                db,
                event_type="alert_delivered",
                alert_source="combo_refresh_suppression_reversal",
            )
            == []
        )
        assert await _events(db, event_type="marker_cleared") == []
    finally:
        await db.close()


async def test_retry_page_is_flagged_and_hashed_after_the_stamp(
    tmp_path, settings_factory, monkeypatch
):
    """A page carried over from an earlier refresh is stamped with its
    detection time. The digest must cover the STAMPED body, or it proves which
    page was rendered rather than which page was sent."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        _stub_all_senders(monkeypatch)
        monkeypatch.setattr(
            combo_refresh, "_send_suppression_reversal_alert", _BoomSender()
        )
        await _drive_newly_suppressed(db, s)
        await combo_refresh.refresh_all(db, s)  # detected + failed -> stays pending

        rev = _StubSender()
        monkeypatch.setattr(combo_refresh, "_send_suppression_reversal_alert", rev)
        await combo_refresh.refresh_all(db, s)  # retry

        assert rev.calls == 1
        assert "[detected " in rev.messages[0]
        delivered = await _events(
            db,
            event_type="alert_delivered",
            alert_source="combo_refresh_suppression_reversal",
        )
        assert len(delivered) == 1
        assert delivered[0]["retry"] == 1
        assert delivered[0]["payload_hash"] == alert_events.payload_digest(
            rev.messages[0]
        )
    finally:
        await db.close()


async def test_superseded_pending_page_is_recorded_in_detail(
    tmp_path, settings_factory, monkeypatch
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        _stub_all_senders(monkeypatch)
        await _seed_30d_row(
            db,
            "combo_a",
            suppressed=0,
            suppressed_at=None,
            parole_at=None,
            parole_trades_remaining=None,
            trades=1,
            wins=1,
            losses=0,
            win_rate_pct=100.0,
            reversal_alert_pending_json=json.dumps(
                {"transition": "newly_suppressed", "detected_at": "old", "message": "m"}
            ),
        )
        await _drive_newly_suppressed(db, s)
        await combo_refresh.refresh_all(db, s)

        rows = await _events(db, event_type="reversal_pending_recorded")
        assert len(rows) == 1
        assert rows[0]["detail"] == "superseded an undelivered page"
    finally:
        await db.close()


async def test_unreadable_pending_payload_records_a_marker_anomaly(
    tmp_path, settings_factory, monkeypatch
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        _stub_all_senders(monkeypatch)
        await _seed_30d_row(
            db, "combo_a", reversal_alert_pending_json="{not json at all"
        )
        await combo_refresh.refresh_all(db, s)

        rows = await _events(db, event_type="marker_anomaly")
        assert len(rows) == 1
        assert rows[0]["delivery_result"] == "pending_unreadable"
        assert rows[0]["transition"] == "reversal_alert_pending_json"
        assert rows[0]["combo_key"] == "combo_a"
    finally:
        await db.close()


async def test_pending_not_cleared_records_a_marker_anomaly(
    tmp_path, settings_factory, monkeypatch
):
    """The payload-bound clear matched zero rows — superseded mid-flight or
    already cleared. Neutral, but it must be visible."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        _stub_all_senders(monkeypatch)

        class _SupersedeDuringSend:
            calls = 0

            async def __call__(self, settings_, message):
                type(self).calls += 1
                await db._conn.execute(
                    "UPDATE combo_performance "
                    "SET reversal_alert_pending_json = '{\"x\": 1}' "
                    "WHERE combo_key='combo_a' AND window='30d'"
                )
                await db._conn.commit()

        sender = _SupersedeDuringSend()
        monkeypatch.setattr(combo_refresh, "_send_suppression_reversal_alert", sender)
        await _drive_newly_suppressed(db, s)
        await combo_refresh.refresh_all(db, s)

        assert sender.calls == 1
        rows = await _events(
            db, event_type="marker_anomaly", delivery_result="pending_not_cleared"
        )
        assert len(rows) == 1
        assert await _events(db, event_type="marker_cleared") == []
    finally:
        await db.close()


# --- permanent suppression -------------------------------------------------


async def test_permanent_suppression_bracket_and_marker(
    tmp_path, settings_factory, monkeypatch
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        perm, _, _ = _stub_all_senders(monkeypatch)
        # Suppressed, no trades at all in the window -> permanent-suppression.
        await _seed_30d_row(db, "combo_a")
        await combo_refresh.refresh_all(db, s)

        assert perm.calls == 1
        dispatched = await _events(
            db,
            event_type="alert_dispatched",
            alert_source="combo_refresh_permanent_suppression",
        )
        delivered = await _events(
            db,
            event_type="alert_delivered",
            alert_source="combo_refresh_permanent_suppression",
        )
        assert len(dispatched) == 1
        assert len(delivered) == 1
        assert delivered[0]["payload_hash"] == alert_events.payload_digest(
            perm.messages[0]
        )
        stamped = await _events(
            db, event_type="marker_stamped", transition="perm_suppression_alerted_at"
        )
        assert len(stamped) == 1
    finally:
        await db.close()


async def test_permanent_suppression_failure_records_failed_and_no_marker(
    tmp_path, settings_factory, monkeypatch
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        _stub_all_senders(monkeypatch)
        monkeypatch.setattr(
            combo_refresh, "_send_permanent_suppression_alert", _BoomSender()
        )
        await _seed_30d_row(db, "combo_a")
        await combo_refresh.refresh_all(db, s)

        failed = await _events(
            db,
            event_type="alert_failed",
            alert_source="combo_refresh_permanent_suppression",
        )
        assert len(failed) == 1
        assert failed[0]["delivery_result"] == "error:RuntimeError"
        assert (
            await _events(
                db,
                event_type="marker_stamped",
                transition="perm_suppression_alerted_at",
            )
            == []
        )
    finally:
        await db.close()


# --- retest terminal-incomplete -------------------------------------------


async def _seed_terminal_incomplete(db, s):
    now = datetime.now(timezone.utc)
    parole_at = (now - timedelta(days=3)).isoformat()
    await _seed_30d_row(
        db, "combo_a", parole_at=parole_at, parole_trades_remaining=0, trades=25, wins=4
    )
    # Two valid closes admitted under the parole, none still open, no slots.
    for _ in range(2):
        await _insert_trade(
            db,
            "combo_a",
            -10,
            now - timedelta(days=1),
            opened_at=now - timedelta(days=2),
        )


async def test_retest_incomplete_bracket_and_marker(
    tmp_path, settings_factory, monkeypatch
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        _, _, retest = _stub_all_senders(monkeypatch)
        await _seed_terminal_incomplete(db, s)
        await combo_refresh._process_retest_terminal_incomplete(db, s)

        assert retest.calls == 1
        dispatched = await _events(
            db,
            event_type="alert_dispatched",
            alert_source="combo_refresh_retest_terminal_incomplete",
        )
        delivered = await _events(
            db,
            event_type="alert_delivered",
            alert_source="combo_refresh_retest_terminal_incomplete",
        )
        assert len(dispatched) == 1
        assert len(delivered) == 1
        assert delivered[0]["payload_hash"] == alert_events.payload_digest(
            retest.messages[0]
        )
        stamped = await _events(
            db, event_type="marker_stamped", transition="retest_incomplete_alerted_at"
        )
        assert len(stamped) == 1
    finally:
        await db.close()


async def test_retest_incomplete_failure_records_failed_and_no_marker(
    tmp_path, settings_factory, monkeypatch
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        _stub_all_senders(monkeypatch)

        async def _boom(message, settings_):
            raise RuntimeError("telegram send failed status=400")

        monkeypatch.setattr(combo_refresh, "_send_retest_incomplete_alert", _boom)
        await _seed_terminal_incomplete(db, s)
        await combo_refresh._process_retest_terminal_incomplete(db, s)

        failed = await _events(
            db,
            event_type="alert_failed",
            alert_source="combo_refresh_retest_terminal_incomplete",
        )
        assert len(failed) == 1
        assert failed[0]["delivery_result"] == "error:RuntimeError"
        assert await _events(db, event_type="marker_stamped") == []
    finally:
        await db.close()


async def test_retest_marker_stale_generation_records_a_marker_anomaly(
    tmp_path, settings_factory, monkeypatch
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        _stub_all_senders(monkeypatch)

        class _MoveGenerationDuringSend:
            calls = 0

            async def __call__(self, message, settings_):
                type(self).calls += 1
                await db._conn.execute(
                    "UPDATE combo_performance SET parole_at = ? "
                    "WHERE combo_key='combo_a' AND window='30d'",
                    ("2099-01-01T00:00:00+00:00",),
                )
                await db._conn.commit()

        sender = _MoveGenerationDuringSend()
        monkeypatch.setattr(combo_refresh, "_send_retest_incomplete_alert", sender)
        await _seed_terminal_incomplete(db, s)
        alerted = await combo_refresh._process_retest_terminal_incomplete(db, s)

        assert sender.calls == 1
        assert alerted == []
        rows = await _events(
            db, event_type="marker_anomaly", delivery_result="stale_generation"
        )
        assert len(rows) == 1
        assert rows[0]["transition"] == "retest_incomplete_alerted_at"
        assert await _events(db, event_type="marker_stamped") == []
    finally:
        await db.close()


# --- suppression fail-open alert ------------------------------------------


async def test_fallback_alert_is_bracketed(tmp_path, settings_factory, monkeypatch):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        suppression._fallback_timestamps.clear()
        suppression._last_alerted_ts = float("-inf")

        sent: list[str] = []

        async def _capture(text, session, settings_, **_kw):
            sent.append(text)

        import scout.alerter as _alerter

        monkeypatch.setattr(_alerter, "send_telegram_message", _capture)

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(
            suppression.aiohttp, "ClientSession", lambda *a, **k: _FakeSession()
        )

        for _ in range(s.FEEDBACK_FALLBACK_ALERT_THRESHOLD):
            await suppression._record_fallback(db, "combo_a", "database is locked", s)

        assert len(sent) == 1
        dispatched = await _events(
            db, event_type="alert_dispatched", alert_source="suppression"
        )
        delivered = await _events(
            db, event_type="alert_delivered", alert_source="suppression"
        )
        assert len(dispatched) == 1
        assert len(delivered) == 1
        assert delivered[0]["payload_hash"] == alert_events.payload_digest(sent[0])
        assert delivered[0]["combo_key"] == "combo_a"
    finally:
        suppression._fallback_timestamps.clear()
        suppression._last_alerted_ts = float("-inf")
        await db.close()


async def test_fallback_alert_failure_is_bracketed(
    tmp_path, settings_factory, monkeypatch
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        suppression._fallback_timestamps.clear()
        suppression._last_alerted_ts = float("-inf")

        async def _boom(*a, **k):
            raise RuntimeError("network down")

        import scout.alerter as _alerter

        monkeypatch.setattr(_alerter, "send_telegram_message", _boom)

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(
            suppression.aiohttp, "ClientSession", lambda *a, **k: _FakeSession()
        )

        for _ in range(s.FEEDBACK_FALLBACK_ALERT_THRESHOLD):
            await suppression._record_fallback(db, "combo_a", "database is locked", s)

        failed = await _events(
            db, event_type="alert_failed", alert_source="suppression"
        )
        assert len(failed) == 1
        assert failed[0]["delivery_result"] == "error:RuntimeError"
        assert (
            await _events(db, event_type="alert_delivered", alert_source="suppression")
            == []
        )
    finally:
        suppression._fallback_timestamps.clear()
        suppression._last_alerted_ts = float("-inf")
        await db.close()


async def test_fallback_from_inside_the_locked_gate_does_not_deadlock(
    tmp_path, settings_factory, monkeypatch
):
    """The locked-and-busy branch of `_open_gate` runs with `db._txn_lock` HELD.
    `_record_fallback` now writes ledger rows through a writer that takes the
    same non-reentrant lock, so the call is deferred past the lock release. If
    it is ever moved back inside, the writer's bounded acquire blocks for its
    full timeout and the ledger rows never land — both assertions below fail.

    `FEEDBACK_FALLBACK_ALERT_THRESHOLD=1` is load-bearing: below the threshold
    no alert fires, so no ledger write happens and the test would prove nothing
    about the lock at all."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory(FEEDBACK_FALLBACK_ALERT_THRESHOLD=1)
        suppression._fallback_timestamps.clear()
        suppression._last_alerted_ts = float("-inf")
        parole_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        await _seed_30d_row(
            db, "combo_a", parole_at=parole_at, parole_trades_remaining=5
        )

        sent: list[str] = []

        async def _capture(text, session, settings_, **_kw):
            sent.append(text)

        import scout.alerter as _alerter

        monkeypatch.setattr(_alerter, "send_telegram_message", _capture)

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(
            suppression.aiohttp, "ClientSession", lambda *a, **k: _FakeSession()
        )

        real_execute = db._conn.execute

        async def _boom_on_begin(sql, *a, **k):
            if str(sql).strip().upper().startswith("BEGIN IMMEDIATE"):
                raise aiosqlite.OperationalError("database is locked")
            return await real_execute(sql, *a, **k)

        monkeypatch.setattr(db._conn, "execute", _boom_on_begin)
        started = time.monotonic()
        allow, reason = await suppression.should_open(db, "combo_a", settings=s)
        elapsed = time.monotonic() - started
        monkeypatch.undo()

        assert (allow, reason) == (True, "db_error_fallback_allow")
        assert len(sent) == 1
        assert elapsed < 5.0, (
            f"took {elapsed:.1f}s — the fail-open path is contending with the "
            "trading lock it already holds"
        )
        dispatched = await _events(
            db, event_type="alert_dispatched", alert_source="suppression"
        )
        delivered = await _events(
            db, event_type="alert_delivered", alert_source="suppression"
        )
        assert len(dispatched) == 1, "the ledger write never completed"
        assert len(delivered) == 1
    finally:
        suppression._fallback_timestamps.clear()
        suppression._last_alerted_ts = float("-inf")
        await db.close()


# --- auto_suspend ----------------------------------------------------------


async def _seed_hard_loss(db, signal_type="gainers_early", n=30):
    now = datetime.now(timezone.utc)
    for i in range(n):
        await db._conn.execute(
            """INSERT INTO paper_trades
               (token_id, symbol, name, chain, signal_type, signal_data,
                entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price,
                sl_price, status, exit_price, pnl_usd, pnl_pct, peak_pct,
                opened_at, closed_at)
               VALUES (?, 'TOK', 'T', 'coingecko', ?, '{}', 1.0, 100.0, 100.0,
                       20.0, 15.0, 1.2, 0.85, 'closed_sl', 1.0, -100.0, -100.0,
                       5.0, ?, ?)""",
            (
                f"as-{next(_counter)}",
                signal_type,
                (now - timedelta(days=1, seconds=i)).isoformat(),
                (now - timedelta(hours=20, seconds=i)).isoformat(),
            ),
        )
    await db._conn.commit()


class _FakeAlerter:
    """Stands in for `scout.alerter`, honouring the `raise_on_failure` contract:
    a non-200 raises only when the caller asked it to. That is the exact
    property under test — without the flag the real alerter returns normally on
    a rejection and the caller stamps `delivered`."""

    def __init__(self, status=200):
        self.status = status
        self.kwargs: list[dict] = []

    async def send_telegram_message(self, text, session, settings, **kwargs):
        self.kwargs.append(kwargs)
        if self.status != 200 and kwargs.get("raise_on_failure"):
            raise RuntimeError(f"telegram send failed status={self.status}")


async def test_auto_suspend_alert_bracket_records_delivered(
    tmp_path, settings_factory, monkeypatch
):
    from scout.trading import auto_suspend
    from scout.trading.params import clear_cache_for_tests

    clear_cache_for_tests()
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        fake = _FakeAlerter(status=200)
        import scout.alerter as _alerter

        monkeypatch.setattr(
            _alerter, "send_telegram_message", fake.send_telegram_message
        )
        await _seed_hard_loss(db)
        s = settings_factory(SIGNAL_PARAMS_ENABLED=True)
        suspended = await auto_suspend.maybe_suspend_signals(db, s, session=object())
        assert any(r["signal_type"] == "gainers_early" for r in suspended)

        dispatched = await _events(
            db, event_type="alert_dispatched", alert_source="auto_suspend"
        )
        delivered = await _events(
            db, event_type="alert_delivered", alert_source="auto_suspend"
        )
        assert len(dispatched) == 1
        assert len(delivered) == 1
        assert delivered[0]["signal_type"] == "gainers_early"
        assert delivered[0]["combo_key"] is None
        assert delivered[0]["transition"] == "hard_loss"
        assert fake.kwargs[0]["raise_on_failure"] is True
        assert fake.kwargs[0]["parse_mode"] is None
    finally:
        clear_cache_for_tests()
        await db.close()


async def test_auto_suspend_non_200_records_failed_and_keeps_the_suspension(
    tmp_path, settings_factory, monkeypatch
):
    """The #525 class: without `raise_on_failure=True` a rejected page returns
    normally and the caller stamps `delivered`. The suspension itself is
    already committed and must STAY applied — only the page is lost, loudly."""
    from scout.trading import auto_suspend
    from scout.trading.params import clear_cache_for_tests

    clear_cache_for_tests()
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        fake = _FakeAlerter(status=400)
        import scout.alerter as _alerter

        monkeypatch.setattr(
            _alerter, "send_telegram_message", fake.send_telegram_message
        )
        await _seed_hard_loss(db)
        s = settings_factory(SIGNAL_PARAMS_ENABLED=True)
        # Must NOT raise: a failed page cannot abort the suspension pass.
        suspended = await auto_suspend.maybe_suspend_signals(db, s, session=object())
        assert any(r["signal_type"] == "gainers_early" for r in suspended)

        failed = await _events(
            db, event_type="alert_failed", alert_source="auto_suspend"
        )
        assert len(failed) == 1
        assert failed[0]["delivery_result"] == "error:RuntimeError"
        assert (
            await _events(db, event_type="alert_delivered", alert_source="auto_suspend")
            == []
        )

        cur = await db._conn.execute(
            "SELECT enabled FROM signal_params WHERE signal_type='gainers_early'"
        )
        (enabled,) = await cur.fetchone()
        assert enabled == 0, "the state change must survive a failed page"
    finally:
        clear_cache_for_tests()
        await db.close()


def test_auto_suspend_passes_raise_on_failure_statically():
    """Belt-and-braces against a future edit dropping the flag: the runtime test
    above proves the behaviour, this pins the call itself."""
    import inspect

    from scout.trading import auto_suspend

    src = inspect.getsource(auto_suspend._send_suspend_alert)
    assert "raise_on_failure=True" in src
    assert "parse_mode=None" in src


async def test_scheduler_contains_a_raising_suspension_pass(tmp_path):
    """Second containment layer, driven for real. `raise_on_failure=True` makes
    a rejected page raise inside the alerter; `_send_suspend_alert` catches it,
    but if a future edit ever lets one escape `maybe_suspend_signals`, the
    pipeline loop must still survive the tick rather than dying on a page."""
    import contextlib
    from unittest.mock import AsyncMock, MagicMock, patch

    import structlog

    from scout.config import Settings
    from scout.main import _run_feedback_schedulers

    settings = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        DB_PATH=tmp_path / "scout.db",
        SIGNAL_PARAMS_ENABLED=True,
    )
    db = MagicMock()
    now_local = datetime(2026, 5, 18, settings.SUSPENSION_CHECK_HOUR, 0, 0)

    boom = AsyncMock(side_effect=RuntimeError("page rejected"))
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch(
                "scout.main._combo_refresh.refresh_all", new=AsyncMock(return_value={})
            )
        )
        stack.enter_context(
            patch("scout.main._weekly_digest.send_weekly_digest", new=AsyncMock())
        )
        stack.enter_context(
            patch("scout.trading.cohort_digest.send_cohort_digest", new=AsyncMock())
        )
        stack.enter_context(
            patch("scout.trading.auto_suspend.maybe_suspend_signals", new=boom)
        )
        with structlog.testing.capture_logs() as logs:
            # Must not raise: the tick is contained.
            await _run_feedback_schedulers(db, settings, "", "", "", now_local)

    boom.assert_awaited_once()
    errors = [e for e in logs if e["event"] == "auto_suspend_loop_error"]
    assert len(errors) == 1, "a failed suspension pass was not contained + logged"
