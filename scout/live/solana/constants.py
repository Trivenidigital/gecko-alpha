"""Solana mainnet constants and unit conversions for the swap adapter."""

from __future__ import annotations

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL_MINT = "So11111111111111111111111111111111111111112"

USDC_DECIMALS = 6
SOL_DECIMALS = 9
LAMPORTS_PER_SOL = 1_000_000_000


def usdc_to_base_units(amount_usd: float) -> int:
    """USD (== USDC 1:1) to integer base units (6 decimals), rounded."""
    return int(round(amount_usd * (10**USDC_DECIMALS)))


def base_units_to_usdc(units: int) -> float:
    """Integer USDC base units back to a float USD amount."""
    return units / (10**USDC_DECIMALS)
