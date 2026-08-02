"""Observe-only per-token capture wiring (I2/I3), extracted from main.py so the
flag-gated capture is unit-testable end-to-end (the writers alone don't prove
the pipeline invokes them). Gated by ``DEX_INSTRUMENTATION_ENABLED``. No aiohttp
import, so this is locally testable.
"""


async def capture_entry_mcap(db, token, settings) -> None:
    """I2: record the earliest DEX-side entry mcap for a scored token (gated)."""
    if not getattr(settings, "DEX_INSTRUMENTATION_ENABLED", False):
        return
    fsa = token.first_seen_at
    await db.record_entry_mcap(
        token.contract_address,
        token.chain,
        fsa.isoformat() if hasattr(fsa, "isoformat") else str(fsa),
        token.market_cap_usd,
        token.liquidity_usd,
        token.token_age_days,
    )


async def capture_txns(db, token, settings) -> None:
    """I3: snapshot raw buy/sell counts from whichever source provided them (gated).

    DexScreener counts live on ``txns_h1_*``; GeckoTerminal counts on the
    instrumentation-only ``gt_txns_*`` fields (kept out of the scorer). The source
    is derived from which field is populated — not a heuristic — so the recorded
    ``source`` is always accurate.
    """
    if not getattr(settings, "DEX_INSTRUMENTATION_ENABLED", False):
        return
    if token.txns_h1_buys is not None or token.txns_h1_sells is not None:
        await db.log_txns_snapshot(
            token.contract_address, token.txns_h1_buys, token.txns_h1_sells, "dexscreener"
        )
    elif token.gt_txns_h1_buys is not None or token.gt_txns_h1_sells is not None:
        await db.log_txns_snapshot(
            token.contract_address,
            token.gt_txns_h1_buys,
            token.gt_txns_h1_sells,
            "geckoterminal",
        )
