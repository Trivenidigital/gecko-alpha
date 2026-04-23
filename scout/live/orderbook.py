"""Pure VWAP walker. No I/O (spec §8)."""
from __future__ import annotations

from decimal import Decimal

from scout.live.types import Depth, WalkResult


def walk_asks(depth: Depth, size_usd: Decimal) -> WalkResult:
    """Walk ask side accumulating notional until >= size_usd.

    Returns a WalkResult with VWAP and slippage in bps relative to depth.mid.
    If asks do not contain enough notional to fill ``size_usd``, returns
    ``insufficient_liquidity=True`` and ``vwap=None``.
    """
    remaining = size_usd
    filled_notional = Decimal(0)
    filled_qty = Decimal(0)
    for level in depth.asks:
        level_notional = level.price * level.qty
        take_notional = min(level_notional, remaining)
        take_qty = take_notional / level.price
        filled_notional += take_notional
        filled_qty += take_qty
        remaining -= take_notional
        if remaining <= 0:
            break
    if remaining > 0:
        return WalkResult(
            vwap=None,
            filled_qty=filled_qty,
            filled_notional=filled_notional,
            slippage_bps=None,
            insufficient_liquidity=True,
        )
    vwap = filled_notional / filled_qty
    bps = int((vwap - depth.mid) / depth.mid * Decimal(10000))
    return WalkResult(
        vwap=vwap,
        filled_qty=filled_qty,
        filled_notional=filled_notional,
        slippage_bps=bps,
        insufficient_liquidity=False,
    )


def walk_bids(depth: Depth, qty: Decimal) -> WalkResult:
    """Walk bid side accumulating quantity until >= qty.

    Returns a WalkResult with VWAP and slippage in bps relative to depth.mid
    (positive bps = sold below mid). If bids do not contain enough quantity,
    returns ``insufficient_liquidity=True`` and ``vwap=None``.
    """
    remaining = qty
    filled_notional = Decimal(0)
    filled_qty = Decimal(0)
    for level in depth.bids:
        take_qty = min(level.qty, remaining)
        filled_qty += take_qty
        filled_notional += take_qty * level.price
        remaining -= take_qty
        if remaining <= 0:
            break
    if remaining > 0:
        return WalkResult(
            vwap=None,
            filled_qty=filled_qty,
            filled_notional=filled_notional,
            slippage_bps=None,
            insufficient_liquidity=True,
        )
    vwap = filled_notional / filled_qty
    bps = int((depth.mid - vwap) / depth.mid * Decimal(10000))
    return WalkResult(
        vwap=vwap,
        filled_qty=filled_qty,
        filled_notional=filled_notional,
        slippage_bps=bps,
        insufficient_liquidity=False,
    )
