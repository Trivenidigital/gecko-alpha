from __future__ import annotations

import pytest

from scout.config import Settings
from scout.live.solana_swap_adapter import SolanaSwapAdapter

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
MINT = "So11111111111111111111111111111111111111112"


def _settings(**o):
    return Settings(_env_file=None, **_REQUIRED, **o)


class _FakeRpc:
    def __init__(self, *, usdc=0.0, sol=0.0, sim=True):
        self._usdc, self._sol, self._sim = usdc, sol, sim

    async def get_token_balance(self, *, owner, mint):
        return self._usdc

    async def get_sol_balance(self, *, owner):
        return self._sol

    async def simulate_transaction(self, tx_b64):
        return self._sim


class _FakeJupiter:
    def __init__(self, *, route=True, swap_ok=True):
        self._route, self._swap_ok = route, swap_ok

    async def get_quote(self, *, input_mint, output_mint, amount, slippage_bps):
        if not self._route:
            raise RuntimeError("no route")
        return {"outAmount": "1", "priceImpactPct": "0.001", "routePlan": [{}]}

    async def build_swap_tx(self, *, quote, user_pubkey, priority_fee_lamports):
        if not self._swap_ok:
            raise RuntimeError("no swap")
        return "QUJD"


class _FakeSigner:
    def pubkey(self):
        return "OWNER_PUBKEY"

    def sign(self, tx_b64):
        return tx_b64


def _adapter(jup, rpc, signer=_FakeSigner()):
    return SolanaSwapAdapter(settings=_settings(), jupiter=jup, rpc=rpc, signer=signer)


@pytest.mark.asyncio
async def test_fetch_balance_usdc_and_sol():
    a = _adapter(_FakeJupiter(), _FakeRpc(usdc=25.0, sol=0.5))
    assert await a.fetch_account_balance("USDC") == 25.0
    assert await a.fetch_account_balance("USDT") == 25.0
    assert await a.fetch_account_balance("SOL") == 0.5


@pytest.mark.asyncio
async def test_is_sellable_true_when_route_and_sim_ok():
    a = _adapter(_FakeJupiter(route=True), _FakeRpc(sim=True))
    assert await a.is_sellable(venue_pair=MINT, expected_out_amount=1000) is True


@pytest.mark.asyncio
async def test_is_sellable_false_when_no_sell_route():
    a = _adapter(_FakeJupiter(route=False), _FakeRpc(sim=True))
    assert await a.is_sellable(venue_pair=MINT, expected_out_amount=1000) is False


@pytest.mark.asyncio
async def test_is_sellable_false_when_sim_fails():
    a = _adapter(_FakeJupiter(route=True), _FakeRpc(sim=False))
    assert await a.is_sellable(venue_pair=MINT, expected_out_amount=1000) is False
