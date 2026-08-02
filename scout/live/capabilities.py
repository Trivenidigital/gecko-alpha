"""Declared venue capabilities.

Routing currently infers what a venue can do — a call is attempted and the venue's
answer decides. Inference has two failure modes that matter here:

* A venue silently acquires a capability it does not have, because one call
  happened to succeed under conditions that will not hold next time.
* A money-moving capability becomes reachable from a path that only meant to trade,
  because nothing in the type system separated "can place an order" from "can move
  funds off the exchange".

So capabilities are **declared, and undeclared means absent**. Every flag defaults to
``False``; an adapter that forgets to override ``describe_capabilities`` gets a
descriptor that permits nothing rather than one that permits everything.

``MONEY_MOVEMENT`` is enumerated separately and no trading path consults it. Transfers,
withdrawals and sweeps are not reachable through a normal trade intent — they are a
different authority, and this is where that separation is expressed in types rather
than in prose.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["VenueCapabilities"]


@dataclass(frozen=True)
class VenueCapabilities:
    """What a venue is declared to support. Absent flags are absent capabilities."""

    venue: str

    # --- order forms ---------------------------------------------------------
    supports_market_orders: bool = False
    supports_limit_orders: bool = False
    supports_reduce_only: bool = False
    supports_cancel: bool = False
    supports_client_order_id: bool = False
    supports_partial_fills: bool = False

    # --- execution model -----------------------------------------------------
    # supports_unsigned_transaction: the venue returns an artifact complete enough
    # to validate and sign ourselves — chainId, nonce, amountOutMin, expiry all
    # present. Nothing in tree declares this today; see
    # docs/minara/MINARA_DEX_CAPABILITY_MATRIX.md for why Minara does not qualify.
    supports_unsigned_transaction: bool = False
    supports_host_owned_signing: bool = False
    supports_private_broadcast: bool = False
    supports_simulation: bool = False

    # --- environment ---------------------------------------------------------
    supports_testnet: bool = False
    supports_cross_chain: bool = False

    # --- money movement — NOT trade authority. See MONEY_MOVEMENT. -----------
    supports_withdrawal: bool = False
    supports_transfer: bool = False
    supports_sweep: bool = False

    MONEY_MOVEMENT = frozenset(
        {"supports_withdrawal", "supports_transfer", "supports_sweep"}
    )

    def grants_money_movement(self) -> bool:
        """True if this venue is declared able to move funds off-venue.

        No trading path may call this to *enable* anything — it exists so that a
        connector carrying withdrawal authority can be reported and refused.
        """
        return any(getattr(self, name) for name in self.MONEY_MOVEMENT)

    def permits_order(
        self, *, order_type: str, reduce_only: bool
    ) -> tuple[bool, str | None]:
        """Whether an intent of this shape may be routed here.

        Returns ``(False, reason)`` rather than raising: the router needs to record
        why each venue was rejected, not abort on the first ineligible one.
        """
        if order_type == "market" and not self.supports_market_orders:
            return False, f"{self.venue} does not declare market orders"
        if order_type == "limit" and not self.supports_limit_orders:
            return False, f"{self.venue} does not declare limit orders"
        # Dropping reduce_only silently turns a bounded exit into a plain sell.
        if reduce_only and not self.supports_reduce_only:
            return False, f"{self.venue} does not declare reduce_only"
        return True, None
