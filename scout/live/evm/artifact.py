"""Shared 0x response primitives: errors, address normalization, response hashing.

*** THERE IS DELIBERATELY NO GENERIC QUOTE TYPE HERE. ***
An earlier draft had one ``ZeroExQuote`` carrying an optional ``permit2_eip712``
field. That made Permit2 validation OPTIONAL inside a shape written for a flow
needing no signature at all, so an unvalidated Permit2 response could travel the
AllowanceHolder code path. Each flow now has its own artifact type — see
``scout/live/evm/allowance_holder.py`` — and a response must identify exactly one
reviewed flow or be refused.

What remains here is only what BOTH flows share.

What 0x actually returns (verified against mainnet, 2026-08-02)
---------------------------------------------------------------
``GET /swap/allowance-holder/quote`` and ``/swap/permit2/quote`` both return::

    transaction : {to, data, gas, gasPrice, value}
    sellToken / buyToken / sellAmount / buyAmount / minBuyAmount
    allowanceTarget, blockNumber, zid, route, fees, issues, tokenMetadata
    permit2 : {eip712: {domain, types, primaryType, message}}   (permit2 flavor)

**``chainId`` and ``nonce`` are absent, and that is correct.** ``chainId`` is a
request parameter — we supplied it, so we already hold it — and ``nonce`` is wallet
state only the signer can know. Both are fields the SIGNER owns rather than fields
the provider withheld, which is exactly what distinguishes 0x from a provider that
cannot hand us anything to sign at all.

**There is no quote-expiry field on the allowance-holder flavor.** ``blockNumber``
is the only freshness signal it carries. The permit2 flavor does have an expiry,
inside ``permit2.eip712.message`` — but it governs the ALLOWANCE, not the quote.
So quote freshness is OURS to enforce: this module records ``blockNumber`` and the
retrieval time, and :meth:`ZeroExQuote.is_stale` is what callers must ask. A
provider that does not promise expiry is not a provider whose quotes do not go
stale.

Why every field is re-validated rather than read
------------------------------------------------
The response is an untrusted document from a third party. It is the input to a
signature over money. So parsing is total and strict: unknown shapes raise, string
integers are converted explicitly, addresses are normalized and compared as bytes,
and any field that will be bound into the signing bundle must be present. A parser
that "helpfully" defaults a missing ``minBuyAmount`` to zero would produce a
perfectly valid signature authorizing an unbounded loss.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

__all__ = [
    "ZeroExArtifactError",
    "canonical_response_hash",
    "normalize_address",
]

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HEXDATA_RE = re.compile(r"^0x[0-9a-fA-F]*$")


class ZeroExArtifactError(ValueError):
    """The 0x response cannot be trusted as the basis for a signature.

    A ValueError subclass so a caller that catches broad parse failures still
    catches this, but named so a refusal is legible in a log.
    """


def normalize_address(value: Any, *, field_name: str) -> str:
    """Lowercase 0x-prefixed address, or raise.

    Lowercased rather than checksummed because these are COMPARED, and comparing
    checksummed strings makes equality depend on which side happened to
    checksum. The bytes are what matter.
    """
    if not isinstance(value, str) or not _ADDRESS_RE.match(value):
        raise ZeroExArtifactError(
            f"{field_name} is not a 20-byte hex address: {value!r}"
        )
    return value.lower()


def _hexdata(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not _HEXDATA_RE.match(value):
        raise ZeroExArtifactError(f"{field_name} is not 0x-prefixed hex: {value!r}")
    if len(value) % 2 != 0:
        raise ZeroExArtifactError(f"{field_name} has an odd number of hex digits")
    return value.lower()


def _uint(value: Any, *, field_name: str, allow_zero: bool = True) -> int:
    """Parse a decimal-string or int amount. 0x returns these as STRINGS."""
    if isinstance(value, bool):  # bool is an int subclass; never a valid amount
        raise ZeroExArtifactError(f"{field_name} is a bool, not an amount")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text or not text.lstrip("-").isdigit():
            raise ZeroExArtifactError(f"{field_name} is not an integer: {value!r}")
        parsed = int(text, 10)
    else:
        raise ZeroExArtifactError(
            f"{field_name} must be an integer or decimal string, got "
            f"{type(value).__name__}"
        )
    if parsed < 0:
        raise ZeroExArtifactError(f"{field_name} is negative: {parsed}")
    if parsed == 0 and not allow_zero:
        raise ZeroExArtifactError(f"{field_name} is zero")
    return parsed


def canonical_response_hash(raw: dict[str, Any]) -> str:
    """SHA-256 over the COMPLETE 0x response, canonically serialized.

    Bound into the signing bundle so the artifact that was decoded, validated and
    simulated is provably the artifact that was signed. Hashing the whole document
    — not the fields we happened to extract — means a field this code does not yet
    read cannot change between validation and signing without the hash moving.
    """
    return hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
