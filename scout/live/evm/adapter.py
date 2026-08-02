"""``ZeroExAllowanceHolderAdapter`` — the flow wired into the neutral router.

Declared, not inferred
----------------------
``describe_capabilities`` reports what THIS adapter implements, which is the same
discipline every other adapter follows. It declares ``venue_family="dex"``,
``supports_unsigned_transaction=True`` and ``supports_simulation=True``, and it
declares NO money movement: withdrawal, transfer and sweep stay false because
this adapter cannot do them and must never be the reason something thinks it can.

The Permit2 flow is a SEPARATE adapter that does not exist. Its spec is declared
in ``flows.py`` with ``supports_unsigned_transaction=False``, and
:meth:`ZeroExAllowanceHolderAdapter.reject_flow` refuses any response that
identifies it — an adapter for a flow whose calldata has not been decoded would
be an adapter that cannot validate what it signs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog

from scout.live.capabilities import VenueCapabilities
from scout.live.evm.allowance_holder import (
    AllowanceHolderArtifact,
    build_allowance_holder_artifact,
)
from scout.live.evm.artifact import ZeroExArtifactError
from scout.live.evm.flows import (
    ALLOWANCE_HOLDER_BY_CHAIN,
    FLOW_SPECS,
    FlowNotSupported,
    UnknownFlow,
    ZeroExFlow,
    flow_for_endpoint,
)

log = structlog.get_logger(__name__)

__all__ = ["ZeroExAllowanceHolderAdapter", "ZeroExQuoteRequest"]


@dataclass(frozen=True)
class ZeroExQuoteRequest:
    """What the intent asks for. Compared against what 0x returns."""

    chain_id: int
    sell_token: str
    buy_token: str
    sell_amount: int
    taker: str


class ZeroExAllowanceHolderAdapter:
    """Validates 0x AllowanceHolder artifacts for the routing layer.

    Deliberately NOT an ``ExchangeAdapter``: that ABC is shaped around CEX order
    placement (``place_order_request`` / ``await_fill_confirmation`` / venue
    pairs), and forcing a DEX signing flow through it would mean implementing
    half its surface as ``NotImplementedError`` — the "declared capability that
    raises" shape ``VenueCapabilities`` exists to prevent. It exposes
    ``describe_capabilities`` so the same fail-closed gate applies.
    """

    venue_name = "zeroex-allowance-holder"

    def __init__(
        self,
        *,
        chain_id: int,
        http_fetch=None,
        max_fee_bps: Decimal | None = None,
    ) -> None:
        self.chain_id = chain_id
        self._fetch = http_fetch
        self._max_fee_bps = max_fee_bps

    # ------------------------------------------------------------------
    def describe_capabilities(self) -> VenueCapabilities:
        spec = FLOW_SPECS[ZeroExFlow.ALLOWANCE_HOLDER]
        supported = spec.supports_unsigned_transaction and self.supports_chain(
            self.chain_id
        )
        return VenueCapabilities(
            venue=self.venue_name,
            venue_family="dex",
            supports_market_orders=supported,
            supports_limit_orders=False,
            supports_reduce_only=False,
            supports_cancel=False,
            supports_client_order_id=False,
            supports_partial_fills=False,
            supports_unsigned_transaction=supported,
            supports_host_owned_signing=supported,
            supports_private_broadcast=False,
            supports_simulation=True,
            supports_testnet=False,
            supports_cross_chain=False,
            # Money movement: NOT declared, and this adapter cannot do it.
            supports_withdrawal=False,
            supports_transfer=False,
            supports_sweep=False,
        )

    @staticmethod
    def supports_chain(chain_id: int) -> bool:
        """A chain with no reviewed AllowanceHolder is a chain we cannot verify."""
        return bool(ALLOWANCE_HOLDER_BY_CHAIN.get(chain_id))

    # ------------------------------------------------------------------
    @staticmethod
    def reject_flow(endpoint_or_flow: str) -> ZeroExFlow:
        """Resolve to exactly one reviewed flow, refusing unsupported ones."""
        flow = flow_for_endpoint(endpoint_or_flow)
        spec = FLOW_SPECS[flow]
        if not spec.supports_unsigned_transaction:
            raise FlowNotSupported(flow, spec.refusal_reason or "not supported")
        return flow

    # ------------------------------------------------------------------
    def validate_response(
        self,
        raw: dict[str, Any],
        *,
        request: ZeroExQuoteRequest,
        retrieved_at: datetime | None = None,
    ) -> AllowanceHolderArtifact:
        """Every refusal the adapter is required to make, in one place."""
        if not self.supports_chain(request.chain_id):
            raise ZeroExArtifactError(
                f"chain {request.chain_id} has no reviewed AllowanceHolder address; "
                "refusing to validate an artifact we cannot check the spender of"
            )
        if request.chain_id != self.chain_id:
            raise ZeroExArtifactError(
                f"request is for chain {request.chain_id}, adapter is bound to "
                f"{self.chain_id}"
            )
        # A permit2 payload here is a different flow wearing this one's clothes.
        if isinstance(raw, dict) and "permit2" in raw:
            spec = FLOW_SPECS[ZeroExFlow.PERMIT2]
            raise FlowNotSupported(
                ZeroExFlow.PERMIT2, spec.refusal_reason or "not supported"
            )

        kwargs: dict[str, Any] = dict(
            chain_id=request.chain_id,
            taker=request.taker,
            expected_sell_token=request.sell_token,
            expected_buy_token=request.buy_token,
            expected_sell_amount=request.sell_amount,
            retrieved_at=retrieved_at,
        )
        if self._max_fee_bps is not None:
            kwargs["max_fee_bps"] = self._max_fee_bps
        artifact = build_allowance_holder_artifact(raw, **kwargs)
        log.info(
            "zeroex_artifact_validated",
            venue=self.venue_name,
            chain_id=artifact.chain_id,
            spender=artifact.allowance_spender,
            inner_target=artifact.decoded.inner_target,
            minimum_output=artifact.minimum_buy_amount,
            slippage_bps=str(artifact.slippage_bps()),
            requires_approval=artifact.requires_approval,
            block=artifact.block_number,
        )
        return artifact
