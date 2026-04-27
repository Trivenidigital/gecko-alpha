"""BL-064 message parser — pure regex extraction.

Extracts cashtags ($SYMBOL), blockchain contract addresses (multi-chain),
and DEX/explorer URLs from free-form Telegram message text. No I/O, no
state — fully testable in isolation.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from scout.social.telegram.models import ContractRef, ParsedMessage

# $SYMBOL — 2-12 alphanumerics (allows the standard $WIF, $RIV, $PEPE shape).
# Excludes $ followed by digit-only strings ($100 etc.) which would be amounts.
_CASHTAG_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9_]{1,11})\b")

# EVM 0x — exactly 40 hex chars after 0x. Word-boundary on each side.
_EVM_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")

# Solana base58 — 32-44 chars, restricted alphabet (no 0/O/I/l).
# Word-boundary anchors prevent matching inside longer hex strings.
_SOLANA_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

# DEX / explorer URLs — used to extract the embedded CA when the post
# links to a tracker rather than typing the CA out.
_URL_RE = re.compile(r"https?://[^\s<>\")]+", re.IGNORECASE)

# CAs embedded in DexScreener / Birdeye / Photon URLs.
# DexScreener:  https://dexscreener.com/solana/<pair>  OR  /<chain>/<token>
# Birdeye:      https://birdeye.so/token/<address>?chain=solana
# Photon:       https://photon-sol.tinyastro.io/en/lp/<lp>
# We don't try to invert pair→token here — that's the resolver's job.
_DEX_HOST_CHAIN: dict[str, str | None] = {
    # None = chain inferred from URL path (dexscreener) or query (birdeye)
    "dexscreener.com": None,
    "www.dexscreener.com": None,
    "birdeye.so": None,
    "www.birdeye.so": None,
    "photon-sol.tinyastro.io": "solana",
    "solscan.io": "solana",
    "etherscan.io": "ethereum",
    "basescan.org": "base",
    "polygonscan.com": "polygon",
    "arbiscan.io": "arbitrum",
    "optimistic.etherscan.io": "optimism",
    "bscscan.com": "bsc",
    "snowtrace.io": "avalanche",
    "ftmscan.com": "fantom",
}
_DEX_HOSTS = set(_DEX_HOST_CHAIN.keys())


def _classify_chain(address: str) -> str | None:
    """Best-effort chain classification from address shape.

    EVM addresses are unambiguous (0x + 40 hex). Solana base58 is not
    rigorously distinguishable from other base58 chains (Sui, Aptos, etc.)
    — we tag it 'solana' as the dominant case and let the resolver fix
    misclassifications by chain-probing.
    """
    if address.startswith("0x") and len(address) == 42:
        return "ethereum"  # resolver may re-tag to base / arbitrum / etc.
    if (
        32 <= len(address) <= 44
        and address[0] in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    ):
        return "solana"
    return None


def parse_message(text: str | None) -> ParsedMessage:
    """Pure parse: text → ParsedMessage{cashtags, contracts, urls}.

    Idempotent. Empty / None input yields ParsedMessage with empty lists.
    Cashtags are normalised to UPPERCASE without the '$' prefix.
    Contract addresses are deduped within a single message (same address
    can appear repeated or inside a URL).
    """
    if not text:
        return ParsedMessage()

    # Cashtags — uppercase, dedup
    cashtags_set = set()
    for m in _CASHTAG_RE.finditer(text):
        cashtags_set.add(m.group(1).upper())
    cashtags = sorted(cashtags_set)

    # URLs (extracted first; their host helps decide CA chain attribution)
    urls = sorted(set(_URL_RE.findall(text)))

    # Contracts — dedup by normalized key (chain:address)
    contracts: dict[str, ContractRef] = {}
    for m in _EVM_RE.finditer(text):
        addr = m.group(0)
        ref = ContractRef(chain=_classify_chain(addr) or "ethereum", address=addr)
        contracts[ref.normalized] = ref
    for m in _SOLANA_RE.finditer(text):
        addr = m.group(0)
        # Skip if this is part of an EVM match we already captured (substring guard).
        if any(addr in evm_match.address for evm_match in contracts.values()):
            continue
        chain = _classify_chain(addr) or "solana"
        ref = ContractRef(chain=chain, address=addr)
        contracts[ref.normalized] = ref

    # URL-embedded CAs — pull CAs out of dex/explorer URL paths. The host
    # provides a stronger chain attribution than the address-shape heuristic
    # (e.g., basescan.org → 'base' instead of the parser default 'ethereum').
    # Closes round-2 Low #4.
    for url in urls:
        try:
            parsed = urlparse(url)
        except ValueError:
            continue
        if parsed.hostname not in _DEX_HOSTS:
            continue
        host_chain = _DEX_HOST_CHAIN.get(parsed.hostname)
        path_parts = [p for p in parsed.path.split("/") if p]
        # DexScreener / Birdeye encode the chain in the path or query.
        if (
            host_chain is None
            and parsed.hostname in ("dexscreener.com", "www.dexscreener.com")
            and path_parts
        ):
            # dexscreener.com/<chain>/<address>
            host_chain = path_parts[0]
        for part in path_parts:
            for re_ in (_EVM_RE, _SOLANA_RE):
                m = re_.search(part)
                if m:
                    addr = m.group(0)
                    chain = host_chain or _classify_chain(addr) or "solana"
                    ref = ContractRef(chain=chain, address=addr)
                    contracts.setdefault(ref.normalized, ref)

    return ParsedMessage(
        cashtags=cashtags,
        contracts=list(contracts.values()),
        urls=urls,
    )
