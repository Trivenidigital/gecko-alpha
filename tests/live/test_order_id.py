"""Per-venue client order ids derived from the intent hash.

The property under test is the one the whole intent-binding story rests on: the
id a submission carries is a function of the trade's TERMS, so a mutated intent
cannot reuse the id an authorization was recorded against.

Venue-shape claims are asserted against each venue adapter's OWN validator where
one exists, not against a restatement of it. A test that re-encoded Kraken's rules
locally would pass while the adapter rejected the id.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from scout.live.intent import TradeIntent
from scout.live.order_id import (
    VENUE_ORDER_ID_FORMS,
    UnknownVenueOrderIdForm,
    VenueOrderIdForm,
    client_order_id_for_venue,
    derives_from_intent,
)

_T0 = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


def _intent(**overrides) -> TradeIntent:
    kw = dict(
        strategy_id="first_signal",
        decision_id="paper-1",
        created_at=_T0,
        expires_at=_T0 + timedelta(minutes=5),
        execution_deadline=_T0 + timedelta(minutes=5),
        mode="SUPERVISED_LIVE",
        policy_version="engine-dispatch-v1",
        venue_family="cex",
        preferred_venue="binance",
        base_asset="BTC",
        quote_asset="USDT",
        side="buy",
        exact_quantity=D("100"),
        quantity_denomination="quote",
        maximum_notional=D("100"),
        order_type="market",
        maximum_slippage_bps=50,
        maximum_price_impact_bps=50,
    )
    kw.update(overrides)
    return TradeIntent(**kw)


class TestTheFormsThemselves:
    def test_every_declared_form_fits_its_own_declared_ceiling(self):
        """The dataclass validates this at construction, so this test is really
        asserting that the registry constructed at all — but it names the
        invariant so a future form that violates it fails with a reason rather
        than an ImportError nobody reads."""
        for venue, form in VENUE_ORDER_ID_FORMS.items():
            assert len(form.prefix) + form.hash_chars <= form.max_len, venue

    def test_a_form_that_cannot_fit_its_ceiling_is_refused_at_construction(self):
        with pytest.raises(ValueError, match="max_len"):
            VenueOrderIdForm(
                venue="v", prefix="gecko-", hash_chars=40, max_len=28, source="x"
            )

    def test_a_form_cannot_ask_for_more_than_sha256_provides(self):
        """65 hex characters would silently render a SHORTER id than declared,
        breaking the length arithmetic every other check relies on."""
        with pytest.raises(ValueError, match="64 hex"):
            VenueOrderIdForm(
                venue="v", prefix="", hash_chars=65, max_len=99, source="x"
            )

    def test_each_form_carries_its_source(self):
        """A number with no stated reason is a number the next person changes."""
        for venue, form in VENUE_ORDER_ID_FORMS.items():
            assert form.source.strip(), venue


class TestContentBinding:
    def test_the_id_is_a_function_of_the_terms(self):
        a = client_order_id_for_venue(_intent(), "binance")
        b = client_order_id_for_venue(_intent(), "binance")
        assert a == b

    @pytest.mark.parametrize(
        "field,value",
        [
            ("exact_quantity", D("101")),
            ("maximum_notional", D("101")),
            ("side", "sell"),
            ("base_asset", "ETH"),
            ("quote_asset", "USDC"),
            ("maximum_slippage_bps", 51),
            ("mode", "BOUNDED_AUTONOMOUS"),
            ("preferred_venue", "kraken"),
        ],
    )
    def test_changing_any_material_term_changes_the_id(self, field, value):
        """*** THE POINT OF THE WHOLE MODULE. ***

        `intent_uuid = str(uuid4())` produced the same id for a mutated intent.
        Every one of these mutations must produce a different one.
        """
        base = client_order_id_for_venue(_intent(), "binance")
        mutated = client_order_id_for_venue(_intent(**{field: value}), "binance")
        assert mutated != base

    def test_decimal_representation_does_not_change_the_id(self):
        """0.10 and 0.1 are one quantity, so they are one order, so they get one
        id. The inverse of the test above, and the reason canonicalization is
        value-based rather than repr-based."""
        assert client_order_id_for_venue(
            _intent(exact_quantity=D("100.0"), maximum_notional=D("100.00")), "binance"
        ) == client_order_id_for_venue(
            _intent(exact_quantity=D("100"), maximum_notional=D("100")), "binance"
        )

    def test_an_intent_whose_hash_no_longer_matches_mints_nothing(self):
        """A row read back from the DB, or an object.__setattr__ poke, can carry
        terms that no longer match the stored hash. Minting an id from it would
        assert a binding verify() says is broken."""
        intent = _intent()
        object.__setattr__(intent, "exact_quantity", D("999999"))
        assert not intent.verify()
        with pytest.raises(ValueError, match="verify"):
            client_order_id_for_venue(intent, "binance")


class TestVenueShapes:
    def test_kraken_ids_are_accepted_by_krakens_own_validator(self):
        """Asserted against the adapter's `_validate_cl_ord_id`, not against a
        local copy of Kraken's rules — a local copy would pass while the real
        request was rejected."""
        from scout.live.kraken_adapter import _validate_cl_ord_id

        cid = client_order_id_for_venue(_intent(preferred_venue="kraken"), "kraken")
        _validate_cl_ord_id(cid)  # raises KrakenClientOrderIdError on refusal
        assert len(cid) == 32
        assert all(c in "0123456789abcdef" for c in cid)

    def test_the_legacy_binance_id_is_rejected_by_kraken(self):
        """Guard on the guard: if `_validate_cl_ord_id` accepted anything, the
        test above would be vacuous. The 28-char Binance-shaped id is the exact
        thing kraken_adapter fact 10 says does not fit."""
        from scout.live.kraken_adapter import (
            KrakenClientOrderIdError,
            _validate_cl_ord_id,
        )

        with pytest.raises(KrakenClientOrderIdError):
            _validate_cl_ord_id("gecko-1234567890-abcdef12345")

    def test_binance_ids_stay_within_the_projects_28_char_ceiling(self):
        cid = client_order_id_for_venue(_intent(), "binance")
        assert len(cid) == 28
        assert cid.startswith("gecko-")

    def test_binance_ids_use_only_characters_the_venue_documents_accepting(self):
        cid = client_order_id_for_venue(_intent(), "binance")
        assert all(c in "0123456789abcdef-gecko" for c in cid)

    def test_the_two_forms_carry_different_amounts_of_hash_and_say_so(self):
        """The asymmetry is deliberate (Binance spends 6 characters on the
        prefix), and it is reported as a number rather than buried in prose."""
        assert VENUE_ORDER_ID_FORMS["kraken"].entropy_bits == 128
        assert VENUE_ORDER_ID_FORMS["binance"].entropy_bits == 88

    def test_an_undeclared_venue_gets_no_guessed_id(self):
        """Fail-closed. A fallback shape is how an id a venue rejects — or worse,
        accepts under different uniqueness semantics — reaches a signed request."""
        with pytest.raises(UnknownVenueOrderIdForm, match="coinbase"):
            client_order_id_for_venue(_intent(), "coinbase")


class TestReconciliationDirection:
    def test_a_matching_pair_verifies(self):
        intent = _intent()
        cid = client_order_id_for_venue(intent, "binance")
        assert derives_from_intent(cid, intent, "binance")

    def test_an_id_from_a_different_intent_does_not_verify(self):
        cid = client_order_id_for_venue(_intent(), "binance")
        assert not derives_from_intent(cid, _intent(side="sell"), "binance")

    def test_an_id_from_a_different_venues_form_does_not_verify(self):
        """Same intent, wrong form. The venue is part of the question."""
        intent = _intent(preferred_venue="kraken")
        kraken_cid = client_order_id_for_venue(intent, "kraken")
        assert not derives_from_intent(kraken_cid, intent, "binance")

    def test_an_unknown_venue_answers_false_rather_than_raising(self):
        """The caller is asking a yes/no question about evidence it already
        holds; 'cannot establish the binding' is a False answer, not an error a
        caller might catch too broadly and treat as a pass."""
        assert not derives_from_intent("whatever", _intent(), "coinbase")
