"""The capability gate in front of route selection.

Covers the §9c defect directly: routing chose a venue and the order went to the
adapter injected at construction, so a non-Binance selection placed a Binance
order carrying the other venue's pair string. `select_route` returns the adapter
WITH the candidate, so there is no second place to get one from.
"""

from __future__ import annotations

from decimal import Decimal

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
