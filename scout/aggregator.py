"""Candidate token aggregation and deduplication."""

from scout.models import CandidateToken


def aggregate(candidates: list[CandidateToken]) -> list[CandidateToken]:
    """Merge and deduplicate candidates by contract_address.

    Last-write-wins: if the same contract_address appears multiple times
    (e.g., from DexScreener and GeckoTerminal), the last occurrence's
    price/volume fields take precedence.
    """
    seen: dict[str, CandidateToken] = {}
    for token in candidates:
        seen[token.contract_address] = token
    return list(seen.values())
