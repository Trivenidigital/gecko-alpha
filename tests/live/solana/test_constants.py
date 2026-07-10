from __future__ import annotations

from scout.live.solana import constants as c


def test_mints_and_decimals():
    assert c.USDC_MINT == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    assert c.WSOL_MINT == "So11111111111111111111111111111111111111112"
    assert c.USDC_DECIMALS == 6
    assert c.SOL_DECIMALS == 9
    assert c.LAMPORTS_PER_SOL == 1_000_000_000


def test_usdc_unit_conversion_roundtrip():
    assert c.usdc_to_base_units(10.0) == 10_000_000
    assert c.base_units_to_usdc(10_000_000) == 10.0
    # sub-cent rounds to integer base units
    assert c.usdc_to_base_units(0.000001) == 1
