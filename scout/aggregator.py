"""Candidate token aggregation and deduplication."""

import structlog

from scout.models import CandidateToken

logger = structlog.get_logger()

# Fields to preserve from earlier entries if the later entry has None
_PRESERVE_FIELDS = [
    "cg_trending_rank",
    "price_change_1h",
    "price_change_24h",
    "vol_7d_avg",
    "txns_h1_buys",
    "txns_h1_sells",
]


def aggregate(candidates: list[CandidateToken]) -> list[CandidateToken]:
    """Merge and deduplicate candidates by contract_address.

    Last-write-wins for most fields, but preserves enrichment fields
    (cg_trending_rank, price_change, txns) from earlier entries if the
    later entry has None for them.
    """
    seen: dict[str, CandidateToken] = {}
    for token in candidates:
        addr = token.contract_address
        if addr in seen:
            # Merge: new token wins, but preserve non-None fields from old
            old = seen[addr]
            updates = {}
            for field in _PRESERVE_FIELDS:
                new_val = getattr(token, field)
                old_val = getattr(old, field)
                if new_val is None and old_val is not None:
                    updates[field] = old_val
            if updates:
                token = token.model_copy(update=updates)
        seen[addr] = token

    # Log how many tokens have trending rank after aggregation
    ranked = sum(1 for t in seen.values() if t.cg_trending_rank is not None)
    if ranked > 0:
        logger.info("aggregator_trending_preserved", ranked_tokens=ranked)

    return list(seen.values())
