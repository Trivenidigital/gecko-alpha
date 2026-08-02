"""Venue capabilities are declared, never inferred.

Routing today infers what a venue can do from whether a call happens to succeed.
That is how a venue silently acquires a capability it does not have — and, worse,
how a money-moving capability (withdraw, transfer, sweep) becomes reachable from a
path that only meant to trade.

Two properties are load-bearing here:

1. **Fail closed.** An adapter that declares nothing has nothing. Undeclared is
   absent, not assumed.
2. **Transfer authority is not trade authority.** ``supports_withdrawal`` and
   friends are separate flags that no trading path consults.
"""

from __future__ import annotations

import dataclasses

import pytest

from scout.live.capabilities import VenueCapabilities


class TestFailClosed:
    def test_bare_capabilities_grant_nothing(self):
        caps = VenueCapabilities(venue="unknown")
        for f in dataclasses.fields(caps):
            if f.name == "venue":
                continue
            if f.name == "venue_family":
                # None is the fail-closed value here: an undeclared family matches
                # no intent, rather than defaulting to "cex" and approving one.
                assert caps.venue_family is None
                continue
            assert getattr(caps, f.name) is False, f"{f.name} defaults to True"

    def test_base_adapter_declares_nothing_by_default(self):
        """An adapter that forgets to override gets an all-False descriptor —
        not an exception, and not a permissive default.

        Applied to a bare stand-in rather than a real subclass: ExchangeAdapter
        has nine abstract methods, so any subclass declared here would need nine
        stubs that test nothing. The default's only input is ``venue_name``."""
        from scout.live.adapter_base import ExchangeAdapter

        class Forgetful:
            venue_name = "forgetful"

        caps = ExchangeAdapter.describe_capabilities(Forgetful())
        assert caps.venue == "forgetful"
        assert not caps.supports_market_orders
        assert not caps.supports_withdrawal
        assert not caps.grants_money_movement()

    def test_capabilities_are_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            VenueCapabilities(venue="k").supports_withdrawal = True  # type: ignore[misc]


class TestTransferAuthorityIsSeparate:
    def test_full_trading_capability_does_not_imply_transfer(self):
        caps = VenueCapabilities(
            venue="kraken",
            supports_market_orders=True,
            supports_limit_orders=True,
            supports_cancel=True,
            supports_client_order_id=True,
            supports_reduce_only=True,
            supports_partial_fills=True,
        )
        assert not caps.supports_withdrawal
        assert not caps.supports_transfer
        assert not caps.supports_sweep

    def test_money_movement_flags_are_enumerated_and_stay_enumerated(self):
        """If someone adds a new money-movement capability they must add it here
        too, or this test stops protecting the thing it names."""
        assert VenueCapabilities.MONEY_MOVEMENT == frozenset(
            {"supports_withdrawal", "supports_transfer", "supports_sweep"}
        )

    def test_grants_money_movement_reports_true_only_when_declared(self):
        assert not VenueCapabilities(venue="k").grants_money_movement()
        assert VenueCapabilities(venue="k", supports_withdrawal=True
                                 ).grants_money_movement()


class TestIntentCompatibility:
    def _caps(self, **kw):
        return VenueCapabilities(venue="v", **kw)

    def test_market_intent_rejected_when_market_orders_undeclared(self):
        ok, reason = self._caps(supports_limit_orders=True).permits_order(
            order_type="market", reduce_only=False
        )
        assert not ok
        assert "market" in reason

    def test_limit_intent_rejected_when_limit_orders_undeclared(self):
        ok, reason = self._caps(supports_market_orders=True).permits_order(
            order_type="limit", reduce_only=False
        )
        assert not ok
        assert "limit" in reason

    def test_reduce_only_intent_rejected_when_undeclared(self):
        """Silently dropping reduce_only turns a bounded exit into a plain sell."""
        ok, reason = self._caps(supports_market_orders=True).permits_order(
            order_type="market", reduce_only=True
        )
        assert not ok
        assert "reduce_only" in reason

    def test_permitted_when_all_declared(self):
        ok, reason = self._caps(
            supports_market_orders=True, supports_reduce_only=True
        ).permits_order(order_type="market", reduce_only=True)
        assert ok
        assert reason is None


class TestRealAdaptersDeclareHonestly:
    def test_kraken_declares_limit_only_and_no_withdrawal(self):
        """kraken_adapter.py:8 — send_order / place_order_request /
        place_exit_order ALL raise; place_limit_order is the only method that
        reaches AddOrder. So Kraken must declare limit orders and NOT market
        orders, or routing will hand it an intent it structurally cannot fill.
        It also raises KrakenWithdrawalCapabilityError rather than withdrawing."""
        from scout.live.kraken_adapter import KrakenSpotAdapter

        caps = KrakenSpotAdapter.describe_capabilities(
            KrakenSpotAdapter.__new__(KrakenSpotAdapter)
        )
        assert caps.venue == "kraken"
        assert caps.supports_limit_orders
        assert not caps.supports_market_orders
        assert caps.supports_client_order_id
        assert caps.supports_cancel
        assert not caps.supports_withdrawal
        assert not caps.supports_transfer

    def test_no_adapter_declares_unsigned_transaction_support(self):
        """No venue in tree can hand us a host-signable artifact today. If one
        ever can, this test must be updated deliberately — it should never start
        passing by accident."""
        from scout.live.binance_adapter import BinanceSpotAdapter
        from scout.live.kraken_adapter import KrakenSpotAdapter

        for cls in (KrakenSpotAdapter, BinanceSpotAdapter):
            caps = cls.describe_capabilities(cls.__new__(cls))
            assert not caps.supports_unsigned_transaction, cls.__name__
            assert not caps.supports_host_owned_signing, cls.__name__


class TestVenueFamilySeparation:
    def test_cex_descriptor_refuses_a_dex_intent(self):
        """Kraken previously returned (True, None) for a venue_family='dex',
        chain='solana' intent purely because the order type matched."""
        from scout.live.kraken_adapter import KrakenSpotAdapter

        caps = KrakenSpotAdapter.describe_capabilities(
            KrakenSpotAdapter.__new__(KrakenSpotAdapter)
        )
        ok, reason = caps.permits_order(
            order_type="limit", reduce_only=False, venue_family="dex"
        )
        assert not ok
        assert "dex" in reason

    def test_undeclared_family_matches_nothing(self):
        ok, reason = VenueCapabilities(
            venue="v", supports_market_orders=True
        ).permits_order(order_type="market", reduce_only=False, venue_family="cex")
        assert not ok
        assert "venue_family" in reason

    def test_binance_no_longer_declares_reduce_only(self):
        """place_exit_order fires a bare MARKET SELL with a caller-supplied
        base_qty — no position lookup, no clamp. Declaring reduce_only let the
        router approve an exit and then land on the unbounded entry path."""
        from scout.live.binance_adapter import BinanceSpotAdapter

        caps = BinanceSpotAdapter.describe_capabilities(
            BinanceSpotAdapter.__new__(BinanceSpotAdapter)
        )
        assert not caps.supports_reduce_only
        ok, reason = caps.permits_order(
            order_type="market", reduce_only=True, venue_family="cex"
        )
        assert not ok
        assert "reduce_only" in reason
