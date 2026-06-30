"""Daily float sweep: keep live USDC at/below SOLANA_FLOAT_CAP_USD.

This module implements and tests the sweep DECISION. The actual cold-wallet
transfer is gated behind SOLANA_SWEEP_COLD_WALLET and logged as not-yet-
implemented until the transfer-instruction design lands (spec open items).

DEFERRED (spec §8 / §11): the **wallet-drain tripwire** — an unexpected balance
drop beyond tolerance that should engage the kill switch + fire a Telegram alert
— is NOT implemented. This sweep job only computes/decides the periodic
excess-over-cap transfer; it does NOT watch for anomalous drains between runs.
Hot-wallet risk is currently bounded only by the static SOLANA_FLOAT_CAP_USD
exposure gate (Gates.evaluate_onchain) plus this sweep decision. Do not assume a
drain detector is protecting the hot wallet.
"""

from __future__ import annotations

import asyncio

import structlog

log = structlog.get_logger(__name__)


async def compute_sweep_amount(*, balance_usd: float, float_cap_usd: float) -> float:
    return max(0.0, balance_usd - float_cap_usd)


async def main() -> None:  # pragma: no cover - operational entrypoint
    import aiohttp

    from scout.config import Settings
    from scout.live.solana_factory import build_solana_adapter

    settings = Settings()
    async with aiohttp.ClientSession() as session:
        adapter = build_solana_adapter(settings=settings, session=session, db=None)
        if adapter is None:
            log.info("solana_sweep_skipped_no_adapter")
            return
        balance = await adapter.fetch_account_balance("USDC")
        excess = await compute_sweep_amount(
            balance_usd=balance, float_cap_usd=float(settings.SOLANA_FLOAT_CAP_USD)
        )
        if excess <= 0:
            log.info("solana_sweep_noop", balance_usd=balance)
            return
        if not settings.SOLANA_SWEEP_COLD_WALLET:
            log.warning("solana_sweep_no_cold_wallet", excess_usd=excess)
            return
        log.warning(
            "solana_sweep_transfer_not_implemented",
            excess_usd=excess,
            cold_wallet=settings.SOLANA_SWEEP_COLD_WALLET,
        )


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
