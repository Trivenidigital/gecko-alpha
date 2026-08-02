"""Per-venue client order ids, derived from a ``TradeIntent``'s content hash.

Why this exists rather than one global id
-----------------------------------------
``scout/live/idempotency.py`` mints ``gecko-{paper_trade_id}-{uuid8}`` and truncates
it to ``CLIENT_ORDER_ID_BINANCE_MAX_LEN = 28``. Two problems, and they pull in
opposite directions:

* The id is derived from a **random** ``intent_uuid``, so it identifies a submission
  but says nothing about its terms. That is the gap ``TradeIntent.intent_hash``
  closes — and it stays open for as long as the id is not derived from that hash.
* One global 28-character shape does not fit every venue. Kraken accepts a dashed
  UUID, 32 hex characters, or free text of at most **18** ASCII characters
  (``kraken_adapter`` fact 10) — the 28-character form fits none of those, and
  ``_validate_cl_ord_id`` rejects it locally before any HTTP.

So the id is derived per venue, from the intent hash, into a form that venue is
documented (or verified) to accept. A venue with no declared form gets no id: the
lookup raises rather than guessing, because a guessed id is either a wasted signed
round trip or — worse — an id that the venue silently accepts and that no local
check can bind back to an intent.

The 28-vs-32 question, settled as far as the evidence goes
----------------------------------------------------------
Binance's spot REST documentation for ``POST /api/v3/order`` (fetched 2026-08-02,
https://developers.binance.com/docs/binance-spot-api-docs/rest-api/trading-endpoints)
describes ``newClientOrderId`` as "A unique id among open orders. Automatically
generated if not sent. Orders with the same ``newClientOrderID`` can be accepted only
when the previous one is filled, otherwise the order will be rejected." It states **no
maximum length and no character pattern**.

That does not source the 28. It is kept anyway, and the reasoning is recorded rather
than the number: an id *shorter* than an unknown limit cannot be rejected for length,
so 28 is the safe direction to be wrong in. Widening it would need a citation; keeping
it does not.

*** THE TWO FORMS ARE NOT EQUALLY STRONG, AND CALLERS SHOULD KNOW WHICH THEY HOLD. ***
Kraken's form carries 32 hex characters — the full 128-bit prefix of the intent hash.
Binance's carries 22, or 88 bits, because 6 characters go to the ``gecko-`` prefix that
makes our orders identifiable in a shared account. 88 bits is far beyond collision risk
at any volume this project will see, but it is a smaller number than 128 and the
asymmetry is deliberate, not an oversight.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from scout.live.intent import TradeIntent

__all__ = [
    "VenueOrderIdForm",
    "UnknownVenueOrderIdForm",
    "VENUE_ORDER_ID_FORMS",
    "client_order_id_for_venue",
    "derives_from_intent",
]

# The prefix exists so a human reading Kraken's or Binance's order list can tell our
# orders from anything else in the account. It costs hash characters, which is why the
# Kraken form — whose accepted shapes are exact and leave no room for a prefix — does
# without it.
_GECKO_PREFIX = "gecko-"


class UnknownVenueOrderIdForm(LookupError):
    """No client-order-id form is declared for this venue.

    Fail-closed on purpose. The alternative — falling back to some default shape —
    is how an id that a venue rejects (or, worse, accepts under different uniqueness
    semantics) reaches a signed request.
    """


@dataclass(frozen=True)
class VenueOrderIdForm:
    """How one venue's client order id is built from an intent hash.

    ``hash_chars`` and ``prefix`` are the whole specification: the id is
    ``prefix + intent_hash[:hash_chars]``. ``max_len`` is asserted against that at
    construction, so a form that cannot satisfy its own venue's limit fails at import
    rather than at submission.
    """

    venue: str
    prefix: str
    hash_chars: int
    max_len: int
    # Prose, not machinery: what makes this shape acceptable at this venue. Carried on
    # the object so the reason travels with the form instead of living only in a
    # comment somebody may not read before changing the numbers.
    source: str

    def __post_init__(self) -> None:
        if self.hash_chars <= 0:
            raise ValueError(f"{self.venue}: hash_chars must be positive")
        # A sha256 hexdigest is 64 characters. Asking for more would silently yield a
        # shorter id than declared, which would break the length arithmetic below.
        if self.hash_chars > 64:
            raise ValueError(
                f"{self.venue}: hash_chars={self.hash_chars} exceeds the 64 hex "
                "characters a sha256 digest provides"
            )
        rendered = len(self.prefix) + self.hash_chars
        if rendered > self.max_len:
            raise ValueError(
                f"{self.venue}: form renders {rendered} chars but max_len is "
                f"{self.max_len}"
            )

    def render(self, intent_hash: str) -> str:
        if len(intent_hash) < self.hash_chars:
            raise ValueError(
                f"{self.venue}: intent_hash is {len(intent_hash)} chars, form needs "
                f"{self.hash_chars}"
            )
        return self.prefix + intent_hash[: self.hash_chars]

    @property
    def entropy_bits(self) -> int:
        """Bits of intent hash the id carries. Reported so the asymmetry between
        venues is a number a caller can read, not a claim in a docstring."""
        return self.hash_chars * 4


VENUE_ORDER_ID_FORMS: dict[str, VenueOrderIdForm] = {
    # Kraken's short-UUID form: exactly 32 hex characters, no dashes. Verified
    # accepted by the adapter's own `_validate_cl_ord_id` via
    # `_CL_ORD_ID_SHORT_UUID_RE` — tested against that function directly, not
    # against a restatement of it. No prefix: the form is exact and admits none.
    "kraken": VenueOrderIdForm(
        venue="kraken",
        prefix="",
        hash_chars=32,
        max_len=32,
        source=(
            "Kraken AddOrder cl_ord_id short-UUID form (32 hex, no dashes); "
            "kraken_adapter fact 10, enforced locally by _validate_cl_ord_id"
        ),
    ),
    # Binance: `gecko-` + 22 hex = 28 characters exactly, the self-imposed ceiling
    # carried over from idempotency.CLIENT_ORDER_ID_BINANCE_MAX_LEN. See the module
    # docstring for why 28 is retained despite being unsourced.
    "binance": VenueOrderIdForm(
        venue="binance",
        prefix=_GECKO_PREFIX,
        hash_chars=22,
        max_len=28,
        source=(
            "Binance spot POST /api/v3/order newClientOrderId; the published docs "
            "state no length limit (fetched 2026-08-02), so the project's own 28 is "
            "retained as the conservative direction"
        ),
    ),
}


def client_order_id_for_venue(intent: "TradeIntent", venue: str) -> str:
    """The client order id this intent must carry at ``venue``.

    Deterministic in the intent: the same terms always produce the same id, and any
    change to any term produces a different one. That is the property that makes the
    id a binding rather than a label — a mutated intent cannot reuse the id an
    authorization was recorded against.

    Raises ``UnknownVenueOrderIdForm`` for a venue with no declared form, and
    ``ValueError`` if the intent's own hash does not verify.
    """
    # An intent whose hash does not match its terms must not mint an id at all: the id
    # would assert a binding that verify() says is broken. Deserialization boundaries
    # are where this bites (a DB row, a pickle, an object.__setattr__ poke).
    if not intent.verify():
        raise ValueError(
            f"intent {intent.intent_id} fails verify(); its terms no longer match its "
            "hash, so no client order id may be derived from it"
        )
    try:
        form = VENUE_ORDER_ID_FORMS[venue]
    except KeyError:
        raise UnknownVenueOrderIdForm(
            f"no client-order-id form declared for venue {venue!r}; declared venues "
            f"are {sorted(VENUE_ORDER_ID_FORMS)}. Add a VenueOrderIdForm with a cited "
            "source before routing an order there."
        ) from None
    return form.render(intent.intent_hash)


def derives_from_intent(
    client_order_id: str, intent: "TradeIntent", venue: str
) -> bool:
    """Whether ``client_order_id`` is the one ``intent`` mints at ``venue``.

    The reconciliation direction. A receipt or a recovered row carries an id and an
    intent hash independently; this is what says they belong together. Returns
    ``False`` rather than raising for an unknown venue or an unverifiable intent —
    the caller is asking a yes/no question about evidence it already holds, and
    "cannot establish the binding" is a ``False`` answer, not an error condition.
    """
    try:
        return client_order_id == client_order_id_for_venue(intent, venue)
    except (UnknownVenueOrderIdForm, ValueError):
        return False
