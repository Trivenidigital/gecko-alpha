"""DB-side helpers for narrative API endpoints.

Split from narrative.py to keep the router thin and the resolver
independently testable. Per BL-072 + the "Hermes does heavy lifting"
principle, this module is intentionally minimal — it persists Hermes-emitted
events and does CA-only resolution lookups via existing infrastructure.

V1 CA-only resolver (Vector A FC-1 fold): gecko-alpha has no
``lookup_by_symbol(symbol, chain)`` primitive. Cashtag-only tweets get
``resolved_coin_id=NULL`` and Hermes side handles the deferred-resolution
retry. This module ONLY resolves by CA.
"""

from __future__ import annotations

from typing import Any

import aiosqlite
import structlog

_log = structlog.get_logger()


async def resolve_ca(db_path: str, *, ca: str, chain: str) -> dict[str, Any] | None:
    """Resolve a contract address to canonical CG/DexScreener data.

    Reads from existing tables populated by scout/ingestion/* and scout/safety.py.
    Returns ``None`` if no resolution found across all sources (Hermes side
    treats as deferred-resolution case).

    V1 scope:
      - solana: looks up candidates / price_cache by contract_address; falls
        back to DexScreener-side schema if available.
      - ethereum / base: looks up candidates / price_cache; same fallback.

    Implementation is intentionally simple — V1 returns first-match-wins from
    the available tables. V2 may layer a richer multi-source resolution.
    """
    async with aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True) as db:
        db.row_factory = aiosqlite.Row

        # 1. Try candidates table — primary CoinGecko-ingestion home.
        try:
            cur = await db.execute(
                """SELECT contract_address, chain, token_name, ticker,
                          market_cap_usd, liquidity_usd
                   FROM candidates
                   WHERE contract_address = ? AND chain = ?
                   LIMIT 1""",
                (ca, chain),
            )
            row = await cur.fetchone()
            if row is not None:
                # price_cache lookup keyed on coin_id; not contract_address.
                # Skip the price join for V1 — Hermes can call back for it if needed.
                return {
                    "coin_id": None,  # candidates doesn't carry coin_id directly
                    "symbol": row["ticker"],
                    "name": row["token_name"],
                    "market_cap_usd": row["market_cap_usd"],
                    "liquidity_usd": row["liquidity_usd"],
                    "price_usd": None,
                    "source": "candidates",
                }
        except aiosqlite.OperationalError as e:
            # Table missing on very old DB snapshot — fall through.
            _log.warning("narrative_resolver_candidates_oe", err=str(e))

        # 2. No additional V1 source. Return None → deferred-resolution.
        return None


async def insert_narrative_alert(db_path: str, payload: Any) -> dict[str, Any]:
    """Insert a Hermes-emitted narrative event into ``narrative_alerts_inbound``.

    Idempotent via ``UNIQUE(event_id)``. Returns:
      - ``{"status": "created", "id": <rowid>}`` on first insert
      - ``{"status": "duplicate"}`` if event_id already seen

    Errors propagate as exceptions; the FastAPI layer converts to 500.
    """
    async with aiosqlite.connect(db_path) as db:
        try:
            cur = await db.execute(
                """INSERT INTO narrative_alerts_inbound (
                    event_id, tweet_id, tweet_author, tweet_ts, tweet_text,
                    tweet_text_hash, extracted_cashtag, extracted_ca, extracted_chain,
                    resolved_coin_id, narrative_theme, urgency_signal,
                    classifier_confidence, classifier_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    payload.event_id,
                    payload.tweet_id,
                    payload.tweet_author,
                    payload.tweet_ts,
                    payload.tweet_text,
                    payload.tweet_text_hash,
                    payload.extracted_cashtag,
                    payload.extracted_ca,
                    payload.extracted_chain,
                    payload.resolved_coin_id,
                    payload.narrative_theme,
                    payload.urgency_signal,
                    payload.classifier_confidence,
                    payload.classifier_version,
                ),
            )
            await db.commit()
            _log.info(
                "narrative_alert_inserted",
                event_id=payload.event_id,
                tweet_id=payload.tweet_id,
                author=payload.tweet_author,
                chain=payload.extracted_chain,
                resolved=payload.resolved_coin_id is not None,
            )
            return {"status": "created", "id": cur.lastrowid}
        except aiosqlite.IntegrityError as e:
            if "UNIQUE" in str(e) or "unique" in str(e):
                _log.info(
                    "narrative_alert_duplicate",
                    event_id=payload.event_id,
                )
                return {"status": "duplicate"}
            raise
