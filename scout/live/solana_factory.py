"""Construct the SolanaSwapAdapter and its sub-modules from settings."""

from __future__ import annotations

from typing import Any

import aiohttp
import structlog

from scout.live.solana.jupiter_client import JupiterClient
from scout.live.solana.rpc import SolanaRpc
from scout.live.solana.wallet import make_signer
from scout.live.solana_swap_adapter import SolanaSwapAdapter

log = structlog.get_logger(__name__)


def build_solana_adapter(
    *, settings, session: aiohttp.ClientSession, db: Any | None
) -> SolanaSwapAdapter | None:
    signer = make_signer(settings)
    if signer is None:
        log.info("solana_adapter_skipped_no_secret")
        return None
    api_key = (
        settings.SOLANA_JUPITER_API_KEY.get_secret_value()
        if settings.SOLANA_JUPITER_API_KEY is not None
        else None
    )
    jupiter = JupiterClient(
        session, base_url=settings.SOLANA_JUPITER_URL, api_key=api_key
    )
    rpc = SolanaRpc(session, settings.SOLANA_RPC_URL)
    log.info("solana_adapter_built", pubkey=signer.pubkey())
    return SolanaSwapAdapter(
        settings=settings, jupiter=jupiter, rpc=rpc, signer=signer, db=db
    )
