"""BL-NEW-LIVE-HYBRID M1 v2.1: operator-in-loop threshold evaluation.

Pre-registered thresholds per design v2.1 §"Operator-in-loop scaling rules":
  1. new-venue gate: < 30 successful autonomous fills on this (signal × venue)
  2. trade-size gate: > 2× median trade size for this (signal × venue)
  3. venue-health gate: any caution-range metric in past 24h on the venue
  4. operator-set /approval-required flag (via Telegram command, 24h expiry)

ALL FOUR FALSE → trade auto-executes (autonomous).
ANY ONE TRUE → trade requires operator approval via Telegram (Task 13).

Thresholds (30, 2×, 24h) are pre-registered and NOT runtime-tunable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

from scout.db import Database

log = structlog.get_logger(__name__)

NEW_VENUE_FILL_THRESHOLD = 30  # pre-registered; not runtime-tunable
TRADE_SIZE_MEDIAN_MULTIPLIER = 2.0  # pre-registered
VENUE_HEALTH_LOOKBACK_HOURS = 24  # pre-registered
RATE_LIMIT_CAUTION_PCT = 30.0  # below this is "caution range"


async def should_require_approval(
    *,
    db: Database,
    settings,
    signal_type: str,
    venue: str,
    size_usd: float,
) -> tuple[bool, str | None]:
    """Returns (require_approval, gate_name_if_required).

    gate_name is one of: 'new_venue_gate', 'trade_size_gate',
    'venue_health_gate', 'operator_flag', or None when no gate fires.

    Gates evaluate in fixed order; first to fire short-circuits the
    rest. Order is intentional — new-venue is the cheapest query,
    operator-flag the most explicit.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    # Gate 1: new-venue (< 30 consecutive no-correction fills)
    cur = await db._conn.execute(
        "SELECT consecutive_no_correction FROM signal_venue_correction_count "
        "WHERE signal_type = ? AND venue = ?",
        (signal_type, venue),
    )
    row = await cur.fetchone()
    fills = row[0] if row else 0
    if fills < NEW_VENUE_FILL_THRESHOLD:
        log.info(
            "approval_required_new_venue_gate",
            signal_type=signal_type,
            venue=venue,
            fills=fills,
        )
        return True, "new_venue_gate"

    # Gate 2: trade-size (> 2× median of last 30 closed live trades)
    cur = await db._conn.execute(
        """SELECT CAST(size_usd AS REAL) FROM live_trades
           WHERE signal_type = ? AND venue = ? AND status LIKE 'closed%'
           ORDER BY created_at DESC LIMIT 30""",
        (signal_type, venue),
    )
    sizes = [row[0] for row in await cur.fetchall()]
    if sizes:
        sizes_sorted = sorted(sizes)
        median = sizes_sorted[len(sizes_sorted) // 2]
        if size_usd > TRADE_SIZE_MEDIAN_MULTIPLIER * median:
            log.info(
                "approval_required_trade_size_gate",
                signal_type=signal_type,
                venue=venue,
                size_usd=size_usd,
                median=median,
            )
            return True, "trade_size_gate"

    # Gate 3: venue-health (any caution-range metric in past 24h)
    lookback_iso = (
        datetime.now(timezone.utc) - timedelta(hours=VENUE_HEALTH_LOOKBACK_HOURS)
    ).isoformat()
    cur = await db._conn.execute(
        """SELECT auth_ok, rest_responsive, rate_limit_headroom_pct
           FROM venue_health
           WHERE venue = ? AND probe_at >= ?
           ORDER BY probe_at DESC LIMIT 30""",
        (venue, lookback_iso),
    )
    for auth_ok, rest_resp, headroom in await cur.fetchall():
        caution = headroom is not None and headroom < RATE_LIMIT_CAUTION_PCT
        if not auth_ok or not rest_resp or caution:
            log.info(
                "approval_required_venue_health_gate",
                venue=venue,
                auth_ok=auth_ok,
                rest_responsive=rest_resp,
                rate_limit_headroom_pct=headroom,
            )
            return True, "venue_health_gate"

    # Gate 4: operator /approval-required flag (24h-ephemeral row)
    cur = await db._conn.execute(
        """SELECT 1 FROM live_operator_overrides
           WHERE override_type = 'approval_required'
             AND (venue = ? OR venue IS NULL)
             AND expires_at > ?
           LIMIT 1""",
        (venue, datetime.now(timezone.utc).isoformat()),
    )
    if await cur.fetchone() is not None:
        log.info("approval_required_operator_flag", venue=venue)
        return True, "operator_flag"

    return False, None
