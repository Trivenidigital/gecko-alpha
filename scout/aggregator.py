"""Candidate token aggregation and deduplication."""

import structlog

from scout.ingestion.dexscreener import BoostInfo, _normalize_address
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


def apply_boost_decorations(
    candidates: list[CandidateToken],
    boosts: list[BoostInfo],
) -> list[CandidateToken]:
    """Decorate deduped candidates with DexScreener top-boost data (BL-051).

    Rank is derived positionally from the incoming `boosts` list order
    (index+1 = rank), reflecting the API's own totalAmount-desc ordering.
    Join key is (chain, normalized_address); EVM addresses are matched
    case-insensitive, non-EVM chains preserve case.

    Unmatched boost entries are silently dropped; unmatched candidates are
    returned unchanged (their `boost_total_amount` / `boost_rank` remain None).
    """
    if not boosts:
        return candidates

    boost_map: dict[tuple[str, str], tuple[float, int]] = {}
    for idx, b in enumerate(boosts):
        key = (b.chain, _normalize_address(b.chain, b.address))
        boost_map[key] = (b.total_amount, idx + 1)

    result: list[CandidateToken] = []
    for cand in candidates:
        key = (cand.chain, _normalize_address(cand.chain, cand.contract_address))
        hit = boost_map.get(key)
        if hit is None:
            result.append(cand)
            continue
        total, rank = hit
        result.append(
            cand.model_copy(update={"boost_total_amount": total, "boost_rank": rank})
        )
    return result
