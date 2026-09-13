"""Isolated sustained-capacity probe for the RH/Pons capture loop.

Runs the real ``run_rh_pons_loop`` against a public RPC with a TEMPORARY
database and in-code settings. It never reads ``.env``, production config or
the production DB, never writes outside its temp directory, sends no alerts
and places no trades. Read-only JSON-RPC methods only (the collector's own).

Phases, measured separately:
1. Drain: a finite cold-start backlog (``--backlog-blocks``) is scanned until
   completion-time lag is <= ``--catchup-lag``. Only here must unique coverage
   exceed the chain rate (>= 1.2x over the same monotonic window).
2. Steady state: passes after catch-up. Coverage cannot outpace block
   arrival here, so lag p50/p95 and event delay are the measures. Only events
   in blocks after the catch-up head count toward real-time delay; backfilled
   samples never do.

Quota is unknown and NOT probed. The loop backs off on 429; the probe stops
after ``--stop-after-rate-limits`` throttled passes and reports it.

Run on the host (native shell), from the repo root:
    python investigation/rh_pons_sustained_capacity_probe_20260913.py \
        --max-seconds 900 --steady-passes 300 --output rh_capacity.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp  # noqa: E402

from scout.config import Settings  # noqa: E402
from scout.db import Database  # noqa: E402
from scout.ingestion import rh_pons  # noqa: E402

PUBLIC_RPC = "https://rpc.mainnet.chain.robinhood.com"
REAL_TIME_EVENTS = ("token_launched", "curve_buy", "curve_sell", "pool_graduated")


def redact(url: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return f"{parts.scheme or 'unknown'}://{parts.hostname or 'unknown'}"


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return round(ordered[rank - 1], 3)


def build_settings(args: argparse.Namespace) -> Settings:
    return Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="unused",
        TELEGRAM_CHAT_ID="unused",
        ANTHROPIC_API_KEY="unused",
        DB_PATH="unused.db",
        RH_PONS_COLLECTOR_ENABLED=True,
        RH_PONS_RPC_URL=args.rpc_url,
        RH_PONS_INITIAL_LOOKBACK_BLOCKS=args.backlog_blocks,
        RH_PONS_BACKFILL_BLOCK_SPAN=args.max_span,
        RH_PONS_MIN_SCAN_SPAN_BLOCKS=args.min_span,
        RH_PONS_HEADER_BATCH_SIZE=args.header_batch_size,
        RH_PONS_IDLE_SLEEP_SEC=args.idle_sleep,
        RH_PONS_POLL_TIMEOUT_SEC=args.pass_timeout,
        RH_PONS_TOPIC_ONLY_TRADE_QUERY=True,
    )


async def event_delays(db: Database, after_block: int | None) -> dict[str, Any]:
    if after_block is None:
        return {"samples": 0, "note": "no catch-up; no real-time samples"}
    names = ",".join(f"'{name}'" for name in REAL_TIME_EVENTS)
    cur = await db._conn.execute(
        f"""SELECT event_name,
            (julianday(observed_at) - julianday(event_time)) * 86400.0
        FROM curve_launch_events
        WHERE block_number > ? AND event_time IS NOT NULL
        AND event_name IN ({names})""",
        (after_block,),
    )
    rows = await cur.fetchall()
    delays = [r[1] for r in rows if r[1] is not None]
    launches = [r[1] for r in rows if r[0] == "token_launched" and r[1] is not None]
    return {
        "samples": len(delays),
        "p50_s": percentile(delays, 50),
        "p95_s": percentile(delays, 95),
        "max_s": round(max(delays), 3) if delays else None,
        "launch_samples": len(launches),
        "launch_p50_s": percentile(launches, 50),
        "launch_p95_s": percentile(launches, 95),
        "clock_note": "block timestamps have 1 s resolution",
    }


def summarize(results: list[dict], started: float, catchup: dict | None) -> dict:
    completed = [r for r in results if r["status"] == "completed"]
    statuses: dict[str, int] = {}
    for r in results:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    regressions = sum(
        1
        for prev, cur in zip(completed, completed[1:])
        if cur["to_block"] is not None
        and prev["to_block"] is not None
        and cur["to_block"] < prev["to_block"]
    )
    out: dict[str, Any] = {
        "passes": len(results),
        "statuses": statuses,
        "checkpoint_regressions": regressions,
        "rate_limited_passes": sum(1 for r in results if r["rate_limited"]),
        "rpc_calls": sum(r["rpc_calls"] for r in results),
    }
    first = completed[0] if completed else None
    if first is None:
        return out
    initial_backlog = first["start_head"] - first["from_block"] + 1
    out["initial_backlog_blocks"] = initial_backlog
    if catchup is not None:
        window = catchup["t"] - started
        chain = catchup["head"] - first["start_head"]
        covered = sum(r["new_blocks"] for r in results[: catchup["index"] + 1])
        out["drain"] = {
            "seconds": round(window, 3),
            "unique_blocks_covered": covered,
            "coverage_blocks_per_s": round(covered / window, 3) if window else None,
            "chain_blocks_per_s": round(chain / window, 3) if window else None,
            "coverage_to_chain_ratio": (
                round(covered / chain, 3) if chain > 0 else None
            ),
        }
        steady = [
            r for r in results[catchup["index"] + 1 :] if r["status"] == "completed"
        ]
        steady_all = results[catchup["index"] + 1 :]
        if steady:
            last = steady[-1]
            span_s = last["completed_monotonic"] - catchup["t"]
            lags = [r["completion_head"] - r["to_block"] for r in steady]
            out["steady"] = {
                "completed_passes": len(steady),
                "passes": len(steady_all),
                "seconds": round(span_s, 3),
                "chain_blocks_per_s": (
                    round((last["completion_head"] - catchup["head"]) / span_s, 3)
                    if span_s > 0
                    else None
                ),
                "coverage_blocks_per_s": (
                    round(sum(r["new_blocks"] for r in steady) / span_s, 3)
                    if span_s > 0
                    else None
                ),
                "lag_blocks_p50": percentile(lags, 50),
                "lag_blocks_p95": percentile(lags, 95),
                "lag_blocks_max": max(lags),
                "pass_duration_p50_s": percentile(
                    [r["duration_s"] for r in steady], 50
                ),
                "pass_duration_p95_s": percentile(
                    [r["duration_s"] for r in steady], 95
                ),
                "timeouts": sum(1 for r in steady_all if r["status"] == "timeout"),
                "rpc_calls_per_min": (
                    round(sum(r["rpc_calls"] for r in steady_all) / span_s * 60, 1)
                    if span_s > 0
                    else None
                ),
                "header_heights_mean": round(
                    sum(r["header_heights"] for r in steady) / len(steady), 1
                ),
                "trade_logs": sum(r["trade_logs"] for r in steady),
                "excluded_foreign_logs": sum(
                    r["excluded_foreign_logs"] for r in steady
                ),
            }
    return out


def evaluate(summary: dict, delays: dict, min_steady: int) -> dict:
    verdict: dict[str, Any] = {}
    drain = summary.get("drain")
    verdict["drain_ratio_ge_1_2"] = (
        None
        if not drain or drain["coverage_to_chain_ratio"] is None
        else drain["coverage_to_chain_ratio"] >= 1.2
    )
    steady = summary.get("steady")
    if not steady or steady["completed_passes"] < min_steady:
        verdict["steady_state"] = "not_evaluated_insufficient_passes"
    else:
        verdict["lag_p50_le_50"] = steady["lag_blocks_p50"] <= 50
        verdict["lag_p95_le_150"] = steady["lag_blocks_p95"] <= 150
        verdict["timeouts_le_1pct"] = steady["timeouts"] <= 0.01 * steady["passes"]
    if delays.get("samples", 0) == 0:
        verdict["event_delay"] = "not_evaluated_no_real_time_samples"
    else:
        verdict["event_delay_p50_le_10s"] = delays["p50_s"] <= 10
        verdict["event_delay_p95_le_30s"] = delays["p95_s"] <= 30
    verdict["zero_checkpoint_regressions"] = summary["checkpoint_regressions"] == 0
    return verdict


async def run(args: argparse.Namespace) -> dict:
    settings = build_settings(args)
    results: list[dict] = []
    catchup: dict | None = None
    done = asyncio.Event()
    started = time.monotonic()

    def observe(result: rh_pons._PassResult) -> None:
        nonlocal catchup
        row = {
            "status": result.status,
            "reason": result.reason,
            "from_block": result.from_block,
            "to_block": result.to_block,
            "new_blocks": result.new_blocks,
            "start_head": result.start_head,
            "completion_head": result.completion_head,
            "recorded_events": result.recorded_events,
            "header_heights": result.header_heights,
            "trade_logs": result.trade_logs,
            "excluded_foreign_logs": result.excluded_foreign_logs,
            "rpc_calls": result.rpc_calls,
            "rate_limited": result.rate_limited,
            "duration_s": result.duration_s,
            "completed_monotonic": result.completed_monotonic,
        }
        results.append(row)
        if (
            catchup is None
            and result.status == "completed"
            and result.completion_head - result.to_block <= args.catchup_lag
        ):
            catchup = {
                "index": len(results) - 1,
                "t": result.completed_monotonic,
                "head": result.completion_head,
                "block": result.to_block,
            }
        steady = 0 if catchup is None else len(results) - catchup["index"] - 1
        if (
            steady >= args.steady_passes
            or len(results) >= args.max_passes
            or sum(1 for r in results if r["rate_limited"])
            >= args.stop_after_rate_limits
        ):
            done.set()

    with tempfile.TemporaryDirectory(prefix="rh-pons-capacity-") as folder:
        db = Database(Path(folder) / "isolated.db")
        await db.initialize()
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                task = asyncio.create_task(
                    rh_pons.run_rh_pons_loop(session, db, settings, on_pass=observe)
                )
                try:
                    await asyncio.wait_for(done.wait(), args.max_seconds)
                    stop_reason = "target_reached_or_limit"
                except TimeoutError:
                    stop_reason = "max_seconds"
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            summary = summarize(results, started, catchup)
            delays = await event_delays(
                db, None if catchup is None else catchup["block"]
            )
            cur = await db._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(execution_eligible), 0) "
                "FROM curve_launch_discoveries"
            )
            launches, eligible = await cur.fetchone()
            cur = await db._conn.execute("SELECT COUNT(*) FROM curve_launch_events")
            (events,) = await cur.fetchone()
        finally:
            await db.close()
    return {
        "scope": "isolated_public_rpc_sustained_capacity",
        "production_mutated": False,
        "endpoint": redact(args.rpc_url),
        "quota": "unknown_not_probed",
        "stop_reason": stop_reason,
        "config": {
            "backlog_blocks": args.backlog_blocks,
            "min_span": args.min_span,
            "max_span": args.max_span,
            "header_batch_size": args.header_batch_size,
            "idle_sleep_s": args.idle_sleep,
            "pass_timeout_s": args.pass_timeout,
            "catchup_lag_blocks": args.catchup_lag,
        },
        "catchup": catchup,
        "summary": summary,
        "real_time_event_delay": delays,
        "db": {
            "events": events,
            "launches": launches,
            "execution_eligible": eligible,
        },
        "acceptance": evaluate(summary, delays, args.min_steady_passes),
        "passes": results if args.include_passes else None,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--rpc-url", default=PUBLIC_RPC)
    p.add_argument("--max-seconds", type=float, default=900)
    p.add_argument("--max-passes", type=int, default=5000)
    p.add_argument("--steady-passes", type=int, default=300)
    p.add_argument("--min-steady-passes", type=int, default=100)
    p.add_argument("--backlog-blocks", type=int, default=3000)
    p.add_argument("--catchup-lag", type=int, default=100)
    p.add_argument("--min-span", type=int, default=100)
    p.add_argument("--max-span", type=int, default=2000)
    p.add_argument("--header-batch-size", type=int, default=50)
    p.add_argument("--idle-sleep", type=float, default=2.0)
    p.add_argument("--pass-timeout", type=float, default=30)
    p.add_argument("--stop-after-rate-limits", type=int, default=3)
    p.add_argument("--include-passes", action="store_true")
    p.add_argument("--output", type=Path)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = asyncio.run(run(args))
    text = json.dumps(report, indent=2, default=str)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
