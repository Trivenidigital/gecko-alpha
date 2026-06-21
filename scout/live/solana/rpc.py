"""Thin async Solana JSON-RPC client over aiohttp. No keys."""

from __future__ import annotations

from typing import Any

import aiohttp
import structlog

from scout.live.solana.constants import LAMPORTS_PER_SOL

log = structlog.get_logger(__name__)


class RpcError(RuntimeError):
    """JSON-RPC returned an error object."""


class SolanaRpc:
    def __init__(self, session: aiohttp.ClientSession, url: str) -> None:
        self._session = session
        self._url = url

    async def _call(self, method: str, params: list[Any]) -> Any:
        req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        async with self._session.post(self._url, json=req) as resp:
            body = await resp.json()
        if "error" in body:
            raise RpcError(f"{method}: {body['error']}")
        return body.get("result")

    async def get_token_balance(self, *, owner: str, mint: str) -> float:
        result = await self._call(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
        )
        accounts = (result or {}).get("value", [])
        if not accounts:
            return 0.0
        info = accounts[0]["account"]["data"]["parsed"]["info"]
        return float(info["tokenAmount"]["uiAmount"] or 0.0)

    async def get_sol_balance(self, *, owner: str) -> float:
        result = await self._call("getBalance", [owner])
        lamports = (result or {}).get("value", 0)
        return lamports / LAMPORTS_PER_SOL

    async def send_raw_transaction(self, signed_b64: str) -> str:
        return await self._call(
            "sendTransaction",
            [
                signed_b64,
                {"encoding": "base64", "skipPreflight": False, "maxRetries": 2},
            ],
        )

    async def confirm_signature(self, signature: str) -> str:
        result = await self._call(
            "getSignatureStatuses", [[signature], {"searchTransactionHistory": True}]
        )
        value = (result or {}).get("value", [None])
        status = value[0] if value else None
        if status is None:
            return "pending"
        if status.get("err") is not None:
            return "failed"
        if status.get("confirmationStatus") in ("confirmed", "finalized"):
            return "success"
        return "pending"

    async def simulate_transaction(self, tx_b64: str) -> bool:
        result = await self._call(
            "simulateTransaction", [tx_b64, {"encoding": "base64", "sigVerify": False}]
        )
        return (result or {}).get("value", {}).get("err") is None
