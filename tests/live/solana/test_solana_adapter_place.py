from __future__ import annotations

import base64

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.transaction import VersionedTransaction

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
    def __init__(self, sig):
        self._sig = sig
        self.sent = []

    async def send_raw_transaction(self, signed_b64):
        self.sent.append(signed_b64)
        return self._sig


class _FakeSigner:
    """Produces a genuinely signed VersionedTransaction b64 so the adapter can
    derive the first signature (C2 two-phase place)."""

    def __init__(self):
        self._kp = Keypair()
        msg = MessageV0.try_compile(self._kp.pubkey(), [], [], Hash.default())
        self.signed_b64 = base64.b64encode(
            bytes(VersionedTransaction(msg, [self._kp]))
        ).decode()
        raw = base64.b64decode(self.signed_b64)
        self.expected_sig = str(VersionedTransaction.from_bytes(raw).signatures[0])

    def pubkey(self):
        return str(self._kp.pubkey())

    def sign(self, tx_b64):
        return self.signed_b64


def _req(side="buy"):
    return OrderRequest(
        paper_trade_id=1,
        canonical=MINT,
        venue_pair=MINT,
        side=side,
        size_usd=10.0,
        intent_uuid="abcd1234ef",
    )


@pytest.mark.asyncio
async def test_place_order_signs_sends_and_returns_signature():
    signer = _FakeSigner()
    rpc = _FakeRpc(signer.expected_sig)
    a = SolanaSwapAdapter(
        settings=_settings(), jupiter=_FakeJupiter(), rpc=rpc, signer=signer
    )
    sig = await a.place_order_request(_req())
    assert sig == signer.expected_sig
    assert rpc.sent == [signer.signed_b64]
    assert a._pending[sig]["out_amount"] == 555000
    assert a._pending[sig]["side"] == "buy"


@pytest.mark.asyncio
async def test_place_order_raises_without_signer():
    a = SolanaSwapAdapter(
        settings=_settings(),
        jupiter=_FakeJupiter(),
        rpc=_FakeRpc("X"),
        signer=None,
    )
    with pytest.raises(RuntimeError, match="no signer"):
        await a.place_order_request(_req())
