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
        # The floor the INTENT will accept, from our own price reference rather
        # than from the 0x response. 1% under the fixture's real minimum.
        expected_min_buy_amount=MIN_BUY_AMOUNT * 99 // 100,
    )
    kw.update(over)
    return ZeroExQuoteRequest(**kw)


def _artifact(raw=None, **over):
    return ZeroExAllowanceHolderAdapter(chain_id=1).validate_response(
        raw if raw is not None else allowance_holder_response(),
        request=_request(**over),
        retrieved_at=_T0,
    )


def _artifact_now(raw=None, **over):
    """Artifact stamped with real current time, for paths that check expiry."""
    return ZeroExAllowanceHolderAdapter(chain_id=1).validate_response(
        raw if raw is not None else allowance_holder_response(),
        request=_request(**over),
        retrieved_at=datetime.now(timezone.utc),
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

        artifact = _artifact_now()
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


# ===========================================================================
# The orchestrated path: guards that are CALLED, not merely callable
# ===========================================================================


class TestPreparedExecutionEnforcesTheGuards:
    """*** A GUARD NOTHING CALLS IS NOT A GUARD. ***

    `assert_fresh` and `require_successful_simulation` were defined, tested, and
    invoked by NO production path — a guarantee that lived in a docstring. These
    assert they are now on the only route to a bundle.
    """

    def _funded(self, **over):
        """The fixture taker is unfunded and has zero allowance, so the swap path
        needs a response where the approval is already in place."""
        raw = allowance_holder_response()
        raw["issues"]["allowance"]["actual"] = str(SELL_AMOUNT * 2)
        raw["issues"]["balance"] = None
        for path, value in over.items():
            parts = path.split(".")
            node = raw
            for p in parts[:-1]:
                node = node[p]
            node[parts[-1]] = value
        return _artifact(raw)

    async def test_a_stale_quote_never_reaches_simulation_or_a_bundle(self):
        from scout.live.evm.execution import prepare_execution

        sim = _Simulator(SimulationResult(True, 1, None, MIN_BUY_AMOUNT))
        with pytest.raises(ZeroExArtifactError, match="old"):
            await prepare_execution(
                artifact=self._funded(),
                intent_hash="i" * 64,
                wallet=GECKO_TEST_TAKER,
                simulator=sim,
                current_block=QUOTE_BLOCK,
                now=_T0 + timedelta(minutes=10),
            )
        assert sim.calls == [], "a stale quote was simulated"

    async def test_block_drift_also_stops_it(self):
        from scout.live.evm.execution import prepare_execution

        sim = _Simulator(SimulationResult(True, 1, None, MIN_BUY_AMOUNT))
        with pytest.raises(ZeroExArtifactError, match="blocks behind"):
            await prepare_execution(
                artifact=self._funded(),
                intent_hash="i" * 64,
                wallet=GECKO_TEST_TAKER,
                simulator=sim,
                current_block=QUOTE_BLOCK + 1000,
                now=_T0 + timedelta(seconds=1),
            )
        assert sim.calls == []

    async def test_a_failing_simulation_yields_no_bundle(self):
        from scout.live.evm.execution import prepare_execution

        sim = _Simulator(SimulationResult(False, None, "STF", None))
        with pytest.raises(SimulationRefused):
            await prepare_execution(
                artifact=self._funded(),
                intent_hash="i" * 64,
                wallet=GECKO_TEST_TAKER,
                simulator=sim,
                current_block=QUOTE_BLOCK + 1,
                now=_T0 + timedelta(seconds=1),
            )

    async def test_a_missing_allowance_refuses_rather_than_approving_inline(self):
        """*** APPROVAL IS NEVER HIDDEN INSIDE A SWAP. ***"""
        from scout.live.evm.execution import prepare_execution

        sim = _Simulator(SimulationResult(True, 1, None, MIN_BUY_AMOUNT))
        with pytest.raises(SimulationRefused, match="approval is required FIRST"):
            await prepare_execution(
                artifact=_artifact(),  # fixture has allowance 0
                intent_hash="i" * 64,
                wallet=GECKO_TEST_TAKER,
                simulator=sim,
                current_block=QUOTE_BLOCK + 1,
                now=_T0 + timedelta(seconds=1),
            )
        assert sim.calls == []

    async def test_the_happy_path_simulates_the_exact_final_calldata(self):
        """Guard on the guard: it must still be possible to prepare."""
        from scout.live.evm.execution import prepare_execution

        art = self._funded()
        sim = _Simulator(SimulationResult(True, 210000, None, MIN_BUY_AMOUNT + 5))
        prepared = await prepare_execution(
            artifact=art,
            intent_hash="i" * 64,
            wallet=GECKO_TEST_TAKER,
            simulator=sim,
            current_block=QUOTE_BLOCK + 1,
            now=_T0 + timedelta(seconds=1),
        )
        assert sim.calls[0]["data"] == art.data
        assert prepared.bundle.minimum_output == MIN_BUY_AMOUNT
        assert prepared.bundle.verify()

    def test_the_orchestrator_is_the_only_thing_that_calls_the_guards(self):
        """Structural: if a future refactor drops a call, this fails rather than
        the guarantee silently reverting to unenforced."""
        import inspect

        from scout.live.evm import execution

        src = inspect.getsource(execution.prepare_execution)
        assert "assert_fresh(" in src
        assert "require_successful_simulation(" in src
        assert "build_bundle_from_artifact(" in src
        # And freshness precedes simulation, which precedes the bundle.
        assert src.index("assert_fresh(") < src.index("require_successful_simulation(")
        assert src.index("require_successful_simulation(") < src.index(
            "build_bundle_from_artifact("
        )


# ===========================================================================
# Structural pin: prepare_execution is the ONLY production route to a bundle
# ===========================================================================


class TestOnlyOneRouteToABundle:
    """*** PINNED STRUCTURALLY, NOT BY A HAPPY-PATH ORDERING TEST. ***

    An ordering test proves the orchestrator calls the guards in order. It proves
    nothing about a SECOND route that skips them. These walk the AST of the whole
    package so a new call site fails the build.
    """

    def _production_sources(self):
        return sorted(Path("scout").rglob("*.py"))

    def test_only_prepare_execution_builds_a_bundle(self):
        import ast

        callers: set[tuple[str, str]] = set()
        for path in self._production_sources():
            rel = str(path).replace("\\", "/")
            tree = ast.parse(path.read_text("utf-8"))
            in_evm = rel.startswith("scout/live/evm/")
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = None
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                # `dataclasses.replace` produces a fully-verifying bundle with
                # any field changed: `verify()` guards against tampering IN
                # PLACE, not against RE-CONSTRUCTION, so a replaced bundle
                # recomputes its own hash and validates while carrying
                # minimum_output=1 beside a response_hash that still names the
                # honest response. `getattr` is the same evasion by another name.
                # Both are legitimate elsewhere in scout/, so they only count
                # inside the evm package.
                builders = ("build_bundle_from_artifact", "ExecutionSigningBundle")
                flagged = name in builders
                if in_evm and not flagged:
                    # `dataclasses.replace(bundle, ...)` rebuilds a bundle that
                    # recomputes its own hash, so it VERIFIES while carrying any
                    # field changed — `verify()` guards tampering in place, not
                    # re-construction. Matched narrowly: a bare `replace(...)`
                    # (i.e. `from dataclasses import replace`) or an explicit
                    # `dataclasses.replace(...)`. `str.replace` is an Attribute
                    # on a non-`dataclasses` value and is not flagged.
                    if name == "replace":
                        func = node.func
                        bare = isinstance(func, ast.Name)
                        qualified = (
                            isinstance(func, ast.Attribute)
                            and isinstance(func.value, ast.Name)
                            and func.value.id == "dataclasses"
                        )
                        flagged = bare or qualified
                    elif name == "getattr":
                        # Only when it NAMES a builder — `getattr(x, "close")`
                        # is not an evasion.
                        flagged = (
                            len(node.args) >= 2
                            and isinstance(node.args[1], ast.Constant)
                            and node.args[1].value in builders
                        )
                if not flagged:
                    continue
                enclosing = "<module>"
                for fn in ast.walk(tree):
                    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                        n is node for n in ast.walk(fn)
                    ):
                        enclosing = fn.name
                callers.add((rel, enclosing))
        assert callers == {
            ("scout/live/evm/execution.py", "prepare_execution"),
            ("scout/live/evm/execution.py", "build_bundle_from_artifact"),
        }, (
            f"a bundle can be built outside prepare_execution: {sorted(callers)}. "
            "Freshness and simulation would be bypassed."
        )

    def test_no_production_module_imports_the_raw_builder(self):
        """Importing it is how a second route starts."""
        import ast

        offenders = []
        for path in self._production_sources():
            rel = str(path).replace("\\", "/")
            if rel in ("scout/live/evm/execution.py", "scout/live/evm/signer.py"):
                continue
            for node in ast.walk(ast.parse(path.read_text("utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.module:
                    for alias in node.names:
                        if alias.name in (
                            "build_bundle_from_artifact",
                            "ExecutionSigningBundle",
                        ):
                            offenders.append((rel, alias.name))
        assert offenders == [], f"raw bundle construction imported by {offenders}"


# ===========================================================================
# The four guards mutation testing proved had ZERO effective coverage
# ===========================================================================


def _wrap_outer(inner: bytes, *, operator: str, target: str) -> str:
    from eth_abi import encode as abi_encode

    body = abi_encode(
        ["address", "address", "uint256", "address", "bytes"],
        [
            bytes.fromhex(operator[2:]),
            bytes.fromhex(WETH[2:]),
            SELL_AMOUNT,
            bytes.fromhex(target[2:]),
            inner,
        ],
    )
    return "0x2213bc0b" + body.hex()


class TestGuardsWithPreviouslyNoCoverage:
    """Each kills a mutant that survived the original suite.

    The existing tests exercised the harmless DIRECTION or the wrong BRANCH, so
    deleting the guard entirely left them green.
    """

    def test_calldata_enforcing_LESS_than_advertised_is_refused(self):
        """*** THE MONEY-LOSING DIRECTION, previously untested. ***

        The original test set `minBuyAmount="1"` — calldata enforcing MORE than
        advertised, which is harmless — so flipping `!=` to `>` survived. This is
        the direction where the chain enforces a floor BELOW what was shown.
        """
        raw = allowance_holder_response()
        raw["minBuyAmount"] = str(MIN_BUY_AMOUNT + 1000)
        raw["buyAmount"] = str(MIN_BUY_AMOUNT + 1001)
        with pytest.raises(ZeroExArtifactError, match="not the number the chain"):
            _artifact(raw)

    def test_the_INNER_alignment_refusal_fires(self):
        """The original test appended bytes to the OUTER payload, so only the
        outer check ran.

        It was also VACUOUS in a second way: 33 bytes of 0x11 fail eth_abi's
        padding validation whether or not the guard exists, and the match string
        "inner Settler" is a substring of BOTH the alignment refusal and the
        generic decode failure — so it passed with the guard deleted. The inner
        body here is a VALID encoding plus one byte, so with the guard removed
        the payload decodes cleanly and only the alignment check can refuse it.
        """
        from eth_abi import encode as abi_encode

        valid_inner_body = abi_encode(
            ["(address,address,uint256)", "bytes[]", "bytes32"],
            [
                (
                    bytes.fromhex(GECKO_TEST_TAKER[2:]),
                    bytes.fromhex(USDC[2:]),
                    MIN_BUY_AMOUNT,
                ),
                [b"\x11\x22\x33\x44"],
                b"\x00" * 32,
            ],
        )
        inner = bytes.fromhex("1fff991f") + valid_inner_body + b"\x00"
        payload = _wrap_outer(inner, operator=SETTLER_ADDRESS, target=SETTLER_ADDRESS)
        with pytest.raises(CalldataError, match="32-byte boundary"):
            decode_allowance_holder_calldata(payload, chain_id=1)

    def test_an_unreviewed_inner_selector_is_refused(self):
        """Deleting ALLOWED_INNER_SELECTORS previously survived."""
        inner = bytes.fromhex("deadbeef") + b"\x00" * 32
        payload = _wrap_outer(inner, operator=SETTLER_ADDRESS, target=SETTLER_ADDRESS)
        with pytest.raises(CalldataError, match="not a reviewed entrypoint"):
            decode_allowance_holder_calldata(payload, chain_id=1)

    def test_an_inner_target_outside_the_reviewed_settlers_is_refused(self):
        """The original test only covered 'no Settlers declared for this chain',
        never 'target not in the declared set'."""
        from eth_abi import encode as abi_encode

        rogue = "0x" + "cc" * 20
        inner_body = abi_encode(
            ["(address,address,uint256)", "bytes[]", "bytes32"],
            [
                (
                    bytes.fromhex(GECKO_TEST_TAKER[2:]),
                    bytes.fromhex(USDC[2:]),
                    MIN_BUY_AMOUNT,
                ),
                [b"\x11\x22\x33\x44"],
                b"\x00" * 32,
            ],
        )
        inner = bytes.fromhex("1fff991f") + inner_body
        payload = _wrap_outer(inner, operator=rogue, target=rogue)
        with pytest.raises(CalldataError, match="not a reviewed Settler"):
            decode_allowance_holder_calldata(payload, chain_id=1)

    def test_an_excessive_slippage_quote_is_refused(self):
        """*** THE BOUND THAT WAS MISSING ENTIRELY. ***

        `minimum_buy_amount` was bound everywhere and bounded nowhere, so a
        response advertising a 99.99% loss — with calldata enforcing the same
        degenerate floor, satisfying the response/calldata comparison — validated
        cleanly and cleared simulation, because simulation compares against that
        same floor.
        """
        raw = allowance_holder_response()
        # minBuyAmount still matches the calldata (so the response/calldata
        # comparison is not what refuses) and clears the intent floor, so the
        # RATIO ceiling is isolated: buyAmount is 100x the minimum, a ~99%
        # advertised loss.
        raw["buyAmount"] = str(MIN_BUY_AMOUNT * 100)
        raw["fees"]["zeroExFee"] = None
        with pytest.raises(ZeroExArtifactError, match="slippage"):
            _artifact(raw)

    def test_an_understated_buy_amount_cannot_evade_the_ratio_ceiling(self):
        """*** THE RATIO IS NOT A BOUND ON LOSS. ***

        `(buyAmount - minBuyAmount) / buyAmount` divides two numbers from the
        same untrusted response. Understating `buyAmount` keeps the ratio small
        while the absolute floor goes anywhere — `buyAmount=1, minBuyAmount=1`
        scores 0 bps while authorising ~$188 of WETH for a millionth of a USDC.

        The calldata must agree with the understated response, or the
        response/calldata comparison refuses first and the intent floor is never
        reached. That agreement is the realistic threat: a provider returning a
        bad quote CONSISTENTLY, where nothing internal to the document is wrong.
        """
        raw = self._crafted(min_out=1, buy_amount=1)
        with pytest.raises(ZeroExArtifactError, match="intent's floor"):
            _artifact(raw)

    def test_a_graded_understatement_is_also_refused(self):
        """99% understated: a floor of ~1.87 USDC against ~188 USDC of value,
        scoring only ~29 bps on the ratio."""
        low = MIN_BUY_AMOUNT // 100
        raw = self._crafted(min_out=low, buy_amount=low * 10029 // 10000)
        with pytest.raises(ZeroExArtifactError, match="intent's floor"):
            _artifact(raw)

    def _crafted(self, *, min_out: int, buy_amount: int) -> dict:
        """A self-consistent response whose calldata encodes `min_out`."""
        from eth_abi import encode as abi_encode

        inner_body = abi_encode(
            ["(address,address,uint256)", "bytes[]", "bytes32"],
            [
                (
                    bytes.fromhex(GECKO_TEST_TAKER[2:]),
                    bytes.fromhex(USDC[2:]),
                    min_out,
                ),
                [b"\x11\x22\x33\x44"],
                b"\x00" * 32,
            ],
        )
        inner = bytes.fromhex("1fff991f") + inner_body
        payload = _wrap_outer(inner, operator=SETTLER_ADDRESS, target=SETTLER_ADDRESS)
        raw = allowance_holder_response()
        raw["transaction"]["data"] = payload
        raw["buyAmount"] = str(buy_amount)
        raw["minBuyAmount"] = str(min_out)
        raw["fees"]["zeroExFee"] = None
        return raw


class TestSignerRefusesNonDecisions:
    """A coroutine is not an authorization, and neither is None."""

    def _boundary(self, mandate, loader, tmp_path):
        import os

        from scout.live.evm.signer import REQUIRED_MODE, EvmSignerBoundary

        key = tmp_path / "evm.key"
        key.write_text("0x" + "11" * 32, encoding="utf-8")
        os.chmod(key, REQUIRED_MODE)
        return EvmSignerBoundary(mandate=mandate, key_path=key, key_loader=loader)

    def _fresh_bundle(self):
        # Retrieved NOW: `sign_bundle` checks expiry against wall-clock time, so
        # a bundle pinned to the fixture's historical timestamp would refuse at
        # the expiry gate before reaching the one under test.
        art = _artifact_now()
        return build_bundle_from_artifact(
            art, intent_hash="i" * 64, wallet=GECKO_TEST_TAKER
        )

    def test_an_async_mandate_that_is_never_awaited_does_not_authorize(self, tmp_path):
        """*** THE BREACH. ***

        An async `authorize_bundle` returns an un-awaited coroutine: truthy, no
        exception, key read. The only mandate in this tree IS async.
        """
        from scout.live.evm.signer import SignerRefused

        calls = []

        class _AsyncMandate:
            async def authorize_bundle(self, bundle):
                raise RuntimeError("mandate is DISABLED")

        def _tripwire(_p):
            calls.append(1)
            raise AssertionError("the key was READ")

        with pytest.raises(SignerRefused) as exc:
            self._boundary(_AsyncMandate(), _tripwire, tmp_path).sign_bundle(
                self._fresh_bundle()
            )
        assert exc.value.gate == "mandate"
        assert "never awaited" in exc.value.message
        assert calls == []

    @pytest.mark.parametrize("result", [None, False, 0, ""])
    def test_a_falsy_return_is_a_refusal(self, tmp_path, result):
        from scout.live.evm.signer import SignerRefused

        calls = []

        class _Falsy:
            def authorize_bundle(self, bundle):
                return result

        def _tripwire(_p):
            calls.append(1)
            raise AssertionError("the key was READ")

        with pytest.raises(SignerRefused) as exc:
            self._boundary(_Falsy(), _tripwire, tmp_path).sign_bundle(
                self._fresh_bundle()
            )
        assert exc.value.gate == "mandate"
        assert calls == []

    def test_an_expired_bundle_refuses_before_the_gate(self, tmp_path):
        """`sign_bundle` accepts ANY bundle, so freshness cannot depend on the
        caller having come through the orchestrator."""
        from scout.live.evm.signer import SignerRefused

        art = _artifact()
        stale = build_bundle_from_artifact(
            art, intent_hash="i" * 64, wallet=GECKO_TEST_TAKER, max_age_seconds=0.001
        )
        reached = []

        class _Permitting:
            def authorize_bundle(self, bundle):
                reached.append(1)
                return {"ok": True}

        with pytest.raises(SignerRefused) as exc:
            self._boundary(_Permitting(), lambda _p: None, tmp_path).sign_bundle(stale)
        assert exc.value.gate == "expiry"
        assert reached == [], "an expired bundle reached the mandate"


class TestSuccessfulSubmissionIsPersisted:
    async def test_the_pending_receipt_is_written_not_just_returned(self):
        """*** THE DOUBLE-SPEND WINDOW. ***

        The ambiguous path persisted; the success path returned without doing so,
        leaving the durable row at `not_submitted` — the one state this module
        defines as permitting a rebuild.
        """
        from scout.live.evm.execution import EvmReceipt, ExecutionState, submit_once

        written: list = []

        async def _persist(r):
            written.append(r.state)

        class _Ok:
            async def submit(self, **kw):
                return "0x" + "cc" * 32

            async def lookup(self, **kw):
                return None

        out = await submit_once(
            broadcaster=_Ok(),
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
        assert out.state is ExecutionState.PENDING
        assert written == [
            ExecutionState.NOT_SUBMITTED,
            ExecutionState.PENDING,
        ], f"durable row after a successful submit: {written}"


# ===========================================================================
# The eight round-2 guards that mutation testing found unpinned
# ===========================================================================
#
# Each of these was added in response to a review finding, and deleting it left
# the suite green — the same shape as round one. Individually they are
# fail-closed defensive guards; collectively, unpinned guards are guards the
# next refactor reverts silently.


class TestApprovalGuardsArePinned:
    def _build(self, **over):
        from scout.live.evm.approval import build_approval_intent

        kw = dict(
            chain_id=1,
            owner=GECKO_TEST_TAKER,
            token=WETH,
            spender=ALLOWANCE_HOLDER_ADDRESS,
            required_amount=SELL_AMOUNT,
            inner_target=SETTLER_ADDRESS,
        )
        kw.update(over)
        return build_approval_intent(**kw)

    def test_the_headroom_cap_is_enforced(self):
        """Without the cap, 'bounded, never unlimited' is a check on one magic
        number: solving for UNLIMITED-1 yields ~1e60x the trade while still
        reporting is_unlimited=False."""
        from scout.live.evm.approval import MAX_HEADROOM_BPS, ApprovalRefused

        with pytest.raises(ApprovalRefused, match="outside 0"):
            self._build(headroom_bps=MAX_HEADROOM_BPS + 1)
        # And the boundary is allowed.
        assert self._build(headroom_bps=MAX_HEADROOM_BPS).amount > SELL_AMOUNT

    @pytest.mark.parametrize("bad", [100.0, "100", None])
    def test_a_non_integer_headroom_is_refused(self, bad):
        """Basis points as a float re-introduces the precision problem integer
        arithmetic was adopted to remove."""
        from scout.live.evm.approval import ApprovalRefused

        with pytest.raises(ApprovalRefused, match="must be an int"):
            self._build(headroom_bps=bad)

    @pytest.mark.parametrize(
        "sentinel",
        [
            "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            "0x0000000000000000000000000000000000000000",
        ],
    )
    def test_a_native_asset_sentinel_is_refused(self, sentinel):
        """ERC-20 approve does not exist for the chain's native asset — there is
        no contract to call."""
        from scout.live.evm.approval import ApprovalRefused

        with pytest.raises(ApprovalRefused, match="native-asset"):
            self._build(token=sentinel)

    def test_an_approval_below_the_required_amount_is_refused(self):
        """Floor division can only round down, so a zero headroom on a tiny
        amount is the boundary case."""
        from scout.live.evm.approval import ApprovalRefused

        assert self._build(required_amount=1, headroom_bps=0).amount == 1
        with pytest.raises(ApprovalRefused, match="positive"):
            self._build(required_amount=0)

    def test_the_granted_allowance_is_reconciled(self):
        from scout.live.evm.approval import reconcile_approval

        intent = self._build()
        ok, why = reconcile_approval(intent=intent, allowance_after=intent.amount)
        assert ok and why is None
        # More than authorized is a standing grant nobody asked for.
        bad, why = reconcile_approval(intent=intent, allowance_after=intent.amount * 2)
        assert not bad and "revoke" in why
        # Never re-read is not the same as reconciled.
        unknown, why = reconcile_approval(intent=intent, allowance_after=None)
        assert not unknown and "unverified" in why


class TestCalldataAndReconcileGuardsArePinned:
    def test_zero_length_actions_are_refused_per_element(self):
        """Three zero-length elements produce ('0x','0x','0x'), which is truthy —
        so an emptiness check passed a payload performing nothing."""
        from eth_abi import encode as abi_encode

        inner_body = abi_encode(
            ["(address,address,uint256)", "bytes[]", "bytes32"],
            [
                (
                    bytes.fromhex(GECKO_TEST_TAKER[2:]),
                    bytes.fromhex(USDC[2:]),
                    MIN_BUY_AMOUNT,
                ),
                [b"", b"", b""],
                b"\x00" * 32,
            ],
        )
        inner = bytes.fromhex("1fff991f") + inner_body
        payload = _wrap_outer(inner, operator=SETTLER_ADDRESS, target=SETTLER_ADDRESS)
        with pytest.raises(CalldataError, match="no selector"):
            decode_allowance_holder_calldata(payload, chain_id=1)

    @pytest.mark.parametrize("bad", ["0x2213bc0bZZ", "0x2213bc0b0", "not-hex-at-all"])
    def test_malformed_hex_raises_the_declared_type(self, bad):
        """`bytes.fromhex` raises a bare ValueError, which a caller writing
        `except CalldataError` would miss."""
        with pytest.raises(CalldataError):
            decode_allowance_holder_calldata(bad, chain_id=1)

    def test_the_settler_refusal_surfaces_as_the_adapters_declared_type(self):
        """It escaped as a bare ValueError, so a caller catching
        ZeroExArtifactError missed the single most important refusal."""
        raw = allowance_holder_response()
        raw["allowanceTarget"] = SETTLER_ADDRESS
        raw["issues"]["allowance"]["spender"] = SETTLER_ADDRESS
        raw["transaction"]["to"] = SETTLER_ADDRESS
        with pytest.raises(ZeroExArtifactError, match="NEVER be approved"):
            _artifact(raw)

    def test_reconcile_refuses_to_call_nothing_checked_reconciled(self):
        """With every delta None it reported reconciled=True, which reads in a
        log as a clean settlement when no chain state was read at all."""
        from scout.live.evm.execution import EvmReceipt, ExecutionState, reconcile

        verdict = reconcile(
            receipt=EvmReceipt(
                intent_hash="i" * 64,
                bundle_hash="b" * 64,
                chain_id=1,
                wallet=GECKO_TEST_TAKER,
                expected_transaction_hash="0x" + "ab" * 32,
                state=ExecutionState.FINALIZED,
            ),
            artifact=_artifact(),
            buy_balance_delta=None,
            sell_balance_delta=None,
            allowance_after=None,
            gas_used=None,
        )
        assert not verdict.reconciled and verdict.trips_breaker
        assert any("nothing was checked" in d for d in verdict.discrepancies)


class TestOnlyAMandateDecisionAuthorizes:
    """`if not decision` refused None/False/0 but SIGNED for True, object() and
    MagicMock() — the shape a partially-configured test double takes."""

    def _boundary(self, mandate, loader, tmp_path):
        import os

        from scout.live.evm.signer import REQUIRED_MODE, EvmSignerBoundary

        key = tmp_path / "evm.key"
        key.write_text("0x" + "11" * 32, encoding="utf-8")
        os.chmod(key, REQUIRED_MODE)
        return EvmSignerBoundary(mandate=mandate, key_path=key, key_loader=loader)

    def _fresh_bundle(self):
        return build_bundle_from_artifact(
            _artifact_now(), intent_hash="i" * 64, wallet=GECKO_TEST_TAKER
        )

    @pytest.mark.parametrize("shape", ["magicmock", "true", "object"])
    def test_a_truthy_non_decision_does_not_authorize(self, tmp_path, shape):
        from unittest.mock import MagicMock

        from scout.live.evm.signer import SignerRefused

        value = {"magicmock": MagicMock(), "true": True, "object": object()}[shape]
        read = []

        class _Truthy:
            def authorize_bundle(self, bundle):
                return value

        def _tripwire(_p):
            read.append(1)
            raise AssertionError("the key was READ")

        with pytest.raises(SignerRefused) as exc:
            self._boundary(_Truthy(), _tripwire, tmp_path).sign_bundle(
                self._fresh_bundle()
            )
        assert exc.value.gate == "mandate"
        assert "not a MandateDecision" in exc.value.message
        assert read == []

    def test_a_real_mandate_decision_does_authorize(self, tmp_path):
        """Guard on the guard: the boundary must still be able to sign."""
        from datetime import datetime, timezone

        from scout.live.mandate import MandateDecision, MandateEnvelope

        loaded = []

        class _Account:
            address = GECKO_TEST_TAKER

        class _Real:
            def authorize_bundle(self, bundle):
                return MandateDecision(
                    mode="SUPERVISED_LIVE",
                    venue="zeroex-allowance-holder",
                    venue_family="dex",
                    intent_hash=bundle.intent_hash,
                    envelope=MandateEnvelope(
                        per_trade_max_notional_usd=Decimal("500"),
                        daily_max_notional_usd=Decimal("500"),
                        max_open_positions=1,
                    ),
                    supervised_reconciled=None,
                    decided_at=datetime.now(timezone.utc),
                )

        def _loader(_p):
            loaded.append(1)
            return _Account()

        session = self._boundary(_Real(), _loader, tmp_path).sign_bundle(
            self._fresh_bundle()
        )
        assert loaded == [1]
        assert session.bundle.verify()


class TestTheIntentFloorCannotBeForgotten:
    """*** A FORGOTTEN FLOOR MUST NOT MEAN "NO FLOOR". ***

    `ZeroExQuoteRequest.expected_min_buy_amount` carries a dataclass default of
    0, so a caller can omit it. That default must refuse rather than disable the
    only bound anchored outside the 0x response.
    """

    def test_the_default_of_zero_refuses(self):
        req = ZeroExQuoteRequest(
            chain_id=1,
            sell_token=WETH,
            buy_token=USDC,
            sell_amount=SELL_AMOUNT,
            taker=GECKO_TEST_TAKER,
        )
        assert req.expected_min_buy_amount == 0
        with pytest.raises(ZeroExArtifactError, match="must be positive"):
            ZeroExAllowanceHolderAdapter(chain_id=1).validate_response(
                allowance_holder_response(), request=req
            )

    def test_a_floor_above_the_quote_refuses(self):
        """The intent will not accept less than it asked for, even from an
        otherwise perfectly consistent quote."""
        with pytest.raises(ZeroExArtifactError, match="intent's floor"):
            _artifact(expected_min_buy_amount=MIN_BUY_AMOUNT + 1)

    def test_a_floor_at_or_below_the_quote_passes(self):
        """Guard on the guard."""
        assert _artifact(expected_min_buy_amount=MIN_BUY_AMOUNT).minimum_buy_amount == (
            MIN_BUY_AMOUNT
        )

    @pytest.mark.parametrize("truthy", [True, False])
    def test_a_bool_floor_is_refused(self, truthy):
        """*** `True` IS AN `int`, AND IT MEANS NO FLOOR. ***

        `bool` subclasses `int`, so `True > 0` and a positivity check alone
        admits it — as a floor of ONE BASE UNIT, which bounds nothing. The
        realistic route in is a caller writing `expected_min_buy_amount=bool(...)`
        or passing a truthiness test's result where a quantity was wanted; the
        value then reads as "yes, we have a floor" in every log while permitting
        a swap of ~$188 of WETH for a millionth of a USDC.

        `False` is caught by the positivity check either way; it is parametrized
        so that deleting the `isinstance` clause fails on `True` alone rather
        than being masked.
        """
        with pytest.raises(ZeroExArtifactError, match="must not be a bool"):
            _artifact(expected_min_buy_amount=truthy)

    def test_the_bool_refusal_is_not_masked_by_the_ordinary_floor_check(self):
        """`True` must be refused for BEING a bool, not for being too small — a
        floor of 1 is below the quote's minimum, so an implementation that only
        compared magnitudes would let it through."""
        assert MIN_BUY_AMOUNT > 1  # the ordinary comparison would not fire
        with pytest.raises(ZeroExArtifactError) as exc:
            _artifact(expected_min_buy_amount=True)
        assert "intent's floor" not in str(exc.value)
