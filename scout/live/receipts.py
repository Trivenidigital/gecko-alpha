"""Provider-neutral execution receipts.

The problem
-----------
A CEX fill and a DEX swap are the same event — "the intent executed, here is the
proof" — recorded in two shapes that share no field names. A Binance fill is an
``OrderConfirmation`` with a ``venue_order_id``; a Solana swap is a
``solana_executions`` row whose identity is a transaction *signature* and whose
lifecycle has eleven states. Anything that wants to reconcile, recover or report
across both has to know both, which is how venue-specific assumptions leak into
venue-neutral code.

``ExecutionReceipt`` is the one shape. It is deliberately thin: identity, outcome,
and the raw provider payload kept intact underneath. It does not try to be a common
denominator of everything the two providers can say — only of what a reconciler needs
to decide whether an intent executed and whether the thing that executed was the
intent it claims.

The binding is the point
------------------------
*** A RECEIPT THAT NAMES AN INTENT IS NOT THE SAME AS A RECEIPT BOUND TO ONE. ***
``intent_hash`` and ``client_order_id`` arrive from different places — one from our
ledger, one echoed back by the venue — and nothing about holding both proves they
belong together. :meth:`ExecutionReceipt.verify_binding` re-derives the venue's id
form from the intent and compares. A mismatch means the row and the order are about
different trades, which is precisely the state a reconciler must not resume from.

**What it establishes, and where it stops.** The CEX direction is real: the venue
echoes back a ``client_order_id`` that must be the one the intent mints. The DEX
direction is NOT yet establishable and this module says so by answering ``False``
rather than by relaxing the standard — ``solana_executions`` carries no
``intent_hash``, so a DEX receipt's hash is supplied by the caller, and comparing a
value to the value the caller passed in verifies nothing. Closing that needs the
hash recorded on the execution row at write time. Until then a DEX receipt is
unverified, and unverified is what it reports.

Even on the CEX side the guarantee is bounded, and the bound is worth stating: the
id carries a PREFIX of the hash (22 hex at Binance, 32 at Kraken of 64), so the
check establishes that the id and the hash agree — not that the hash matches the
terms. ``TradeIntent.verify()`` is what checks terms against a hash, and it needs
the terms, which no ledger row stores.

Statuses are normalized, not reinterpreted
------------------------------------------
Both providers' terminal vocabularies are mapped onto five words. The mapping is
lossy on purpose and the loss is one-directional: every unrecognised provider status
becomes ``unknown``, never ``filled``. A status this module has not been taught is a
status it must not claim to understand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from scout.live.order_id import derives_from_intent

if TYPE_CHECKING:  # pragma: no cover
    from scout.live.adapter_base import OrderConfirmation
    from scout.live.intent import TradeIntent

__all__ = [
    "ExecutionReceipt",
    "RECEIPT_STATUSES",
    "receipt_from_order_confirmation",
    "receipt_from_solana_execution",
]

#: The five outcomes a reconciler branches on. ``unknown`` is a real answer — an
#: ambiguous submission is not a pending one, and treating it as pending is how a
#: transaction that may exist gets rebuilt.
RECEIPT_STATUSES = frozenset({"filled", "partial", "rejected", "pending", "unknown"})

#: CEX ``OrderConfirmation.status`` → receipt status. ``timeout`` maps to ``unknown``
#: rather than ``pending``: a request that timed out may have reached the matching
#: engine, and the difference between "not yet" and "cannot say" decides whether a
#: retry is safe.
_CEX_STATUS_MAP = {
    "filled": "filled",
    "partial": "partial",
    "rejected": "rejected",
    "pending": "pending",
    "timeout": "unknown",
}

#: Solana execution ``state`` → receipt status. ``landed`` / ``confirmed`` /
#: ``finalized`` / ``reconciled`` all mean the transaction executed; they differ in how
#: much of the accounting has been checked, which is a lifecycle question rather than
#: an outcome one. ``submission_unknown`` is the ambiguous case the lane exists to
#: resolve and maps to ``unknown``, never to ``rejected`` — a signature that may have
#: landed must not be reported as a failure.
_SOLANA_STATE_MAP = {
    "landed": "filled",
    "confirmed": "filled",
    "finalized": "filled",
    "reconciled": "filled",
    "failed": "rejected",
    "submission_unknown": "unknown",
    "quote_created": "pending",
    "transaction_built": "pending",
    "simulation_passed": "pending",
    "awaiting_authorization": "pending",
    "authorized": "pending",
    "signed": "pending",
    "submission_attempted": "unknown",
}


def _opt_decimal(value: Any) -> Decimal | None:
    """Parse to a finite ``Decimal`` or ``None``.

    Non-finite input yields ``None``, not an infinity: a receipt carrying
    ``fill_price=Infinity`` would satisfy every downstream positivity guard and then
    compare greater than any bound.
    """
    if value is None:
        return None
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return dec if dec.is_finite() else None


@dataclass(frozen=True)
class ExecutionReceipt:
    """One executed (or refused) intent, in the same shape whatever produced it."""

    venue: str
    venue_family: str
    status: str
    #: The gecko-side content-bound id. ``None`` for providers with no client-order-id
    #: concept — Solana has none, and inventing one would assert a binding the chain
    #: cannot corroborate.
    client_order_id: str | None
    #: The intent this receipt claims to satisfy. ``None`` on rows that predate
    #: intent binding; ``verify_binding`` refuses those rather than assuming.
    intent_hash: str | None
    #: The provider's own identity for the execution: a venue order id, or a Solana
    #: transaction signature. One field because a reconciler asks the same question of
    #: both — "what do I quote back to the provider to ask about this?"
    venue_ref: str | None
    filled_quantity: Decimal | None = None
    fill_price: Decimal | None = None
    raw: dict[str, Any] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.status not in RECEIPT_STATUSES:
            raise ValueError(
                f"status {self.status!r} is not one of {sorted(RECEIPT_STATUSES)}"
            )
        if self.venue_family not in ("cex", "dex"):
            raise ValueError(
                f"venue_family must be 'cex' or 'dex', got {self.venue_family!r}"
            )

    @property
    def is_terminal(self) -> bool:
        """``pending`` is the only non-terminal outcome.

        ``unknown`` is terminal for the purposes of *this pass*: nothing further will
        be learned by waiting on the same evidence, and the resolution path is a
        different question asked of the provider.
        """
        return self.status != "pending"

    def verify_binding(self, intent: "TradeIntent") -> bool:
        """Whether this receipt provably belongs to ``intent``.

        Both halves must hold: the recorded hash must equal the intent's, and the
        client order id must be the one that intent mints at this venue. Requiring
        both means neither a copied hash nor a coincidental id is sufficient.

        Returns ``False`` — never raises — when either field is absent. A receipt that
        cannot establish the binding has not established it, and callers fail closed
        on ``False`` rather than on an exception they might catch too broadly.
        """
        if self.intent_hash is None or self.intent_hash != intent.intent_hash:
            return False
        if self.client_order_id is None:
            # *** NO CLIENT ORDER ID MEANS NO BINDING THIS METHOD CAN ESTABLISH. ***
            # An earlier version returned True here for `venue_family == "dex"`,
            # reasoning that Solana has no client-order-id concept so the hash
            # alone had to do. That was wrong, and an adversarial review broke it
            # by executing the counterexample: `solana_executions` has no
            # `intent_hash` column, so the hash on a DEX receipt is whatever the
            # CALLER passed in — and `venue_ref`, the actual transaction
            # signature, is compared to nothing. A receipt built from trade A's
            # execution row and stamped with intent B's hash verified against
            # intent B.
            #
            # Comparing a value to the value the caller supplied is not
            # verification. So this answers False, which is the truth: the binding
            # has not been established. Making it establishable needs evidence the
            # ledger does not yet carry — an `intent_hash` recorded on
            # `solana_executions` at `record_execution` time, so the hash is
            # something the row asserts rather than something the caller claims.
            # Recorded as the follow-up rather than papered over with a True.
            return False
        return derives_from_intent(self.client_order_id, intent, self.venue)


def receipt_from_order_confirmation(
    confirmation: "OrderConfirmation",
    *,
    venue_family: str = "cex",
    intent_hash: str | None = None,
) -> ExecutionReceipt:
    """Normalize a CEX ``OrderConfirmation``.

    ``intent_hash`` is passed in rather than read off the confirmation because the
    venue does not echo it — it is ours, and it comes from the ledger row the
    submission was recorded under.
    """
    return ExecutionReceipt(
        venue=confirmation.venue,
        venue_family=venue_family,
        status=_CEX_STATUS_MAP.get(confirmation.status, "unknown"),
        client_order_id=confirmation.client_order_id,
        intent_hash=intent_hash,
        venue_ref=confirmation.venue_order_id,
        filled_quantity=_opt_decimal(confirmation.filled_qty),
        fill_price=_opt_decimal(confirmation.fill_price),
        raw=confirmation.raw_response,
    )


def receipt_from_solana_execution(
    row: dict[str, Any],
    *,
    venue: str = "jupiter",
    intent_hash: str | None = None,
) -> ExecutionReceipt:
    """Normalize a ``solana_executions`` row.

    The row's identity is its ``expected_signature`` — derived before submission and
    persisted before the bytes are sent, which is what makes it usable as the receipt
    reference even for a submission whose outcome is still unknown.
    """
    return ExecutionReceipt(
        venue=venue,
        venue_family="dex",
        status=_SOLANA_STATE_MAP.get(str(row.get("state")), "unknown"),
        client_order_id=None,
        intent_hash=intent_hash if intent_hash is not None else row.get("intent_hash"),
        venue_ref=row.get("expected_signature"),
        filled_quantity=_opt_decimal(row.get("filled_quantity")),
        fill_price=_opt_decimal(row.get("fill_price")),
        raw=dict(row),
    )
