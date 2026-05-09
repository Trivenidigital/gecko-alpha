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
from scout.live.balance_gate import check_sufficient_balance
from scout.live.binance_adapter import BinanceAuthError
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
        # M1.5a (design-stage R1-I1 + R2-I3) — Gate 10 disambiguation:
        "live_signed_disabled",
        "api_key_lacks_trade_scope",
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
        bid_total = sum((lv.price * lv.qty for lv in top_bids), Decimal(0))
        ask_total = sum((lv.price * lv.qty for lv in top_asks), Decimal(0))
        if bid_total < required or ask_total < required:
            return (
                GateResult(
                    passed=False,
                    reject_reason="insufficient_depth",
                    detail=(f"bid={bid_total} ask={ask_total} " f"required={required}"),
                ),
                venue,
            )

        # Gate 6: slippage projection
        walk = walk_asks(depth, size_usd)
        slippage_cap = self._config._s.LIVE_SLIPPAGE_BPS_CAP
        if walk.insufficient_liquidity or (
            walk.slippage_bps is not None and walk.slippage_bps > slippage_cap
        ):
            return (
                GateResult(
                    passed=False,
                    reject_reason="slippage_exceeds_cap",
                    detail=(f"slippage_bps={walk.slippage_bps} " f"cap={slippage_cap}"),
                ),
                venue,
            )

        # Gate 7: exposure cap. Query path is conditional on master kill:
        # - LIVE_TRADING_ENABLED=True (M1 v2.1):  query cross_venue_exposure
        #   view (Tasks 3+4 — aggregates binance live_trades + per-chain
        #   minara_<chain> paper_trades).
        # - LIVE_TRADING_ENABLED=False (BL-055 shadow soak): query
        #   shadow_trades directly (back-compat for the existing soak loop).
        # Master-kill flip in production = view-mode flip in semantics.
        assert self._db._conn is not None
        live_master_kill = getattr(self._config._s, "LIVE_TRADING_ENABLED", False)
        if live_master_kill:
            cur = await self._db._conn.execute(
                "SELECT COALESCE(SUM(open_exposure_usd), 0), "
                "       COALESCE(SUM(open_count), 0) "
                "FROM cross_venue_exposure"
            )
        else:
            cur = await self._db._conn.execute(
                "SELECT COALESCE(SUM(CAST(size_usd AS REAL)), 0), COUNT(*) "
                "FROM shadow_trades WHERE status = 'open'"
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
                    detail=(f"sum={sum_open_dec}+{size_usd} cap={max_exposure}"),
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

        # Gate 8 (per-trade notional cap) + Gate 9 (per-signal opt-in) only
        # fire when master kill is OFF — they enforce live-execution
        # invariants. Shadow-mode soak (BL-055) should not trip them.
        if live_master_kill:
            # Gate 8: per-trade notional cap (BL-NEW-LIVE-HYBRID M1 v2.1).
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

        # Gate 10: balance (live-only). Wired in M1.5a — replaces M1's
        # NotImplementedError stub. Returns 1 of 3 reject_reasons:
        # - 'live_signed_disabled' when LIVE_USE_REAL_SIGNED_REQUESTS=False
        #   (R1-I1: kill-switch state visibility on dashboard)
        # - 'api_key_lacks_trade_scope' when -2015 surfaces from balance
        #   fetch (R2-I3: scope-fail vs balance-fail disambiguation)
        # - 'insufficient_balance' on real balance shortage
        if self._config.mode == "live":
            # R1-I1: surface explicit kill-switch reject_reason so dashboard
            # can distinguish "balance gate disabled" from "balance insufficient."
            if not getattr(self._config._s, "LIVE_USE_REAL_SIGNED_REQUESTS", False):
                return (
                    GateResult(
                        passed=False,
                        reject_reason="live_signed_disabled",
                        detail=(
                            "LIVE_USE_REAL_SIGNED_REQUESTS=False — "
                            "emergency-revert posture"
                        ),
                    ),
                    venue,
                )

            try:
                bal_result = await check_sufficient_balance(
                    self._adapter,
                    float(size_usd),
                    margin_factor=1.1,
                )
            except BinanceAuthError as exc:
                # R2-I3: -2015 from POST = key lacks SPOT trade scope.
                # Distinguish from real balance shortage (operator might
                # otherwise debug "fund Binance" when actual fix is
                # "rotate key with TRADE permission").
                if "2015" in str(exc):
                    return (
                        GateResult(
                            passed=False,
                            reject_reason="api_key_lacks_trade_scope",
                            detail=f"Binance auth-fail on balance fetch: {exc}",
                        ),
                        venue,
                    )
                # -2014 / -1021 / other auth-class — surface as transient;
                # engine layer writes needs_manual_review row in M1.5b.
                raise

            if not bal_result.passed:
                # PR #86 V3-I3 fold: balance_gate signals venue-down via
                # detail prefix 'venue_unavailable:' so dashboards don't
                # misread Binance maintenance as 'insufficient_balance'.
                detail = bal_result.detail or ""
                reason = (
                    "venue_unavailable"
                    if detail.startswith("venue_unavailable:")
                    else "insufficient_balance"
                )
                return (
                    GateResult(
                        passed=False,
                        reject_reason=reason,
                        detail=detail,
                    ),
                    venue,
                )

        return GateResult(passed=True), venue
