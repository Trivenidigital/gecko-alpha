"""PR-S1: tx_inspector — content verification of the transaction Jupiter built.

Every adversarial case is a REAL compiled v0 transaction (see
tests/solana_tx_builder.py) that differs from the happy path in exactly one
property, so a failure names the check that caught it.
"""

from __future__ import annotations

import base64

import pytest
from solders.keypair import Keypair

from scout.live.solana.constants import SOL_MINT, USDC_MINT
from scout.live.solana.exceptions import SolanaVerificationError
from scout.live.solana.tx_inspector import (
    derive_associated_token_address,
    verify_swap_transaction,
)

# Bare import, not `tests.solana_tx_builder`: pytest's prepend import mode puts
# this file's own directory on sys.path, whereas the `tests.` prefix depends on
# the repo root being importable AND on no `tests` package existing in
# site-packages — one does on some dev boxes, which shadows the local dir.
from solana_tx_builder import (
    ALT,
    OTHER_PUBKEY,
    PAYER_PUBKEY,
    STRANGER,
    TIP_ACCOUNT,
    build_swap_tx,
)


def _check(report, name):
    matches = [c for c in report.checks if c.name == name]
    assert matches, f"no check named {name!r}; have {[c.name for c in report.checks]}"
    return matches[0]


def _failed_names(report):
    return {c.name for c in report.failures}


async def test_happy_path_passes_every_check(settings_factory):
    built = build_swap_tx()
    report = await verify_swap_transaction(
        tx_b64=built.tx_b64,
        expected_signer=PAYER_PUBKEY,
        settings=settings_factory(),
    )

    assert report.passed, f"unexpected failures: {_failed_names(report)}"
    assert report.fee_payer == PAYER_PUBKEY
    assert report.num_required_signatures == 1
    assert report.jito_tip_lamports == 100_000
    assert report.jito_tip_destination == TIP_ACCOUNT
    # 1000 micro-lamports/CU * 200_000 CU / 1e6 = 200 lamports.
    assert report.priority_fee_lamports == 200
    assert report.total_fee_lamports == 5_000 + 200 + 100_000
    assert len(report.message_sha256) == 64
    report.raise_if_failed()  # must not raise


async def test_wrong_signer_is_caught(settings_factory):
    """The transaction is well-formed but built for somebody else's wallet."""
    built = build_swap_tx()
    report = await verify_swap_transaction(
        tx_b64=built.tx_b64,
        expected_signer=OTHER_PUBKEY,
        settings=settings_factory(),
    )

    assert not report.passed
    assert "fee_payer_is_expected_signer" in _failed_names(report)
    with pytest.raises(SolanaVerificationError, match="fee_payer_is_expected_signer"):
        report.raise_if_failed()


async def test_extra_required_signer_is_caught(settings_factory):
    """A second required signer means a co-signer we did not authorise."""
    built = build_swap_tx(extra_signer=OTHER_PUBKEY)
    report = await verify_swap_transaction(
        tx_b64=built.tx_b64,
        expected_signer=PAYER_PUBKEY,
        settings=settings_factory(),
    )

    assert not report.passed
    failed = _failed_names(report)
    assert "single_required_signer" in failed
    assert "no_additional_signers" in failed
    assert report.num_required_signatures == 2


async def test_unexpected_output_mint_is_caught(settings_factory):
    """Route ends in a token that is not the USDC we asked for."""
    decoy = "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"  # not USDC
    built = build_swap_tx(mints=(SOL_MINT, decoy))
    report = await verify_swap_transaction(
        tx_b64=built.tx_b64,
        expected_signer=PAYER_PUBKEY,
        settings=settings_factory(),
    )

    assert not report.passed
    assert "expected_mints_present" in _failed_names(report)
    assert USDC_MINT in _check(report, "expected_mints_present").detail


async def test_tip_over_ceiling_is_caught(settings_factory):
    built = build_swap_tx(tip_lamports=750_000)
    report = await verify_swap_transaction(
        tx_b64=built.tx_b64,
        expected_signer=PAYER_PUBKEY,
        settings=settings_factory(SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS=500_000),
    )

    assert not report.passed
    assert "jito_tip_within_ceiling" in _failed_names(report)
    assert report.jito_tip_lamports == 750_000


async def test_priority_fee_over_ceiling_is_caught(settings_factory):
    """200_000 micro-lamports/CU over 1_000_000 CU = 200_000 lamports."""
    built = build_swap_tx(compute_unit_price=200_000, compute_unit_limit=1_000_000)
    report = await verify_swap_transaction(
        tx_b64=built.tx_b64,
        expected_signer=PAYER_PUBKEY,
        settings=settings_factory(SOLANA_PILOT_MAX_PRIORITY_FEE_LAMPORTS=10_000),
    )

    assert not report.passed
    assert "priority_fee_within_ceiling" in _failed_names(report)
    assert report.priority_fee_lamports == 200_000


async def test_missing_compute_unit_limit_assumes_protocol_maximum(settings_factory):
    """No explicit limit must be priced at the ceiling, not treated as zero.

    Under-estimating here would let a transaction past a fee ceiling it
    actually breaches, which is the exact failure this default exists to
    prevent.
    """
    built = build_swap_tx(compute_unit_limit=None, compute_unit_price=1_000)
    report = await verify_swap_transaction(
        tx_b64=built.tx_b64,
        expected_signer=PAYER_PUBKEY,
        settings=settings_factory(),
    )

    # 1_000 * 1_400_000 / 1e6 = 1_400 lamports.
    assert report.priority_fee_lamports == 1_400
    assert report.compute_unit_limit is None


async def test_transfer_to_unknown_destination_is_caught(settings_factory):
    """A lamport transfer to an address that is neither ours nor a Jito tip."""
    built = build_swap_tx(extra_transfer_dest=STRANGER, extra_transfer_lamports=250_000)
    report = await verify_swap_transaction(
        tx_b64=built.tx_b64,
        expected_signer=PAYER_PUBKEY,
        settings=settings_factory(),
    )

    assert not report.passed
    assert "transfer_destinations_known" in _failed_names(report)
    assert STRANGER in _check(report, "transfer_destinations_known").detail
    # The tip check must not be confused by the extra transfer.
    assert report.jito_tip_lamports == 100_000


async def test_unknown_program_is_caught(settings_factory):
    """An instruction targeting a program outside the closed allowlist."""
    rogue = "MEViEnscUm6tsQRoGd9h6nLQaQspKj7DB2M5FwM3Xvz"
    built = build_swap_tx(swap_program_id=rogue)
    report = await verify_swap_transaction(
        tx_b64=built.tx_b64,
        expected_signer=PAYER_PUBKEY,
        settings=settings_factory(),
    )

    assert not report.passed
    assert "program_ids_allowlisted" in _failed_names(report)
    assert rogue in _check(report, "program_ids_allowlisted").detail


async def test_prefilled_signature_is_caught(settings_factory):
    """A transaction that already carries somebody's signature."""
    built = build_swap_tx(sign=True)
    report = await verify_swap_transaction(
        tx_b64=built.tx_b64,
        expected_signer=PAYER_PUBKEY,
        settings=settings_factory(),
    )

    assert not report.passed
    assert "arrives_unsigned" in _failed_names(report)


async def test_tip_to_non_jito_account_is_not_counted_as_a_tip(settings_factory):
    """A 'tip' to an address that is not Jito's is an unknown transfer.

    The dangerous shape: the request asked for a tip, so a naive check that
    trusts the REQUEST would see one. Only the destination proves it.
    """
    built = build_swap_tx(tip_destination=STRANGER, tip_lamports=100_000)
    report = await verify_swap_transaction(
        tx_b64=built.tx_b64,
        expected_signer=PAYER_PUBKEY,
        settings=settings_factory(),
    )

    assert not report.passed
    assert "transfer_destinations_known" in _failed_names(report)
    assert report.jito_tip_lamports == 0
    assert report.jito_tip_destination is None


async def test_unparseable_expected_signer_fails_closed(settings_factory):
    """A malformed signer must fail the report, not raise out of the inspector.

    With the own-account allowlist underivable, every transfer must also be
    treated as unknown rather than waved through.
    """
    built = build_swap_tx()
    report = await verify_swap_transaction(
        tx_b64=built.tx_b64,
        expected_signer="not-a-real-pubkey",
        settings=settings_factory(),
    )

    assert not report.passed
    failed = _failed_names(report)
    assert "own_accounts_derivable" in failed
    assert "transfer_destinations_known" in failed


async def test_garbage_input_fails_closed(settings_factory):
    report = await verify_swap_transaction(
        tx_b64="not-valid-base64!!!",
        expected_signer=PAYER_PUBKEY,
        settings=settings_factory(),
    )

    assert not report.passed
    assert "deserialize" in _failed_names(report)
    with pytest.raises(SolanaVerificationError):
        report.raise_if_failed()


async def test_degenerate_transaction_fails_closed(settings_factory):
    """A run of zero bytes DOES parse — as an empty legacy transaction.

    Worth pinning: deserialization succeeding is not evidence of anything, so
    the content checks have to be what rejects this. They do — no signer, an
    all-zero blockhash and no mints.
    """
    report = await verify_swap_transaction(
        tx_b64=base64.b64encode(b"\x00" * 40).decode(),
        expected_signer=PAYER_PUBKEY,
        settings=settings_factory(),
    )

    assert not report.passed
    assert _failed_names(report) >= {
        "single_required_signer",
        "fee_payer_is_expected_signer",
        "recent_blockhash_present",
        "expected_mints_present",
    }
    with pytest.raises(SolanaVerificationError):
        report.raise_if_failed()


async def test_total_fee_ceiling_catches_stacked_components(settings_factory):
    """Both components individually under ceiling, combined over it."""
    built = build_swap_tx(
        tip_lamports=400_000, compute_unit_price=2_000, compute_unit_limit=200_000
    )
    report = await verify_swap_transaction(
        tx_b64=built.tx_b64,
        expected_signer=PAYER_PUBKEY,
        settings=settings_factory(
            SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS=500_000,
            SOLANA_PILOT_MAX_PRIORITY_FEE_LAMPORTS=500_000,
            SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS=1_005_000,
        ),
    )

    assert _check(report, "jito_tip_within_ceiling").passed
    assert _check(report, "priority_fee_within_ceiling").passed
    # 5_000 base + 400 priority + 400_000 tip = 405_400, under 1_005_000.
    assert report.total_fee_lamports == 405_400
    assert report.passed


async def test_custom_tip_account_list_is_honoured(settings_factory):
    """A rotated tip account resolves via the live list, not the fallback."""
    rotated = "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE"
    built = build_swap_tx(tip_destination=rotated, tip_lamports=50_000)

    stale = await verify_swap_transaction(
        tx_b64=built.tx_b64,
        expected_signer=PAYER_PUBKEY,
        settings=settings_factory(),
    )
    assert not stale.passed  # unknown under the static fallback

    fresh = await verify_swap_transaction(
        tx_b64=built.tx_b64,
        expected_signer=PAYER_PUBKEY,
        settings=settings_factory(),
        tip_accounts={rotated},
    )
    assert fresh.passed, f"unexpected failures: {_failed_names(fresh)}"
    assert fresh.jito_tip_lamports == 50_000
    assert fresh.jito_tip_destination == rotated


class _FakeAltRpc:
    """Minimal stand-in exposing only the method the inspector calls."""

    def __init__(self, tables: dict[str, list[str]] | None = None, raises=None):
        self.tables = tables or {}
        self.raises = raises
        self.calls: list[list[str]] = []

    async def fetch_address_lookup_tables(self, table_keys, **_kw):
        self.calls.append(list(table_keys))
        if self.raises is not None:
            raise self.raises
        return {k: v for k, v in self.tables.items() if k in table_keys}


async def test_lookup_tables_without_rpc_client_fail_closed(settings_factory):
    """ALT-bearing transaction + no resolver = failure, never a skipped check."""
    built = build_swap_tx(lookup_tables=[ALT])
    report = await verify_swap_transaction(
        tx_b64=built.tx_b64,
        expected_signer=PAYER_PUBKEY,
        settings=settings_factory(),
        rpc_client=None,
    )

    assert not report.passed
    assert "lookup_tables_resolved" in _failed_names(report)
    assert "no rpc_client" in _check(report, "lookup_tables_resolved").detail


async def test_lookup_tables_resolved_via_rpc(settings_factory):
    """With the table resolved, ALT-hosted mints satisfy the mint check."""
    built = build_swap_tx(lookup_tables=[ALT])
    rpc = _FakeAltRpc({str(ALT.key): [str(a) for a in ALT.addresses]})

    report = await verify_swap_transaction(
        tx_b64=built.tx_b64,
        expected_signer=PAYER_PUBKEY,
        settings=settings_factory(),
        rpc_client=rpc,
    )

    assert rpc.calls == [[str(ALT.key)]]
    assert report.passed, f"unexpected failures: {_failed_names(report)}"
    assert _check(report, "expected_mints_present").passed
    assert report.unresolved_lookup_tables == ()


async def test_lookup_table_fetch_failure_fails_closed(settings_factory):
    """An RPC error resolving the table is 'we could not look' — not a pass."""
    built = build_swap_tx(lookup_tables=[ALT])
    rpc = _FakeAltRpc(raises=RuntimeError("rpc exploded"))

    report = await verify_swap_transaction(
        tx_b64=built.tx_b64,
        expected_signer=PAYER_PUBKEY,
        settings=settings_factory(),
        rpc_client=rpc,
    )

    assert not report.passed
    assert "lookup_tables_resolved" in _failed_names(report)
    assert "rpc exploded" in _check(report, "lookup_tables_resolved").detail


async def test_unresolved_lookup_table_is_reported_and_fails_closed(settings_factory):
    """A table the RPC did not return must fail, and be named in the report."""
    built = build_swap_tx(lookup_tables=[ALT])
    rpc = _FakeAltRpc({})  # table simply not returned

    report = await verify_swap_transaction(
        tx_b64=built.tx_b64,
        expected_signer=PAYER_PUBKEY,
        settings=settings_factory(),
        rpc_client=rpc,
    )

    assert not report.passed
    failed = _failed_names(report)
    assert "lookup_tables_resolved" in failed
    assert report.unresolved_lookup_tables == (str(ALT.key),)


def test_derive_associated_token_address_is_deterministic():
    """ATA derivation must be stable — the wrap-destination check depends on it."""
    first = derive_associated_token_address(PAYER_PUBKEY, SOL_MINT)
    second = derive_associated_token_address(PAYER_PUBKEY, SOL_MINT)
    assert first == second
    assert first != derive_associated_token_address(PAYER_PUBKEY, USDC_MINT)
    assert first != derive_associated_token_address(OTHER_PUBKEY, SOL_MINT)


def test_builder_produces_distinct_keypairs():
    assert PAYER_PUBKEY != OTHER_PUBKEY
    assert (
        Keypair.from_seed(bytes([11]) * 32).pubkey()
        == Keypair.from_seed(bytes([11]) * 32).pubkey()
    )
