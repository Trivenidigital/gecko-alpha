"""BL-NEW-LIVE-HYBRID M1 v2.1: balance_gate (was BL-055 prereq).

`check_sufficient_balance(adapter, required_usd, margin_factor)` queries
the adapter's `fetch_account_balance` (Task 5 ABC method) and returns
a `BalanceGateResult` with `passed: bool` + `available_usd: float` +
`required_with_margin_usd: float` + `detail: str`.

The gate is invoked from `scout/live/gates.py` AFTER depth check
(Gate 5) and BEFORE order submission (Gate 10 / engine entry). On
NotImplementedError from the adapter (BL-055 shadow / M1.5 CCXT
scaffold), the gate returns `passed=False` with a clear detail so
the engine reports the right `reject_reason='insufficient_balance'`
without crashing the loop.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from scout.live.adapter_base import ExchangeAdapter

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class BalanceGateResult:
    passed: bool
    available_usd: float
    required_with_margin_usd: float
    detail: str


async def check_sufficient_balance(
    adapter: ExchangeAdapter,
    required_usd: float,
    *,
    margin_factor: float = 1.1,
    asset: str = "USDT",
) -> BalanceGateResult:
    """Return a BalanceGateResult.

    `margin_factor` defaults to 1.1 — adapter must hold at least 110%
    of the required notional to clear the gate (covers slippage +
    fee allowance per spec). Operator can pass 1.0 for no margin
    when running on a venue with predictable fees.

    On adapter NotImplementedError (BL-055 shadow / M1.5 scaffold):
    returns passed=False with explicit detail; does NOT raise. This
    keeps the live loop running in shadow mode without surprise
    crashes — the gate just blocks every fire as if balance were 0.
    """
    required_with_margin = required_usd * margin_factor
    try:
        available = await adapter.fetch_account_balance(asset=asset)
    except NotImplementedError as exc:
        log.info(
            "balance_gate_adapter_not_wired",
            adapter=type(adapter).__name__,
            asset=asset,
            err=str(exc),
        )
        return BalanceGateResult(
            passed=False,
            available_usd=0.0,
            required_with_margin_usd=required_with_margin,
            detail=f"adapter.fetch_account_balance not implemented: {exc}",
        )
    except Exception as exc:
        # PR #86 V3-I3 fold: surface VenueTransientError + IPBan as a
        # distinct detail string — caller (Gate 10) maps to
        # reject_reason='venue_unavailable' instead of confusing operator
        # with 'insufficient_balance' during a Binance maintenance window.
        from scout.live.exceptions import VenueTransientError

        try:
            from scout.live.binance_adapter import (
                BinanceIPBanError,
                BinanceInsufficientFundsError,
            )
        except ImportError:  # pragma: no cover
            BinanceIPBanError = type("BinanceIPBanError", (Exception,), {})
            BinanceInsufficientFundsError = type(
                "BinanceInsufficientFundsError", (Exception,), {}
            )

        if isinstance(exc, BinanceInsufficientFundsError):
            # PR #86 V1-C1: -2018/-2019 from Binance. Real funds shortage —
            # NOT venue-down — so treat as insufficient_balance.
            log.info("balance_gate_insufficient_funds_from_venue", err=str(exc))
            return BalanceGateResult(
                passed=False,
                available_usd=0.0,
                required_with_margin_usd=required_with_margin,
                detail=f"venue reported insufficient funds: {exc}",
            )
        if isinstance(exc, (VenueTransientError, BinanceIPBanError)):
            log.warning(
                "balance_gate_venue_unavailable",
                exc_type=type(exc).__name__,
                err=str(exc),
            )
            return BalanceGateResult(
                passed=False,
                available_usd=0.0,
                required_with_margin_usd=required_with_margin,
                detail=f"venue_unavailable: {type(exc).__name__}: {exc}",
            )
        log.exception("balance_gate_fetch_failed", asset=asset)
        return BalanceGateResult(
            passed=False,
            available_usd=0.0,
            required_with_margin_usd=required_with_margin,
            detail=f"fetch_account_balance failed: {exc}",
        )

    passed = available >= required_with_margin
    return BalanceGateResult(
        passed=passed,
        available_usd=available,
        required_with_margin_usd=required_with_margin,
        detail=(
            f"available={available:.2f} required={required_with_margin:.2f} "
            f"(base={required_usd:.2f} × margin={margin_factor})"
        ),
    )
