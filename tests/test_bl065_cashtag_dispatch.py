"""BL-065: cashtag dispatch tests — schema, gate evaluation, end-to-end.

Tests gated by SKIP_AIOHTTP_TESTS=1 on Windows where they touch aiohttp/
network paths (matches Bundle A pattern).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

_SKIP_AIOHTTP = pytest.mark.skipif(
    sys.platform == "win32" and os.environ.get("SKIP_AIOHTTP_TESTS") == "1",
    reason="Windows + SKIP_AIOHTTP_TESTS=1: skip aiohttp tests",
)

from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_cashtag_trade_eligible_column_exists(db):
    """BL-065: schema migration adds column with NOT NULL DEFAULT 0."""
    cur = await db._conn.execute("PRAGMA table_info(tg_social_channels)")
    cols = {row[1]: (row[2], row[3], row[4]) for row in await cur.fetchall()}
    # (type, notnull, dflt_value)
    assert "cashtag_trade_eligible" in cols
    coltype, notnull, default = cols["cashtag_trade_eligible"]
    assert coltype == "INTEGER"
    assert notnull == 1
    assert default == "0"


@pytest.mark.asyncio
async def test_cashtag_trade_eligible_default_zero_for_new_channel(db):
    """New rows default to fail-closed (cashtag dispatch off)."""
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO tg_social_channels "
        "(channel_handle, display_name, trade_eligible, safety_required, added_at) "
        "VALUES (?, ?, 1, 1, ?)",
        ("@test", "Test", now),
    )
    await db._conn.commit()
    cur = await db._conn.execute(
        "SELECT cashtag_trade_eligible FROM tg_social_channels WHERE channel_handle='@test'"
    )
    assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_cashtag_trade_eligible_migration_paper_migrations_row(tmp_path):
    """Migration records bl065_cashtag_trade_eligible in paper_migrations
    (idempotency gate; second startup is a no-op)."""
    db = Database(tmp_path / "mig.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT name FROM paper_migrations WHERE name = ?",
        ("bl065_cashtag_trade_eligible",),
    )
    assert (await cur.fetchone()) is not None
    await db.close()


# ---------------------------------------------------------------------------
# BL-065 v3 dispatcher tests (Task 2)
# ---------------------------------------------------------------------------


from scout.social.telegram.dispatcher import (
    _channel_cashtag_trade_eligible,
    _evaluate_cashtag,
    dispatch_cashtag_to_engine,
)
from scout.social.telegram.models import ResolvedToken


def _candidate(
    token_id: str, symbol: str, mcap: float, price: float = 1.0
) -> ResolvedToken:
    """Build a cashtag-resolution candidate (no CA, safety_skipped_no_ca=True)."""
    return ResolvedToken(
        token_id=token_id,
        symbol=symbol,
        chain=None,
        contract_address=None,
        mcap=mcap,
        price_usd=price,
        safety_pass=False,
        safety_check_completed=False,
        safety_skipped_no_ca=True,
    )


async def _seed_channel(
    db, handle: str, *, trade_eligible=1, safety_required=1, cashtag=0
):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO tg_social_channels "
        "(channel_handle, display_name, trade_eligible, safety_required, "
        "cashtag_trade_eligible, added_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (handle, handle, trade_eligible, safety_required, cashtag, now),
    )
    await db._conn.commit()


@pytest.mark.asyncio
async def test_channel_cashtag_eligible_helper(db):
    await _seed_channel(db, "@on", cashtag=1)
    await _seed_channel(db, "@off", cashtag=0)
    assert await _channel_cashtag_trade_eligible(db, "@on") is True
    assert await _channel_cashtag_trade_eligible(db, "@off") is False
    assert await _channel_cashtag_trade_eligible(db, "@missing") is False


@pytest.mark.asyncio
async def test_evaluate_cashtag_blocked_when_channel_disabled(db, settings_factory):
    await _seed_channel(db, "@off", cashtag=0)
    s = settings_factory(
        PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD=100_000.0,
        PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO=2.0,
        PAPER_TG_SOCIAL_CASHTAG_MAX_PER_CHANNEL_PER_DAY=5,
        TG_SOCIAL_MAX_OPEN_TRADES=20,
    )
    decision = await _evaluate_cashtag(
        db=db,
        settings=s,
        candidates=[_candidate("token-a", "A", 5_000_000)],
        channel_handle="@off",
    )
    assert decision.dispatch_trade is False
    assert decision.blocked_gate == "cashtag_disabled"


@pytest.mark.asyncio
async def test_evaluate_cashtag_empty_candidates_returns_no_candidates_gate(
    db, settings_factory
):
    """R2#3 v2: empty candidates returns distinct gate (NOT cashtag_disabled)."""
    await _seed_channel(db, "@on", cashtag=1)
    s = settings_factory(
        PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD=100_000.0,
        PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO=2.0,
        PAPER_TG_SOCIAL_CASHTAG_MAX_PER_CHANNEL_PER_DAY=5,
        TG_SOCIAL_MAX_OPEN_TRADES=20,
    )
    decision = await _evaluate_cashtag(
        db=db, settings=s, candidates=[], channel_handle="@on"
    )
    assert decision.dispatch_trade is False
    assert decision.blocked_gate == "cashtag_no_candidates"


@pytest.mark.asyncio
async def test_evaluate_cashtag_blocked_when_below_floor(db, settings_factory):
    await _seed_channel(db, "@on", cashtag=1)
    s = settings_factory(
        PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD=100_000.0,
        PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO=2.0,
        PAPER_TG_SOCIAL_CASHTAG_MAX_PER_CHANNEL_PER_DAY=5,
        TG_SOCIAL_MAX_OPEN_TRADES=20,
    )
    decision = await _evaluate_cashtag(
        db=db,
        settings=s,
        candidates=[_candidate("token-dust", "D", 50_000)],
        channel_handle="@on",
    )
    assert decision.dispatch_trade is False
    assert decision.blocked_gate == "cashtag_below_floor"


@pytest.mark.asyncio
async def test_evaluate_cashtag_blocked_when_ambiguous(db, settings_factory):
    await _seed_channel(db, "@on", cashtag=1)
    s = settings_factory(
        PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD=100_000.0,
        PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO=2.0,
        PAPER_TG_SOCIAL_CASHTAG_MAX_PER_CHANNEL_PER_DAY=5,
        TG_SOCIAL_MAX_OPEN_TRADES=20,
    )
    decision = await _evaluate_cashtag(
        db=db,
        settings=s,
        candidates=[
            _candidate("token-top", "TOP", 5_000_000),
            _candidate("token-look", "LOOK", 4_000_000),
        ],
        channel_handle="@on",
    )
    assert decision.dispatch_trade is False
    assert decision.blocked_gate == "cashtag_ambiguous"


@pytest.mark.asyncio
async def test_evaluate_cashtag_passes_when_clearly_dominant(db, settings_factory):
    await _seed_channel(db, "@on", cashtag=1)
    s = settings_factory(
        PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD=100_000.0,
        PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO=2.0,
        PAPER_TG_SOCIAL_CASHTAG_MAX_PER_CHANNEL_PER_DAY=5,
        TG_SOCIAL_MAX_OPEN_TRADES=20,
    )
    decision = await _evaluate_cashtag(
        db=db,
        settings=s,
        candidates=[
            _candidate("token-clear", "CLR", 5_000_000),
            _candidate("token-other", "OTH", 1_000_000),
        ],
        channel_handle="@on",
    )
    assert decision.dispatch_trade is True
    assert decision.blocked_gate is None


@pytest.mark.asyncio
async def test_evaluate_cashtag_passes_when_only_one_candidate(db, settings_factory):
    """Single-candidate case: no disambiguity check needed."""
    await _seed_channel(db, "@on", cashtag=1)
    s = settings_factory(
        PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD=100_000.0,
        PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO=2.0,
        PAPER_TG_SOCIAL_CASHTAG_MAX_PER_CHANNEL_PER_DAY=5,
        TG_SOCIAL_MAX_OPEN_TRADES=20,
    )
    decision = await _evaluate_cashtag(
        db=db,
        settings=s,
        candidates=[_candidate("token-only", "ONLY", 1_000_000)],
        channel_handle="@on",
    )
    assert decision.dispatch_trade is True


@pytest.mark.asyncio
async def test_dispatch_cashtag_end_to_end_opens_paper_trade(
    db, settings_factory, monkeypatch
):
    """BL-065 acceptance: paper_trade opens with signal_data carrying
    {resolution, cashtag, candidate_rank, candidates_total}."""
    await _seed_channel(db, "@trusted", cashtag=1)
    s = settings_factory(
        PAPER_TG_SOCIAL_CASHTAG_TRADE_AMOUNT_USD=300.0,
        PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD=100_000.0,
        PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO=2.0,
        PAPER_TG_SOCIAL_CASHTAG_MAX_PER_CHANNEL_PER_DAY=5,
        TG_SOCIAL_MAX_OPEN_TRADES=20,
    )

    captured_calls = []

    class _StubEngine:
        async def open_trade(self, **kwargs):
            captured_calls.append(kwargs)
            return 42

    candidates = [
        _candidate("either-coin", "EITHER", 5_000_000),
        _candidate("either-token", "EITHER", 1_000_000),
    ]
    paper_trade_id, blocked = await dispatch_cashtag_to_engine(
        db=db,
        settings=s,
        engine=_StubEngine(),
        candidates=candidates,
        cashtag="EITHER",
        channel_handle="@trusted",
    )
    assert paper_trade_id == 42
    assert blocked is None
    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert call["signal_type"] == "tg_social"
    assert call["amount_usd"] == 300.0
    sd = call["signal_data"]
    assert sd["resolution"] == "cashtag"
    assert sd["cashtag"] == "$EITHER"
    assert sd["candidate_rank"] == 1
    assert sd["candidates_total"] == 2
    assert sd["channel_handle"] == "@trusted"


@pytest.mark.asyncio
async def test_dispatch_cashtag_logs_symbol_collision(
    db, settings_factory, monkeypatch
):
    """R1#6 v3 + R2#5 v3: when a cashtag trade opens with symbol matching
    another open tg_social trade (different token_id), INFO is logged
    (NOT WARNING — wallpaper antipattern for memecoins where every chain
    has its own PEPE)."""
    from scout.social.telegram import dispatcher as dispatcher_mod

    captured = []
    captured_warnings = []  # R3#3 v3: anti-wallpaper guard
    real_info = dispatcher_mod.log.info
    real_warning = dispatcher_mod.log.warning

    def _capture_info(event, **kwargs):
        captured.append((event, kwargs))
        return real_info(event, **kwargs)

    def _capture_warning(event, **kwargs):
        captured_warnings.append((event, kwargs))
        return real_warning(event, **kwargs)

    monkeypatch.setattr(dispatcher_mod.log, "info", _capture_info)
    monkeypatch.setattr(dispatcher_mod.log, "warning", _capture_warning)

    await _seed_channel(db, "@trusted", cashtag=1)
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_price, sl_price,
            status, opened_at)
           VALUES ('pepe','PEPE','Pepe','coingecko','tg_social','{}',
                   1.0, 300, 300, 1.2, 0.9, 'open', ?)""",
        (now,),
    )
    pt_cur = await db._conn.execute("SELECT last_insert_rowid()")
    existing_trade_id = (await pt_cur.fetchone())[0]
    await db._conn.execute(
        """INSERT INTO tg_social_messages
           (channel_handle, msg_id, posted_at, sender, text, cashtags,
            contracts, urls, parsed_at)
           VALUES ('@trusted', 99, ?, 'tester', 'test', '[]', '[]', '[]', ?)""",
        (now, now),
    )
    msg_cur = await db._conn.execute("SELECT last_insert_rowid()")
    await db._conn.execute(
        """INSERT INTO tg_social_signals
           (message_pk, token_id, symbol, contract_address, chain,
            mcap_at_sighting, resolution_state, source_channel_handle,
            paper_trade_id, created_at)
           VALUES (?, 'pepe', 'PEPE', NULL, NULL, 5000000.0,
                   'cashtag', '@trusted', ?, ?)""",
        ((await msg_cur.fetchone())[0], existing_trade_id, now),
    )
    await db._conn.commit()

    s = settings_factory(
        PAPER_TG_SOCIAL_CASHTAG_TRADE_AMOUNT_USD=300.0,
        PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD=100_000.0,
        PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO=2.0,
        PAPER_TG_SOCIAL_CASHTAG_MAX_PER_CHANNEL_PER_DAY=5,
        TG_SOCIAL_MAX_OPEN_TRADES=20,
    )

    class _StubEngine:
        async def open_trade(self, **kwargs):
            return 99

    paper_trade_id, blocked = await dispatch_cashtag_to_engine(
        db=db,
        settings=s,
        engine=_StubEngine(),
        candidates=[_candidate("pepe-bsc", "PEPE", 5_000_000)],
        cashtag="PEPE",
        channel_handle="@trusted",
    )
    assert paper_trade_id == 99  # trade STILL opens (informational, not block)

    collision_logs = [
        c for c in captured if c[0] == "tg_social_potential_duplicate_symbol"
    ]
    assert len(collision_logs) == 1
    _, kwargs = collision_logs[0]
    assert kwargs["symbol"] == "PEPE"
    assert kwargs["new_token_id"] == "pepe-bsc"
    assert "pepe" in kwargs["colliding_token_ids"]

    # R3#3 v3 anti-wallpaper guard: a future contributor "improving
    # observability" by re-promoting to WARNING would not break the INFO
    # assertion alone. Catch the regression by asserting NO collision
    # WARNING fires (R2#5 v3 design intent).
    collision_warnings = [
        c for c in captured_warnings
        if c[0] == "tg_social_potential_duplicate_symbol"
    ]
    assert collision_warnings == [], (
        f"symbol-collision must log INFO (anti-wallpaper for memecoins where "
        f"every chain has its own PEPE); got WARNING: {collision_warnings}"
    )


# ---------------------------------------------------------------------------
# R1-M1 v3: T1-T8 placeholder skip tests so build-phase gap is visible in CI
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t1_gate_f_blocks_when_channel_hits_daily_cap(db, settings_factory):
    """T1 (PR-review R3#4 promoted): seed CAP cashtag-resolution paper_trades
    opened today; assert next dispatch returns blocked_gate='cashtag_channel_rate_limited'.

    Critical: signal_data must have BOTH channel_handle AND resolution='cashtag'
    AND opened_at>=start of day for the json_extract count helper to count it.
    A under-specified fixture would make a green test that doesn't actually
    exercise the json_extract path."""
    import json

    await _seed_channel(db, "@noisy", cashtag=1)
    cap = 3  # smaller than default 5 — cheaper test fixture
    s = settings_factory(
        PAPER_TG_SOCIAL_CASHTAG_TRADE_AMOUNT_USD=300.0,
        PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD=100_000.0,
        PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO=2.0,
        PAPER_TG_SOCIAL_CASHTAG_MAX_PER_CHANNEL_PER_DAY=cap,
        TG_SOCIAL_MAX_OPEN_TRADES=20,
    )

    # Seed `cap` cashtag-resolution trades for @noisy opened today
    now = datetime.now(timezone.utc).isoformat()
    for i in range(cap):
        signal_data_json = json.dumps(
            {
                "channel_handle": "@noisy",
                "resolution": "cashtag",
                "cashtag": f"$T{i}",
                "candidate_rank": 1,
                "candidates_total": 1,
            }
        )
        await db._conn.execute(
            """INSERT INTO paper_trades
               (token_id, symbol, name, chain, signal_type, signal_data,
                entry_price, amount_usd, quantity, tp_price, sl_price,
                status, opened_at)
               VALUES (?, 'X', 'X', 'coingecko', 'tg_social', ?,
                       1.0, 300, 300, 1.2, 0.9, 'open', ?)""",
            (f"token-{i}", signal_data_json, now),
        )
    await db._conn.commit()

    decision = await _evaluate_cashtag(
        db=db,
        settings=s,
        candidates=[_candidate("token-fresh", "FRESH", 5_000_000)],
        channel_handle="@noisy",
    )
    assert decision.dispatch_trade is False
    assert decision.blocked_gate == "cashtag_channel_rate_limited"


@pytest.mark.asyncio
async def test_t1b_gate_f_does_not_count_other_channels(db, settings_factory):
    """Companion to T1: counter is per-channel, not global. Trades from
    @other should NOT exhaust @noisy's daily quota."""
    import json

    await _seed_channel(db, "@noisy", cashtag=1)
    await _seed_channel(db, "@other", cashtag=1)
    cap = 3
    s = settings_factory(
        PAPER_TG_SOCIAL_CASHTAG_TRADE_AMOUNT_USD=300.0,
        PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD=100_000.0,
        PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO=2.0,
        PAPER_TG_SOCIAL_CASHTAG_MAX_PER_CHANNEL_PER_DAY=cap,
        TG_SOCIAL_MAX_OPEN_TRADES=20,
    )
    now = datetime.now(timezone.utc).isoformat()
    # Seed CAP trades for @other (NOT @noisy)
    for i in range(cap):
        sd = json.dumps({"channel_handle": "@other", "resolution": "cashtag"})
        await db._conn.execute(
            """INSERT INTO paper_trades
               (token_id, symbol, name, chain, signal_type, signal_data,
                entry_price, amount_usd, quantity, tp_price, sl_price,
                status, opened_at)
               VALUES (?, 'X', 'X', 'coingecko', 'tg_social', ?,
                       1.0, 300, 300, 1.2, 0.9, 'open', ?)""",
            (f"token-other-{i}", sd, now),
        )
    await db._conn.commit()

    # @noisy should still pass — its quota is independent
    decision = await _evaluate_cashtag(
        db=db,
        settings=s,
        candidates=[_candidate("token-noisy", "NOISY", 5_000_000)],
        channel_handle="@noisy",
    )
    assert decision.dispatch_trade is True


# ---------------------------------------------------------------------------
# R3#2 v3 (PR-review MUST-FIX): Settings @field_validator coverage
# ---------------------------------------------------------------------------


def test_settings_cashtag_trade_amount_must_be_positive():
    """@field_validator: PAPER_TG_SOCIAL_CASHTAG_TRADE_AMOUNT_USD > 0."""
    from pydantic import ValidationError

    from scout.config import Settings

    with pytest.raises(ValidationError):
        Settings(
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            ANTHROPIC_API_KEY="k",
            PAPER_TG_SOCIAL_CASHTAG_TRADE_AMOUNT_USD=0,
        )


def test_settings_cashtag_min_mcap_must_be_positive():
    """@field_validator: PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD > 0."""
    from pydantic import ValidationError

    from scout.config import Settings

    with pytest.raises(ValidationError):
        Settings(
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            ANTHROPIC_API_KEY="k",
            PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD=-1,
        )


def test_settings_cashtag_disambiguity_ratio_must_be_at_least_one():
    """@field_validator: PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO >= 1.0
    (else top can't actually dominate the comparison — divide-by-near-zero
    risk + always-pass semantic)."""
    from pydantic import ValidationError

    from scout.config import Settings

    with pytest.raises(ValidationError):
        Settings(
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            ANTHROPIC_API_KEY="k",
            PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO=0.5,
        )


def test_settings_cashtag_max_per_channel_per_day_must_be_at_least_one():
    """@field_validator: PAPER_TG_SOCIAL_CASHTAG_MAX_PER_CHANNEL_PER_DAY >= 1
    (else cap=0 silently disables ALL cashtag dispatches — fail-loud is
    better than fail-quiet)."""
    from pydantic import ValidationError

    from scout.config import Settings

    with pytest.raises(ValidationError):
        Settings(
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            ANTHROPIC_API_KEY="k",
            PAPER_TG_SOCIAL_CASHTAG_MAX_PER_CHANNEL_PER_DAY=0,
        )


@pytest.mark.skip(reason="BL-065 build phase: implement T2 — Gate F passes under cap")
@pytest.mark.asyncio
async def test_t2_gate_f_passes_when_channel_under_cap(db, settings_factory):
    raise NotImplementedError("BL-065 build phase")


@pytest.mark.skip(
    reason="BL-065 build phase: implement T4 — DLQ-write failure does not kill listener"
)
@pytest.mark.asyncio
async def test_t4_dlq_write_failure_does_not_kill_listener(db, monkeypatch):
    """Monkeypatch _append_dlq to raise; assert listener loop continues,
    tg_social_dlq_write_failed log fires, original error context captured
    BEFORE the DLQ attempt. PR #55 listener-death class."""
    raise NotImplementedError("BL-065 build phase")


@pytest.mark.skip(reason="BL-065 build phase: implement T5 — CancelledError propagates")
@pytest.mark.asyncio
async def test_t5_cancellederror_propagates_not_swallowed(db, monkeypatch):
    """Monkeypatch dispatch_cashtag_to_engine to raise asyncio.CancelledError;
    assert it propagates (NOT swallowed into cashtag_dispatch_exception)."""
    raise NotImplementedError("BL-065 build phase")


@pytest.mark.skip(
    reason="BL-065 build phase: implement T6 — distinct cashtag_dispatch_exception gate"
)
@pytest.mark.asyncio
async def test_t6_other_exception_uses_distinct_gate_not_engine_rejected(
    db, monkeypatch
):
    """Monkeypatch dispatch_cashtag_to_engine to raise RuntimeError; assert
    listener catches, sets blocked_gate='cashtag_dispatch_exception' (NOT
    'engine_rejected')."""
    raise NotImplementedError("BL-065 build phase")


@pytest.mark.skip(
    reason="BL-065 build phase: implement T7 — _persist_signal_row failure does not kill listener"
)
@pytest.mark.asyncio
async def test_t7_persist_signal_row_failure_does_not_kill_listener(db, monkeypatch):
    """Monkeypatch _persist_signal_row to raise; assert listener catches,
    logs tg_social_persist_signal_failed, does NOT propagate (trade is
    already opened, lifecycle owns it)."""
    raise NotImplementedError("BL-065 build phase")


@pytest.mark.skip(
    reason="BL-065 build phase: implement T8 — second_mcap=0 passes top through"
)
@pytest.mark.asyncio
async def test_t8_disambiguity_with_zero_second_mcap_passes_top(db, settings_factory):
    """When second candidate has mcap=0, the `if second_mcap > 0` guard
    skips the comparison and top passes through."""
    raise NotImplementedError("BL-065 build phase")
