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
    Returns:
      - ``None`` if the CA is genuinely not found (Hermes treats as deferred)
      - ``dict`` with resolved fields if found
      - ``dict`` with ``_resolver_error: True`` if a DB-side error prevented
        resolution (V2-PR-review C-SFC2 fold — differentiate from genuinely-unknown)

    Case-normalization (V2-PR-review A-coverage-gap fold): EVM addresses are
    checksummed mixed-case by convention; SQLite WHERE is case-sensitive.
    Lowercase the CA before comparison for ETH/BASE; Solana base58 is
    case-sensitive natively (no normalization).

    Defensive open (V2-PR-review A-I3 fold): aiosqlite.connect can raise
    OperationalError if the DB file is missing at first request — return
    _resolver_error sentinel rather than 500-ing the request.
    """
    # Case-normalize for EVM chains.
    query_ca = ca.lower() if chain in ("ethereum", "base") else ca

    try:
        conn_ctx = aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True)
    except aiosqlite.OperationalError as e:
        _log.warning(
            "narrative_resolver_connect_failed", err=str(e), ca=ca, chain=chain
        )
        return {"_resolver_error": True}

    try:
        async with conn_ctx as db:
            db.row_factory = aiosqlite.Row

            try:
                cur = await db.execute(
                    """SELECT contract_address, chain, token_name, ticker,
                              market_cap_usd, liquidity_usd
                       FROM candidates
                       WHERE LOWER(contract_address) = LOWER(?) AND chain = ?
                       LIMIT 1""",
                    (query_ca, chain),
                )
                row = await cur.fetchone()
                if row is not None:
                    return {
                        "coin_id": None,
                        "symbol": row["ticker"],
                        "name": row["token_name"],
                        "market_cap_usd": row["market_cap_usd"],
                        "liquidity_usd": row["liquidity_usd"],
                        "price_usd": None,
                        "source": "candidates",
                    }
            except aiosqlite.OperationalError as e:
                # Table missing on pre-migration DB snapshot. Signal upstream so
                # Hermes can distinguish "unknown CA" from "resolver broken".
                _log.warning(
                    "narrative_resolver_query_oe",
                    err=str(e),
                    ca=ca,
                    chain=chain,
                )
                return {"_resolver_error": True}

            return None
    except aiosqlite.OperationalError as e:
        _log.warning("narrative_resolver_ctx_oe", err=str(e))
        return {"_resolver_error": True}


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
            # V2-PR-review A-N2 fold: prefer sqlite_errorcode (2067 = SQLITE_CONSTRAINT_UNIQUE)
            # over substring match. Fall back to substring if errorcode unavailable
            # (older aiosqlite versions on weird Python builds).
            errcode = getattr(e, "sqlite_errorcode", None)
            is_unique = errcode == 2067 or "UNIQUE" in str(e) or "unique" in str(e)
            if is_unique:
                _log.info(
                    "narrative_alert_duplicate",
                    event_id=payload.event_id,
                )
                return {"status": "duplicate"}
            raise
