"""Tests for suppression entry-gate (spec §5.2)."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading import suppression


async def _seed_combo(
    db,
    key: str,
    *,
    window: str = "30d",
    trades: int = 0,
    wins: int = 0,
    suppressed: int = 0,
    suppressed_at: str | None = None,
    parole_at: str | None = None,
    parole_remaining: int | None = None,
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    losses = max(trades - wins, 0)
    wr = (wins / trades * 100.0) if trades else 0.0
    await db._conn.execute(
        "INSERT OR REPLACE INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, "
        " avg_pnl_pct, win_rate_pct, suppressed, suppressed_at, "
        " parole_at, parole_trades_remaining, refresh_failures, last_refreshed) "
        "VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, 0, ?)",
        (
            key,
            window,
            trades,
            wins,
            losses,
            wr,
            suppressed,
            suppressed_at,
            parole_at,
            parole_remaining,
            now_iso,
        ),
    )
    await db._conn.commit()


@pytest.fixture(autouse=True)
def _reset_fallback_state():
    suppression._fallback_timestamps.clear()
    suppression._last_alerted_ts = float("-inf")
    yield
    suppression._fallback_timestamps.clear()
    suppression._last_alerted_ts = float("-inf")


async def test_cold_start_allows(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    allow, reason = await suppression.should_open(db, "never_seen", settings=s)
    assert allow is True
    assert reason == "cold_start"
    await db.close()


async def test_not_suppressed_allows(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_combo(db, "good_combo", trades=30, wins=20, suppressed=0)
    allow, reason = await suppression.should_open(
        db, "good_combo", settings=settings_factory()
    )
    assert allow is True
    assert reason == "ok"
    await db.close()


async def test_suppressed_pre_parole_denies(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    await _seed_combo(
        db,
        "bad_combo",
        trades=25,
        wins=5,
        suppressed=1,
        suppressed_at=datetime.now(timezone.utc).isoformat(),
        parole_at=future,
        parole_remaining=5,
    )
    allow, reason = await suppression.should_open(
        db, "bad_combo", settings=settings_factory()
    )
    assert allow is False
    assert reason == "suppressed"
    await db.close()


async def test_parole_allows_and_decrements(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await _seed_combo(
        db,
        "parole_combo",
        trades=25,
        wins=5,
        suppressed=1,
        suppressed_at=past,
        parole_at=past,
        parole_remaining=3,
    )
    allow, reason = await suppression.should_open(
        db, "parole_combo", settings=settings_factory()
    )
    assert allow is True
    assert reason == "parole_retest"
    cur = await db._conn.execute(
        "SELECT parole_trades_remaining FROM combo_performance "
        "WHERE combo_key = ? AND window = '30d'",
        ("parole_combo",),
    )
    row = await cur.fetchone()
    assert row[0] == 2
    await db.close()


async def test_parole_exhausted_denies(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await _seed_combo(
        db,
        "exhausted",
        trades=25,
        wins=5,
        suppressed=1,
        suppressed_at=past,
        parole_at=past,
        parole_remaining=0,
    )
    allow, reason = await suppression.should_open(
        db, "exhausted", settings=settings_factory()
    )
    assert allow is False
    assert reason == "parole_exhausted"
    await db.close()


async def test_parole_boundary_at_exact_now(tmp_path, settings_factory):
    """When parole_at == now exactly, the window is open (not-in-future) → allow."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime.now(timezone.utc)
    await _seed_combo(
        db,
        "boundary",
        trades=25,
        wins=5,
        suppressed=1,
        suppressed_at=now.isoformat(),
        parole_at=now.isoformat(),
        parole_remaining=3,
    )
    allow, reason = await suppression.should_open(
        db, "boundary", settings=settings_factory()
    )
    assert allow is True
    assert reason == "parole_retest"
    await db.close()


async def test_concurrent_decrement_grants_only_one(tmp_path, settings_factory):
    """Per spec D16 — BEGIN IMMEDIATE + SQLite file-level locking serializes
    across SEPARATE aiosqlite connections (two Database objects pointing at the
    same DB file). A single shared connection is not a concurrency test (SQLite
    would reject nested BEGIN on the same conn), so we open two instances."""
    path = tmp_path / "race.db"
    seeder = Database(path)
    await seeder.initialize()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await _seed_combo(
        seeder,
        "race_combo",
        trades=25,
        wins=5,
        suppressed=1,
        suppressed_at=past,
        parole_at=past,
        parole_remaining=1,
    )
    await seeder.close()

    # Two independent connections — mimic what two signals-dispatcher paths
    # would see if they ever raced. In practice gecko-alpha is single-process
    # single-loop so this test upper-bounds the concurrency surface.
    db_a = Database(path)
    db_b = Database(path)
    await db_a.initialize()
    await db_b.initialize()
    s = settings_factory()
    results = await asyncio.gather(
        suppression.should_open(db_a, "race_combo", settings=s),
        suppression.should_open(db_b, "race_combo", settings=s),
    )
    reasons = sorted(r[1] for r in results)
    # One retest, one exhausted (in either order). OR — if SQLite serialization
    # causes one to fail with "database is locked" — that caller falls through
    # to the DB-error fallback-allow path, which is also acceptable per D17.
    assert (
        reasons == ["parole_exhausted", "parole_retest"]
        or "db_error_fallback_allow" in reasons
    ), f"unexpected reasons: {reasons}"
    # At most one successful decrement.
    cur = await db_a._conn.execute(
        "SELECT parole_trades_remaining FROM combo_performance "
        "WHERE combo_key='race_combo' AND window='30d'",
    )
    assert (await cur.fetchone())[0] == 0
    await db_a.close()
    await db_b.close()


async def test_db_locked_error_fallback_allows(tmp_path, monkeypatch, settings_factory):
    """A 'database is locked' OperationalError must fail-open (legacy behaviour)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    import aiosqlite

    async def _boom(*a, **k):
        raise aiosqlite.OperationalError("database is locked")

    monkeypatch.setattr(db._conn, "execute", _boom)
    allow, reason = await suppression.should_open(
        db, "whatever", settings=settings_factory()
    )
    assert allow is True
    assert reason == "db_error_fallback_allow"
    await db.close()


async def test_db_busy_error_fallback_allows(tmp_path, monkeypatch, settings_factory):
    """A 'database is busy' OperationalError must also fail-open."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    import aiosqlite

    async def _boom(*a, **k):
        raise aiosqlite.OperationalError("database is busy")

    monkeypatch.setattr(db._conn, "execute", _boom)
    allow, reason = await suppression.should_open(
        db, "whatever", settings=settings_factory()
    )
    assert allow is True
    assert reason == "db_error_fallback_allow"
    await db.close()


async def test_non_lock_operational_error_blocks(
    tmp_path, monkeypatch, settings_factory
):
    """A non-lock OperationalError (e.g. 'no such table') must BLOCK, not fail-open.

    Previously the broad except aiosqlite.Error treated all DB errors as lock
    contention and failed open — a schema-drift bug would silently ungated
    all combos. Now such errors return (False, 'error') to block the trade.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    import aiosqlite

    async def _boom(*a, **k):
        raise aiosqlite.OperationalError("no such table: combo_performance")

    monkeypatch.setattr(db._conn, "execute", _boom)
    allow, reason = await suppression.should_open(
        db, "whatever", settings=settings_factory()
    )
    assert allow is False, "Non-lock DB error must block, not fail-open"
    assert reason == "error"
    await db.close()


async def test_generic_db_error_blocks(tmp_path, monkeypatch, settings_factory):
    """A generic aiosqlite.Error (non-OperationalError) must also BLOCK."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    import aiosqlite

    async def _boom(*a, **k):
        raise aiosqlite.DatabaseError("corruption detected")

    monkeypatch.setattr(db._conn, "execute", _boom)
    allow, reason = await suppression.should_open(
        db, "whatever", settings=settings_factory()
    )
    assert allow is False, "Generic DB error must block, not fail-open"
    assert reason == "error"
    await db.close()


async def test_fallback_counter_alerts_at_threshold(
    tmp_path, monkeypatch, settings_factory
):
    """Lock-contention errors (message contains 'locked') must fail-open and
    trigger Telegram alerts once the fallback counter hits the threshold."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()  # threshold=5, cooldown=900 from defaults

    sent: list[tuple] = []

    async def _capture(text, session, settings):
        # Real alerter.send_telegram_message signature: (text, session, settings).
        sent.append((text, session, settings))

    import scout.alerter as _alerter

    monkeypatch.setattr(_alerter, "send_telegram_message", _capture)

    import aiosqlite

    async def _boom(*a, **k):
        # Must contain "locked" so the narrow check routes to fail-open path.
        raise aiosqlite.OperationalError("database is locked")

    monkeypatch.setattr(db._conn, "execute", _boom)

    for _ in range(5):
        await suppression.should_open(db, "x", settings=s)
    assert len(sent) == 1, f"expected 1 alert after threshold, got {len(sent)}"
    assert "fail-open" in sent[0][0].lower()
    # The third positional arg is the settings instance.
    assert sent[0][2] is s

    # Immediate 6th failure within cooldown — no new alert.
    await suppression.should_open(db, "x", settings=s)
    assert len(sent) == 1

    # Force cooldown expiry by rewinding _last_alerted_ts.
    suppression._last_alerted_ts = time.monotonic() - (
        s.FEEDBACK_FALLBACK_ALERT_COOLDOWN_SEC + 1
    )
    await suppression.should_open(db, "x", settings=s)
    assert len(sent) == 2
    await db.close()
