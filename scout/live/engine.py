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
        belt-and-braces guard). Shadow mode (`LIVE_MODE='shadow'`) is
        paper-money and continues to flow through the engine for BL-055
        soak telemetry. The engine entry NO LONGER short-circuits on
        master kill, because that would also block shadow-mode soak.
        """
        assert self._config.mode != "live", (
            "LiveEngine reached in LIVE_MODE=live — startup guard in "
            "scout/main.py failed; refusing to write any row"
        )
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
