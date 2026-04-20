"""Unit tests for scout.trading.qualifier_state (BL-050)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database


async def test_schema_creates_signal_qualifier_state_table(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cursor = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='signal_qualifier_state'"
    )
    row = await cursor.fetchone()
    assert row is not None, "signal_qualifier_state table must exist after initialize()"

    cursor = await db._conn.execute("PRAGMA table_info(signal_qualifier_state)")
    cols = {r[1]: r[2] for r in await cursor.fetchall()}
    assert cols == {
        "signal_type": "TEXT",
        "token_id": "TEXT",
        "first_qualified_at": "TEXT",
        "last_qualified_at": "TEXT",
    }

    cursor = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_sqs_last_qualified_at'"
    )
    assert await cursor.fetchone() is not None
    await db.close()


def test_config_defaults_for_qualifier_settings(settings_factory):
    s = settings_factory()
    assert s.QUALIFIER_EXIT_GRACE_HOURS == 48
    assert s.QUALIFIER_PRUNE_RETENTION_HOURS == 168
    assert s.QUALIFIER_PRUNE_EVERY_CYCLES == 100


def test_config_rejects_retention_le_grace(settings_factory):
    with pytest.raises(ValueError, match="QUALIFIER_PRUNE_RETENTION_HOURS"):
        settings_factory(
            QUALIFIER_EXIT_GRACE_HOURS=48,
            QUALIFIER_PRUNE_RETENTION_HOURS=48,
        )
    with pytest.raises(ValueError, match="QUALIFIER_PRUNE_RETENTION_HOURS"):
        settings_factory(
            QUALIFIER_EXIT_GRACE_HOURS=48,
            QUALIFIER_PRUNE_RETENTION_HOURS=24,
        )


from scout.trading.qualifier_state import classify_transitions


async def _qualifier_row(db, signal_type, token_id):
    cur = await db._conn.execute(
        "SELECT first_qualified_at, last_qualified_at FROM signal_qualifier_state "
        "WHERE signal_type = ? AND token_id = ?",
        (signal_type, token_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def _seed_qualifier(db, signal_type, token_id, first_at, last_at):
    await db._conn.execute(
        "INSERT OR REPLACE INTO signal_qualifier_state "
        "(signal_type, token_id, first_qualified_at, last_qualified_at) "
        "VALUES (?, ?, ?, ?)",
        (signal_type, token_id, first_at.isoformat(), last_at.isoformat()),
    )
    await db._conn.commit()


async def test_classify_returns_all_tokens_on_first_call(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)

    result = await classify_transitions(
        db,
        signal_type="first_signal",
        current_token_ids={"a", "b", "c"},
        now=now,
        exit_grace_hours=48,
    )
    # Returns dict[token_id -> prior_last_qualified_at]; None = no prior row.
    assert result == {"a": None, "b": None, "c": None}

    for tid in ("a", "b", "c"):
        row = await _qualifier_row(db, "first_signal", tid)
        assert row is not None
        assert row["first_qualified_at"] == now.isoformat()
        assert row["last_qualified_at"] == now.isoformat()
    await db.close()


async def test_classify_returns_empty_when_all_tokens_already_present(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    earlier = datetime(2026, 4, 19, 10, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)

    await _seed_qualifier(db, "first_signal", "a", earlier, earlier)
    await _seed_qualifier(db, "first_signal", "b", earlier, earlier)

    result = await classify_transitions(
        db,
        signal_type="first_signal",
        current_token_ids={"a", "b"},
        now=now,
        exit_grace_hours=48,
    )
    assert result == {}

    # last_qualified_at bumped to now; first_qualified_at preserved
    for tid in ("a", "b"):
        row = await _qualifier_row(db, "first_signal", tid)
        assert row["first_qualified_at"] == earlier.isoformat()
        assert row["last_qualified_at"] == now.isoformat()
    await db.close()


async def test_classify_returns_only_new_token(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    earlier = datetime(2026, 4, 19, 10, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)

    await _seed_qualifier(db, "first_signal", "a", earlier, earlier)

    result = await classify_transitions(
        db,
        signal_type="first_signal",
        current_token_ids={"a", "b"},
        now=now,
        exit_grace_hours=48,
    )
    # Only "b" transitioned; "b" has no prior row → prior is None.
    assert result == {"b": None}
    await db.close()


async def test_re_entry_outside_grace_counts_as_transition(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)
    stale = now - timedelta(hours=49)  # outside 48h grace

    await _seed_qualifier(db, "first_signal", "a", stale, stale)

    result = await classify_transitions(
        db,
        signal_type="first_signal",
        current_token_ids={"a"},
        now=now,
        exit_grace_hours=48,
    )
    # Re-entry transition; prior last_qualified_at is reported for observability.
    assert set(result.keys()) == {"a"}
    assert result["a"] == stale.isoformat()

    # first_qualified_at RESETS to now on re-entry
    row = await _qualifier_row(db, "first_signal", "a")
    assert row["first_qualified_at"] == now.isoformat()
    assert row["last_qualified_at"] == now.isoformat()
    await db.close()


async def test_re_entry_inside_grace_is_not_transition(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)
    recent = now - timedelta(hours=47)  # inside 48h grace

    await _seed_qualifier(db, "first_signal", "a", recent, recent)

    result = await classify_transitions(
        db,
        signal_type="first_signal",
        current_token_ids={"a"},
        now=now,
        exit_grace_hours=48,
    )
    assert result == {}

    row = await _qualifier_row(db, "first_signal", "a")
    assert row["first_qualified_at"] == recent.isoformat()  # preserved
    assert row["last_qualified_at"] == now.isoformat()       # bumped
    await db.close()


async def test_re_entry_exactly_at_grace_boundary_is_not_transition(tmp_path):
    """Boundary convention: last_qualified_at == now - grace → continuation (inclusive)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)
    exactly = now - timedelta(hours=48)  # exactly at boundary

    await _seed_qualifier(db, "first_signal", "a", exactly, exactly)

    result = await classify_transitions(
        db,
        signal_type="first_signal",
        current_token_ids={"a"},
        now=now,
        exit_grace_hours=48,
    )
    assert result == {}
    await db.close()


async def test_re_entry_one_second_past_grace_is_transition(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)
    stale = now - timedelta(hours=48, seconds=1)

    await _seed_qualifier(db, "first_signal", "a", stale, stale)

    result = await classify_transitions(
        db,
        signal_type="first_signal",
        current_token_ids={"a"},
        now=now,
        exit_grace_hours=48,
    )
    assert set(result.keys()) == {"a"}
    assert result["a"] == stale.isoformat()
    await db.close()


import aiosqlite


async def test_empty_current_ids_returns_empty_without_transaction(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.db")
    await db.initialize()

    async def _boom(*args, **kwargs):
        raise AssertionError("txn lock must not be acquired for empty input")

    # Patch the lock's acquire method to raise if called
    monkeypatch.setattr(db._txn_lock, "acquire", _boom)

    result = await classify_transitions(
        db,
        signal_type="first_signal",
        current_token_ids=set(),
        now=datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc),
        exit_grace_hours=48,
    )
    assert result == {}
    await db.close()


async def test_classify_raises_on_aiosqlite_error(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.db")
    await db.initialize()

    original_execute = db._conn.execute
    call_count = {"n": 0}

    async def _execute_then_fail(sql, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise aiosqlite.OperationalError("simulated failure")
        return await original_execute(sql, *args, **kwargs)

    monkeypatch.setattr(db._conn, "execute", _execute_then_fail)

    with pytest.raises(aiosqlite.OperationalError, match="simulated failure"):
        await classify_transitions(
            db,
            signal_type="first_signal",
            current_token_ids={"a"},
            now=datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc),
            exit_grace_hours=48,
        )
    await db.close()


async def test_different_signal_types_do_not_interfere(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)

    first_result = await classify_transitions(
        db,
        signal_type="first_signal",
        current_token_ids={"shared_token"},
        now=now,
        exit_grace_hours=48,
    )
    assert first_result == {"shared_token": None}

    # Same token under a different signal_type is a fresh transition
    other_result = await classify_transitions(
        db,
        signal_type="other_signal",
        current_token_ids={"shared_token"},
        now=now,
        exit_grace_hours=48,
    )
    assert other_result == {"shared_token": None}

    # Two independent rows exist
    cur = await db._conn.execute(
        "SELECT signal_type FROM signal_qualifier_state WHERE token_id = ?",
        ("shared_token",),
    )
    rows = {r[0] for r in await cur.fetchall()}
    assert rows == {"first_signal", "other_signal"}
    await db.close()


from scout.trading.qualifier_state import prune_stale_qualifiers


async def test_prune_stale_removes_old_rows_only(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)

    # Row A: last_qualified_at = 8 days ago → stale (retention 168h = 7 days)
    stale = now - timedelta(days=8)
    await _seed_qualifier(db, "first_signal", "stale_a", stale, stale)
    # Row B: last_qualified_at = 3 days ago → fresh
    fresh = now - timedelta(days=3)
    await _seed_qualifier(db, "first_signal", "fresh_b", fresh, fresh)

    deleted = await prune_stale_qualifiers(db, now=now, retention_hours=168)
    assert deleted == 1

    assert await _qualifier_row(db, "first_signal", "stale_a") is None
    assert await _qualifier_row(db, "first_signal", "fresh_b") is not None
    await db.close()


async def test_prune_retention_zero_raises_value_error(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="retention_hours"):
        await prune_stale_qualifiers(db, now=now, retention_hours=0)
    with pytest.raises(ValueError, match="retention_hours"):
        await prune_stale_qualifiers(db, now=now, retention_hours=-1)
    await db.close()


async def test_prune_returns_zero_when_no_stale_rows(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)

    # All rows fresh
    fresh = now - timedelta(days=2)
    await _seed_qualifier(db, "first_signal", "a", fresh, fresh)
    await _seed_qualifier(db, "first_signal", "b", fresh, fresh)

    deleted = await prune_stale_qualifiers(db, now=now, retention_hours=168)
    assert deleted == 0

    # Rows still present
    assert await _qualifier_row(db, "first_signal", "a") is not None
    assert await _qualifier_row(db, "first_signal", "b") is not None
    await db.close()


def test_heartbeat_stats_has_qualifier_counters():
    from scout.heartbeat import _heartbeat_stats, _reset_heartbeat_stats

    _reset_heartbeat_stats()
    assert _heartbeat_stats["qualifier_transitions"] == 0
    assert _heartbeat_stats["qualifier_skips"] == 0
    assert _heartbeat_stats["qualifier_prune_consecutive_failures"] == 0

    _heartbeat_stats["qualifier_transitions"] += 3
    _reset_heartbeat_stats()
    assert _heartbeat_stats["qualifier_transitions"] == 0
