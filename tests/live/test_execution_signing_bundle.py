"""``ExecutionSigningBundle`` — the generalization of "bind the message hash".

The Solana rule assumes ONE signature over ONE message. Permit2 needs an EIP-712
typed-data signature AND a transaction signature, with the first inserted into the
calldata of the second, so a rule that binds one message hash cannot describe it.
The bundle is what the authorization binds to instead.

These tests are mostly "does changing X change the hash", because that is the
entire contract: if a term can move without moving the hash, the authorization
does not cover it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.live.evm.signing_bundle import (
    ADAPTER_VERSION,
    INSERTION_ALGORITHM_VERSION,
    ExecutionSigningBundle,
)

_T0 = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
_WALLET = "0x28c6c06298d514db089934071355e5743bf21d60"
_ALLOWANCE_HOLDER = "0x0000000000001ff3684f28c67538d4d072c22734"
_PERMIT2 = dict(
    permit2_domain_hash="d" * 64,
    permit2_types_hash="t" * 64,
    permit2_message_hash="m" * 64,
    permit_token="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    permit_spender=_ALLOWANCE_HOLDER,
    permitted_amount=10**18,
    permit_nonce=7,
    permit_expiration=1785000000,
)


def _bundle(**over) -> ExecutionSigningBundle:
    kw = dict(
        intent_hash="i" * 64,
        provider="0x",
        provider_version="v2",
        adapter_version=ADAPTER_VERSION,
        response_hash="r" * 64,
        chain_id=1,
        wallet=_WALLET,
        to=_ALLOWANCE_HOLDER,
        calldata_template_hash="c" * 64,
        value=0,
        sell_token="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        buy_token="0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        sell_amount=10**17,
        minimum_output=187310865,
        quote_block=25670103,
        quote_expires_at=_T0 + timedelta(minutes=1),
        gas_ceiling=233965,
        gas_price_ceiling=73673518,
    )
    kw.update(over)
    return ExecutionSigningBundle(**kw)


class TestEveryCommitmentIsBound:
    """The operator enumerated what the bundle must commit to. Each one, asserted
    by showing the hash moves when it does."""

    @pytest.mark.parametrize(
        "field,value",
        [
            ("intent_hash", "j" * 64),
            ("provider", "1inch"),
            ("provider_version", "v3"),
            ("adapter_version", "zeroex-adapter-v2"),
            ("response_hash", "s" * 64),
            ("chain_id", 8453),
            ("wallet", "0x" + "ab" * 20),
            ("to", "0x" + "cd" * 20),
            ("calldata_template_hash", "e" * 64),
            ("value", 1),
            ("sell_token", "0x" + "11" * 20),
            ("buy_token", "0x" + "22" * 20),
            ("sell_amount", 10**17 + 1),
            ("minimum_output", 187310866),
            ("quote_block", 25670104),
            ("gas_ceiling", 233966),
            ("gas_price_ceiling", 73673519),
            ("insertion_algorithm_version", "permit2-append-v2"),
        ],
    )
    def test_changing_a_committed_term_changes_the_hash(self, field, value):
        assert _bundle(**{field: value}).bundle_hash != _bundle().bundle_hash

    def test_changing_the_quote_expiry_changes_the_hash(self):
        assert (
            _bundle(quote_expires_at=_T0 + timedelta(minutes=2)).bundle_hash
            != _bundle().bundle_hash
        )

    @pytest.mark.parametrize("field", sorted(_PERMIT2))
    def test_changing_a_permit2_term_changes_the_hash(self, field):
        base = _bundle(**_PERMIT2)
        moved = dict(_PERMIT2)
        current = moved[field]
        moved[field] = (
            current + 1
            if isinstance(current, int)
            else "0x" + "99" * 20 if str(current).startswith("0x") else "z" * 64
        )
        assert _bundle(**moved).bundle_hash != base.bundle_hash

    def test_the_insertion_algorithm_is_committed(self):
        """*** THE ONE THAT IS EASY TO FORGET. ***

        Two different insertion algorithms produce two different final
        transactions from identical inputs. A bundle that did not commit to it
        would authorize both.
        """
        assert "insertion_algorithm_version" in _bundle().canonical_form()
        assert _bundle().insertion_algorithm_version == INSERTION_ALGORITHM_VERSION


class TestIdentity:
    def test_the_hash_is_deterministic(self):
        assert _bundle().bundle_hash == _bundle().bundle_hash

    def test_the_hash_is_not_an_input(self):
        """A bundle cannot be constructed claiming to be another one."""
        with pytest.raises(TypeError):
            ExecutionSigningBundle(bundle_hash="0" * 64)  # type: ignore[call-arg]

    def test_verify_detects_tampering(self):
        b = _bundle()
        assert b.verify()
        object.__setattr__(b, "minimum_output", 1)
        assert not b.verify()

    def test_the_token_is_a_prefix_of_the_hash(self):
        b = _bundle()
        assert b.authorization_token == b.bundle_hash[:8].upper()

    def test_absent_is_not_zero(self):
        """*** A BUNDLE WHERE 'no expiration' AND 'expires at 0' HASH THE SAME
        WOULD AUTHORIZE BOTH. ***

        The allowance-holder flavor legitimately has no Permit2 terms, so absence
        must be its own value rather than a falsy collision.
        """
        without = _bundle()
        # Zero is refused outright, so the collision is demonstrated one step up:
        # a present-and-minimal permit differs from an absent one.
        with_permit = _bundle(**{**_PERMIT2, "permit_expiration": 1})
        assert without.bundle_hash != with_permit.bundle_hash
        assert without.requires_permit2_signature is False
        assert with_permit.requires_permit2_signature is True


class TestRefusals:
    def test_a_zero_minimum_output_is_refused(self):
        """A zero floor authorizes receiving nothing — the whole point of binding
        it is that it bounds the loss."""
        with pytest.raises(ValueError, match="bounds nothing"):
            _bundle(minimum_output=0)

    def test_a_checksummed_address_is_refused(self):
        """Addresses are hashed as strings, so a checksummed form and its
        lowercase twin would be two bundles for one transaction."""
        with pytest.raises(ValueError, match="lowercased"):
            _bundle(wallet="0x28C6c06298d514Db089934071355E5743bf21d60")

    def test_a_missing_intent_hash_is_refused(self):
        """Without it the bundle authorizes a swap, not THIS swap."""
        with pytest.raises(ValueError, match="intent_hash"):
            _bundle(intent_hash="")

    def test_a_missing_response_hash_is_refused(self):
        with pytest.raises(ValueError, match="response_hash"):
            _bundle(response_hash="")

    @pytest.mark.parametrize("field", ["gas_ceiling", "gas_price_ceiling"])
    def test_a_zero_cost_ceiling_is_refused(self, field):
        with pytest.raises(ValueError):
            _bundle(**{field: 0})

    def test_a_naive_expiry_is_refused(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            _bundle(quote_expires_at=datetime(2026, 8, 2, 12, 0))

    def test_partial_permit2_terms_are_refused(self):
        """*** ALL-OR-NOTHING. ***

        A bundle carrying a spender but no expiration commits to half an approval
        and leaves the other half — the half that can outlive the trade —
        unbound.
        """
        partial = dict(_PERMIT2)
        del partial["permit_expiration"]
        with pytest.raises(ValueError, match="all-or-nothing"):
            _bundle(**{**partial, "permit_expiration": None})

    def test_a_permit_that_never_expires_is_refused(self):
        with pytest.raises(ValueError, match="expiration"):
            _bundle(**{**_PERMIT2, "permit_expiration": 0})

    def test_a_zero_permitted_amount_is_refused(self):
        with pytest.raises(ValueError, match="permitted_amount"):
            _bundle(**{**_PERMIT2, "permitted_amount": 0})


class TestExpiry:
    def test_an_expired_bundle_reports_expired(self):
        b = _bundle()
        assert not b.is_expired(at=_T0)
        assert b.is_expired(at=_T0 + timedelta(minutes=2))

    def test_expiry_is_inclusive_at_the_boundary(self):
        b = _bundle(quote_expires_at=_T0)
        assert b.is_expired(at=_T0)

    def test_a_naive_comparison_time_is_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            _bundle().is_expired(at=datetime(2026, 8, 2, 12, 0))
