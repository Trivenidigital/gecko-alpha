"""Isolated sustained-capacity probe for the RH/Pons capture loop.

Runs the real ``run_rh_pons_loop`` against a public RPC with a TEMPORARY
database. Settings come ONLY from in-code values (no ``.env`` and no process
environment variables), never the production DB; no alerts, no trades.
Read-only JSON-RPC methods only (the collector's own).

Phases, measured separately:
1. Drain: a finite cold-start backlog (``--backlog-blocks``) is scanned until
   completion-time lag is <= ``--catchup-lag``. Only here must unique coverage
   exceed the chain rate (>= 1.2x), with chain growth measured from the first
   head the probe observed.
2. Steady state: passes after catch-up. Coverage cannot outpace block
   arrival here, so lag p50/p95, failed/throttled passes and event delay are
   the measures. Only events in blocks after the catch-up HEAD count toward
   real-time delay; backfilled samples never do.

Every report carries an overall ``verdict`` (pass | fail | inconclusive) and an
explicit ``stop_reason``. A run that never catches up, or stops on repeated
429s or ``--stop-after-failed-passes`` consecutive failed passes, is a FAIL.
Quota is unknown and NOT probed: the probe stops after
``--stop-after-rate-limits`` throttled passes or ``--max-rpc-calls``.

An authenticated endpoint is passed by environment variable NAME
(``--rpc-url-env``), never on the command line; only that one variable is
read. ``--provider-log-range-cap`` / ``--provider-batch-cap`` refuse, before
any traffic, settings whose eth_getLogs range (span + reorg overlap) or header
batch exceeds the provider's documented limits.

Run on the host (native shell), from the repo root:
    python investigation/rh_pons_sustained_capacity_probe_20260913.py \
        --max-seconds 900 --steady-passes 300 --output rh_capacity.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp  # noqa: E402

from scout.config import Settings  # noqa: E402
from scout.db import Database  # noqa: E402
from scout.ingestion import rh_pons  # noqa: E402

PUBLIC_RPC = "https://rpc.mainnet.chain.robinhood.com"
REAL_TIME_EVENTS = ("token_launched", "curve_buy", "curve_sell", "pool_graduated")
FAILED_STATUSES = ("failed", "timeout", "error", "refused")


class ProbeConfigError(ValueError):
    """Invalid probe configuration, raised before any RPC traffic. Messages
    name inputs but never contain endpoint values."""


def positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def resolve_rpc_url(args: argparse.Namespace) -> str:
    """``--rpc-url`` or, with ``--rpc-url-env``, only that named variable."""
    name = args.rpc_url_env
    if name is None:
        return args.rpc_url
    value = os.environ.get(name, "")
    if not value.strip():
        raise ProbeConfigError(
            f"environment variable {name} is not set or empty"
        ) from None
    try:
        parts = urlsplit(value)
        valid = parts.scheme in ("http", "https") and bool(parts.hostname)
        parts.port  # raises ValueError (quoting the port text) when malformed
    except ValueError:
        valid = False
    if not valid:
        raise ProbeConfigError(
            f"environment variable {name} is not an http(s) URL with a host"
        ) from None
    return value


class IsolatedSettings(Settings):
    """Settings from init values only: no .env and no process environment,
    so an operator shell export (for example RH_PONS_START_BLOCK) cannot
    redirect the probe."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        return (init_settings,)


def redact(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme or 'unknown'}://{parts.hostname or 'unknown'}"


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return round(ordered[rank - 1], 3)


def build_settings(args: argparse.Namespace) -> Settings:
    return IsolatedSettings(
        TELEGRAM_BOT_TOKEN="unused",
        TELEGRAM_CHAT_ID="unused",
        ANTHROPIC_API_KEY="unused",
        DB_PATH="unused.db",
        RH_PONS_COLLECTOR_ENABLED=True,
        RH_PONS_RPC_URL=resolve_rpc_url(args),
        RH_PONS_INITIAL_LOOKBACK_BLOCKS=args.backlog_blocks,
        RH_PONS_START_BLOCK=None,
        RH_PONS_BACKFILL_BLOCK_SPAN=args.max_span,
        RH_PONS_MIN_SCAN_SPAN_BLOCKS=args.min_span,
        RH_PONS_HEADER_BATCH_SIZE=args.header_batch_size,
        RH_PONS_IDLE_SLEEP_SEC=args.idle_sleep,
        RH_PONS_POLL_TIMEOUT_SEC=args.pass_timeout,
        RH_PONS_TOPIC_ONLY_TRADE_QUERY=True,
        RH_PONS_RPC_CALLS_PER_SEC=args.rpc_calls_per_sec,
        RH_PONS_RPC_MIN_CALLS_PER_SEC=args.rpc_min_calls_per_sec,
        RH_PONS_RPC_BURST_CALLS=args.rpc_burst,
        RH_PONS_MAX_HEADERS_PER_PASS=args.max_headers_per_pass,
    )


def effective_settings(settings: Settings) -> dict[str, Any]:
    """Every RH setting the run actually used (URL redacted)."""
    values = {
        key: value
        for key, value in settings.model_dump().items()
        if key.startswith("RH_PONS_")
    }
    values["RH_PONS_RPC_URL"] = redact(settings.RH_PONS_RPC_URL)
    values["SQLITE_BUSY_TIMEOUT_MS"] = settings.SQLITE_BUSY_TIMEOUT_MS
    return values


def isolation_report(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "database": "temporary_directory",
        "settings_sources": "in_code_init_only",
        "reads_dotenv": False,
        # The endpoint variable's NAME only; its value is never reported.
        "environment_variables_read": [args.rpc_url_env] if args.rpc_url_env else [],
    }


def preflight_provider_caps(args: argparse.Namespace, settings: Settings) -> None:
    """Refuse settings beyond the provider's documented limits, before traffic.

    A checkpointed pass queries eth_getLogs from ``next_block - overlap``
    through ``next_block + span - 1``: span + overlap blocks inclusive. The
    header batch only ever shrinks (HTTP 413) from its configured size.
    """
    range_cap = args.provider_log_range_cap
    if range_cap is not None:
        span = settings.RH_PONS_BACKFILL_BLOCK_SPAN
        overlap = settings.RH_PONS_REORG_OVERLAP_BLOCKS
        if span + overlap > range_cap:
            raise ProbeConfigError(
                f"eth_getLogs range needs {span + overlap} blocks (max span {span}"
                f" + reorg overlap {overlap}), above --provider-log-range-cap"
                f" {range_cap}"
            )
    batch_cap = args.provider_batch_cap
    batch = settings.RH_PONS_HEADER_BATCH_SIZE
    if batch_cap is not None and batch > batch_cap:
        raise ProbeConfigError(
            f"header batch size {batch} is above --provider-batch-cap {batch_cap}"
        )


def read_durable_checkpoint(db_path: Path) -> dict[str, Any] | None:
    """The primary deployment's committed checkpoint, read independently."""
    deployment = rh_pons.active_deployment()
    if deployment is None or not Path(db_path).exists():
        return None
    try:
        uri = f"file:{Path(db_path).as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            row = conn.execute(
                "SELECT next_block, head_block, updated_at FROM curve_scan_checkpoints "
                "WHERE chain_id=? AND protocol=? AND factory=?",
                (deployment.chain_id, deployment.version, deployment.factory.lower()),
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return {"next_block": row[0], "head_block": row[1], "updated_at": row[2]}


def real_time_cutoff(catchup: dict | None) -> int | None:
    """Events at or below the head observed at catch-up may be residual
    backlog, so only blocks strictly after that HEAD count as real time."""
    return None if catchup is None else catchup["head"]


async def event_delays(db: Database, after_head: int | None) -> dict[str, Any]:
    if after_head is None:
        return {"samples": 0, "note": "no catch-up; no real-time samples"}
    names = ",".join(f"'{name}'" for name in REAL_TIME_EVENTS)
    cur = await db._conn.execute(
        f"""SELECT event_name,
            (julianday(observed_at) - julianday(event_time)) * 86400.0
        FROM curve_launch_events
        WHERE block_number > ? AND event_name IN ({names})""",
        (after_head,),
    )
    rows = await cur.fetchall()
    # Missing/unparseable clocks and negative delays are counted, never
    # silently dropped into a better-looking percentile.
    invalid = sum(1 for r in rows if r[1] is None)
    negative = sum(1 for r in rows if r[1] is not None and r[1] < 0)
    delays = [r[1] for r in rows if r[1] is not None and r[1] >= 0]
    launches = [r[1] for r in rows if r[0] == "token_launched" and r[1] is not None]
    launches = [d for d in launches if d >= 0]
    return {
        "samples": len(delays),
        "invalid_clock": invalid,
        "negative_delay": negative,
        "p50_s": percentile(delays, 50),
        "p95_s": percentile(delays, 95),
        "max_s": round(max(delays), 3) if delays else None,
        "launch_samples": len(launches),
        "launch_p50_s": percentile(launches, 50),
        "launch_p95_s": percentile(launches, 95),
        "clock_note": "block timestamps have 1 s resolution",
    }


def backup_isolated_db(source: Path, target: Path) -> dict[str, Any]:
    """Copy the closed isolated evidence DB via SQLite's online backup API.

    Refuses to overwrite anything, so it cannot clobber an existing (for
    example production) database. The copy is integrity-checked.
    """
    target = target.resolve()
    if target.exists():
        raise FileExistsError(f"refusing to overwrite {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with (
        closing(sqlite3.connect(source)) as src,
        closing(sqlite3.connect(target)) as dst,
    ):
        src.backup(dst)
    with closing(sqlite3.connect(target)) as check:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        events = check.execute("SELECT COUNT(*) FROM curve_launch_events").fetchone()[0]
    return {"path": str(target), "integrity": integrity, "events": events}


def stop_reason_for(
    results: list[dict], catchup: dict | None, args: argparse.Namespace, timed_out: bool
) -> str:
    """The limit that actually ended the run, most severe first."""
    if sum(1 for r in results if r["rate_limited"]) >= args.stop_after_rate_limits:
        return "rate_limited"
    failed_limit = getattr(args, "stop_after_failed_passes", None)
    if failed_limit is not None:
        # Longest run anywhere: once the stop fired, a pass that finished
        # during cancellation does not rewrite the reason.
        streak = longest = 0
        for r in results:
            streak = streak + 1 if r.get("status") in FAILED_STATUSES else 0
            longest = max(longest, streak)
        if longest >= failed_limit:
            return "failed_passes"
    max_calls = getattr(args, "max_rpc_calls", None)
    if max_calls is not None and sum(r["rpc_calls"] for r in results) >= max_calls:
        return "max_rpc_calls"
    steady = 0 if catchup is None else len(results) - catchup["index"] - 1
    if steady >= args.steady_passes:
        return "steady_target"
    if len(results) >= args.max_passes:
        return "max_passes"
    return "max_seconds" if timed_out else "not_stopped"


def summarize(results: list[dict], started: float, catchup: dict | None) -> dict:
    completed = [r for r in results if r["status"] == "completed"]
    statuses: dict[str, int] = {}
    for r in results:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    durable = [
        r["durable_next_block"]
        for r in results
        if r.get("durable_next_block") is not None
    ]
    regressions = sum(1 for prev, cur in zip(durable, durable[1:]) if cur < prev)
    out: dict[str, Any] = {
        "passes": len(results),
        "statuses": statuses,
        "durable_checkpoint_samples": len(durable),
        "checkpoint_regressions": regressions,
        "rate_limited_passes": sum(1 for r in results if r["rate_limited"]),
        "failed_passes": sum(1 for r in results if r["status"] in FAILED_STATUSES),
        "rpc_calls": sum(r["rpc_calls"] for r in results),
        "truncated_passes": sum(1 for r in results if r.get("truncated")),
        "rpc_s_total": round(sum(r.get("rpc_s", 0.0) for r in results), 3),
        "pacing_wait_s_total": round(
            sum(r.get("pacing_wait_s", 0.0) for r in results), 3
        ),
        "duration_s_total": round(sum(r["duration_s"] for r in results), 3),
    }
    first = completed[0] if completed else None
    if first is None:
        return out
    out["initial_backlog_blocks"] = first["start_head"] - first["from_block"] + 1
    # Chain growth from the FIRST head observed by any pass: early failures
    # must not shrink the chain side of the drain ratio.
    first_head = next(r["start_head"] for r in results if r["start_head"] is not None)
    if catchup is not None:
        window = catchup["t"] - started
        chain = catchup["head"] - first_head
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
        steady_all = results[catchup["index"] + 1 :]
        steady = [r for r in steady_all if r["status"] == "completed"]
        if steady:
            last = steady[-1]
            span_s = last["completed_monotonic"] - catchup["t"]
            lags = [r["completion_head"] - r["to_block"] for r in steady]
            out["steady"] = {
                "completed_passes": len(steady),
                "passes": len(steady_all),
                "failed_passes": sum(
                    1 for r in steady_all if r["status"] in FAILED_STATUSES
                ),
                "rate_limited_passes": sum(1 for r in steady_all if r["rate_limited"]),
                "timeouts": sum(1 for r in steady_all if r["status"] == "timeout"),
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


def evaluate(
    summary: dict,
    delays: dict,
    min_steady: int,
    stop_reason: str | None = None,
    max_rpc_calls_per_min: float | None = None,
) -> dict:
    """Criteria plus one overall verdict; missing evidence is never a pass."""
    verdict: dict[str, Any] = {}
    failures: list[str] = []
    inconclusive: list[str] = []
    if stop_reason == "rate_limited":
        failures.append("stopped_on_rate_limits")
    if stop_reason == "failed_passes":
        failures.append("stopped_on_failed_passes")
    drain = summary.get("drain")
    if not drain or drain.get("coverage_to_chain_ratio") is None:
        verdict["drain_ratio_ge_1_2"] = False
        failures.append("backlog_not_drained")
    else:
        verdict["drain_ratio_ge_1_2"] = drain["coverage_to_chain_ratio"] >= 1.2
        if not verdict["drain_ratio_ge_1_2"]:
            failures.append("drain_ratio_below_1_2")
    steady = summary.get("steady")
    if not steady or steady["completed_passes"] < min_steady:
        verdict["steady_state"] = "not_evaluated_insufficient_passes"
        inconclusive.append("insufficient_steady_passes")
    else:
        checks = {
            "lag_p50_le_50": steady["lag_blocks_p50"] <= 50,
            "lag_p95_le_150": steady["lag_blocks_p95"] <= 150,
            "failed_passes_le_1pct": steady.get("failed_passes", 0)
            <= 0.01 * steady["passes"],
            "no_rate_limited_passes": steady.get("rate_limited_passes", 0) == 0,
        }
        if max_rpc_calls_per_min is not None:
            checks["rpc_calls_per_min_within_budget"] = (
                steady.get("rpc_calls_per_min") is not None
                and steady["rpc_calls_per_min"] <= max_rpc_calls_per_min
            )
        verdict.update(checks)
        failures.extend(name for name, ok in checks.items() if not ok)
    bad_clocks = delays.get("invalid_clock", 0) + delays.get("negative_delay", 0)
    if delays.get("samples", 0) == 0 and bad_clocks == 0:
        verdict["event_delay"] = "not_evaluated_no_real_time_samples"
        inconclusive.append("no_real_time_samples")
    else:
        verdict["event_clocks_valid"] = bad_clocks == 0
        verdict["event_delay_p50_le_10s"] = (
            delays.get("p50_s") is not None and delays["p50_s"] <= 10
        )
        verdict["event_delay_p95_le_30s"] = (
            delays.get("p95_s") is not None and delays["p95_s"] <= 30
        )
        failures.extend(
            name
            for name in (
                "event_clocks_valid",
                "event_delay_p50_le_10s",
                "event_delay_p95_le_30s",
            )
            if not verdict[name]
        )
    verdict["zero_checkpoint_regressions"] = (
        summary.get("checkpoint_regressions", 0) == 0
    )
    if not verdict["zero_checkpoint_regressions"]:
        failures.append("checkpoint_regressed")
    verdict["failures"] = failures
    verdict["inconclusive"] = inconclusive
    verdict["verdict"] = (
        "fail" if failures else ("inconclusive" if inconclusive else "pass")
    )
    return verdict


async def run(args: argparse.Namespace) -> dict:
    # Every refusal below happens before any network call.
    settings = build_settings(args)
    preflight_provider_caps(args, settings)
    if args.db_output is not None and args.db_output.exists():
        # Never overwrite an existing database.
        raise FileExistsError(f"refusing to overwrite {args.db_output}")
    results: list[dict] = []
    catchup: dict | None = None
    done = asyncio.Event()
    started = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="rh-pons-capacity-") as folder:
        db_path = Path(folder) / "isolated.db"

        def observe(result: rh_pons._PassResult) -> None:
            nonlocal catchup
            durable = read_durable_checkpoint(db_path)
            results.append(
                {
                    "status": result.status,
                    "reason": result.reason,
                    "stage": result.stage,
                    "from_block": result.from_block,
                    "to_block": result.to_block,
                    "new_blocks": result.new_blocks,
                    "start_head": result.start_head,
                    "completion_head": result.completion_head,
                    "durable_next_block": durable["next_block"] if durable else None,
                    "recorded_events": result.recorded_events,
                    "header_heights": result.header_heights,
                    "trade_logs": result.trade_logs,
                    "excluded_foreign_logs": result.excluded_foreign_logs,
                    "rpc_calls": result.rpc_calls,
                    "rpc_responses": result.rpc_responses,
                    "rate_limited": result.rate_limited,
                    "duration_s": result.duration_s,
                    "completed_monotonic": result.completed_monotonic,
                    "truncated": result.truncated,
                    "header_budget": result.header_budget,
                    "rpc_s": result.rpc_s,
                    "pacing_wait_s": result.pacing_wait_s,
                }
            )
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
            if (
                stop_reason_for(results, catchup, args, timed_out=False)
                != "not_stopped"
            ):
                done.set()

        db = Database(db_path)
        await db.initialize()
        try:
            # Like the pipeline session: no proxy from the environment.
            async with aiohttp.ClientSession(trust_env=False) as session:
                task = asyncio.create_task(
                    rh_pons.run_rh_pons_loop(session, db, settings, on_pass=observe)
                )
                timed_out = False
                try:
                    await asyncio.wait_for(done.wait(), args.max_seconds)
                except TimeoutError:
                    timed_out = True
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            stop_reason = stop_reason_for(results, catchup, args, timed_out)
            summary = summarize(results, started, catchup)
            final_checkpoint = read_durable_checkpoint(db_path)
            delays = await event_delays(db, real_time_cutoff(catchup))
            cur = await db._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(execution_eligible), 0) "
                "FROM curve_launch_discoveries"
            )
            launches, eligible = await cur.fetchone()
            cur = await db._conn.execute("SELECT COUNT(*) FROM curve_launch_events")
            (events,) = await cur.fetchone()
        finally:
            await db.close()
        # The worker is cancelled and every connection closed before copying.
        backup = (
            None
            if args.db_output is None
            else backup_isolated_db(db_path, args.db_output)
        )
    return {
        "scope": "isolated_public_rpc_sustained_capacity",
        "isolation": isolation_report(args),
        "endpoint": redact(settings.RH_PONS_RPC_URL),
        "quota": "unknown_not_probed",
        "stop_reason": stop_reason,
        "effective_settings": effective_settings(settings),
        "catchup": catchup,
        "real_time_cutoff_head": real_time_cutoff(catchup),
        "final_durable_checkpoint": final_checkpoint,
        "isolated_db_backup": backup,
        "summary": summary,
        "real_time_event_delay": delays,
        "db": {
            "events": events,
            "launches": launches,
            "execution_eligible": eligible,
        },
        "acceptance": evaluate(
            summary,
            delays,
            args.min_steady_passes,
            stop_reason,
            args.max_rpc_calls_per_min,
        ),
        "passes": results if args.include_passes else None,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    endpoint = p.add_mutually_exclusive_group()
    endpoint.add_argument(
        "--rpc-url",
        default=PUBLIC_RPC,
        help="Credential-free endpoint only: arguments are visible to other users",
    )
    endpoint.add_argument(
        "--rpc-url-env",
        metavar="NAME",
        help="Read the endpoint URL from this one environment variable",
    )
    p.add_argument(
        "--provider-log-range-cap",
        type=positive_int,
        help="Provider's max eth_getLogs blocks; refuse max span + reorg overlap above it",
    )
    p.add_argument(
        "--provider-batch-cap",
        type=positive_int,
        help="Provider's max JSON-RPC batch; refuse a larger header batch",
    )
    p.add_argument(
        "--stop-after-failed-passes",
        type=positive_int,
        help="Stop (verdict fail) after this many consecutive failed passes",
    )
    p.add_argument("--max-seconds", type=float, default=900)
    p.add_argument("--max-passes", type=int, default=5000)
    p.add_argument("--max-rpc-calls", type=int, default=10_000)
    p.add_argument("--max-rpc-calls-per-min", type=float, default=None)
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
    p.add_argument("--rpc-calls-per-sec", type=float, default=8.0)
    p.add_argument("--rpc-min-calls-per-sec", type=float, default=1.0)
    p.add_argument("--rpc-burst", type=int, default=100)
    p.add_argument("--max-headers-per-pass", type=int, default=80)
    p.add_argument("--include-passes", action="store_true")
    p.add_argument("--output", type=Path)
    p.add_argument(
        "--db-output",
        type=Path,
        help="Copy the isolated evidence DB here after the run (must not exist)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = asyncio.run(run(args))
    except ProbeConfigError as exc:
        print(f"refused before any RPC traffic: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2, default=str)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
