"""BL-NEW-LIVE-HYBRID M1 v2.1: balance_gate tests."""

from __future__ import annotations

import pytest

from scout.live.balance_gate import (
    BalanceGateResult,
    check_sufficient_balance,
)


class _StubAdapter:
    """Minimal stub — no aiohttp imports, dodges Windows OpenSSL crash."""

    def __init__(self, *, balance: float = 0.0, raise_kind: str | None = None) -> None:
        self.venue_name = "stub"
        self._balance = balance
        self._raise_kind = raise_kind

    async def fetch_account_balance(self, asset: str = "USDT") -> float:
        if self._raise_kind == "not_implemented":
            raise NotImplementedError("stub explicitly defers")
        if self._raise_kind == "transient":
            raise RuntimeError("stub transient")
        return self._balance


@pytest.mark.asyncio
async def test_balance_gate_passes_when_sufficient():
    adapter = _StubAdapter(balance=200.0)
    result = await check_sufficient_balance(adapter, 100.0, margin_factor=1.1)
    assert isinstance(result, BalanceGateResult)
    assert result.passed is True
    assert result.available_usd == 200.0
    assert result.required_with_margin_usd == pytest.approx(110.0)


@pytest.mark.asyncio
async def test_balance_gate_blocks_when_insufficient():
    adapter = _StubAdapter(balance=50.0)
    result = await check_sufficient_balance(adapter, 100.0, margin_factor=1.1)
    assert result.passed is False
    assert result.available_usd == 50.0
    assert result.required_with_margin_usd == pytest.approx(110.0)


@pytest.mark.asyncio
async def test_balance_gate_blocks_at_exact_boundary():
    """Required-with-margin = 110.0; balance exactly 109.99 must fail."""
    adapter = _StubAdapter(balance=109.99)
    result = await check_sufficient_balance(adapter, 100.0, margin_factor=1.1)
    assert result.passed is False


@pytest.mark.asyncio
async def test_balance_gate_passes_with_minimal_excess():
    """Balance just above required-with-margin passes. Avoids the
    100.0 * 1.1 = 110.00000000000001 float-precision gotcha — code
    uses raw float comparison per plan; if precision becomes a real
    issue, M2 should switch to Decimal per code-quality M3 from Task 5."""
    adapter = _StubAdapter(balance=110.01)
    result = await check_sufficient_balance(adapter, 100.0, margin_factor=1.1)
    assert result.passed is True


@pytest.mark.asyncio
async def test_balance_gate_handles_not_implemented_gracefully():
    """BL-055 shadow / M1.5 scaffold: adapter raises NotImplementedError;
    gate returns passed=False, NOT raises."""
    adapter = _StubAdapter(raise_kind="not_implemented")
    result = await check_sufficient_balance(adapter, 100.0)
    assert result.passed is False
    assert result.available_usd == 0.0
    assert "not implemented" in result.detail.lower()


@pytest.mark.asyncio
async def test_balance_gate_handles_transient_failures_gracefully():
    """Network/REST failure: gate logs + returns passed=False, NOT raises."""
    adapter = _StubAdapter(raise_kind="transient")
    result = await check_sufficient_balance(adapter, 100.0)
    assert result.passed is False
    assert "failed" in result.detail.lower()


@pytest.mark.asyncio
async def test_balance_gate_respects_no_margin():
    """margin_factor=1.0 means required_with_margin == required."""
    adapter = _StubAdapter(balance=100.0)
    result = await check_sufficient_balance(adapter, 100.0, margin_factor=1.0)
    assert result.passed is True
    assert result.required_with_margin_usd == pytest.approx(100.0)
