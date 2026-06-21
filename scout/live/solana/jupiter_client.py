"""Jupiter v6 aggregator HTTP client. Pure HTTP — holds no keys, signs nothing.

Quote → swap-transaction flow:
  get_quote()       -> GET  /quote  (routing + priceImpactPct)
  build_swap_tx()   -> POST /swap   (returns a base64 VersionedTransaction)
"""

from __future__ import annotations

from typing import Any

import aiohttp
import structlog

log = structlog.get_logger(__name__)


class JupiterError(RuntimeError):
    """Quote/swap failed (no route, HTTP error, malformed response)."""


class JupiterClient:
    def __init__(
        self, session: aiohttp.ClientSession, base_url: str, api_key: str | None = None
    ) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        # api.jup.ag requires a free key via x-api-key; lite-api.jup.ag is keyless.
        self._headers = {"x-api-key": api_key} if api_key else {}

    async def get_quote(
        self, *, input_mint: str, output_mint: str, amount: int, slippage_bps: int
    ) -> dict[str, Any]:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
        }
        async with self._session.get(
            f"{self._base}/quote", params=params, headers=self._headers
        ) as resp:
            body = await resp.json()
            if resp.status != 200:
                raise JupiterError(f"quote http {resp.status}: {body}")
            if not body.get("outAmount") or not body.get("routePlan"):
                raise JupiterError(f"quote no route: {body}")
            return body

    async def build_swap_tx(
        self, *, quote: dict[str, Any], user_pubkey: str, priority_fee_lamports: int
    ) -> str:
        payload = {
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "prioritizationFeeLamports": priority_fee_lamports,
        }
        async with self._session.post(
            f"{self._base}/swap", json=payload, headers=self._headers
        ) as resp:
            body = await resp.json()
            if resp.status != 200:
                raise JupiterError(f"swap http {resp.status}: {body}")
            tx = body.get("swapTransaction")
            if not tx:
                raise JupiterError(f"swap missing swapTransaction: {body}")
            return tx
