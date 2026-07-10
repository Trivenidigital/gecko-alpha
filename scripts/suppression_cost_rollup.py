#!/usr/bin/env python3
"""Weekly suppression-cost rollup over the #421 dispatcher-layer recall lane.

Dispatcher-layer suppression (scout/trading/signals.py should_open ->
reason='suppressed') is the dominant winner-killer and is opaque to the
operator. PR #421 records every suppressed block AT EMISSION into
``signal_outcome_ledger`` as a ``gated_out_sample`` row tagged
``gate_verdicts.reason='suppressed'`` + ``source_layer='dispatcher'``, and the
hourly labeler (scout/outcome_ledger.py :func:`label_pending`) resolves forward
returns from IN-DB prices. This script answers the operator's weekly one-liner:
"what did suppression cost me?"

DEPLOY STATE (important): #421 is MERGED but NOT yet deployed to prod, so prod
today has ZERO dispatcher ``reason='suppressed'`` ledger rows — SAMPLING APPEARS
DEAD is the EXPECTED state until #421 ships. Do NOT schedule this on cron before
#421 deploys. (The 42 ``gated_out_sample`` rows currently on prod are the
engine's 1-in-N ``reason='signal_disabled'`` lane, which this script's cohort
filter deliberately excludes.)

Read-only (SELECTs only; never writes any table). Two blocks:

1. HEALTH (always computed first) — verifies the #421 lane is alive before any
   number is trusted:
     * window sampling: dispatcher-suppression ``gated_out_sample`` rows emitted
       in the last ``--window-days`` vs total ``reason='suppressed'`` blocks in
       ``trade_decision_events`` for the same window, with the sampling fraction
       and rows/day. Zero sampled rows while blocks exist -> SAMPLING APPEARS
       DEAD (expected pre-#421-deploy; a real outage once deployed). DEGRADED
       trips on a low sampling fraction OR a low rows/day floor — the fraction
       arm catches a fail-soft drop at post-deploy record-all volumes (~6,860
       rows/day) that a rows/day floor alone would miss.
     * maturation (lookback-scoped, NOT window-scoped, because r7d cannot
       mature inside a 7d window): how many rows have r24h / r7d labels resolved
       vs pending/unlabelable, reported at BOTH row and distinct-token grain.

2. COST (n-gated) — only when the matured (r7d-resolved) cohort meets
   ``MIN_SAMPLE`` DISTINCT TOKENS. #421 records EVERY suppressed block, so one
   repeated token (prod: skyai 1,834 rows/week) would inflate a naive per-row
   sum ~100-300x; the cohort is first collapsed to ONE row per token_id at its
   EARLIEST emission in the lookback (the honest "you could have traded it here"
   anchor). The ledger's label columns store forward RETURNS (``r7d =
   price_at_horizon_7d / price_at_emission - 1``; see scout/outcome_ledger.py
   label_pending / _price_at_or_after), so the basis is a buy-at-emission /
   mark-at-7d gross return with NO exit modeling (no TP/SL, no slippage):
   per-token counterfactual PnL = ``r7d * notional``. Below the floor the line
   reads INSUFFICIENT_DATA and NEVER a dollar number — the ledger is ~1 week old
   (born 2026-07-03) so the first meaningful weekly read is expected ~2026-07-31.

Config: CLI flags with env-var defaults (mirrors scripts/
source_call_coverage_watchdog.py). Optional Telegram send is off by default
behind ``--send`` (plain text, ``parse_mode=None``, with §12b
``*_alert_dispatched`` / ``*_alert_delivered`` logs around the call). The send
path lazily imports aiohttp + the alerter so the default/analysis path never
touches the network (and runs on Windows dev boxes).

Exit codes:
  0 — ok
  5 — SAMPLING APPEARS DEAD (expected until #421 deploys; a real outage after)
  1 — DB missing, runtime error, or (with --send) dispatch failure
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite
import structlog

_log = structlog.get_logger()

# The ledger was born 2026-07-03; MIN_SAMPLE matured (r7d) suppression rows are
# not expected before this date. Surfaced in the INSUFFICIENT_DATA line so the
# operator reads the gate as "too early", not "broken".
_FIRST_MEANINGFUL_READ = "2026-07-31"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _parse_verdicts(raw) -> tuple[dict, bool]:
    """Return ``(verdicts_dict, parse_failed)``.

    ``parse_failed`` is True when *raw* is NULL/empty or non-decodable to a JSON
    object. Without the flag such rows silently vanish from every count (S3-c):
    they are gated_out_sample rows we cannot attribute, so the caller tallies
    them into ``json_parse_failures`` instead of dropping them uncounted.
    """
    if raw is None or raw == "":
        return {}, True
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return {}, True
    if not isinstance(obj, dict):
        return {}, True
    return obj, False


async def analyze(
    db_path: str,
    *,
    window_days: int = 7,
    min_sample: int = 10,
    notional_usd: float = 1000.0,
    min_rows_per_day: float = 1.0,
    min_sampling_fraction: float = 0.5,
    lookback_days: int = 120,
    now: datetime | None = None,
) -> dict:
    """Compute the read-only health + cost rollup. Opens its own connection.

    All timestamp bounds are ISO strings from ``datetime.isoformat()`` compared
    lexicographically — the ledger's ``emitted_at`` and ``trade_decision_events``
    ``created_at`` are both written via ``datetime.now(timezone.utc).isoformat()``
    (same 'T'-separated UTC format), so string comparison is order-correct.
    """
    now = now or datetime.now(timezone.utc)
    window_start = (now - timedelta(days=window_days)).isoformat()
    lookback_start = (now - timedelta(days=lookback_days)).isoformat()

    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row

        cur = await conn.execute(
            "SELECT COUNT(*) FROM trade_decision_events "
            "WHERE reason = 'suppressed' AND created_at >= ?",
            (window_start,),
        )
        blocks_in_window = int((await cur.fetchone())[0])

        cur = await conn.execute(
            "SELECT token_id, surface, gate_verdicts, emitted_at, "
            "r24h, r7d, label_status "
            "FROM signal_outcome_ledger "
            "WHERE kind = 'gated_out_sample' AND emitted_at >= ? "
            "ORDER BY emitted_at DESC",
            (lookback_start,),
        )
        rows = await cur.fetchall()

    # Isolate the dispatcher-layer SUPPRESSION cohort. #421 tags these rows with
    # BOTH reason='suppressed' AND source_layer='dispatcher' (signals.py:79-84);
    # requiring both keeps a future lane that also emits reason='suppressed' from
    # polluting the cohort (S3-a). Rows with malformed/NULL gate_verdicts would
    # vanish uncounted, so they are tallied into json_parse_failures (S3-c).
    supp: list = []
    json_parse_failures = 0
    for r in rows:
        verdicts, failed = _parse_verdicts(r["gate_verdicts"])
        if failed:
            json_parse_failures += 1
            continue
        if (
            verdicts.get("reason") == "suppressed"
            and verdicts.get("source_layer") == "dispatcher"
        ):
            supp.append(r)

    sampled_in_window = sum(1 for r in supp if r["emitted_at"] >= window_start)
    distinct_in_window = len(
        {r["token_id"] for r in supp if r["emitted_at"] >= window_start}
    )

    # Row-level maturation counts (describe the raw record-all lane).
    n_r24h_rows = sum(1 for r in supp if r["r24h"] is not None)
    n_r7d_rows = sum(1 for r in supp if r["r7d"] is not None)
    n_pending = sum(1 for r in supp if r["label_status"] in ("pending", "partial"))
    n_unlabelable = sum(1 for r in supp if r["label_status"] == "unlabelable")

    # Collapse to ONE row per token before any aggregation (S1). #421 records
    # EVERY suppressed block, so a single repeated token (prod: skyai 1,834
    # rows/week) would otherwise dominate the sum ~100-300x. Anchor on the
    # token's FIRST (earliest) emission in the lookback — the honest
    # "you could have traded it here" entry.
    earliest_by_token: dict = {}
    for r in supp:
        prev = earliest_by_token.get(r["token_id"])
        if prev is None or r["emitted_at"] < prev["emitted_at"]:
            earliest_by_token[r["token_id"]] = r
    distinct_tokens_lookback = len(earliest_by_token)
    matured_tokens = [r for r in earliest_by_token.values() if r["r7d"] is not None]
    n_distinct_matured = len(matured_tokens)

    rows_per_day = sampled_in_window / window_days if window_days > 0 else 0.0
    sampling_fraction = (
        sampled_in_window / blocks_in_window if blocks_in_window > 0 else None
    )
    sampling_dead = blocks_in_window > 0 and sampled_in_window == 0
    # DEGRADED trips on EITHER a low sampling fraction (catches a fail-soft drop
    # at post-deploy record-all volumes where rows/day stays high) OR a low
    # rows/day floor (catches the low-absolute-volume case) — S3-b.
    degraded_reasons: list = []
    if blocks_in_window > 0 and sampled_in_window > 0:
        if sampling_fraction is not None and sampling_fraction < min_sampling_fraction:
            degraded_reasons.append(f"fraction<{min_sampling_fraction:g}")
        if rows_per_day < min_rows_per_day:
            degraded_reasons.append(f"rows/day<{min_rows_per_day:g}")

    health = {
        "sampled_in_window": sampled_in_window,
        "distinct_tokens_in_window": distinct_in_window,
        "suppressed_blocks_in_window": blocks_in_window,
        "sampling_fraction": sampling_fraction,
        "rows_per_day": rows_per_day,
        "sampling_dead": sampling_dead,
        "sampling_degraded": bool(degraded_reasons),
        "degraded_reasons": degraded_reasons,
        "min_sampling_fraction": min_sampling_fraction,
        "min_rows_per_day": min_rows_per_day,
        "total_sampled_rows_lookback": len(supp),
        "distinct_tokens_lookback": distinct_tokens_lookback,
        "matured_r24h_rows": n_r24h_rows,
        "matured_r7d_rows": n_r7d_rows,
        "distinct_tokens_matured": n_distinct_matured,
        "pending": n_pending,
        "unlabelable": n_unlabelable,
        "json_parse_failures": json_parse_failures,
    }

    cost: dict = {
        "gated": n_distinct_matured < min_sample,
        "n_distinct_tokens": n_distinct_matured,
        "n_matured_rows": n_r7d_rows,
        "min_sample": min_sample,
        "notional_usd": notional_usd,
        "est_pnl_usd": None,
        "mean_return": None,
        "win_rate": None,
        "wins": 0,
        "top_movers": [],
    }
    if n_distinct_matured >= min_sample:
        returns = [float(r["r7d"]) for r in matured_tokens]
        wins = sum(1 for x in returns if x > 0)
        cost["est_pnl_usd"] = sum(x * notional_usd for x in returns)
        cost["mean_return"] = sum(returns) / len(returns)
        cost["wins"] = wins
        cost["win_rate"] = 100.0 * wins / len(returns)
        # matured_tokens is already one-per-token, so top movers are unique.
        top = sorted(matured_tokens, key=lambda r: float(r["r7d"]), reverse=True)[:5]
        cost["top_movers"] = [
            {
                "token_id": r["token_id"],
                "signal_type": r["surface"],
                "r7d": float(r["r7d"]),
            }
            for r in top
        ]

    return {
        "now": now.isoformat(),
        "window_days": window_days,
        "lookback_days": lookback_days,
        "health": health,
        "cost": cost,
    }


def format_summary(result: dict) -> str:
    """Compact plain-text summary (<= 6 lines) for stdout / Telegram."""
    h = result["health"]
    c = result["cost"]
    w = result["window_days"]
    lines = [f"[suppression-cost-rollup] window={w}d as-of {result['now']}"]

    if h["sampling_dead"]:
        lines.append(
            f"HEALTH: SAMPLING APPEARS DEAD - 0 dispatcher-suppression rows vs "
            f"{h['suppressed_blocks_in_window']} suppressed blocks in {w}d "
            f"(EXPECTED until #421 deploys - merged, not deployed; "
            f"do not schedule cron before deploy)"
        )
    elif h["suppressed_blocks_in_window"] == 0:
        lines.append(
            f"HEALTH: no suppressed blocks in {w}d window; "
            f"{h['sampled_in_window']} sampled ({h['rows_per_day']:.1f}/day)"
        )
    else:
        deg = (
            f" DEGRADED[{','.join(h['degraded_reasons'])}]"
            if h["sampling_degraded"]
            else ""
        )
        lines.append(
            f"HEALTH: {h['sampled_in_window']} sampled / "
            f"{h['suppressed_blocks_in_window']} suppressed blocks in {w}d "
            f"(fraction {h['sampling_fraction']:.3f}, "
            f"{h['rows_per_day']:.1f}/day{deg})"
        )

    lines.append(
        f"MATURATION ({result['lookback_days']}d lookback): "
        f"rows={h['total_sampled_rows_lookback']} "
        f"distinct_tokens={h['distinct_tokens_lookback']} "
        f"r24h_rows={h['matured_r24h_rows']} r7d_rows={h['matured_r7d_rows']} "
        f"matured_tokens={h['distinct_tokens_matured']} pending={h['pending']} "
        f"unlabelable={h['unlabelable']} json_parse_failures={h['json_parse_failures']}"
    )

    if c["gated"]:
        lines.append(
            f"COST: INSUFFICIENT_DATA (n={c['n_distinct_tokens']} matured, "
            f"need >={c['min_sample']}; first meaningful read expected "
            f"~{_FIRST_MEANINGFUL_READ})"
        )
    else:
        lines.append(
            f"COST (n={c['n_distinct_tokens']} distinct tokens / "
            f"{c['n_matured_rows']} matured rows, basis r7d fwd return x "
            f"${c['notional_usd']:.0f} notional, 1 row/token @ earliest emission; "
            f"buy@emit mark@7d, no TP/SL/slippage):"
        )
        lines.append(
            f"  est counterfactual PnL ${c['est_pnl_usd']:,.2f} | "
            f"mean {c['mean_return'] * 100:+.1f}% | "
            f"win-rate {c['win_rate']:.1f}% ({c['wins']}/{c['n_distinct_tokens']})"
        )
        movers = "; ".join(
            f"{m['token_id']} {m['r7d'] * 100:+.1f}% ({m['signal_type']})"
            for m in c["top_movers"]
        )
        lines.append(f"  top movers: {movers}")

    return "\n".join(lines)


async def _dispatch_alert(text: str) -> None:
    """Send the summary as a plain-text operator alert. Lazy heavy imports."""
    import aiohttp

    from scout.alerter import send_telegram_message
    from scout.config import Settings

    settings = Settings()
    _log.info("suppression_cost_rollup_alert_dispatched", chars=len(text))
    async with aiohttp.ClientSession() as session:
        await send_telegram_message(
            text,
            session,
            settings,
            parse_mode=None,
            source="suppression_cost_rollup",
        )
    _log.info("suppression_cost_rollup_alert_delivered")


def main() -> int:
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))

    parser = argparse.ArgumentParser(description="Weekly suppression-cost rollup.")
    parser.add_argument("--db", default="scout.db")
    parser.add_argument("--window-days", type=int, default=7)
    parser.add_argument(
        "--min-sample",
        type=int,
        default=_env_int("SUPPRESSION_COST_MIN_SAMPLE", 10),
    )
    parser.add_argument(
        "--notional-usd",
        type=float,
        default=_env_float("SUPPRESSION_COST_NOTIONAL_USD", 1000.0),
    )
    parser.add_argument(
        "--min-rows-per-day",
        type=float,
        default=_env_float("SUPPRESSION_COST_MIN_ROWS_PER_DAY", 1.0),
    )
    parser.add_argument(
        "--min-sampling-fraction",
        type=float,
        default=_env_float("SUPPRESSION_COST_MIN_SAMPLING_FRACTION", 0.5),
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=_env_int("SUPPRESSION_COST_LOOKBACK_DAYS", 120),
    )
    parser.add_argument(
        "--send", action="store_true", help="Telegram-send the summary."
    )
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(f"[suppression-cost-rollup] ERROR db_not_found: {db_path}")
        return 1

    try:
        result = asyncio.run(
            analyze(
                str(db_path),
                window_days=args.window_days,
                min_sample=args.min_sample,
                notional_usd=args.notional_usd,
                min_rows_per_day=args.min_rows_per_day,
                min_sampling_fraction=args.min_sampling_fraction,
                lookback_days=args.lookback_days,
            )
        )
    except Exception as exc:  # read-only; surface, never silently pass
        print(f"[suppression-cost-rollup] ERROR runtime_error: {str(exc)[:200]}")
        return 1

    summary = format_summary(result)
    print(summary)

    if args.send:
        try:
            asyncio.run(_dispatch_alert(summary))
        except Exception as exc:
            _log.warning("suppression_cost_rollup_alert_failed", error=str(exc)[:200])
            return 1

    return 5 if result["health"]["sampling_dead"] else 0


if __name__ == "__main__":
    sys.exit(main())
