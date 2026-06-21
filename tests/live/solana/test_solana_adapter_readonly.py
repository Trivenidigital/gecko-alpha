from __future__ import annotations

from decimal import Decimal

import pytest

from scout.config import Settings
from scout.live.adapter_base import VenueMetadata
from scout.live.solana_swap_adapter import SolanaSwapAdapter

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
MINT = "So11111111111111111111111111111111111111112"


def _settings(**o):
    return Settings(_env_file=None, **_REQUIRED, **o)


class _FakeJupiter:
    def __init__(self, quote):
        self._quote = quote
        self.calls = []

    async def get_quote(self, *, input_mint, output_mint, amount, slippage_bps):
        self.calls.append((input_mint, output_mint, amount))
        return self._quote


def _adapter(quote):
    return SolanaSwapAdapter(
        settings=_settings(), jupiter=_FakeJupiter(quote), rpc=None, signer=None
    )


@pytest.mark.asyncio
async def test_resolve_pair_returns_mint_when_routable():
    a = _adapter({"outAmount": "1000", "priceImpactPct": "0.001", "routePlan": [{}]})
    assert await a.resolve_pair_for_symbol(MINT) == MINT


@pytest.mark.asyncio
async def test_fetch_venue_metadata_shape():
    a = _adapter({"outAmount": "1000", "priceImpactPct": "0.001", "routePlan": [{}]})
    meta = await a.fetch_venue_metadata(MINT)
    assert isinstance(meta, VenueMetadata)
    assert meta.venue == "solana"
    assert meta.venue_pair == MINT
    assert meta.quote == "USDC"
    assert meta.asset_class == "spot"


@pytest.mark.asyncio
async def test_quote_at_size_buy_uses_usdc_input_and_converts_impact():
    # priceImpactPct "0.0042" (fraction) -> 0.42 percent
    a = _adapter(
        {"outAmount": "123456789", "priceImpactPct": "0.0042", "routePlan": [{}]}
    )
    out = await a.quote_at_size(venue_pair=MINT, side="buy", size_usd=10.0)
    assert out["out_amount"] == 123456789
    assert round(out["price_impact_pct"], 4) == 0.42
    # input mint was USDC, amount = 10 * 1e6 base units
    assert a._jupiter.calls[0][0].endswith("Dt1v")  # USDC mint
    assert a._jupiter.calls[0][2] == 10_000_000


@pytest.mark.asyncio
async def test_fetch_depth_synthesizes_from_quote():
    a = _adapter({"outAmount": "1000000", "priceImpactPct": "0.01", "routePlan": [{}]})
    depth = await a.fetch_depth(MINT)
    assert depth.pair == MINT
    assert depth.mid > Decimal("0")
    assert len(depth.asks) >= 1
