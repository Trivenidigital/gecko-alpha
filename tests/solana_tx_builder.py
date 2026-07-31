"""Real ``VersionedTransaction`` fixtures for the Solana lane tests.

Not mocks. Every fixture is a genuine solders v0 transaction compiled from
genuine instructions, so ``tx_inspector`` is exercised against the same byte
layout Jupiter produces — a hand-rolled dict fixture would pass an inspector
that cannot actually parse a transaction.

The adversarial variants (wrong signer, extra signer, unknown program,
over-ceiling tip, unknown transfer destination) are built by the SAME
function, differing only in the parameter under test.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from scout.live.solana.constants import (
    COMPUTE_BUDGET_PROGRAM_ID,
    COMPUTE_BUDGET_SET_UNIT_LIMIT,
    COMPUTE_BUDGET_SET_UNIT_PRICE,
    JUPITER_V6_PROGRAM_ID,
    SOL_MINT,
    SYSTEM_PROGRAM_ID,
    SYSTEM_TRANSFER_INSTRUCTION,
    TOKEN_PROGRAM_ID,
    USDC_MINT,
)
from scout.live.solana.tx_inspector import derive_associated_token_address

# Deterministic across runs so failures are reproducible.
PAYER = Keypair.from_seed(bytes([11]) * 32)
OTHER = Keypair.from_seed(bytes([22]) * 32)
PAYER_PUBKEY = str(PAYER.pubkey())
OTHER_PUBKEY = str(OTHER.pubkey())

# A real Jito tip account (first entry of the published mainnet list).
TIP_ACCOUNT = "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5"
# Not a tip account, not ours — the exfiltration destination under test.
STRANGER = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"

BLOCKHASH = "EETubP5AKHgjPAhzPAFcb8BAY1hMH639CWCFTqi3hq1k"

_P = Pubkey.from_string

# An address lookup table holding both mints, so a transaction compiled
# against it can only be mint-verified once the table is resolved. That is
# the property the ALT tests exercise.
ALT = AddressLookupTableAccount(
    key=_P("3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT"),
    addresses=[_P(SOL_MINT), _P(USDC_MINT)],
)


def _compute_unit_limit_ix(units: int) -> Instruction:
    data = bytes([COMPUTE_BUDGET_SET_UNIT_LIMIT]) + units.to_bytes(4, "little")
    return Instruction(_P(COMPUTE_BUDGET_PROGRAM_ID), data, [])


def _compute_unit_price_ix(micro_lamports: int) -> Instruction:
    data = bytes([COMPUTE_BUDGET_SET_UNIT_PRICE]) + micro_lamports.to_bytes(8, "little")
    return Instruction(_P(COMPUTE_BUDGET_PROGRAM_ID), data, [])


def _system_transfer_ix(source: str, dest: str, lamports: int) -> Instruction:
    data = SYSTEM_TRANSFER_INSTRUCTION.to_bytes(4, "little") + lamports.to_bytes(
        8, "little"
    )
    return Instruction(
        _P(SYSTEM_PROGRAM_ID),
        data,
        [
            AccountMeta(_P(source), is_signer=True, is_writable=True),
            AccountMeta(_P(dest), is_signer=False, is_writable=True),
        ],
    )


def _swap_ix(
    payer: str, mints: tuple[str, ...], program_id: str, extra_signer: str | None
) -> Instruction:
    """A stand-in for Jupiter's route instruction.

    Its data is opaque to the inspector by design (see tx_inspector's
    "cannot verify statically" section); what matters for the fixture is the
    ACCOUNT list, since that is what the mint-presence and program-allowlist
    checks read.
    """
    accounts = [AccountMeta(_P(payer), is_signer=True, is_writable=True)]
    if extra_signer is not None:
        accounts.append(
            AccountMeta(_P(extra_signer), is_signer=True, is_writable=False)
        )
    accounts.append(
        AccountMeta(_P(TOKEN_PROGRAM_ID), is_signer=False, is_writable=False)
    )
    for mint in mints:
        accounts.append(AccountMeta(_P(mint), is_signer=False, is_writable=False))
    return Instruction(_P(program_id), b"\xe5\x17\xcb\x97\x7a\xe3\xad\x2a", accounts)


@dataclass(frozen=True)
class BuiltTx:
    """A fixture transaction plus the facts a test wants to assert against."""

    tx_b64: str
    transaction: VersionedTransaction
    payer_pubkey: str
    tip_lamports: int
    priority_fee_lamports: int


def build_swap_tx(
    *,
    payer: Keypair = PAYER,
    fee_payer_pubkey: str | None = None,
    tip_destination: str | None = TIP_ACCOUNT,
    tip_lamports: int = 100_000,
    compute_unit_limit: int | None = 200_000,
    compute_unit_price: int | None = 1_000,
    mints: tuple[str, ...] = (SOL_MINT, USDC_MINT),
    swap_program_id: str = JUPITER_V6_PROGRAM_ID,
    extra_signer: str | None = None,
    extra_transfer_dest: str | None = None,
    extra_transfer_lamports: int = 50_000,
    include_wrap_transfer: bool = True,
    blockhash: str = BLOCKHASH,
    sign: bool = False,
    lookup_tables: list | None = None,
) -> BuiltTx:
    """Compile a realistic SOL->USDC swap transaction.

    Every parameter exists so one adversarial property can be varied while the
    rest of the transaction stays legitimate — a fixture that is wrong in
    several ways at once cannot show WHICH check caught it.
    """
    payer_pubkey = fee_payer_pubkey or str(payer.pubkey())
    instructions: list[Instruction] = []

    if compute_unit_limit is not None:
        instructions.append(_compute_unit_limit_ix(compute_unit_limit))
    if compute_unit_price is not None:
        instructions.append(_compute_unit_price_ix(compute_unit_price))

    if include_wrap_transfer:
        wsol_ata = derive_associated_token_address(payer_pubkey, SOL_MINT)
        instructions.append(_system_transfer_ix(payer_pubkey, wsol_ata, 5_000_000))

    if tip_destination is not None and tip_lamports:
        instructions.append(
            _system_transfer_ix(payer_pubkey, tip_destination, tip_lamports)
        )

    if extra_transfer_dest is not None:
        instructions.append(
            _system_transfer_ix(
                payer_pubkey, extra_transfer_dest, extra_transfer_lamports
            )
        )

    instructions.append(_swap_ix(payer_pubkey, mints, swap_program_id, extra_signer))

    message = MessageV0.try_compile(
        _P(payer_pubkey),
        instructions,
        lookup_tables or [],
        Hash.from_string(blockhash),
    )

    if sign:
        transaction = VersionedTransaction(message, [payer])
    else:
        # Jupiter returns the transaction UNSIGNED, with zero-filled
        # signature placeholders — one per required signature.
        placeholders = [Signature.default()] * message.header.num_required_signatures
        transaction = VersionedTransaction.populate(message, placeholders)

    priority_fee = 0
    if compute_unit_price is not None:
        limit = compute_unit_limit if compute_unit_limit is not None else 1_400_000
        priority_fee = -(-compute_unit_price * limit // 1_000_000)

    return BuiltTx(
        tx_b64=base64.b64encode(bytes(transaction)).decode(),
        transaction=transaction,
        payer_pubkey=payer_pubkey,
        tip_lamports=tip_lamports if tip_destination else 0,
        priority_fee_lamports=priority_fee,
    )
