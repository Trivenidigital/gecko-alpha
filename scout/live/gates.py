"""Pre-trade gates (spec §5). First-fail short-circuit. Returns GateResult.

Execution order (spec §5, first-failure-wins):

1. kill_switch           — KillSwitch.is_active() non-None
2. allowlist (special)   — signal not in allowlist; reject_reason=None,
                           detail='not_allowlisted'; engine treats as no-op
3. venue resolution      — no_venue vs override_disabled
4. depth_health          — top-10 bid+ask total notional >= multiplier * size_usd
                           (fetch_depth may raise VenueTransientError →
                           venue_unavailable)
5. slippage              — walk_asks projected slippage > LIVE_SLIPPAGE_BPS_CAP
                           or insufficient_liquidity
6. exposure              — SUM(size_usd) + new > cap  OR  COUNT >= max_positions
7. balance               — live-only; raises NotImplementedError in BL-055

Valid reject_reasons match the shadow_trades / live_trades CHECK constraint in
:func:`scout.db.Database.initialize`.
"""
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from scout.db import Database
from scout.live.exceptions import VenueTransientError
from scout.live.orderbook import walk_asks
from scout.live.types import GateResult, ResolvedVenue

if TYPE_CHECKING:
    from scout.live.adapter_base import ExchangeAdapter
    from scout.live.config import LiveConfig
    from scout.live.kill_switch import KillSwitch
    from scout.live.resolver import VenueResolver

log = structlog.get_logger(__name__)


# Valid reject reasons — must stay in sync with the CHECK constraint in
# scout.db (shadow_trades.reject_reason / live_trades.reject_reason).
# The gates module emits a subset pre-trade; the rest (daily_cap_hit,
# insufficient_balance) are emitted by the evaluator / live path.
VALID_REJECT_REASONS: frozenset[str] = frozenset(
    {
        "no_venue",
        "insufficient_depth",
        "slippage_exceeds_cap",
        "insufficient_balance",
        "daily_cap_hit",
        "kill_switch",
        "exposure_cap",
        "override_disabled",
        "venue_unavailable",
        # BL-NEW-LIVE-HYBRID M1 v2.1 additions:
        "notional_cap_exceeded",
        "signal_disabled",
        "token_aggregate",
        "dual_signal_aggregate",
        "all_candidates_failed",
        "master_kill",
        "mode_paper",
    }
)


class Gates:
    """Pre-trade gate evaluator. Stateless beyond injected dependencies."""

    def __init__(
        self,
        *,
        config: "LiveConfig",
        db: Database,
        resolver: "VenueResolver",
        adapter: "ExchangeAdapter",
        kill_switch: "KillSwitch",
    ) -> None:
        self._config = config
        self._db = db
        self._resolver = resolver
        self._adapter = adapter
        self._ks = kill_switch

    async def evaluate(
        self,
        *,
        signal_type: str,
        symbol: str,
        size_usd: Decimal,
    ) -> tuple[GateResult, ResolvedVenue | None]:
        """Run gates in spec §5 order. Return ``(GateResult, ResolvedVenue | None)``.

        The resolved venue is returned when resolution succeeds so the caller
        can skip re-resolving. Venue is ``None`` when:

        * resolution has not yet run (kill-switch / allowlist-skip paths), OR
        * resolution ran but returned ``None`` (no_venue / override_disabled).

        Allowlist-skip is a sentinel: ``passed=False`` with
        ``reject_reason=None`` and ``detail='not_allowlisted'``. The engine must
        treat this as a no-op and NOT insert a ``shadow_trades`` row.
        """
        # Gate 1: kill switch
        kill = await self._ks.is_active()
        if kill is not None:
            return (
                GateResult(
                    passed=False,
                    reject_reason="kill_switch",
                    detail=f"kill_event_id={kill.kill_event_id}",
                ),
                None,
            )

        # Gate 2: allowlist (special — no reject_reason, engine skips DB row)
        if not self._config.is_signal_enabled(signal_type):
            return (
                GateResult(
                    passed=False,
                    reject_reason=None,
                    detail="not_allowlisted",
                ),
                None,
            )

        # Gate 3+4: venue resolution + override_disabled discrimination
        venue = await self._resolver.resolve(symbol)
        if venue is None:
            # Distinguish override_disabled from genuine no_venue by reading
            # venue_overrides directly. A row with disabled=1 means an operator
            # has explicitly banned this symbol; no row (or disabled=0) means
            # neither exchangeInfo nor override could map it.
            assert self._db._conn is not None
            cur = await self._db._conn.execute(
                "SELECT disabled FROM venue_overrides WHERE symbol = ?",
                (symbol.upper(),),
            )
            row = await cur.fetchone()
            if row is not None and row[0] == 1:
                return (
                    GateResult(
                        passed=False,
                        reject_reason="override_disabled",
                        detail=f"symbol={symbol}",
                    ),
                    None,
                )
            return (
                GateResult(
                    passed=False,
                    reject_reason="no_venue",
                    detail=f"symbol={symbol}",
                ),
                None,
            )

        # Gate 5: depth health (fetch_depth may raise VenueTransientError)
        try:
            depth = await self._adapter.fetch_depth(venue.pair)
        except VenueTransientError as exc:
            return (
                GateResult(
                    passed=False,
                    reject_reason="venue_unavailable",
                    detail=f"fetch_depth: {exc}",
                ),
                venue,
            )

        multiplier = self._config._s.LIVE_DEPTH_HEALTH_MULTIPLIER
        required = multiplier * size_usd
        top_bids = depth.bids[:10]
        top_asks = depth.asks[:10]
        bid_total = sum(
            (lv.price * lv.qty for lv in top_bids), Decimal(0)
        )
        ask_total = sum(
            (lv.price * lv.qty for lv in top_asks), Decimal(0)
        )
        if bid_total < required or ask_total < required:
            return (
                GateResult(
                    passed=False,
                    reject_reason="insufficient_depth",
                    detail=(
                        f"bid={bid_total} ask={ask_total} "
                        f"required={required}"
                    ),
                ),
                venue,
            )

        # Gate 6: slippage projection
        walk = walk_asks(depth, size_usd)
        slippage_cap = self._config._s.LIVE_SLIPPAGE_BPS_CAP
        if walk.insufficient_liquidity or (
            walk.slippage_bps is not None
            and walk.slippage_bps > slippage_cap
        ):
            return (
                GateResult(
                    passed=False,
                    reject_reason="slippage_exceeds_cap",
                    detail=(
                        f"slippage_bps={walk.slippage_bps} "
                        f"cap={slippage_cap}"
                    ),
                ),
                venue,
            )

        # Gate 7: cross-venue exposure cap (BL-NEW-LIVE-HYBRID M1 v2.1).
        # Queries cross_venue_exposure SQL view (Tasks 3+4). View aggregates
        # binance live_trades open + per-chain minara_<chain> paper_trades open.
        assert self._db._conn is not None
        cur = await self._db._conn.execute(
            "SELECT COALESCE(SUM(open_exposure_usd), 0), "
            "       COALESCE(SUM(open_count), 0) "
            "FROM cross_venue_exposure"
        )
        row = await cur.fetchone()
        sum_open = float(row[0]) if row is not None else 0.0
        count_open = int(row[1]) if row is not None else 0
        sum_open_dec = Decimal(str(sum_open))
        max_exposure = self._config._s.LIVE_MAX_EXPOSURE_USD
        max_positions = self._config._s.LIVE_MAX_OPEN_POSITIONS
        if sum_open_dec + size_usd > max_exposure:
            return (
                GateResult(
                    passed=False,
                    reject_reason="exposure_cap",
                    detail=(
                        f"sum={sum_open_dec}+{size_usd} "
                        f"cap={max_exposure}"
                    ),
                ),
                venue,
            )
        if count_open >= max_positions:
            return (
                GateResult(
                    passed=False,
                    reject_reason="exposure_cap",
                    detail=f"count={count_open} cap={max_positions}",
                ),
                venue,
            )

        # Gate 8: per-trade notional cap (BL-NEW-LIVE-HYBRID M1 v2.1).
        # Reuses existing LIVE_TRADE_AMOUNT_USD per Option A drift-check.
        notional_cap = self._config._s.LIVE_TRADE_AMOUNT_USD
        if size_usd > notional_cap:
            return (
                GateResult(
                    passed=False,
                    reject_reason="notional_cap_exceeded",
                    detail=f"size_usd={size_usd} cap={notional_cap}",
                ),
                venue,
            )

        # Gate 9: per-signal opt-in (Layer 3 of 4-layer kill stack).
        cur = await self._db._conn.execute(
            "SELECT live_eligible FROM signal_params WHERE signal_type = ?",
            (signal_type,),
        )
        row = await cur.fetchone()
        if not (row and bool(row[0])):
            return (
                GateResult(
                    passed=False,
                    reject_reason="signal_disabled",
                    detail=f"signal_params.live_eligible=0 for {signal_type}",
                ),
                venue,
            )

        # Gate 10: balance (live-only — BL-055 must be blocked at startup;
        # full implementation lands in Task 8 / balance_gate.py).
        if self._config.mode == "live":
            raise NotImplementedError(
                "Balance gate wired in Task 8; LIVE_MODE=live must be "
                "blocked at startup in BL-055"
            )

        return GateResult(passed=True), venue
