"""`parole_denied` must record a denial STATE, not a denial ATTEMPT.

THE DEFECT (measured, not hypothetical). The C3 brief that added
`parole_denied` asserted denials were low-frequency "only during open parole
windows". That premise was wrong. Once a combo latches — `suppressed=1` and the
parole window OPEN and `parole_trades_remaining=0` — EVERY dispatch attempt
takes the `parole_exhausted` branch and appends a byte-identical row, forever.
Prod wrote 20,094 such rows in 17h across 3 latched combos (~1,200/hour).

THE RULE. Persist the FIRST occurrence of a distinct denial state within a
suppression generation: keyed on combo + denial reason + generation identity.
A later MEANINGFUL change (different reason, or a new generation) still records;
byte-identical repetition collapses.

`generation_changed_before_reservation` is deliberately NOT deduped — it
describes a race OCCURRENCE, not a latched steady state, and prod measured
ZERO of them over the same 17h window in which `parole_exhausted` fired 20,094
times.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from scout import db as db_module
from scout.db import _ALERT_EVENTS_DDL, Database
from scout.trading import suppression
from scout.trading.alert_events import denial_digest, record_alert_event


async def _seed_30d(db, combo_key, **cols):
    """A latched, parole-window-OPEN, budget-spent 30d row — the exact shape
    the three prod combos are in."""
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


async def _count(db, **where):
    clause = " AND ".join(f"{k} = ?" for k in where)
    cur = await db._conn.execute(
        f"SELECT COUNT(*) FROM alert_events WHERE {clause}", tuple(where.values())
    )
    return (await cur.fetchone())[0]


async def _remaining(db, combo_key):
    cur = await db._conn.execute(
        "SELECT parole_trades_remaining FROM combo_performance "
        "WHERE combo_key = ? AND window = '30d'",
        (combo_key,),
    )
    return (await cur.fetchone())[0]


# --- 1. the latched steady state collapses --------------------------------


async def test_latched_combo_records_exactly_one_denial(tmp_path, settings_factory):
    """The prod shape: N consecutive denials, one generation, one reason.
    COUNT, not truthiness — `if rows:` passes on 20,094 rows just as happily
    as on one."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        await _seed_30d(db, "combo_a")
        for _ in range(25):
            assert await suppression.should_open(db, "combo_a", settings=s) == (
                False,
                "parole_exhausted",
            )
        assert (await _count(db, event_type="parole_denied", combo_key="combo_a")) == 1
    finally:
        await db.close()


# --- 2. a CHANGED denial reason is still a new durable fact ---------------


def test_denial_digest_discriminates_the_denial_reason():
    """The half that separates the ruling from naive once-per-generation: the
    reason is IN the key. Drop it and these two digests collapse."""
    common = dict(
        combo_key="combo_a",
        suppressed=1,
        suppressed_at="2026-08-01T00:00:00+00:00",
        parole_at="2026-08-10T00:00:00+00:00",
    )
    exhausted = denial_digest(denial_reason="parole_exhausted", **common)
    other = denial_digest(denial_reason="some_other_reason", **common)
    assert exhausted != other


async def test_changed_denial_reason_writes_a_second_row(tmp_path):
    """Same combo, same generation, DIFFERENT reason -> a second row. A
    once-per-generation implementation fails here."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        common = dict(
            combo_key="combo_a",
            suppressed=1,
            suppressed_at="2026-08-01T00:00:00+00:00",
            parole_at="2026-08-10T00:00:00+00:00",
        )
        for reason in ("parole_exhausted", "parole_exhausted", "some_other_reason"):
            await record_alert_event(
                db,
                event_type="parole_denied",
                combo_key="combo_a",
                transition=reason,
                payload_hash=denial_digest(denial_reason=reason, **common),
                dedupe_on_payload_hash=True,
            )
        assert (await _count(db, event_type="parole_denied")) == 2
    finally:
        await db.close()


def test_denial_digest_discriminates_the_generation():
    """Generation identity is `suppressed_at` + `parole_at`. Either moving is a
    new generation and a new durable fact."""
    common = dict(combo_key="combo_a", denial_reason="parole_exhausted", suppressed=1)
    base = denial_digest(
        suppressed_at="2026-08-01T00:00:00+00:00",
        parole_at="2026-08-10T00:00:00+00:00",
        **common,
    )
    moved_latch = denial_digest(
        suppressed_at="2026-08-02T00:00:00+00:00",
        parole_at="2026-08-10T00:00:00+00:00",
        **common,
    )
    moved_parole = denial_digest(
        suppressed_at="2026-08-01T00:00:00+00:00",
        parole_at="2026-08-11T00:00:00+00:00",
        **common,
    )
    assert len({base, moved_latch, moved_parole}) == 3


def test_denial_digest_normalises_bool_against_int():
    """`bool` IS an `int`. SQLite hands `suppressed` back as 1; a caller passing
    `True` for the same state must not produce a second row."""
    common = dict(
        combo_key="combo_a",
        denial_reason="parole_exhausted",
        suppressed_at="2026-08-01T00:00:00+00:00",
        parole_at="2026-08-10T00:00:00+00:00",
    )
    assert denial_digest(suppressed=1, **common) == denial_digest(
        suppressed=True, **common
    )


def test_denial_digest_separates_null_from_empty_and_resists_component_bleed():
    """Two distinct component tuples must never join to one string. Without a
    separator, `combo_key='a'` + reason `'b'` and `combo_key='ab'` + reason `''`
    are the same bytes; without a NULL sentinel, absent and empty collapse."""
    gen = dict(
        suppressed=1,
        suppressed_at="2026-08-01T00:00:00+00:00",
        parole_at="2026-08-10T00:00:00+00:00",
    )
    bleed = {
        denial_digest(combo_key="a", denial_reason="b", **gen),
        denial_digest(combo_key="ab", denial_reason="", **gen),
    }
    assert len(bleed) == 2

    nulls = {
        denial_digest(combo_key=None, denial_reason="r", **gen),
        denial_digest(combo_key="", denial_reason="r", **gen),
    }
    assert len(nulls) == 2


# --- 3. a new generation records again ------------------------------------


async def test_new_generation_records_a_new_denial(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        now = datetime.now(timezone.utc)
        await _seed_30d(db, "combo_a")
        for _ in range(5):
            await suppression.should_open(db, "combo_a", settings=s)
        assert (await _count(db, event_type="parole_denied")) == 1

        # combo_refresh re-latches: new suppressed_at, new parole boundary,
        # budget still spent. Same combo, same reason, DIFFERENT generation.
        await db._conn.execute(
            "UPDATE combo_performance SET suppressed_at = ?, parole_at = ? "
            "WHERE combo_key = 'combo_a' AND window = '30d'",
            (
                (now - timedelta(days=2)).isoformat(),
                (now - timedelta(hours=1)).isoformat(),
            ),
        )
        await db._conn.commit()

        for _ in range(5):
            assert await suppression.should_open(db, "combo_a", settings=s) == (
                False,
                "parole_exhausted",
            )
        assert (await _count(db, event_type="parole_denied")) == 2
    finally:
        await db.close()


# --- 4. the race stays per-occurrence -------------------------------------


async def test_generation_change_denial_is_per_occurrence(tmp_path, settings_factory):
    """ZERO of these fired in 17h of prod while `parole_exhausted` fired
    20,094 times. It is an occurrence, not a state — N races, N rows."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        now = datetime.now(timezone.utc)
        await _seed_30d(db, "combo_a", parole_trades_remaining=5)

        real_execute = db._conn.execute
        relatches = {"n": 0}

        async def _relatch_on_begin(sql, *a, **k):
            if str(sql).strip().upper().startswith("BEGIN IMMEDIATE"):
                cur = await real_execute(sql, *a, **k)
                # Push the window into the future INSIDE the transaction, then
                # pull it back after the gate returns, so every call re-races
                # against an identical state.
                await real_execute(
                    "UPDATE combo_performance SET parole_at = ? "
                    "WHERE combo_key='combo_a' AND window='30d'",
                    ((now + timedelta(days=9)).isoformat(),),
                )
                relatches["n"] += 1
                return cur
            return await real_execute(sql, *a, **k)

        for _ in range(4):
            db._conn.execute = _relatch_on_begin
            try:
                assert await suppression.should_open(db, "combo_a", settings=s) == (
                    False,
                    "suppressed",
                )
            finally:
                db._conn.execute = real_execute
            # Restore the open window for the next attempt's fast path.
            await db._conn.execute(
                "UPDATE combo_performance SET parole_at = ? "
                "WHERE combo_key='combo_a' AND window='30d'",
                ((now - timedelta(days=6)).isoformat(),),
            )
            await db._conn.commit()

        assert relatches["n"] == 4, "the fixture never re-latched"
        assert (
            await _count(
                db,
                event_type="parole_denied",
                transition="generation_changed_before_reservation",
            )
        ) == 4
    finally:
        await db.close()


# --- 5. the DECISION is untouched -----------------------------------------


async def test_dedup_does_not_change_the_denial_decision(tmp_path, settings_factory):
    """Observability only. Every call returns the identical
    (allow, reason, generation) tuple and no slot is consumed or returned."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        s = settings_factory()
        await _seed_30d(db, "combo_a")
        for _ in range(10):
            assert await suppression._open_gate(db, "combo_a", settings=s) == (
                False,
                "parole_exhausted",
                None,
            )
            assert await _remaining(db, "combo_a") == 0

        # The admission path is likewise unchanged: a combo with budget still
        # spends exactly one slot and reports its generation.
        await _seed_30d(db, "combo_b", parole_trades_remaining=2)
        allow, reason, generation = await suppression._open_gate(
            db, "combo_b", settings=s
        )
        assert (allow, reason) == (True, suppression.PAROLE_RETEST_REASON)
        assert generation is not None and len(generation) == 3
        assert await _remaining(db, "combo_b") == 1
    finally:
        await db.close()


# --- 6. the dedup state is DURABLE, not in-memory -------------------------


async def test_dedup_state_survives_a_restart(tmp_path, settings_factory):
    """Module counters reset on restart; the ledger does not. Reopening must
    neither re-write the collapsed row nor swallow a genuine first row."""
    path = tmp_path / "t.db"
    s = settings_factory()

    db = Database(path)
    await db.initialize()
    try:
        await _seed_30d(db, "combo_a")
        await suppression.should_open(db, "combo_a", settings=s)
        assert (await _count(db, event_type="parole_denied")) == 1
    finally:
        await db.close()

    db = Database(path)
    await db.initialize()
    try:
        # Same state, new process: still ONE row.
        for _ in range(5):
            await suppression.should_open(db, "combo_a", settings=s)
        assert (await _count(db, event_type="parole_denied")) == 1

        # A genuinely new denial state after the restart is NOT swallowed.
        await _seed_30d(db, "combo_b")
        await suppression.should_open(db, "combo_b", settings=s)
        assert (await _count(db, event_type="parole_denied", combo_key="combo_b")) == 1
    finally:
        await db.close()


# --- the dedup write must be index-supported -------------------------------


async def test_dedup_probe_is_index_supported(tmp_path):
    """A per-dispatch FULL SCAN of a ledger that only grows would trade one
    unbounded cost for a worse one."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        cur = await db._conn.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM alert_events "
            "WHERE event_type = ? AND payload_hash = ?",
            ("parole_denied", "x"),
        )
        plan = " ".join(str(r[-1]) for r in await cur.fetchall())
        assert "SCAN" not in plan.upper(), plan
        assert "idx_alert_events_dedup" in plan, plan
    finally:
        await db.close()


async def test_missing_dedup_index_fails_the_boot_loudly(tmp_path, monkeypatch):
    """The index has ONE definition, in `_ALERT_EVENTS_INDEXES`. Drop it there
    and the `alert_events_dedup_index_v1` step must refuse to boot — a probe
    that silently degrades to a full scan is the failure this whole change
    exists to avoid."""
    monkeypatch.setattr(
        db_module,
        "_ALERT_EVENTS_INDEXES",
        tuple(i for i in db_module._ALERT_EVENTS_INDEXES if "_dedup " not in i),
    )
    db = Database(tmp_path / "t.db")
    with pytest.raises(RuntimeError, match="idx_alert_events_dedup"):
        await db.initialize()


async def test_upgrade_rebuild_keeps_the_index_and_the_existing_rows(tmp_path):
    """The vocabulary-drift rebuild DROPs `alert_events`, taking its indexes
    with it. Built from the PRE-`parole_denied` shape so the rebuild actually
    runs — a fresh tmp_path DB would skip it and pass vacuously.

    Also pins the operator ruling on the 20,094 prod rows: existing rows are
    truthful observations of a bad instrumentation policy and are preserved.
    Any compaction is a separate, explicitly-ruled migration."""
    db_path = tmp_path / "old.db"
    old_ddl = _ALERT_EVENTS_DDL.replace(",\n        'parole_denied'", "")
    conn = sqlite3.connect(str(db_path))
    conn.executescript(old_ddl)
    for i in range(3):
        conn.execute(
            "INSERT INTO alert_events (created_at, event_type, combo_key, "
            "transition, payload_hash) VALUES (?, 'parole_slot_spent', "
            "'combo_a', 'parole_retest', ?)",
            (f"2026-08-15T0{i}:00:00+00:00", f"hash{i}"),
        )
    conn.commit()
    conn.close()
    assert "parole_denied" not in old_ddl, "fixture already has the new member"

    db = Database(db_path)
    await db.initialize()
    try:
        cur = await db._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_alert_events_dedup'"
        )
        assert await cur.fetchone() is not None, "rebuild dropped the index"

        cur = await db._conn.execute(
            "SELECT created_at, payload_hash FROM alert_events "
            "WHERE event_type = 'parole_slot_spent' ORDER BY id"
        )
        assert [tuple(r) for r in await cur.fetchall()] == [
            ("2026-08-15T00:00:00+00:00", "hash0"),
            ("2026-08-15T01:00:00+00:00", "hash1"),
            ("2026-08-15T02:00:00+00:00", "hash2"),
        ]
    finally:
        await db.close()


async def test_dedupe_without_a_hash_is_a_wiring_error(tmp_path):
    """`payload_hash IS NULL` never satisfies `= ?`, so a NULL key would
    silently degrade back to a row per attempt. Fail loudly instead."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        with pytest.raises(ValueError):
            await record_alert_event(
                db,
                event_type="parole_denied",
                combo_key="combo_a",
                dedupe_on_payload_hash=True,
            )
    finally:
        await db.close()
