from __future__ import annotations

import pytest

from scout.config import Settings
from scout.live.adapter_base import OrderRequest
from scout.live.solana_swap_adapter import SolanaSwapAdapter

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
MINT = "So11111111111111111111111111111111111111112"


def _settings(**o):
    return Settings(_env_file=None, **_REQUIRED, **o)


class _FakeJupiter:
    async def get_quote(self, *, input_mint, output_mint, amount, slippage_bps):
        return {"outAmount": "555000", "priceImpactPct": "0.001", "routePlan": [{}]}

    async def build_swap_tx(self, *, quote, user_pubkey, priority_fee_lamports):
        return "UNSIGNED_B64"


class _FakeRpc:
    def __init__(self):
        self.sent = []

    async def send_raw_transaction(self, signed_b64):
        self.sent.append(signed_b64)
        return "SIG_ABC"


class _FakeSigner:
    def pubkey(self):
        return "OWNER"

    def sign(self, tx_b64):
        return "SIGNED_" + tx_b64


def _req(side="buy"):
    return OrderRequest(
        paper_trade_id=1, canonical=MINT, venue_pair=MINT,
        side=side, size_usd=10.0, intent_uuid="abcd1234ef",
    )


@pytest.mark.asyncio
async def test_place_order_signs_sends_and_returns_signature():
    rpc = _FakeRpc()
    a = SolanaSwapAdapter(settings=_settings(), jupiter=_FakeJupiter(), rpc=rpc, signer=_FakeSigner())
    sig = await a.place_order_request(_req())
    assert sig == "SIG_ABC"
    assert rpc.sent == ["SIGNED_UNSIGNED_B64"]
    assert a._pending["SIG_ABC"]["out_amount"] == 555000
    assert a._pending["SIG_ABC"]["side"] == "buy"


@pytest.mark.asyncio
async def test_place_order_raises_without_signer():
    a = SolanaSwapAdapter(settings=_settings(), jupiter=_FakeJupiter(), rpc=_FakeRpc(), signer=None)
    with pytest.raises(RuntimeError, match="no signer"):
        await a.place_order_request(_req())
