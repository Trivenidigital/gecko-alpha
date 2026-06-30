"""C2: two-phase place — prepare_order (no broadcast) + broadcast_prepared.

The tx signature equals the first signature of the SIGNED transaction and is
deterministic pre-broadcast, so the engine can persist it to live_trades
BEFORE any network send (crash-recovery invariant).
"""

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


def _signed_tx_b64(kp: Keypair) -> str:
    msg = MessageV0.try_compile(kp.pubkey(), [], [], Hash.default())
    signed = VersionedTransaction(msg, [kp])
    return base64.b64encode(bytes(signed)).decode()


class _FakeJupiter:
    async def get_quote(self, *, input_mint, output_mint, amount, slippage_bps):
        return {"outAmount": "555000", "priceImpactPct": "0.001", "routePlan": [{}]}

    async def build_swap_tx(self, *, quote, user_pubkey, priority_fee_lamports):
        return "UNSIGNED_B64"


class _RealSigner:
    """Returns a genuinely signed VersionedTransaction b64 so the adapter can
    decode it and derive the first signature."""

    def __init__(self):
        self._kp = Keypair()
        self.signed_b64 = _signed_tx_b64(self._kp)
        # The signature the adapter must derive (base58 of first signature).
        raw = base64.b64decode(self.signed_b64)
        self.expected_sig = str(VersionedTransaction.from_bytes(raw).signatures[0])

    def pubkey(self):
        return str(self._kp.pubkey())

    def sign(self, tx_b64):
        return self.signed_b64


class _ExplodingRpc:
    """send_raw_transaction must NOT be called during prepare_order."""

    async def send_raw_transaction(self, signed_b64):
        raise AssertionError("prepare_order must NOT broadcast")


class _RecordingRpc:
    def __init__(self, sig):
        self._sig = sig
        self.sent = []

    async def send_raw_transaction(self, signed_b64):
        self.sent.append(signed_b64)
        return self._sig


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
async def test_prepare_order_returns_signature_and_does_not_broadcast():
    signer = _RealSigner()
    a = SolanaSwapAdapter(
        settings=_settings(),
        jupiter=_FakeJupiter(),
        rpc=_ExplodingRpc(),
        signer=signer,
    )
    signature, signed_b64 = await a.prepare_order(_req())
    assert signature == signer.expected_sig
    assert signature  # non-empty
    assert signed_b64 == signer.signed_b64
    # _pending stashed under the derived signature, ready for await_fill.
    assert a._pending[signature]["out_amount"] == 555000
    assert a._pending[signature]["side"] == "buy"


@pytest.mark.asyncio
async def test_broadcast_prepared_sends_and_returns_signature():
    signer = _RealSigner()
    rpc = _RecordingRpc(signer.expected_sig)
    a = SolanaSwapAdapter(
        settings=_settings(), jupiter=_FakeJupiter(), rpc=rpc, signer=signer
    )
    signature, signed_b64 = await a.prepare_order(_req())
    reported = await a.broadcast_prepared(signed_b64)
    assert rpc.sent == [signed_b64]
    assert reported == signer.expected_sig


@pytest.mark.asyncio
async def test_place_order_request_still_works_via_two_phase():
    """Back-compat: place_order_request signs + sends, returns signature."""
    signer = _RealSigner()
    rpc = _RecordingRpc(signer.expected_sig)
    a = SolanaSwapAdapter(
        settings=_settings(), jupiter=_FakeJupiter(), rpc=rpc, signer=signer
    )
    sig = await a.place_order_request(_req())
    assert sig == signer.expected_sig
    assert rpc.sent == [signer.signed_b64]
    assert a._pending[sig]["out_amount"] == 555000
