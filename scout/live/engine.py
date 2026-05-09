"""LiveEngine — single chokepoint between PaperTrader and the live subsystem.

Entry point: :meth:`on_paper_trade_opened`. Fire-and-forget from caller.

Handoff matrix (spec §5 + §2.2):

1. is_eligible=False         → log live_handoff_skipped, NO DB row
2. Kill active               → log live_handoff_skipped_killed, NO DB row
3. Resolver None (no_venue)  → DB row rejected/no_venue, metric inc
4. override_disabled         → DB row rejected/override_disabled, metric inc
5. insufficient_depth        → DB row rejected/insufficient_depth, metric inc
6. slippage_exceeds_cap      → DB row rejected/slippage_exceeds_cap, metric inc
7. exposure_cap              → DB row rejected/exposure_cap, metric inc
8. Happy path under shadow / live-without-routing → shadow_trades open row
9. Happy path under live + LIVE_USE_ROUTING_LAYER=True → _dispatch_live
   (M1.5b — routes via RoutingLayer, places order, awaits fill, increments
   correction counter on terminal=filled)

M1.5b: live mode dispatch is permitted via _dispatch_live. main.py boot
guards (scout/main.py:1062-1086) enforce LIVE_TRADING_ENABLED=True +
LIVE_USE_REAL_SIGNED_REQUESTS=True for mode='live'. Engine __init__
raises RuntimeError if LIVE_USE_ROUTING_LAYER=True without
LIVE_USE_REAL_SIGNED_REQUESTS=True (silent-no-op misconfig CRASH per
design §2.2). BL-055 shadow soak ends at first live signal under
LIVE_USE_ROUTING_LAYER=True (design §2.7a).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol

import structlog

from scout.db import Database
from scout.live.adapter_base import ExchangeAdapter
from scout.live.config import LiveConfig
from scout.live.gates import Gates
from scout.live.kill_switch import KillSwitch
from scout.live.metrics import inc
from scout.live.orderbook import walk_asks
from scout.live.resolver import VenueResolver

if TYPE_CHECKING:
    from scout.live.routing import RoutingLayer

log = structlog.get_logger(__name__)


class _PaperTradeLike(Protocol):
    id: int
    coin_id: str
    symbol: str
    signal_type: str


class LiveEngine:
    """Chokepoint dispatcher. Reads from Gates, writes to shadow_trades ledger.

    M1.5b: optionally dispatches live trades when `LIVE_MODE='live'` AND
    `LIVE_USE_ROUTING_LAYER=True` AND a `routing` layer is wired.
    """

    def __init__(
        self,
        *,
        config: LiveConfig,
        resolver: VenueResolver,
        adapter: ExchangeAdapter,
        db: Database,
        kill_switch: KillSwitch,
        routing: "RoutingLayer | None" = None,
    ) -> None:
        self._config = config
        self._resolver = resolver
        self._adapter = adapter
        self._db = db
        self._ks = kill_switch
        self._routing = routing
        self._gates = Gates(
            config=config,
            db=db,
            resolver=resolver,
            adapter=adapter,
            kill_switch=kill_switch,
        )

        # M1.5b R2-C1 + R2-I3 + R1-M2 fold: fail-closed CRASH on misconfig.
        # Cost-of-crash is bounded by systemd RestartSec=30s +
        # StartLimitBurst=3 + OnFailure Telegram (M1.5a runbook §1+§2).
        # Cost-of-WARN-and-skip is unbounded (operator walkaway = arbitrary
        # missed signals). Shadow mode is exempt — no live trades are
        # dispatched under shadow.
        if config.mode == "live":
            settings = getattr(config, "_s", None)
            flag_routing = getattr(
                settings, "LIVE_USE_ROUTING_LAYER", False
            )
            flag_signed = getattr(
                settings, "LIVE_USE_REAL_SIGNED_REQUESTS", False
            )
            if flag_routing and not flag_signed:
                raise RuntimeError(
                    "Misconfig: LIVE_USE_ROUTING_LAYER=True but "
                    "LIVE_USE_REAL_SIGNED_REQUESTS=False. Engine would "
                    "silently no-op every signal. Set "
                    "LIVE_USE_REAL_SIGNED_REQUESTS=True or "
                    "LIVE_USE_ROUTING_LAYER=False before boot."
                )
            if flag_routing and routing is None:
                raise RuntimeError(
                    "Misconfig: LIVE_USE_ROUTING_LAYER=True but "
                    "routing=None. Check scout/main.py construction "
                    "passes routing=live_routing kwarg to LiveEngine."
                )

    def is_eligible(self, signal_type: str) -> bool:
        """Cheap pre-check for chokepoint (spec §2.3). No I/O."""
        return self._config.is_signal_enabled(signal_type)

    async def on_paper_trade_opened(self, paper_trade: _PaperTradeLike) -> None:
        """Single entry point from PaperTrader chokepoint. Fire-and-forget.

        Layer 1 of 4-layer kill stack (BL-NEW-LIVE-HYBRID M1 v2.1):
        master kill (`LIVE_TRADING_ENABLED`) is enforced at process
        startup in `scout/main.py` — when `LIVE_MODE='live'` AND
        `LIVE_TRADING_ENABLED=False`, startup refuses to construct the
        live adapter (existing balance_gate NotImplementedError is the
        belt-and-braces guard).

        M1.5b: live mode dispatch is permitted via `_dispatch_live` when
        `LIVE_USE_ROUTING_LAYER=True` AND a `routing` layer is wired.
        The previous belt-and-braces assert was removed (R1+R2 plan-stage
        finding C1) — main.py boot guards + engine __init__ misconfig
        CRASH provide the safety contract.
        """
        # M1.5b: assert removed. main.py boot guards + engine __init__
        # misconfig CRASH provide the safety contract.
        trade_id = paper_trade.id
        size_usd = self._config.resolve_size_usd(paper_trade.signal_type)

        log.info(
            "live_handoff_started",
            paper_trade_id=trade_id,
            signal_type=paper_trade.signal_type,
            mode=self._config.mode,
        )

        # Evaluate all gates. Allowlist-skip and kill-active produce no DB row.
        result, venue = await self._gates.evaluate(
            signal_type=paper_trade.signal_type,
            symbol=paper_trade.symbol,
            size_usd=size_usd,
        )

        # Allowlist skip (passed=False, reject_reason=None, detail='not_allowlisted')
        if not result.passed and result.reject_reason is None:
            log.info(
                "live_handoff_skipped",
                paper_trade_id=trade_id,
                signal_type=paper_trade.signal_type,
                reason="not_allowlisted",
            )
            return

        # Kill active → no DB row (spec §2.2 point 2)
        if not result.passed and result.reject_reason == "kill_switch":
            log.info(
                "live_handoff_skipped_killed",
                paper_trade_id=trade_id,
                detail=result.detail,
            )
            return

        now_iso = datetime.now(timezone.utc).isoformat()

        # Other gate failure → write rejected row + inc metric
        if not result.passed:
            reason = result.reject_reason
            pair = venue.pair if venue is not None else ""
            venue_name = venue.venue if venue is not None else "binance"
            assert self._db._conn is not None
            async with self._db._txn_lock:
                await self._db._conn.execute(
                    "INSERT INTO shadow_trades "
                    "(paper_trade_id, coin_id, symbol, venue, pair, signal_type, "
                    " size_usd, status, reject_reason, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'rejected', ?, ?)",
                    (
                        trade_id,
                        paper_trade.coin_id,
                        paper_trade.symbol,
                        venue_name,
                        pair,
                        paper_trade.signal_type,
                        str(size_usd),
                        reason,
                        now_iso,
                    ),
                )
                await self._db._conn.commit()
            await inc(self._db, f"shadow_rejects_{reason}")
            log.info(
                "live_pretrade_gate_failed",
                paper_trade_id=trade_id,
                reject_reason=reason,
                detail=result.detail,
            )
            return

        # Happy path → walk asks for entry vwap, write open row, inc metric
        assert venue is not None
        try:
            depth = await self._adapter.fetch_depth(venue.pair)
        except Exception as exc:
            # Gate 5 (depth health) already fetched depth and passed; if the
            # second fetch here fails transiently we write rejected as
            # venue_unavailable. This is rare but possible.
            assert self._db._conn is not None
            async with self._db._txn_lock:
                await self._db._conn.execute(
                    "INSERT INTO shadow_trades "
                    "(paper_trade_id, coin_id, symbol, venue, pair, signal_type, "
                    " size_usd, status, reject_reason, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'rejected', "
                    " 'venue_unavailable', ?)",
                    (
                        trade_id,
                        paper_trade.coin_id,
                        paper_trade.symbol,
                        venue.venue,
                        venue.pair,
                        paper_trade.signal_type,
                        str(size_usd),
                        now_iso,
                    ),
                )
                await self._db._conn.commit()
            await inc(self._db, "shadow_rejects_venue_unavailable")
            log.warning(
                "live_handoff_walk_fetch_failed",
                paper_trade_id=trade_id,
                error=str(exc),
            )
            return

        walk = walk_asks(depth, size_usd)
        entry_vwap = str(walk.vwap) if walk.vwap is not None else None
        entry_slip = walk.slippage_bps  # may be None
        mid = str(depth.mid)

        # M1.5b: under live + routing-flag + routing-layer, dispatch via
        # _dispatch_live and SKIP shadow_trades happy-path write (design
        # §2.7a — BL-055 shadow soak ends at first live signal).
        settings = getattr(self._config, "_s", None)
        flag_routing = getattr(settings, "LIVE_USE_ROUTING_LAYER", False)
        if (
            self._config.mode == "live"
            and flag_routing
            and self._routing is not None
        ):
            await self._dispatch_live(
                paper_trade=paper_trade,
                size_usd=size_usd,
            )
            return

        assert self._db._conn is not None
        async with self._db._txn_lock:
            await self._db._conn.execute(
                "INSERT INTO shadow_trades "
                "(paper_trade_id, coin_id, symbol, venue, pair, signal_type, "
                " size_usd, entry_walked_vwap, mid_at_entry, "
                " entry_slippage_bps, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                (
                    trade_id,
                    paper_trade.coin_id,
                    paper_trade.symbol,
                    venue.venue,
                    venue.pair,
                    paper_trade.signal_type,
                    str(size_usd),
                    entry_vwap,
                    mid,
                    entry_slip,
                    now_iso,
                ),
            )
            await self._db._conn.commit()
        await inc(self._db, "shadow_orders_opened")
        log.info(
            "live_shadow_order_opened",
            paper_trade_id=trade_id,
            venue=venue.venue,
            pair=venue.pair,
            entry_walked_vwap=entry_vwap,
            mid=mid,
            slippage_bps=entry_slip,
            size_usd=str(size_usd),
        )

    async def _dispatch_live(
        self,
        *,
        paper_trade: _PaperTradeLike,
        size_usd,
    ) -> None:
        """M1.5b live-mode dispatch (V1-C1 routing-half + V1-C2 closures).

        - Routes via RoutingLayer
        - Calls adapter.place_order_request (M1.5a idempotency-aware)
        - Calls adapter.await_fill_confirmation (M1.5a polling) using
          the same cid the adapter just wrote to live_trades
        - On terminal=filled -> increment correction counter
        - On BinanceAuthError mid-session -> engages KillSwitch
        - On no candidates -> writes live_trades reject row (Q2 fold)
        """
        from uuid import uuid4

        from scout.live.adapter_base import OrderRequest
        from scout.live.binance_adapter import (
            BinanceAuthError,
            BinanceIPBanError,
        )
        from scout.live.correction_counter import increment_consecutive
        from scout.live.exceptions import VenueTransientError
        from scout.live.idempotency import make_client_order_id

        canonical = paper_trade.symbol
        chain_hint = getattr(paper_trade, "chain", None)

        log.info(
            "live_dispatch_entered",
            paper_trade_id=paper_trade.id,
            canonical=canonical,
            size_usd=str(size_usd),
            signal_type=paper_trade.signal_type,
        )

        candidates = await self._routing.get_candidates(
            canonical=canonical,
            chain_hint=chain_hint,
            signal_type=paper_trade.signal_type,
            size_usd=float(size_usd),
        )

        log.info(
            "live_dispatch_candidates_returned",
            paper_trade_id=paper_trade.id,
            count=len(candidates),
            top_venue=candidates[0].venue if candidates else None,
        )

        if not candidates:
            now_iso = datetime.now(timezone.utc).isoformat()
            assert self._db._conn is not None
            async with self._db._txn_lock:
                await self._db._conn.execute(
                    "INSERT INTO live_trades "
                    "(paper_trade_id, status, reject_reason, created_at) "
                    "VALUES (?, 'rejected', 'no_venue', ?)",
                    (paper_trade.id, now_iso),
                )
                await self._db._conn.commit()
            log.info(
                "live_dispatch_no_venue",
                paper_trade_id=paper_trade.id,
                canonical=canonical,
            )
            return

        top = candidates[0]
        intent_uuid = str(uuid4())
        request = OrderRequest(
            paper_trade_id=paper_trade.id,
            canonical=canonical,
            venue_pair=top.venue_pair,
            side="buy",
            size_usd=float(size_usd),
            intent_uuid=intent_uuid,
        )
        # R1-C2 fix: derive same cid the adapter writes to live_trades.
        cid = make_client_order_id(paper_trade.id, intent_uuid)

        try:
            venue_order_id = await self._adapter.place_order_request(request)
        except NotImplementedError as exc:
            log.info(
                "live_dispatch_signed_disabled",
                paper_trade_id=paper_trade.id,
                err=str(exc),
            )
            return
        except BinanceAuthError as exc:
            log.error(
                "live_dispatch_auth_revoked_mid_session",
                paper_trade_id=paper_trade.id,
                err=str(exc),
            )
            await self._ks.engage(reason="binance_auth_revoked_mid_session")
            return
        except BinanceIPBanError as exc:
            log.error(
                "live_dispatch_ip_banned",
                paper_trade_id=paper_trade.id,
                err=str(exc),
            )
            await self._ks.engage(reason="binance_ip_banned")
            return
        except VenueTransientError as exc:
            log.info(
                "live_dispatch_venue_transient",
                paper_trade_id=paper_trade.id,
                err=str(exc),
            )
            return
        except Exception:
            log.exception(
                "live_dispatch_place_order_failed",
                paper_trade_id=paper_trade.id,
            )
            return

        try:
            confirmation = await self._adapter.await_fill_confirmation(
                venue_order_id=venue_order_id,
                client_order_id=cid,
                timeout_sec=30.0,
            )
        except Exception:
            log.exception(
                "live_dispatch_await_fill_failed",
                paper_trade_id=paper_trade.id,
                venue_order_id=venue_order_id,
            )
            return

        log.info(
            "live_dispatch_terminal",
            paper_trade_id=paper_trade.id,
            venue_order_id=venue_order_id,
            status=confirmation.status,
            fill_price=confirmation.fill_price,
        )

        if confirmation.status == "filled":
            await increment_consecutive(
                self._db, paper_trade.signal_type, top.venue
            )
