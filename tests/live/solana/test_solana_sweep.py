from __future__ import annotations

import pytest

from scripts.solana_sweep import compute_sweep_amount


@pytest.mark.asyncio
async def test_sweep_amount_above_cap():
    assert await compute_sweep_amount(balance_usd=175.0, float_cap_usd=100.0) == 75.0


@pytest.mark.asyncio
async def test_sweep_amount_at_or_below_cap_is_zero():
    assert await compute_sweep_amount(balance_usd=80.0, float_cap_usd=100.0) == 0.0
    assert await compute_sweep_amount(balance_usd=100.0, float_cap_usd=100.0) == 0.0
