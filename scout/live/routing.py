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
from scout.live.capabilities import VenueCapabilities

log = structlog.get_logger(__name__)

# ONE query, used at both call sites in `get_candidates` — the initial read and
# the re-read after the on-demand metadata fetch. They were two copies, and when
# the column list was widened only the first was updated: the second returned 3
# columns into a 4-tuple unpack, so the FIRST-EVER signal on any token raised
# ValueError out of routing, past `_dispatch_live`'s NoRoutableVenue handler, and
# produced no reject row and no log. A constant makes that divergence unstateable.
#
# *** `delisted_at IS NULL` FILTERS ON A COLUMN NOTHING EVER WRITES. ***
# Verified 2026-08-02: `delisted_at` is declared in `venue_listings` (db.py) and
# READ here, and there is no writer anywhere in the repository — not in scout/,
# not in scripts/, not in any migration. The module docstring below still
# advertises a "delisting fallback: re-evaluates on adapter reject with
# 'delisted'"; that fallback does not exist.
#
# So this clause currently excludes nothing, and the protection it appears to
# provide is not being provided. It is left in place because it is the correct
# predicate the moment a writer exists, and because removing it would make the
# gap harder to find, not easier. Recorded rather than quietly relied upon:
# a filter that reads as a safety check and matches no rows is the shape that
# gets trusted in an incident.
#
# *** WHOEVER ADDS THAT WRITER MUST ADD A CLEARER IN THE SAME CHANGE. ***
# `_on_demand_listings_fetch` deliberately PRESERVES `delisted_at` (it used to
# erase it, which resurrected delisted venues). Nothing else sets it back to
# NULL. So the moment delisting becomes writable, it also becomes ONE-WAY: a
# venue that re-lists the pair can never be routed again, because the fetch that
# would rediscover it cannot clear the flag.
#
# Two things go wrong then, and the second is the less obvious one:
#   1. Permanent invisibility. Under the old REPLACE this self-healed — wrongly,
#      but it healed. Fixing the erasure removed the only mechanism that ever
#      cleared the flag without adding a replacement.
#   2. A freshness signal that lies. `if not listings` stays true forever for
#      that canonical, so the on-demand REST fetch re-runs on EVERY signal and
#      bumps `refreshed_at` each time. The row looks continuously fresh to any
#      staleness check while routing can never see it, and the venue takes an
#      unbounded repeated metadata fetch. A healthy-looking indicator over a dead
#      path — the §12a shape.
#
# The replacement is a deliberate clear, not a silent one: set `delisted_at`
# back to NULL in the DO UPDATE when the venue's own `fetch_venue_metadata`
# reports the pair tradable again, and LOG it. That is a re-listing decision
# based on the venue's answer, which is what the old REPLACE only ever did by
# accident. Alternatively, skip the fetch entirely for canonicals whose only
# rows are delisted, which removes both the repeated call and the false
# freshness. Either is fine; doing neither is not.
_LISTINGS_SQL = (
    "SELECT venue, venue_pair, asset_class, quote FROM venue_listings "
    "WHERE canonical = ? AND delisted_at IS NULL"
)


@dataclass(frozen=True)
class RouteCandidate:
    venue: str
    venue_pair: str
    expected_fill_price: float | None
    expected_slippage_bps: float | None
    available_capital_usd: float | None
    venue_health_score: float
    # Trailing + defaulted so every existing construction site is unaffected. Both
    # come straight from the `venue_listings` row and are needed to mint a correct
    # TradeIntent: `quote` is a hashed term (the instrument is BTC/USDT, not "BTC"),
    # and `asset_class` decides whether the listing is even a spot instrument.
    quote: str | None = None
    asset_class: str | None = None


@dataclass(frozen=True)
class RoutedVenue:
    """A candidate that survived the capability gate, WITH the adapter that will
    execute it.

    *** THE ADAPTER TRAVELS WITH THE VENUE, AND THAT IS THE WHOLE POINT. ***
    Before this type existed, ``engine._dispatch_live`` chose ``top = candidates[0]``
    and then submitted through ``self._adapter`` — the single adapter injected at
    construction. Routing looked like the lever that selected a venue; the
    constructor argument was the actual control. Selecting a non-Binance venue placed
    a Binance order carrying the other venue's pair string.

    Returning the adapter alongside the candidate makes the two impossible to
    disagree: there is no second place for a caller to get an adapter from.
    """

    candidate: RouteCandidate
    adapter: ExchangeAdapter
    capabilities: VenueCapabilities

    @property
    def venue(self) -> str:
        return self.candidate.venue

    @property
    def venue_pair(self) -> str:
        return self.candidate.venue_pair


class NoRoutableVenue(Exception):
    """No candidate could execute this order shape.

    ``reject_reason`` is one of the ``live_trades.reject_reason`` CHECK values so the
    caller can persist it without a translation table. ``rejections`` carries the
    per-venue detail — an operator asking "why did nothing route?" needs to see that
    Kraken was dropped for market orders and Coinbase for having no adapter, not a
    single flat ``no_venue``.
    """

    def __init__(
        self, reject_reason: str, rejections: tuple[tuple[str, str], ...]
    ) -> None:
        detail = "; ".join(f"{venue}: {why}" for venue, why in rejections) or "none"
        super().__init__(f"no routable venue ({reject_reason}) — {detail}")
        self.reject_reason = reject_reason
        self.rejections = rejections


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
        cur = await self._db._conn.execute(_LISTINGS_SQL, (canonical,))
        listings = list(await cur.fetchall())

        # Step 3 — on-demand fetch if empty
        if not listings:
            log.info("venue_listings_miss", canonical=canonical)
            await self._on_demand_listings_fetch(canonical)
            cur = await self._db._conn.execute(_LISTINGS_SQL, (canonical,))
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
        for venue, venue_pair, asset_class, quote in listings:
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
                        quote=quote,
                        asset_class=asset_class,
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
                    quote=quote,
                    asset_class=asset_class,
                )
            )

        # Step 6 — OverrideStore PREPEND semantics
        candidates = await self._apply_override_prepend(canonical, candidates)

        # Step 7 — rank by venue_health_score descending; tied scores
        # keep insertion order (Python sort is stable)
        candidates.sort(key=lambda c: c.venue_health_score, reverse=True)
        return candidates

    async def select_route(
        self,
        *,
        canonical: str,
        chain_hint: str | None,
        signal_type: str,
        size_usd: float,
        venue_family: str,
        order_type: str,
        reduce_only: bool,
    ) -> RoutedVenue:
        """Rank candidates, then return the first one that can actually execute this.

        Two gates, in this order, applied to each candidate by rank:

        1. **An adapter must exist for the venue.** ``venue_listings`` rows are
           populated by per-venue services and by CoinGecko metadata; nothing
           guarantees the process has a connector for every venue it can name. A
           candidate with no adapter is not a route.
        2. **The adapter must DECLARE the capability.** ``permits_order`` is
           fail-closed — an adapter that forgot to override
           ``describe_capabilities`` declares nothing and is refused, rather than
           being assumed universal.

        Raises :class:`NoRoutableVenue` when nothing survives, carrying the reason
        each candidate was dropped.

        Takes the order SHAPE rather than a ``TradeIntent`` on purpose. The intent's
        hashed terms include ``preferred_venue``, so an intent cannot be minted until
        the venue is known — selection has to run first, and a function that took an
        intent would force callers to mint a throwaway one with a placeholder venue.
        """
        candidates = await self.get_candidates(
            canonical=canonical,
            chain_hint=chain_hint,
            signal_type=signal_type,
            size_usd=size_usd,
        )
        if not candidates:
            raise NoRoutableVenue("no_venue", ())

        rejections: list[tuple[str, str]] = []
        for candidate in candidates:
            # The listing must describe a SPOT instrument. `venue_listings` also holds
            # perp / option / equity / forex rows, and a market order shaped for spot
            # placed against a perp listing is a different trade with different risk.
            # `TradeIntent.instrument_type` accepts only "spot", so this is the gate
            # that stops a non-spot listing reaching a spot-only intent.
            if candidate.asset_class not in (None, "spot"):
                rejections.append(
                    (
                        candidate.venue,
                        f"listing is asset_class={candidate.asset_class!r}; this path "
                        "places spot orders only",
                    )
                )
                continue

            # The quote asset is a hashed term of the intent — the instrument is
            # BTC/USDT, not "BTC". Without it there is no correct intent to mint, and
            # guessing one would bind the order to an instrument nobody chose.
            if not candidate.quote:
                rejections.append(
                    (
                        candidate.venue,
                        "venue_listings row carries no quote asset; the intent's "
                        "instrument cannot be determined",
                    )
                )
                continue

            adapter = self._adapters.get(candidate.venue)
            if adapter is None:
                rejections.append(
                    (
                        candidate.venue,
                        "no adapter is wired for this venue in this process",
                    )
                )
                continue

            caps = adapter.describe_capabilities()

            # The adapter map key and the descriptor's own venue name must agree.
            # They are used for different things downstream — the key selects the
            # client-order-id form, the descriptor drives the capability answer — so a
            # disagreement means one of those two decisions is being made about a
            # different venue than the other. Refuse rather than pick a side.
            if caps.venue != candidate.venue:
                rejections.append(
                    (
                        candidate.venue,
                        f"adapter registered under {candidate.venue!r} declares "
                        f"itself {caps.venue!r}; the adapter map is inconsistent",
                    )
                )
                continue

            permitted, why = caps.permits_order(
                venue_family=venue_family,
                order_type=order_type,
                reduce_only=reduce_only,
            )
            if not permitted:
                rejections.append((candidate.venue, why or "capability refused"))
                continue

            log.info(
                "routing_venue_selected",
                canonical=canonical,
                venue=candidate.venue,
                venue_pair=candidate.venue_pair,
                order_type=order_type,
                rejected_ahead_of_it=len(rejections),
            )
            return RoutedVenue(candidate=candidate, adapter=adapter, capabilities=caps)

        # Every candidate was dropped. Report the reason of the HIGHEST-RANKED one:
        # it is the venue the operator expected to trade on, so it is the refusal
        # that answers "why didn't this go through?".
        top_reason = rejections[0][1] if rejections else ""
        reject_reason = (
            "no_adapter_for_venue"
            if "no adapter" in top_reason
            else "venue_capability_refused"
        )
        log.warning(
            "routing_all_candidates_refused",
            canonical=canonical,
            reject_reason=reject_reason,
            rejections=rejections,
        )
        raise NoRoutableVenue(reject_reason, tuple(rejections))

    async def _on_demand_listings_fetch(self, canonical: str) -> None:
        """Sync REST call per adapter to populate venue_listings rows.

        *** UPSERT, NEVER `INSERT OR REPLACE`. ***
        REPLACE deletes the conflicting row and inserts a fresh one, so every column
        NOT in the statement reverts to its default. ``venue_listings.delisted_at``
        is not in this statement and never could be — a metadata fetch has no opinion
        about delisting — so REPLACE silently ERASES the delisting marker.

        And the erasure lands exactly where it does damage. ``_LISTINGS_SQL`` filters
        `delisted_at IS NULL`, so a token whose only listing is delisted is precisely
        the token that reaches this fetch at all: the resurrection path is reachable
        only in the case that resurrects a delisted venue and then routes an order to
        it. Naming the columns in a DO UPDATE leaves everything else — including the
        delisting — untouched.

        This is the project's recorded UPSERT lesson, not a new discovery. It has
        been latent since May only because the caller crashed before reaching it;
        repairing that crash is what made this live, so it is fixed alongside.
        """
        if self._db._conn is None:
            raise RuntimeError("Database not initialized.")
        now_iso = datetime.now(timezone.utc).isoformat()
        for venue, adapter in self._adapters.items():
            try:
                meta = await adapter.fetch_venue_metadata(canonical)
                if meta is not None:
                    await self._db._conn.execute(
                        """INSERT INTO venue_listings
                           (venue, canonical, venue_pair, quote, asset_class,
                            refreshed_at)
                           VALUES (?, ?, ?, ?, ?, ?)
                           ON CONFLICT(venue, canonical, asset_class) DO UPDATE SET
                               venue_pair   = excluded.venue_pair,
                               quote        = excluded.quote,
                               refreshed_at = excluded.refreshed_at""",
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
