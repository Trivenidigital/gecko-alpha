from __future__ import annotations

from decimal import Decimal

import pytest

from scout.config import Settings
from scout.live.config import LiveConfig
from scout.live.gates import VALID_REJECT_REASONS, Gates

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
MINT = "So11111111111111111111111111111111111111112"


def _settings(**o):
    return Settings(_env_file=None, **_REQUIRED, **o)


class _Adapter:
    is_onchain = True

    def __init__(self, *, impact, sellable, sol):
        self._impact, self._sellable, self._sol = impact, sellable, sol
        self.venue_name = "solana"

    async def quote_at_size(self, *, venue_pair, side, size_usd):
        return {"out_amount": 1000, "price_impact_pct": self._impact, "mid": Decimal("1")}

    async def is_sellable(self, *, venue_pair, expected_out_amount):
        return self._sellable

    async def fetch_account_balance(self, asset="USDT"):
        return self._sol if asset == "SOL" else 1000.0


class _KS:
    def is_active(self):
        return None


def _gates(adapter, **so):
    s = _settings(**so)
    return Gates(config=LiveConfig(s), db=None, resolver=None, adapter=adapter, kill_switch=_KS())


def test_not_sellable_is_a_valid_reject_reason():
    assert "not_sellable" in VALID_REJECT_REASONS


@pytest.mark.asyncio
async def test_onchain_pass():
    g = _gates(_Adapter(impact=0.5, sellable=True, sol=0.5))
    res = await g.evaluate_onchain(signal_type="x", symbol="X", venue_pair=MINT, size_usd=Decimal("10"))
    assert res.passed is True


@pytest.mark.asyncio
async def test_onchain_price_impact_reject():
    g = _gates(_Adapter(impact=9.0, sellable=True, sol=0.5))  # > 3.0 default
    res = await g.evaluate_onchain(signal_type="x", symbol="X", venue_pair=MINT, size_usd=Decimal("10"))
    assert res.passed is False
    assert res.reject_reason == "insufficient_depth"


@pytest.mark.asyncio
async def test_onchain_not_sellable_reject():
    g = _gates(_Adapter(impact=0.5, sellable=False, sol=0.5))
    res = await g.evaluate_onchain(signal_type="x", symbol="X", venue_pair=MINT, size_usd=Decimal("10"))
    assert res.passed is False
    assert res.reject_reason == "not_sellable"


@pytest.mark.asyncio
async def test_onchain_gas_reserve_reject():
    g = _gates(_Adapter(impact=0.5, sellable=True, sol=0.0))  # < 0.02 default
    res = await g.evaluate_onchain(signal_type="x", symbol="X", venue_pair=MINT, size_usd=Decimal("10"))
    assert res.passed is False
    assert res.reject_reason == "insufficient_balance"
