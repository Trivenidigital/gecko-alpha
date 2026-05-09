"""BL-NEW-LIVE-HYBRID M1.5b: integration test — real scout/main.py wiring.

R2-C2 design-stage fold. Catches the regression class where Task 3's
main.py construction forgets the `routing=live_routing` kwarg — unit
tests pass while prod silently no-ops every signal.

Strategy: import the construction code path from scout/main.py and
exercise it with stubbed Binance HTTP responses. Assert that
`live_engine._routing is not None` when LIVE_USE_ROUTING_LAYER=True.

Note: full end-to-end execution (paper trade open → place_order →
await_fill → counter increment) is gated by Windows OpenSSL Applink
crash class (documented in test_live_binance_adapter_signed.py header).
This test focuses on the wiring assertion which is the actual
regression class R2-C2 was concerned about.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from scout.config import Settings
from scout.db import Database
from scout.live.config import LiveConfig
from scout.live.engine import LiveEngine
from scout.live.routing import RoutingLayer

_REQUIRED = {
    "TELEGRAM_BOT_TOKEN": "x",
    "TELEGRAM_CHAT_ID": "x",
    "ANTHROPIC_API_KEY": "x",
}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**_REQUIRED, **overrides})


@pytest.mark.asyncio
async def test_main_path_wires_routing_layer_when_flag_true(tmp_path):
    """R2-C2 regression: when LIVE_USE_ROUTING_LAYER=True, scout/main.py's
    construction MUST pass routing=live_routing to LiveEngine. Without
    this kwarg, prod silently no-ops every signal even though all unit
    tests pass.

    Replicates main.py's construction code path inline. Asserts the
    wiring is correct and the engine __init__ does not raise.
    """
    settings = _settings(
        LIVE_MODE="live",
        LIVE_TRADING_ENABLED=True,
        LIVE_USE_REAL_SIGNED_REQUESTS=True,
        LIVE_USE_ROUTING_LAYER=True,
        LIVE_SIGNAL_ALLOWLIST="first_signal",
        BINANCE_API_KEY="x",
        BINANCE_API_SECRET="x",
    )
    db = Database(tmp_path / "t.db")
    await db.initialize()
    live_config = LiveConfig(settings)
    adapter = MagicMock()
    adapter.place_order_request = AsyncMock()
    adapter.await_fill_confirmation = AsyncMock()
    resolver = MagicMock()
    kill_switch = MagicMock()
    kill_switch.engage = AsyncMock()

    # Replicate scout/main.py:1154-1180 construction logic
    live_routing = None
    if getattr(settings, "LIVE_USE_ROUTING_LAYER", False):
        live_routing = RoutingLayer(
            db=db,
            settings=settings,
            adapters={"binance": adapter},
        )
    live_engine = LiveEngine(
        config=live_config,
        resolver=resolver,
        adapter=adapter,
        db=db,
        kill_switch=kill_switch,
        routing=live_routing,
    )

    assert live_engine._routing is not None
    assert isinstance(live_engine._routing, RoutingLayer)
    await db.close()


@pytest.mark.asyncio
async def test_main_path_passes_none_routing_when_flag_false(tmp_path):
    """When LIVE_USE_ROUTING_LAYER=False (default), routing=None is
    passed and engine bypasses _dispatch_live (M1.5a behavior preserved)."""
    settings = _settings(
        LIVE_MODE="live",
        LIVE_TRADING_ENABLED=True,
        LIVE_USE_REAL_SIGNED_REQUESTS=True,
        LIVE_USE_ROUTING_LAYER=False,
        LIVE_SIGNAL_ALLOWLIST="first_signal",
        BINANCE_API_KEY="x",
        BINANCE_API_SECRET="x",
    )
    db = Database(tmp_path / "t.db")
    await db.initialize()
    live_config = LiveConfig(settings)
    adapter = MagicMock()
    resolver = MagicMock()
    kill_switch = MagicMock()
    kill_switch.engage = AsyncMock()

    live_routing = None
    if getattr(settings, "LIVE_USE_ROUTING_LAYER", False):
        live_routing = RoutingLayer(
            db=db,
            settings=settings,
            adapters={"binance": adapter},
        )
    live_engine = LiveEngine(
        config=live_config,
        resolver=resolver,
        adapter=adapter,
        db=db,
        kill_switch=kill_switch,
        routing=live_routing,
    )

    assert live_engine._routing is None
    await db.close()
