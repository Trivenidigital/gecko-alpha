"""BL-NEW-LIVE-HYBRID M1 v2.1 (Task 12): client_order_id idempotency.

Helpers for constructing + dedup-checking the gecko-side idempotency
key. The contract:

    client_order_id = f"gecko-{paper_trade_id}-{intent_uuid}"

Stable + deterministic — same (paper_trade_id, intent_uuid) pair
always produces the same client_order_id. Pre-retry dedup query checks
`live_trades.client_order_id` before submitting to the venue; retried
submits return the existing venue_order_id rather than placing a
duplicate.

The `intent_uuid` is generated once per intent at the engine layer
(NOT per-call); subsequent retries on transient failures reuse the
same UUID so the client_order_id remains stable across the retry
window.

Binance accepts `newClientOrderId` (28-char limit). Our format
"gecko-<paper_id>-<uuid>" can exceed 28 chars (uuid alone is 36).
Truncate to first 8 chars of the uuid to fit within Binance's limit
while preserving enough entropy to avoid collisions across the
expected daily fill rate.
"""

from __future__ import annotations

from typing import Any

import structlog

from scout.db import Database

log = structlog.get_logger(__name__)

CLIENT_ORDER_ID_BINANCE_MAX_LEN = 28


def make_client_order_id(paper_trade_id: int, intent_uuid: str) -> str:
    """Construct the deterministic client_order_id.

    Format: "gecko-{paper_trade_id}-{uuid8}" where uuid8 is the first 8
    hex chars of the intent_uuid (stripping dashes). Total length stays
    ≤ 28 chars for paper_trade_id < 10**12.
    """
    short_uuid = intent_uuid.replace("-", "")[:8]
    cid = f"gecko-{paper_trade_id}-{short_uuid}"
    if len(cid) > CLIENT_ORDER_ID_BINANCE_MAX_LEN:
        # Defensive truncation — should not happen at expected scales.
        cid = cid[:CLIENT_ORDER_ID_BINANCE_MAX_LEN]
    return cid


async def lookup_existing_order_id(db: Database, client_order_id: str) -> str | None:
    """Pre-retry dedup. Returns existing venue_order_id if a live_trades
    row already has this client_order_id; else None.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    cur = await db._conn.execute(
        "SELECT entry_order_id FROM live_trades "
        "WHERE client_order_id = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (client_order_id,),
    )
    row = await cur.fetchone()
    return row[0] if row is not None else None


async def record_pending_order(
    db: Database,
    *,
    client_order_id: str,
    paper_trade_id: int,
    coin_id: str,
    symbol: str,
    venue: str,
    pair: str,
    signal_type: str,
    size_usd: str,
    mid_at_entry: str | None = None,
) -> int:
    """Insert a `live_trades` row in 'open' status before venue submit.

    The row's UNIQUE client_order_id constraint backstops idempotency
    at the DB layer — concurrent retries that race past the application-
    level dedup query will still fail with IntegrityError on the
    constraint.

    Returns the inserted live_trades.id.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat()
    cur = await db._conn.execute(
        """INSERT INTO live_trades
           (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
            size_usd, mid_at_entry, status, client_order_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
        (
            paper_trade_id,
            coin_id,
            symbol,
            venue,
            pair,
            signal_type,
            size_usd,
            mid_at_entry,
            client_order_id,
            now_iso,
        ),
    )
    await db._conn.commit()
    return cur.lastrowid or 0
