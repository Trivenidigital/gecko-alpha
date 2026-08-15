"""F3 — `scout.trading.alert_events.record_alert_event` writer contract.

The load-bearing property is the one nobody rehearses: this writer must NEVER
raise and must never poison the transaction it was handed. The `except` block
that guarantees it is untested code unless a test actually forces the INSERT to
fail — so both transaction modes are driven through a real failure here, and the
log the handler emits is asserted by name (a handler that referenced the wrong
logger binding would raise NameError from inside the containment path).
"""

from __future__ import annotations

import asyncio

import aiosqlite

import pytest
import structlog

from scout.db import Database
from scout.trading import alert_events


async def _count(db, **where) -> int:
    """Rows written by the WRITER. The migration seeds one `ledger_installed`
    epoch row, which is schema, not writer output — counting it would make
    every "no row was written" assertion below read as 1 and quietly stop
    discriminating."""
    clause = "event_type != 'ledger_installed'"
    params: tuple = ()
    if where:
        clause += " AND " + " AND ".join(f"{k} = ?" for k in where)
        params = tuple(where.values())
    cur = await db._conn.execute(
        f"SELECT COUNT(*) FROM alert_events WHERE {clause}", params
    )
    (n,) = await cur.fetchone()
    return n


async def test_self_committed_write_is_durable(tmp_path):
    """`managed_txn=False` opens its own transaction and commits it."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        await alert_events.record_alert_event(
            db,
            event_type="refresh_completed",
            state_json=alert_events.encode_state(refreshed=3, failed=0),
        )
        # Read through a SEPARATE connection: a row read back through the same
        # connection that wrote it proves nothing about durability, because
        # uncommitted writes are visible to their own writer.
        other = Database(tmp_path / "t.db")
        await other.initialize()
        try:
            assert await _count(other, event_type="refresh_completed") == 1
        finally:
            await other.close()
    finally:
        await db.close()


async def test_managed_txn_write_is_atomic_with_the_caller_transaction(tmp_path):
    """`managed_txn=True` must NOT commit — the row lives or dies with the
    caller's transaction."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        async with db._txn_lock:
            await db._conn.execute("BEGIN IMMEDIATE")
            await alert_events.record_alert_event(
                db,
                event_type="parole_slot_spent",
                combo_key="combo_a",
                managed_txn=True,
            )
            # Visible to its own writer mid-transaction...
            assert await _count(db, event_type="parole_slot_spent") == 1
            # ...but the transaction is still open, which is the actual claim.
            assert db._conn.in_transaction
            await db._conn.rollback()
        assert await _count(db, event_type="parole_slot_spent") == 0
    finally:
        await db.close()


async def test_managed_txn_write_commits_with_the_caller(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        async with db._txn_lock:
            await db._conn.execute("BEGIN IMMEDIATE")
            await alert_events.record_alert_event(
                db,
                event_type="parole_slot_spent",
                combo_key="combo_a",
                managed_txn=True,
            )
            await db._conn.commit()
        other = Database(tmp_path / "t.db")
        await other.initialize()
        try:
            assert await _count(other, combo_key="combo_a") == 1
        finally:
            await other.close()
    finally:
        await db.close()


async def test_all_columns_round_trip(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        await alert_events.record_alert_event(
            db,
            event_type="alert_delivered",
            combo_key="combo_a",
            signal_type="volume_spike",
            alert_source="combo_refresh_suppression_reversal",
            transition="newly_suppressed",
            detected_at="2026-08-14T00:00:00+00:00",
            delivery_result="ok",
            retry=True,
            payload_hash=alert_events.payload_digest("hello"),
            state_json=alert_events.encode_state(a=1),
            detail="d",
        )
        cur = await db._conn.execute(
            "SELECT event_type, combo_key, signal_type, alert_source, transition, "
            "detected_at, delivery_result, retry, payload_hash, state_json, detail "
            "FROM alert_events WHERE event_type != 'ledger_installed'"
        )
        rows = await cur.fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row[0] == "alert_delivered"
        assert row[1] == "combo_a"
        assert row[2] == "volume_spike"
        assert row[3] == "combo_refresh_suppression_reversal"
        assert row[4] == "newly_suppressed"
        assert row[5] == "2026-08-14T00:00:00+00:00"
        assert row[6] == "ok"
        assert row[7] == 1
        assert row[8] == alert_events.payload_digest("hello")
        assert row[9] == '{"a": 1}'
        assert row[10] == "d"
    finally:
        await db.close()


@pytest.mark.parametrize(
    "value,expected", [(True, 1), (False, 0), (1, 1), (0, 0), (None, None)]
)
async def test_retry_is_normalised_to_0_1_or_null(tmp_path, value, expected):
    """`retry` is a FLAG. `bool` is an `int` in Python, so the writer normalises
    explicitly instead of binding whatever the caller happened to pass."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        await alert_events.record_alert_event(
            db, event_type="alert_dispatched", retry=value
        )
        cur = await db._conn.execute(
            "SELECT retry FROM alert_events WHERE event_type != 'ledger_installed'"
        )
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == expected
    finally:
        await db.close()


async def test_failure_is_swallowed_and_logged_in_self_committed_mode(tmp_path):
    """A forced INSERT failure must log and return — never raise into the
    control path. Driven through the real CHECK constraint, not a mock."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        with structlog.testing.capture_logs() as logs:
            await alert_events.record_alert_event(
                db, event_type="not_a_real_event_type", combo_key="combo_a"
            )
        failures = [e for e in logs if e["event"] == "alert_event_write_failed"]
        assert len(failures) == 1
        assert failures[0]["err_id"] == "ALERT_EVENT_WRITE"
        assert failures[0]["event_type"] == "not_a_real_event_type"
        assert failures[0]["combo_key"] == "combo_a"
        assert failures[0]["managed_txn"] is False
        assert await _count(db) == 0

        # The connection must be usable afterwards — a half-open transaction
        # left behind would break the NEXT writer, not this one.
        await alert_events.record_alert_event(db, event_type="refresh_completed")
        assert await _count(db, event_type="refresh_completed") == 1
    finally:
        await db.close()


async def test_failure_is_swallowed_and_does_not_poison_the_outer_txn(tmp_path):
    """`managed_txn=True`: a failed ledger INSERT must leave the caller's
    transaction intact and committable. The ledger observes the control plane;
    it may never veto it."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        async with db._txn_lock:
            await db._conn.execute("BEGIN IMMEDIATE")
            await db._conn.execute(
                "INSERT INTO combo_performance "
                "(combo_key, window, trades, wins, losses, total_pnl_usd, "
                " avg_pnl_pct, win_rate_pct, suppressed, last_refreshed) "
                "VALUES ('combo_a', '30d', 1, 1, 0, 0, 0, 100.0, 0, '2026-08-14')"
            )
            with structlog.testing.capture_logs() as logs:
                await alert_events.record_alert_event(
                    db,
                    event_type="not_a_real_event_type",
                    combo_key="combo_a",
                    managed_txn=True,
                )
            await db._conn.commit()

        failures = [e for e in logs if e["event"] == "alert_event_write_failed"]
        assert len(failures) == 1
        assert failures[0]["managed_txn"] is True

        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM combo_performance WHERE combo_key = 'combo_a'"
        )
        (survived,) = await cur.fetchone()
        assert survived == 1, "the ledger failure rolled back the caller's work"
        assert await _count(db) == 0
    finally:
        await db.close()


async def test_uninitialised_lock_is_logged_not_raised(tmp_path):
    """A writer called before `initialize()` completed must not raise either."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        db._txn_lock = None
        with structlog.testing.capture_logs() as logs:
            await alert_events.record_alert_event(db, event_type="refresh_completed")
        failures = [e for e in logs if e["event"] == "alert_event_write_failed"]
        assert len(failures) == 1
        assert failures[0]["err_type"] == "RuntimeError"
    finally:
        db._txn_lock = asyncio.Lock()
        await db.close()


async def test_payload_digest_hashes_the_exact_string(tmp_path):
    """No normalisation: two bodies differing only in whitespace must not share
    a digest, or the digest cannot prove which body went out."""
    assert alert_events.payload_digest("a b") != alert_events.payload_digest("a  b")
    assert (
        alert_events.payload_digest("x")
        == "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881"
    )


async def test_generation_state_records_absence_as_explicit_nulls(tmp_path):
    """An absent row and an unread row must not look the same later."""
    before = alert_events.generation_state(None, prefix="before")
    assert before == {
        "before_suppressed": None,
        "before_suppressed_at": None,
        "before_parole_at": None,
        "before_parole_trades_remaining": None,
    }


async def test_managed_failure_that_aborted_the_caller_txn_is_re_raised(tmp_path):
    """THE DANGEROUS CASE. SQLite's IO class (SQLITE_FULL / IOERR / BUSY /
    NOMEM) aborts the whole TRANSACTION, not just the statement — and disk-full
    has real history on this box. Swallowing there would let `_open_gate`
    return an admission whose `parole_trades_remaining` decrement SQLite had
    already discarded: silent over-admission against a live lock period.

    So the writer distinguishes the two abort scopes rather than assuming the
    benign one, and re-raises only when the caller's transaction is actually
    gone."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        async with db._txn_lock:
            await db._conn.execute("BEGIN IMMEDIATE")
            await db._conn.execute(
                "INSERT INTO combo_performance "
                "(combo_key, window, trades, wins, losses, total_pnl_usd, "
                " avg_pnl_pct, win_rate_pct, suppressed, last_refreshed) "
                "VALUES ('combo_a', '30d', 1, 1, 0, 0, 0, 100.0, 0, '2026-08-14')"
            )
            assert db._conn.in_transaction

            real_execute = db._conn.execute

            async def _abort_the_txn(sql, *a, **k):
                # Simulate the IO class: the transaction is discarded and the
                # statement reports failure.
                await real_execute("ROLLBACK")
                raise aiosqlite.OperationalError("database or disk is full")

            db._conn.execute = _abort_the_txn
            try:
                with pytest.raises(aiosqlite.OperationalError):
                    await alert_events.record_alert_event(
                        db,
                        event_type="parole_slot_spent",
                        combo_key="combo_a",
                        managed_txn=True,
                    )
            finally:
                db._conn.execute = real_execute
            assert not db._conn.in_transaction
    finally:
        await db.close()


async def test_managed_constraint_failure_still_swallowed_when_txn_survives(tmp_path):
    """The common case must NOT escalate: a constraint violation undoes only the
    statement, the caller's transaction is intact, and the ledger stays silent
    about it beyond its log line."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        async with db._txn_lock:
            await db._conn.execute("BEGIN IMMEDIATE")
            await db._conn.execute(
                "INSERT INTO combo_performance "
                "(combo_key, window, trades, wins, losses, total_pnl_usd, "
                " avg_pnl_pct, win_rate_pct, suppressed, last_refreshed) "
                "VALUES ('combo_b', '30d', 1, 1, 0, 0, 0, 100.0, 0, '2026-08-14')"
            )
            # Must not raise.
            await alert_events.record_alert_event(
                db,
                event_type="not_a_real_event_type",
                combo_key="combo_b",
                managed_txn=True,
            )
            assert db._conn.in_transaction, "the constraint failure killed the txn"
            await db._conn.commit()

        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM combo_performance WHERE combo_key='combo_b'"
        )
        (survived,) = await cur.fetchone()
        assert survived == 1
        assert await _count(db) == 0
    finally:
        await db.close()
