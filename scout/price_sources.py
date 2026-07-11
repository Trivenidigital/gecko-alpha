"""Registered price sources + exit provenances (Phase 6 slices 2+3).

The GA-01 class of silent failure was "caller passes a non-market price":
positions opened via a caller-supplied entry_price for a token_id no
price writer serves, and closes recorded at bookkeeping prices that were
indistinguishable from market fills. This module is the single registry
that makes both states label-carrying:

- **Open boundary** — a paper trade may only be opened with a price
  source from :data:`REGISTERED_PRICE_SOURCES` (resolved by
  :func:`resolve_price_source`). ``'legacy'`` exists ONLY as a
  migration backfill label for rows opened before the invariant; it is
  deliberately NOT registered, so no new open can claim it.
- **Close boundary** — every close records one of
  :data:`EXIT_PROVENANCES` in ``paper_trades.exit_provenance``.

Kept free of aiohttp/ingestion imports (Windows OpenSSL Applink hazard)
so the trading path can import it unconditionally — same rationale as
``scout.token_ids``.
"""

from __future__ import annotations

import structlog

from scout.token_ids import is_cg_coin_id

logger = structlog.get_logger(__name__)

# --- Open boundary -----------------------------------------------------------

#: Served by the CG markets/trending writers + held-position refresh lane.
PRICE_SOURCE_CG_LANE = "cg_lane"
#: Some writer demonstrably serves this token_id (a price_cache row exists).
PRICE_SOURCE_PRICE_CACHE_ROW = "price_cache_row"
#: Migration-only backfill label for rows opened before the invariant.
#: NOT registered — new opens can never claim it.
PRICE_SOURCE_LEGACY = "legacy"

REGISTERED_PRICE_SOURCES: frozenset[str] = frozenset(
    {PRICE_SOURCE_CG_LANE, PRICE_SOURCE_PRICE_CACHE_ROW}
)

#: Every value that may legitimately appear in paper_trades.price_source.
KNOWN_PRICE_SOURCES: frozenset[str] = REGISTERED_PRICE_SOURCES | {PRICE_SOURCE_LEGACY}


def resolve_price_source(token_id: str | None, has_price_cache_row: bool) -> str | None:
    """Resolve the registered price source for a token_id, or None.

    Mirrors the GA-01 dispatch-gate admissibility rule
    (scout/trading/engine.py step 0c): a token is re-priceable iff it is
    CG-id-shaped (the CG lanes serve it) OR a price_cache row already
    exists (some writer demonstrably serves it). ``None`` means
    unresolvable — the position must NOT open.

    Funnel-reopening precondition (REC-06): a ``dex:{chain}:{address}``
    token_id NEVER resolves ``'price_cache_row'``. #408 made every open
    label-carrying but only STAMPS the source — admissibility still admits
    any token that has a price_cache row. Today dex: opens are blocked only
    EMPIRICALLY (the CG-markets price_cache writer keys by CG slug, never
    dex:), so no dex: row exists at dispatch. Before any funnel reopening
    adds a writer that keys dex: rows, this exclusion keeps a dex: token
    unpriceable-by-registry regardless of whether a row exists. A row
    EXISTING while we refuse it is the §9c tripwire — it means a dex-keyed
    price_cache writer WAS added — so we warn loudly (``dex_price_cache_row_
    blocked``). See tasks/findings_w3_analysis_gates_2026_07_11.md §REC-06.
    """
    if not token_id:
        return None
    if token_id.startswith("dex:"):
        if has_price_cache_row:
            logger.warning(
                "dex_price_cache_row_blocked",
                token_id=token_id,
                hint=(
                    "a price_cache row exists for a dex: token_id — a "
                    "dex-keyed price_cache writer was added; refusing the "
                    "price_cache_row lane (GA-01 unpriceable-position guard). "
                    "Audit the writer before reopening the dex: funnel."
                ),
            )
        return None
    if is_cg_coin_id(token_id):
        return PRICE_SOURCE_CG_LANE
    if has_price_cache_row:
        return PRICE_SOURCE_PRICE_CACHE_ROW
    return None


# --- Close boundary ----------------------------------------------------------

#: Fresh market price from price_cache (the normal exit path).
EXIT_PROVENANCE_MARKET = "market"
#: Last-good cached price older than the evaluator freshness window —
#: real-ish mark, not a live fill (stale-onset exits + stale expiry).
EXIT_PROVENANCE_STALE_SNAPSHOT = "stale_snapshot"
#: No price available at all — bookkeeping close at entry_price
#: (pnl exactly $0, fabricated).
EXIT_PROVENANCE_ENTRY_FALLBACK = "entry_fallback"
#: SIG-05 stop-fill slippage model: a stop close whose observed price gapped
#: below the bounded gap floor, so the fill was clamped to
#: sl_price*(1 - PAPER_STOP_GAP_BPS/10000) rather than the deep crash
#: snapshot. A real-ish modeled fill (counts like a market exit downstream);
#: the raw observed price is recorded in a stop_fill_slippage_model decision
#: event so realized-vs-modeled stays auditable.
EXIT_PROVENANCE_STOP_GAP_MODEL = "stop_gap_model"

EXIT_PROVENANCES: frozenset[str] = frozenset(
    {
        EXIT_PROVENANCE_MARKET,
        EXIT_PROVENANCE_STALE_SNAPSHOT,
        EXIT_PROVENANCE_ENTRY_FALLBACK,
        EXIT_PROVENANCE_STOP_GAP_MODEL,
    }
)
