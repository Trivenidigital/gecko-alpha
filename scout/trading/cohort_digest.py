"""Cohort digest builder — weekly would_be_live vs full-cohort comparison.

BL-NEW-LIVE-ELIGIBLE-WEEKLY-DIGEST (cycle 5). Verdict-classification rule
mirrors dashboard/frontend/components/TradingTab.jsx:389-425 verbatim
so the digest and dashboard cannot diverge — both retune in lockstep via
.env override + restart.

Per memory `project_dashboard_cohort_view_shipped_2026_05_12.md` the
dashboard cohort-view is decision-locked at writer-deployment + 28d =
2026-06-08. The cohort digest carries the FINAL decision-recommendation
block on the first weekly run with end_date >= 2026-06-08.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import structlog

from scout.config import Settings
from scout.db import Database

log = structlog.get_logger()


_NEAR_IDENTICAL_COHORTS = ("chain_completed",)

# Mirrors dashboard/db.py:1097. The only signals that meaningfully stack
# to >=3 (Tier 1a+) — every other signal has structurally-empty eligible
# cohort and gets surfaced in an "excluded" footer rather than verdicted.
_LIVE_ELIGIBLE_ENUMERATED_TYPES = (
    "chain_completed",
    "volume_spike",
    "gainers_early",
)


def _classify_verdict(
    *,
    eN: int,
    fN: int,
    wrDelta: float | None,
    fPnl: float,
    ePnl: float,
    signal_type: str,
    n_gate: int,
    strong_wr_gap_pp: float = 15.0,
    strong_pnl_floor_usd: float = 200.0,
    moderate_wr_gap_pp: float = 5.0,
) -> str:
    """Classify a signal's cohort comparison. Mirrors
    dashboard/frontend/components/TradingTab.jsx:389-425 verbatim.

    Inputs:
        eN, fN: eligible / full cohort counts
        wrDelta: eWr - fWr in pp; None when eN == 0
        fPnl, ePnl: total pnl_usd in each cohort
        signal_type: for near-identical carve-out
        n_gate: floor for non-INSUFFICIENT_DATA verdicts
        strong_wr_gap_pp / strong_pnl_floor_usd / moderate_wr_gap_pp:
            dashboard-equivalent thresholds (settable via Settings)
    """
    if signal_type in _NEAR_IDENTICAL_COHORTS:
        return "near-identical"
    if eN == 0:
        return "INSUFFICIENT_DATA (n=0)"
    if eN < n_gate:
        return f"INSUFFICIENT_DATA (n={eN}, need >={n_gate})"

    signFlipRaw = (fPnl > 0 and ePnl < 0) or (fPnl < 0 and ePnl > 0)
    strongPattern = (
        signFlipRaw
        and abs(fPnl) >= strong_pnl_floor_usd
        and abs(ePnl) >= strong_pnl_floor_usd
        and wrDelta is not None
        and abs(wrDelta) > strong_wr_gap_pp  # STRICT > per dashboard
    )
    if strongPattern:
        return "strong-pattern (exploratory)"
    if signFlipRaw or (
        wrDelta is not None and abs(wrDelta) > moderate_wr_gap_pp
    ):
        return "moderate"
    return "tracking"


async def _compute_all_cohorts_stats(
    db: Database, *, start: datetime, end: datetime
) -> dict[str, dict]:
    """One SQL pair (full + eligible) for all enumerated types in the window.

    Returns {signal_type: stats_dict} where stats_dict carries fN, fWins,
    fPnl, fWr, eN, eWins, ePnl, eWr, wrDelta. signal_types absent from
    paper_trades in this window get default zero/None values so every
    enumerated type appears in the result.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    start_iso = start.isoformat()
    end_iso = end.isoformat()
    placeholders = ",".join(["?"] * len(_LIVE_ELIGIBLE_ENUMERATED_TYPES))

    # Full cohort — mirrors dashboard/db.py:1148-1167 verbatim (status, pnl_usd, closed_at).
    full_q = (
        f"""SELECT signal_type, COUNT(*) AS n,
                  SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                  COALESCE(SUM(pnl_usd), 0) AS pnl
             FROM paper_trades
            WHERE status != 'open'
              AND closed_at >= ?
              AND closed_at < ?
              AND signal_type IN ({placeholders})
            GROUP BY signal_type"""
    )
    cur = await db._conn.execute(
        full_q, (start_iso, end_iso, *_LIVE_ELIGIBLE_ENUMERATED_TYPES)
    )
    full_rows = await cur.fetchall()

    eligible_q = (
        f"""SELECT signal_type, COUNT(*) AS n,
                  SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                  COALESCE(SUM(pnl_usd), 0) AS pnl
             FROM paper_trades
            WHERE status != 'open'
              AND closed_at >= ?
              AND closed_at < ?
              AND signal_type IN ({placeholders})
              AND would_be_live = 1
            GROUP BY signal_type"""
    )
    cur = await db._conn.execute(
        eligible_q, (start_iso, end_iso, *_LIVE_ELIGIBLE_ENUMERATED_TYPES)
    )
    eligible_rows = await cur.fetchall()

    def _row_to_dict(row):
        n = int(row[1] or 0)
        wins = int(row[2] or 0)
        pnl = float(row[3] or 0)
        wr = (wins / n * 100.0) if n > 0 else 0.0
        return n, wins, pnl, wr

    full_by_signal = {row[0]: _row_to_dict(row) for row in full_rows}
    elig_by_signal = {row[0]: _row_to_dict(row) for row in eligible_rows}

    result: dict[str, dict] = {}
    for signal_type in _LIVE_ELIGIBLE_ENUMERATED_TYPES:
        fN, fWins, fPnl, fWr = full_by_signal.get(signal_type, (0, 0, 0.0, 0.0))
        eN, eWins, ePnl, eWr_raw = elig_by_signal.get(
            signal_type, (0, 0, 0.0, 0.0)
        )
        eWr = eWr_raw if eN > 0 else None
        wrDelta = (eWr - fWr) if eWr is not None else None
        result[signal_type] = {
            "fN": fN,
            "fWins": fWins,
            "fPnl": fPnl,
            "fWr": fWr,
            "eN": eN,
            "eWins": eWins,
            "ePnl": ePnl,
            "eWr": eWr,
            "wrDelta": wrDelta,
        }
    return result


async def _compute_signal_cohort_stats(
    db: Database, *, signal_type: str, start: datetime, end: datetime
) -> dict:
    """Per-signal stats (test-only seam — production path uses
    `_compute_all_cohorts_stats`).
    """
    all_stats = await _compute_all_cohorts_stats(db, start=start, end=end)
    if signal_type in all_stats:
        return all_stats[signal_type]
    # Signal not in enumerated types — synthesize empty result for tests
    return {
        "fN": 0, "fWins": 0, "fPnl": 0.0, "fWr": 0.0,
        "eN": 0, "eWins": 0, "ePnl": 0.0, "eWr": None, "wrDelta": None,
    }
