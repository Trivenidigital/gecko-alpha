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

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

import structlog

from scout.db import Database
from scout.live.adapter_base import ExchangeAdapter
from scout.live.config import LiveConfig
from scout.live.gates import Gates
from scout.live.intent import TradeIntent
from scout.live.kill_switch import KillSwitch
from scout.live.mandate import ExecutionMandate, MandateRefused
from scout.live.metrics import inc
from scout.live.orderbook import walk_asks
from scout.live.resolver import VenueResolver

if TYPE_CHECKING:
    from scout.live.routing import RoutingLayer

log = structlog.get_logger(__name__)

#: How long a dispatch intent stays valid. The engine builds, authorizes and submits
#: within one coroutine, so this is a crash-recovery bound rather than a queueing one:
#: an intent read back from the ledger after a restart must not still look live.
INTENT_TTL = timedelta(minutes=5)

#: Bumped whenever the terms the engine puts INTO an intent change shape. It is part
#: of the hashed content, so a bump makes every subsequently-minted intent
#: distinguishable from one built by the previous policy — which is what lets a
#: reconciler tell "same trade" from "same trade under different rules".
DISPATCH_POLICY_VERSION = "engine-dispatch-v1"


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
        mandate: ExecutionMandate | None = None,
    ) -> None:
        self._config = config
        self._resolver = resolver
        self._adapter = adapter
        self._db = db
        self._ks = kill_switch
        self._routing = routing
        # Default-constructed from settings when not injected, so a caller cannot get
        # an UNGATED engine by forgetting the kwarg. Absent settings yields a mandate
        # whose every getattr default refuses — the fail-closed direction.
        self._mandate = mandate or ExecutionMandate(
            settings=getattr(config, "_s", None), db=db
        )
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
            flag_routing = getattr(settings, "LIVE_USE_ROUTING_LAYER", False)
            flag_signed = getattr(settings, "LIVE_USE_REAL_SIGNED_REQUESTS", False)
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
            # LIVE-02 fail-closed guard: a routing engine dispatches live BUYS
            # (_dispatch_live). The ONLY thing that ever sells a live position is
            # live_evaluator_loop, gated on LIVE_CLOSER_ENABLED. Booting into
            # "can buy, cannot sell" orphans real money — refuse it. Default
            # True; set False only for a maintenance window with routing also
            # off. (main.py spawns the closer loop iff this flag is True.)
            flag_closer = getattr(settings, "LIVE_CLOSER_ENABLED", True)
            if flag_routing and not flag_closer:
                raise RuntimeError(
                    "Misconfig: LIVE_USE_ROUTING_LAYER=True but "
                    "LIVE_CLOSER_ENABLED=False. The live close/exit loop is "
                    "the only path that sells a live position; booting buy-only "
                    "orphans real money. Set LIVE_CLOSER_ENABLED=True or "
                    "LIVE_USE_ROUTING_LAYER=False before boot."
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
        if self._config.mode == "live" and flag_routing and self._routing is not None:
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

    async def _write_live_reject(
        self,
        *,
        paper_trade: _PaperTradeLike,
        size_usd,
        reject_reason: str,
        venue: str = "",
        pair: str = "",
        intent_hash: str | None = None,
    ) -> None:
        """Write a terminal ``rejected`` ``live_trades`` row.

        ``live_trades`` declares venue and pair NOT NULL, so a rejection that
        happened before a venue was chosen writes sentinel-empty strings — the
        existing no-venue convention, factored out because there are now four
        pre-submission refusal paths rather than one.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        assert self._db._conn is not None
        async with self._db._txn_lock:
            await self._db._conn.execute(
                "INSERT INTO live_trades "
                "(paper_trade_id, coin_id, symbol, venue, pair, "
                " signal_type, size_usd, status, reject_reason, "
                " created_at, intent_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'rejected', ?, ?, ?)",
                (
                    paper_trade.id,
                    paper_trade.coin_id,
                    paper_trade.symbol,
                    venue,
                    pair,
                    paper_trade.signal_type,
                    str(size_usd),
                    reject_reason,
                    now_iso,
                    intent_hash,
                ),
            )
            await self._db._conn.commit()
        await inc(self._db, f"live_dispatch_rejected_{reject_reason}")

    def _build_entry_intent(
        self,
        *,
        paper_trade: _PaperTradeLike,
        size_usd,
        routed,
        now: datetime,
    ) -> TradeIntent:
        """Mint the content-bound intent for this dispatch.

        Every term the execution depends on goes in, because the hash is only worth
        what it covers: venue, instrument, side, size, order type and the slippage
        bound. ``exact_quantity`` is denominated in QUOTE units — the Binance path
        submits ``quoteOrderQty``, i.e. "spend this much USDT", not "buy this many
        coins" — and ``quantity_denomination='quote'`` records that so a reader
        cannot mistake the number for a base amount.

        Built AFTER routing because ``preferred_venue`` is one of the hashed terms:
        an intent minted before the venue is known would have to carry a placeholder,
        and a placeholder in a hashed term is a hash that does not bind what happened.
        """
        notional = Decimal(str(size_usd))
        slippage_cap = int(getattr(self._config._s, "LIVE_SLIPPAGE_BPS_CAP", 50))
        return TradeIntent(
            strategy_id=paper_trade.signal_type,
            decision_id=f"paper-{paper_trade.id}",
            created_at=now,
            expires_at=now + INTENT_TTL,
            execution_deadline=now + INTENT_TTL,
            mode=self._mandate.mode,
            policy_version=DISPATCH_POLICY_VERSION,
            venue_family="cex",
            preferred_venue=routed.venue,
            base_asset=paper_trade.symbol,
            quote_asset=str(routed.candidate.quote),
            side="buy",
            exact_quantity=notional,
            quantity_denomination="quote",
            maximum_notional=notional,
            order_type="market",
            # The SAME cap Gate 5 already enforces (gates.py:196), read from the same
            # setting rather than restated — a second slippage number here would be a
            # bound the pre-trade gate does not know about, and the two would drift.
            maximum_slippage_bps=slippage_cap,
            maximum_price_impact_bps=slippage_cap,
        )

    async def _dispatch_live(
        self,
        *,
        paper_trade: _PaperTradeLike,
        size_usd,
    ) -> None:
        """M1.5b live-mode dispatch, now mandate-gated and intent-bound.

        Order of operations, and why each step is where it is:

        1. **Mandate precheck**, before anything with a side effect. Routing does
           on-demand venue-metadata fetches and DB writes; an inactive mandate must
           not cause them.
        2. **Route**, through the capability gate — which also yields the ADAPTER for
           the selected venue. Previously this method routed to pick ``top.venue`` and
           then submitted through ``self._adapter``, so selecting any venue other than
           the injected one placed that venue's pair on the injected venue.
        3. **Mint the intent** bound to the selected venue and its instrument.
        4. **Authorize** the full intent against the mandate.
        5. **Derive the venue's client order id from the intent hash**, so the
           submission's identity is its terms.
        6. Submit through the routed adapter; await the fill; record.
        """
        from scout.live.adapter_base import OrderRequest
        from scout.live.binance_adapter import (
            BinanceAuthError,
            BinanceIPBanError,
        )
        from scout.live.correction_counter import increment_consecutive
        from scout.live.exceptions import VenueTransientError
        from scout.live.kill_switch import compute_kill_duration
        from scout.live.order_id import (
            UnknownVenueOrderIdForm,
            client_order_id_for_venue,
        )
        from scout.live.routing import NoRoutableVenue

        canonical = paper_trade.symbol
        chain_hint = getattr(paper_trade, "chain", None)

        log.info(
            "live_dispatch_entered",
            paper_trade_id=paper_trade.id,
            canonical=canonical,
            size_usd=str(size_usd),
            signal_type=paper_trade.signal_type,
        )

        # --- 1. Mandate precheck, before any side effect -------------------
        try:
            self._mandate.precheck()
        except MandateRefused as exc:
            await self._write_live_reject(
                paper_trade=paper_trade,
                size_usd=size_usd,
                reject_reason="mandate_inactive",
            )
            log.warning(
                "live_dispatch_mandate_refused",
                paper_trade_id=paper_trade.id,
                gate=exc.gate,
                detail=exc.message,
                phase="precheck",
            )
            return

        # --- 2. Route through the capability gate --------------------------
        try:
            routed = await self._routing.select_route(
                canonical=canonical,
                chain_hint=chain_hint,
                signal_type=paper_trade.signal_type,
                size_usd=float(size_usd),
                venue_family="cex",
                order_type="market",
                reduce_only=False,
            )
        except NoRoutableVenue as exc:
            await self._write_live_reject(
                paper_trade=paper_trade,
                size_usd=size_usd,
                reject_reason=exc.reject_reason,
            )
            # V3-I2: WARN level — an operator asking "did this signal succeed?"
            # should not have to skim INFO to find out nothing routed.
            log.warning(
                "live_dispatch_no_route",
                paper_trade_id=paper_trade.id,
                canonical=canonical,
                reject_reason=exc.reject_reason,
                rejections=list(exc.rejections),
            )
            return

        log.info(
            "live_dispatch_venue_selected",
            paper_trade_id=paper_trade.id,
            venue=routed.venue,
            venue_pair=routed.venue_pair,
        )

        # --- 3 + 4. Mint the intent, then authorize it ---------------------
        now = datetime.now(timezone.utc)
        try:
            intent = self._build_entry_intent(
                paper_trade=paper_trade,
                size_usd=size_usd,
                routed=routed,
                now=now,
            )
        except (ValueError, TypeError) as exc:
            # An intent that will not construct is terms that will not validate.
            # Refusing is the only safe answer — there is nothing to bind an order to.
            await self._write_live_reject(
                paper_trade=paper_trade,
                size_usd=size_usd,
                reject_reason="venue_capability_refused",
                venue=routed.venue,
                pair=routed.venue_pair,
            )
            log.warning(
                "live_dispatch_intent_invalid",
                paper_trade_id=paper_trade.id,
                venue=routed.venue,
                err=str(exc),
            )
            return

        try:
            decision = await self._mandate.authorize(intent, at=now)
        except MandateRefused as exc:
            await self._write_live_reject(
                paper_trade=paper_trade,
                size_usd=size_usd,
                reject_reason="mandate_inactive",
                venue=routed.venue,
                pair=routed.venue_pair,
                intent_hash=intent.intent_hash,
            )
            log.warning(
                "live_dispatch_mandate_refused",
                paper_trade_id=paper_trade.id,
                gate=exc.gate,
                detail=exc.message,
                phase="authorize",
                intent_id=intent.intent_id,
            )
            return

        # --- 5. The submission's identity IS its terms ---------------------
        try:
            cid = client_order_id_for_venue(intent, routed.venue)
        except (UnknownVenueOrderIdForm, ValueError) as exc:
            await self._write_live_reject(
                paper_trade=paper_trade,
                size_usd=size_usd,
                reject_reason="venue_capability_refused",
                venue=routed.venue,
                pair=routed.venue_pair,
                intent_hash=intent.intent_hash,
            )
            log.warning(
                "live_dispatch_no_order_id_form",
                paper_trade_id=paper_trade.id,
                venue=routed.venue,
                err=str(exc),
            )
            return

        request = OrderRequest(
            paper_trade_id=paper_trade.id,
            canonical=canonical,
            venue_pair=routed.venue_pair,
            side="buy",
            size_usd=float(size_usd),
            # Legacy field, retained so adapters that have not been wired to intents
            # still have something stable to derive from. It is NOT what mints the
            # cid here — `client_order_id` below is, and it is content-bound.
            intent_uuid=intent.intent_id,
            client_order_id=cid,
            intent_hash=intent.intent_hash,
            mandate_mode=decision.mode,
        )

        # --- 6. Submit through the ROUTED adapter --------------------------
        adapter = routed.adapter
        top = routed.candidate
        try:
            venue_order_id = await adapter.place_order_request(request)
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
            # V1+V2+V3 PR-stage CRITICAL fix: KillSwitch.engage does not
            # exist; real API is trigger(triggered_by, reason, duration).
            # PR-1 2026-06-01 fail-safe fix: triggered_by MUST be a
            # kill_events.triggered_by CHECK-allowed value
            # ('daily_loss_cap','manual','ops_maintenance'). The prior
            # 'live_engine' violated the constraint → IntegrityError, so this
            # venue-fatal kill NEVER engaged. The specific cause is preserved
            # in `reason`; 'ops_maintenance' is the operational-halt bucket.
            await self._ks.trigger(
                triggered_by="ops_maintenance",
                reason="binance_auth_revoked_mid_session",
                duration=compute_kill_duration(datetime.now(timezone.utc)),
            )
            return
        except BinanceIPBanError as exc:
            log.error(
                "live_dispatch_ip_banned",
                paper_trade_id=paper_trade.id,
                err=str(exc),
            )
            # 'ops_maintenance' = CHECK-allowed operational-halt bucket; cause
            # preserved in `reason` (see auth-revoked branch above).
            await self._ks.trigger(
                triggered_by="ops_maintenance",
                reason="binance_ip_banned",
                duration=compute_kill_duration(datetime.now(timezone.utc)),
            )
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
            confirmation = await adapter.await_fill_confirmation(
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
            # LIVE-02: persist the REAL entry fill so the live evaluator can
            # size the sell (entry_fill_qty base units) and compute realized PnL
            # (entry_fill_price). Keyed by the same cid the adapter wrote the
            # 'open' row under. Under txn_lock per the shared-connection rule.
            assert self._db._conn is not None
            async with self._db._txn_lock:
                await self._db._conn.execute(
                    "UPDATE live_trades SET entry_fill_price=?, entry_fill_qty=? "
                    "WHERE client_order_id=?",
                    (
                        (
                            str(confirmation.fill_price)
                            if confirmation.fill_price is not None
                            else None
                        ),
                        (
                            str(confirmation.filled_qty)
                            if confirmation.filled_qty is not None
                            else None
                        ),
                        cid,
                    ),
                )
                await self._db._conn.commit()
            await increment_consecutive(
                self._db,
                paper_trade.signal_type,
                top.venue,
                paper_trade_id=paper_trade.id,
            )
        else:
            # LIVE-08 (audit S2-3): a non-'filled' terminal buy
            # (partial/rejected/timeout) must NOT leave a permanent 'open' row —
            # once Gate 7 is live those sum monotonically into exposure_cap and
            # lock the subsystem out. Terminalize to needs_manual_review so the
            # row stops counting as open and the operator (or the boot
            # reconciler) resolves it. No auto-retry: a partial risks reselling
            # more than is held, and a rejected buy has no position to manage.
            assert self._db._conn is not None
            async with self._db._txn_lock:
                await self._db._conn.execute(
                    "UPDATE live_trades SET status='needs_manual_review' "
                    "WHERE client_order_id=? AND status='open'",
                    (cid,),
                )
                await self._db._conn.commit()
            await inc(self._db, f"live_dispatch_terminal_{confirmation.status}")
            log.warning(
                "live_dispatch_terminal_needs_review",
                paper_trade_id=paper_trade.id,
                venue_order_id=venue_order_id,
                status=confirmation.status,
            )
