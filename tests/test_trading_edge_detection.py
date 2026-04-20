"""BL-050 integration tests — end-to-end transition gate behavior
through trade_first_signals → engine.open_trade.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from scout.db import Database
from scout.heartbeat import _reset_heartbeat_stats
from scout.trading.signals import trade_first_signals


def _scored(token_factory, contract_address: str, mcap: float = 10_000_000):
    """Build a (token, quant_score, signals_fired) tuple matching the
    trade_first_signals input shape. chain must be 'coingecko' (the
    function skips other chains)."""
    token = token_factory(
        contract_address=contract_address,
        chain="coingecko",
        ticker="TST",
        token_name="Test",
        market_cap_usd=mcap,
    )
    return (token, 55, ["volume_acceleration"])


@pytest.fixture(autouse=True)
def _hb_reset():
    _reset_heartbeat_stats()
    yield
    _reset_heartbeat_stats()


async def test_restart_does_not_replay_qualifying_tokens(
    tmp_path, token_factory, settings_factory
):
    """Cycle N: 5 tokens transition → open_trade called 5x.
    Close DB, reopen same file. Cycle N+1 with same 5 tokens → open_trade
    called 0 times."""
    db_path = tmp_path / "t.db"
    db = Database(db_path)
    await db.initialize()

    # NOTE: `engine` is a full AsyncMock; engine-side gates (warmup, dedup,
    # cooldown, max-open) are bypassed entirely. We deliberately do NOT pass
    # PAPER_STARTUP_WARMUP_SECONDS here — trade_first_signals does not read it.
    settings = settings_factory(PAPER_MIN_MCAP=5_000_000)
    engine = AsyncMock()
    engine.open_trade = AsyncMock(return_value=1)

    candidates = [_scored(token_factory, f"addr_{i}") for i in range(5)]

    # Cycle N
    await trade_first_signals(engine, db, candidates, settings=settings)
    assert engine.open_trade.await_count == 5

    # Restart: close + re-open same DB file
    await db.close()
    db2 = Database(db_path)
    await db2.initialize()

    engine.open_trade.reset_mock()

    # Cycle N+1 — same tokens
    await trade_first_signals(engine, db2, candidates, settings=settings)
    assert engine.open_trade.await_count == 0
    await db2.close()


async def test_fresh_transition_opens_exactly_one_trade(
    tmp_path, token_factory, settings_factory
):
    """Cycle N: token A qualifies. Cycle N+1: tokens A+B qualify → open_trade
    called exactly once (for B)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()

    settings = settings_factory(PAPER_MIN_MCAP=5_000_000)
    engine = AsyncMock()
    engine.open_trade = AsyncMock(return_value=1)

    cycle_n = [_scored(token_factory, "addr_A")]
    cycle_n1 = [
        _scored(token_factory, "addr_A"),
        _scored(token_factory, "addr_B"),
    ]

    await trade_first_signals(engine, db, cycle_n, settings=settings)
    assert engine.open_trade.await_count == 1
    engine.open_trade.reset_mock()

    await trade_first_signals(engine, db, cycle_n1, settings=settings)
    assert engine.open_trade.await_count == 1
    # Confirm it was addr_B
    call_args = engine.open_trade.await_args
    assert call_args.kwargs["token_id"] == "addr_B"
    await db.close()


async def test_restart_with_re_entry_during_downtime(
    tmp_path, token_factory, settings_factory
):
    """Pre-seed qualifier row aged >grace. First post-restart scan with same
    token → fires exactly once (re-entry transition)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()

    # Seed a stale row — 49h ago, beyond 48h grace
    stale = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    await db._conn.execute(
        "INSERT INTO signal_qualifier_state "
        "(signal_type, token_id, first_qualified_at, last_qualified_at) "
        "VALUES (?, ?, ?, ?)",
        ("first_signal", "addr_re", stale, stale),
    )
    await db._conn.commit()

    settings = settings_factory(
        PAPER_MIN_MCAP=5_000_000,
        QUALIFIER_EXIT_GRACE_HOURS=48,
    )
    engine = AsyncMock()
    engine.open_trade = AsyncMock(return_value=1)

    await trade_first_signals(
        engine, db, [_scored(token_factory, "addr_re")], settings=settings
    )
    assert engine.open_trade.await_count == 1

    # Same cycle again (no restart) — this is a continuation now, must NOT fire
    engine.open_trade.reset_mock()
    await trade_first_signals(
        engine, db, [_scored(token_factory, "addr_re")], settings=settings
    )
    assert engine.open_trade.await_count == 0
    await db.close()


async def test_transition_blocked_by_cooldown_still_upserts(
    tmp_path, token_factory, settings_factory
):
    """Seed paper_trades row for token within 48h → open_trade returns None
    (the real engine's cooldown). classify_transitions must STILL upsert the
    qualifier row so next scan treats it as a continuation, not another
    transition.

    To avoid dependence on the real engine's cooldown implementation, we
    simulate open_trade returning None and then assert that a second call
    with the same token is NOT a transition (no second open_trade call).
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()

    settings = settings_factory(PAPER_MIN_MCAP=5_000_000)
    engine = AsyncMock()
    engine.open_trade = AsyncMock(return_value=None)  # simulate cooldown block

    candidates = [_scored(token_factory, "addr_block")]

    # First scan: transition classified, open_trade called, returns None
    await trade_first_signals(engine, db, candidates, settings=settings)
    assert engine.open_trade.await_count == 1

    # Row was upserted despite open_trade returning None
    cur = await db._conn.execute(
        "SELECT token_id FROM signal_qualifier_state "
        "WHERE signal_type = ? AND token_id = ?",
        ("first_signal", "addr_block"),
    )
    assert await cur.fetchone() is not None

    # Second scan: continuation, open_trade must NOT be called again
    engine.open_trade.reset_mock()
    await trade_first_signals(engine, db, candidates, settings=settings)
    assert engine.open_trade.await_count == 0
    await db.close()


async def test_transition_blocked_by_max_open_still_upserts(
    tmp_path, token_factory, settings_factory
):
    """Same contract as the cooldown test but with a different reason —
    open_trade returns None simulating the max-open cap being hit.
    Qualifier row must still be upserted; heartbeat skip counter increments.
    """
    from scout.heartbeat import _heartbeat_stats

    db = Database(tmp_path / "t.db")
    await db.initialize()

    settings = settings_factory(PAPER_MIN_MCAP=5_000_000)
    engine = AsyncMock()
    engine.open_trade = AsyncMock(return_value=None)  # simulate max-open block

    candidates = [_scored(token_factory, "addr_maxopen")]

    assert _heartbeat_stats["qualifier_transitions"] == 0
    assert _heartbeat_stats["qualifier_skips"] == 0

    await trade_first_signals(engine, db, candidates, settings=settings)

    assert engine.open_trade.await_count == 1
    assert _heartbeat_stats["qualifier_transitions"] == 1
    assert _heartbeat_stats["qualifier_skips"] == 1

    # Qualifier row upserted
    cur = await db._conn.execute(
        "SELECT token_id FROM signal_qualifier_state "
        "WHERE signal_type = ? AND token_id = ?",
        ("first_signal", "addr_maxopen"),
    )
    assert await cur.fetchone() is not None
    await db.close()
