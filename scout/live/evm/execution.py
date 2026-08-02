"""Bundle construction, simulation, broadcast interface, receipt and recovery.

Nothing here can broadcast in this build
-----------------------------------------
:class:`DisabledBroadcaster` is the only transport wired, and it raises. The
interface is complete and tested so that enabling it later is a configuration
decision reviewed on its own, rather than a scramble written under time pressure
on the day it is first needed.

The ordering that matters
-------------------------
    validate → simulate → bundle → GATE → load key → sign → persist → submit once

Persist BEFORE submit, always. A submitted transaction whose expected hash was
never written down is a transaction nobody can ask about afterwards — and "ask,
never re-send" is the only safe response to an ambiguous submission.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Protocol

import structlog

from scout.live.evm.allowance_holder import (
    DEFAULT_MAX_QUOTE_AGE_SECONDS,
    AllowanceHolderArtifact,
)
from scout.live.evm.signing_bundle import (
    ADAPTER_VERSION,
    ExecutionSigningBundle,
)

log = structlog.get_logger(__name__)

__all__ = [
    "ExecutionState",
    "SimulationResult",
    "Simulator",
    "SimulationRefused",
    "build_bundle_from_artifact",
    "EvmReceipt",
    "Broadcaster",
    "DisabledBroadcaster",
    "BroadcastRefused",
    "ReconciliationVerdict",
    "reconcile",
]

#: Insertion is a no-op for AllowanceHolder — a single transaction signature and
#: nothing spliced into calldata. Named explicitly so the bundle commits to the
#: fact that NOTHING is inserted, rather than leaving it implied by absence.
ALLOWANCE_HOLDER_INSERTION = "none-single-signature-v1"


class ExecutionState(str, Enum):
    """Every state a submission can be in. ``UNKNOWN`` is a real answer.

    Distinguishing NOT_SUBMITTED from UNKNOWN is the whole point: the first
    permits a rebuild, the second forbids it. Collapsing them is how a
    double-spend gets written.
    """

    NOT_SUBMITTED = "not_submitted"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FINALIZED = "finalized"
    REVERTED = "reverted"
    REPLACED = "replaced"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------


class SimulationRefused(Exception):
    """The transaction must not be signed."""


@dataclass(frozen=True)
class SimulationResult:
    succeeded: bool
    gas_used: int | None
    revert_reason: str | None
    buy_amount_out: int | None
    raw: dict[str, Any] | None = field(default=None, repr=False)


class Simulator(Protocol):
    """Independent simulation of the EXACT final calldata.

    Independent of 0x on purpose: ``issues.simulationIncomplete`` is the
    provider's own report on its own transaction, and a provider attesting to its
    own output is not a second opinion.
    """

    async def simulate(
        self,
        *,
        chain_id: int,
        from_address: str,
        to: str,
        data: str,
        value: int,
        gas: int,
    ) -> SimulationResult: ...


async def require_successful_simulation(
    simulator: Simulator, artifact: AllowanceHolderArtifact
) -> SimulationResult:
    """Simulate, and refuse anything that is not a clean success."""
    result = await simulator.simulate(
        chain_id=artifact.chain_id,
        from_address=artifact.taker,
        to=artifact.to,
        data=artifact.data,
        value=artifact.value,
        gas=artifact.gas,
    )
    if not result.succeeded:
        raise SimulationRefused(
            f"simulation reverted: {result.revert_reason or 'no reason given'} — "
            "refusing to sign a transaction that fails against current state"
        )
    if (
        result.buy_amount_out is not None
        and result.buy_amount_out < artifact.minimum_buy_amount
    ):
        raise SimulationRefused(
            f"simulation delivers {result.buy_amount_out}, below the enforced "
            f"minimum {artifact.minimum_buy_amount} — the route no longer clears "
            "its own floor"
        )
    return result


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------


def build_bundle_from_artifact(
    artifact: AllowanceHolderArtifact,
    *,
    intent_hash: str,
    wallet: str,
    gas_ceiling: int | None = None,
    gas_price_ceiling: int | None = None,
    max_age_seconds: float = DEFAULT_MAX_QUOTE_AGE_SECONDS,
) -> ExecutionSigningBundle:
    """The bundle for a validated AllowanceHolder artifact.

    ``calldata_template_hash`` is the hash of the FINAL calldata, because this
    flow inserts nothing — the template and the final payload are the same bytes.
    Committing to it anyway keeps the bundle's shape identical across flows, so a
    flow that DOES insert cannot quietly omit the commitment.
    """
    if wallet.lower() != artifact.taker:
        raise ValueError(
            f"wallet {wallet.lower()} is not the artifact's taker {artifact.taker}; "
            "the transaction would be signed by an account the quote was not built "
            "for, and its recipient check would be meaningless"
        )
    calldata_hash = hashlib.sha256(artifact.data.encode("utf-8")).hexdigest()
    return ExecutionSigningBundle(
        intent_hash=intent_hash,
        provider="0x",
        provider_version="v2-allowance-holder",
        adapter_version=ADAPTER_VERSION,
        response_hash=artifact.response_hash,
        chain_id=artifact.chain_id,
        wallet=artifact.taker,
        to=artifact.to,
        calldata_template_hash=calldata_hash,
        value=artifact.value,
        sell_token=artifact.sell_token,
        buy_token=artifact.buy_token,
        sell_amount=artifact.sell_amount,
        minimum_output=artifact.minimum_buy_amount,
        quote_block=artifact.block_number,
        quote_expires_at=artifact.expires_at(max_age_seconds=max_age_seconds),
        gas_ceiling=gas_ceiling or artifact.gas,
        gas_price_ceiling=gas_price_ceiling or max(artifact.gas_price, 1),
        insertion_algorithm_version=ALLOWANCE_HOLDER_INSERTION,
    )


# ---------------------------------------------------------------------------
# Receipt / broadcast / recovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvmReceipt:
    """The durable record of one EVM execution attempt.

    ``expected_transaction_hash`` is persisted BEFORE submission, which is what
    makes recovery possible at all: after an ambiguous send the only safe question
    is "did THIS hash land", and it cannot be asked if it was never written down.
    """

    intent_hash: str
    bundle_hash: str
    chain_id: int
    wallet: str
    expected_transaction_hash: str
    state: ExecutionState
    zid: str | None = None
    quote_block: int | None = None
    submitted_at: datetime | None = None
    observed_transaction_hash: str | None = None
    gas_used: int | None = None
    effective_gas_price: int | None = None
    buy_amount_received: int | None = None
    allowance_after: int | None = None
    detail: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in (
            ExecutionState.FINALIZED,
            ExecutionState.REVERTED,
            ExecutionState.NOT_SUBMITTED,
        )

    @property
    def blocks_the_lane(self) -> bool:
        """UNKNOWN and REPLACED block: a transaction that MAY exist must never be
        rebuilt, and a replacement means someone else moved the nonce."""
        return self.state in (ExecutionState.UNKNOWN, ExecutionState.REPLACED)


class BroadcastRefused(Exception):
    """Submission was refused. Nothing was sent."""


class Broadcaster(Protocol):
    async def submit(self, *, chain_id: int, signed_raw_tx: str) -> str: ...

    async def lookup(
        self, *, chain_id: int, transaction_hash: str
    ) -> EvmReceipt | None: ...


class DisabledBroadcaster:
    """The only transport wired in this build. It refuses.

    Present rather than absent so the whole path — persist, submit-once, recover —
    is exercised by tests against a real interface, and so enabling a live
    transport is a reviewed decision rather than new code written in a hurry.
    """

    def __init__(self, reason: str = "live broadcast is not authorized") -> None:
        self.reason = reason
        self.submit_calls = 0

    async def submit(self, *, chain_id: int, signed_raw_tx: str) -> str:
        self.submit_calls += 1
        raise BroadcastRefused(
            f"{self.reason} — refusing to broadcast on chain {chain_id}. "
            "Nothing was sent."
        )

    async def lookup(
        self, *, chain_id: int, transaction_hash: str
    ) -> EvmReceipt | None:
        return None


async def submit_once(
    *,
    broadcaster: Broadcaster,
    receipt: EvmReceipt,
    signed_raw_tx: str,
    persist,
) -> EvmReceipt:
    """Persist, then submit exactly once. Never blind-retry.

    ``persist`` is called BEFORE the transport is touched. If submission then
    fails ambiguously the row already names the hash to ask about, which is the
    difference between recovering and guessing.
    """
    if receipt.state is not ExecutionState.NOT_SUBMITTED:
        raise BroadcastRefused(
            f"receipt is already {receipt.state.value}; a second submission would "
            "be a second transaction, not a retry"
        )
    await persist(receipt)
    try:
        observed = await broadcaster.submit(
            chain_id=receipt.chain_id, signed_raw_tx=signed_raw_tx
        )
    except BroadcastRefused:
        raise
    except Exception as exc:
        # AMBIGUOUS. The transaction may or may not have reached a node, so the
        # lane blocks on UNKNOWN and the resolver asks about the expected hash.
        unknown = EvmReceipt(
            **{
                **receipt.__dict__,
                "state": ExecutionState.UNKNOWN,
                "detail": f"submission ambiguous: {type(exc).__name__}: {exc}",
                "submitted_at": datetime.now(timezone.utc),
            }
        )
        await persist(unknown)
        return unknown
    return EvmReceipt(
        **{
            **receipt.__dict__,
            "state": ExecutionState.PENDING,
            "observed_transaction_hash": observed,
            "submitted_at": datetime.now(timezone.utc),
        }
    )


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconciliationVerdict:
    reconciled: bool
    discrepancies: tuple[str, ...]
    trips_breaker: bool


def reconcile(
    *,
    receipt: EvmReceipt,
    artifact: AllowanceHolderArtifact,
    buy_balance_delta: int | None,
    sell_balance_delta: int | None,
    allowance_after: int | None,
    gas_used: int | None,
) -> ReconciliationVerdict:
    """Compare what was authorized against what actually happened.

    An unresolved discrepancy trips the connector breaker rather than being
    logged and moved past — the whole reason to reconcile is to stop trading on a
    connector whose behaviour no longer matches its description.
    """
    problems: list[str] = []

    if receipt.state is ExecutionState.REVERTED:
        return ReconciliationVerdict(False, ("transaction reverted",), True)
    if receipt.blocks_the_lane:
        return ReconciliationVerdict(False, (f"state is {receipt.state.value}",), True)

    if buy_balance_delta is not None:
        if buy_balance_delta < artifact.minimum_buy_amount:
            problems.append(
                f"received {buy_balance_delta} of {artifact.buy_token}, below the "
                f"enforced minimum {artifact.minimum_buy_amount}"
            )
    if sell_balance_delta is not None:
        spent = -sell_balance_delta
        if spent > artifact.sell_amount:
            problems.append(
                f"spent {spent} of {artifact.sell_token}, more than the authorized "
                f"{artifact.sell_amount}"
            )
    if allowance_after is not None and allowance_after > artifact.sell_amount:
        # A residual allowance larger than the trade is a standing grant nobody
        # asked for — the exact thing bounded approval exists to prevent.
        problems.append(
            f"residual allowance {allowance_after} exceeds the trade size "
            f"{artifact.sell_amount}; revoke it"
        )
    if gas_used is not None and gas_used > artifact.gas:
        problems.append(f"gas used {gas_used} exceeded the ceiling {artifact.gas}")

    return ReconciliationVerdict(
        reconciled=not problems,
        discrepancies=tuple(problems),
        trips_breaker=bool(problems),
    )
