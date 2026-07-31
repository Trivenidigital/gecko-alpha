"""PR-S1: §7 resolver — the four verdicts and the conservatism between them.

The verdict that matters most is ``definitively_not_submitted``, because it is
the only one that licenses building and signing a replacement transaction. It
requires BOTH a clean absence AND an expired blockhash, twice. Every test here
that reaches ``unresolved`` is a test that the resolver refused to guess.
"""

from __future__ import annotations

import pytest

from scout.live.solana.resolver import resolve_submission
from scout.live.solana.rpc_client import SignatureStatus

_SIGNATURE = "4Rf81Nd6uXrp39hFFr1WMFnGjzxyGXrvwb8VSRUnotH1uTbzLEsatgthdk1mR2yUq2P1Bz5HXoccfQX7YTUgffwy"
_LAST_VALID = 283_000_500


class FakeRpc:
    """Scripted RPC. Each sweep pops the next status / block height."""

    def __init__(self, statuses, block_heights=None, balances=None, raises=None):
        self._statuses = list(statuses)
        self._heights = list(block_heights or [])
        self._balances = balances or {}
        self._raises = raises
        self.status_calls = 0
        self.height_calls = 0

    async def get_signature_statuses(self, signatures, **_kw):
        self.status_calls += 1
        if self._raises is not None:
            raise self._raises
        return [self._statuses.pop(0)]

    async def get_block_height(self, **_kw):
        self.height_calls += 1
        return self._heights.pop(0)

    async def get_balance(self, _owner, **_kw):
        return self._balances.get("sol", 0)

    async def get_token_balance(self, _owner, mint, **_kw):
        return self._balances.get(mint, 0)


async def test_landed(settings_factory):
    rpc = FakeRpc(
        [
            SignatureStatus(
                known=True, slot=283_000_400, err=None, confirmation_status="finalized"
            )
        ]
    )
    report = await resolve_submission(
        expected_signature=_SIGNATURE,
        last_valid_block_height=_LAST_VALID,
        rpc_client=rpc,
        settings=settings_factory(SOLANA_SUBMISSION_SETTLE_SEC=0.0),
    )

    assert report.verdict == "landed"
    assert report.slot == 283_000_400
    assert report.confirmation_status == "finalized"
    assert report.rebuild_is_safe is False
    # Found on the first sweep — no need for a second.
    assert rpc.status_calls == 1


async def test_failed_on_chain(settings_factory):
    """It DID run and the fee was paid. A rebuild here is a second swap."""
    rpc = FakeRpc(
        [
            SignatureStatus(
                known=True,
                slot=1,
                err={"InstructionError": [3, "Custom"]},
                confirmation_status="confirmed",
            )
        ]
    )
    report = await resolve_submission(
        expected_signature=_SIGNATURE,
        last_valid_block_height=_LAST_VALID,
        rpc_client=rpc,
        settings=settings_factory(SOLANA_SUBMISSION_SETTLE_SEC=0.0),
    )

    assert report.verdict == "failed_on_chain"
    assert report.on_chain_err == {"InstructionError": [3, "Custom"]}
    assert report.rebuild_is_safe is False


async def test_definitively_not_submitted_requires_absence_and_expiry(settings_factory):
    """Absent in BOTH sweeps AND the blockhash has expired in both."""
    rpc = FakeRpc(
        [SignatureStatus(known=False), SignatureStatus(known=False)],
        block_heights=[_LAST_VALID + 10, _LAST_VALID + 20],
    )
    report = await resolve_submission(
        expected_signature=_SIGNATURE,
        last_valid_block_height=_LAST_VALID,
        rpc_client=rpc,
        settings=settings_factory(SOLANA_SUBMISSION_SETTLE_SEC=0.0),
    )

    assert report.verdict == "definitively_not_submitted"
    assert report.rebuild_is_safe is True
    assert rpc.status_calls == 2, "must take two sweeps before licensing a rebuild"
    assert all(p.blockhash_expired for p in report.probes)


async def test_absent_but_blockhash_still_valid_is_unresolved(settings_factory):
    """In-flight transactions are absent right up until they land."""
    rpc = FakeRpc([SignatureStatus(known=False)], block_heights=[_LAST_VALID - 50])
    report = await resolve_submission(
        expected_signature=_SIGNATURE,
        last_valid_block_height=_LAST_VALID,
        rpc_client=rpc,
        settings=settings_factory(SOLANA_SUBMISSION_SETTLE_SEC=0.0),
    )

    assert report.verdict == "unresolved"
    assert report.rebuild_is_safe is False
    assert report.probes[0].blockhash_expired is False


async def test_transport_error_is_unresolved_never_not_submitted(settings_factory):
    """THE conservatism property: a failed probe is 'we could not look'."""
    rpc = FakeRpc([], raises=ConnectionError("rpc unreachable"))
    report = await resolve_submission(
        expected_signature=_SIGNATURE,
        last_valid_block_height=_LAST_VALID,
        rpc_client=rpc,
        settings=settings_factory(SOLANA_SUBMISSION_SETTLE_SEC=0.0),
    )

    assert report.verdict == "unresolved"
    assert report.rebuild_is_safe is False
    assert report.probes[0].outcome == "error"
    assert report.probes[0].error_type == "ConnectionError"


async def test_block_height_probe_failure_is_unresolved(settings_factory):
    """Cannot test expiry => cannot claim definitive absence."""

    class _HeightFails(FakeRpc):
        async def get_block_height(self, **_kw):
            raise TimeoutError("height probe timed out")

    rpc = _HeightFails([SignatureStatus(known=False)])
    report = await resolve_submission(
        expected_signature=_SIGNATURE,
        last_valid_block_height=_LAST_VALID,
        rpc_client=rpc,
        settings=settings_factory(SOLANA_SUBMISSION_SETTLE_SEC=0.0),
    )

    assert report.verdict == "unresolved"
    assert report.probes[0].outcome == "error"


async def test_missing_last_valid_block_height_makes_definitive_unreachable(
    settings_factory,
):
    """Without the expiry bound, absence can never become definitive."""
    rpc = FakeRpc(
        [SignatureStatus(known=False), SignatureStatus(known=False)],
        block_heights=[_LAST_VALID + 10, _LAST_VALID + 20],
    )
    report = await resolve_submission(
        expected_signature=_SIGNATURE,
        last_valid_block_height=None,
        rpc_client=rpc,
        settings=settings_factory(SOLANA_SUBMISSION_SETTLE_SEC=0.0),
    )

    assert report.verdict == "unresolved"
    assert report.rebuild_is_safe is False


async def test_late_landing_between_sweeps_is_caught(settings_factory):
    """Absent on sweep 1, landed on sweep 2 — exactly why there are two.

    A single-sweep resolver would have called this 'not submitted' and a
    caller would have rebuilt, producing two swaps.
    """
    rpc = FakeRpc(
        [SignatureStatus(known=False), SignatureStatus(known=True, slot=9, err=None)],
        block_heights=[_LAST_VALID + 1],
    )
    report = await resolve_submission(
        expected_signature=_SIGNATURE,
        last_valid_block_height=_LAST_VALID,
        rpc_client=rpc,
        settings=settings_factory(SOLANA_SUBMISSION_SETTLE_SEC=0.0),
    )

    assert report.verdict == "landed"
    assert report.rebuild_is_safe is False


async def test_balances_are_recorded_but_do_not_decide(settings_factory):
    """Balance movement is evidence for a human, never an input to the verdict."""
    from scout.live.solana.constants import USDC_MINT

    rpc = FakeRpc(
        [SignatureStatus(known=False)],
        block_heights=[_LAST_VALID - 1],
        balances={"sol": 1_400_000_000, USDC_MINT: 9_000_000},
    )
    report = await resolve_submission(
        expected_signature=_SIGNATURE,
        last_valid_block_height=_LAST_VALID,
        rpc_client=rpc,
        settings=settings_factory(SOLANA_SUBMISSION_SETTLE_SEC=0.0),
        owner_pubkey="7v54NWdBtkjuAFJrLGsS2SXnuk8nKam81mZJeeYxVFi9",
    )

    # A big USDC balance looks like a landed swap; the verdict must ignore it.
    assert report.balances["output_token"] == 9_000_000
    assert report.verdict == "unresolved"


async def test_report_carries_full_probe_trail(settings_factory):
    rpc = FakeRpc(
        [SignatureStatus(known=False), SignatureStatus(known=False)],
        block_heights=[_LAST_VALID + 5, _LAST_VALID + 6],
    )
    report = await resolve_submission(
        expected_signature=_SIGNATURE,
        last_valid_block_height=_LAST_VALID,
        rpc_client=rpc,
        settings=settings_factory(SOLANA_SUBMISSION_SETTLE_SEC=0.0),
    )

    assert len(report.probes) == 2
    assert [p.sweep for p in report.probes] == [0, 1]
    assert report.signature == _SIGNATURE
    assert report.last_valid_block_height == _LAST_VALID
    assert report.checked_at.endswith("+00:00")
