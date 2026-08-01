"""Solana mainnet addresses and program ids for the supervised swap lane (PR-S1).

Every address here is load-bearing: ``tx_inspector`` decides whether a
remotely-built transaction is safe to sign by checking it against these sets,
so a wrong or over-broad entry is a hole in the only content check the lane
has. Nothing is inferred at runtime — the allowlists are closed sets.

Verified facts (2026-07-31)
---------------------------
1. Mints. SOL's wrapped-SOL mint is
   ``So11111111111111111111111111111111111111112`` (9 decimals) and mainnet
   USDC is ``EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`` (6 decimals).
   Both are echoed verbatim in Jupiter's own quote example
   (https://developers.jup.ag/docs/api-reference/swap/quote).
2. Jito block engine (https://docs.jito.wtf/lowlatencytxnsend/): mainnet host
   is ``https://mainnet.block-engine.jito.wtf``; the single-transaction path
   is ``/api/v1/transactions``. No API key is required — "You no longer need
   an approved auth key for default sends." Default rate limit is 1 request
   per second per IP per region. Minimum tip is 1000 lamports, with the docs
   warning that the minimum "might not be sufficient" under load.
3. Jito tip accounts: the 8 mainnet addresses below are the current list
   published for ``getTipAccounts``
   (https://mainnet.block-engine.jito.wtf/api/v1/getTipAccounts). They are a
   FALLBACK only — ``jito_client.fetch_tip_accounts`` prefers the live list,
   because Jito can rotate them and a stale allowlist would make the
   inspector reject a legitimately-tipped transaction.
"""

from __future__ import annotations

from typing import Final

# ----------------------------------------------------------------------
# Mints + decimals. The pilot is SOL -> USDC only.
# ----------------------------------------------------------------------
SOL_MINT: Final[str] = "So11111111111111111111111111111111111111112"
USDC_MINT: Final[str] = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

SOL_DECIMALS: Final[int] = 9
USDC_DECIMALS: Final[int] = 6

LAMPORTS_PER_SOL: Final[int] = 10**SOL_DECIMALS

# ----------------------------------------------------------------------
# Cluster identity
# ----------------------------------------------------------------------
# The genesis hash of Solana mainnet-beta, as returned by ``getGenesisHash``.
# It is the only unforgeable statement an endpoint makes about WHICH CHAIN it
# serves: a URL can say "mainnet", a provider dashboard can say "mainnet", and
# the node behind it can still be devnet or a fork.
#
# This matters most for the resolver. Every verdict it reaches is a claim about
# whether one signature exists on the chain the swap was submitted to, and an
# endpoint on a different chain answers "absent" to every signature it is ever
# asked about — manufacturing `definitively_not_submitted` for a transaction
# that landed. The host-substring checks elsewhere in the lane are a typo
# guard; this is the actual proof.
SOLANA_MAINNET_GENESIS_HASH: Final[str] = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"

# ----------------------------------------------------------------------
# Jito
# ----------------------------------------------------------------------
JITO_MAINNET_BLOCK_ENGINE_URL: Final[str] = "https://mainnet.block-engine.jito.wtf"
JITO_TRANSACTIONS_PATH: Final[str] = "/api/v1/transactions"
JITO_MIN_TIP_LAMPORTS: Final[int] = 1_000

# Fallback only — see module docstring fact 3.
JITO_TIP_ACCOUNTS_FALLBACK: Final[frozenset[str]] = frozenset(
    {
        "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
        "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
        "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
        "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
        "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
        "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
        "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
        "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
    }
)

# ----------------------------------------------------------------------
# Program ids the inspector will accept as an instruction target.
#
# Closed allowlist. A swap built by Jupiter for SOL->USDC touches: the
# Jupiter aggregator program, the SPL token programs (transfers + ATA
# creation), the system program (SOL wrap + the Jito tip transfer) and the
# compute-budget program (priority fee). Anything else is unrecognised and
# fails the inspection closed.
# ----------------------------------------------------------------------
SYSTEM_PROGRAM_ID: Final[str] = "11111111111111111111111111111111"
COMPUTE_BUDGET_PROGRAM_ID: Final[str] = "ComputeBudget111111111111111111111111111111"
TOKEN_PROGRAM_ID: Final[str] = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID: Final[str] = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ASSOCIATED_TOKEN_PROGRAM_ID: Final[str] = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
MEMO_PROGRAM_ID: Final[str] = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"

# Jupiter's aggregator programs. v6 is the current router; v4 is retained
# because Jupiter has historically routed a minority of paths through the
# older program during migrations, and an inspector that rejects it would
# fail closed on a legitimate route.
JUPITER_V6_PROGRAM_ID: Final[str] = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
JUPITER_V4_PROGRAM_ID: Final[str] = "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB"

ALLOWED_PROGRAM_IDS: Final[frozenset[str]] = frozenset(
    {
        SYSTEM_PROGRAM_ID,
        COMPUTE_BUDGET_PROGRAM_ID,
        TOKEN_PROGRAM_ID,
        TOKEN_2022_PROGRAM_ID,
        ASSOCIATED_TOKEN_PROGRAM_ID,
        MEMO_PROGRAM_ID,
        JUPITER_V6_PROGRAM_ID,
        JUPITER_V4_PROGRAM_ID,
    }
)

# ----------------------------------------------------------------------
# Compute-budget instruction discriminators (first byte of instruction data).
# Source: solana-program-library compute_budget instruction enum.
# Only the two the inspector needs to price a priority fee.
# ----------------------------------------------------------------------
COMPUTE_BUDGET_SET_UNIT_LIMIT: Final[int] = 2
COMPUTE_BUDGET_SET_UNIT_PRICE: Final[int] = 3

# System-program ``transfer`` discriminator (little-endian u32 = 2), the
# instruction a Jito tip arrives as.
SYSTEM_TRANSFER_INSTRUCTION: Final[int] = 2

# Base signature fee per required signature, in lamports. Fixed protocol
# constant, used to price the total-fee ceiling alongside the priority fee
# and the tip.
LAMPORTS_PER_SIGNATURE: Final[int] = 5_000

# ----------------------------------------------------------------------
# Associated token account rent (PR-S2 balance gate)
# ----------------------------------------------------------------------
# An SPL token account is 165 bytes. If the wallet has never held the output
# mint, the swap transaction creates its ATA and the rent-exempt minimum for
# those 165 bytes leaves the wallet alongside the swap and the fees.
TOKEN_ACCOUNT_DATA_LENGTH: Final[int] = 165

# Rent-exempt minimum for a 165-byte account under mainnet's current rent
# parameters. A FALLBACK only: rent is a cluster setting rather than a
# protocol constant, so the pilot asks
# ``rpc_client.get_minimum_balance_for_rent_exemption(165)`` first and records
# in the evidence which of the two figures it used.
ATA_RENT_LAMPORTS_FALLBACK: Final[int] = 2_039_280
