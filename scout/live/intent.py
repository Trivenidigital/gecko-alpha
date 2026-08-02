"""Canonical immutable trade intent, bound to its own content by hash.

The gap this closes
-------------------
``scout/live/engine.py`` mints ``intent_uuid = str(uuid4())`` and
``scout/live/idempotency.py`` folds that into
``client_order_id = f"gecko-{paper_trade_id}-{intent_uuid}"``. That is a stable *identity*
for a submission — though note it is not by itself a double-fill guarantee: Kraken's
duplicate-``cl_ord_id`` semantics are explicitly undocumented and the Kraken safety story
does not rest on venue-side dedup (``kraken_adapter.py`` fact 11). What that identity does
not capture at all is the *terms*: the amount, side, asset, chain, recipient,
venue and slippage bound are nowhere in the identity. An intent whose amount changed
between approval and submission carries the same ``intent_uuid`` and the same
``client_order_id``, and no downstream check can tell.

``intent_hash`` is that binding. It is a SHA-256 over a canonical serialization of every
material field, so any change to any term yields a different intent — a different
``intent_id``, a different ``client_order_id``, and a mismatch against whatever hash an
authorization was recorded against.

This mirrors the property the Solana lane already enforces at the transaction layer
(``scout/live/solana_lane.py``: authorization binds the transaction *message hash*, and
the funded key is not read until after it matches). This module lifts the same discipline
one level up, to the venue- and chain-neutral intent, so CEX and DEX paths share it.

Canonicalization rules — each exists because the naive choice is unsound:

* **Decimals compare by exact value, not repr and not rounded value.**
  ``Decimal("0.10")`` and ``Decimal("0.1")`` are one quantity; keying on ``str()`` would
  mint two identities for one trade. Canonicalization is built from ``as_tuple()`` rather
  than ``normalize()`` precisely because ``normalize()`` rounds to the ambient decimal
  context — see ``_canon_decimal``.
* **Non-finite decimals are rejected.** ``Decimal("Infinity") <= 0`` is ``False``, so an
  infinite ``maximum_notional`` would satisfy every positivity guard and then compare
  greater than any size — a field named "maximum" that imposes no maximum.
* **Datetimes must be timezone-aware, and are normalized to UTC.** A naive datetime
  canonicalizes ambiguously — two hosts in different zones would hash the same wall-clock
  string to the same intent.
* **Sequences must be tuples.** A list field would let a caller mutate the terms after the
  hash was taken.
* **Derived fields are excluded from their own input.** ``intent_hash``, ``intent_id`` and
  ``client_order_id`` are computed, never hashed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

__all__ = ["TradeIntent"]

SIDES = frozenset({"buy", "sell"})
ORDER_TYPES = frozenset({"market", "limit"})
VENUE_FAMILIES = frozenset({"cex", "dex"})
QUANTITY_DENOMINATIONS = frozenset({"base", "quote"})
INSTRUMENT_TYPES = frozenset({"spot"})

# 10000 bps = 100%. A bound with only a floor is not a bound.
MAX_BPS = 10_000

# Modes are mirrored from scout.live.solana_lane rather than imported: this module
# must stay importable by CEX paths that never load the Solana lane. The lane remains
# authoritative for what each mode *permits*; this set only constrains what an intent
# may claim.
MODES = frozenset(
    {"DISABLED", "SIMULATION_ONLY", "SUPERVISED_LIVE", "BOUNDED_AUTONOMOUS"}
)


def _canon_decimal(value: Decimal) -> str:
    """Canonical value-based decimal form. Pure function of the value.

    Derived from ``as_tuple()`` — sign, digit tuple, exponent — with trailing
    zeros stripped by hand, then rendered positionally.

    It deliberately does NOT use ``normalize()``. ``Decimal.normalize()`` consults
    the ambient thread-local ``decimal.getcontext()`` and rounds to ``prec``
    (default 28), which is both lossy and context-dependent:

    * ``Decimal("10000000000123456789012345678")`` and the value one unit larger
      both round to ...680 at prec=28, so two materially different quantities
      canonicalize identically and collide to one ``intent_hash``. Wei-denominated
      amounts of an 18-decimal token cross 28 digits above ~10 whole tokens, which
      is squarely inside this project's cohort.
    * The same value hashes differently under ``localcontext(prec=9)`` vs 28 vs 50,
      and ``canonical_form()`` re-reads the context at call time — so a verifier
      recomputing the hash could see a false mismatch on an untampered intent.

    ``as_tuple()`` is unaffected by context, so canonicalization is now total.
    """
    if not value.is_finite():
        raise ValueError(f"non-finite Decimal is not canonicalizable: {value!r}")
    sign, digits, exponent = value.as_tuple()
    digit_str = "".join(str(d) for d in digits)

    if digit_str.strip("0") == "":
        return "0"  # 0, -0, 0.00 all converge; no signed zero

    # Strip trailing zeros by raising the exponent, so 0.10 -> 0.1 and 100.00 -> 100.
    while exponent < 0 and digit_str.endswith("0"):
        digit_str = digit_str[:-1]
        exponent += 1

    if exponent >= 0:
        out = digit_str + "0" * exponent
    else:
        frac = -exponent
        if len(digit_str) <= frac:
            out = "0." + "0" * (frac - len(digit_str)) + digit_str
        else:
            out = digit_str[: len(digit_str) - frac] + "." + digit_str[len(digit_str) - frac :]

    return ("-" if sign else "") + out


def _canon_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _canon(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _canon_decimal(value)
    if isinstance(value, datetime):
        return _canon_datetime(value)
    if isinstance(value, tuple):
        return [_canon(v) for v in value]
    if isinstance(value, (str, int, bool, type(None))):
        return value
    raise TypeError(f"non-canonicalizable field type: {type(value).__name__}")


@dataclass(frozen=True)
class TradeIntent:
    """One decision, one intended execution, bound to its own terms.

    Construct by keyword. ``intent_hash`` / ``intent_id`` / ``client_order_id`` are
    derived in ``__post_init__`` and must not be passed.
    """

    # --- identity of the deciding process -----------------------------------
    strategy_id: str
    decision_id: str

    # --- lifetime ------------------------------------------------------------
    created_at: datetime
    expires_at: datetime
    execution_deadline: datetime

    # --- authority -----------------------------------------------------------
    mode: str
    policy_version: str

    # --- venue ---------------------------------------------------------------
    venue_family: str
    preferred_venue: str

    # --- instrument ----------------------------------------------------------
    base_asset: str
    quote_asset: str
    side: str
    exact_quantity: Decimal
    quantity_denomination: str
    maximum_notional: Decimal
    order_type: str

    # --- execution bounds ----------------------------------------------------
    maximum_slippage_bps: int
    maximum_price_impact_bps: int

    # --- optional, but material when present ---------------------------------
    instrument_type: str = "spot"
    allowed_fallback_venues: tuple[str, ...] = ()
    chain: str | None = None
    network_id: int | None = None
    base_contract: str | None = None
    quote_contract: str | None = None
    limit_price: Decimal | None = None
    minimum_output: Decimal | None = None
    recipient: str | None = None
    wallet: str | None = None
    signer_identity: str | None = None
    reduce_only: bool = False
    position_id: str | None = None
    provider_version: str | None = None
    reconciliation_requirements: tuple[str, ...] = ()

    # --- derived (never inputs to the hash) ----------------------------------
    intent_hash: str = field(init=False, repr=False)
    intent_id: str = field(init=False)
    client_order_id: str = field(init=False)

    # ------------------------------------------------------------------
    def __post_init__(self) -> None:
        # Name-mangled: a subclass overriding `_validate` to a no-op could otherwise
        # mint a well-formed hash over invalid terms (negative quantity, arbitrary side).
        self.__validate()
        digest = hashlib.sha256(
            json.dumps(
                self.canonical_form(), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        object.__setattr__(self, "intent_hash", digest)
        object.__setattr__(self, "intent_id", f"gi-{digest[:16]}")
        # Kraken's cl_ord_id accepts exactly three forms (kraken_adapter.py:90-97): a
        # dashed UUID, 32 hex characters, or free text of at most 18 ASCII characters.
        # The 32-hex form is verified accepted by Kraken's own `_validate_cl_ord_id`
        # via `_CL_ORD_ID_SHORT_UUID_RE`.
        #
        # UNRESOLVED, and deliberately recorded rather than papered over: this is 32
        # chars, but `scout/live/idempotency.py` defines CLIENT_ORDER_ID_BINANCE_MAX_LEN
        # = 28 and truncates defensively, and kraken_adapter.py / kraken_pilot.py repeat
        # 28. Binance's documented newClientOrderId limit is larger than 28, so the
        # in-tree 28 is likely self-imposed rather than a venue limit — but that is not
        # verified against a cited Binance doc, and 32 > 28 either way. Until it is
        # verified, a Binance caller must mint its id via idempotency.make_client_order_id
        # rather than using this field. Nothing consumes this field yet, so no venue is
        # currently at risk; wiring it to Binance requires resolving 28-vs-32 first.
        object.__setattr__(self, "client_order_id", digest[:32])

    # ------------------------------------------------------------------
    def canonical_form(self) -> dict[str, Any]:
        """The exact structure that is hashed. Every ``init=True`` field, canonicalized.

        Derived fields are excluded by the ``f.init`` filter rather than by an
        explicit denylist, so a future derived field cannot accidentally become
        self-referential input.
        """
        return {
            f.name: _canon(getattr(self, f.name)) for f in fields(self) if f.init
        }

    def is_expired(self, *, at: datetime) -> bool:
        """Inclusive at the boundary: an intent is expired *at* ``expires_at``.

        Expiry is a policy verdict, not a construction error — an expired intent
        still hashes, so recovery can re-identify it.
        """
        if at.tzinfo is None:
            raise ValueError("`at` must be timezone-aware")
        return at >= self.expires_at

    # ------------------------------------------------------------------
    def verify(self) -> bool:
        """Recompute the hash from the current field values and compare.

        Required at every deserialization boundary. A row read back from the DB, a
        pickle, or an ``object.__setattr__`` poke can carry terms that no longer match
        the stored ``intent_hash``; without this there is no API that can detect it,
        which would defeat the module's whole purpose.
        """
        try:
            recomputed = hashlib.sha256(
                json.dumps(
                    self.canonical_form(), sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
        except (ValueError, TypeError):
            return False
        return (
            recomputed == self.intent_hash
            and self.intent_id == f"gi-{recomputed[:16]}"
            and self.client_order_id == recomputed[:32]
        )

    def __validate(self) -> None:
        for f in fields(self):
            if not f.init:
                continue
            value = getattr(self, f.name)
            if isinstance(value, (list, set, dict)):
                raise TypeError(
                    f"{f.name} must be an immutable tuple, got {type(value).__name__} "
                    "— a mutable container lets terms change after hashing"
                )

        for name in ("created_at", "expires_at", "execution_deadline"):
            if getattr(self, name).tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware (missing timezone)")

        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        if self.execution_deadline < self.created_at:
            raise ValueError("execution_deadline must not precede created_at")

        if self.side not in SIDES:
            raise ValueError(f"side must be one of {sorted(SIDES)}, got {self.side!r}")
        if self.order_type not in ORDER_TYPES:
            raise ValueError(f"order_type must be one of {sorted(ORDER_TYPES)}")
        if self.venue_family not in VENUE_FAMILIES:
            raise ValueError(f"venue_family must be one of {sorted(VENUE_FAMILIES)}")
        if self.quantity_denomination not in QUANTITY_DENOMINATIONS:
            raise ValueError(
                f"quantity_denomination must be one of {sorted(QUANTITY_DENOMINATIONS)}"
            )
        if self.instrument_type not in INSTRUMENT_TYPES:
            raise ValueError(
                f"instrument_type must be one of {sorted(INSTRUMENT_TYPES)} — "
                "derivatives are a separate capability, not a spot intent"
            )
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {sorted(MODES)}")

        # Finiteness FIRST: Decimal("Infinity") <= 0 is False, so an infinite value
        # satisfies every positivity guard below and then compares greater than any
        # size downstream. NaN raises InvalidOperation (an ArithmeticError, not a
        # ValueError) on comparison, which escapes callers catching ValueError/TypeError.
        for name in (
            "exact_quantity",
            "maximum_notional",
            "limit_price",
            "minimum_output",
        ):
            value = getattr(self, name)
            if value is not None and not value.is_finite():
                raise ValueError(f"{name} must be a finite Decimal, got {value!r}")

        if self.exact_quantity <= 0:
            raise ValueError("exact_quantity must be positive")
        if self.maximum_notional <= 0:
            raise ValueError("maximum_notional must be positive")
        if self.maximum_slippage_bps < 0 or self.maximum_price_impact_bps < 0:
            raise ValueError("slippage and price-impact bounds must be non-negative")
        # Floored-only bounds are not bounds: 10**9 bps was a valid "limit".
        if self.maximum_slippage_bps > MAX_BPS or self.maximum_price_impact_bps > MAX_BPS:
            raise ValueError(
                f"slippage and price-impact bounds must not exceed {MAX_BPS} bps (100%)"
            )

        if self.execution_deadline > self.expires_at:
            raise ValueError(
                "execution_deadline must not outlive expires_at — otherwise is_expired() "
                "reports True while a deadline-checking caller still sees runway"
            )

        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("limit orders require a limit_price")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("limit_price must be positive")
        if self.minimum_output is not None and self.minimum_output <= 0:
            raise ValueError("minimum_output must be positive")

        if self.venue_family == "dex" and not self.chain:
            raise ValueError("dex intents require a chain")

        # A reduce-only exit that names no position cannot be checked against the
        # held size — that is how an "exit" becomes an unbounded sell.
        if self.reduce_only and not self.position_id:
            raise ValueError("reduce_only intents require a position_id")

        if self.preferred_venue in self.allowed_fallback_venues:
            raise ValueError(
                "preferred_venue must not repeat in allowed_fallback_venues"
            )


# Re-exported so callers can catch the frozen-mutation error without importing
# dataclasses directly.
FrozenIntentError = dataclasses.FrozenInstanceError
