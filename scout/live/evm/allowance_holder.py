"""The validated AllowanceHolder artifact — response and calldata cross-checked.

This is the flow-specific replacement for the generic quote object. Construction
is the validation: an ``AllowanceHolderArtifact`` cannot exist unless every check
below passed, so holding one IS the proof rather than something to re-derive.

The central discipline: the response's claims are checked AGAINST the decoded
bytes, never accepted alongside them. ``minBuyAmount`` is what a human is shown;
``minAmountOut`` inside the calldata is what the chain enforces. They must be
equal, exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from scout.live.evm.artifact import (
    ZeroExArtifactError,
    canonical_response_hash,
    normalize_address,
)
from scout.live.evm.calldata import (
    CalldataError,
    DecodedAllowanceHolderCall,
    decode_allowance_holder_calldata,
)
from scout.live.evm.flows import (
    FLOW_SPECS,
    ZeroExFlow,
    assert_spender_is_not_settler,
)

__all__ = [
    "AllowanceHolderArtifact",
    "build_allowance_holder_artifact",
    "DEFAULT_MAX_QUOTE_AGE_SECONDS",
    "DEFAULT_MAX_QUOTE_AGE_BLOCKS",
    "DEFAULT_MAX_ZEROEX_FEE_BPS",
]

#: Gecko-owned freshness. 0x publishes no quote expiry on this flow, so BOTH
#: bounds are ours: wall clock catches a stalled chain where blockNumber sits
#: still while off-chain prices move, block drift catches a busy chain moving the
#: route out from under us.
DEFAULT_MAX_QUOTE_AGE_SECONDS = 60.0
DEFAULT_MAX_QUOTE_AGE_BLOCKS = 5

#: Ceiling on what 0x may take, in bps of the buy amount. Bounded rather than
#: trusted: the fee is disclosed in the response, and a disclosed fee is still a
#: fee somebody has to have agreed to.
DEFAULT_MAX_ZEROEX_FEE_BPS = Decimal("50")

#: *** CEILING ON SLIPPAGE — the bound that was missing entirely. ***
#: `minimum_buy_amount` was BOUND everywhere and BOUNDED nowhere: fees were
#: capped at 50 bps while slippage was uncapped, so a response advertising
#: `buyAmount=187886590` with `minBuyAmount=1` — and calldata enforcing 1, so the
#: response/calldata comparison passed — validated cleanly, built a bundle and
#: cleared simulation, because simulation compares against the same degenerate
#: floor. A 99.99%-loss quote passed the entire orchestrated path.
#: 300 bps is generous for the pairs in scope and still refuses that.
DEFAULT_MAX_SLIPPAGE_BPS = Decimal("300")


@dataclass(frozen=True)
class AllowanceHolderArtifact:
    """A fully validated AllowanceHolder quote. Existence implies validation."""

    flow: ZeroExFlow

    # --- identity (ours; 0x echoes neither) ---------------------------------
    chain_id: int
    taker: str

    # --- provenance ---------------------------------------------------------
    zid: str | None
    block_number: int
    retrieved_at: datetime
    response_hash: str

    # --- economics, agreed by BOTH response and calldata --------------------
    sell_token: str
    buy_token: str
    sell_amount: int
    buy_amount: int
    minimum_buy_amount: int
    recipient: str

    # --- the transaction ----------------------------------------------------
    to: str
    data: str
    value: int
    gas: int
    gas_price: int

    # --- approval -----------------------------------------------------------
    allowance_spender: str
    allowance_actual: int

    # --- fees ---------------------------------------------------------------
    zeroex_fee_amount: int
    zeroex_fee_token: str | None
    integrator_fee_amount: int

    decoded: DecodedAllowanceHolderCall = field(repr=False)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------
    @property
    def requires_approval(self) -> bool:
        """A missing allowance is a SEPARATE signed operation, never folded in."""
        return self.allowance_actual < self.sell_amount

    def slippage_bps(self) -> Decimal:
        if self.buy_amount == 0:
            return Decimal(0)
        delta = Decimal(self.buy_amount - self.minimum_buy_amount)
        return (delta / Decimal(self.buy_amount)) * Decimal(10_000)

    def expires_at(
        self, *, max_age_seconds: float = DEFAULT_MAX_QUOTE_AGE_SECONDS
    ) -> datetime:
        return self.retrieved_at + timedelta(seconds=max_age_seconds)

    def assert_fresh(
        self,
        *,
        current_block: int,
        now: datetime | None = None,
        max_age_seconds: float = DEFAULT_MAX_QUOTE_AGE_SECONDS,
        max_age_blocks: int = DEFAULT_MAX_QUOTE_AGE_BLOCKS,
    ) -> None:
        """Both bounds, re-checked immediately before signing.

        A refreshed quote is a NEW artifact with a new response hash, hence a new
        bundle hash — re-quoting can never silently reuse an authorization.
        """
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            raise ValueError("`now` must be timezone-aware")
        age = (moment - self.retrieved_at).total_seconds()
        if age > max_age_seconds:
            raise ZeroExArtifactError(
                f"quote is {age:.1f}s old, ceiling {max_age_seconds}s — re-quote. "
                "0x publishes no expiry for this flow, so freshness is ours."
            )
        drift = current_block - self.block_number
        if drift > max_age_blocks:
            raise ZeroExArtifactError(
                f"quote is {drift} blocks behind (block {self.block_number}, now "
                f"{current_block}), ceiling {max_age_blocks} — re-quote."
            )
        if drift < 0:
            raise ZeroExArtifactError(
                f"the reported current block {current_block} precedes the quote's "
                f"own block {self.block_number}; the node is behind or lying"
            )


def _fee_amount(fees: Any, key: str) -> tuple[int, str | None]:
    if not isinstance(fees, dict):
        return 0, None
    entry = fees.get(key)
    if not isinstance(entry, dict):
        return 0, None
    amount = entry.get("amount")
    token = entry.get("token")
    try:
        return (int(str(amount)) if amount is not None else 0), (
            normalize_address(token, field_name=f"fees.{key}.token") if token else None
        )
    except (ValueError, ZeroExArtifactError):
        return 0, None


def build_allowance_holder_artifact(
    raw: dict[str, Any],
    *,
    chain_id: int,
    taker: str,
    expected_sell_token: str,
    expected_buy_token: str,
    expected_sell_amount: int,
    expected_min_buy_amount: int,
    retrieved_at: datetime | None = None,
    max_fee_bps: Decimal = DEFAULT_MAX_ZEROEX_FEE_BPS,
    max_slippage_bps: Decimal = DEFAULT_MAX_SLIPPAGE_BPS,
) -> AllowanceHolderArtifact:
    """Validate an AllowanceHolder response into an artifact, or raise.

    The ``expected_*`` arguments come from the TradeIntent. They are what we asked
    for; everything else is what 0x sent. Comparing the two is the point — an
    artifact that is internally consistent but describes a different trade than
    the intent is still the wrong artifact.
    """
    spec = FLOW_SPECS[ZeroExFlow.ALLOWANCE_HOLDER]
    if not spec.supports_unsigned_transaction:  # pragma: no cover - defensive
        raise ZeroExArtifactError("the AllowanceHolder flow is disabled")

    if not isinstance(raw, dict):
        raise ZeroExArtifactError(f"response is {type(raw).__name__}, not an object")
    if raw.get("liquidityAvailable") is False:
        raise ZeroExArtifactError("0x reports liquidityAvailable=false — no route")
    if "permit2" in raw:
        raise ZeroExArtifactError(
            "an AllowanceHolder response carries a permit2 payload; this is not "
            "the reviewed shape for this flow"
        )

    tx = raw.get("transaction")
    if not isinstance(tx, dict):
        raise ZeroExArtifactError("response has no `transaction` object")

    def _uint(value: Any, name: str, *, allow_zero: bool = True) -> int:
        if isinstance(value, bool) or value is None:
            raise ZeroExArtifactError(f"{name} is not an amount: {value!r}")
        try:
            parsed = int(str(value), 10)
        except (TypeError, ValueError) as exc:
            raise ZeroExArtifactError(f"{name} is not an integer: {value!r}") from exc
        if parsed < 0 or (parsed == 0 and not allow_zero):
            raise ZeroExArtifactError(f"{name} is {parsed}")
        return parsed

    sell_token = normalize_address(raw.get("sellToken"), field_name="sellToken")
    buy_token = normalize_address(raw.get("buyToken"), field_name="buyToken")
    sell_amount = _uint(raw.get("sellAmount"), "sellAmount", allow_zero=False)
    buy_amount = _uint(raw.get("buyAmount"), "buyAmount", allow_zero=False)
    min_buy = _uint(raw.get("minBuyAmount"), "minBuyAmount", allow_zero=False)
    if min_buy > buy_amount:
        raise ZeroExArtifactError(
            f"minBuyAmount {min_buy} exceeds buyAmount {buy_amount}"
        )

    # --- the intent's terms, not merely 0x's internal consistency -----------
    expected_sell = normalize_address(expected_sell_token, field_name="expected sell")
    expected_buy = normalize_address(expected_buy_token, field_name="expected buy")
    if sell_token != expected_sell:
        raise ZeroExArtifactError(
            f"response sells {sell_token}, intent asked for {expected_sell}"
        )
    if buy_token != expected_buy:
        raise ZeroExArtifactError(
            f"response buys {buy_token}, intent asked for {expected_buy}"
        )
    if sell_amount != expected_sell_amount:
        raise ZeroExArtifactError(
            f"response sells {sell_amount}, intent asked for {expected_sell_amount}"
        )

    to = normalize_address(tx.get("to"), field_name="transaction.to")
    allowance_target = normalize_address(
        raw.get("allowanceTarget"), field_name="allowanceTarget"
    )
    issues = raw.get("issues") or {}
    allowance = issues.get("allowance") if isinstance(issues, dict) else None
    if not isinstance(allowance, dict) or allowance.get("spender") is None:
        raise ZeroExArtifactError(
            "response carries no issues.allowance.spender; the ERC-20 spender "
            "cannot be established, and it must never be inferred from calldata"
        )
    spender = normalize_address(
        allowance.get("spender"), field_name="issues.allowance.spender"
    )
    if spender != allowance_target:
        raise ZeroExArtifactError(
            f"issues.allowance.spender {spender} disagrees with allowanceTarget "
            f"{allowance_target}; the two must name one address"
        )
    if to != allowance_target:
        raise ZeroExArtifactError(
            f"transaction.to {to} is not the allowanceTarget {allowance_target}; "
            "for this flow both are the AllowanceHolder contract"
        )

    # --- decode the bytes, then cross-check the response against them -------
    try:
        decoded = decode_allowance_holder_calldata(
            tx.get("data") or "", chain_id=chain_id
        )
    except CalldataError as exc:
        raise ZeroExArtifactError(f"calldata refused: {exc}") from exc

    # *** NEVER APPROVE SETTLER. ***
    # Re-raised as ZeroExArtifactError: it escaped as a bare ValueError, so a
    # caller writing `except ZeroExArtifactError` missed the single most
    # important refusal in the module.
    try:
        assert_spender_is_not_settler(
            chain_id=chain_id, spender=spender, inner_target=decoded.inner_target
        )
    except ValueError as exc:
        raise ZeroExArtifactError(str(exc)) from exc

    taker_norm = normalize_address(taker, field_name="taker")
    if decoded.recipient != taker_norm:
        raise ZeroExArtifactError(
            f"calldata pays out to {decoded.recipient}, not to the taker "
            f"{taker_norm} — the proceeds would go somewhere nobody approved"
        )
    if decoded.sell_token != sell_token:
        raise ZeroExArtifactError(
            f"calldata sells {decoded.sell_token}, response says {sell_token}"
        )
    if decoded.buy_token != buy_token:
        raise ZeroExArtifactError(
            f"calldata buys {decoded.buy_token}, response says {buy_token}"
        )
    if decoded.sell_amount != sell_amount:
        raise ZeroExArtifactError(
            f"calldata sells {decoded.sell_amount}, response says {sell_amount}"
        )
    if decoded.minimum_amount_out != min_buy:
        # *** THE MOST IMPORTANT COMPARISON IN THE MODULE. ***
        # minBuyAmount is what a human is shown; minAmountOut is what the chain
        # enforces. A gap between them is a loss bound nobody agreed to.
        raise ZeroExArtifactError(
            f"calldata enforces minAmountOut {decoded.minimum_amount_out} but the "
            f"response advertises minBuyAmount {min_buy}. The number shown is not "
            "the number the chain will enforce."
        )

    # --- the ABSOLUTE floor, anchored OUTSIDE the response -------------------
    # *** THE RATIO CHECK BELOW IS NOT A BOUND ON LOSS. ***
    # `slippage = (buyAmount - minBuyAmount) / buyAmount` divides two numbers
    # that both come from the same untrusted document and are anchored to
    # nothing. Understate `buyAmount` and the RATIO stays small while the
    # absolute floor goes anywhere: a response claiming `buyAmount=1`,
    # `minBuyAmount=1` scores 0 bps of slippage while authorizing ~$188 of WETH
    # for one millionth of a USDC. The ratio bounds the provider's internal
    # self-consistency; only a figure the INTENT supplies can bound loss.
    if expected_min_buy_amount <= 0:
        raise ZeroExArtifactError(
            "expected_min_buy_amount must be positive — without a floor derived "
            "from our own price reference there is no bound on what this trade "
            "may lose"
        )
    if min_buy < expected_min_buy_amount:
        raise ZeroExArtifactError(
            f"the quote's enforced minimum {min_buy} is below the intent's floor "
            f"{expected_min_buy_amount}. The response's own buyAmount/minBuyAmount "
            "ratio cannot detect this: understating buyAmount keeps that ratio "
            "small while the absolute floor goes anywhere."
        )

    # --- slippage, bounded ---------------------------------------------------
    # Computed from the two amounts rather than read from a field, because 0x
    # publishes no slippage figure. This is the ONLY ceiling on what the trade
    # may lose; without it `minimum_output > 0` was the entire protection.
    slippage = ((Decimal(buy_amount - min_buy)) / Decimal(buy_amount)) * 10_000
    if slippage > max_slippage_bps:
        raise ZeroExArtifactError(
            f"quote slippage is {slippage:.1f} bps (buyAmount {buy_amount}, "
            f"minBuyAmount {min_buy}), ceiling {max_slippage_bps} bps. The "
            "minimum output is the only on-chain bound on this trade's loss, and "
            "this one bounds almost nothing."
        )

    # --- fees, bounded -------------------------------------------------------
    zeroex_fee, zeroex_fee_token = _fee_amount(raw.get("fees"), "zeroExFee")
    integrator_fee, _ = _fee_amount(raw.get("fees"), "integratorFee")
    if buy_amount:
        fee_bps = (Decimal(zeroex_fee + integrator_fee) / Decimal(buy_amount)) * 10_000
        if fee_bps > max_fee_bps:
            raise ZeroExArtifactError(
                f"provider fees are {fee_bps:.1f} bps of the buy amount, ceiling "
                f"{max_fee_bps} bps"
            )

    return AllowanceHolderArtifact(
        flow=ZeroExFlow.ALLOWANCE_HOLDER,
        chain_id=chain_id,
        taker=taker_norm,
        zid=raw.get("zid") if isinstance(raw.get("zid"), str) else None,
        block_number=_uint(raw.get("blockNumber"), "blockNumber"),
        retrieved_at=retrieved_at or datetime.now(timezone.utc),
        response_hash=canonical_response_hash(raw),
        sell_token=sell_token,
        buy_token=buy_token,
        sell_amount=sell_amount,
        buy_amount=buy_amount,
        minimum_buy_amount=min_buy,
        recipient=decoded.recipient,
        to=to,
        data=(tx.get("data") or "").lower(),
        value=_uint(tx.get("value"), "transaction.value"),
        gas=_uint(tx.get("gas"), "transaction.gas", allow_zero=False),
        gas_price=_uint(tx.get("gasPrice"), "transaction.gasPrice"),
        allowance_spender=spender,
        allowance_actual=_uint(allowance.get("actual", 0), "issues.allowance.actual"),
        zeroex_fee_amount=zeroex_fee,
        zeroex_fee_token=zeroex_fee_token,
        integrator_fee_amount=integrator_fee,
        decoded=decoded,
        raw=raw,
    )
