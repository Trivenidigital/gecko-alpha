"""W3 — resolver facts forwarded into `signal_data` on BOTH dispatch paths.

`price_usd`, `volume_24h_usd`, `age_days` and the safety trio all exist on
`ResolvedToken` and were being dropped at the dispatcher boundary. Widening
them is a field addition, not a new mechanism: no gate reads them, and the
canonical recovery source for the shadow stays the persisted snapshot (with
tg_social quarantined, `signal_data` may never reach a trade record at all).

`spec=TradingEngine` on the mock is deliberate — an unspecced mock accepts a
misspelled kwarg silently, which is exactly how a "forwarded" field ends up
forwarded nowhere.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from scout.db import Database
from scout.social.telegram.dispatcher import (
    dispatch_cashtag_to_engine,
    dispatch_to_engine,
)
from scout.social.telegram.models import ResolvedToken
from scout.trading.engine import TradingEngine

_EXPECTED_FIELDS = {
    "price_usd": 0.5,
    "volume_24h_usd": 77_000.25,
    "age_days": 12.5,
    "safety_pass": True,
    "safety_check_completed": True,
    "safety_skipped_no_ca": False,
}


def _resolved(**overrides) -> ResolvedToken:
    defaults = dict(
        token_id="tok",
        symbol="TOK",
        chain="solana",
        contract_address="0xabc",
        mcap=1_000_000.0,
        **_EXPECTED_FIELDS,
    )
    defaults.update(overrides)
    return ResolvedToken(**defaults)


def _engine() -> MagicMock:
    engine = MagicMock(spec=TradingEngine)
    engine.open_trade = AsyncMock(return_value=99)
    return engine


async def _add_channel(db: Database, **flags) -> None:
    cols = {"trade_eligible": 1, "cashtag_trade_eligible": 0}
    cols.update(flags)
    await db._conn.execute(
        "INSERT OR REPLACE INTO tg_social_channels "
        "(channel_handle, display_name, trade_eligible, cashtag_trade_eligible, "
        " safety_required, added_at) VALUES ('@gem', 'Gem', ?, ?, 1, ?)",
        (
            cols["trade_eligible"],
            cols["cashtag_trade_eligible"],
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    await db._conn.commit()


@pytest.mark.asyncio
async def test_ca_path_forwards_resolver_facts(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        await _add_channel(db)
        engine = _engine()
        trade_id, gate = await dispatch_to_engine(
            db=db,
            settings=settings_factory(),
            engine=engine,
            token=_resolved(),
            channel_handle="@gem",
        )
        assert (trade_id, gate) == (99, None)
        signal_data = engine.open_trade.await_args.kwargs["signal_data"]
        for key, value in _EXPECTED_FIELDS.items():
            assert signal_data[key] == value, key
        # Pre-existing fields are unchanged.
        assert signal_data["channel_handle"] == "@gem"
        assert signal_data["contract_address"] == "0xabc"
        assert signal_data["mcap_at_sighting"] == 1_000_000.0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cashtag_path_forwards_resolver_facts(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        await _add_channel(db, cashtag_trade_eligible=1)
        engine = _engine()
        candidate = _resolved(
            contract_address=None,
            safety_pass=False,
            safety_check_completed=False,
            safety_skipped_no_ca=True,
        )
        trade_id, gate = await dispatch_cashtag_to_engine(
            db=db,
            settings=settings_factory(),
            engine=engine,
            candidates=[candidate],
            cashtag="TOK",
            channel_handle="@gem",
        )
        assert (trade_id, gate) == (99, None)
        signal_data = engine.open_trade.await_args.kwargs["signal_data"]
        assert signal_data["price_usd"] == 0.5
        assert signal_data["volume_24h_usd"] == 77_000.25
        assert signal_data["age_days"] == 12.5
        assert signal_data["safety_pass"] is False
        assert signal_data["safety_check_completed"] is False
        assert signal_data["safety_skipped_no_ca"] is True
        # Cashtag provenance fields are unchanged.
        assert signal_data["resolution"] == "cashtag"
        assert signal_data["cashtag"] == "$TOK"
        assert signal_data["candidate_rank"] == 1
        assert signal_data["candidates_total"] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_missing_resolver_facts_forward_as_none(tmp_path, settings_factory):
    """An unavailable value forwards as None, never as 0 — a zero price and an
    unknown price are different facts."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        await _add_channel(db)
        engine = _engine()
        await dispatch_to_engine(
            db=db,
            settings=settings_factory(),
            engine=engine,
            token=_resolved(price_usd=None, volume_24h_usd=None, age_days=None),
            channel_handle="@gem",
        )
        signal_data = engine.open_trade.await_args.kwargs["signal_data"]
        assert signal_data["price_usd"] is None
        assert signal_data["volume_24h_usd"] is None
        assert signal_data["age_days"] is None
    finally:
        await db.close()
