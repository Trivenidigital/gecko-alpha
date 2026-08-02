"""The 0x quote artifact: parsed strictly, never trusted as prose.

The response is an untrusted document from a third party and it is the input to a
signature over money. Every field a signature depends on is re-validated, and the
tests below are mostly about what the parser REFUSES — a parser that helpfully
defaults a missing ``minBuyAmount`` to zero produces a perfectly valid signature
authorizing an unbounded loss.

The baseline payload is the real mainnet shape, captured 2026-08-02 from
``/swap/allowance-holder/quote`` (0.1 WETH → USDC, chainId 1).
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from scout.live.evm.artifact import (
    ZeroExArtifactError,
    ZeroExQuote,
    canonical_response_hash,
    normalize_address,
    parse_quote,
)

_T0 = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
_TAKER = "0x28C6c06298d514Db089934071355E5743bf21d60"
_ALLOWANCE_HOLDER = "0x0000000000001ff3684f28c67538d4d072c22734"
_WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
_USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


def _raw(**over) -> dict:
    """The captured mainnet response shape."""
    payload = {
        "liquidityAvailable": True,
        "transaction": {
            "to": _ALLOWANCE_HOLDER,
            "data": "0x2213bc0b" + "ab" * 1500,
            "gas": "233965",
            "gasPrice": "73673518",
            "value": "0",
        },
        "sellToken": _WETH,
        "buyToken": _USDC,
        "sellAmount": "100000000000000000",
        "buyAmount": "189205765",
        "minBuyAmount": "187310865",
        "allowanceTarget": _ALLOWANCE_HOLDER,
        "blockNumber": "25670103",
        "zid": "0x14908f2e535aa3a139d351b7",
        "issues": {
            "allowance": {"actual": "0", "spender": _ALLOWANCE_HOLDER},
            "balance": None,
            "simulationIncomplete": False,
        },
        "tokenMetadata": {
            "buyToken": {"buyTaxBps": "0", "sellTaxBps": "0"},
            "sellToken": {"buyTaxBps": "0", "sellTaxBps": "0"},
        },
    }
    payload.update(over)
    return payload


def _parse(raw=None, **kw) -> ZeroExQuote:
    opts = dict(flavor="allowance-holder", chain_id=1, taker=_TAKER, retrieved_at=_T0)
    opts.update(kw)
    return parse_quote(raw if raw is not None else _raw(), **opts)


class TestTheRealShapeParses:
    def test_the_captured_mainnet_response_parses(self):
        """Guard on every refusal test below: the real thing must be accepted."""
        q = _parse()
        assert q.to == _ALLOWANCE_HOLDER
        assert q.sell_token == _WETH and q.buy_token == _USDC
        assert q.sell_amount == 100000000000000000
        assert q.minimum_buy_amount == 187310865
        assert q.gas == 233965 and q.value == 0
        assert q.block_number == 25670103

    def test_chain_id_and_taker_come_from_US_not_from_the_response(self):
        """*** 0x ECHOES NEITHER. ***

        `chainId` is a request parameter and `nonce` is wallet state — both are
        fields the SIGNER owns, not fields the provider withheld. Binding what we
        ASKED FOR lets a later check compare it against what the calldata encodes.
        """
        assert "chainId" not in _raw()
        assert "nonce" not in _raw()["transaction"]
        q = _parse(chain_id=8453)
        assert q.chain_id == 8453
        assert q.taker == _TAKER.lower()

    def test_amounts_arrive_as_strings_and_become_ints(self):
        q = _parse()
        assert isinstance(q.sell_amount, int)
        assert isinstance(q.gas_price, int)

    def test_the_slippage_bound_is_computed_not_trusted(self):
        """0x publishes no slippage field. The only number bounding this
        transaction's loss is derived from buyAmount vs minBuyAmount."""
        q = _parse()
        assert 95 < float(q.slippage_bps()) < 105  # ~1%

    def test_an_approval_shortfall_is_surfaced(self):
        """A separate signed action, and therefore a SEPARATE authorization."""
        assert _parse().requires_approval is True
        ok = _raw()
        ok["issues"] = {
            "allowance": {"actual": "10" * 20, "spender": _ALLOWANCE_HOLDER}
        }
        assert _parse(ok).requires_approval is False

    def test_the_response_hash_covers_fields_the_parser_never_reads(self):
        """Hashing the WHOLE document means a field this code does not yet
        understand cannot change between validation and signing unnoticed."""
        a = _raw()
        b = _raw()
        b["someFutureField"] = {"nested": 1}
        assert canonical_response_hash(a) != canonical_response_hash(b)

    def test_the_response_hash_is_order_insensitive(self):
        a = _raw()
        b = {k: a[k] for k in reversed(list(a))}
        assert canonical_response_hash(a) == canonical_response_hash(b)


class TestRefusals:
    @pytest.mark.parametrize(
        "field",
        [
            "sellToken",
            "buyToken",
            "allowanceTarget",
            "sellAmount",
            "buyAmount",
            "minBuyAmount",
            "blockNumber",
            "transaction",
        ],
    )
    def test_a_missing_required_field_is_refused(self, field):
        raw = _raw()
        del raw[field]
        with pytest.raises(ZeroExArtifactError):
            _parse(raw)

    def test_a_missing_minimum_is_refused_rather_than_defaulted(self):
        """*** THE ONE THAT MATTERS MOST. ***

        Defaulting `minBuyAmount` to zero would produce a structurally perfect
        signature authorizing receipt of nothing.
        """
        raw = _raw()
        del raw["minBuyAmount"]
        with pytest.raises(ZeroExArtifactError, match="minBuyAmount"):
            _parse(raw)

    def test_a_zero_minimum_is_refused(self):
        with pytest.raises(ZeroExArtifactError, match="minBuyAmount"):
            _parse(_raw(minBuyAmount="0"))

    def test_a_minimum_above_the_estimate_is_refused(self):
        """Internally inconsistent: the floor cannot exceed the expectation."""
        with pytest.raises(ZeroExArtifactError, match="exceeds buyAmount"):
            _parse(_raw(minBuyAmount="999999999999"))

    def test_no_liquidity_is_refused_not_returned_empty(self):
        """0x answers `liquidityAvailable: false` with HTTP 200 and a thin body.
        Treating it as a parse failure stops a zero-amount transaction that looks
        structurally fine from ever reaching a caller."""
        with pytest.raises(ZeroExArtifactError, match="liquidityAvailable"):
            _parse(_raw(liquidityAvailable=False))

    @pytest.mark.parametrize(
        "bad", ["0x123", "not-an-address", "", None, 42, "0x" + "g" * 40]
    )
    def test_a_malformed_address_is_refused(self, bad):
        with pytest.raises(ZeroExArtifactError):
            _parse(_raw(sellToken=bad))

    @pytest.mark.parametrize("bad", ["-1", "abc", "", None, True, 1.5])
    def test_a_malformed_amount_is_refused(self, bad):
        with pytest.raises(ZeroExArtifactError):
            _parse(_raw(sellAmount=bad))

    def test_a_zero_sell_amount_is_refused(self):
        with pytest.raises(ZeroExArtifactError):
            _parse(_raw(sellAmount="0"))

    def test_calldata_too_short_to_hold_a_selector_is_refused(self):
        raw = _raw()
        raw["transaction"]["data"] = "0x1234"
        with pytest.raises(ZeroExArtifactError, match="selector"):
            _parse(raw)

    def test_non_hex_calldata_is_refused(self):
        raw = _raw()
        raw["transaction"]["data"] = "not hex at all"
        with pytest.raises(ZeroExArtifactError, match="hex"):
            _parse(raw)

    def test_zero_gas_is_refused(self):
        raw = _raw()
        raw["transaction"]["gas"] = "0"
        with pytest.raises(ZeroExArtifactError, match="gas"):
            _parse(raw)

    def test_an_unknown_flavor_is_refused(self):
        with pytest.raises(ZeroExArtifactError, match="flavor"):
            _parse(flavor="whatever")

    def test_a_non_positive_chain_id_is_refused(self):
        with pytest.raises(ZeroExArtifactError, match="chain_id"):
            _parse(chain_id=0)


class TestFlavourConsistency:
    def test_permit2_requested_but_absent_is_refused(self):
        """There would be nothing to sign — better to say so than to proceed and
        discover it at the signing step."""
        with pytest.raises(ZeroExArtifactError, match="permit2"):
            _parse(flavor="permit2")

    def test_allowance_holder_carrying_a_permit2_payload_is_refused(self):
        """Refused rather than ignored: it is not the document this flavor's
        validation was written against."""
        raw = _raw(permit2={"eip712": {"domain": {}, "types": {}, "message": {}}})
        with pytest.raises(ZeroExArtifactError, match="permit2"):
            _parse(raw)

    def test_permit2_payload_is_carried_through_when_present(self):
        eip712 = {
            "domain": {"name": "Permit2", "chainId": 1},
            "types": {"PermitSingle": []},
            "primaryType": "PermitSingle",
            "message": {"details": {"amount": "1", "expiration": "999"}},
        }
        q = _parse(_raw(permit2={"eip712": eip712}), flavor="permit2")
        assert q.permit2_eip712 == eip712


class TestStaleness:
    """0x publishes no expiry for the allowance-holder flavor, so freshness is
    ours to define — and a provider that does not promise expiry is not a
    provider whose quotes do not go stale."""

    def test_a_fresh_quote_is_not_stale(self):
        q = _parse()
        assert not q.is_stale(now=_T0 + timedelta(seconds=5), current_block=25670104)

    def test_wall_clock_age_makes_it_stale(self):
        """Catches a STALLED chain, where the block number sits still while
        off-chain prices keep moving."""
        q = _parse()
        assert q.is_stale(now=_T0 + timedelta(minutes=5), current_block=25670103)

    def test_block_drift_makes_it_stale(self):
        """Catches a busy chain moving the route out from under us."""
        q = _parse()
        assert q.is_stale(now=_T0 + timedelta(seconds=1), current_block=25670200)

    def test_a_naive_timestamp_is_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            _parse().is_stale(now=datetime(2026, 8, 2, 12, 0))


class TestTransferTaxes:
    def test_a_fee_on_transfer_token_is_surfaced(self):
        """A sell tax makes `minBuyAmount` a promise the token itself can break,
        so it is parsed rather than left for on-chain discovery."""
        raw = _raw()
        raw["tokenMetadata"]["sellToken"]["sellTaxBps"] = "300"
        assert _parse(raw).sell_tax_bps == 300

    def test_absent_metadata_defaults_to_no_tax(self):
        raw = _raw()
        del raw["tokenMetadata"]
        assert _parse(raw).buy_tax_bps == 0


class TestAddressNormalization:
    def test_addresses_compare_as_bytes_not_as_checksums(self):
        """Comparing checksummed strings makes equality depend on which side
        happened to checksum."""
        mixed = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
        assert normalize_address(mixed, field_name="x") == mixed.lower()

    def test_a_quote_normalizes_the_taker_it_was_given(self):
        assert _parse().taker == _TAKER.lower()
