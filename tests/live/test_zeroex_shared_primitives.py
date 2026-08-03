"""Shared 0x response primitives, and the absence of a generic quote type.

The generic ``ZeroExQuote`` with an optional ``permit2_eip712`` field was removed
deliberately: it made Permit2 validation optional inside a shape written for a
flow that needs no signature at all. The flow-specific artifact lives in
``allowance_holder.py`` and is tested in ``test_zeroex_allowance_holder.py``.
"""

from __future__ import annotations

import pytest

from scout.live.evm import artifact as artifact_module
from scout.live.evm.artifact import (
    ZeroExArtifactError,
    canonical_response_hash,
    normalize_address,
)


class TestNoGenericQuoteType:
    """*** A REGRESSION GUARD ON THE FLOW SEPARATION. ***

    Re-introducing a generic quote object is how Permit2 validation becomes
    optional again, so its absence is asserted rather than assumed.
    """

    def test_there_is_no_generic_quote_class(self):
        assert not hasattr(artifact_module, "ZeroExQuote")

    def test_there_is_no_flow_agnostic_parser(self):
        assert not hasattr(artifact_module, "parse_quote")

    def test_the_module_carries_no_flow_specific_symbols(self):
        """Shared primitives must know about NEITHER flow, not handle both.

        Asserted over the module's public API rather than over its prose — the
        docstring necessarily NAMES the flows to explain why it does not handle
        them, so a substring search would match the documentation and fail on a
        module that is structurally correct.
        """
        exported = {n for n in artifact_module.__all__}
        assert exported == {
            "ZeroExArtifactError",
            "canonical_response_hash",
            "normalize_address",
        }
        # Names that would mean this module had grown flow-specific behaviour
        # again. `QUOTE_AGE` constants are deliberately NOT in this list — they
        # are generic freshness bounds, not knowledge of either flow — but they
        # do belong with the artifact that enforces them, so they are asserted to
        # have moved rather than merely tolerated here.
        flow_words = ("permit", "allowance", "settler", "eip712")
        leaked = [
            name
            for name in dir(artifact_module)
            if not name.startswith("_")
            and any(word in name.lower() for word in flow_words)
        ]
        assert leaked == [], f"flow-specific symbols leaked into shared: {leaked}"

    def test_freshness_bounds_live_with_the_artifact_that_enforces_them(self):
        """A constant nobody in the module reads is a constant that drifts from
        the code actually using it."""
        from scout.live.evm import allowance_holder

        assert hasattr(allowance_holder, "DEFAULT_MAX_QUOTE_AGE_SECONDS")
        assert hasattr(allowance_holder, "DEFAULT_MAX_QUOTE_AGE_BLOCKS")


class TestAddressNormalization:
    def test_addresses_compare_as_bytes_not_as_checksums(self):
        mixed = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
        assert normalize_address(mixed, field_name="x") == mixed.lower()

    @pytest.mark.parametrize(
        "bad", ["0x123", "not-an-address", "", None, 42, "0x" + "g" * 40]
    )
    def test_a_malformed_address_is_refused(self, bad):
        with pytest.raises(ZeroExArtifactError):
            normalize_address(bad, field_name="x")


class TestResponseHash:
    def test_it_covers_fields_no_parser_reads(self):
        """Hashing the WHOLE document means a field this code does not yet
        understand cannot change between validation and signing unnoticed."""
        a = {"a": 1}
        b = {"a": 1, "someFutureField": {"nested": 2}}
        assert canonical_response_hash(a) != canonical_response_hash(b)

    def test_it_is_key_order_insensitive(self):
        a = {"a": 1, "b": 2}
        b = {"b": 2, "a": 1}
        assert canonical_response_hash(a) == canonical_response_hash(b)

    def test_it_is_stable(self):
        a = {"x": [1, 2, {"y": "z"}]}
        assert canonical_response_hash(a) == canonical_response_hash(dict(a))
