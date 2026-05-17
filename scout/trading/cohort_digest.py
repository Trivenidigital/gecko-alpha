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

import secrets
from datetime import date, datetime, timedelta, timezone

import aiohttp
import structlog

from scout import alerter
from scout.config import Settings
from scout.db import Database
from scout.trading.weekly_digest import _TG_SPLIT_LIMIT, _split_for_telegram

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


def _detect_verdict_flip(
    *,
    current: dict[str, dict],
    previous: dict[str, dict],
    n_gate: int,
) -> list[tuple[str, str, str]]:
    """Detect week-over-week verdict-label flips. Returns
    [(signal_type, prev_verdict, curr_verdict), ...].

    Excludes transitions where either side is INSUFFICIENT_DATA* or
    near-identical (n-rate / structural, not regime change).

    Inputs: {signal_type: {"verdict": str, "eN": int}} for each week.
    """
    flips: list[tuple[str, str, str]] = []
    structural_labels = ("near-identical",)
    for signal_type, curr in current.items():
        prev = previous.get(signal_type)
        if prev is None:
            continue
        curr_verdict = curr["verdict"]
        prev_verdict = prev["verdict"]
        if curr_verdict == prev_verdict:
            continue
        # Either side INSUFFICIENT_DATA → n-rate, not flip.
        if curr_verdict.startswith("INSUFFICIENT_DATA") or prev_verdict.startswith(
            "INSUFFICIENT_DATA"
        ):
            continue
        if curr_verdict in structural_labels or prev_verdict in structural_labels:
            continue
        # n-gate guard: both weeks must have qualified for a verdict.
        if curr.get("eN", 0) < n_gate or prev.get("eN", 0) < n_gate:
            continue
        flips.append((signal_type, prev_verdict, curr_verdict))
    return flips


def _format_signal_block(signal_type: str, stats: dict, verdict: str) -> list[str]:
    """Vertical mobile-readable block for one signal."""
    lines = [f"[{signal_type}]"]
    if verdict == "near-identical":
        lines.append(
            "  near-identical (Tier 1a — eligible ≈ full by construction; verdict not informative)"
        )
        return lines
    eN = stats["eN"]
    fN = stats["fN"]
    eWr = stats["eWr"]
    fWr = stats["fWr"]
    ePnl = stats["ePnl"]
    fPnl = stats["fPnl"]
    wrDelta = stats["wrDelta"]
    e_wr_str = f"{eWr:.1f}%" if eWr is not None else "n/a"
    delta_str = f"{wrDelta:+.1f}pp" if wrDelta is not None else "n/a"
    signFlip = (fPnl > 0 and ePnl < 0) or (fPnl < 0 and ePnl > 0)
    flip_str = "YES" if signFlip else "no"
    lines.append(
        f"  eligible: n={eN}, wr={e_wr_str}, pnl=${ePnl:+.0f}"
    )
    lines.append(
        f"  full:     n={fN}, wr={fWr:.1f}%, pnl=${fPnl:+.0f}"
    )
    lines.append(f"  Δwr={delta_str}, signFlip={flip_str} → {verdict}")
    return lines


def _classify_all(
    stats_by_signal: dict[str, dict], *, settings: Settings
) -> dict[str, dict]:
    """Helper: classify each signal in `stats_by_signal`. Returns
    {signal_type: {"verdict": str, "eN": int}}."""
    return {
        signal_type: {
            "verdict": _classify_verdict(
                eN=stats["eN"],
                fN=stats["fN"],
                wrDelta=stats["wrDelta"],
                fPnl=stats["fPnl"],
                ePnl=stats["ePnl"],
                signal_type=signal_type,
                n_gate=settings.COHORT_DIGEST_N_GATE,
                strong_wr_gap_pp=settings.COHORT_DIGEST_STRONG_WR_GAP_PP,
                strong_pnl_floor_usd=settings.COHORT_DIGEST_STRONG_PNL_FLOOR_USD,
                moderate_wr_gap_pp=settings.COHORT_DIGEST_MODERATE_WR_GAP_PP,
            ),
            "eN": stats["eN"],
        }
        for signal_type, stats in stats_by_signal.items()
    }


def _build_final_block(
    current_verdicts: dict[str, dict]
) -> list[str]:
    """V28 SHOULD-FIX final-window decision-recommendation block.
    Softened wording — BL-055 live-trading is the gating dependency."""
    by_label: dict[str, list[str]] = {
        "strong-pattern (exploratory)": [],
        "moderate": [],
        "tracking": [],
        "near-identical": [],
    }
    for signal_type, info in current_verdicts.items():
        v = info["verdict"]
        if v.startswith("INSUFFICIENT_DATA"):
            continue
        by_label.setdefault(v, []).append(signal_type)
    out = ["", "=== 4-week decision point (2026-06-08 anchor) ==="]
    if by_label["strong-pattern (exploratory)"]:
        out.append(
            "Strong-pattern signals (exploratory): "
            + ", ".join(by_label["strong-pattern (exploratory)"])
        )
        out.append(
            "  → Recommend operator review for live-promotion candidacy."
        )
        out.append(
            "    (BL-055 live-trading unlock is the gating dependency; "
            "this is a pre-approval signal, not an auto-promote.)"
        )
    if by_label["moderate"]:
        out.append("Moderate signals: " + ", ".join(by_label["moderate"]))
        out.append(
            "  → Continue paper soak; re-evaluate at +4w."
        )
    if by_label["tracking"]:
        out.append("Tracking signals: " + ", ".join(by_label["tracking"]))
        out.append("  → No regime change observed. Continue as-is.")
    if by_label["near-identical"]:
        out.append(
            "Near-identical / Excluded: "
            + ", ".join(by_label["near-identical"])
        )
        out.append("  → Structural — verdict not informative.")
    return out


async def build_cohort_digest(
    db: Database, end_date: date, settings: Settings
) -> str | None:
    """Build the cohort-digest text. Returns None if no activity in
    week N AND week N-1 across all enumerated types.

    end_date is the day the digest fires (exclusive upper bound for the
    most recent window). Two windows are read:
      Window N:    [end_date - 7d,  end_date)
      Window N-1:  [end_date - 14d, end_date - 7d)
    """
    end_n = datetime.combine(end_date, datetime.min.time(), tzinfo=timezone.utc)
    start_n = end_n - timedelta(days=7)
    start_n_minus_1 = end_n - timedelta(days=14)

    curr_stats = await _compute_all_cohorts_stats(db, start=start_n, end=end_n)
    prev_stats = await _compute_all_cohorts_stats(
        db, start=start_n_minus_1, end=start_n
    )

    # Activity check: any non-zero fN in either window?
    activity = any(
        s["fN"] > 0 for s in curr_stats.values()
    ) or any(s["fN"] > 0 for s in prev_stats.values())
    if not activity:
        log.info("cohort_digest_empty", end_date=end_date.isoformat())
        return None

    curr_verdicts = _classify_all(curr_stats, settings=settings)
    prev_verdicts = _classify_all(prev_stats, settings=settings)

    flips = _detect_verdict_flip(
        current=curr_verdicts,
        previous=prev_verdicts,
        n_gate=settings.COHORT_DIGEST_N_GATE,
    )

    lines: list[str] = []
    lines.append(
        f"Cohort Digest — Week of {start_n.date().isoformat()} → "
        f"{(end_n - timedelta(seconds=1)).date().isoformat()}"
    )
    lines.append(f"n-gate: eN ≥ {settings.COHORT_DIGEST_N_GATE} for a verdict.")
    lines.append("")

    for signal_type in _LIVE_ELIGIBLE_ENUMERATED_TYPES:
        stats = curr_stats[signal_type]
        verdict = curr_verdicts[signal_type]["verdict"]
        lines.extend(_format_signal_block(signal_type, stats, verdict))
        lines.append("")

    if flips:
        flip_strs = [
            f"{sig} ({prev} → {curr})" for sig, prev, curr in flips
        ]
        lines.append("⚠ FLIPS THIS WEEK: " + ", ".join(flip_strs))
        lines.append("")
        # V28 SHOULD-FIX rolled-up event: ONE WARNING per digest.
        log.warning("cohort_digest_verdict_flip", flips=flip_strs)

    lines.append(
        f"Window: 4w toward {settings.COHORT_DIGEST_FINAL_DATE.isoformat()} decision point"
    )

    # V28 SHOULD-FIX final-block fallback: fires on first eligible run with
    # end_date >= COHORT_DIGEST_FINAL_DATE AND not already fired.
    state = await db.cohort_digest_read_state()
    if (
        end_date >= settings.COHORT_DIGEST_FINAL_DATE
        and state.get("last_final_block_fired_at") is None
    ):
        lines.extend(_build_final_block(curr_verdicts))
        # Defer the stamp until AFTER successful TG dispatch — see
        # send_cohort_digest.
        # (We mark the digest as "final-block included" via a flag the
        # caller can read; simplest implementation is a sentinel line that
        # send_cohort_digest detects.)
        lines.append("__FINAL_BLOCK_INCLUDED__")

    return "\n".join(lines)


async def send_cohort_digest(db: Database, settings: Settings) -> None:
    """Orchestrator: build + send via alerter. Never silent on error.

    Mirrors scout.trading.weekly_digest.send_weekly_digest:311 shape —
    opens one aiohttp.ClientSession, chunks via _split_for_telegram,
    stamps state AFTER successful dispatch (so TG failure does NOT
    silently advance the flag).
    """
    if not settings.COHORT_DIGEST_ENABLED:
        log.debug("cohort_digest_skipped_disabled")  # observability symmetry
        return

    corr = f"cd-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{secrets.token_hex(2)}"
    today = date.today()
    async with aiohttp.ClientSession() as session:
        try:
            text = await build_cohort_digest(db, today, settings)
            if text is None:
                log.info("cohort_digest_empty_skipped")
                return

            includes_final_block = "__FINAL_BLOCK_INCLUDED__" in text
            text = text.replace("\n__FINAL_BLOCK_INCLUDED__", "").replace(
                "__FINAL_BLOCK_INCLUDED__", ""
            )

            chunks = _split_for_telegram(text, _TG_SPLIT_LIMIT)
            if not chunks:
                log.error("cohort_digest_produced_no_chunks", text_len=len(text))
                return

            for chunk in chunks:
                await alerter.send_telegram_message(
                    chunk, session, settings, parse_mode=None
                )
            log.info("cohort_digest_sent", bytes=len(text))

            # Stamp ONLY after every chunk dispatched successfully.
            await db.cohort_digest_stamp_last_digest_date(today.isoformat())
            if includes_final_block:
                await db.cohort_digest_stamp_final_block_fired(
                    datetime.now(timezone.utc).isoformat()
                )
        except Exception as e:
            log.exception("cohort_digest_failed", corr=corr)
            try:
                await alerter.send_telegram_message(
                    f"Cohort digest failed: {type(e).__name__} [ref={corr}]. Check logs.",
                    session,
                    settings,
                    parse_mode=None,
                )
            except Exception:
                log.exception(
                    "cohort_digest_fallback_dispatch_error", corr=corr
                )
