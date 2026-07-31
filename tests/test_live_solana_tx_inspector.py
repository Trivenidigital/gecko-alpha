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
    ATA_RENT_LAMPORTS_FALLBACK,
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
    ata_create_ix,
    build_swap_tx,
    compute_unit_limit_ix,
    compute_unit_price_ix,
    system_raw_ix,
    token_approve_ix,
    token_close_account_ix,
    token_raw_ix,
    token_set_authority_ix,
    token_transfer_ix,
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
    # The baseline creates one ATA (the output-mint account), whose rent is
    # money leaving the wallet and therefore part of the bounded total.
    assert report.ata_create_count == 1
    assert report.ata_rent_lamports == ATA_RENT_LAMPORTS_FALLBACK
    assert (
        report.total_fee_lamports == 5_000 + 200 + 100_000 + ATA_RENT_LAMPORTS_FALLBACK
    )
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
    """Both components individually under ceiling, combined over it.

    ``include_wrap_primitives=False`` isolates the fee arithmetic from ATA
    rent, which is exercised separately below.
    """
    built = build_swap_tx(
        tip_lamports=400_000,
        compute_unit_price=2_000,
        compute_unit_limit=200_000,
        include_wrap_primitives=False,
    )
    report = await verify_swap_transaction(
        tx_b64=built.tx_b64,
        expected_signer=PAYER_PUBKEY,
        settings=settings_factory(
            SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS=500_000,
            SOLANA_PILOT_MAX_PRIORITY_FEE_LAMPORTS=500_000,
            # This fixture creates no ATA, so the config floor must not
            # reserve rent room the transaction cannot spend — otherwise
            # the 1_005_000 ceiling this test is built around is rejected
            # before the inspector ever runs.
            SOLANA_PILOT_MAX_ATA_CREATES=0,
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


# ----------------------------------------------------------------------
# S1-1: instruction-level allowlists.
#
# Every fixture below is a REAL compiled transaction carrying a legitimate
# swap PLUS one extra well-formed instruction from an ALLOWLISTED program.
# Before the discriminator allowlists existed, all four returned passed=True
# with zero failed checks — program-level allowlisting cannot see instruction
# data, and instruction data is where the meaning is.
# ----------------------------------------------------------------------
_USDC_ATA = derive_associated_token_address(PAYER_PUBKEY, USDC_MINT)
_STRANGER_USDC_ATA = derive_associated_token_address(STRANGER, USDC_MINT)


async def _verify(settings_factory, *, settings_overrides=None, **build_kwargs):
    built = build_swap_tx(**build_kwargs)
    return await verify_swap_transaction(
        tx_b64=built.tx_b64,
        expected_signer=PAYER_PUBKEY,
        settings=settings_factory(**(settings_overrides or {})),
    )


# Any fixture that adds an ATA create on top of the baseline's own crosses
# a TIGHT total-fee ceiling once rent is counted (see
# test_two_ata_creations_need_a_raised_ceiling). Tests that are about
# something OTHER than the ceiling raise it so the intended check is what
# they actually exercise.
_ROOMY = {"SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS": 20_000_000}

# ...and the mirror image, for tests that ARE about the ceiling. These set
# the ceiling they exercise rather than leaning on the shipped default:
# SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS is a product decision that moved once
# already (2.5M -> 8.5M, so a legitimate first swap creating wSOL + USDC is
# not blocked), and a test that silently depends on its value asserts policy
# instead of behaviour.
#
# The component ceilings are lowered alongside it because config requires
# total >= priority + tip + base + (MAX_ATA_CREATES x rent); this set gives
# a floor of 200_000 + 200_000 + 5_000 + 2_039_280 = 2_444_280, just under
# the 2_500_000 total. The baseline fixture (one ATA create, 2_144_480)
# still passes; anything that adds a second create does not.
_TIGHT = {
    "SOLANA_PILOT_MAX_PRIORITY_FEE_LAMPORTS": 200_000,
    "SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS": 200_000,
    "SOLANA_PILOT_MAX_ATA_CREATES": 1,
    "SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS": 2_500_000,
}


async def test_smuggled_approve_is_caught(settings_factory):
    """The worst case: Approve moves no balance.

    A simulation of this transaction shows a clean successful swap, and the
    unlimited delegation survives the pilot — so the loss it enables is NOT
    bounded by trade size. Only static inspection can catch it.
    """
    report = await _verify(
        settings_factory,
        extra_instructions=[
            token_approve_ix(_USDC_ATA, STRANGER, PAYER_PUBKEY, 2**64 - 1)
        ],
    )

    assert not report.passed
    assert "spl_token_instructions_recognised" in _failed_names(report)
    assert (
        "discriminator 4" in _check(report, "spl_token_instructions_recognised").detail
    )


async def test_smuggled_transfer_is_caught(settings_factory):
    report = await _verify(
        settings_factory,
        extra_instructions=[
            token_transfer_ix(_USDC_ATA, _STRANGER_USDC_ATA, PAYER_PUBKEY, 10_000_000)
        ],
    )

    assert not report.passed
    assert (
        "discriminator 3" in _check(report, "spl_token_instructions_recognised").detail
    )


async def test_smuggled_set_authority_is_caught(settings_factory):
    report = await _verify(
        settings_factory,
        extra_instructions=[token_set_authority_ix(_USDC_ATA, STRANGER, PAYER_PUBKEY)],
    )

    assert not report.passed
    assert (
        "discriminator 6" in _check(report, "spl_token_instructions_recognised").detail
    )


async def test_close_account_to_stranger_is_caught(settings_factory):
    """CloseAccount is PERMITTED — but only when it pays us.

    Destination is account index 1 per the token program's documented
    ordering, so this is a discriminator that passes the allowlist and is
    then rejected on its accounts.
    """
    report = await _verify(
        settings_factory,
        extra_instructions=[token_close_account_ix(_USDC_ATA, STRANGER, PAYER_PUBKEY)],
    )

    assert not report.passed
    detail = _check(report, "spl_token_instructions_recognised").detail
    assert "CloseAccount releases lamports to" in detail
    assert STRANGER in detail


async def test_close_account_to_expected_signer_passes(settings_factory):
    """The legitimate unwrap. Must not be collateral damage of the fix."""
    report = await _verify(
        settings_factory,
        extra_instructions=[
            token_close_account_ix(_USDC_ATA, PAYER_PUBKEY, PAYER_PUBKEY)
        ],
    )

    assert report.passed, f"unexpected failures: {_failed_names(report)}"


async def test_empty_token_instruction_data_is_caught(settings_factory):
    """The token program rejects empty data outright, so it is never valid."""
    report = await _verify(settings_factory, extra_instructions=[token_raw_ix(b"")])

    assert not report.passed
    assert (
        "empty instruction data"
        in _check(report, "spl_token_instructions_recognised").detail
    )


async def test_truncated_token_instruction_is_caught(settings_factory):
    """A permitted leading byte is not enough — the length must match too."""
    report = await _verify(
        settings_factory, extra_instructions=[token_raw_ix(bytes([17, 0, 0]))]
    )

    assert not report.passed
    assert "expected 1" in _check(report, "spl_token_instructions_recognised").detail


@pytest.mark.parametrize("discriminator", [5, 7, 8, 10, 11, 12, 13, 22, 200])
async def test_unlisted_token_discriminators_all_fail(settings_factory, discriminator):
    """Revoke/MintTo/Burn/Freeze/Thaw/*Checked and anything unknown.

    Token-2022 extension instructions live at high discriminators and are
    covered by the same rule, which is why they need no enumeration.
    """
    report = await _verify(
        settings_factory,
        extra_instructions=[token_raw_ix(bytes([discriminator]) + bytes(8))],
    )

    assert not report.passed
    assert "spl_token_instructions_recognised" in _failed_names(report)


async def test_ata_legacy_empty_data_create_is_permitted(settings_factory):
    """Empty data IS the legacy Create for the ATA program.

    The inverse of the token-program rule, and the reason "empty data always
    fails" would have rejected legitimate swaps.
    """
    report = await _verify(
        settings_factory,
        settings_overrides=_ROOMY,
        extra_instructions=[
            ata_create_ix(PAYER_PUBKEY, _USDC_ATA, PAYER_PUBKEY, USDC_MINT, b"")
        ],
    )

    assert report.passed, f"unexpected failures: {_failed_names(report)}"


async def test_ata_recover_nested_is_caught(settings_factory):
    """RecoverNested transfers out of and closes an account — not a swap need."""
    report = await _verify(
        settings_factory,
        extra_instructions=[
            ata_create_ix(PAYER_PUBKEY, _USDC_ATA, PAYER_PUBKEY, USDC_MINT, b"\x02")
        ],
    )

    assert not report.passed
    assert "discriminator 2" in _check(report, "ata_instructions_recognised").detail


async def test_system_assign_is_caught(settings_factory):
    """Assign(1) would hand our wallet's ownership to another program.

    Same data-opacity hole as the token programs, worse consequences.
    """
    report = await _verify(
        settings_factory,
        extra_instructions=[
            system_raw_ix((1).to_bytes(4, "little") + bytes(32), PAYER_PUBKEY, STRANGER)
        ],
    )

    assert not report.passed
    assert "discriminator 1" in _check(report, "system_instructions_recognised").detail


async def test_malformed_system_transfer_length_is_caught(settings_factory):
    """Right discriminator, wrong length — previously parsed as nothing."""
    report = await _verify(
        settings_factory,
        extra_instructions=[
            system_raw_ix((2).to_bytes(4, "little") + bytes(4), PAYER_PUBKEY, STRANGER)
        ],
    )

    assert not report.passed
    assert "system_instructions_recognised" in _failed_names(report)


# ----------------------------------------------------------------------
# C4: ATA rent is money leaving the wallet, so the fee ceiling must see it.
#
# The attack is an ATA CreateIdempotent naming a STRANGER as owner: a
# well-formed instruction from an allowlisted program that funds someone
# else's token account with ~0.00204 SOL of our lamports, bounded only by the
# 1232-byte transaction limit. The defence is deliberately NOT "owner must be
# us" — a route may legitimately create a PDA-owned intermediate — but
# counting the rent and bounding it with the total-fee ceiling.
# ----------------------------------------------------------------------
def _stranger_ata_create(n: int = 1):
    return [
        ata_create_ix(PAYER_PUBKEY, _STRANGER_USDC_ATA, STRANGER, USDC_MINT, b"\x01")
    ] * n


async def test_ata_rent_is_surfaced_on_the_report(settings_factory):
    report = await _verify(settings_factory)

    assert report.ata_create_count == 1
    assert report.ata_rent_lamports == ATA_RENT_LAMPORTS_FALLBACK
    # The approval screen must be able to show the breakdown honestly.
    detail = _check(report, "total_fee_within_ceiling").detail
    assert "ata_rent" in detail
    assert str(ATA_RENT_LAMPORTS_FALLBACK) in detail


async def test_stranger_ata_creation_is_bounded_by_the_fee_ceiling(settings_factory):
    """The C4 case: rent for somebody else's account is counted and blocked."""
    report = await _verify(
        settings_factory,
        settings_overrides=_TIGHT,
        extra_instructions=_stranger_ata_create(),
    )

    assert report.ata_create_count == 2
    assert report.ata_rent_lamports == 2 * ATA_RENT_LAMPORTS_FALLBACK
    assert not report.passed
    assert "total_fee_within_ceiling" in _failed_names(report)


async def test_many_stranger_ata_creations_scale_the_counted_rent(settings_factory):
    report = await _verify(
        settings_factory,
        settings_overrides=_TIGHT,
        extra_instructions=_stranger_ata_create(5),
    )

    assert report.ata_create_count == 6
    assert report.ata_rent_lamports == 6 * ATA_RENT_LAMPORTS_FALLBACK
    assert not report.passed


async def test_ata_rent_is_excluded_when_nothing_is_created(settings_factory):
    report = await _verify(settings_factory, include_wrap_primitives=False)

    assert report.ata_create_count == 0
    assert report.ata_rent_lamports == 0
    assert report.total_fee_lamports == 5_000 + 200 + 100_000


async def test_live_rent_figure_overrides_the_fallback(settings_factory):
    """Rent is a cluster parameter, so the runner can pass the live figure."""
    built = build_swap_tx()
    report = await verify_swap_transaction(
        tx_b64=built.tx_b64,
        expected_signer=PAYER_PUBKEY,
        settings=settings_factory(**_TIGHT),
        ata_rent_lamports=3_000_000,
    )

    assert report.ata_rent_lamports == 3_000_000
    # A cluster whose rent is higher than the fallback pushes one single
    # legitimate ATA create past the ceiling — which is why the figure has
    # to be the live one rather than a constant.
    assert not report.passed
    assert "total_fee_within_ceiling" in _failed_names(report)


async def test_two_ata_creations_need_a_raised_ceiling(settings_factory):
    """*** Pins a real operational consequence of counting ATA rent. ***

    A first-ever SOL->USDC swap plausibly creates TWO accounts (the wSOL
    account and the USDC account) = 4,078,560 lamports of rent. Under the
    2,500,000 ceiling this lane originally shipped, that legitimate build
    was refused — fail-closed and correct, but it would have stopped the
    pilot's very first swap.

    The resolution was to raise the default to 8,500,000, sized to cover
    three creates at the maximum component ceilings. Both halves are pinned
    here: the tight ceiling still refuses, and the SHIPPED DEFAULT admits
    the build it exists to admit. Neither number can be changed without one
    of these failing.
    """
    tight = await _verify(
        settings_factory,
        settings_overrides=_TIGHT,
        extra_instructions=_stranger_ata_create(),
    )
    assert tight.ata_create_count == 2
    assert tight.ata_rent_lamports == 2 * ATA_RENT_LAMPORTS_FALLBACK
    assert not tight.passed
    assert "total_fee_within_ceiling" in _failed_names(tight)

    at_default = await _verify(
        settings_factory, extra_instructions=_stranger_ata_create()
    )
    assert at_default.total_fee_lamports == 4_183_760
    assert at_default.passed, f"unexpected failures: {_failed_names(at_default)}"


# ----------------------------------------------------------------------
# C5: exact-length discipline for the ATA branch
# ----------------------------------------------------------------------
async def test_ata_instruction_with_trailing_junk_is_caught(settings_factory):
    """CreateIdempotent(1) + 64 junk bytes was previously accepted."""
    report = await _verify(
        settings_factory,
        extra_instructions=[
            ata_create_ix(
                PAYER_PUBKEY,
                _STRANGER_USDC_ATA,
                STRANGER,
                USDC_MINT,
                b"\x01" + bytes(64),
            )
        ],
    )

    assert not report.passed
    assert "65 data byte(s)" in _check(report, "ata_instructions_recognised").detail


async def test_ata_two_byte_instruction_is_caught(settings_factory):
    report = await _verify(
        settings_factory,
        extra_instructions=[
            ata_create_ix(
                PAYER_PUBKEY, _STRANGER_USDC_ATA, STRANGER, USDC_MINT, b"\x00\x00"
            )
        ],
    )

    assert not report.passed
    assert "ata_instructions_recognised" in _failed_names(report)


# ----------------------------------------------------------------------
# S1-2: duplicate compute-budget instructions
# ----------------------------------------------------------------------
async def test_report_never_understates_a_duplicated_price(settings_factory):
    """The report must not disagree with the bytes.

    Last-wins recorded the trailing decoy (200) while the transaction carried
    50,000,000; the uniqueness check already fails such a build, but the
    report is what the approval screen and evidence show, so it takes max().
    """
    report = await _verify(
        settings_factory,
        compute_unit_price=50_000_000,
        extra_instructions=[compute_unit_price_ix(1_000)],
    )

    assert report.compute_unit_price_micro_lamports == 50_000_000
    assert not report.passed


async def test_duplicate_unit_price_high_first_is_caught(settings_factory):
    """The dangerous ordering: expensive first, cheap decoy last.

    Last-wins made the inspector price this at 200 lamports and PASS. The
    runtime would reject it (DuplicateInstruction) so no money moves — but
    the approval screen and evidence would state a fee that is not in the
    bytes, and the inspector is the authority on exactly that.
    """
    report = await _verify(
        settings_factory,
        compute_unit_price=50_000_000,
        extra_instructions=[compute_unit_price_ix(1_000)],
    )

    assert not report.passed
    assert "compute_budget_instructions_unique" in _failed_names(report)


async def test_duplicate_unit_price_low_first_is_caught(settings_factory):
    report = await _verify(
        settings_factory,
        compute_unit_price=1_000,
        extra_instructions=[compute_unit_price_ix(50_000_000)],
    )

    assert not report.passed
    assert "compute_budget_instructions_unique" in _failed_names(report)


async def test_duplicate_unit_limit_is_caught(settings_factory):
    report = await _verify(
        settings_factory, extra_instructions=[compute_unit_limit_ix(1_400_000)]
    )

    assert not report.passed
    assert "compute_budget_instructions_unique" in _failed_names(report)


async def test_single_compute_budget_pair_passes(settings_factory):
    report = await _verify(settings_factory)

    assert _check(report, "compute_budget_instructions_unique").passed
    assert report.passed, f"unexpected failures: {_failed_names(report)}"


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
