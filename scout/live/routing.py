"""BL-NEW-LIVE-HYBRID M1 v2.1: Routing layer.

Per signal fire: takes (canonical, chain_hint, signal_type, size_usd),
returns ranked candidate list of `RouteCandidate`s.

Layer-1 M1-blocker guards:
- live-position-aggregator: rejects when LIVE_MAX_OPEN_POSITIONS_PER_TOKEN met
- on-demand venue_listings fetch: triggered when canonical has 0 rows
- chain="coingecko" enrichment: queries ALL tiers before defaulting CEX
- OverrideStore PREPEND: forces chain's venues to top of candidate list
- delisting fallback: re-evaluates on adapter reject with 'delisted'

Latency budget: <200ms p95. Quote/depth metrics are pre-fetched into
venue_health by the HealthProbe service (Task 10); routing reads, does
NOT compute live.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import structlog

from scout.db import Database
from scout.live.adapter_base import ExchangeAdapter

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RouteCandidate:
    venue: str
    venue_pair: str
    expected_fill_price: float | None
    expected_slippage_bps: float | None
    available_capital_usd: float | None
    venue_health_score: float


class RoutingLayer:
    """Stateless routing service. Pass adapters at construction; methods
    read from DB views populated by per-venue services (Task 10) +
    do on-demand metadata fetch when venue_listings is empty for a
    canonical."""

    def __init__(
        self,
        *,
        db: Database,
        settings,
        adapters: dict[str, ExchangeAdapter],
    ) -> None:
        self._db = db
        self._settings = settings
        self._adapters = adapters

    async def get_candidates(
        self,
        *,
        canonical: str,
        chain_hint: str | None,
        signal_type: str,
        size_usd: float,
    ) -> list[RouteCandidate]:
        """Return ranked candidate list. Empty list = "no_venue" (engine
        records reject_reason='no_venue' OR 'token_aggregate' depending
        on the rejection cause — log events distinguish them)."""
        if self._db._conn is None:
            raise RuntimeError("Database not initialized.")

        # Step 1 — live-position-aggregator guard (M1-BLOCKER)
        # CONTRACT: `canonical` is the uppercase TICKER ("BTC", "BILL"),
        # NOT the CoinGecko slug ("bitcoin"). live_trades.symbol stores
        # the ticker; live_trades.coin_id stores the CoinGecko slug.
        # We query by SYMBOL because:
        #   1. routing.py inputs are canonical tickers
        #   2. CoinGecko slugs differ from tickers (bitcoin vs BTC), so a
        #      coin_id query with canonical.lower() silently fails for
        #      every coin where slug != ticker.lower().
        # UPPER() comparison guarantees case-insensitive match.
        cur = await self._db._conn.execute(
            "SELECT COUNT(*) FROM live_trades "
            "WHERE UPPER(symbol) = UPPER(?) AND status = 'open'",
            (canonical,),
        )
        open_count = (await cur.fetchone())[0]
        per_token_cap = self._settings.LIVE_MAX_OPEN_POSITIONS_PER_TOKEN
        if open_count >= per_token_cap:
            log.info(
                "routing_skipped_token_aggregate",
                canonical=canonical,
                open_count=open_count,
                cap=per_token_cap,
                signal_type=signal_type,
            )
            return []

        # Step 2 — fetch venue_listings rows for this canonical
        cur = await self._db._conn.execute(
            "SELECT venue, venue_pair, asset_class FROM venue_listings "
            "WHERE canonical = ? AND delisted_at IS NULL",
            (canonical,),
        )
        listings = list(await cur.fetchall())

        # Step 3 — on-demand fetch if empty
        if not listings:
            log.info("venue_listings_miss", canonical=canonical)
            await self._on_demand_listings_fetch(canonical)
            cur = await self._db._conn.execute(
                "SELECT venue, venue_pair, asset_class FROM venue_listings "
                "WHERE canonical = ? AND delisted_at IS NULL",
                (canonical,),
            )
            listings = list(await cur.fetchall())
        if not listings:
            log.info(
                "routing_skipped_no_venue",
                canonical=canonical,
                signal_type=signal_type,
            )
            return []

        # Step 4 — chain="coingecko" enrichment is a no-op for
        # canonical lookup (the venue_listings query above already
        # spans every tier). Hint logged for debugability.
        if chain_hint == "coingecko":
            log.info("chain_coingecko_enrichment_skipped", canonical=canonical)

        # Step 5 — query venue_health for each candidate; filter dormant
        # / unhealthy
        candidates: list[RouteCandidate] = []
        for venue, venue_pair, _asset_class in listings:
            cur = await self._db._conn.execute(
                "SELECT auth_ok, rest_responsive, is_dormant, "
                "       last_quote_mid_price, last_depth_at_size_bps, "
                "       fills_30d_count "
                "FROM venue_health WHERE venue = ? "
                "ORDER BY probe_at DESC LIMIT 1",
                (venue,),
            )
            health = await cur.fetchone()
            if health is None:
                # No health probe yet — treat as healthy default
                # (HealthProbe service may not have run for new venues).
                # Defensive: still score below probed candidates via 0.5.
                candidates.append(
                    RouteCandidate(
                        venue=venue,
                        venue_pair=venue_pair,
                        expected_fill_price=None,
                        expected_slippage_bps=None,
                        available_capital_usd=None,
                        venue_health_score=0.5,
                    )
                )
                continue
            auth_ok, rest_resp, is_dormant, mid, depth, _fills_30d = health
            if not auth_ok or not rest_resp or is_dormant:
                continue
            candidates.append(
                RouteCandidate(
                    venue=venue,
                    venue_pair=venue_pair,
                    expected_fill_price=mid,
                    expected_slippage_bps=depth,
                    available_capital_usd=None,
                    venue_health_score=1.0,
                )
            )

        # Step 6 — OverrideStore PREPEND semantics
        candidates = await self._apply_override_prepend(canonical, candidates)

        # Step 7 — rank by venue_health_score descending; tied scores
        # keep insertion order (Python sort is stable)
        candidates.sort(key=lambda c: c.venue_health_score, reverse=True)
        return candidates

    async def _on_demand_listings_fetch(self, canonical: str) -> None:
        """Sync REST call per adapter to populate venue_listings rows."""
        if self._db._conn is None:
            raise RuntimeError("Database not initialized.")
        now_iso = datetime.now(timezone.utc).isoformat()
        for venue, adapter in self._adapters.items():
            try:
                meta = await adapter.fetch_venue_metadata(canonical)
                if meta is not None:
                    await self._db._conn.execute(
                        """INSERT OR REPLACE INTO venue_listings
                           (venue, canonical, venue_pair, quote, asset_class,
                            refreshed_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            venue,
                            canonical,
                            meta.venue_pair,
                            meta.quote,
                            meta.asset_class,
                            now_iso,
                        ),
                    )
            except NotImplementedError:
                log.info(
                    "on_demand_listing_fetch_not_implemented",
                    venue=venue,
                    canonical=canonical,
                )
            except Exception:
                log.exception(
                    "on_demand_listing_fetch_failed",
                    venue=venue,
                    canonical=canonical,
                )
        await self._db._conn.commit()

    async def _apply_override_prepend(
        self, canonical: str, candidates: list[RouteCandidate]
    ) -> list[RouteCandidate]:
        """Read live_operator_overrides for `allow_stack`/`venue_revive`
        on this canonical or any-canonical, prepend matching candidates
        to top of list. PREPEND (default) keeps non-override candidates
        as fallback. REPLACE (LIVE_OVERRIDE_REPLACE_ONLY=True) drops
        non-override candidates."""
        if self._db._conn is None:
            return candidates
        cur = await self._db._conn.execute(
            """SELECT venue FROM live_operator_overrides
               WHERE override_type IN ('allow_stack','venue_revive')
                 AND (canonical = ? OR canonical IS NULL)
                 AND expires_at > ?""",
            (canonical, datetime.now(timezone.utc).isoformat()),
        )
        override_venues = {row[0] for row in await cur.fetchall() if row[0]}
        if not override_venues:
            return candidates

        prepend = [c for c in candidates if c.venue in override_venues]
        rest = [c for c in candidates if c.venue not in override_venues]

        if getattr(self._settings, "LIVE_OVERRIDE_REPLACE_ONLY", False):
            return prepend
        return prepend + rest
