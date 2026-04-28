"""Per-channel safety_required tests.

The BL-064 dispatcher's Gate 4 part A originally fail-closed on
`safety_check_completed=False` (GoPlus 5xx / timeout / no-record). On prod
this rejected 95% of admission attempts because Pump.fun memecoins minted
~30 minutes ago aren't yet indexed by GoPlus, and the trade-eligible
curators (@thanos_mind, @detecter_calls) are exactly the audience for
those fresh tokens.

The fix: per-channel `safety_required` flag (default 1 = strict). When a
trusted curator sets it to 0, the no-record path is allowed through.
Honeypot/high-tax verdicts still block UNCONDITIONALLY because those are
definitive checks, not no-record fallbacks.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scout.db import Database
from scout.social.telegram.dispatcher import (
    _channel_safety_required,
    evaluate,
)
from scout.social.telegram.models import ResolvedToken


def _resolved(
    *,
    ca: str | None = "0xabc",
    chain: str | None = "ethereum",
    safe: bool = True,
    completed: bool = True,
    mcap: float = 1_000_000.0,
) -> ResolvedToken:
    return ResolvedToken(
        token_id="tok",
        symbol="TOK",
        chain=chain,
        contract_address=ca,
        mcap=mcap,
        price_usd=1.0,
        volume_24h_usd=100.0,
        safety_pass=safe,
        safety_check_completed=completed,
    )


async def _add_channel(
    db: Database,
    handle: str = "@gem",
    trade_eligible: int = 1,
    safety_required: int = 1,
):
    await db._conn.execute(
        "INSERT OR REPLACE INTO tg_social_channels "
        "(channel_handle, display_name, trade_eligible, safety_required, added_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            handle,
            "Gem",
            trade_eligible,
            safety_required,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    await db._conn.commit()


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_adds_safety_required_with_strict_default(tmp_path):
    """Fresh DB must have safety_required column with default 1 (strict)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(tg_social_channels)")
    cols = {row[1]: (row[2], row[3], row[4]) for row in await cur.fetchall()}
    assert "safety_required" in cols, "safety_required column must exist"
    type_, notnull, dflt_value = cols["safety_required"]
    assert type_ == "INTEGER"
    assert notnull == 1, "safety_required must be NOT NULL"
    assert dflt_value == "1", "default must be 1 (strict / fail-closed)"
    await db.close()


@pytest.mark.asyncio
async def test_migration_records_paper_migrations_row(tmp_path):
    """The bl064_safety_required_per_channel cutover row must be recorded
    so the post-assertion in db.py doesn't trip on an existing-DB upgrade."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT name FROM paper_migrations "
        "WHERE name = 'bl064_safety_required_per_channel'"
    )
    assert await cur.fetchone() is not None
    await db.close()


# ---------------------------------------------------------------------------
# _channel_safety_required helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safety_required_helper_defaults_strict_on_missing_channel(tmp_path):
    """Unknown handle → True (strict). Never silently lenient."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    assert await _channel_safety_required(db, "@nobody") is True
    await db.close()


@pytest.mark.asyncio
async def test_safety_required_helper_reads_strict_channel(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _add_channel(db, handle="@strict", safety_required=1)
    assert await _channel_safety_required(db, "@strict") is True
    await db.close()


@pytest.mark.asyncio
async def test_safety_required_helper_reads_lenient_channel(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _add_channel(db, handle="@lenient", safety_required=0)
    assert await _channel_safety_required(db, "@lenient") is False
    await db.close()


# ---------------------------------------------------------------------------
# Dispatcher behavior matrix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strict_channel_blocks_on_no_record(tmp_path, settings_factory):
    """Regression — pre-fix behavior preserved for channels that haven't
    opted into lenient. safety_check_completed=False → blocked."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _add_channel(db, handle="@strict", safety_required=1)
    decision = await evaluate(
        db=db,
        settings=settings_factory(),
        token=_resolved(completed=False, safe=False),  # no-record state
        channel_handle="@strict",
    )
    assert not decision.dispatch_trade
    assert decision.blocked_gate == "safety_unknown"
    await db.close()


@pytest.mark.asyncio
async def test_lenient_channel_passes_on_no_record(tmp_path, settings_factory):
    """Lenient channel + GoPlus no-record (5xx/timeout/no-data) → ALLOWS.
    This is the prod unblocker for fresh Pump.fun memecoins."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _add_channel(db, handle="@lenient", safety_required=0)
    decision = await evaluate(
        db=db,
        settings=settings_factory(),
        token=_resolved(completed=False, safe=False),  # no-record state
        channel_handle="@lenient",
    )
    assert decision.dispatch_trade is True, (
        f"lenient channel must allow no-record, got blocked: "
        f"{decision.blocked_gate}: {decision.reason}"
    )
    await db.close()


@pytest.mark.asyncio
async def test_lenient_channel_still_blocks_honeypot(tmp_path, settings_factory):
    """Even a lenient channel MUST block when GoPlus returned a definitive
    honeypot/high-tax verdict. Lenient only relaxes the no-record path."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _add_channel(db, handle="@lenient", safety_required=0)
    decision = await evaluate(
        db=db,
        settings=settings_factory(),
        token=_resolved(completed=True, safe=False),  # confirmed honeypot
        channel_handle="@lenient",
    )
    assert not decision.dispatch_trade
    assert decision.blocked_gate == "safety_failed"
    await db.close()


@pytest.mark.asyncio
async def test_strict_channel_blocks_honeypot(tmp_path, settings_factory):
    """Regression — strict channel blocks honeypot the same way as before."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _add_channel(db, handle="@strict", safety_required=1)
    decision = await evaluate(
        db=db,
        settings=settings_factory(),
        token=_resolved(completed=True, safe=False),  # confirmed honeypot
        channel_handle="@strict",
    )
    assert not decision.dispatch_trade
    assert decision.blocked_gate == "safety_failed"
    await db.close()


@pytest.mark.asyncio
async def test_lenient_channel_allows_safe_complete(tmp_path, settings_factory):
    """Sanity check — lenient channel + clean GoPlus pass → trades, same
    as strict channel would."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _add_channel(db, handle="@lenient", safety_required=0)
    decision = await evaluate(
        db=db,
        settings=settings_factory(),
        token=_resolved(completed=True, safe=True),
        channel_handle="@lenient",
    )
    assert decision.dispatch_trade is True
    await db.close()
