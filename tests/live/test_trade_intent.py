"""Canonical immutable TradeIntent — content-bound identity.

The gap this closes: ``scout/live/engine.py:399`` mints ``intent_uuid = str(uuid4())``
and ``make_client_order_id`` binds that identifier to a ``paper_trade_id``. Nothing binds
the intent's *terms*. Two intents differing in amount, chain, recipient or venue can carry
the same identity. ``intent_hash`` closes that: any material change is a different intent.
"""

from __future__ import annotations

import dataclasses
import pickle
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

import pytest

from scout.live.intent import TradeIntent, _canon_decimal

_T0 = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)


def _intent_kwargs(**overrides) -> dict:
    base = dict(
        strategy_id="gainers_early",
        decision_id="dec-1",
        created_at=_T0,
        expires_at=_T0 + timedelta(minutes=5),
        execution_deadline=_T0 + timedelta(minutes=5),
        mode="SUPERVISED_LIVE",
        venue_family="cex",
        preferred_venue="kraken",
        base_asset="BTC",
        quote_asset="USD",
        side="buy",
        exact_quantity=Decimal("0.0002"),
        quantity_denomination="base",
        maximum_notional=Decimal("25"),
        order_type="market",
        maximum_slippage_bps=100,
        maximum_price_impact_bps=100,
        policy_version="v1",
    )
    base.update(overrides)
    return base


def _intent(**overrides) -> TradeIntent:
    return TradeIntent(**_intent_kwargs(**overrides))


class TestCanonicalHash:
    def test_hash_is_deterministic_across_instances(self):
        assert _intent().intent_hash == _intent().intent_hash

    def test_hash_is_stable_across_field_construction_order(self):
        a = TradeIntent(strategy_id="s", decision_id="d", created_at=_T0,
                        expires_at=_T0 + timedelta(minutes=1),
                        execution_deadline=_T0 + timedelta(minutes=1),
                        mode="SUPERVISED_LIVE", venue_family="cex",
                        preferred_venue="kraken", base_asset="BTC",
                        quote_asset="USD", side="buy",
                        exact_quantity=Decimal("1"), quantity_denomination="base",
                        maximum_notional=Decimal("25"), order_type="market",
                        maximum_slippage_bps=100, maximum_price_impact_bps=100,
                        policy_version="v1")
        b = TradeIntent(policy_version="v1", maximum_price_impact_bps=100,
                        maximum_slippage_bps=100, order_type="market",
                        maximum_notional=Decimal("25"), quantity_denomination="base",
                        exact_quantity=Decimal("1"), side="buy", quote_asset="USD",
                        base_asset="BTC", preferred_venue="kraken",
                        venue_family="cex", mode="SUPERVISED_LIVE",
                        execution_deadline=_T0 + timedelta(minutes=1),
                        expires_at=_T0 + timedelta(minutes=1), created_at=_T0,
                        decision_id="d", strategy_id="s")
        assert a.intent_hash == b.intent_hash

    def test_decimal_equality_is_value_based_not_repr_based(self):
        """Decimal('0.10') and Decimal('0.1') are the same quantity. A canonical
        form keyed on str() would split them and mint two identities for one trade."""
        assert (
            _intent(exact_quantity=Decimal("0.10")).intent_hash
            == _intent(exact_quantity=Decimal("0.1")).intent_hash
        )

    @pytest.mark.parametrize(
        "field,value",
        [
            ("exact_quantity", Decimal("0.0003")),
            ("side", "sell"),
            ("base_asset", "ETH"),
            ("quote_asset", "USDT"),
            ("preferred_venue", "binance"),
            ("maximum_notional", Decimal("50")),
            ("maximum_slippage_bps", 200),
            ("maximum_price_impact_bps", 200),
            ("quantity_denomination", "quote"),
            ("mode", "BOUNDED_AUTONOMOUS"),
            ("policy_version", "v2"),
            ("strategy_id", "other"),
            ("decision_id", "dec-2"),
            ("chain", "base"),
            ("network_id", 8453),
            ("base_contract", "0xabc"),
            ("quote_contract", "0xdef"),
            ("recipient", "0xfeed"),
            ("wallet", "0xbeef"),
            ("signer_identity", "pilot"),
            ("position_id", "pos-9"),
            ("limit_price", Decimal("63000")),
            ("minimum_output", Decimal("6.5")),
            ("allowed_fallback_venues", ("binance",)),
        ],
    )
    def test_every_material_field_changes_the_hash(self, field, value):
        """§9c: a field that does not enter the hash is a field an attacker
        can mutate for free after approval."""
        assert _intent().intent_hash != _intent(**{field: value}).intent_hash

    def test_expiry_changes_the_hash(self):
        assert (
            _intent().intent_hash
            != _intent(expires_at=_T0 + timedelta(minutes=9)).intent_hash
        )

    def test_fields_with_companion_requirements_change_the_hash(self):
        """venue_family/order_type/reduce_only cannot vary alone — each has a
        validation companion. Vary the pair; the hash must still move."""
        base = _intent().intent_hash
        assert base != _intent(venue_family="dex", chain="base").intent_hash
        assert base != _intent(order_type="limit",
                               limit_price=Decimal("63000")).intent_hash
        assert base != _intent(reduce_only=True, side="sell",
                               position_id="pos-1").intent_hash


class TestImmutability:
    def test_intent_is_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            _intent().exact_quantity = Decimal("999")  # type: ignore[misc]

    def test_mutable_containers_are_not_accepted(self):
        """A list field would let the caller mutate terms after hashing."""
        with pytest.raises((TypeError, ValueError)):
            _intent(allowed_fallback_venues=["binance"])


class TestValidation:
    def test_rejects_non_positive_quantity(self):
        with pytest.raises(ValueError, match="exact_quantity"):
            _intent(exact_quantity=Decimal("0"))

    def test_rejects_naive_datetimes(self):
        """A naive datetime canonicalizes ambiguously — two hosts in different
        zones would hash the same wall-clock string to the same intent."""
        with pytest.raises(ValueError, match="timezone"):
            _intent(created_at=datetime(2026, 8, 2, 12, 0, 0))

    def test_rejects_expiry_before_creation(self):
        with pytest.raises(ValueError, match="expires_at"):
            _intent(expires_at=_T0 - timedelta(minutes=1))

    def test_rejects_unknown_side(self):
        with pytest.raises(ValueError, match="side"):
            _intent(side="long")

    def test_rejects_limit_order_without_price(self):
        with pytest.raises(ValueError, match="limit_price"):
            _intent(order_type="limit")

    def test_dex_family_requires_chain(self):
        with pytest.raises(ValueError, match="chain"):
            _intent(venue_family="dex")

    def test_reduce_only_requires_position(self):
        """A reduce-only exit that names no position cannot be checked against
        the held size — that is how an 'exit' becomes an unbounded sell."""
        with pytest.raises(ValueError, match="position_id"):
            _intent(reduce_only=True, side="sell")


class TestExpiry:
    def test_is_expired_at_deadline_is_inclusive(self):
        i = _intent()
        assert not i.is_expired(at=i.expires_at - timedelta(seconds=1))
        assert i.is_expired(at=i.expires_at)

    def test_expired_intent_still_hashes(self):
        """Expiry is a policy verdict, not a construction error — recovery must
        be able to re-identify an expired intent by hash."""
        assert _intent().is_expired(at=_T0 + timedelta(hours=1))
        assert _intent().intent_hash


class TestIdentityBinding:
    def test_intent_id_is_derived_from_the_hash(self):
        i = _intent()
        assert i.intent_id.endswith(i.intent_hash[:16])

    def test_client_order_id_is_kraken_short_uuid_form(self):
        """Kraken accepts a dashed UUID, 32 hex chars, or <=18 ASCII free text
        (kraken_adapter.py:90-97). Only the 32-hex form satisfies Kraken and
        Binance at once while staying derived from the hash. A prefixed id like
        "gecko<hex>" is neither hex nor <=18 and gets rejected locally."""
        i = _intent()
        assert i.client_order_id == _intent().client_order_id
        assert len(i.client_order_id) == 32
        assert all(c in "0123456789abcdef" for c in i.client_order_id)

    def test_differing_intents_get_differing_client_order_ids(self):
        assert (
            _intent().client_order_id
            != _intent(exact_quantity=Decimal("0.9")).client_order_id
        )


class TestCanonicalizationIsContextIndependent:
    """Regression for the two BLOCKERs found in adversarial review of PR #498.

    `Decimal.normalize()` consults the ambient thread-local decimal context and
    rounds to `prec` (default 28). It is neither value-preserving nor deterministic,
    so it must never appear in canonicalization.
    """

    def test_29_digit_neighbours_do_not_collide(self):
        """The confirmed collision: two wei quantities one unit apart both rounded
        to ...680 at prec=28 and produced one intent_hash AND one client_order_id.
        Reachable for any 18-decimal token above ~10 whole tokens."""
        a = _intent(exact_quantity=Decimal("10000000000123456789012345678"))
        b = _intent(exact_quantity=Decimal("10000000000123456789012345679"))
        assert a.intent_hash != b.intent_hash
        assert a.client_order_id != b.client_order_id

    def test_canonical_value_is_not_rounded(self):
        """The hash must bind the number the intent actually holds."""
        q = Decimal("10000000000123456789012345678")
        assert _canon_decimal(q) == "10000000000123456789012345678"

    def test_high_precision_neighbours_do_not_collide(self):
        a = _intent(exact_quantity=Decimal("1.000000000000000000000000000001"))
        b = _intent(exact_quantity=Decimal("1.000000000000000000000000000002"))
        assert a.intent_hash != b.intent_hash

    @pytest.mark.parametrize("prec", [9, 28, 50, 60])
    def test_hash_is_identical_under_every_decimal_context(self, prec):
        """Construction AND later recomputation must both be context-blind, or a
        verifier checking approved==signed gets a false mismatch."""
        baseline = _intent(exact_quantity=Decimal("1.2345678901234567890123456789"))
        with localcontext() as ctx:
            ctx.prec = prec
            under_ctx = _intent(exact_quantity=Decimal("1.2345678901234567890123456789"))
            assert under_ctx.intent_hash == baseline.intent_hash
            assert baseline.verify()

    def test_trailing_zeros_and_signed_zero_still_converge(self):
        assert _canon_decimal(Decimal("0.10")) == _canon_decimal(Decimal("0.1"))
        assert _canon_decimal(Decimal("100.00")) == "100"
        assert _canon_decimal(Decimal("1E+2")) == "100"
        assert _canon_decimal(Decimal("-0")) == _canon_decimal(Decimal("0")) == "0"


class TestNonFiniteRejected:
    @pytest.mark.parametrize("raw", ["Infinity", "-Infinity", "NaN"])
    @pytest.mark.parametrize(
        "fieldname", ["exact_quantity", "maximum_notional", "minimum_output"]
    )
    def test_non_finite_decimal_is_rejected_as_valueerror(self, raw, fieldname):
        """Decimal('Infinity') <= 0 is False, so it passed every positivity guard
        and then compared greater than any size — maximum_notional imposed no
        maximum. NaN raised InvalidOperation, not ValueError, escaping callers."""
        with pytest.raises(ValueError, match="finite"):
            _intent(**{fieldname: Decimal(raw)})

    def test_bps_bounds_have_a_ceiling(self):
        """A floored-only bound is not a bound: 10**9 bps was a valid 'limit'."""
        with pytest.raises(ValueError, match="10000"):
            _intent(maximum_slippage_bps=10**9)


class TestValidationCannotBeBypassed:
    def test_subclass_cannot_neuter_validation(self):
        """A frozen subclass overriding _validate to a no-op previously minted a
        well-formed hash over side='SELL_EVERYTHING', quantity=-1."""

        @dataclasses.dataclass(frozen=True)
        class Sneaky(TradeIntent):
            def _validate(self) -> None:
                return None

        with pytest.raises(ValueError):
            Sneaky(**{**_intent_kwargs(), "exact_quantity": Decimal("-1")})

    def test_verify_detects_a_tampered_field(self):
        """object.__setattr__ leaves intent_hash untouched; verify() must catch it."""
        i = _intent()
        assert i.verify()
        object.__setattr__(i, "exact_quantity", Decimal("999999"))
        assert not i.verify()

    def test_verify_survives_pickle_roundtrip(self):
        i = _intent()
        assert pickle.loads(pickle.dumps(i)).verify()


class TestDeadlineOrdering:
    def test_execution_deadline_may_not_outlive_expiry(self):
        """Otherwise is_expired() says True while a deadline-checking caller still
        sees runway."""
        with pytest.raises(ValueError, match="execution_deadline"):
            _intent(execution_deadline=_T0 + timedelta(days=30))
