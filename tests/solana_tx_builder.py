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
    ASSOCIATED_TOKEN_PROGRAM_ID,
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
    payer: str,
    mints: tuple[str, ...],
    program_id: str,
    extra_signer: str | None,
    route_amount_lamports: int = 50_000_000,
    route_slippage_bps: int = 100,
) -> Instruction:
    """A stand-in for Jupiter's route instruction.

    Its data is opaque to the INSPECTOR by design (see tx_inspector's
    "cannot verify statically" section); what matters there is the ACCOUNT
    list, since that is what the mint-presence and program-allowlist checks
    read.

    The route amount and slippage ARE encoded into the data, the way Jupiter
    encodes them into its real instruction blob. Nothing parses them back —
    they exist so that changing the amount or the slippage changes the
    transaction MESSAGE and therefore its hash. Without that, a test claiming
    "changing the amount invalidates the authorization" would be asserting
    against a fixture where the amount is not in the message at all.
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
    data = (
        b"\xe5\x17\xcb\x97\x7a\xe3\xad\x2a"
        + route_amount_lamports.to_bytes(8, "little")
        + route_slippage_bps.to_bytes(2, "little")
    )
    return Instruction(_P(program_id), data, accounts)


# ----------------------------------------------------------------------
# SPL Token / ATA instruction builders.
#
# Discriminators per tx_inspector's verified-facts table (fact 5/6). These
# build the ADVERSARIAL fixtures: each is a perfectly well-formed instruction
# from an allowlisted program, which is exactly why program-level
# allowlisting alone let them through.
# ----------------------------------------------------------------------
def token_approve_ix(
    source_ata: str, delegate: str, owner: str, amount: int
) -> Instruction:
    """Approve(4) — grants `delegate` authority over `source_ata`.

    The nastiest of the four: it moves no balance, so simulation shows a
    clean swap, and the delegation outlives the pilot.
    """
    data = bytes([4]) + amount.to_bytes(8, "little")
    return Instruction(
        _P(TOKEN_PROGRAM_ID),
        data,
        [
            AccountMeta(_P(source_ata), is_signer=False, is_writable=True),
            AccountMeta(_P(delegate), is_signer=False, is_writable=False),
            AccountMeta(_P(owner), is_signer=True, is_writable=False),
        ],
    )


def token_transfer_ix(
    source_ata: str, dest_ata: str, owner: str, amount: int
) -> Instruction:
    """Transfer(3) — moves SPL tokens straight out."""
    data = bytes([3]) + amount.to_bytes(8, "little")
    return Instruction(
        _P(TOKEN_PROGRAM_ID),
        data,
        [
            AccountMeta(_P(source_ata), is_signer=False, is_writable=True),
            AccountMeta(_P(dest_ata), is_signer=False, is_writable=True),
            AccountMeta(_P(owner), is_signer=True, is_writable=False),
        ],
    )


def token_close_account_ix(account: str, destination: str, owner: str) -> Instruction:
    """CloseAccount(9) — releases rent lamports to `destination` (index 1)."""
    return Instruction(
        _P(TOKEN_PROGRAM_ID),
        bytes([9]),
        [
            AccountMeta(_P(account), is_signer=False, is_writable=True),
            AccountMeta(_P(destination), is_signer=False, is_writable=True),
            AccountMeta(_P(owner), is_signer=True, is_writable=False),
        ],
    )


def token_set_authority_ix(account: str, new_authority: str, owner: str) -> Instruction:
    """SetAuthority(6) — hands the account to a new authority."""
    data = bytes([6, 2, 1]) + bytes(_P(new_authority))
    return Instruction(
        _P(TOKEN_PROGRAM_ID),
        data,
        [
            AccountMeta(_P(account), is_signer=False, is_writable=True),
            AccountMeta(_P(owner), is_signer=True, is_writable=False),
        ],
    )


def token_sync_native_ix(wsol_ata: str) -> Instruction:
    """SyncNative(17) — legitimate, part of every wrap."""
    return Instruction(
        _P(TOKEN_PROGRAM_ID),
        bytes([17]),
        [AccountMeta(_P(wsol_ata), is_signer=False, is_writable=True)],
    )


def token_raw_ix(data: bytes, accounts: list[str] | None = None) -> Instruction:
    """Arbitrary token-program data — for empty/truncated-data cases."""
    metas = [
        AccountMeta(_P(a), is_signer=False, is_writable=True) for a in (accounts or [])
    ]
    return Instruction(_P(TOKEN_PROGRAM_ID), data, metas)


def ata_create_ix(
    payer: str, ata: str, owner: str, mint: str, data: bytes
) -> Instruction:
    """ATA creation. `data` empty = legacy Create, b"\\x01" = CreateIdempotent."""
    return Instruction(
        _P(ASSOCIATED_TOKEN_PROGRAM_ID),
        data,
        [
            AccountMeta(_P(payer), is_signer=True, is_writable=True),
            AccountMeta(_P(ata), is_signer=False, is_writable=True),
            AccountMeta(_P(owner), is_signer=False, is_writable=False),
            AccountMeta(_P(mint), is_signer=False, is_writable=False),
        ],
    )


def compute_unit_price_ix(micro_lamports: int) -> Instruction:
    """Public alias — duplicate-instruction fixtures need to append one."""
    return _compute_unit_price_ix(micro_lamports)


def compute_unit_limit_ix(units: int) -> Instruction:
    """Public alias — see ``compute_unit_price_ix``."""
    return _compute_unit_limit_ix(units)


def system_raw_ix(data: bytes, source: str, dest: str) -> Instruction:
    """Arbitrary system-program data — for Assign / malformed-transfer cases."""
    return Instruction(
        _P(SYSTEM_PROGRAM_ID),
        data,
        [
            AccountMeta(_P(source), is_signer=True, is_writable=True),
            AccountMeta(_P(dest), is_signer=False, is_writable=True),
        ],
    )


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
    include_wrap_primitives: bool = True,
    blockhash: str = BLOCKHASH,
    sign: bool = False,
    lookup_tables: list | None = None,
    extra_instructions: list[Instruction] | None = None,
    route_amount_lamports: int = 50_000_000,
    route_slippage_bps: int = 100,
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

    wsol_ata = derive_associated_token_address(payer_pubkey, SOL_MINT)
    # The ATA created is the one for the swap's OUTPUT mint — derived from
    # `mints` rather than hardcoded, so a fixture that routes to a decoy mint
    # does not accidentally reintroduce the real USDC mint into the account
    # keys and mask the mint-presence check.
    output_mint = mints[-1] if mints else USDC_MINT
    output_ata = derive_associated_token_address(payer_pubkey, output_mint)

    if include_wrap_primitives:
        # The legitimate token/ATA primitives a real Jupiter SOL->USDC swap
        # emits at top level. Present in the baseline so the discriminator
        # allowlists are proven to PERMIT a real swap, not merely to reject
        # attacks.
        instructions.append(
            ata_create_ix(payer_pubkey, output_ata, payer_pubkey, output_mint, b"\x01")
        )

    if include_wrap_transfer:
        instructions.append(_system_transfer_ix(payer_pubkey, wsol_ata, 5_000_000))

    if include_wrap_primitives:
        instructions.append(token_sync_native_ix(wsol_ata))

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

    instructions.append(
        _swap_ix(
            payer_pubkey,
            mints,
            swap_program_id,
            extra_signer,
            route_amount_lamports=route_amount_lamports,
            route_slippage_bps=route_slippage_bps,
        )
    )

    if include_wrap_primitives:
        # Unwrap: closing the WSOL account releases its lamports to US.
        instructions.append(
            token_close_account_ix(wsol_ata, payer_pubkey, payer_pubkey)
        )

    if extra_instructions:
        instructions.extend(extra_instructions)

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
