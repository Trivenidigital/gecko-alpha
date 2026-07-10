from __future__ import annotations

from decimal import Decimal

import pytest

from scout.config import Settings
from scout.live.config import LiveConfig
from scout.live.gates import Gates
from scout.live.solana_swap_adapter import SolanaSwapAdapter

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
MINT = "So11111111111111111111111111111111111111112"


def _settings(**o):
    return Settings(
        _env_file=None,
        **_REQUIRED,
        LIVE_MODE="shadow",
        LIVE_SIGNAL_ALLOWLIST="first_signal",
        **o,
    )


class _Jupiter:
    async def get_quote(self, *, input_mint, output_mint, amount, slippage_bps):
        return {"outAmount": "1000000", "priceImpactPct": "0.005", "routePlan": [{}]}

    async def build_swap_tx(self, *, quote, user_pubkey, priority_fee_lamports):
        return "SIM_TX"


class _Rpc:
    def __init__(self):
        self.simulated = False

    async def get_sol_balance(self, *, owner):
        return 0.5

    async def get_token_balance(self, *, owner, mint):
        return 50.0

    async def simulate_transaction(self, tx_b64):
        self.simulated = True
        return True

    async def send_raw_transaction(self, signed_b64):  # invariant guard
        raise AssertionError("shadow mode must NOT broadcast")


class _Signer:
    def pubkey(self):
        return "OWNER"

    def sign(self, tx_b64):
        return tx_b64


class _KS:
    async def is_active(self):
        return None


@pytest.mark.asyncio
async def test_shadow_runs_gate_without_broadcast():
    s = _settings()
    rpc = _Rpc()
    adapter = SolanaSwapAdapter(
        settings=s, jupiter=_Jupiter(), rpc=rpc, signer=_Signer()
    )
    gates = Gates(
        config=LiveConfig(s), db=None, resolver=None, adapter=adapter, kill_switch=_KS()
    )

    res = await gates.evaluate_onchain(
        signal_type="first_signal", symbol="X", venue_pair=MINT, size_usd=Decimal("10")
    )

    assert res.passed is True  # 0.5% impact < 3% cap, sellable, gas ok
    assert rpc.simulated is True  # sellability simulation ran
    # no broadcast happened (would have raised). Reaching here proves it.
