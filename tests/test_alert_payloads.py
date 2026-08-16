"""Content-addressed alert-body preimages (`alert_payloads`).

The property under test is RECONSTRUCTION, not equality. `alert_events` already
proves that a body you hold is the body that went out; these tests pin that the
exact bytes come back out of the substrate, that one body is stored once no
matter how many ledger rows reference it, that a body which does not hash to its
own key is refused rather than returned, and that a failure to store any of it
can never cost the operator a page.

Every assertion is a COUNT or an identity. "A row exists" passes when the writer
fired twice, and a duplicated body is the exact failure the content-addressing
exists to prevent.
"""

from __future__ import annotations

import hashlib
import sqlite3

import aiosqlite
import pytest
import structlog

from scout.db import Database
from scout.exceptions import AlertPayloadCorrupt
from scout.trading import alert_events


async def _payload_count(db, **where) -> int:
    clause = "1=1"
    params: tuple = ()
    if where:
        clause = " AND ".join(f"{k} = ?" for k in where)
        params = tuple(where.values())
    cur = await db._conn.execute(
        f"SELECT COUNT(*) FROM alert_payloads WHERE {clause}", params
    )
    (n,) = await cur.fetchone()
    return n


# --- exact-byte fidelity ---------------------------------------------------


# The real bodies carry em-dashes, arrows and multi-line text; the CRLF and the
# trailing newline are the cases a "normalise on the way in" implementation
# silently eats.
_BODIES = [
    pytest.param("plain ascii body", id="ascii"),
    pytest.param(
        "gecko-alpha: parole retest STALLED for gainers_early\n"
        "window opened 2026-08-16T06:14:56Z — 1.5 days elapsed → HELD\n",
        id="unicode-lf-trailing-newline",
    ),
    pytest.param("first line\r\nsecond line\r\n", id="crlf"),
    pytest.param("mixed\r\nlf\nand\rcr\n", id="mixed-line-endings"),
    pytest.param("", id="empty"),
    pytest.param("   leading and trailing whitespace   \n\n", id="whitespace"),
    pytest.param("emoji ⚠ и кириллица 中文 \U0001f680", id="astral-and-cyrillic"),
]


@pytest.mark.parametrize("body", _BODIES)
async def test_body_round_trips_byte_for_byte(tmp_path, body):
    """Store and read back the EXACT bytes — not a normalised rendering."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        digest = await alert_events.record_alert_payload(db, body)
        assert digest == alert_events.payload_digest(body)

        # The stored column, read raw, must BE the utf-8 bytes of the body.
        cur = await db._conn.execute(
            "SELECT payload, byte_length FROM alert_payloads WHERE payload_hash = ?",
            (digest,),
        )
        stored, byte_length = await cur.fetchone()
        assert isinstance(stored, bytes), "payload must round-trip as BLOB, not TEXT"
        assert stored == body.encode("utf-8")
        assert byte_length == len(body.encode("utf-8"))

        # ...and the reader hands back a string equal to the original.
        assert await alert_events.load_alert_payload(db, digest) == body
    finally:
        await db.close()


async def test_stored_body_survives_a_reopen(tmp_path):
    """Durability through a SEPARATE connection — a body read back through the
    connection that wrote it proves nothing, because uncommitted writes are
    visible to their own writer."""
    body = "durable — page body\nwith a second line\n"
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        digest = await alert_events.record_alert_payload(db, body)
    finally:
        await db.close()

    other = Database(tmp_path / "t.db")
    await other.initialize()
    try:
        assert await alert_events.load_alert_payload(other, digest) == body
    finally:
        await other.close()


async def test_absent_digest_reads_as_none(tmp_path):
    """Every pre-cutover `payload_hash` resolves to nothing. That is the honest
    state, not an error."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        assert await alert_events.load_alert_payload(db, "0" * 64) is None
    finally:
        await db.close()


# --- corruption ------------------------------------------------------------


async def test_body_that_does_not_hash_to_its_key_is_refused(tmp_path):
    """A stored body whose sha256 != its key is corruption. Returning it would
    be worse than returning nothing: the caller asked what the operator was
    told and would receive text that provably is not it."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        real = "the body that was actually sent"
        digest = await alert_events.record_alert_payload(db, real)
        tampered = b"a different body entirely"
        async with db._txn_lock:
            await db._conn.execute(
                "UPDATE alert_payloads SET payload = ?, byte_length = ? "
                "WHERE payload_hash = ?",
                (tampered, len(tampered), digest),
            )
            await db._conn.commit()

        with pytest.raises(AlertPayloadCorrupt) as excinfo:
            await alert_events.load_alert_payload(db, digest)
        assert excinfo.value.payload_hash == digest
        assert excinfo.value.actual_hash == hashlib.sha256(tampered).hexdigest()
    finally:
        await db.close()


async def test_disagreeing_byte_length_is_refused(tmp_path):
    """`byte_length` is a second, independent statement about the same bytes. If
    the two disagree the row is not trustworthy even when the hash matches —
    something wrote this row that was not the writer."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        body = "a body whose recorded length gets corrupted"
        digest = await alert_events.record_alert_payload(db, body)
        async with db._txn_lock:
            await db._conn.execute(
                "UPDATE alert_payloads SET byte_length = ? WHERE payload_hash = ?",
                (len(body.encode("utf-8")) + 1, digest),
            )
            await db._conn.commit()

        with pytest.raises(AlertPayloadCorrupt):
            await alert_events.load_alert_payload(db, digest)
    finally:
        await db.close()


async def test_text_typed_row_is_verified_not_crashed(tmp_path):
    """SQLite is dynamically typed, so an analyst's `sqlite3` session can leave
    TEXT in a BLOB column. That must be verified like any other row, not raise a
    TypeError out of `hashlib`."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        body = "written as TEXT — not by record_alert_payload"
        digest = alert_events.payload_digest(body)
        async with db._txn_lock:
            await db._conn.execute(
                "INSERT INTO alert_payloads "
                "(payload_hash, payload, byte_length, first_seen_at) "
                "VALUES (?, ?, ?, '2026-08-16T00:00:00+00:00')",
                (digest, body, len(body.encode("utf-8"))),
            )
            await db._conn.commit()
        assert await alert_events.load_alert_payload(db, digest) == body
    finally:
        await db.close()


# --- content addressing ----------------------------------------------------


async def test_three_events_sharing_one_payload_store_one_body(tmp_path):
    """The dispatched/delivered/failed triplet references one digest. The body
    is paid for ONCE — this is the whole reason the substrate is keyed on the
    digest instead of carrying a column on `alert_events`."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        body = "gecko-alpha: combo x+y is SUPPRESSED AND IDLE — no trades in >30d"
        digests = set()
        for event_type in ("alert_dispatched", "alert_failed", "alert_delivered"):
            digest = await alert_events.record_alert_payload(db, body)
            digests.add(digest)
            await alert_events.record_alert_event(
                db,
                event_type=event_type,
                combo_key="x+y",
                payload_hash=digest,
            )

        assert len(digests) == 1
        assert await _payload_count(db) == 1
        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM alert_events WHERE payload_hash = ?",
            (digests.pop(),),
        )
        (event_rows,) = await cur.fetchone()
        assert event_rows == 3
    finally:
        await db.close()


async def test_first_seen_at_is_not_moved_by_a_later_reference(tmp_path):
    """`INSERT OR IGNORE`, not REPLACE. The column answers "when was this body
    first seen", and a REPLACE would silently rewrite it on every retry — the
    same clobber class as the UPSERT lesson."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        body = "retried page body"
        digest = await alert_events.record_alert_payload(db, body)
        cur = await db._conn.execute(
            "SELECT first_seen_at FROM alert_payloads WHERE payload_hash = ?",
            (digest,),
        )
        (first,) = await cur.fetchone()

        await alert_events.record_alert_payload(db, body)
        cur = await db._conn.execute(
            "SELECT first_seen_at FROM alert_payloads WHERE payload_hash = ?",
            (digest,),
        )
        (second,) = await cur.fetchone()
        assert second == first
        assert await _payload_count(db) == 1
    finally:
        await db.close()


async def test_distinct_bodies_get_distinct_rows(tmp_path):
    """The dedup must key on the BODY, not on anything ambient. Two pages that
    differ only by a retry stamp are two different things the operator was told."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        base = "combo a+b auto-suppressed"
        await alert_events.record_alert_payload(db, base)
        await alert_events.record_alert_payload(db, f"{base} [detected 2026-08-16]")
        assert await _payload_count(db) == 2
    finally:
        await db.close()


# --- fail-soft -------------------------------------------------------------


async def test_unmanaged_write_failure_is_loud_and_swallowed(tmp_path):
    """A preimage that cannot be stored must not raise into the alert path. The
    digest still comes back, so the ledger row still lands and the page still
    goes out — the ledger degrades to what it was before this substrate."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        async with db._txn_lock:
            await db._conn.execute("DROP TABLE alert_payloads")
            await db._conn.commit()

        cap = structlog.testing.LogCapture()
        structlog.configure(processors=[cap])
        try:
            body = "a page nobody can store the body of"
            digest = await alert_events.record_alert_payload(db, body)
        finally:
            structlog.reset_defaults()

        assert digest == alert_events.payload_digest(body)
        failures = [
            e for e in cap.entries if e["event"] == "alert_payload_write_failed"
        ]
        assert len(failures) == 1
        assert failures[0]["err_id"] == "ALERT_PAYLOAD_WRITE"
        assert failures[0]["managed_txn"] is False

        # The lock must be released even on the failure path, or the next
        # writer wedges forever.
        assert not db._txn_lock.locked()

        # ...and the ledger row referencing the unresolvable digest still lands.
        await alert_events.record_alert_event(
            db, event_type="alert_dispatched", payload_hash=digest
        )
        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM alert_events WHERE payload_hash = ?", (digest,)
        )
        (n,) = await cur.fetchone()
        assert n == 1
    finally:
        await db.close()


async def test_managed_write_failure_does_not_poison_the_caller_transaction(tmp_path):
    """A statement-level failure undoes only the INSERT. The caller's open
    transaction must survive it and commit its own work — the preimage is
    evidence about the control plane and can never be allowed to break it."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        async with db._txn_lock:
            await db._conn.execute("DROP TABLE alert_payloads")
            await db._conn.commit()

        cap = structlog.testing.LogCapture()
        structlog.configure(processors=[cap])
        try:
            async with db._txn_lock:
                await db._conn.execute("BEGIN IMMEDIATE")
                await db._conn.execute(
                    "INSERT INTO combo_performance "
                    "(combo_key, window, trades, wins, losses, total_pnl_usd, "
                    " avg_pnl_pct, win_rate_pct, suppressed, last_refreshed) "
                    "VALUES ('caller+work', '30d', 0, 0, 0, 0, 0, 0, 0, "
                    "'2026-08-16T00:00:00+00:00')"
                )
                digest = await alert_events.record_alert_payload(
                    db, "body", managed_txn=True
                )
                # THE claim: SQLite undid the statement, not the transaction.
                assert db._conn.in_transaction
                await db._conn.commit()
        finally:
            structlog.reset_defaults()

        assert digest == alert_events.payload_digest("body")
        failures = [
            e for e in cap.entries if e["event"] == "alert_payload_write_failed"
        ]
        assert len(failures) == 1
        assert failures[0]["managed_txn"] is True

        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM combo_performance WHERE combo_key = 'caller+work'"
        )
        (n,) = await cur.fetchone()
        assert n == 1
    finally:
        await db.close()


async def test_managed_write_reraises_when_the_caller_transaction_was_aborted(
    tmp_path,
):
    """The one documented exception to fail-soft, and the same one
    `record_alert_event` makes. If SQLite discarded the caller's transaction,
    swallowing would leave the caller believing a state change that no longer
    exists is still pending."""
    db = Database(tmp_path / "t.db")
    await db.initialize()

    class _AbortingConn:
        """Fails the INSERT the way the IO class does: transaction gone."""

        def __init__(self, real):
            self._real = real
            self.in_transaction = True

        async def execute(self, sql, params=()):
            if "alert_payloads" in sql:
                self.in_transaction = False
                raise aiosqlite.OperationalError("database or disk is full")
            return await self._real.execute(sql, params)

    real_conn = db._conn
    db._conn = _AbortingConn(real_conn)
    try:
        with pytest.raises(aiosqlite.OperationalError):
            await alert_events.record_alert_payload(db, "body", managed_txn=True)
    finally:
        db._conn = real_conn
        await db.close()


async def test_cancellation_propagates_out_of_the_unmanaged_path(tmp_path):
    """`CancelledError` is a `BaseException` and must not be swallowed — the
    preimage writer is not where shutdown goes to die."""
    db = Database(tmp_path / "t.db")
    await db.initialize()

    real_execute = db._conn.execute

    async def _cancel(sql, params=()):
        if "alert_payloads" in sql:
            raise __import__("asyncio").CancelledError()
        return await real_execute(sql, params)

    db._conn.execute = _cancel
    try:
        import asyncio

        with pytest.raises(asyncio.CancelledError):
            await alert_events.record_alert_payload(db, "body")
        assert not db._txn_lock.locked()
    finally:
        db._conn.execute = real_execute
        await db.close()


# --- migration -------------------------------------------------------------

# The pre-migration shape: an `alert_events` ledger with NO `alert_payloads`
# beside it. A fresh tmp_path DB would hit the already-migrated path and prove
# only that the DDL parses.
_OLD_SCHEMA = """
CREATE TABLE alert_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT NOT NULL,
    event_type       TEXT NOT NULL CHECK (event_type IN (
        'suppression_transition', 'parole_slot_spent', 'parole_slot_refunded',
        'reversal_pending_recorded', 'alert_dispatched', 'alert_delivered',
        'alert_failed', 'marker_stamped', 'marker_cleared', 'marker_anomaly',
        'refresh_completed', 'ledger_installed', 'parole_denied'
    )),
    combo_key        TEXT,
    signal_type      TEXT,
    alert_source     TEXT,
    transition       TEXT,
    detected_at      TEXT,
    delivery_result  TEXT,
    retry            INTEGER,
    payload_hash     TEXT,
    state_json       TEXT,
    detail           TEXT
);
"""


async def test_migration_lands_on_a_db_that_predates_it(tmp_path):
    """Upgrade path: the table appears, and the pre-existing ledger rows keep
    their digests untouched. There is no backfill and none is possible — the
    bodies behind those digests do not exist anywhere to recover from."""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO alert_events (created_at, event_type, payload_hash) "
        "VALUES ('2026-08-15T00:00:00+00:00', 'alert_delivered', 'deadbeef')"
    )
    conn.commit()
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()
    assert "alert_payloads" not in tables, "fixture must predate the migration"

    db = Database(db_path)
    await db.initialize()
    try:
        cur = await db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='alert_payloads'"
        )
        assert await cur.fetchone() is not None

        # Pre-cutover rows are PRESERVED and resolve to nothing.
        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM alert_events WHERE payload_hash = 'deadbeef'"
        )
        (n,) = await cur.fetchone()
        assert n == 1
        assert await alert_events.load_alert_payload(db, "deadbeef") is None
        assert await _payload_count(db) == 0

        cur = await db._conn.execute(
            "SELECT description FROM schema_version WHERE version = 20260817"
        )
        assert (await cur.fetchone())[0] == "alert_payloads_v1"
    finally:
        await db.close()


async def test_payload_hash_is_the_primary_key(tmp_path):
    """The PK IS the dedup mechanism. Without it `INSERT OR IGNORE` appends a
    body per referencing row and the content-addressing silently evaporates."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        cur = await db._conn.execute("PRAGMA table_info(alert_payloads)")
        info = {row[1]: row for row in await cur.fetchall()}
        assert info["payload_hash"][5] == 1
        assert info["payload"][3] == 1, "payload must be NOT NULL"
        assert info["byte_length"][3] == 1
        assert info["first_seen_at"][3] == 1
    finally:
        await db.close()


async def test_migration_is_idempotent_across_reinitialize(tmp_path):
    """Boots run the step every time. A second run must not disturb stored
    bodies or duplicate the version stamp."""
    db_path = tmp_path / "t.db"
    db = Database(db_path)
    await db.initialize()
    body = "a body that must survive the next boot"
    digest = await alert_events.record_alert_payload(db, body)
    await db.close()

    again = Database(db_path)
    await again.initialize()
    try:
        assert await alert_events.load_alert_payload(again, digest) == body
        assert await _payload_count(again) == 1
        cur = await again._conn.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version = 20260817"
        )
        (n,) = await cur.fetchone()
        assert n == 1
    finally:
        await again.close()
