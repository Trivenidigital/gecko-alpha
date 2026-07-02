"""Shared token_id shape helpers.

``is_cg_coin_id`` was extracted from ``scout/ingestion/held_position_prices.py``
(GA-01 unpriceable-position safety) so the paper-trade dispatch gate in
``scout/trading/engine.py`` can reuse the exact same heuristic without
importing the ingestion module — which pulls ``aiohttp`` at import time
(Windows OpenSSL Applink hazard) and would couple the trading path to the
ingestion layer. Single source of truth: any future refinement of the
heuristic lands here and flows to both the refresh lane and the gate.
"""

from __future__ import annotations


def is_cg_coin_id(token_id: str | None) -> bool:
    """Heuristic: skip obvious contract addresses; pass everything else.

    CG coin_ids are lowercase alphanumeric + hyphens/underscores.
    Contract addresses look different:
      - EVM: starts with '0x', 40+ hex chars
      - Solana base58 mints: mixed case, 32-44 chars (e.g.,
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v" — too short to be
        caught by a length>60 check; the mixed-case check catches it).
      - DexScreener-fallback namespace ids: `dex:{chain}:{address}` — the
        ':' separator fails the trailing charset check.

    Permissive on the CG side, strict on the obvious-contract side —
    false-negatives at CG just produce `not_found_in_cg` log entries and
    skip, not data corruption.
    """
    if not token_id:
        return False
    if token_id.startswith("0x"):
        return False
    if len(token_id) > 60:
        return False
    # CG coin_ids are lowercase; mixed-case is a strong signal of base58
    # (Solana mints, etc.). The triage script that produced the empirical
    # held cohort confirmed 0 false-skips of real CG ids under this rule.
    if token_id != token_id.lower():
        return False
    return all(c.isalnum() or c in "-_" for c in token_id)
