"""Canonical immutable trade intent, bound to its own content by hash.

The gap this closes
-------------------
``scout/live/engine.py`` mints ``intent_uuid = str(uuid4())`` and
``scout/live/idempotency.py`` folds that into
``client_order_id = f"gecko-{paper_trade_id}-{intent_uuid}"``. That is idempotent with
respect to the *identifier* — resubmitting the same identifier cannot double-fill. It is
not idempotent with respect to the *terms*: the amount, side, asset, chain, recipient,
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

* **Decimals compare by value, not repr.** ``Decimal("0.10")`` and ``Decimal("0.1")`` are
  one quantity; keying on ``str()`` would mint two identities for one trade.
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

# Modes are mirrored from scout.live.solana_lane rather than imported: this module
# must stay importable by CEX paths that never load the Solana lane. The lane remains
# authoritative for what each mode *permits*; this set only constrains what an intent
# may claim.
MODES = frozenset(
    {"DISABLED", "SIMULATION_ONLY", "SUPERVISED_LIVE", "BOUNDED_AUTONOMOUS"}
)


def _canon_decimal(value: Decimal) -> str:
    """Canonical value-based decimal form.

    ``normalize()`` strips trailing zeros so 0.10 and 0.1 converge, but it also
    renders large integers in exponent form (``Decimal("100").normalize()`` is
    ``Decimal("1E+2")``). ``format(..., "f")`` forces positional notation, so
    100, 1E+2 and 100.00 all canonicalize to ``"100"``.
    """
    return format(value.normalize(), "f")


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
        self._validate()
        digest = hashlib.sha256(
            json.dumps(
                self.canonical_form(), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        object.__setattr__(self, "intent_hash", digest)
        object.__setattr__(self, "intent_id", f"gi-{digest[:16]}")
        # Kraken's cl_ord_id accepts exactly three forms (kraken_adapter.py:90-97):
        # a dashed UUID, 32 hex characters with no dashes, or free text of at most
        # 18 ASCII characters. Binance allows 36. The 32-hex form is the only one
        # that satisfies both venues AND stays derived from the hash, so a prefixed
        # id like "gecko<hex>" is not an option — it is neither hex nor <=18 and
        # `_validate_cl_ord_id` rejects it locally before any HTTP.
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
    def _validate(self) -> None:
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

        if self.exact_quantity <= 0:
            raise ValueError("exact_quantity must be positive")
        if self.maximum_notional <= 0:
            raise ValueError("maximum_notional must be positive")
        if self.maximum_slippage_bps < 0 or self.maximum_price_impact_bps < 0:
            raise ValueError("slippage and price-impact bounds must be non-negative")

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
