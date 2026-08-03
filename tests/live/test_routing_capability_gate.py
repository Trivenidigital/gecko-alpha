"""The capability gate in front of route selection.

Covers the §9c defect directly: routing chose a venue and the order went to the
adapter injected at construction, so a non-Binance selection placed a Binance
order carrying the other venue's pair string. `select_route` returns the adapter
WITH the candidate, so there is no second place to get one from.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest

from scout.config import Settings
from scout.db import Database
from scout.live.adapter_base import ExchangeAdapter, VenueMetadata
from scout.live.capabilities import VenueCapabilities
from scout.live.routing import NoRoutableVenue, RoutingLayer


class _Adapter(ExchangeAdapter):
    """Metadata-only adapter. It cannot place an order at all, which is correct:
    these tests are about SELECTION and must not be able to reach a venue."""

    def __init__(self, venue: str, caps: VenueCapabilities) -> None:
        self.venue_name = venue
        self._caps = caps

    def describe_capabilities(self) -> VenueCapabilities:
        return self._caps

    async def fetch_exchange_info_row(self, pair):
        return None

    async def resolve_pair_for_symbol(self, symbol):
        return None

    async def fetch_depth(self, pair, limit: int = 100):
        raise NotImplementedError

    async def fetch_price(self, pair) -> Decimal:
        raise NotImplementedError

    async def send_order(self, *, pair, side, size_usd) -> dict:
        raise AssertionError("selection must not reach a venue")

    async def fetch_venue_metadata(self, canonical) -> VenueMetadata | None:
        return None

    async def place_order_request(self, request) -> str:
        raise AssertionError("selection must not reach a venue")

    async def await_fill_confirmation(self, **kw):
        raise AssertionError("selection must not reach a venue")

    async def fetch_account_balance(self, asset: str = "USDT") -> float:
        return 0.0


_BINANCE_CAPS = VenueCapabilities(
    venue="binance",
    venue_family="cex",
    supports_market_orders=True,
    supports_client_order_id=True,
)
# Kraken as the real adapter declares itself: limit-only, no market orders,
# because `send_order` / `place_order_request` / `place_exit_order` all raise and
# only `place_limit_order` reaches AddOrder.
_KRAKEN_CAPS = VenueCapabilities(
    venue="kraken",
    venue_family="cex",
    supports_limit_orders=True,
    supports_cancel=True,
    supports_client_order_id=True,
)


def _settings() -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        LIVE_MAX_OPEN_POSITIONS_PER_TOKEN=5,
    )


async def _db_with_listings(tmp_path, listings):
    """`listings` is a list of (venue, venue_pair, quote, asset_class)."""
    db = Database(str(tmp_path / "r.db"))
    await db.initialize()
    for venue, pair, quote, asset_class in listings:
        await db._conn.execute(
            "INSERT INTO venue_listings (venue, canonical, venue_pair, quote, "
            "asset_class, refreshed_at) VALUES (?, 'SYM', ?, ?, ?, "
            "'2026-08-02T00:00:00Z')",
            (venue, pair, quote, asset_class),
        )
    await db._conn.commit()
    return db


def _routing(db, adapters):
    return RoutingLayer(db=db, settings=_settings(), adapters=adapters)


async def _select(routing, **overrides):
    kw = dict(
        canonical="SYM",
        chain_hint=None,
        signal_type="first_signal",
        size_usd=100.0,
        venue_family="cex",
        order_type="market",
        reduce_only=False,
    )
    kw.update(overrides)
    return await routing.select_route(**kw)


class TestTheAdapterTravelsWithTheVenue:
    async def test_the_returned_adapter_is_the_one_for_the_selected_venue(
        self, tmp_path
    ):
        """*** The §9c defect, asserted directly. ***

        Kraken ranks first and is refused (limit-only), so binance is selected —
        and the adapter that comes back must be binance's, not whichever one a
        caller happened to hold.
        """
        db = await _db_with_listings(
            tmp_path,
            [
                ("kraken", "XBTUSD", "USD", "spot"),
                ("binance", "SYMUSDT", "USDT", "spot"),
            ],
        )
        try:
            binance = _Adapter("binance", _BINANCE_CAPS)
            kraken = _Adapter("kraken", _KRAKEN_CAPS)
            routed = await _select(_routing(db, {"binance": binance, "kraken": kraken}))
            assert routed.venue == "binance"
            assert routed.adapter is binance
            assert routed.venue_pair == "SYMUSDT"
        finally:
            await db.close()

    async def test_a_limit_only_venue_is_refused_a_market_order(self, tmp_path):
        db = await _db_with_listings(tmp_path, [("kraken", "XBTUSD", "USD", "spot")])
        try:
            routing = _routing(db, {"kraken": _Adapter("kraken", _KRAKEN_CAPS)})
            with pytest.raises(NoRoutableVenue) as exc:
                await _select(routing)
            assert exc.value.reject_reason == "venue_capability_refused"
            assert "does not declare market orders" in exc.value.rejections[0][1]
        finally:
            await db.close()

    async def test_the_same_venue_is_accepted_for_the_order_type_it_declares(
        self, tmp_path
    ):
        """Guard on the guard: Kraken is not refused for being Kraken."""
        db = await _db_with_listings(tmp_path, [("kraken", "XBTUSD", "USD", "spot")])
        try:
            routing = _routing(db, {"kraken": _Adapter("kraken", _KRAKEN_CAPS)})
            routed = await _select(routing, order_type="limit")
            assert routed.venue == "kraken"
        finally:
            await db.close()


class TestFailClosed:
    async def test_a_venue_with_no_adapter_is_not_a_route(self, tmp_path):
        """`venue_listings` is populated by metadata services; nothing guarantees
        this process has a connector for every venue it can name."""
        db = await _db_with_listings(tmp_path, [("coinbase", "BTC-USD", "USD", "spot")])
        try:
            with pytest.raises(NoRoutableVenue) as exc:
                await _select(_routing(db, {}))
            assert exc.value.reject_reason == "no_adapter_for_venue"
        finally:
            await db.close()

    async def test_an_adapter_that_declares_nothing_is_refused(self, tmp_path):
        """An adapter that forgot to override `describe_capabilities` gets a
        descriptor permitting nothing — it must not be treated as universal."""
        db = await _db_with_listings(tmp_path, [("binance", "SYMUSDT", "USDT", "spot")])
        try:
            silent = _Adapter("binance", VenueCapabilities(venue="binance"))
            with pytest.raises(NoRoutableVenue) as exc:
                await _select(_routing(db, {"binance": silent}))
            assert "does not declare a venue_family" in exc.value.rejections[0][1]
        finally:
            await db.close()

    async def test_a_dex_intent_is_refused_by_a_cex_descriptor(self, tmp_path):
        db = await _db_with_listings(tmp_path, [("binance", "SYMUSDT", "USDT", "spot")])
        try:
            routing = _routing(db, {"binance": _Adapter("binance", _BINANCE_CAPS)})
            with pytest.raises(NoRoutableVenue) as exc:
                await _select(routing, venue_family="dex")
            assert "intent requires dex" in exc.value.rejections[0][1]
        finally:
            await db.close()

    async def test_a_reduce_only_order_is_refused_where_it_is_not_declared(
        self, tmp_path
    ):
        """Dropping reduce_only silently turns a bounded exit into a plain sell.
        Binance does not declare it — `place_exit_order` fires a bare MARKET SELL
        on a caller-supplied quantity with no clamp."""
        db = await _db_with_listings(tmp_path, [("binance", "SYMUSDT", "USDT", "spot")])
        try:
            routing = _routing(db, {"binance": _Adapter("binance", _BINANCE_CAPS)})
            with pytest.raises(NoRoutableVenue) as exc:
                await _select(routing, reduce_only=True)
            assert "does not declare reduce_only" in exc.value.rejections[0][1]
        finally:
            await db.close()

    async def test_a_non_spot_listing_is_refused(self, tmp_path):
        """A market order shaped for spot placed against a perp listing is a
        different trade with different risk. `TradeIntent.instrument_type` accepts
        only 'spot', so this is where the perp row has to stop."""
        db = await _db_with_listings(tmp_path, [("binance", "SYMUSDT", "USDT", "perp")])
        try:
            routing = _routing(db, {"binance": _Adapter("binance", _BINANCE_CAPS)})
            with pytest.raises(NoRoutableVenue) as exc:
                await _select(routing)
            assert "asset_class='perp'" in exc.value.rejections[0][1]
        finally:
            await db.close()

    async def test_a_listing_with_no_quote_asset_is_refused(self, tmp_path):
        """The quote is a hashed term of the intent — the instrument is BTC/USDT,
        not 'BTC'. Guessing one would bind the order to an instrument nobody
        chose."""
        db = await _db_with_listings(tmp_path, [("binance", "SYMUSDT", "", "spot")])
        try:
            routing = _routing(db, {"binance": _Adapter("binance", _BINANCE_CAPS)})
            with pytest.raises(NoRoutableVenue) as exc:
                await _select(routing)
            assert "no quote asset" in exc.value.rejections[0][1]
        finally:
            await db.close()

    async def test_an_adapter_registered_under_the_wrong_key_is_refused(self, tmp_path):
        """The map key selects the client-order-id form; the descriptor drives the
        capability answer. A disagreement means those two decisions are being made
        about different venues — refuse rather than pick a side."""
        db = await _db_with_listings(tmp_path, [("binance", "SYMUSDT", "USDT", "spot")])
        try:
            wrong = _Adapter("binance", _KRAKEN_CAPS)  # descriptor says "kraken"
            with pytest.raises(NoRoutableVenue) as exc:
                await _select(_routing(db, {"binance": wrong}))
            assert "adapter map is inconsistent" in exc.value.rejections[0][1]
        finally:
            await db.close()

    async def test_no_listings_at_all_reports_no_venue(self, tmp_path):
        db = await _db_with_listings(tmp_path, [])
        try:
            with pytest.raises(NoRoutableVenue) as exc:
                await _select(_routing(db, {}))
            assert exc.value.reject_reason == "no_venue"
        finally:
            await db.close()

    async def test_every_rejection_reason_is_reported_not_just_the_first(
        self, tmp_path
    ):
        """An operator asking 'why did nothing route?' needs the per-venue detail,
        not one flat `no_venue`."""
        db = await _db_with_listings(
            tmp_path,
            [
                ("kraken", "XBTUSD", "USD", "spot"),
                ("coinbase", "BTC-USD", "USD", "spot"),
            ],
        )
        try:
            routing = _routing(db, {"kraken": _Adapter("kraken", _KRAKEN_CAPS)})
            with pytest.raises(NoRoutableVenue) as exc:
                await _select(routing)
            venues = {venue for venue, _ in exc.value.rejections}
            assert venues == {"kraken", "coinbase"}
        finally:
            await db.close()


class TestTheOnDemandListingsPath:
    """*** THE FIRST-EVER SIGNAL ON A TOKEN TAKES A DIFFERENT QUERY. ***

    `get_candidates` reads `venue_listings`, and when that comes back empty it
    fetches metadata from the adapters and RE-READS. The two reads were separate
    string literals; widening the column list updated only the first, so the
    re-read returned 3 columns into a 4-tuple unpack. Every first-ever signal on a
    token raised ValueError out of routing — past `_dispatch_live`'s
    NoRoutableVenue handler, so no reject row, no metric, and nothing in the log
    for an operator auditing refusals to find.

    No test covered this path before; it is the one branch a fixture that seeds
    `venue_listings` upfront can never reach.
    """

    async def test_a_first_ever_signal_routes_through_the_on_demand_fetch(
        self, tmp_path
    ):
        from scout.live.adapter_base import VenueMetadata as _VM

        db = await _db_with_listings(tmp_path, [])  # deliberately EMPTY

        class _FetchingAdapter(_Adapter):
            async def fetch_venue_metadata(self, canonical):
                return _VM(
                    venue="binance",
                    canonical=canonical,
                    venue_pair=f"{canonical}USDT",
                    quote="USDT",
                    asset_class="spot",
                    min_size=None,
                    tick_size=None,
                    lot_size=None,
                )

        try:
            adapter = _FetchingAdapter("binance", _BINANCE_CAPS)
            routed = await _select(_routing(db, {"binance": adapter}))
            assert routed.venue == "binance"
            assert routed.venue_pair == "SYMUSDT"
            # The quote came back from the re-read, not from a default — an intent
            # cannot be minted without it.
            assert routed.candidate.quote == "USDT"
            assert routed.candidate.asset_class == "spot"
        finally:
            await db.close()

    async def test_the_on_demand_fetch_does_not_resurrect_a_delisted_listing(
        self, tmp_path
    ):
        """*** THE RESURRECTION PATH IS REACHABLE ONLY WHERE IT DOES DAMAGE. ***

        `INSERT OR REPLACE` deletes the conflicting row and inserts a fresh one, so
        `delisted_at` — a column no metadata fetch has an opinion about — reverts to
        NULL. And `_LISTINGS_SQL` filters `delisted_at IS NULL`, so a token whose
        only listing is DELISTED is exactly the token that falls through to the
        on-demand fetch. The erasure therefore happens precisely in the case where
        it un-delists a venue and then routes an order to it.

        Latent since May only because the caller crashed at the 4-tuple unpack
        before reaching this; repairing that crash is what made it live.
        """
        from scout.live.adapter_base import VenueMetadata as _VM

        db = await _db_with_listings(tmp_path, [])
        await db._conn.execute(
            "INSERT INTO venue_listings (venue, canonical, venue_pair, quote, "
            "asset_class, refreshed_at, delisted_at) VALUES ('binance','SYM',"
            "'SYMUSDT','USDT','spot','2026-06-01T00:00:00Z','2026-07-01T00:00:00Z')"
        )
        await db._conn.commit()

        class _FetchingAdapter(_Adapter):
            async def fetch_venue_metadata(self, canonical):
                return _VM(
                    venue="binance",
                    canonical=canonical,
                    venue_pair=f"{canonical}USDT",
                    quote="USDT",
                    asset_class="spot",
                    min_size=None,
                    tick_size=None,
                    lot_size=None,
                )

        try:
            with pytest.raises(NoRoutableVenue):
                await _select(
                    _routing(
                        db, {"binance": _FetchingAdapter("binance", _BINANCE_CAPS)}
                    )
                )
            cur = await db._conn.execute(
                "SELECT delisted_at FROM venue_listings WHERE venue='binance'"
            )
            assert (await cur.fetchone())[
                0
            ] == "2026-07-01T00:00:00Z", (
                "the on-demand fetch erased the delisting marker"
            )
        finally:
            await db.close()

    async def test_the_on_demand_fetch_still_refreshes_a_live_listing(self, tmp_path):
        """Guard on the guard: the UPSERT must still update what it is for."""
        from scout.live.adapter_base import VenueMetadata as _VM

        db = await _db_with_listings(tmp_path, [])
        await db._conn.execute(
            "INSERT INTO venue_listings (venue, canonical, venue_pair, quote, "
            "asset_class, refreshed_at) VALUES ('binance','SYM','STALE','USD',"
            "'spot','2026-06-01T00:00:00Z')"
        )
        await db._conn.commit()

        class _FetchingAdapter(_Adapter):
            async def fetch_venue_metadata(self, canonical):
                return _VM(
                    venue="binance",
                    canonical=canonical,
                    venue_pair="SYMUSDT",
                    quote="USDT",
                    asset_class="spot",
                    min_size=None,
                    tick_size=None,
                    lot_size=None,
                )

        try:
            # A live listing is found by the FIRST read, so drive the fetch directly.
            await _routing(
                db, {"binance": _FetchingAdapter("binance", _BINANCE_CAPS)}
            )._on_demand_listings_fetch("SYM")
            cur = await db._conn.execute(
                "SELECT venue_pair, quote, refreshed_at FROM venue_listings "
                "WHERE venue='binance'"
            )
            pair, quote, refreshed = tuple(await cur.fetchone())
            assert (pair, quote) == ("SYMUSDT", "USDT")
            assert refreshed != "2026-06-01T00:00:00Z"
        finally:
            await db.close()

    async def test_the_two_listing_reads_are_the_same_query(self):
        """Structural guard on the fix: the constant exists and is used at both
        sites, so the column lists cannot drift apart again."""
        import ast
        import inspect

        from scout.live import routing as routing_mod

        src = inspect.getsource(routing_mod.RoutingLayer.get_candidates)
        tree = ast.parse(inspect.cleandoc(src))
        literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "venue_listings" in node.value
            and "SELECT" in node.value.upper()
        ]
        assert literals == [], (
            "get_candidates inlines a venue_listings SELECT again: "
            f"{literals}. Use _LISTINGS_SQL so the two reads cannot diverge."
        )
        assert "quote" in routing_mod._LISTINGS_SQL


class TestBackwardCompatibility:
    async def test_get_candidates_still_returns_ungated_candidates(self, tmp_path):
        """`select_route` is additive. The existing `get_candidates` contract —
        rank everything listed, gate nothing — is what the M1 tests and the
        override-prepend behaviour depend on."""
        db = await _db_with_listings(tmp_path, [("kraken", "XBTUSD", "USD", "spot")])
        try:
            candidates = await _routing(db, {}).get_candidates(
                canonical="SYM",
                chain_hint=None,
                signal_type="first_signal",
                size_usd=100.0,
            )
            assert [c.venue for c in candidates] == ["kraken"]
            assert candidates[0].quote == "USD"
            assert candidates[0].asset_class == "spot"
        finally:
            await db.close()


class TestTheDelistingFilterHasNoWriter:
    """*** A SAFETY FILTER THAT MATCHES NO ROWS IS NOT PROVIDING SAFETY. ***

    `_LISTINGS_SQL` excludes `delisted_at IS NOT NULL`, and `venue_listings`
    declares the column — but nothing in the repository ever writes it, while
    `RoutingLayer`'s module docstring advertises a "delisting fallback:
    re-evaluates on adapter reject with 'delisted'".

    So the clause excludes nothing today. That is recorded here as an assertion
    rather than left as a comment, so the day a writer appears this test fails and
    someone has to decide deliberately whether the fallback is now real.
    """

    def test_nothing_in_the_repository_writes_delisted_at(self):
        """Scans SQL STRING LITERALS via the AST, not raw file text.

        The first draft used a file-wide regex with `[^;]*` between the verb and
        the column. Python has no statement semicolons, so that span swallowed the
        whole file — and the first thing it matched was the explanatory COMMENT in
        `routing.py` describing this very gap. A prose mention of a column is not a
        write to it; only a literal that is itself SQL can be.
        """
        import ast
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        # Applied per string literal, where a statement really is one statement.
        writer = re.compile(
            r"UPDATE\s+venue_listings\b.*\bdelisted_at\s*="
            r"|INSERT\b.*\bvenue_listings\s*\([^)]*\bdelisted_at\b",
            re.IGNORECASE | re.DOTALL,
        )
        offenders = []
        sources = list(root.glob("scout/**/*.py")) + list(root.glob("scripts/**/*.py"))
        for path in sources:
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text("utf-8", errors="ignore"))
            except SyntaxError:
                continue
            literals = [
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            ]
            # A docstring is a string literal too, so require the literal to look
            # like a statement rather than merely to contain the words.
            if any(writer.search(lit) for lit in literals):
                offenders.append(str(path.relative_to(root)))
        assert offenders == [], (
            f"{offenders} now write venue_listings.delisted_at. The delisting "
            "filter in _LISTINGS_SQL has become live — re-read the UPSERT in "
            "_on_demand_listings_fetch, which deliberately PRESERVES delisted_at "
            "and therefore now needs a way for a re-listed venue to be cleared."
        )

    async def test_a_delisted_listing_is_excluded_when_one_exists(self, tmp_path):
        """The predicate itself is correct — it is only the writer that is
        missing. Asserted by writing the column by hand, which is the one place
        in the tree that does."""
        db = await _db_with_listings(tmp_path, [])
        await db._conn.execute(
            "INSERT INTO venue_listings (venue, canonical, venue_pair, quote, "
            "asset_class, refreshed_at, delisted_at) VALUES ('binance','SYM',"
            "'SYMUSDT','USDT','spot','2026-06-01T00:00:00Z','2026-07-01T00:00:00Z')"
        )
        await db._conn.commit()
        try:
            candidates = await _routing(db, {}).get_candidates(
                canonical="SYM",
                chain_hint=None,
                signal_type="first_signal",
                size_usd=100.0,
            )
            assert candidates == []
        finally:
            await db.close()


class TestZeroExAdapterThroughTheRealRouter:
    """*** THE CAPABILITY MUST BE REACHABLE THROUGH THE ROUTER, NOT ONLY ITS
    OWN TESTS. ***

    An adapter with zero production importers is an adapter the system does not
    have. These drive the REAL `RoutingLayer.select_route` against the 0x
    adapter, and assert both that a dex intent reaches it and that the
    signal-driven cex path cannot.
    """

    def _zeroex(self):
        from scout.live.evm.adapter import ZeroExAllowanceHolderAdapter

        return ZeroExAllowanceHolderAdapter(chain_id=1)

    async def test_a_dex_intent_routes_to_the_zeroex_adapter(self, tmp_path):
        adapter = self._zeroex()
        db = await _db_with_listings(
            tmp_path, [(adapter.venue_name, "WETH-USDC", "USDC", "spot")]
        )
        try:
            routed = await _select(
                _routing(db, {adapter.venue_name: adapter}), venue_family="dex"
            )
            assert routed.adapter is adapter
            assert routed.capabilities.supports_unsigned_transaction is True
        finally:
            await db.close()

    async def test_the_signal_driven_cex_path_cannot_select_it(self, tmp_path):
        """`_dispatch_live` builds `venue_family="cex"` intents, so the family
        gate alone keeps a DEX descriptor off the autonomous path. Registration
        exposes the capability without creating a route to it."""
        adapter = self._zeroex()
        db = await _db_with_listings(
            tmp_path, [(adapter.venue_name, "WETH-USDC", "USDC", "spot")]
        )
        try:
            with pytest.raises(NoRoutableVenue) as exc:
                await _select(
                    _routing(db, {adapter.venue_name: adapter}), venue_family="cex"
                )
            assert "intent requires cex" in exc.value.rejections[0][1]
        finally:
            await db.close()

    async def test_it_declares_no_money_movement_through_the_router(self, tmp_path):
        adapter = self._zeroex()
        db = await _db_with_listings(
            tmp_path, [(adapter.venue_name, "WETH-USDC", "USDC", "spot")]
        )
        try:
            routed = await _select(
                _routing(db, {adapter.venue_name: adapter}), venue_family="dex"
            )
            assert routed.capabilities.grants_money_movement() is False
        finally:
            await db.close()


class TestTheZeroExAdapterSkipsListingsInsteadOfErroring:
    """*** AN EXPECTED ERROR-LEVEL TRACEBACK IS A REAL COST. ***

    `_on_demand_listings_fetch` calls `fetch_venue_metadata` on EVERY registered
    adapter. The 0x adapter is deliberately not an `ExchangeAdapter`, so before
    this it had no such method and the call raised `AttributeError` — caught by
    the broad `except Exception` and logged as an ERROR-level traceback for
    every canonical that missed the listings cache.

    Routing completed either way, so this was never a functional break. It is a
    logging defect, and the reason it matters is that a recurring ERROR which is
    actually expected behaviour is exactly how operators learn to skim past
    routing errors — and then miss one that isn't expected.
    """

    def _adapter(self):
        from scout.live.evm.adapter import ZeroExAllowanceHolderAdapter

        return ZeroExAllowanceHolderAdapter(chain_id=1)

    async def test_it_raises_not_implemented_not_attribute_error(self):
        """`NotImplementedError` is the router's SANCTIONED skip branch (info
        level, loop continues). `AttributeError` is not — it falls through to
        the error branch."""
        with pytest.raises(NotImplementedError, match="no venue_listings row"):
            await self._adapter().fetch_venue_metadata("SYM")

    async def test_the_on_demand_fetch_skips_it_quietly_and_writes_no_row(
        self, tmp_path
    ):
        """End-to-end through the real router: registering the 0x adapter must
        not write a listing (there is no exchange-listed pair to record) and
        must not log at error level."""
        db = await _db_with_listings(tmp_path, [])
        try:
            adapter = self._adapter()
            with patch("scout.live.routing.log") as mock_log:
                await _routing(
                    db, {adapter.venue_name: adapter}
                )._on_demand_listings_fetch("SYM")

            assert mock_log.exception.call_args_list == [], (
                "the 0x adapter produced an ERROR-level traceback during a "
                "routine listings miss"
            )
            skipped = [
                c
                for c in mock_log.info.call_args_list
                if c.args and c.args[0] == "on_demand_listing_fetch_not_implemented"
            ]
            assert len(skipped) == 1, "expected exactly one info-level skip"
            assert skipped[0].kwargs["venue"] == adapter.venue_name

            cur = await db._conn.execute("SELECT COUNT(*) FROM venue_listings")
            assert (await cur.fetchone())[
                0
            ] == 0, "a DEX swap venue must not fabricate a venue_listings row"
        finally:
            await db.close()
