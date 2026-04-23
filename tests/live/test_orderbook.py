from decimal import Decimal
from datetime import datetime, timezone

from scout.live.types import Depth, DepthLevel
from scout.live.orderbook import walk_asks, walk_bids


def _levels(levels):
    return tuple(DepthLevel(price=Decimal(p), qty=Decimal(q)) for p, q in levels)


def _depth(bids, asks, mid):
    return Depth(
        pair="X",
        bids=_levels(bids),
        asks=_levels(asks),
        mid=Decimal(mid),
        fetched_at=datetime.now(timezone.utc),
    )


def test_walk_asks_vwap_single_level():
    d = _depth([], [("100", "10")], "100")
    r = walk_asks(d, Decimal("500"))
    assert not r.insufficient_liquidity
    assert r.filled_qty == Decimal("5")
    assert r.vwap == Decimal("100")
    assert r.slippage_bps == 0


def test_walk_asks_vwap_two_levels():
    d = _depth([], [("100", "1"), ("110", "10")], "100")
    # Need $200 -> $100 from level 1 (fills 1 unit), $100 from level 2 (fills 10/11)
    r = walk_asks(d, Decimal("200"))
    assert r.vwap > Decimal("100")
    assert r.vwap < Decimal("110")
    assert r.slippage_bps > 0


def test_walk_asks_flags_insufficient_liquidity():
    d = _depth([], [("100", "0.5")], "100")
    r = walk_asks(d, Decimal("200"))
    assert r.insufficient_liquidity
    assert r.vwap is None


def test_walk_bids_symmetrical():
    d = _depth([("100", "10"), ("90", "5")], [], "100")
    r = walk_bids(d, Decimal("12"))
    # All 10 units from top bid, 2 units from second
    assert r.filled_qty == Decimal("12")
    assert r.vwap < Decimal("100")
