"""BL-NEW-LIVE-ELIGIBLE: Compute `would_be_live` flag at paper-trade open.

Pure observability — no production behavior change. Paper trades open at
the same rate; this just stamps a boolean column on each row so the
dashboard + digests can show "what the live-tradeable subset looked like."

Tier rules derived from `tasks/findings_live_eligibility_winners_vs_losers_2026_05_11.md`:
- **Tier 1 (mandatory):** signal_type='chain_completed' OR conviction
  stack >= 3 at open time. n=27 in soak, 77.8% WR, $47/trade.
- **Tier 2 (high quality):** signal_type='volume_spike' (any spike_ratio)
  OR signal_type='gainers_early' AND mcap >= PAPER_TIER2_GAINERS_MIN_MCAP_USD
  AND 24h >= PAPER_TIER2_GAINERS_MIN_24H_PCT. n=95 in soak, 55.8% WR.
- **Tier 3+** (narrative fit≥65, losers_contrarian mcap≤50M, etc.) are
  NOT included in the live-eligible filter — Tier 1+2 alone already
  oversubscribes the 20-slot cap.

Cap: PAPER_LIVE_ELIGIBLE_SLOTS (default 20). Approximate FCFS — if a
Tier-1/2 signal fires while N>=cap trades with would_be_live=1 are
already open, the new trade stamps would_be_live=0 (recorded for later
digest analysis but excluded from the "would-have-been-live" cohort).

Race note (PR-review IMPORTANT fold): the SELECT-then-INSERT is NOT
under the paper-trade lock, so concurrent opens during a burst can each
see (open_live_count < cap) and over-stamp by 1-2 rows briefly. This is
acceptable for purely observational use; the digest cohort will rarely
contain 21-22 rows instead of 20 under burst conditions. If/when live
trading routes through this filter, this code MUST be re-wrapped under
`db._txn_lock` so the cap is strict.

would_be_live=0 for any signal that does not match Tier 1 or 2, regardless
of slot availability. That's intentional — we want to see only the
quality subset, not a FCFS-20 cap on the firehose.
"""

from __future__ import annotations

from typing import Any

from scout.config import Settings


def matches_tier_1_or_2(
    signal_type: str,
    signal_data: dict[str, Any],
    conviction_stack: int,
    settings: Settings,
) -> bool:
    """True if (signal_type, signal_data, conviction_stack) clears the
    Tier-1 or Tier-2 live-eligibility filter.

    Falsy-safe: missing keys in signal_data → 0 / "" / None.
    """
    # Tier 1a: chain_completed (any pattern) — strongest cohort.
    if signal_type == "chain_completed":
        return True
    # Tier 1b: conviction stack >= 3 (BL-067 trigger).
    if conviction_stack >= 3:
        return True
    # Tier 2a: volume_spike at the current 5x+ threshold (any spike_ratio
    # ≥ existing detector floor — no extra gate needed; volume_spike's
    # own detector already filters).
    if signal_type == "volume_spike":
        return True
    # Tier 2b: gainers_early with mcap + 24h gates.
    if signal_type == "gainers_early":
        try:
            mcap = float(signal_data.get("mcap") or 0)
            chg24 = float(signal_data.get("price_change_24h") or 0)
        except (TypeError, ValueError):
            return False
        if (
            mcap >= settings.PAPER_TIER2_GAINERS_MIN_MCAP_USD
            and chg24 >= settings.PAPER_TIER2_GAINERS_MIN_24H_PCT
        ):
            return True
    return False


async def compute_would_be_live(
    db,
    *,
    signal_type: str,
    signal_data: dict[str, Any],
    conviction_stack: int,
    settings: Settings,
) -> int:
    """Return 1 if the new trade should be marked live-eligible, else 0.

    Two conditions BOTH required for 1:
      1. (signal_type, signal_data, conviction_stack) clears Tier 1 or 2.
      2. Current open trades with would_be_live=1 < PAPER_LIVE_ELIGIBLE_SLOTS.

    Defensive: returns 0 on any DB error (fail-closed; never blocks the
    paper-trade open).
    """
    if not matches_tier_1_or_2(signal_type, signal_data, conviction_stack, settings):
        return 0
    if db._conn is None:
        return 0
    try:
        cap = settings.PAPER_LIVE_ELIGIBLE_SLOTS
        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM paper_trades "
            "WHERE would_be_live = 1 AND status = 'open'"
        )
        row = await cur.fetchone()
        open_live_count = (row[0] if row else 0) or 0
        if open_live_count >= cap:
            return 0
        return 1
    except Exception:
        # Fail-closed: any query failure → not live-eligible. Paper trade
        # still opens; just no eligibility stamp.
        return 0
