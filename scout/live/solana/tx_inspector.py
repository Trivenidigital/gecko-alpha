"""Content verification for the transaction Jupiter built (PR-S1).

Jupiter builds the transaction; we sign it. Everything between those two
facts is this module. A quote request is not a promise about what comes back:
the returned bytes could tip an account that is not Jito's, could carry a
priority fee an order of magnitude over the ceiling, could require a second
signer, or could touch a program we have never heard of. None of that is
detectable from the request we sent — only from the transaction's own bytes.

**Unknown is a FAILURE.** Every check names what it proved; anything the
inspector cannot parse, cannot resolve, or does not recognise produces a
failed check rather than a skipped one. That is the whole design: an
inspector that passes what it does not understand provides no protection
precisely when it matters.

Verified facts (2026-07-31)
---------------------------
1. **Signing payload.** For a v0 transaction the signed bytes are the
   VERSION-PREFIXED message: ``0x80 || bytes(message)``, i.e.
   ``solders.message.to_bytes_versioned(msg)``. Verified empirically against
   solders 0.28.0: ``Keypair.sign_message(to_bytes_versioned(msg))`` equals
   the signature ``VersionedTransaction(msg, [kp])`` produces, while signing
   the UNPREFIXED ``bytes(msg)`` produces a different, invalid signature.
   ``message_sha256`` below therefore hashes the version-prefixed form — the
   approval screen shows a digest of exactly what the key will sign.

2. **Address-lookup-table index resolution.** Verified empirically against
   solders 0.28.0 by compiling a v0 message with a populated ALT: the
   resolved account-key list is

       static account_keys
       ++ [table[i] for each lookup in order, for i in writable_indexes]
       ++ [table[i] for each lookup in order, for i in readonly_indexes]

   A message with 2 static keys, lookup writable_indexes ``[0, 2]`` and
   readonly_indexes ``[1]`` produced instruction account indexes 2, 3 and 4
   mapping to table entries 0, 2 and 1 respectively.

3. **Compute-budget encoding.** ``SetComputeUnitLimit`` is discriminator 2
   followed by a u32 LE unit count (5 bytes total); ``SetComputeUnitPrice``
   is discriminator 3 followed by a u64 LE micro-lamports-per-compute-unit
   (9 bytes). Priority fee in lamports is
   ``ceil(price_micro_lamports * unit_limit / 1e6)``.

4. **System-program transfer encoding.** Discriminator is a u32 LE ``2``
   followed by a u64 LE lamport amount (12 bytes), with accounts
   ``[from, to]``. This is the shape a Jito tip arrives as.

5. **SPL Token instruction discriminators.** Verified against the token
   program's own ``TokenInstruction::unpack`` match arms
   (https://raw.githubusercontent.com/solana-program/token/main/interface/src/instruction.rs),
   cross-checked against the enum declaration order at
   https://docs.rs/spl-token/latest/spl_token/instruction/enum.TokenInstruction.html.
   The leading data byte is the tag::

       0 InitializeMint       9 CloseAccount       18 InitializeAccount3
       1 InitializeAccount   10 FreezeAccount      19 InitializeMultisig2
       2 InitializeMultisig  11 ThawAccount        20 InitializeMint2
       3 Transfer            12 TransferChecked    21 GetAccountDataSize
       4 Approve             13 ApproveChecked     22 InitializeImmutableOwner
       5 Revoke              14 MintToChecked      23 AmountToUiAmount
       6 SetAuthority        15 BurnChecked        24 UiAmountToAmount
       7 MintTo              16 InitializeAccount2
       8 Burn                17 SyncNative

   EMPTY data is an error, not a default: ``unpack`` opens with
   ``let (&tag, rest) = input.split_first().ok_or(InvalidInstruction)?;``, so
   a zero-length token instruction can never be valid.

   Account order, same source: ``CloseAccount`` is ``[account_to_close,
   destination, owner]`` — released lamports go to index **1**. ``Approve``
   is ``[source, delegate, owner]``.

   Token-2022 shares this base layout and places its extension instructions
   at higher discriminators, so they fail closed here without being
   enumerated.

6. **Associated-token-account discriminators.** ``Create = 0``,
   ``CreateIdempotent = 1``, ``RecoverNested = 2``. *** Unlike the token
   program, EMPTY data is LEGITIMATE here. *** The processor reads
   ``let instruction = if input.is_empty() {
   AssociatedTokenAccountInstruction::Create } else {
   ...try_from_slice(input)... };``
   (https://raw.githubusercontent.com/solana-program/associated-token-account/main/program/src/processor.rs),
   for backwards compatibility with legacy zero-data Create calls. A blanket
   "empty data fails" rule would reject legitimate swaps.

7. **Why allowlisting the PROGRAM is not enough.** An instruction's meaning
   lives in its DATA. Before the discriminator allowlists below existed this
   module decoded data only for ComputeBudget and System-transfer, so every
   other instruction from an allowlisted program contributed nothing but its
   program id. Four real compiled transactions, each carrying a legitimate
   swap PLUS one extra top-level SPL instruction — ``Approve(delegate=
   stranger, u64::MAX)``, ``Transfer(->stranger)``,
   ``CloseAccount(->stranger)``, ``SetAuthority(->stranger)`` — all returned
   ``passed=True`` with zero failed checks, directly contradicting this
   module's stated invariant that unknown is a failure.

   ``Approve`` is the worst case and the reason simulation is not a
   substitute for inspection: it moves no balance, so simulating the
   transaction shows a clean successful swap, while the unlimited delegation
   it grants outlives the pilot entirely. Loss would NOT be bounded by trade
   size. Hence the permitted sets are deliberately minimal.

What this module CANNOT verify statically
------------------------------------------
These are real gaps, not oversights. They are why independent simulation
(``rpc_client.simulate_transaction``) is a required second gate and not a
nicety, and why the on-chain minimum-output bound matters:

- **Inner instructions (CPIs).** Only TOP-LEVEL instructions appear in the
  message. The Jupiter program CPIs into AMM programs, and those callees are
  invisible here. A transaction whose top-level program list is entirely
  allowlisted can still do arbitrary things inside the aggregator program.
  Simulation is what exercises them.
- **Jupiter instruction semantics.** The aggregator's instruction data is an
  opaque blob to this module. We do NOT parse the encoded route or the
  encoded minimum-output. The slippage guarantee rests on Jupiter's on-chain
  program enforcing the ``otherAmountThreshold`` compiled into it, plus the
  post-trade balance reconciliation — not on anything checked here.
- **ALT mutability race.** A lookup table can be EXTENDED between our
  ``getMultipleAccounts`` read and the transaction executing. An index that
  resolves to a known address now could resolve to a newly-appended one at
  execution. The window is small and the tip/priority/signer checks do not
  depend on ALT contents, but the program-allowlist and mint-presence checks
  do. Treat ALT-heavy routes as lower-assurance.
- **Account ownership and state.** Whether a destination ATA is really ours,
  whether a mint is really USDC's mint account rather than a lookalike with
  the same base58 prefix — resolved by exact address comparison here, but not
  by reading on-chain account state.
"""

from __future__ import annotations

import base64
import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, Collection, Final, Iterable

import structlog
from solders.message import to_bytes_versioned
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from scout.config import Settings
from scout.live.solana.constants import (
    ALLOWED_PROGRAM_IDS,
    ASSOCIATED_TOKEN_PROGRAM_ID,
    COMPUTE_BUDGET_PROGRAM_ID,
    COMPUTE_BUDGET_SET_UNIT_LIMIT,
    COMPUTE_BUDGET_SET_UNIT_PRICE,
    JITO_TIP_ACCOUNTS_FALLBACK,
    LAMPORTS_PER_SIGNATURE,
    SOL_MINT,
    SYSTEM_PROGRAM_ID,
    SYSTEM_TRANSFER_INSTRUCTION,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    USDC_MINT,
)
from scout.live.solana.exceptions import SolanaVerificationError

log = structlog.get_logger(__name__)

# Protocol ceiling on a transaction's compute-unit limit. Used as the
# conservative stand-in when a priority PRICE is set without an explicit
# LIMIT: over-estimating the fee fails closed, under-estimating would let a
# transaction past the ceiling it actually breaches.
MAX_COMPUTE_UNIT_LIMIT = 1_400_000

# A blockhash of all zeros is solders' default — it means "unset", not "an
# unusual blockhash", and such a transaction can never land.
_DEFAULT_BLOCKHASH = "11111111111111111111111111111111"

# ----------------------------------------------------------------------
# Per-instruction discriminator allowlists (module docstring facts 5-7).
#
# Allowlisting a PROGRAM is not enough. An instruction's meaning lives in its
# data, so "the SPL Token program" covers SyncNative (harmless) and Approve
# (hands a stranger unlimited delegated authority over our token account)
# alike. These sets are what make the program allowlist mean something.
#
# Deliberately minimal: exactly what a Jupiter SOL->USDC swap needs. A
# legitimate build that uses anything else FAILS CLOSED and names the
# refused discriminator, which is a blocked pilot the operator can reason
# about — the opposite failure is a signed transaction nobody inspected.
# ----------------------------------------------------------------------

# SPL Token / Token-2022. Both share the base instruction layout; Token-2022's
# extension instructions occupy higher discriminators and therefore fail
# closed here automatically, which is the desired behaviour.
TOKEN_IX_CLOSE_ACCOUNT: Final = 9  # unwrap WSOL back to SOL
TOKEN_IX_SYNC_NATIVE: Final = 17  # after depositing SOL into the WSOL account

# Value is the EXACT expected data length, so a truncated instruction whose
# leading byte happens to be permitted still fails. Both are single-byte.
PERMITTED_TOKEN_INSTRUCTIONS: Final[dict[int, int]] = {
    TOKEN_IX_CLOSE_ACCOUNT: 1,
    TOKEN_IX_SYNC_NATIVE: 1,
}

# CloseAccount account order is [account_to_close, destination, owner], so the
# lamports released by closing the WSOL account go to index 1. That index must
# be us — otherwise closing our own wrapped-SOL account pays a stranger.
TOKEN_CLOSE_ACCOUNT_DESTINATION_INDEX: Final = 1

# Associated Token Account program. NOTE the empty-data case: the ATA program
# treats zero-length data as the legacy Create, so unlike the token programs
# an empty ATA instruction is legitimate and must be permitted.
ATA_IX_CREATE: Final = 0
ATA_IX_CREATE_IDEMPOTENT: Final = 1
PERMITTED_ATA_INSTRUCTIONS: Final[frozenset[int]] = frozenset(
    {ATA_IX_CREATE, ATA_IX_CREATE_IDEMPOTENT}
)
# RecoverNested (2) is NOT permitted: it transfers out of and closes a nested
# account, which is a fund movement a swap has no reason to make.

# System program. Only Transfer, at its exact 12-byte length. The same
# data-opacity argument as the token programs applies here and the
# consequences are worse — Assign(1) would hand our wallet's ownership to
# another program, and TransferWithSeed(11) moves lamports by a path the
# transfer-destination check never sees.
SYSTEM_IX_TRANSFER: Final = 2
PERMITTED_SYSTEM_INSTRUCTIONS: Final[dict[int, int]] = {SYSTEM_IX_TRANSFER: 12}


@dataclass(frozen=True)
class Check:
    """One named verification step and its outcome."""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class VerificationReport:
    """Structured result of inspecting a built swap transaction.

    ``passed`` is the AND of every check. Callers must not sign unless it is
    True; ``raise_if_failed`` is the enforcement helper.
    """

    passed: bool
    checks: tuple[Check, ...]
    message_sha256: str
    fee_payer: str | None
    num_required_signatures: int | None
    recent_blockhash: str | None
    jito_tip_lamports: int
    jito_tip_destination: str | None
    priority_fee_lamports: int
    compute_unit_limit: int | None
    compute_unit_price_micro_lamports: int | None
    total_fee_lamports: int
    program_ids: tuple[str, ...] = ()
    unresolved_lookup_tables: tuple[str, ...] = ()
    resolved_key_count: int | None = None
    raw_tx_b64: str = field(default="", repr=False)

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if not c.passed)

    def raise_if_failed(self) -> None:
        """Raise ``SolanaVerificationError`` unless every check passed."""
        if self.passed:
            return
        summary = "; ".join(f"{c.name}: {c.detail}" for c in self.failures)
        raise SolanaVerificationError(
            f"transaction verification failed ({len(self.failures)} of "
            f"{len(self.checks)} checks): {summary}"
        )


def derive_associated_token_address(
    owner: str, mint: str, *, token_program: str = TOKEN_PROGRAM_ID
) -> str:
    """Canonical associated-token-account address for ``owner``/``mint``."""
    address, _bump = Pubkey.find_program_address(
        [
            bytes(Pubkey.from_string(owner)),
            bytes(Pubkey.from_string(token_program)),
            bytes(Pubkey.from_string(mint)),
        ],
        Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM_ID),
    )
    return str(address)


def _own_accounts(owner: str, mints: Iterable[str]) -> set[str]:
    """Every address that is unambiguously the signer's own.

    The signer's wallet plus its associated token accounts for the mints in
    play, under both token programs. A SOL transfer into one of these is the
    wrap step, not an exfiltration.
    """
    accounts = {owner}
    for mint in mints:
        for program in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
            accounts.add(
                derive_associated_token_address(owner, mint, token_program=program)
            )
    return accounts


def _resolve_account_keys(
    message: Any, lookup_tables: dict[str, list[str]]
) -> tuple[list[str | None], list[str]]:
    """Build the full resolved key list. See module docstring fact 2.

    Returns ``(keys, unresolved_table_keys)``. An index that cannot be
    resolved is ``None`` rather than omitted, so instruction account indexes
    keep pointing at the right slot and an unresolvable one surfaces as
    ``None`` at exactly the position that matters.
    """
    keys: list[str | None] = [str(k) for k in message.account_keys]
    lookups = list(getattr(message, "address_table_lookups", []) or [])
    if not lookups:
        return keys, []

    unresolved: list[str] = []
    writable: list[str | None] = []
    readonly: list[str | None] = []
    for lookup in lookups:
        table_key = str(lookup.account_key)
        table = lookup_tables.get(table_key)
        if table is None:
            unresolved.append(table_key)
        for index in lookup.writable_indexes:
            writable.append(
                table[index] if table is not None and index < len(table) else None
            )
        for index in lookup.readonly_indexes:
            readonly.append(
                table[index] if table is not None and index < len(table) else None
            )
    return keys + writable + readonly, unresolved


def _parse_compute_budget(program_id: str, data: bytes) -> tuple[str, int] | None:
    """Decode a compute-budget instruction. See module docstring fact 3."""
    if program_id != COMPUTE_BUDGET_PROGRAM_ID or not data:
        return None
    if data[0] == COMPUTE_BUDGET_SET_UNIT_LIMIT and len(data) >= 5:
        return "limit", int.from_bytes(data[1:5], "little")
    if data[0] == COMPUTE_BUDGET_SET_UNIT_PRICE and len(data) >= 9:
        return "price", int.from_bytes(data[1:9], "little")
    return None


def _parse_system_transfer(program_id: str, data: bytes) -> int | None:
    """Lamports moved by a system-program transfer, else None. Fact 4."""
    if program_id != SYSTEM_PROGRAM_ID or len(data) != 12:
        return None
    if int.from_bytes(data[:4], "little") != SYSTEM_TRANSFER_INSTRUCTION:
        return None
    return int.from_bytes(data[4:12], "little")


async def verify_swap_transaction(
    *,
    tx_b64: str,
    expected_signer: str,
    settings: Settings,
    rpc_client: Any | None = None,
    tip_accounts: Collection[str] | None = None,
    input_mint: str = SOL_MINT,
    output_mint: str = USDC_MINT,
) -> VerificationReport:
    """Verify a Jupiter-built transaction against intent. Never raises on a
    failed CHECK — it returns a report whose ``passed`` is False so the
    approval screen and the evidence record can show every outcome. Call
    ``report.raise_if_failed()`` to enforce.

    Args:
        expected_signer: base58 pubkey that must be the sole required signer.
        rpc_client: read-only client used to resolve address lookup tables.
            When None and the transaction uses ALTs, the affected checks FAIL
            rather than being skipped.
        tip_accounts: allowed Jito tip destinations. Defaults to the static
            fallback list; the caller should pass the live
            ``jito_client.fetch_tip_accounts`` result when available.
    """
    checks: list[Check] = []
    allowed_tips = (
        set(tip_accounts) if tip_accounts else set(JITO_TIP_ACCOUNTS_FALLBACK)
    )

    def add(name: str, passed: bool, detail: str) -> bool:
        checks.append(Check(name=name, passed=passed, detail=detail))
        return passed

    def bail(
        message_sha256: str = "",
        **kw: Any,
    ) -> VerificationReport:
        return VerificationReport(
            passed=False,
            checks=tuple(checks),
            message_sha256=message_sha256,
            fee_payer=kw.get("fee_payer"),
            num_required_signatures=kw.get("num_required_signatures"),
            recent_blockhash=kw.get("recent_blockhash"),
            jito_tip_lamports=kw.get("jito_tip_lamports", 0),
            jito_tip_destination=kw.get("jito_tip_destination"),
            priority_fee_lamports=kw.get("priority_fee_lamports", 0),
            compute_unit_limit=kw.get("compute_unit_limit"),
            compute_unit_price_micro_lamports=kw.get(
                "compute_unit_price_micro_lamports"
            ),
            total_fee_lamports=kw.get("total_fee_lamports", 0),
            program_ids=tuple(kw.get("program_ids", ())),
            unresolved_lookup_tables=tuple(kw.get("unresolved_lookup_tables", ())),
            resolved_key_count=kw.get("resolved_key_count"),
            raw_tx_b64=tx_b64,
        )

    # ---- deserialize -------------------------------------------------
    try:
        raw = base64.b64decode(tx_b64, validate=True)
        tx = VersionedTransaction.from_bytes(raw)
    except Exception as exc:
        add("deserialize", False, f"{type(exc).__name__}: {exc}")
        return bail()
    add("deserialize", True, f"{len(raw)} bytes, version={tx.version()}")

    message = tx.message
    message_bytes = to_bytes_versioned(message)
    message_sha256 = hashlib.sha256(message_bytes).hexdigest()

    # ---- signature slots ---------------------------------------------
    # Jupiter returns an UNSIGNED transaction with zero-filled signature
    # placeholders. A non-zero signature already present is somebody else's
    # and must never be co-signed.
    default_sig = bytes(64)
    prefilled = [i for i, sig in enumerate(tx.signatures) if bytes(sig) != default_sig]
    add(
        "arrives_unsigned",
        not prefilled,
        (
            "no pre-filled signatures"
            if not prefilled
            else f"transaction already carries signatures at index {prefilled}"
        ),
    )

    # ---- signer ------------------------------------------------------
    header = message.header
    num_required = int(header.num_required_signatures)
    static_keys = [str(k) for k in message.account_keys]
    fee_payer = static_keys[0] if static_keys else None

    add(
        "single_required_signer",
        num_required == 1,
        f"num_required_signatures={num_required}"
        + ("" if num_required == 1 else " (expected exactly 1)"),
    )
    add(
        "signers_are_static_keys",
        num_required <= len(static_keys),
        f"{num_required} required signature(s), {len(static_keys)} static key(s)",
    )
    add(
        "fee_payer_is_expected_signer",
        fee_payer == expected_signer,
        (
            f"fee payer {fee_payer} == expected {expected_signer}"
            if fee_payer == expected_signer
            else f"fee payer {fee_payer} != expected signer {expected_signer}"
        ),
    )
    # Every required-signature slot must be us, not just slot 0.
    extra_signers = [k for k in static_keys[1:num_required] if k != expected_signer]
    add(
        "no_additional_signers",
        not extra_signers,
        (
            "no other required signers"
            if not extra_signers
            else f"additional required signer(s): {extra_signers}"
        ),
    )

    # ---- blockhash ---------------------------------------------------
    recent_blockhash = str(message.recent_blockhash)
    add(
        "recent_blockhash_present",
        recent_blockhash != _DEFAULT_BLOCKHASH,
        (
            f"blockhash={recent_blockhash}"
            if recent_blockhash != _DEFAULT_BLOCKHASH
            else "blockhash is the all-zero default (unset)"
        ),
    )

    # ---- address lookup tables ---------------------------------------
    lookups = list(getattr(message, "address_table_lookups", []) or [])
    lookup_tables: dict[str, list[str]] = {}
    if lookups:
        table_keys = [str(l.account_key) for l in lookups]
        if rpc_client is None:
            add(
                "lookup_tables_resolved",
                False,
                f"transaction uses {len(table_keys)} lookup table(s) but no "
                "rpc_client was supplied to resolve them",
            )
        else:
            try:
                lookup_tables = await rpc_client.fetch_address_lookup_tables(table_keys)
            except Exception as exc:
                add(
                    "lookup_tables_resolved",
                    False,
                    f"lookup-table fetch failed: {type(exc).__name__}: {exc}",
                )
            else:
                missing = [k for k in table_keys if k not in lookup_tables]
                add(
                    "lookup_tables_resolved",
                    not missing,
                    f"resolved {len(lookup_tables)}/{len(table_keys)} table(s)"
                    + ("" if not missing else f"; unresolved: {missing}"),
                )
    else:
        add("lookup_tables_resolved", True, "transaction uses no lookup tables")

    keys, unresolved_tables = _resolve_account_keys(message, lookup_tables)

    # ---- instructions ------------------------------------------------
    program_ids: list[str] = []
    unknown_programs: list[str] = []
    unresolvable_programs: list[int] = []
    transfers: list[tuple[str | None, int]] = []
    unit_limit: int | None = None
    unit_price: int | None = None
    unit_limit_count = 0
    unit_price_count = 0
    token_violations: list[str] = []
    ata_violations: list[str] = []
    system_violations: list[str] = []

    def _account_at(ix_accounts: list[int], position: int) -> str | None:
        if len(ix_accounts) <= position:
            return None
        key_index = int(ix_accounts[position])
        return keys[key_index] if 0 <= key_index < len(keys) else None

    for ix in message.instructions:
        idx = int(ix.program_id_index)
        program_id = keys[idx] if 0 <= idx < len(keys) else None
        if program_id is None:
            unresolvable_programs.append(idx)
            continue
        program_ids.append(program_id)
        if program_id not in ALLOWED_PROGRAM_IDS:
            unknown_programs.append(program_id)

        data = bytes(ix.data)
        accounts = [int(a) for a in ix.accounts]

        parsed = _parse_compute_budget(program_id, data)
        if parsed is not None:
            kind, value = parsed
            if kind == "limit":
                unit_limit = value
                unit_limit_count += 1
            else:
                unit_price = value
                unit_price_count += 1

        # ---- SPL Token / Token-2022 --------------------------------
        if program_id in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
            if not data:
                # The token program rejects empty data outright, so this is
                # malformed rather than a legacy form (unlike the ATA program).
                token_violations.append("empty instruction data")
            elif data[0] not in PERMITTED_TOKEN_INSTRUCTIONS:
                token_violations.append(f"discriminator {data[0]} not permitted")
            elif len(data) != PERMITTED_TOKEN_INSTRUCTIONS[data[0]]:
                token_violations.append(
                    f"discriminator {data[0]} has {len(data)} data byte(s), "
                    f"expected {PERMITTED_TOKEN_INSTRUCTIONS[data[0]]}"
                )
            elif data[0] == TOKEN_IX_CLOSE_ACCOUNT:
                destination = _account_at(
                    accounts, TOKEN_CLOSE_ACCOUNT_DESTINATION_INDEX
                )
                if destination != expected_signer:
                    token_violations.append(
                        f"CloseAccount releases lamports to {destination}, "
                        f"not to {expected_signer}"
                    )

        # ---- Associated Token Account ------------------------------
        elif program_id == ASSOCIATED_TOKEN_PROGRAM_ID:
            # Empty data IS the legacy Create — permitted deliberately.
            if data and data[0] not in PERMITTED_ATA_INSTRUCTIONS:
                ata_violations.append(f"discriminator {data[0]} not permitted")

        # ---- System ------------------------------------------------
        elif program_id == SYSTEM_PROGRAM_ID:
            if len(data) < 4:
                system_violations.append(
                    f"instruction data too short ({len(data)} bytes)"
                )
            else:
                discriminator = int.from_bytes(data[:4], "little")
                expected_len = PERMITTED_SYSTEM_INSTRUCTIONS.get(discriminator)
                if expected_len is None:
                    system_violations.append(
                        f"discriminator {discriminator} not permitted"
                    )
                elif len(data) != expected_len:
                    system_violations.append(
                        f"discriminator {discriminator} has {len(data)} data "
                        f"byte(s), expected {expected_len}"
                    )

        lamports = _parse_system_transfer(program_id, data)
        if lamports is not None:
            transfers.append((_account_at(accounts, 1), lamports))

    add(
        "spl_token_instructions_recognised",
        not token_violations,
        (
            "every SPL Token instruction is a permitted swap primitive"
            if not token_violations
            else f"refused token instruction(s): {token_violations}"
        ),
    )
    add(
        "ata_instructions_recognised",
        not ata_violations,
        (
            "every associated-token-account instruction is a permitted creation"
            if not ata_violations
            else f"refused ATA instruction(s): {ata_violations}"
        ),
    )
    add(
        "system_instructions_recognised",
        not system_violations,
        (
            "every System instruction is a well-formed transfer"
            if not system_violations
            else f"refused system instruction(s): {system_violations}"
        ),
    )
    add(
        "compute_budget_instructions_unique",
        unit_limit_count <= 1 and unit_price_count <= 1,
        (
            f"{unit_limit_count} unit-limit + {unit_price_count} unit-price "
            "instruction(s)"
        )
        + (
            ""
            if unit_limit_count <= 1 and unit_price_count <= 1
            else " — duplicates make the priced fee depend on which one the "
            "inspector read; the runtime rejects the transaction with "
            "DuplicateInstruction, so no build like this is legitimate"
        ),
    )

    add(
        "program_ids_resolvable",
        not unresolvable_programs,
        (
            "every instruction's program id resolved"
            if not unresolvable_programs
            else f"unresolvable program id at account index {unresolvable_programs}"
        ),
    )
    add(
        "program_ids_allowlisted",
        not unknown_programs,
        (
            f"{len(program_ids)} instruction(s), all allowlisted"
            if not unknown_programs
            else f"unrecognised program id(s): {sorted(set(unknown_programs))}"
        ),
    )

    # ---- mints -------------------------------------------------------
    resolved_keys = {k for k in keys if k is not None}
    missing_mints = [m for m in (input_mint, output_mint) if m not in resolved_keys]
    add(
        "expected_mints_present",
        not missing_mints,
        (
            f"both {input_mint} and {output_mint} appear in the account keys"
            if not missing_mints
            else f"expected mint(s) absent from account keys: {missing_mints}"
        ),
    )

    # ---- transfer destinations + tip ---------------------------------
    try:
        own = _own_accounts(expected_signer, (input_mint, output_mint))
    except Exception as exc:
        # A malformed pubkey or mint makes the destination allowlist
        # underivable. Recorded as a failed check rather than raised: the
        # caller gets a report naming the problem, and — because the
        # allowlist is now empty — every transfer is treated as unknown
        # instead of being waved through.
        add(
            "own_accounts_derivable",
            False,
            f"could not derive own accounts for {expected_signer}: "
            f"{type(exc).__name__}: {exc}",
        )
        own = set()
    known_destinations = own | allowed_tips
    unknown_transfers = [
        (dest, lamports)
        for dest, lamports in transfers
        if dest is None or dest not in known_destinations
    ]
    add(
        "transfer_destinations_known",
        not unknown_transfers,
        (
            f"{len(transfers)} system transfer(s), all to own accounts or Jito tip accounts"
            if not unknown_transfers
            else f"transfer(s) to unknown destination(s): {unknown_transfers}"
        ),
    )

    tip_transfers = [
        (dest, lamports) for dest, lamports in transfers if dest in allowed_tips
    ]
    jito_tip_lamports = sum(lamports for _dest, lamports in tip_transfers)
    jito_tip_destination = tip_transfers[0][0] if tip_transfers else None
    tip_ceiling = int(settings.SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS)
    add(
        "jito_tip_within_ceiling",
        jito_tip_lamports <= tip_ceiling,
        f"tip={jito_tip_lamports} lamports to {jito_tip_destination} "
        f"(ceiling {tip_ceiling})",
    )

    # ---- priority fee ------------------------------------------------
    if unit_price is None:
        priority_fee = 0
        effective_limit = unit_limit
    else:
        # No explicit limit => the protocol maximum, the conservative read.
        effective_limit = (
            unit_limit if unit_limit is not None else MAX_COMPUTE_UNIT_LIMIT
        )
        priority_fee = math.ceil(unit_price * effective_limit / 1_000_000)

    priority_ceiling = int(settings.SOLANA_PILOT_MAX_PRIORITY_FEE_LAMPORTS)
    add(
        "priority_fee_within_ceiling",
        priority_fee <= priority_ceiling,
        f"priority_fee={priority_fee} lamports "
        f"(price={unit_price} micro-lamports/CU, limit={effective_limit}, "
        f"ceiling {priority_ceiling})",
    )

    base_fee = LAMPORTS_PER_SIGNATURE * max(num_required, 1)
    total_fee = base_fee + priority_fee + jito_tip_lamports
    total_ceiling = int(settings.SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS)
    add(
        "total_fee_within_ceiling",
        total_fee <= total_ceiling,
        f"total={total_fee} lamports (base {base_fee} + priority {priority_fee} "
        f"+ tip {jito_tip_lamports}; ceiling {total_ceiling})",
    )

    report = VerificationReport(
        passed=all(c.passed for c in checks),
        checks=tuple(checks),
        message_sha256=message_sha256,
        fee_payer=fee_payer,
        num_required_signatures=num_required,
        recent_blockhash=recent_blockhash,
        jito_tip_lamports=jito_tip_lamports,
        jito_tip_destination=jito_tip_destination,
        priority_fee_lamports=priority_fee,
        compute_unit_limit=unit_limit,
        compute_unit_price_micro_lamports=unit_price,
        total_fee_lamports=total_fee,
        program_ids=tuple(dict.fromkeys(program_ids)),
        unresolved_lookup_tables=tuple(unresolved_tables),
        resolved_key_count=len(keys),
        raw_tx_b64=tx_b64,
    )

    log.info(
        "solana_tx_verified",
        passed=report.passed,
        message_sha256=report.message_sha256,
        fee_payer=report.fee_payer,
        jito_tip_lamports=report.jito_tip_lamports,
        priority_fee_lamports=report.priority_fee_lamports,
        total_fee_lamports=report.total_fee_lamports,
        failed_checks=[c.name for c in report.failures],
    )
    return report
