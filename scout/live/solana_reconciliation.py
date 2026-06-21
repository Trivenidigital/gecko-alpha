"""Boot-time recovery for on-chain trades: the tx signature is source of truth.

For each open solana live_trades row carrying a signature, re-check the chain:
success -> filled, failed -> rejected, pending -> leave open for next boot.
"""

from __future__ import annotations

import structlog

from scout.db import Database

log = structlog.get_logger(__name__)


async def reconcile_open_solana_trades(*, db: Database, rpc, settings) -> dict[str, int]:
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    # Only sent-but-unconfirmed rows: a confirmed open position has its
    # entry_fill_price set by the live dispatch path, so it is excluded here.
    cur = await db._conn.execute(
        "SELECT id, entry_order_id FROM live_trades "
        "WHERE venue='solana' AND status='open' AND entry_order_id IS NOT NULL "
        "AND entry_fill_price IS NULL"
    )
    rows = await cur.fetchall()

    confirmed = failed = pending = 0
    for row_id, signature in rows:
        try:
            state = await rpc.confirm_signature(signature)
        except Exception:
            log.warning("solana_reconciliation_row_err", row_id=row_id, signature=signature)
            pending += 1
            continue
        if state == "success":
            # Entry landed → genuinely an OPEN position. Do NOT write 'filled'
            # (forbidden by the live_trades.status CHECK, and a filled buy is
            # open until it exits). Leave status='open'.
            confirmed += 1
        elif state == "failed":
            # Swap reverted on-chain → no position exists.
            await db._conn.execute(
                "UPDATE live_trades SET status='rejected' WHERE id=?", (row_id,)
            )
            failed += 1
        else:
            pending += 1
    await db._conn.commit()

    summary = {"confirmed": confirmed, "failed": failed, "pending": pending}
    log.info("solana_reconciliation_done", rows_inspected=len(rows), **summary)
    return summary
