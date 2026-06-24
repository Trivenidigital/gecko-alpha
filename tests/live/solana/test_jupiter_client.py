from __future__ import annotations

import re

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.live.solana.jupiter_client import JupiterClient, JupiterError

_QUOTE_RE = re.compile(r"https://api\.jup\.ag/swap/v1/quote.*")
_SWAP_RE = re.compile(r"https://api\.jup\.ag/swap/v1/swap.*")


@pytest.mark.asyncio
async def test_get_quote_returns_payload():
    async with aiohttp.ClientSession() as session:
        client = JupiterClient(session, base_url="https://api.jup.ag/swap/v1")
        with aioresponses() as m:
            m.get(
                _QUOTE_RE,
                payload={
                    "inAmount": "10000000",
                    "outAmount": "123456789",
                    "priceImpactPct": "0.0042",
                    "routePlan": [{"swapInfo": {}}],
                },
            )
            q = await client.get_quote(
                input_mint="USDC",
                output_mint="MINT",
                amount=10_000_000,
                slippage_bps=50,
            )
        assert q["outAmount"] == "123456789"
        assert q["priceImpactPct"] == "0.0042"


@pytest.mark.asyncio
async def test_get_quote_raises_on_http_error():
    async with aiohttp.ClientSession() as session:
        client = JupiterClient(session, base_url="https://api.jup.ag/swap/v1")
        with aioresponses() as m:
            m.get(_QUOTE_RE, status=400, payload={"error": "no route"})
            with pytest.raises(JupiterError):
                await client.get_quote(
                    input_mint="USDC", output_mint="MINT", amount=1, slippage_bps=50
                )


@pytest.mark.asyncio
async def test_build_swap_tx_returns_base64():
    async with aiohttp.ClientSession() as session:
        client = JupiterClient(session, base_url="https://api.jup.ag/swap/v1")
        with aioresponses() as m:
            m.post(_SWAP_RE, payload={"swapTransaction": "QUJDRA=="})
            tx = await client.build_swap_tx(
                quote={"outAmount": "1"},
                user_pubkey="PUBKEY",
                priority_fee_lamports=5000,
            )
        assert tx == "QUJDRA=="


@pytest.mark.asyncio
async def test_build_swap_tx_raises_when_missing():
    async with aiohttp.ClientSession() as session:
        client = JupiterClient(session, base_url="https://api.jup.ag/swap/v1")
        with aioresponses() as m:
            m.post(_SWAP_RE, payload={})
            with pytest.raises(JupiterError):
                await client.build_swap_tx(
                    quote={}, user_pubkey="PUBKEY", priority_fee_lamports=5000
                )
