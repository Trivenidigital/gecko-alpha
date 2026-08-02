"""Provider-neutral execution receipts.

The load-bearing assertion is `verify_binding`: a receipt that NAMES an intent is
not the same as one BOUND to it, and the two fields it holds arrive from different
places.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from scout.live.adapter_base import OrderConfirmation
from scout.live.intent import TradeIntent
from scout.live.order_id import client_order_id_for_venue
from scout.live.receipts import (
    ExecutionReceipt,
    receipt_from_order_confirmation,
    receipt_from_solana_execution,
)

_T0 = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


def _intent(**overrides) -> TradeIntent:
    kw = dict(
        strategy_id="first_signal",
        decision_id="paper-1",
        created_at=_T0,
        expires_at=_T0 + timedelta(minutes=5),
        execution_deadline=_T0 + timedelta(minutes=5),
        mode="SUPERVISED_LIVE",
        policy_version="engine-dispatch-v1",
        venue_family="cex",
        preferred_venue="binance",
        base_asset="BTC",
        quote_asset="USDT",
        side="buy",
        exact_quantity=D("100"),
        quantity_denomination="quote",
        maximum_notional=D("100"),
        order_type="market",
        maximum_slippage_bps=50,
        maximum_price_impact_bps=50,
    )
    kw.update(overrides)
    return TradeIntent(**kw)


def _confirmation(**overrides) -> OrderConfirmation:
    kw = dict(
        venue="binance",
        venue_order_id="12345",
        client_order_id="gecko-abc",
        status="filled",
        filled_qty=1.5,
        fill_price=100.25,
        raw_response={"orderId": 12345},
    )
    kw.update(overrides)
    return OrderConfirmation(**kw)


class TestNormalization:
    @pytest.mark.parametrize(
        "venue_status,expected",
        [
            ("filled", "filled"),
            ("partial", "partial"),
            ("rejected", "rejected"),
            ("pending", "pending"),
            ("timeout", "unknown"),
        ],
    )
    def test_cex_statuses_map(self, venue_status, expected):
        receipt = receipt_from_order_confirmation(_confirmation(status=venue_status))
        assert receipt.status == expected

    def test_a_timeout_is_unknown_and_not_pending(self):
        """A request that timed out may have reached the matching engine. The
        difference between 'not yet' and 'cannot say' decides whether a retry is
        safe, so it must not be flattened."""
        assert receipt_from_order_confirmation(
            _confirmation(status="timeout")
        ).status == ("unknown")

    @pytest.mark.parametrize(
        "state,expected",
        [
            ("landed", "filled"),
            ("confirmed", "filled"),
            ("finalized", "filled"),
            ("reconciled", "filled"),
            ("failed", "rejected"),
            ("submission_unknown", "unknown"),
            ("submission_attempted", "unknown"),
            ("quote_created", "pending"),
            ("awaiting_authorization", "pending"),
        ],
    )
    def test_solana_states_map(self, state, expected):
        receipt = receipt_from_solana_execution({"state": state})
        assert receipt.status == expected

    def test_an_unrecognised_status_becomes_unknown_never_filled(self):
        """*** The mapping is lossy in ONE direction only. ***

        A status this module has not been taught is a status it must not claim to
        understand — and reporting it as filled would tell a reconciler a position
        exists.
        """
        assert (
            receipt_from_order_confirmation(
                _confirmation(status="brand_new_venue_state")
            ).status
            == "unknown"
        )
        assert receipt_from_solana_execution({"state": "who_knows"}).status == "unknown"

    def test_a_submission_that_may_have_landed_is_never_reported_rejected(self):
        assert (
            receipt_from_solana_execution({"state": "submission_unknown"}).status
            != "rejected"
        )

    def test_every_solana_lane_state_is_mapped(self):
        """Pinned to the lane's own state list: a state added there and not here
        would silently become 'unknown', which is safe but hides a real state."""
        from scout.live import solana_lane as lane
        from scout.live.receipts import _SOLANA_STATE_MAP

        lane_states = {
            value
            for name, value in vars(lane).items()
            if name.startswith("STATE_") and isinstance(value, str)
        }
        assert lane_states <= set(
            _SOLANA_STATE_MAP
        ), f"unmapped lane states: {lane_states - set(_SOLANA_STATE_MAP)}"

    def test_a_non_finite_fill_price_becomes_none(self):
        """A receipt carrying fill_price=Infinity would satisfy every downstream
        positivity guard and then compare greater than any bound."""
        receipt = receipt_from_order_confirmation(
            _confirmation(fill_price=float("inf"), filled_qty=float("nan"))
        )
        assert receipt.fill_price is None
        assert receipt.filled_quantity is None

    def test_venue_reference_carries_the_provider_identity(self):
        assert receipt_from_order_confirmation(_confirmation()).venue_ref == "12345"
        assert (
            receipt_from_solana_execution(
                {"state": "reconciled", "expected_signature": "3oUrwF"}
            ).venue_ref
            == "3oUrwF"
        )


class TestConstruction:
    def test_an_unknown_status_cannot_be_constructed_directly(self):
        with pytest.raises(ValueError, match="status"):
            ExecutionReceipt(
                venue="binance",
                venue_family="cex",
                status="probably_fine",
                client_order_id=None,
                intent_hash=None,
                venue_ref=None,
            )

    def test_an_unknown_venue_family_cannot_be_constructed(self):
        with pytest.raises(ValueError, match="venue_family"):
            ExecutionReceipt(
                venue="binance",
                venue_family="otc",
                status="filled",
                client_order_id=None,
                intent_hash=None,
                venue_ref=None,
            )

    def test_only_pending_is_non_terminal(self):
        def _r(status):
            return ExecutionReceipt(
                venue="binance",
                venue_family="cex",
                status=status,
                client_order_id=None,
                intent_hash=None,
                venue_ref=None,
            )

        assert not _r("pending").is_terminal
        for status in ("filled", "partial", "rejected", "unknown"):
            assert _r(status).is_terminal


class TestBinding:
    def test_a_correctly_bound_cex_receipt_verifies(self):
        intent = _intent()
        cid = client_order_id_for_venue(intent, "binance")
        receipt = receipt_from_order_confirmation(
            _confirmation(client_order_id=cid), intent_hash=intent.intent_hash
        )
        assert receipt.verify_binding(intent)

    def test_a_receipt_with_no_intent_hash_does_not_verify(self):
        """Naming nothing is not binding to something."""
        intent = _intent()
        cid = client_order_id_for_venue(intent, "binance")
        receipt = receipt_from_order_confirmation(_confirmation(client_order_id=cid))
        assert not receipt.verify_binding(intent)

    def test_a_copied_hash_with_the_wrong_order_id_does_not_verify(self):
        """*** Both halves must hold. ***

        The hash comes from our ledger and the id is echoed by the venue. Holding
        both proves nothing about whether they belong together — which is exactly
        the state a reconciler must not resume from.
        """
        intent = _intent()
        receipt = receipt_from_order_confirmation(
            _confirmation(client_order_id="gecko-someone-elses-order"),
            intent_hash=intent.intent_hash,
        )
        assert not receipt.verify_binding(intent)

    def test_a_correct_order_id_with_the_wrong_hash_does_not_verify(self):
        intent = _intent()
        cid = client_order_id_for_venue(intent, "binance")
        receipt = receipt_from_order_confirmation(
            _confirmation(client_order_id=cid), intent_hash="0" * 64
        )
        assert not receipt.verify_binding(intent)

    def test_a_receipt_does_not_verify_against_a_different_intent(self):
        intent = _intent()
        cid = client_order_id_for_venue(intent, "binance")
        receipt = receipt_from_order_confirmation(
            _confirmation(client_order_id=cid), intent_hash=intent.intent_hash
        )
        assert not receipt.verify_binding(_intent(side="sell"))

    def test_a_dex_receipt_binds_through_the_hash_alone(self):
        """Solana has no client-order-id concept. Requiring one it cannot produce
        would make every DEX receipt unverifiable; inventing one would assert a
        binding the chain cannot corroborate."""
        intent = _intent(venue_family="dex", chain="solana", preferred_venue="jupiter")
        receipt = receipt_from_solana_execution(
            {"state": "reconciled", "expected_signature": "sig"},
            intent_hash=intent.intent_hash,
        )
        assert receipt.client_order_id is None
        assert receipt.verify_binding(intent)

    def test_a_dex_receipt_with_the_wrong_hash_does_not_verify(self):
        intent = _intent(venue_family="dex", chain="solana", preferred_venue="jupiter")
        receipt = receipt_from_solana_execution(
            {"state": "reconciled"}, intent_hash="0" * 64
        )
        assert not receipt.verify_binding(intent)

    def test_binding_never_raises_on_absent_fields(self):
        """Callers fail closed on False, not on an exception they might catch too
        broadly and treat as a pass."""
        receipt = ExecutionReceipt(
            venue="nowhere",
            venue_family="cex",
            status="unknown",
            client_order_id="x",
            intent_hash=None,
            venue_ref=None,
        )
        assert receipt.verify_binding(_intent()) is False
