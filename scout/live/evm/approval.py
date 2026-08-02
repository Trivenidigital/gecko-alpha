"""ERC-20 approval as its OWN execution, never a hidden part of the swap.

Why it is separate
------------------
An approval is a distinct, independently-durable grant: it survives the swap, it
can be exercised by the spender at any later time, and it is what an attacker
inherits if the spender is ever compromised. Folding it into "the swap" would
mean one authorization covering two capabilities with different lifetimes — and
the longer-lived one invisible.

So it gets its own intent, its own hash, its own simulation, its own
authorization, its own receipt and its own reconciliation. And after it lands the
swap must be RE-QUOTED, because blocks have passed and the quote that motivated
the approval is stale by construction.

Bounded, never unlimited
------------------------
The default is the exact amount the swap needs. Unlimited approval
(``2**256 - 1``) is the industry's convenient habit and is disabled here: it
converts a one-trade grant into a standing withdrawal right over the whole
balance, for as long as nobody remembers to revoke it. :data:`UNLIMITED` exists
only so the guard can name what it refuses.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from eth_abi import encode as abi_encode
from eth_utils import keccak

from scout.live.evm.flows import assert_spender_is_not_settler

__all__ = [
    "ApprovalIntent",
    "build_approval_intent",
    "APPROVE_SELECTOR",
    "UNLIMITED",
    "ApprovalRefused",
]

#: `approve(address,uint256)` — derived, not copied.
APPROVE_SELECTOR = "0x" + keccak(text="approve(address,uint256)")[:4].hex()

#: The value this module exists to refuse.
UNLIMITED = 2**256 - 1

#: How much headroom over the exact swap amount is tolerated. Small and explicit:
#: some tokens round on transfer, but a "bounded" approval an order of magnitude
#: above the trade is not bounded in any sense that matters.
DEFAULT_HEADROOM_BPS = Decimal("100")  # 1%


class ApprovalRefused(ValueError):
    """The approval would grant more, or to someone else, than the trade needs."""


@dataclass(frozen=True)
class ApprovalIntent:
    """One ERC-20 approval, bound to its own content hash.

    Deliberately shaped like :class:`~scout.live.intent.TradeIntent`: separate
    identity, separate hash, separate authorization. Nothing here signs.
    """

    chain_id: int
    owner: str
    token: str
    spender: str
    amount: int
    reason: str
    created_at: datetime
    expires_at: datetime

    intent_hash: str = field(init=False)
    calldata: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "calldata", self._encode())
        object.__setattr__(self, "intent_hash", self._hash())

    def _encode(self) -> str:
        body = abi_encode(
            ["address", "uint256"],
            [bytes.fromhex(self.spender[2:]), self.amount],
        )
        return APPROVE_SELECTOR + body.hex()

    def _hash(self) -> str:
        payload = {
            "chain_id": self.chain_id,
            "owner": self.owner,
            "token": self.token,
            "spender": self.spender,
            "amount": str(self.amount),
            "reason": self.reason,
            "created_at": self.created_at.astimezone(timezone.utc).isoformat(),
            "expires_at": self.expires_at.astimezone(timezone.utc).isoformat(),
            "calldata": self.calldata,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @property
    def is_unlimited(self) -> bool:
        return self.amount >= UNLIMITED

    def is_expired(self, *, at: datetime) -> bool:
        if at.tzinfo is None:
            raise ValueError("`at` must be timezone-aware")
        return at >= self.expires_at


def build_approval_intent(
    *,
    chain_id: int,
    owner: str,
    token: str,
    spender: str,
    required_amount: int,
    inner_target: str | None,
    headroom_bps: Decimal = DEFAULT_HEADROOM_BPS,
    now: datetime | None = None,
    ttl: timedelta = timedelta(minutes=10),
) -> ApprovalIntent:
    """Build a tightly-bounded approval, or refuse.

    ``spender`` MUST come from the validated 0x response (``allowanceTarget`` /
    ``issues.allowance.spender``) and is re-checked here against the per-chain
    AllowanceHolder allowlist — and against the inner Settler target, which must
    never be approved.
    """
    owner = owner.lower()
    token = token.lower()
    spender = spender.lower()

    if required_amount <= 0:
        raise ApprovalRefused("required_amount must be positive")

    # *** NEVER APPROVE SETTLER, and never an unlisted address. ***
    assert_spender_is_not_settler(
        chain_id=chain_id, spender=spender, inner_target=inner_target
    )

    amount = int(
        Decimal(required_amount) * (Decimal(10_000) + headroom_bps) / Decimal(10_000)
    )
    if amount >= UNLIMITED:
        raise ApprovalRefused(
            "unlimited approval is disabled: it converts a one-trade grant into a "
            "standing withdrawal right over the entire balance for as long as "
            "nobody remembers to revoke it"
        )
    if amount < required_amount:  # pragma: no cover - arithmetic guard
        raise ApprovalRefused("computed approval is below the amount required")

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("`now` must be timezone-aware")

    return ApprovalIntent(
        chain_id=chain_id,
        owner=owner,
        token=token,
        spender=spender,
        amount=amount,
        reason=f"AllowanceHolder swap of {required_amount} {token}",
        created_at=moment,
        expires_at=moment + ttl,
    )
