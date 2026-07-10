from __future__ import annotations

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.live.solana.rpc import RpcError, SolanaRpc

URL = "https://rpc.test/solana"


@pytest.mark.asyncio
async def test_get_token_balance_parses_ui_amount():
    async with aiohttp.ClientSession() as session:
        rpc = SolanaRpc(session, URL)
        with aioresponses() as m:
            m.post(
                URL,
                payload={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "value": [
                            {
                                "account": {
                                    "data": {
                                        "parsed": {
                                            "info": {"tokenAmount": {"uiAmount": 42.5}}
                                        }
                                    }
                                }
                            }
                        ]
                    },
                },
            )
            bal = await rpc.get_token_balance(owner="OWNER", mint="USDC")
        assert bal == 42.5


@pytest.mark.asyncio
async def test_get_token_balance_zero_when_no_account():
    async with aiohttp.ClientSession() as session:
        rpc = SolanaRpc(session, URL)
        with aioresponses() as m:
            m.post(URL, payload={"jsonrpc": "2.0", "id": 1, "result": {"value": []}})
            bal = await rpc.get_token_balance(owner="OWNER", mint="USDC")
        assert bal == 0.0


@pytest.mark.asyncio
async def test_send_raw_transaction_returns_signature():
    async with aiohttp.ClientSession() as session:
        rpc = SolanaRpc(session, URL)
        with aioresponses() as m:
            m.post(URL, payload={"jsonrpc": "2.0", "id": 1, "result": "SIGNATURE123"})
            sig = await rpc.send_raw_transaction("QUJD")
        assert sig == "SIGNATURE123"


@pytest.mark.asyncio
async def test_send_raw_transaction_raises_on_error():
    async with aiohttp.ClientSession() as session:
        rpc = SolanaRpc(session, URL)
        with aioresponses() as m:
            m.post(
                URL,
                payload={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32002, "message": "blockhash not found"},
                },
            )
            with pytest.raises(RpcError):
                await rpc.send_raw_transaction("QUJD")


@pytest.mark.asyncio
async def test_confirm_signature_states():
    async with aiohttp.ClientSession() as session:
        rpc = SolanaRpc(session, URL)
        # success
        with aioresponses() as m:
            m.post(
                URL,
                payload={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "value": [{"confirmationStatus": "confirmed", "err": None}]
                    },
                },
            )
            assert await rpc.confirm_signature("SIG") == "success"
        # on-chain failure
        with aioresponses() as m:
            m.post(
                URL,
                payload={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "value": [{"confirmationStatus": "confirmed", "err": {"x": 1}}]
                    },
                },
            )
            assert await rpc.confirm_signature("SIG") == "failed"
        # not yet landed
        with aioresponses() as m:
            m.post(
                URL, payload={"jsonrpc": "2.0", "id": 1, "result": {"value": [None]}}
            )
            assert await rpc.confirm_signature("SIG") == "pending"


@pytest.mark.asyncio
async def test_simulate_transaction_success_and_failure():
    async with aiohttp.ClientSession() as session:
        rpc = SolanaRpc(session, URL)
        with aioresponses() as m:
            m.post(
                URL,
                payload={"jsonrpc": "2.0", "id": 1, "result": {"value": {"err": None}}},
            )
            assert await rpc.simulate_transaction("QUJD") is True
        with aioresponses() as m:
            m.post(
                URL,
                payload={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"value": {"err": {"e": 1}}},
                },
            )
            assert await rpc.simulate_transaction("QUJD") is False
