"""The two 0x execution flows, modelled as DISTINCT types — never one shape.

Why they are separate
---------------------
An earlier draft had one ``ZeroExQuote`` with an optional ``permit2_eip712``
field. That is wrong, and the reason is not tidiness: the flows differ in the
addresses they authorize, the signatures they require, and the nonce and expiry
models that bound them. A generic artifact makes Permit2 validation *optional*,
which means an unvalidated Permit2 response can flow through the code path
written for a flow that needs no signature at all.

So each response must identify EXACTLY ONE reviewed flow and fail closed
otherwise.

*** THE APPROVAL-TARGET CORRECTION. ***
For AllowanceHolder the ERC-20 approval goes to the **AllowanceHolder** contract.
It does NOT go to Settler. 0x documents this explicitly and warns against
approving Settler, and the distinction is easy to get backwards because the
decoded outer ``exec`` call names Settler in two of its five fields::

    exec(operator, token, amount, target, data)
          ^Settler                  ^Settler   <- forwarded-to, NOT the spender

    response.allowanceTarget            = AllowanceHolder   <- APPROVE THIS
    response.issues.allowance.spender   = AllowanceHolder   <- and this agrees
    response.transaction.to             = AllowanceHolder

Verified on a live mainnet artifact (2026-08-02, Gecko unfunded taker): spender,
``allowanceTarget`` and ``transaction.to`` were all
``0x0000000000001ff3684f28c67538d4d072c22734`` while the decoded inner target was
``0x0889e9327b98d7d1be3c301a4585ff3330502c9a``. :func:`assert_spender_is_not_settler`
enforces that they must never be the same address.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "ZeroExFlow",
    "FlowSpec",
    "FLOW_SPECS",
    "ALLOWANCE_HOLDER_BY_CHAIN",
    "SETTLER_BY_CHAIN",
    "flow_for_endpoint",
    "assert_spender_is_not_settler",
    "FlowNotSupported",
    "UnknownFlow",
]


class ZeroExFlow(str, Enum):
    """The reviewed flows. A response that matches neither is refused."""

    ALLOWANCE_HOLDER = "allowance-holder"
    PERMIT2 = "permit2"


class UnknownFlow(ValueError):
    """The response does not identify exactly one reviewed flow."""


class FlowNotSupported(ValueError):
    """The flow is modelled and declared, but execution through it is disabled."""

    def __init__(self, flow: "ZeroExFlow", reason: str) -> None:
        super().__init__(f"0x flow {flow.value!r} is not supported: {reason}")
        self.flow = flow
        self.reason = reason


@dataclass(frozen=True)
class FlowSpec:
    """Everything that must be DECLARED about a flow before it can execute.

    Declared, not inferred — the same discipline ``VenueCapabilities`` applies.
    A flow whose ``supports_unsigned_transaction`` is False cannot be used no
    matter how well its metadata happens to parse.
    """

    flow: ZeroExFlow
    endpoint: str

    #: Whether execution through this flow is permitted AT ALL.
    supports_unsigned_transaction: bool
    refusal_reason: str | None

    #: Does the taker have to produce an EIP-712 typed-data signature?
    requires_eip712_signature: bool

    #: Which contract receives the ERC-20 approval. For AllowanceHolder this is
    #: the AllowanceHolder itself; approving Settler is the documented mistake.
    approval_target_role: str

    #: Which contract the transaction is sent to.
    transaction_target_role: str

    #: What the outer calldata must look like.
    expected_outer_selector: str
    expected_outer_signature: str

    #: What the inner call must look like, when there is one.
    expected_inner_selector: str | None
    expected_inner_signature: str | None

    #: Who is permitted to move the tokens once approved.
    spender_role: str

    #: How replay is prevented.
    nonce_model: str

    #: How the authorization goes stale.
    expiry_model: str

    #: How a produced signature is placed into what gets broadcast.
    signature_insertion: str

    #: What identifies the execution afterwards, for recovery/reconciliation.
    reconciliation_identifiers: tuple[str, ...]


#: `exec(address operator, address token, uint256 amount, address target, bytes data)`
#: — selector derived by keccak, not copied: 0x2213bc0b. Confirmed against a live
#: mainnet artifact.
_AH_OUTER_SIG = "exec(address,address,uint256,address,bytes)"
_AH_OUTER_SELECTOR = "0x2213bc0b"

#: `execute((address recipient, address buyToken, uint256 minAmountOut), bytes[]
#: actions, bytes32 affiliate)` — the Settler entrypoint AllowanceHolder forwards
#: to. Selector 0x1fff991f, likewise derived.
_SETTLER_SIG = "execute((address,address,uint256),bytes[],bytes32)"
_SETTLER_SELECTOR = "0x1fff991f"


FLOW_SPECS: dict[ZeroExFlow, FlowSpec] = {
    ZeroExFlow.ALLOWANCE_HOLDER: FlowSpec(
        flow=ZeroExFlow.ALLOWANCE_HOLDER,
        endpoint="/swap/allowance-holder/quote",
        supports_unsigned_transaction=True,
        refusal_reason=None,
        requires_eip712_signature=False,
        approval_target_role="allowance_holder",
        transaction_target_role="allowance_holder",
        expected_outer_selector=_AH_OUTER_SELECTOR,
        expected_outer_signature=_AH_OUTER_SIG,
        expected_inner_selector=_SETTLER_SELECTOR,
        expected_inner_signature=_SETTLER_SIG,
        spender_role="allowance_holder",
        nonce_model="evm_account_nonce",
        expiry_model="gecko_owned_wall_clock_and_block_drift",
        signature_insertion="none — a single transaction signature, nothing inserted",
        reconciliation_identifiers=("transaction_hash", "zid", "block_number"),
    ),
    ZeroExFlow.PERMIT2: FlowSpec(
        flow=ZeroExFlow.PERMIT2,
        endpoint="/swap/permit2/quote",
        # *** DECLARED, AND DISABLED. ***
        # The endpoint is reachable and the EIP-712 metadata parses. Neither is
        # sufficient: the transaction calldata has NOT been completely decoded —
        # a live artifact's args ran 4 bytes past a 32-byte boundary and a strict
        # decode of the element offsets did not resolve. Reporting this as
        # supported because the metadata parses would be exactly the "validate the
        # EIP-712 object while treating calldata as opaque" mistake.
        supports_unsigned_transaction=False,
        refusal_reason=(
            "real calldata decoder not yet proven — a live artifact's ABI args "
            "did not decode cleanly and the trailing bytes are unexplained. The "
            "EIP-712 payload parsing correctly is not sufficient to sign."
        ),
        requires_eip712_signature=True,
        approval_target_role="permit2",
        transaction_target_role="settler",
        expected_outer_selector=_SETTLER_SELECTOR,
        expected_outer_signature=_SETTLER_SIG,
        expected_inner_selector=None,
        expected_inner_signature=None,
        spender_role="settler",
        nonce_model="permit2_unordered_nonce",
        expiry_model="permit2_deadline_plus_gecko_freshness",
        signature_insertion=(
            "the EIP-712 signature is placed inside a TRANSFER_FROM action in the "
            "actions array — UNPROVEN, which is why this flow is disabled"
        ),
        reconciliation_identifiers=("transaction_hash", "zid", "permit_nonce"),
    ),
}


#: Per-chain approved contracts. An address not listed here is not approvable and
#: not callable, whatever a response claims — a response is an untrusted document,
#: and "the provider told us to approve it" is precisely the argument this
#: allowlist exists to refuse.
#:
#: Mainnet AllowanceHolder observed on live artifacts 2026-08-02.
ALLOWANCE_HOLDER_BY_CHAIN: dict[int, frozenset[str]] = {
    1: frozenset({"0x0000000000001ff3684f28c67538d4d072c22734"}),
}

#: Settler is versioned and 0x redeploys it, so several may be valid at once.
#: It is NEVER an approval target — only the inner call destination.
SETTLER_BY_CHAIN: dict[int, frozenset[str]] = {
    1: frozenset({"0x0889e9327b98d7d1be3c301a4585ff3330502c9a"}),
}


def flow_for_endpoint(value: str) -> ZeroExFlow:
    """Map a flavor string to exactly one reviewed flow, or refuse."""
    for flow in ZeroExFlow:
        if value == flow.value:
            return flow
    raise UnknownFlow(
        f"{value!r} identifies no reviewed 0x flow; known flows are "
        f"{[f.value for f in ZeroExFlow]}"
    )


def assert_spender_is_not_settler(
    *, chain_id: int, spender: str, inner_target: str | None
) -> None:
    """*** NEVER APPROVE SETTLER. ***

    0x documents this explicitly. The mistake is easy to make because the decoded
    outer ``exec`` call names Settler in its ``operator`` and ``target`` fields,
    and both sit right next to the token and amount — so reading the spender off
    the calldata instead of off ``allowanceTarget`` produces a Settler approval
    that looks perfectly derived.

    Enforced as an explicit inequality rather than left implicit in "we read the
    right field", because the whole failure mode is reading the wrong one.
    """
    spender = spender.lower()
    if inner_target is not None and spender == inner_target.lower():
        raise ValueError(
            f"the ERC-20 spender {spender} is the inner Settler target. 0x "
            "documents that Settler must NEVER be approved; the spender comes "
            "from allowanceTarget / issues.allowance.spender, which is the "
            "AllowanceHolder contract."
        )
    approved = ALLOWANCE_HOLDER_BY_CHAIN.get(chain_id)
    if not approved:
        raise ValueError(
            f"no approved AllowanceHolder address is declared for chain {chain_id}; "
            "refusing to approve an address on an unreviewed chain"
        )
    if spender not in approved:
        raise ValueError(
            f"spender {spender} is not the approved AllowanceHolder for chain "
            f"{chain_id} ({sorted(approved)}). A response asking us to approve an "
            "unlisted address is a response to refuse."
        )
    settlers = SETTLER_BY_CHAIN.get(chain_id, frozenset())
    if spender in settlers:
        raise ValueError(
            f"spender {spender} is a known Settler address on chain {chain_id} — "
            "never approve Settler"
        )
