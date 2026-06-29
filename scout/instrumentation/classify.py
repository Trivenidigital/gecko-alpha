"""DEX-vs-CoinGecko address classifier (B2 in the instrumentation spec).

`score_history` / `candidates` rows are keyed by `contract_address`, which holds
either an on-chain DEX address (EVM `0x...` or a Solana base58 mint) or — for
CoinGecko-sourced rows — the CG slug (which *is* the coin_id). Coverage metrics
and watchdogs need to bucket these without depending on the pruned
`candidates.chain` column.

Refinement vs the spec's illustrative regex: the spec wrote the CG-slug pattern
as ``^[a-z0-9]+(-[a-z0-9]+)+$`` (hyphen required), which would misclassify
single-word slugs like ``myro``/``bitcoin``. We instead classify by *positive*
match on the two on-chain forms and treat everything else as a CG slug, which
preserves the spec's intent (separate DEX mints from CG slugs) without the
single-word gap.
"""

import re

# EVM contract: 0x + exactly 40 hex chars.
_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Solana mint: base58 (no 0/O/I/l), 32-44 chars, no hyphens. CG slugs that happen
# to be long always contain hyphens or non-base58 chars, so they fall through.
_SOLANA_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def classify_contract(contract_address: str) -> str:
    """Return one of ``"evm"``, ``"solana"`` (DEX), or ``"coingecko"`` (CG slug)."""
    addr = (contract_address or "").strip()
    if _EVM_RE.match(addr):
        return "evm"
    if _SOLANA_RE.match(addr):
        return "solana"
    return "coingecko"


def is_dex(contract_address: str) -> bool:
    """True for on-chain DEX addresses (EVM/Solana), False for CG slugs."""
    return classify_contract(contract_address) in ("evm", "solana")
