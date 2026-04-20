# scout/perp/enrichment.py
"""Enrich CandidateTokens with perp anomaly tags from the DB."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from scout.config import Settings
    from scout.db import Database
    from scout.models import CandidateToken

log = structlog.get_logger(__name__)


async def enrich_candidates_with_perp_anomalies(
    tokens: list["CandidateToken"],
    db: "Database",
    settings: "Settings",
) -> list["CandidateToken"]:
    """Attach perp-anomaly fields to matching candidates. Pure-ish (DB read)."""
    if not tokens:
        return tokens
    tickers = list({t.ticker.upper() for t in tokens if t.ticker})
    if not tickers:
        return tokens
    since = datetime.now(timezone.utc) - timedelta(
        minutes=settings.PERP_ANOMALY_LOOKBACK_MIN
    )
    anomalies = await db.fetch_recent_perp_anomalies(tickers=tickers, since=since)
    if not anomalies:
        return tokens

    # Index by ticker -> best-effort most-recent first (fetch returns DESC)
    by_ticker: dict[str, list] = {}
    for a in anomalies:
        by_ticker.setdefault(a.ticker.upper(), []).append(a)

    enriched: list[CandidateToken] = []
    for token in tokens:
        matches = by_ticker.get((token.ticker or "").upper())
        if not matches:
            enriched.append(token)
            continue
        latest = matches[0]
        funding_flip = any(a.kind == "funding_flip" for a in matches)
        oi_spike_ratio = max(
            (a.magnitude for a in matches if a.kind == "oi_spike"),
            default=None,
        )
        enriched.append(
            token.model_copy(
                update={
                    # None = no anomaly in window; True = flip detected (False never used)
                    "perp_funding_flip": funding_flip or None,
                    "perp_oi_spike_ratio": oi_spike_ratio,
                    "perp_last_anomaly_at": latest.observed_at,
                    "perp_exchange": latest.exchange,
                }
            )
        )
    log.debug(
        "perp_enrichment_done",
        token_count=len(tokens),
        matches=sum(1 for t in enriched if t.perp_last_anomaly_at is not None),
    )
    return enriched
