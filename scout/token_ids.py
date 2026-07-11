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


def match_universe_exclude(patterns: list[str], token_id: str) -> str | None:
    """Return the first exclude-pattern that is a case-insensitive substring of
    *token_id*, else ``None`` (first-match-wins in list order).

    ALR-03 single source of truth for "is this token_id out of universe": the
    send-layer alert filter (``scout/trading/tg_alert_dispatch._check_universe``),
    the paper-engine open gate (``scout/trading/engine.open_trade``), and the
    ``scripts/universe_contamination_report.py`` backfill count all call this
    against the SAME ``ALERT_UNIVERSE_EXCLUDE_ID_PATTERNS`` list, so there is
    exactly one universe definition. The per-layer ENABLED flags stay separate
    — this helper does the substring matching only, never the gating.

    Out-of-universe ids are tokenized equities / ETFs (e.g.
    ``spy-bstocks-tokenized-stock``); the default pattern ``-tokenized-`` covers
    every observed prod offender. Matching is a RAW case-insensitive substring,
    so callers keep patterns specific (see the config field caution).
    """
    if not token_id:
        return None
    lowered = token_id.lower()
    for pattern in patterns:
        if pattern.lower() in lowered:
            return pattern
    return None
