#!/usr/bin/env python3
"""ALR-02 VALIDATE — read-only backtest of the detection-time alert lane.

Replays the detection-lane trigger over the last N days of `candidates`,
using each candidate's own `first_seen_at` as the historical "now", and
reports how many would have fired and WHEN relative to their CG trending
crossover — the "would it have fired pre-run on an ANSEM-class monster?"
question from backlog ALR-02.

Trigger replay (mirrors scout/trading/detection_alert._detection_trigger via
engine._compute_lead_time_vs_trending's approach):
  crossed_at = MIN(trending_snapshots.snapshot_at) for the coin.
  - crossed_at is NULL              -> would fire (no_reference; never trended
                                       inside the window).
  - crossed_at  > first_seen_at     -> would fire (ahead_of_crossover; we were
                                       early by (crossed_at - first_seen) min).
  - crossed_at <= first_seen_at     -> would NOT fire (already trending / late).
Universe-filtered ids (default `-tokenized-` substring) are excluded, matching
the enabled lane.

HARD LIMITATION (stated up front): `trending_snapshots` and the gainers/price
history have only ~7-day retention (backlog DASH-05). So an early catch is only
reconstructible when BOTH the candidate's first_seen and its trending crossover
fall inside the window. For older monsters the crossover reference has been
pruned, so this backtest UNDER-counts early catches and CANNOT reconstruct
peak-gain attribution. A positive result is real; a null result may be
retention-blinded. Forward soak (lane on, small cap) is the honest catch-quality
measure.

Read-only; synchronous sqlite3 (runs anywhere — no aiohttp/async deps).

Usage:
    uv run python scripts/detection_lane_backtest.py --db scout.db [--days 7] [--json]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from statistics import median

# Mirror Settings.ALERT_UNIVERSE_EXCLUDE_ID_PATTERNS default.
_UNIVERSE_PATTERNS = ("-tokenized-",)
# Mirror Settings.DETECTION_ALERT_MAX_PER_DAY default (for the throttle view).
_DEFAULT_CAP_PER_DAY = 5


def _parse_ts(raw: str | None) -> datetime | None:
    """Tolerant ISO parse (handles 'T'/space separator, naive/aware)."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace(" ", "T", 1))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _universe_excluded(token_id: str) -> bool:
    lowered = token_id.lower()
    return any(p in lowered for p in _UNIVERSE_PATTERNS)


def backtest(db_path: str, days: int, cap_per_day: int) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT c.contract_address AS token_id,
                   c.ticker           AS ticker,
                   c.first_seen_at    AS first_seen_at,
                   (SELECT MIN(ts.snapshot_at)
                      FROM trending_snapshots ts
                     WHERE ts.coin_id = c.contract_address) AS crossed_at
              FROM candidates c
             WHERE c.chain = 'coingecko'
               AND datetime(c.first_seen_at) >= datetime('now', ?)
            """,
            (f"-{int(days)} days",),
        ).fetchall()
    finally:
        conn.close()

    total_cg = len(rows)
    excluded_universe = 0
    no_reference = 0
    ahead_of_crossover = 0
    already_trending = 0
    unparseable = 0
    lead_minutes: list[float] = []
    fired_by_day: dict[str, int] = {}
    fired_examples: list[dict] = []

    for r in rows:
        token_id = r["token_id"]
        if _universe_excluded(token_id):
            excluded_universe += 1
            continue
        first_seen = _parse_ts(r["first_seen_at"])
        if first_seen is None:
            unparseable += 1
            continue
        crossed = _parse_ts(r["crossed_at"])

        would_fire = False
        kind = None
        lead = None
        if crossed is None:
            would_fire = True
            kind = "no_reference"
            no_reference += 1
        else:
            delta_min = (first_seen - crossed).total_seconds() / 60.0
            if delta_min < 0:
                would_fire = True
                kind = "ahead_of_crossover"
                lead = abs(delta_min)  # minutes we beat the crossover by
                ahead_of_crossover += 1
                lead_minutes.append(lead)
            else:
                already_trending += 1

        if would_fire:
            day = first_seen.date().isoformat()
            fired_by_day[day] = fired_by_day.get(day, 0) + 1
            if len(fired_examples) < 15:
                fired_examples.append(
                    {
                        "token_id": token_id,
                        "ticker": r["ticker"],
                        "first_seen_at": r["first_seen_at"],
                        "kind": kind,
                        "beat_trending_by_min": round(lead, 1) if lead else None,
                    }
                )

    would_fire_total = no_reference + ahead_of_crossover
    # How the daily cap would throttle the raw fire count.
    fired_after_cap = sum(min(cap_per_day, n) for n in fired_by_day.values())

    return {
        "db": db_path,
        "window_days": days,
        "cap_per_day": cap_per_day,
        "total_cg_candidates": total_cg,
        "excluded_universe_filter": excluded_universe,
        "unparseable_first_seen": unparseable,
        "would_fire_total": would_fire_total,
        "would_fire_no_reference": no_reference,
        "would_fire_ahead_of_crossover": ahead_of_crossover,
        "not_early_already_trending": already_trending,
        "caught_early_later_trended": ahead_of_crossover,
        "median_lead_minutes_ahead_set": (
            round(median(lead_minutes), 1) if lead_minutes else None
        ),
        "fired_per_day": dict(sorted(fired_by_day.items())),
        "fired_after_daily_cap": fired_after_cap,
        "fired_examples": fired_examples,
    }


def _print_report(res: dict) -> None:
    print("=" * 68)
    print("ALR-02 detection-lane backtest (read-only)")
    print("=" * 68)
    print(f"db                      : {res['db']}")
    print(f"window                  : last {res['window_days']} days")
    print(f"daily cap (throttle sim): {res['cap_per_day']}/day")
    print("-" * 68)
    print(f"CG candidates in window : {res['total_cg_candidates']}")
    print(f"  excluded by universe  : {res['excluded_universe_filter']}")
    print(f"  unparseable first_seen: {res['unparseable_first_seen']}")
    print("-" * 68)
    print(f"WOULD FIRE (total)      : {res['would_fire_total']}")
    print(f"  no trending reference : {res['would_fire_no_reference']}")
    print(f"  ahead of crossover    : {res['would_fire_ahead_of_crossover']}")
    print(f"not early (already trend): {res['not_early_already_trending']}")
    print("-" * 68)
    print(
        "caught early (later trended): "
        f"{res['caught_early_later_trended']} "
        f"(median lead {res['median_lead_minutes_ahead_set']} min)"
    )
    print(f"after {res['cap_per_day']}/day cap        : {res['fired_after_daily_cap']}")
    if res["fired_per_day"]:
        print("fired per day           :")
        for day, n in res["fired_per_day"].items():
            print(f"    {day}: {n}")
    if res["fired_examples"]:
        print("-" * 68)
        print("sample would-fire candidates:")
        for ex in res["fired_examples"]:
            beat = ex["beat_trending_by_min"]
            beat_s = f" (beat trending by {beat} min)" if beat is not None else ""
            print(
                f"    {ex['ticker'] or '?':<8} {ex['token_id']:<28} "
                f"{ex['kind']}{beat_s}"
            )
    print("=" * 68)
    print(
        "LIMITATION: trending/gainers history is ~7-day retention (DASH-05); "
        "early catches whose crossover predates the window are UNDER-counted, "
        "and peak-gain attribution is NOT reconstructible here. A null result "
        "may be retention-blinded - confirm catch quality with a forward soak."
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="scout.db", help="path to scout.db")
    ap.add_argument("--days", type=int, default=7, help="lookback window (days)")
    ap.add_argument(
        "--cap",
        type=int,
        default=_DEFAULT_CAP_PER_DAY,
        help="daily cap for the throttle simulation",
    )
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    args = ap.parse_args()

    res = backtest(args.db, args.days, args.cap)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        _print_report(res)


if __name__ == "__main__":
    main()
