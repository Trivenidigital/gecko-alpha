"""The 0x quote artifact, parsed strictly and never trusted as prose.

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
    "ZeroExQuote",
    "ZeroExArtifactError",
    "parse_quote",
    "canonical_response_hash",
    "normalize_address",
]

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HEXDATA_RE = re.compile(r"^0x[0-9a-fA-F]*$")

#: Quote freshness we impose because 0x does not. Blocks, not seconds: the quote's
#: only self-reported anchor is ``blockNumber``, and route economics move with
#: blocks rather than with wall time.
DEFAULT_MAX_QUOTE_AGE_BLOCKS = 5

#: Belt to the block braces — a wall-clock ceiling for the case where the chain
#: stalls and the block number stops advancing while prices keep moving off-chain.
DEFAULT_MAX_QUOTE_AGE_SECONDS = 60.0


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


@dataclass(frozen=True)
class ZeroExQuote:
    """A validated 0x quote. Every field a signature will depend on, typed."""

    # --- provenance ---------------------------------------------------------
    flavor: str  # "allowance-holder" | "permit2"
    chain_id: int  # OURS: the request parameter, never echoed by 0x
    zid: str | None
    block_number: int
    retrieved_at: datetime

    # --- instrument ---------------------------------------------------------
    sell_token: str
    buy_token: str
    sell_amount: int
    buy_amount: int
    minimum_buy_amount: int

    # --- the unsigned transaction -------------------------------------------
    to: str
    data: str
    value: int
    gas: int
    gas_price: int

    # --- approval -----------------------------------------------------------
    allowance_target: str
    allowance_actual: int | None
    taker: str

    # --- permit2 (absent on the allowance-holder flavor) --------------------
    permit2_eip712: dict[str, Any] | None = None

    # --- taxes: a fee-on-transfer token makes minBuyAmount misleading -------
    buy_tax_bps: int = 0
    sell_tax_bps: int = 0

    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------
    @property
    def response_hash(self) -> str:
        return canonical_response_hash(self.raw)

    @property
    def requires_approval(self) -> bool:
        """Whether an ERC-20 approval must land before this can execute.

        A separate signed action, and therefore a SEPARATE authorization — never
        folded into the swap's approval on the theory that it is 'just plumbing'.
        """
        return self.allowance_actual is not None and (
            self.allowance_actual < self.sell_amount
        )

    def slippage_bps(self) -> Decimal:
        """How far ``minimum_buy_amount`` sits below ``buy_amount``, in bps.

        The only number that bounds the loss this transaction can take, so it is
        computed here rather than trusted from a field 0x does not provide.
        """
        if self.buy_amount == 0:
            return Decimal(0)
        delta = Decimal(self.buy_amount - self.minimum_buy_amount)
        return (delta / Decimal(self.buy_amount)) * Decimal(10_000)

    def is_stale(
        self,
        *,
        current_block: int | None = None,
        now: datetime | None = None,
        max_age_blocks: int = DEFAULT_MAX_QUOTE_AGE_BLOCKS,
        max_age_seconds: float = DEFAULT_MAX_QUOTE_AGE_SECONDS,
    ) -> bool:
        """Whether this quote is too old to sign.

        0x publishes no expiry for the allowance-holder flavor, so staleness is
        ours to define. Both bounds are checked: blocks catch a busy chain moving
        the route out from under us, wall time catches a stalled chain where the
        block number sits still while off-chain prices keep moving.
        """
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            raise ValueError("`now` must be timezone-aware")
        if (moment - self.retrieved_at).total_seconds() > max_age_seconds:
            return True
        if current_block is not None and current_block - self.block_number > (
            max_age_blocks
        ):
            return True
        return False


def parse_quote(
    raw: dict[str, Any],
    *,
    flavor: str,
    chain_id: int,
    taker: str,
    retrieved_at: datetime | None = None,
) -> ZeroExQuote:
    """Validate a 0x quote response into a :class:`ZeroExQuote`, or raise.

    ``chain_id`` and ``taker`` are supplied by the CALLER, because 0x does not
    echo them. Passing them in means the bundle binds what we ASKED FOR, and a
    later check can compare that against what the calldata actually encodes.
    """
    if not isinstance(raw, dict):
        raise ZeroExArtifactError(f"response is {type(raw).__name__}, not an object")
    if flavor not in ("allowance-holder", "permit2"):
        raise ZeroExArtifactError(f"unknown 0x flavor {flavor!r}")
    if chain_id <= 0:
        raise ZeroExArtifactError(f"chain_id must be positive, got {chain_id}")

    # `liquidityAvailable: false` is 0x's documented "no route" answer, and it
    # arrives with a 200 and a mostly-empty body. Treating it as a parse failure
    # rather than an empty quote keeps the caller from ever seeing a zero-amount
    # transaction that looks structurally fine.
    if raw.get("liquidityAvailable") is False:
        raise ZeroExArtifactError("0x reports liquidityAvailable=false — no route")

    tx = raw.get("transaction")
    if not isinstance(tx, dict):
        raise ZeroExArtifactError("response has no `transaction` object")

    data = _hexdata(tx.get("data"), field_name="transaction.data")
    if len(data) < 10:
        raise ZeroExArtifactError(
            f"transaction.data is {len(data)} chars — too short to carry a "
            "4-byte selector, so there is nothing to validate"
        )

    issues = raw.get("issues") or {}
    allowance = issues.get("allowance") if isinstance(issues, dict) else None
    allowance_actual = (
        _uint(allowance.get("actual"), field_name="issues.allowance.actual")
        if isinstance(allowance, dict) and allowance.get("actual") is not None
        else None
    )

    # A fee-on-transfer token silently delivers less than the amount the swap
    # moves, which makes `minBuyAmount` a promise the token itself can break.
    # Parsed here so a caller can refuse rather than discover it on-chain.
    meta = raw.get("tokenMetadata") or {}
    buy_meta = meta.get("buyToken") or {} if isinstance(meta, dict) else {}
    sell_meta = meta.get("sellToken") or {} if isinstance(meta, dict) else {}

    permit2 = raw.get("permit2")
    permit2_eip712: dict[str, Any] | None = None
    if flavor == "permit2":
        if not isinstance(permit2, dict) or not isinstance(permit2.get("eip712"), dict):
            raise ZeroExArtifactError(
                "permit2 flavor requested but the response carries no "
                "`permit2.eip712` payload — there would be nothing to sign"
            )
        permit2_eip712 = permit2["eip712"]
    elif permit2 is not None:
        # Refused rather than ignored: an allowance-holder quote that carries a
        # permit2 payload is not the document this flavor's validation was
        # written against.
        raise ZeroExArtifactError(
            "allowance-holder quote unexpectedly carries a permit2 payload"
        )

    sell_amount = _uint(
        raw.get("sellAmount"), field_name="sellAmount", allow_zero=False
    )
    buy_amount = _uint(raw.get("buyAmount"), field_name="buyAmount", allow_zero=False)
    min_buy = _uint(
        raw.get("minBuyAmount"), field_name="minBuyAmount", allow_zero=False
    )
    if min_buy > buy_amount:
        raise ZeroExArtifactError(
            f"minBuyAmount {min_buy} exceeds buyAmount {buy_amount} — the floor is "
            "above the estimate, so the quote is internally inconsistent"
        )

    return ZeroExQuote(
        flavor=flavor,
        chain_id=chain_id,
        zid=raw.get("zid") if isinstance(raw.get("zid"), str) else None,
        block_number=_uint(raw.get("blockNumber"), field_name="blockNumber"),
        retrieved_at=retrieved_at or datetime.now(timezone.utc),
        sell_token=normalize_address(raw.get("sellToken"), field_name="sellToken"),
        buy_token=normalize_address(raw.get("buyToken"), field_name="buyToken"),
        sell_amount=sell_amount,
        buy_amount=buy_amount,
        minimum_buy_amount=min_buy,
        to=normalize_address(tx.get("to"), field_name="transaction.to"),
        data=data,
        value=_uint(tx.get("value"), field_name="transaction.value"),
        gas=_uint(tx.get("gas"), field_name="transaction.gas", allow_zero=False),
        gas_price=_uint(tx.get("gasPrice"), field_name="transaction.gasPrice"),
        allowance_target=normalize_address(
            raw.get("allowanceTarget"), field_name="allowanceTarget"
        ),
        allowance_actual=allowance_actual,
        taker=normalize_address(taker, field_name="taker"),
        permit2_eip712=permit2_eip712,
        buy_tax_bps=_uint(buy_meta.get("buyTaxBps", 0), field_name="buyTaxBps"),
        sell_tax_bps=_uint(sell_meta.get("sellTaxBps", 0), field_name="sellTaxBps"),
        raw=raw,
    )
