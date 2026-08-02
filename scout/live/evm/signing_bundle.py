"""``ExecutionSigningBundle`` — one hash the authorization binds to.

The generalization the operator specified
------------------------------------------
The Solana lane's rule is "authorization binds to the transaction MESSAGE HASH,
and the funded key is not read until after". That rule assumes ONE signature over
ONE message. Permit2 breaks that assumption: it needs an EIP-712 typed-data
signature AND an EVM transaction signature, and the first is inserted into the
calldata of the second. A rule that binds one message hash cannot describe that.

So the rule generalizes to::

    authorization or mandate binds to an exact ExecutionSigningBundle hash

The bundle commits to EVERYTHING the two signatures together will authorize —
including the deterministic transformation that inserts the first signature into
the payload of the second, because a change to THAT changes what gets broadcast
without changing any individual field.

What is committed, and why each one
-----------------------------------
``intent_hash``
    the decision this executes. Without it the bundle authorizes a swap, not
    *this* swap.
``provider`` / ``provider_version`` / ``adapter_version``
    a different quote source, or a different decoder, is a different security
    story even for byte-identical calldata.
``response_hash``
    SHA-256 over the COMPLETE 0x response. The artifact that was decoded,
    validated and simulated is provably the artifact that was signed — including
    fields this code does not yet read.
``chain_id`` / ``wallet``
    replay across chains, and signing from an account nobody approved. ``chainId``
    is absent from 0x's response, so it is bound from what WE requested.
``permit2_domain_hash`` / ``permit2_types_hash`` / ``permit2_message_hash``
    the typed-data payload, split so a diff names which part moved. Absent on the
    allowance-holder flavor, and absent is not zero — see ``_ABSENT``.
``token`` / ``spender`` / ``permitted_amount``
    what the approval actually grants, which is the part of a Permit2 swap that
    can outlive the trade.
``permit_nonce`` / ``permit_expiration``
    replay and unbounded lifetime.
``to`` / ``calldata_template_hash`` / ``value``
    the transaction target and payload. The TEMPLATE, not the final calldata,
    because the signature has not been inserted yet at bundle time.
``minimum_output`` / ``quote_block`` / ``quote_expires_at``
    the loss bound, and the freshness window 0x does not publish for the
    allowance-holder flavor.
``gas_ceiling`` / ``gas_price_ceiling``
    cost bounds. CEILINGS, not exact values — the same monotone rule the Kraken
    lane uses for fees: a cost that IMPROVED must not void an authorization the
    operator would obviously still give.
``insertion_algorithm_version``
    the deterministic signature-insertion transformation. Two different insertion
    algorithms produce two different final transactions from identical inputs, so
    a bundle that did not commit to it would authorize both.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "ExecutionSigningBundle",
    "INSERTION_ALGORITHM_VERSION",
    "ADAPTER_VERSION",
]

#: Bumped whenever the deterministic transformation that inserts the Permit2
#: signature into the swap calldata changes IN ANY WAY. It is committed to by the
#: bundle, so a bump invalidates every previously-minted authorization — which is
#: the point: the same inputs under a different insertion algorithm are a
#: different final transaction.
INSERTION_ALGORITHM_VERSION = "permit2-append-v1"

#: Bumped when THIS adapter's decoding or validation changes. Byte-identical
#: calldata validated by a different decoder is a different security story.
ADAPTER_VERSION = "zeroex-adapter-v1"

#: Distinct from the string "0" and from an empty string, so a field that is
#: legitimately ABSENT (the allowance-holder flavor has no Permit2 payload) can
#: never collide with a field that is present and zero. A bundle where "no
#: expiration" and "expires at 0" hash the same would authorize both.
_ABSENT = "\x00absent"


def _canon(value: Any, *, field_name: str) -> str:
    """Canonical string form. Total, and deliberately narrow."""
    if value is None:
        return _ABSENT
    if isinstance(value, bool):
        raise TypeError(f"{field_name}: bool is not a bindable term")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"{field_name} must be timezone-aware")
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    raise TypeError(f"{field_name}: {type(value).__name__} is not canonicalizable")


@dataclass(frozen=True)
class ExecutionSigningBundle:
    """Everything two signatures will jointly authorize, as one hashable object.

    Frozen, and ``bundle_hash`` is derived in ``__post_init__`` — it is never an
    input, so it cannot be supplied to make a bundle claim to be another one.
    """

    # --- the decision -------------------------------------------------------
    intent_hash: str

    # --- who produced and who validated the artifact ------------------------
    provider: str
    provider_version: str
    adapter_version: str
    response_hash: str

    # --- identity -----------------------------------------------------------
    chain_id: int
    wallet: str

    # --- the transaction ----------------------------------------------------
    to: str
    calldata_template_hash: str
    value: int

    # --- economics ----------------------------------------------------------
    sell_token: str
    buy_token: str
    sell_amount: int
    minimum_output: int
    quote_block: int
    quote_expires_at: datetime

    # --- cost ceilings ------------------------------------------------------
    gas_ceiling: int
    gas_price_ceiling: int

    # --- the insertion transformation ---------------------------------------
    insertion_algorithm_version: str = INSERTION_ALGORITHM_VERSION

    # --- Permit2: absent on the allowance-holder flavor ---------------------
    permit2_domain_hash: str | None = None
    permit2_types_hash: str | None = None
    permit2_message_hash: str | None = None
    permit_token: str | None = None
    permit_spender: str | None = None
    permitted_amount: int | None = None
    permit_nonce: int | None = None
    permit_expiration: int | None = None

    def __post_init__(self) -> None:
        self.__validate()
        object.__setattr__(self, "bundle_hash", self._compute_hash())

    # ------------------------------------------------------------------
    def canonical_form(self) -> dict[str, str]:
        """The exact structure that is hashed — every declared field."""
        return {
            f.name: _canon(getattr(self, f.name), field_name=f.name)
            for f in fields(self)
        }

    def _compute_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.canonical_form(), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()

    def verify(self) -> bool:
        """Recompute and compare. Required at every deserialization boundary."""
        try:
            return self._compute_hash() == getattr(self, "bundle_hash", None)
        except (TypeError, ValueError):
            return False

    @property
    def authorization_token(self) -> str:
        """The short token an operator retypes. Uppercase for legibility."""
        return self.bundle_hash[:8].upper()

    def is_expired(self, *, at: datetime) -> bool:
        if at.tzinfo is None:
            raise ValueError("`at` must be timezone-aware")
        return at >= self.quote_expires_at

    # ------------------------------------------------------------------
    def __validate(self) -> None:
        if not self.intent_hash:
            raise ValueError("intent_hash is required — a bundle must name a decision")
        if not self.response_hash:
            raise ValueError("response_hash is required")
        if self.chain_id <= 0:
            raise ValueError(f"chain_id must be positive, got {self.chain_id}")
        for name in ("wallet", "to", "sell_token", "buy_token"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.startswith("0x"):
                raise ValueError(f"{name} must be a 0x address, got {value!r}")
            if value != value.lower():
                # Compared as strings inside the hash, so a checksummed address
                # and its lowercase form would produce two different bundles for
                # one transaction.
                raise ValueError(f"{name} must be lowercased: {value}")
        if self.sell_amount <= 0:
            raise ValueError("sell_amount must be positive")
        if self.minimum_output <= 0:
            # A zero floor authorizes receiving nothing. The whole point of
            # binding it is that it bounds the loss.
            raise ValueError(
                "minimum_output must be positive — a zero floor bounds nothing"
            )
        if self.value < 0 or self.gas_ceiling <= 0 or self.gas_price_ceiling <= 0:
            raise ValueError("value must be non-negative; gas ceilings positive")
        if self.quote_expires_at.tzinfo is None:
            raise ValueError("quote_expires_at must be timezone-aware")
        if not self.insertion_algorithm_version:
            raise ValueError("insertion_algorithm_version is required")

        # Permit2 is all-or-nothing. A bundle carrying a spender but no expiration
        # would commit to half an approval and leave the other half unbound.
        permit_fields = (
            self.permit2_domain_hash,
            self.permit2_types_hash,
            self.permit2_message_hash,
            self.permit_token,
            self.permit_spender,
            self.permitted_amount,
            self.permit_nonce,
            self.permit_expiration,
        )
        present = [f is not None for f in permit_fields]
        if any(present) and not all(present):
            raise ValueError(
                "Permit2 terms are all-or-nothing: a bundle must commit to the "
                "domain, types, message, token, spender, amount, nonce AND "
                "expiration, or to none of them. Partial commitment leaves part "
                "of the approval unbound."
            )
        if self.permitted_amount is not None and self.permitted_amount <= 0:
            raise ValueError("permitted_amount must be positive when present")
        if self.permit_expiration is not None and self.permit_expiration <= 0:
            raise ValueError(
                "permit_expiration must be positive — an approval that never "
                "expires outlives the trade it was granted for"
            )

    @property
    def requires_permit2_signature(self) -> bool:
        return self.permit2_message_hash is not None
