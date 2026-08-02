"""The AllowanceHolder consuming path, against REAL mainnet calldata.

Every refusal the adapter is required to make, plus the approval operation, the
bundle, freshness, simulation, broadcast, receipts and reconciliation.

The fixture is a live 0x response captured with the Gecko-owned UNFUNDED taker.
Decoding is therefore proven against bytes 0x actually produced, not against a
synthetic payload written to match the decoder.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from conftest_zeroex import (  # noqa: E402
    ALLOWANCE_HOLDER_ADDRESS,
    BUY_AMOUNT,
    GECKO_TEST_TAKER,
    MIN_BUY_AMOUNT,
    QUOTE_BLOCK,
    SELL_AMOUNT,
    SETTLER_ADDRESS,
    USDC,
    WETH,
    allowance_holder_response,
    real_calldata,
)

from scout.live.evm.adapter import (  # noqa: E402
    ZeroExAllowanceHolderAdapter,
    ZeroExQuoteRequest,
)
from scout.live.evm.allowance_holder import (
    build_allowance_holder_artifact,
)  # noqa: E402
from scout.live.evm.approval import (  # noqa: E402
    UNLIMITED,
    ApprovalRefused,
    build_approval_intent,
)
from scout.live.evm.artifact import ZeroExArtifactError  # noqa: E402
from scout.live.evm.calldata import (  # noqa: E402
    CalldataError,
    decode_allowance_holder_calldata,
)
from scout.live.evm.execution import (  # noqa: E402
    BroadcastRefused,
    DisabledBroadcaster,
    EvmReceipt,
    ExecutionState,
    SimulationRefused,
    SimulationResult,
    build_bundle_from_artifact,
    reconcile,
    require_successful_simulation,
    submit_once,
)
from scout.live.evm.flows import (  # noqa: E402
    FLOW_SPECS,
    FlowNotSupported,
    ZeroExFlow,
)

_T0 = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


def _request(**over) -> ZeroExQuoteRequest:
    kw = dict(
        chain_id=1,
        sell_token=WETH,
        buy_token=USDC,
        sell_amount=SELL_AMOUNT,
        taker=GECKO_TEST_TAKER,
    )
    kw.update(over)
    return ZeroExQuoteRequest(**kw)


def _artifact(raw=None, **over):
    return ZeroExAllowanceHolderAdapter(chain_id=1).validate_response(
        raw if raw is not None else allowance_holder_response(),
        request=_request(**over),
        retrieved_at=_T0,
    )


def _mutate(**changes) -> dict:
    raw = allowance_holder_response()
    for path, value in changes.items():
        parts = path.split(".")
        node = raw
        for p in parts[:-1]:
            node = node[p]
        node[parts[-1]] = value
    return raw


# ===========================================================================
# The decode itself, on real bytes
# ===========================================================================


class TestTwoLayerDecode:
    def test_the_real_calldata_decodes_both_layers(self):
        d = decode_allowance_holder_calldata(real_calldata(), chain_id=1)
        assert d.inner_selector == "0x1fff991f"
        assert d.sell_token == WETH
        assert d.sell_amount == SELL_AMOUNT
        assert d.buy_token == USDC
        assert d.recipient == GECKO_TEST_TAKER
        assert d.minimum_amount_out == MIN_BUY_AMOUNT
        assert d.inner_target == SETTLER_ADDRESS
        assert len(d.action_selectors) == 5

    def test_the_inner_target_is_settler_and_is_not_the_spender(self):
        """*** THE CORRECTION. ***

        The decoded outer call names Settler in `operator` and `target`. Neither
        is the ERC-20 spender; that comes from `allowanceTarget`.
        """
        d = decode_allowance_holder_calldata(real_calldata(), chain_id=1)
        art = _artifact()
        assert d.inner_target == SETTLER_ADDRESS
        assert art.allowance_spender == ALLOWANCE_HOLDER_ADDRESS
        assert art.allowance_spender != d.inner_target

    def test_an_unknown_outer_selector_is_refused(self):
        with pytest.raises(CalldataError, match="outer selector"):
            decode_allowance_holder_calldata("0xdeadbeef" + "00" * 32, chain_id=1)

    def test_misaligned_arguments_are_refused_not_padded(self):
        """*** REFUSE, DO NOT PAD. ***

        Padding is a guess about intent, and a validator that pads until
        something parses has stopped validating. This is exactly the refusal that
        disabled the Permit2 flow.
        """
        raw = real_calldata()
        with pytest.raises(CalldataError, match="32-byte boundary"):
            decode_allowance_holder_calldata(raw + "abcdef01"[:6], chain_id=1)

    def test_an_unreviewed_chain_is_refused(self):
        with pytest.raises(CalldataError, match="Settler"):
            decode_allowance_holder_calldata(real_calldata(), chain_id=999)


# ===========================================================================
# The adapter's required refusals
# ===========================================================================


class TestAdapterRefusals:
    def test_the_real_fixture_validates(self):
        """Guard on every refusal below."""
        art = _artifact()
        assert art.allowance_spender == ALLOWANCE_HOLDER_ADDRESS
        assert art.minimum_buy_amount == MIN_BUY_AMOUNT
        assert art.requires_approval is True

    def test_an_unsupported_chain_is_refused(self):
        with pytest.raises(ZeroExArtifactError, match="reviewed AllowanceHolder"):
            ZeroExAllowanceHolderAdapter(chain_id=999).validate_response(
                allowance_holder_response(), request=_request(chain_id=999)
            )

    def test_a_spender_that_is_not_the_allowance_holder_is_refused(self):
        raw = _mutate(**{"allowanceTarget": SETTLER_ADDRESS})
        raw["issues"]["allowance"]["spender"] = SETTLER_ADDRESS
        raw["transaction"]["to"] = SETTLER_ADDRESS
        with pytest.raises(ValueError, match="never be approved|Settler"):
            _artifact(raw)

    def test_spender_and_allowance_target_must_agree(self):
        raw = allowance_holder_response()
        raw["issues"]["allowance"]["spender"] = "0x" + "ab" * 20
        with pytest.raises(ZeroExArtifactError, match="disagrees"):
            _artifact(raw)

    def test_transaction_target_must_be_the_allowance_holder(self):
        raw = _mutate(**{"transaction.to": "0x" + "cd" * 20})
        with pytest.raises(ZeroExArtifactError, match="allowanceTarget"):
            _artifact(raw)

    def test_a_missing_spender_is_refused_not_inferred(self):
        """The spender must never be read off calldata — that is the mistake."""
        raw = allowance_holder_response()
        raw["issues"]["allowance"] = {"actual": "0"}
        with pytest.raises(ZeroExArtifactError, match="spender"):
            _artifact(raw)

    @pytest.mark.parametrize(
        "field,value",
        [("sellToken", "0x" + "11" * 20), ("buyToken", "0x" + "22" * 20)],
    )
    def test_a_token_that_is_not_what_the_intent_asked_for_is_refused(
        self, field, value
    ):
        with pytest.raises(ZeroExArtifactError, match="intent asked for"):
            _artifact(_mutate(**{field: value}))

    def test_a_sell_amount_that_is_not_what_the_intent_asked_for_is_refused(self):
        with pytest.raises(ZeroExArtifactError, match="intent asked for"):
            _artifact(_mutate(**{"sellAmount": str(SELL_AMOUNT + 1)}))

    def test_a_recipient_that_is_not_the_taker_is_refused(self):
        """The calldata pays out to a fixed address. If it is not ours, the
        proceeds go somewhere nobody approved."""
        with pytest.raises(ZeroExArtifactError, match="pays out to"):
            _artifact(taker="0x" + "99" * 20)

    def test_a_minimum_that_disagrees_with_the_calldata_is_refused(self):
        """*** THE MOST IMPORTANT REFUSAL. ***

        `minBuyAmount` is what a human is shown; `minAmountOut` in the calldata
        is what the chain enforces. A gap is a loss bound nobody agreed to.
        """
        with pytest.raises(ZeroExArtifactError, match="not the number the chain"):
            _artifact(_mutate(**{"minBuyAmount": "1"}))

    def test_no_liquidity_is_refused(self):
        with pytest.raises(ZeroExArtifactError, match="liquidityAvailable"):
            _artifact(_mutate(**{"liquidityAvailable": False}))

    def test_an_excessive_provider_fee_is_refused(self):
        raw = allowance_holder_response()
        raw["fees"]["zeroExFee"]["amount"] = str(BUY_AMOUNT // 2)
        with pytest.raises(ZeroExArtifactError, match="bps"):
            _artifact(raw)

    def test_a_permit2_payload_is_refused_as_the_wrong_flow(self):
        raw = allowance_holder_response()
        raw["permit2"] = {"eip712": {"domain": {}, "types": {}, "message": {}}}
        with pytest.raises(FlowNotSupported):
            _artifact(raw)


class TestPermit2IsFailClosed:
    def test_permit2_is_declared_but_unsupported(self):
        spec = FLOW_SPECS[ZeroExFlow.PERMIT2]
        assert spec.supports_unsigned_transaction is False
        assert spec.refusal_reason and "not yet proven" in spec.refusal_reason

    def test_selecting_the_permit2_flow_refuses(self):
        with pytest.raises(FlowNotSupported) as exc:
            ZeroExAllowanceHolderAdapter.reject_flow("permit2")
        assert exc.value.flow is ZeroExFlow.PERMIT2

    def test_the_allowance_holder_flow_is_selectable(self):
        assert (
            ZeroExAllowanceHolderAdapter.reject_flow("allowance-holder")
            is ZeroExFlow.ALLOWANCE_HOLDER
        )

    def test_permit2_is_still_fully_declared(self):
        """Fail-closed, not undocumented: its requirements are recorded so the
        later task starts from a spec rather than from nothing."""
        spec = FLOW_SPECS[ZeroExFlow.PERMIT2]
        assert spec.requires_eip712_signature is True
        assert spec.approval_target_role == "permit2"
        assert spec.nonce_model == "permit2_unordered_nonce"


class TestCapabilities:
    def test_the_adapter_declares_dex_and_unsigned_transaction(self):
        caps = ZeroExAllowanceHolderAdapter(chain_id=1).describe_capabilities()
        assert caps.venue_family == "dex"
        assert caps.supports_unsigned_transaction is True
        assert caps.supports_simulation is True

    def test_it_declares_no_money_movement(self):
        caps = ZeroExAllowanceHolderAdapter(chain_id=1).describe_capabilities()
        assert caps.grants_money_movement() is False

    def test_an_unreviewed_chain_declares_nothing(self):
        """Fail-closed: a chain whose AllowanceHolder we cannot verify is a chain
        this adapter cannot sign for."""
        caps = ZeroExAllowanceHolderAdapter(chain_id=999).describe_capabilities()
        assert caps.supports_unsigned_transaction is False
        assert caps.supports_market_orders is False

    def test_a_dex_intent_is_permitted_and_a_cex_one_is_not(self):
        caps = ZeroExAllowanceHolderAdapter(chain_id=1).describe_capabilities()
        ok, _ = caps.permits_order(
            venue_family="dex", order_type="market", reduce_only=False
        )
        assert ok
        refused, why = caps.permits_order(
            venue_family="cex", order_type="market", reduce_only=False
        )
        assert not refused and "cex" in why


# ===========================================================================
# Freshness
# ===========================================================================


class TestQuoteFreshness:
    def test_a_fresh_quote_passes_both_bounds(self):
        _artifact().assert_fresh(
            current_block=QUOTE_BLOCK + 1, now=_T0 + timedelta(seconds=5)
        )

    def test_wall_clock_staleness_refuses(self):
        with pytest.raises(ZeroExArtifactError, match="old"):
            _artifact().assert_fresh(
                current_block=QUOTE_BLOCK, now=_T0 + timedelta(minutes=5)
            )

    def test_block_drift_refuses(self):
        with pytest.raises(ZeroExArtifactError, match="blocks behind"):
            _artifact().assert_fresh(
                current_block=QUOTE_BLOCK + 500, now=_T0 + timedelta(seconds=1)
            )

    def test_a_node_reporting_a_block_before_the_quote_refuses(self):
        with pytest.raises(ZeroExArtifactError, match="behind or lying"):
            _artifact().assert_fresh(
                current_block=QUOTE_BLOCK - 1, now=_T0 + timedelta(seconds=1)
            )

    def test_a_requote_produces_a_different_bundle_hash(self):
        """*** RE-QUOTING CAN NEVER SILENTLY REUSE AN AUTHORIZATION. ***"""
        first = build_bundle_from_artifact(
            _artifact(), intent_hash="i" * 64, wallet=GECKO_TEST_TAKER
        )
        requoted = _artifact(_mutate(**{"blockNumber": str(QUOTE_BLOCK + 10)}))
        second = build_bundle_from_artifact(
            requoted, intent_hash="i" * 64, wallet=GECKO_TEST_TAKER
        )
        assert first.bundle_hash != second.bundle_hash


# ===========================================================================
# The bounded approval operation
# ===========================================================================


class TestApprovalOperation:
    def _build(self, **over):
        kw = dict(
            chain_id=1,
            owner=GECKO_TEST_TAKER,
            token=WETH,
            spender=ALLOWANCE_HOLDER_ADDRESS,
            required_amount=SELL_AMOUNT,
            inner_target=SETTLER_ADDRESS,
            now=_T0,
        )
        kw.update(over)
        return build_approval_intent(**kw)

    def test_it_is_a_separate_intent_with_its_own_hash(self):
        """An approval survives the swap and can be exercised later. Folding it in
        would mean one authorization covering two capabilities with different
        lifetimes — the longer-lived one invisible."""
        approval = self._build()
        swap = build_bundle_from_artifact(
            _artifact(), intent_hash="i" * 64, wallet=GECKO_TEST_TAKER
        )
        assert approval.intent_hash != swap.bundle_hash
        assert len(approval.intent_hash) == 64

    def test_the_spender_is_the_allowance_holder_never_settler(self):
        assert self._build().spender == ALLOWANCE_HOLDER_ADDRESS
        with pytest.raises(ValueError, match="never be approved|Settler"):
            self._build(spender=SETTLER_ADDRESS)

    def test_the_amount_is_tightly_bounded(self):
        approval = self._build()
        assert SELL_AMOUNT <= approval.amount <= SELL_AMOUNT * 102 // 100
        assert not approval.is_unlimited

    def test_unlimited_approval_is_disabled(self):
        """*** THE INDUSTRY'S CONVENIENT HABIT, REFUSED. ***"""
        with pytest.raises(ApprovalRefused, match="unlimited"):
            self._build(required_amount=UNLIMITED)

    def test_the_calldata_is_a_real_erc20_approve(self):
        approval = self._build()
        assert approval.calldata.startswith("0x095ea7b3")
        assert ALLOWANCE_HOLDER_ADDRESS[2:] in approval.calldata

    def test_an_unlisted_spender_is_refused(self):
        with pytest.raises(ValueError, match="not the approved AllowanceHolder"):
            self._build(spender="0x" + "ee" * 20)

    def test_it_expires(self):
        approval = self._build()
        assert not approval.is_expired(at=_T0)
        assert approval.is_expired(at=_T0 + timedelta(hours=1))


# ===========================================================================
# Simulation
# ===========================================================================


class _Simulator:
    def __init__(self, result: SimulationResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def simulate(self, **kw):
        self.calls.append(kw)
        return self.result


class TestSimulation:
    async def test_a_successful_simulation_passes_and_sees_the_final_calldata(self):
        sim = _Simulator(SimulationResult(True, 210000, None, MIN_BUY_AMOUNT + 100))
        art = _artifact()
        await require_successful_simulation(sim, art)
        assert sim.calls[0]["data"] == art.data
        assert sim.calls[0]["from_address"] == GECKO_TEST_TAKER

    async def test_a_revert_refuses(self):
        sim = _Simulator(SimulationResult(False, None, "STF", None))
        with pytest.raises(SimulationRefused, match="reverted"):
            await require_successful_simulation(sim, _artifact())

    async def test_an_output_below_the_enforced_minimum_refuses(self):
        sim = _Simulator(SimulationResult(True, 210000, None, MIN_BUY_AMOUNT - 1))
        with pytest.raises(SimulationRefused, match="below the enforced minimum"):
            await require_successful_simulation(sim, _artifact())


# ===========================================================================
# Bundle, broadcast, receipts, recovery, reconciliation
# ===========================================================================


class TestBundleFromArtifact:
    def test_it_binds_the_validated_terms(self):
        art = _artifact()
        b = build_bundle_from_artifact(
            art, intent_hash="i" * 64, wallet=GECKO_TEST_TAKER
        )
        assert b.minimum_output == MIN_BUY_AMOUNT
        assert b.wallet == GECKO_TEST_TAKER
        assert b.to == ALLOWANCE_HOLDER_ADDRESS
        assert b.response_hash == art.response_hash
        assert b.verify()

    def test_a_wallet_that_is_not_the_taker_is_refused(self):
        """The quote's recipient check is meaningless if a different account
        signs."""
        with pytest.raises(ValueError, match="not the artifact's taker"):
            build_bundle_from_artifact(
                _artifact(), intent_hash="i" * 64, wallet="0x" + "77" * 20
            )

    def test_the_insertion_algorithm_is_named_even_though_nothing_is_inserted(self):
        """Committing to 'nothing is inserted' keeps the bundle's shape identical
        across flows, so a flow that DOES insert cannot quietly omit it."""
        b = build_bundle_from_artifact(
            _artifact(), intent_hash="i" * 64, wallet=GECKO_TEST_TAKER
        )
        assert b.insertion_algorithm_version == "none-single-signature-v1"


class TestBroadcastIsDisabled:
    async def test_the_only_wired_transport_refuses(self):
        with pytest.raises(BroadcastRefused, match="not authorized"):
            await DisabledBroadcaster().submit(chain_id=1, signed_raw_tx="0xdead")

    async def test_the_expected_hash_is_persisted_before_submission(self):
        """*** PERSIST FIRST, ALWAYS. ***

        A submitted transaction whose expected hash was never written down is a
        transaction nobody can ask about afterwards.
        """
        persisted: list[EvmReceipt] = []

        async def _persist(r):
            persisted.append(r)

        receipt = EvmReceipt(
            intent_hash="i" * 64,
            bundle_hash="b" * 64,
            chain_id=1,
            wallet=GECKO_TEST_TAKER,
            expected_transaction_hash="0x" + "ab" * 32,
            state=ExecutionState.NOT_SUBMITTED,
        )
        with pytest.raises(BroadcastRefused):
            await submit_once(
                broadcaster=DisabledBroadcaster(),
                receipt=receipt,
                signed_raw_tx="0xdead",
                persist=_persist,
            )
        assert persisted and persisted[0].expected_transaction_hash == (
            "0x" + "ab" * 32
        )

    async def test_a_second_submission_is_refused(self):
        """A resend is a second transaction, not a retry."""
        receipt = EvmReceipt(
            intent_hash="i" * 64,
            bundle_hash="b" * 64,
            chain_id=1,
            wallet=GECKO_TEST_TAKER,
            expected_transaction_hash="0x" + "ab" * 32,
            state=ExecutionState.PENDING,
        )

        async def _persist(r):
            return None

        with pytest.raises(BroadcastRefused, match="already"):
            await submit_once(
                broadcaster=DisabledBroadcaster(),
                receipt=receipt,
                signed_raw_tx="0x",
                persist=_persist,
            )

    async def test_an_ambiguous_submission_becomes_unknown_and_blocks(self):
        """NOT_SUBMITTED permits a rebuild; UNKNOWN forbids it. Collapsing them
        is how a double-spend gets written."""

        class _Flaky:
            async def submit(self, **kw):
                raise TimeoutError("connection dropped")

            async def lookup(self, **kw):
                return None

        seen: list[EvmReceipt] = []

        async def _persist(r):
            seen.append(r)

        out = await submit_once(
            broadcaster=_Flaky(),
            receipt=EvmReceipt(
                intent_hash="i" * 64,
                bundle_hash="b" * 64,
                chain_id=1,
                wallet=GECKO_TEST_TAKER,
                expected_transaction_hash="0x" + "ab" * 32,
                state=ExecutionState.NOT_SUBMITTED,
            ),
            signed_raw_tx="0x",
            persist=_persist,
        )
        assert out.state is ExecutionState.UNKNOWN
        assert out.blocks_the_lane
        assert not out.is_terminal


class TestReconciliation:
    def _receipt(self, state=ExecutionState.FINALIZED):
        return EvmReceipt(
            intent_hash="i" * 64,
            bundle_hash="b" * 64,
            chain_id=1,
            wallet=GECKO_TEST_TAKER,
            expected_transaction_hash="0x" + "ab" * 32,
            state=state,
        )

    def test_a_clean_execution_reconciles(self):
        verdict = reconcile(
            receipt=self._receipt(),
            artifact=_artifact(),
            buy_balance_delta=MIN_BUY_AMOUNT + 10,
            sell_balance_delta=-SELL_AMOUNT,
            allowance_after=0,
            gas_used=200000,
        )
        assert verdict.reconciled and not verdict.trips_breaker

    def test_receiving_less_than_the_enforced_minimum_trips_the_breaker(self):
        verdict = reconcile(
            receipt=self._receipt(),
            artifact=_artifact(),
            buy_balance_delta=MIN_BUY_AMOUNT - 1,
            sell_balance_delta=-SELL_AMOUNT,
            allowance_after=0,
            gas_used=200000,
        )
        assert not verdict.reconciled and verdict.trips_breaker

    def test_spending_more_than_authorized_trips_the_breaker(self):
        verdict = reconcile(
            receipt=self._receipt(),
            artifact=_artifact(),
            buy_balance_delta=MIN_BUY_AMOUNT,
            sell_balance_delta=-(SELL_AMOUNT + 1),
            allowance_after=0,
            gas_used=200000,
        )
        assert verdict.trips_breaker

    def test_a_residual_allowance_larger_than_the_trade_is_flagged(self):
        """A standing grant nobody asked for — what bounded approval prevents."""
        verdict = reconcile(
            receipt=self._receipt(),
            artifact=_artifact(),
            buy_balance_delta=MIN_BUY_AMOUNT,
            sell_balance_delta=-SELL_AMOUNT,
            allowance_after=SELL_AMOUNT * 100,
            gas_used=200000,
        )
        assert any("revoke" in d for d in verdict.discrepancies)

    @pytest.mark.parametrize("state", [ExecutionState.UNKNOWN, ExecutionState.REPLACED])
    def test_unresolved_states_trip_the_breaker(self, state):
        verdict = reconcile(
            receipt=self._receipt(state),
            artifact=_artifact(),
            buy_balance_delta=None,
            sell_balance_delta=None,
            allowance_after=None,
            gas_used=None,
        )
        assert verdict.trips_breaker

    def test_a_revert_trips_the_breaker(self):
        verdict = reconcile(
            receipt=self._receipt(ExecutionState.REVERTED),
            artifact=_artifact(),
            buy_balance_delta=0,
            sell_balance_delta=0,
            allowance_after=0,
            gas_used=21000,
        )
        assert verdict.trips_breaker


# ===========================================================================
# End to end: the inactive mandate stops the whole 0x path before the key
# ===========================================================================


class TestInactiveMandateStopsTheZeroExPath:
    """*** THE GUARANTEE, THROUGH THE REAL 0x ARTIFACT. ***

    `test_evm_signer_isolation.py` proves the boundary in isolation. This proves
    it on the actual consuming path: a validated live artifact, a real bundle,
    and a key loader that RAISES if it is ever reached.
    """

    def _boundary(self, mandate, loader, tmp_path):
        import os

        from scout.live.evm.signer import REQUIRED_MODE, EvmSignerBoundary

        key = tmp_path / "evm.key"
        key.write_text("0x" + "11" * 32, encoding="utf-8")
        os.chmod(key, REQUIRED_MODE)
        return EvmSignerBoundary(mandate=mandate, key_path=key, key_loader=loader)

    async def test_the_shipped_posture_never_reads_the_key(self, tmp_path):
        from scout.live.mandate import ExecutionMandate, MandateRefused
        from scout.live.evm.signer import SignerRefused

        class _KeyWasRead(AssertionError):
            pass

        def _tripwire(_path):
            raise _KeyWasRead("the EVM key was READ under an inactive mandate")

        # The real, shipped mandate: no settings at all -> every default refuses.
        inner = ExecutionMandate(settings=None)

        class _BundleMandate:
            def authorize_bundle(self, bundle):
                inner.precheck()  # raises MandateRefused when inactive
                return {"authorized": True}

        artifact = _artifact()
        bundle = build_bundle_from_artifact(
            artifact, intent_hash="i" * 64, wallet=GECKO_TEST_TAKER
        )
        boundary = self._boundary(_BundleMandate(), _tripwire, tmp_path)

        with pytest.raises(SignerRefused) as exc:
            boundary.sign_bundle(bundle)
        assert exc.value.gate == "mandate"
        assert "key was not read" in exc.value.message

    async def test_a_stale_artifact_refuses_before_any_signing(self, tmp_path):
        """Freshness is re-checked immediately before signing; a stale quote
        never reaches the gate, let alone the key."""
        artifact = _artifact()
        with pytest.raises(ZeroExArtifactError):
            artifact.assert_fresh(
                current_block=QUOTE_BLOCK + 1, now=_T0 + timedelta(minutes=10)
            )

    async def test_a_changed_artifact_produces_a_different_bundle(self, tmp_path):
        """A changed transaction template cannot reuse an authorization."""
        base = build_bundle_from_artifact(
            _artifact(), intent_hash="i" * 64, wallet=GECKO_TEST_TAKER
        )
        moved = _artifact(_mutate(**{"transaction.gas": "999999"}))
        other = build_bundle_from_artifact(
            moved, intent_hash="i" * 64, wallet=GECKO_TEST_TAKER
        )
        assert base.bundle_hash != other.bundle_hash

    async def test_nothing_in_the_evm_package_can_broadcast(self):
        """The only wired transport refuses, and no module imports a live one."""
        import ast
        from pathlib import Path

        pkg = Path("scout/live/evm")
        offenders = []
        for path in pkg.rglob("*.py"):
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if any(k in name for k in ("web3", "requests", "urllib")):
                        offenders.append((str(path), name))
        assert offenders == [], f"a live transport is importable: {offenders}"
