"""Tests for suppression entry-gate (spec §5.2)."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scout.db import Database
from scout.spikes.models import VolumeSpike
from scout.trading import signals, suppression


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

    async def _capture(text, session, settings, **_kwargs):
        # Real alerter.send_telegram_message accepts (text, session, settings,
        # *, parse_mode=..., ...) — accept **kwargs so the suppression.py
        # callsite passing parse_mode=None (per §12b hygiene) doesn't
        # break the mock with a TypeError that the production try/except
        # would then silently swallow.
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


# ---------------------------------------------------------------------------
# D1 — parole reservation: a retest slot must never be lost on an admission
# that never commits, and must never be returned when commit state is unknown.
# ---------------------------------------------------------------------------


async def _seed_parole(db, key: str, remaining: int) -> None:
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await _seed_combo(
        db,
        key,
        trades=25,
        wins=5,
        suppressed=1,
        suppressed_at=past,
        parole_at=past,
        parole_remaining=remaining,
    )


async def _remaining(db, key: str):
    cur = await db._conn.execute(
        "SELECT parole_trades_remaining FROM combo_performance "
        "WHERE combo_key = ? AND window = '30d'",
        (key,),
    )
    row = await cur.fetchone()
    return row[0] if row else None


class _StubEngine:
    """Engine double. `outcome` is open_trade's return, or an exception to raise."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    async def open_trade(self, **kwargs):
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


async def test_reservation_refunds_slot_on_verified_no_commit(
    tmp_path, settings_factory
):
    """open_trade -> None is a VERIFIED no-commit: the slot comes back."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_parole(db, "c", 3)
    async with suppression.parole_reservation(
        db, "c", settings=settings_factory()
    ) as res:
        assert res.allow is True
        assert res.reason == suppression.PAROLE_RETEST_REASON
        assert res.slot_taken is True
        # Decrement happens at the GATE — pessimistic, never deferred.
        assert await _remaining(db, "c") == 2
    assert await _remaining(db, "c") == 3
    await db.close()


async def test_reservation_keeps_slot_on_verified_commit(tmp_path, settings_factory):
    """confirm() marks a real commit — the slot stays spent."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_parole(db, "c", 3)
    async with suppression.parole_reservation(
        db, "c", settings=settings_factory()
    ) as res:
        res.confirm()
    assert await _remaining(db, "c") == 2
    await db.close()


async def test_reservation_never_refunds_on_exception(tmp_path, settings_factory):
    """THE over-admission guard.

    After `paper.execute_buy` returns a durable trade id, `open_trade` still
    awaits `_emit_decision` and then `_spawn_tg_alert`, neither individually
    guarded. An exception can surface with the paper_trades row already
    committed, so refunding on a raise would hand back a slot for a trade that
    exists. (`stamp_entry_snapshot` is NOT such a source — `execute_buy` wraps
    it fail-soft after the commit.)
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_parole(db, "c", 3)
    with pytest.raises(RuntimeError):
        async with suppression.parole_reservation(db, "c", settings=settings_factory()):
            assert await _remaining(db, "c") == 2
            raise RuntimeError("post-commit failure, row may already be durable")
    # Leaked, NOT refunded. Under-admits by one; never over-admits.
    assert await _remaining(db, "c") == 2
    await db.close()


async def test_reservation_token_is_settled_after_exception(tmp_path, settings_factory):
    """Pins the belt-and-braces `confirm()` on the ambiguous path.

    Control flow alone (the `raise` preceding the refund) already prevents the
    refund today. This pins the TOKEN state, so that relocating the refund into
    a `finally:` — the natural refactor — still cannot return an ambiguous slot.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_parole(db, "c", 3)
    holder = {}
    with pytest.raises(RuntimeError):
        async with suppression.parole_reservation(
            db, "c", settings=settings_factory()
        ) as res:
            holder["res"] = res
            raise RuntimeError("boom")
    assert holder["res"].settled is True
    # Even an explicit refund attempt after the fact must be a no-op.
    await holder["res"]._settle_refund()
    assert await _remaining(db, "c") == 2
    await db.close()


async def test_reservation_leaks_slot_on_cancellation(tmp_path, settings_factory):
    """CancelledError is a BaseException — still ambiguous, still no refund."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_parole(db, "c", 3)
    with pytest.raises(asyncio.CancelledError):
        async with suppression.parole_reservation(db, "c", settings=settings_factory()):
            raise asyncio.CancelledError()
    assert await _remaining(db, "c") == 2
    await db.close()


async def test_double_refund_is_impossible(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_parole(db, "c", 3)
    async with suppression.parole_reservation(
        db, "c", settings=settings_factory()
    ) as res:
        await res._settle_refund()
        assert await _remaining(db, "c") == 3
        await res._settle_refund()  # no-op
        assert await _remaining(db, "c") == 3
        assert res.settled is True
    # Context-manager exit must not refund a second time either.
    assert await _remaining(db, "c") == 3
    await db.close()


async def test_refund_is_clamped_at_grant_ceiling(tmp_path, settings_factory):
    """A concurrent combo_refresh re-grant must not be pushed above the ceiling."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    ceiling = s.FEEDBACK_PAROLE_RETEST_TRADES
    await _seed_parole(db, "c", ceiling)
    async with suppression.parole_reservation(db, "c", settings=s):
        assert await _remaining(db, "c") == ceiling - 1
        # combo_refresh re-arms parole mid-flight.
        await db._conn.execute(
            "UPDATE combo_performance SET parole_trades_remaining = ? "
            "WHERE combo_key = 'c' AND window = '30d'",
            (ceiling,),
        )
        await db._conn.commit()
    assert await _remaining(db, "c") == ceiling
    await db.close()


async def test_no_slot_taken_means_no_refund(tmp_path, settings_factory):
    """Non-parole reasons never touch the counter in either direction."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_combo(db, "good", trades=30, wins=20, suppressed=0)
    async with suppression.parole_reservation(
        db, "good", settings=settings_factory()
    ) as res:
        assert res.allow is True
        assert res.reason == "ok"
        assert res.slot_taken is False
    assert await _remaining(db, "good") is None
    await db.close()


async def test_exhausted_parole_takes_no_slot(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_parole(db, "c", 0)
    async with suppression.parole_reservation(
        db, "c", settings=settings_factory()
    ) as res:
        assert res.allow is False
        assert res.reason == "parole_exhausted"
        assert res.slot_taken is False
    assert await _remaining(db, "c") == 0
    await db.close()


async def test_slot_leaks_across_process_death_by_design(tmp_path, settings_factory):
    """Pins the DELIBERATE non-durability of the token.

    The reservation is process-local and transaction-local. If the process dies
    between decrement and refund the slot is lost — under-admission, the
    required failure direction. Do not "fix" this with cross-restart state.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_parole(db, "c", 3)
    cm = suppression.parole_reservation(db, "c", settings=settings_factory())
    await cm.__aenter__()  # decrement lands
    assert await _remaining(db, "c") == 2
    del cm  # process dies; __aexit__ never runs
    assert await _remaining(db, "c") == 2
    await db.close()


async def test_dispatcher_refunds_when_open_trade_declines(tmp_path, settings_factory):
    """End-to-end through a real dispatcher, not just the primitive."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_parole(db, "volume_spike", 3)
    spike = VolumeSpike(
        coin_id="realcoin",
        symbol="REAL",
        name="Real Coin",
        current_volume=1_000_000.0,
        avg_volume_7d=100_000.0,
        spike_ratio=10.0,
        market_cap=50_000_000.0,
        price=1.0,
        detected_at=datetime.now(timezone.utc),
    )
    engine = _StubEngine(None)  # verified no-commit
    await signals.trade_volume_spikes(engine, db, [spike], settings_factory())
    assert engine.calls == 1
    assert await _remaining(db, "volume_spike") == 3
    await db.close()


async def test_dispatcher_leaks_when_open_trade_raises(tmp_path, settings_factory):
    """The dispatcher's own `except Exception` must not launder a raise into a refund."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_parole(db, "volume_spike", 3)
    spike = VolumeSpike(
        coin_id="realcoin",
        symbol="REAL",
        name="Real Coin",
        current_volume=1_000_000.0,
        avg_volume_7d=100_000.0,
        spike_ratio=10.0,
        market_cap=50_000_000.0,
        price=1.0,
        detected_at=datetime.now(timezone.utc),
    )
    engine = _StubEngine(RuntimeError("post-commit boom"))
    await signals.trade_volume_spikes(engine, db, [spike], settings_factory())
    assert engine.calls == 1
    assert await _remaining(db, "volume_spike") == 2
    await db.close()


async def test_dispatcher_keeps_slot_on_commit(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_parole(db, "volume_spike", 3)
    spike = VolumeSpike(
        coin_id="realcoin",
        symbol="REAL",
        name="Real Coin",
        current_volume=1_000_000.0,
        avg_volume_7d=100_000.0,
        spike_ratio=10.0,
        market_cap=50_000_000.0,
        price=1.0,
        detected_at=datetime.now(timezone.utc),
    )
    engine = _StubEngine(4242)  # verified commit
    await signals.trade_volume_spikes(engine, db, [spike], settings_factory())
    assert await _remaining(db, "volume_spike") == 2
    await db.close()


def test_every_dispatcher_uses_the_reservation_contract():
    """Caller-drift guard: a 9th dispatcher must not reintroduce the raw gate."""
    src = (
        Path(__file__).resolve().parents[1] / "scout" / "trading" / "signals.py"
    ).read_text(encoding="utf-8")
    raw_gate = "allow, reason = await " + "should_open"
    assert raw_gate not in src, "a dispatcher bypasses parole_reservation"
    assert src.count("async with parole_reservation(") == 8
    assert src.count("res.confirm()") == 8


def test_no_dispatcher_calls_the_refund_primitive_directly():
    """Over-admission must be STRUCTURAL, not caller discipline.

    A dispatcher that refunded manually and then committed would over-admit.
    Refund is internal to the context-manager contract; pin that no caller
    reaches around it.
    """
    src = (
        Path(__file__).resolve().parents[1] / "scout" / "trading" / "signals.py"
    ).read_text(encoding="utf-8")
    assert "settle_refund" not in src
    assert "refund_parole_slot" not in src


async def _resuppress(db, key: str, remaining: int = 5):
    """Emulate combo_refresh installing a NEW suppression generation."""
    now = datetime.now(timezone.utc)
    await db._conn.execute(
        "UPDATE combo_performance SET suppressed = 1, suppressed_at = ?, "
        "parole_at = ?, parole_trades_remaining = ? "
        "WHERE combo_key = ? AND window = '30d'",
        (
            now.isoformat(),
            (now + timedelta(days=14)).isoformat(),
            remaining,
            key,
        ),
    )
    await db._conn.commit()


async def test_resuppression_between_preliminary_read_and_lock_denies_admission(
    tmp_path, settings_factory
):
    """TOCTOU regression.

    `_open_gate` validates suppressed/parole_at on a LOCK-FREE fast path. If
    combo_refresh re-suppresses with a fresh future parole_at + full budget
    before the caller obtains `_txn_lock`, the locked block must re-validate
    the whole admission state — not just see `remaining > 0` and admit inside
    the NEW lock period.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_parole(db, "c", 3)  # window currently OPEN
    s = settings_factory()

    # Hold the lock so the gate blocks after its lock-free read.
    await db._txn_lock.acquire()
    task = asyncio.create_task(suppression.should_open(db, "c", settings=s))
    for _ in range(200):
        await asyncio.sleep(0.005)
        if db._txn_lock._waiters:
            break

    await _resuppress(db, "c", remaining=5)  # new generation lands
    db._txn_lock.release()

    allow, reason = await task
    assert allow is False
    assert reason == "suppressed"
    # The replacement generation's budget is untouched.
    assert await _remaining(db, "c") == 5
    await db.close()


async def test_clearance_between_preliminary_read_and_lock_allows_without_spending(
    tmp_path, settings_factory
):
    """The mirror of the re-suppression race.

    If combo_refresh CLEARS suppression while the caller queues for the lock,
    the locked block must notice and allow on the ordinary `ok` path — not
    spend a parole slot, and not deny because `parole_at` went NULL.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_parole(db, "c", 3)
    s = settings_factory()

    await db._txn_lock.acquire()
    task = asyncio.create_task(suppression.should_open(db, "c", settings=s))
    for _ in range(200):
        await asyncio.sleep(0.005)
        if db._txn_lock._waiters:
            break

    await db._conn.execute(
        "UPDATE combo_performance SET suppressed = 0, suppressed_at = NULL, "
        "parole_at = NULL, parole_trades_remaining = NULL "
        "WHERE combo_key = 'c' AND window = '30d'"
    )
    await db._conn.commit()
    db._txn_lock.release()

    allow, reason = await task
    assert allow is True
    assert reason == "ok"
    assert await _remaining(db, "c") is None  # no slot spent
    await db.close()


async def test_stale_refund_does_not_credit_replacement_generation(
    tmp_path, settings_factory
):
    """A slot taken from generation N must never be returned to generation N+1."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_parole(db, "c", 3)
    async with suppression.parole_reservation(
        db, "c", settings=settings_factory()
    ) as res:
        assert res.slot_taken is True
        assert await _remaining(db, "c") == 2
        await _resuppress(db, "c", remaining=5)
        # exit without confirm -> refund attempted against a dead generation
    assert await _remaining(db, "c") == 5  # NOT 6
    await db.close()


async def test_stale_refund_after_suppression_cleared_is_noop(
    tmp_path, settings_factory
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_parole(db, "c", 3)
    async with suppression.parole_reservation(db, "c", settings=settings_factory()):
        await db._conn.execute(
            "UPDATE combo_performance SET suppressed = 0, suppressed_at = NULL, "
            "parole_at = NULL, parole_trades_remaining = NULL "
            "WHERE combo_key = 'c' AND window = '30d'"
        )
        await db._conn.commit()
    # Must not resurrect a counter on a combo that is no longer suppressed.
    assert await _remaining(db, "c") is None
    await db.close()
