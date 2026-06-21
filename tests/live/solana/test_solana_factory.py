from __future__ import annotations

import aiohttp
import pytest

from scout.config import Settings
from scout.live.solana_factory import build_solana_adapter
from scout.live.solana_swap_adapter import SolanaSwapAdapter
from solders.keypair import Keypair

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")


@pytest.mark.asyncio
async def test_factory_none_without_secret():
    s = Settings(_env_file=None, **_REQUIRED)
    async with aiohttp.ClientSession() as session:
        assert build_solana_adapter(settings=s, session=session, db=None) is None


@pytest.mark.asyncio
async def test_factory_builds_adapter_with_secret():
    s = Settings(_env_file=None, **_REQUIRED, SOLANA_WALLET_SECRET=str(Keypair()))
    async with aiohttp.ClientSession() as session:
        a = build_solana_adapter(settings=s, session=session, db=None)
        assert isinstance(a, SolanaSwapAdapter)
        assert a.venue_name == "solana"
        assert a.is_onchain is True
