"""Strict two-layer decode of AllowanceHolder calldata.

What is being defended against
------------------------------
The calldata is the thing a signature authorizes. Everything else in the response
— ``minBuyAmount``, ``buyToken``, the route summary — is the provider's
DESCRIPTION of that calldata, and a description is not a proof. So the bytes are
decoded independently and the response's claims are checked AGAINST them, never
the other way round.

The structure, verified on a live mainnet artifact (2026-08-02)::

    outer: exec(address operator, address token, uint256 amount,
                address target, bytes data)                     0x2213bc0b
             |
             +-- inner: execute((address recipient,
                                 address buyToken,
                                 uint256 minAmountOut),
                                bytes[] actions,
                                bytes32 affiliate)              0x1fff991f

``minAmountOut`` decoded from the inner call is the ONLY on-chain bound on what
this transaction may lose. It is compared for exact equality against the
response's ``minBuyAmount``; a mismatch means the number shown to a human is not
the number the chain will enforce.

Trailing bytes are refused rather than ignored
-----------------------------------------------
A strict decode requires the ABI arguments to be a whole number of 32-byte words.
Where they are not, this module REFUSES. It deliberately does not right-pad to
make a decoder accept the payload: padding is a guess about intent, the EVM's own
"reads past the end are zero" rule is about execution rather than about what was
meant, and a validator that pads until something parses has stopped validating.
(That refusal is exactly what disabled the Permit2 flow.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eth_abi import decode as abi_decode
from eth_abi.exceptions import DecodingError

from scout.live.evm.flows import (
    ALLOWANCE_HOLDER_BY_CHAIN,
    SETTLER_BY_CHAIN,
    ZeroExFlow,
)

__all__ = [
    "CalldataError",
    "DecodedAllowanceHolderCall",
    "decode_allowance_holder_calldata",
    "ALLOWED_INNER_SELECTORS",
]

_OUTER_SELECTOR = bytes.fromhex("2213bc0b")
_OUTER_TYPES = ["address", "address", "uint256", "address", "bytes"]

_INNER_SELECTOR = bytes.fromhex("1fff991f")
_INNER_TYPES = ["(address,address,uint256)", "bytes[]", "bytes32"]

#: Inner entrypoints this decoder has been reviewed against. Anything else is
#: refused — an unknown selector is an unreviewed contract method, and "0x sent
#: it" is not a security argument.
ALLOWED_INNER_SELECTORS = frozenset({"0x1fff991f"})

#: Settler action selectors seen on reviewed artifacts. Recorded and REPORTED
#: rather than enforced: 0x composes routes from many actions and a fixed
#: allowlist would refuse legitimate routes on the day liquidity moves. The
#: financial terms are bound by `minAmountOut`, `recipient` and `buyToken`, which
#: ARE enforced — an action cannot pay out somewhere else without changing those.
KNOWN_ACTION_SELECTORS = {
    "0xc1fb425e": "TRANSFER_FROM/ALLOWANCE_HOLDER_TRANSFER_FROM",
    "0x38c9c147": "BASIC/pool call",
    "0xaf72634f": "UNISWAPV3/route",
    "0x34ee90ca": "UNISWAPV2/route",
}


class CalldataError(ValueError):
    """The calldata cannot be trusted as the basis for a signature."""


@dataclass(frozen=True)
class DecodedAllowanceHolderCall:
    """Everything the two layers actually encode. Derived from bytes only."""

    # --- outer exec() -------------------------------------------------------
    operator: str
    sell_token: str
    sell_amount: int
    inner_target: str
    inner_selector: str

    # --- inner Settler execute() -------------------------------------------
    recipient: str
    buy_token: str
    minimum_amount_out: int
    affiliate: str
    action_selectors: tuple[str, ...]

    @property
    def unknown_action_selectors(self) -> tuple[str, ...]:
        return tuple(
            s for s in self.action_selectors if s not in KNOWN_ACTION_SELECTORS
        )


def _require_aligned(body: bytes, *, layer: str) -> None:
    remainder = len(body) % 32
    if remainder:
        raise CalldataError(
            f"{layer} ABI arguments are {len(body)} bytes — {remainder} past a "
            "32-byte boundary. Refusing rather than padding: padding is a guess "
            "about intent, and a validator that pads until something parses has "
            "stopped validating."
        )


def _addr(value: Any) -> str:
    return str(value).lower()


def decode_allowance_holder_calldata(
    data: str | bytes, *, chain_id: int
) -> DecodedAllowanceHolderCall:
    """Decode both layers, or refuse. Reads bytes; trusts nothing else.

    ``chain_id`` is used to check the inner target against the reviewed Settler
    allowlist — an inner call to an unlisted contract is an unreviewed
    destination for the tokens this transaction moves.
    """
    if isinstance(data, str):
        # `bytes.fromhex` raises a bare ValueError on malformed input, which a
        # caller writing `except CalldataError` would miss.
        text = data[2:] if data.startswith(("0x", "0X")) else data
        try:
            raw = bytes.fromhex(text)
        except ValueError as exc:
            raise CalldataError(f"calldata is not valid hex: {exc}") from exc
    else:
        raw = bytes(data)
    if len(raw) < 4:
        raise CalldataError("calldata is shorter than a 4-byte selector")
    if raw[:4] != _OUTER_SELECTOR:
        raise CalldataError(
            f"outer selector is 0x{raw[:4].hex()}, expected "
            f"0x{_OUTER_SELECTOR.hex()} (AllowanceHolder exec). An unreviewed "
            "entrypoint is not executable."
        )

    body = raw[4:]
    _require_aligned(body, layer="outer exec()")
    try:
        operator, token, amount, target, inner = abi_decode(_OUTER_TYPES, body)
    except (DecodingError, OverflowError, ValueError) as exc:
        raise CalldataError(f"outer exec() did not decode: {exc}") from exc

    if len(inner) < 4:
        raise CalldataError("inner call carries no selector")
    inner_selector = "0x" + inner[:4].hex()
    if inner_selector not in ALLOWED_INNER_SELECTORS:
        raise CalldataError(
            f"inner selector {inner_selector} is not a reviewed entrypoint "
            f"(allowed: {sorted(ALLOWED_INNER_SELECTORS)}). '0x sent it' is not a "
            "security argument."
        )

    inner_body = inner[4:]
    _require_aligned(inner_body, layer="inner Settler execute()")
    try:
        slippage, actions, affiliate = abi_decode(_INNER_TYPES, inner_body)
    except (DecodingError, OverflowError, ValueError) as exc:
        raise CalldataError(f"inner Settler execute() did not decode: {exc}") from exc

    recipient, buy_token, min_out = slippage

    # The inner call destination must be a reviewed Settler. It is NOT the
    # approval spender — see flows.assert_spender_is_not_settler — but it IS
    # where the tokens go once AllowanceHolder forwards them.
    settlers = SETTLER_BY_CHAIN.get(chain_id, frozenset())
    if not settlers:
        raise CalldataError(
            f"no reviewed Settler address is declared for chain {chain_id}"
        )
    if _addr(target) not in settlers:
        raise CalldataError(
            f"inner target {_addr(target)} is not a reviewed Settler on chain "
            f"{chain_id} ({sorted(settlers)})"
        )
    # `operator` is who AllowanceHolder pulls tokens on behalf of. On reviewed
    # artifacts it equals the target; a divergence is an unreviewed shape.
    if _addr(operator) != _addr(target):
        raise CalldataError(
            f"outer operator {_addr(operator)} differs from target "
            f"{_addr(target)}; this is not the reviewed AllowanceHolder shape"
        )

    action_selectors = tuple(
        "0x" + bytes(a)[:4].hex() if len(bytes(a)) >= 4 else "0x" for a in actions
    )
    # Per-ELEMENT, not list emptiness: three zero-length elements produce
    # ('0x','0x','0x'), which is truthy, so an emptiness check passed a payload
    # that performs nothing while claiming a minimum output.
    if not action_selectors or any(len(bytes(a)) < 4 for a in actions):
        raise CalldataError(
            "the inner call carries an action with no selector — a swap that "
            "performs no action cannot deliver the minimum output it claims"
        )

    return DecodedAllowanceHolderCall(
        operator=_addr(operator),
        sell_token=_addr(token),
        sell_amount=int(amount),
        inner_target=_addr(target),
        inner_selector=inner_selector,
        recipient=_addr(recipient),
        buy_token=_addr(buy_token),
        minimum_amount_out=int(min_out),
        affiliate="0x" + bytes(affiliate).hex(),
        action_selectors=action_selectors,
    )
