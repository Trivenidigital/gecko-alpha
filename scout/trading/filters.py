"""Shared filter helpers used by both predictor.py (upstream defense in depth)
and signals.py dispatchers (downstream pre-open gates).

Extracted from scout/trading/signals.py:565-635 so scout/narrative/predictor.py
can apply `_is_tradeable_candidate` at fetch time without forming a circular
import (predictor → signals → predictor would cycle if `signals` imported
from `predictor`).

Original location of these symbols remains importable from
`scout.trading.signals` for back-compat with existing callers (T5 pin).
"""

from __future__ import annotations

# coin_id substrings that identify wrapped/bridged assets. These pump rarely
# (price tracks the underlying) and consume paper-trade slots.
_JUNK_COINID_SUBSTRINGS = (
    "-bridged-",
    "-wrapped-",
)
_JUNK_COINID_PREFIXES = (
    "bridged-",
    "wrapped-",
    "superbridge-",
    # BL-076: CoinGecko placeholder coins (test-1, test-2, ..., test-N)
    # have real price feeds and triggered paper trades #980 (first_signal,
    # closed -$9.96) and #1551 (volume_spike, closed +$188.91 by lucky
    # pump). Anchored at slug start (startswith) — false positives like
    # "protest-coin", "biggest-token", "pretest" are NOT rejected (T1b
    # pins this). Trade-off: legit testnet-themed tokens like
    # "test-net-token" WOULD be rejected — accepted risk; operator can
    # grep signal_skipped_junk events to spot losses. If the prefix tuple
    # exceeds ~10 entries OR a regex/substring requirement appears,
    # refactor to settings-backed PAPER_JUNK_COINID_PREFIXES.
    "test-",
)


def _is_junk_coinid(coin_id: object) -> bool:
    """True when coin_id matches a wrapped/bridged/superbridge slug pattern.

    Defensive on type: non-str or empty inputs return False (no match). A
    caller that cares about missing/invalid input should check separately —
    here we just report "not a known junk pattern."
    """
    if not isinstance(coin_id, str) or not coin_id:
        return False
    cid = coin_id.lower()
    if cid.startswith(_JUNK_COINID_PREFIXES):
        return True
    return any(s in cid for s in _JUNK_COINID_SUBSTRINGS)


def _is_tradeable_candidate(coin_id: object, ticker: object) -> bool:
    """Paper-trade admission filter shared across the non-prediction signal paths.

    Mirrors the PR #44 gates used by trade_predictions, minus the category
    check — the 6 non-prediction paths query intermediate snapshot tables
    that carry no category column (see BL-061 for the follow-up).

    Returns False when the token is an obvious junk asset:
    - wrapped/bridged/superbridge coin_id (price tracks the underlying)
    - non-ASCII coin_id (Chinese-meme / cyrillic / emoji slug)
    - non-ASCII ticker (same classes on the symbol side — surfaced post-wipe
      on 2026-04-22 with tokens like 我踏马来了 and 币安人生)
    - missing / non-str coin_id or ticker (can't safely trade it)
    - empty / whitespace-only coin_id or ticker (this PR — adv-M1)

    Scope note — DEX-origin tokens: the coin_id prefix and ASCII checks are
    designed for CoinGecko slugs (e.g. "wrapped-bitcoin"). For DexScreener
    and GeckoTerminal inputs, `contract_address` is an EVM hex or Solana
    mint. Hex passes the prefix check by construction (never starts with
    "wrapped-" / "bridged-") and is always ASCII — so on DEX paths this
    filter degenerates to "reject only on non-ASCII ticker." That is the
    intended behavior for BL-059; separate DEX-specific junk guards belong
    in their own backlog item.
    """
    if not isinstance(coin_id, str) or not coin_id or not coin_id.strip():
        return False
    if not isinstance(ticker, str) or not ticker or not ticker.strip():
        return False
    if _is_junk_coinid(coin_id):
        return False
    if not coin_id.isascii() or not ticker.isascii():
        return False
    return True
