#!/usr/bin/env python3
"""Forward-only CA price-snapshot writer cron entrypoint (design #392 C2).

Runs one cycle of ``scout.source_quality.snapshot_writer.write_price_snapshots``:
for active ``eligible_contract`` X source_calls within the forward horizon, fetch
a current GeckoTerminal price by contract address and append a source-tagged row
to ``source_call_price_snapshots``. Append-only; the (separate) C3 pricing hookup
reads these snapshots.

DEPLOY-WITHOUT-ACTIVATE: the writer is INERT unless ``--enabled`` is truthy
(wired from ``SOURCE_CALL_SNAPSHOT_WRITER_ENABLED`` in .env by the .sh wrapper).
When disabled it exits 0 without importing aiohttp or touching the network, so a
merged-but-unscheduled writer does nothing until the operator activates it — no
deploy/activation during the DEX soak without separate approval.

Provider errors are observable (counted in the emitted JSON + structured logs),
never converted into fake prices — see the writer module.

Exit codes:
  0 — success (including the disabled no-op and zero-snapshot cycles)
  1 — DB missing or runtime error
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite
import structlog

from scout.source_quality.snapshot_writer import (
    record_snapshot_run,
    write_price_snapshots,
)

_TRUTHY = {"1", "true", "yes", "on"}


def _is_enabled(value: str) -> bool:
    return value.strip().lower() in _TRUTHY


async def _run(
    db_path: str,
    *,
    horizon_hours: int,
    max_identities_per_run: int,
    max_requests_per_day: int,
    max_cg_identities_per_run: int,
    max_price_cache_age_min: int,
) -> dict:
    """Enabled path: wire the real C0 GT client and run one snapshot cycle.

    aiohttp + the C0 client are imported LAZILY here so the disabled path (and
    Windows unit runs) never import aiohttp.
    """
    import aiohttp

    from scout.ingestion.gt_ohlcv import fetch_pool_ohlcv, resolve_pool_address

    started = datetime.now(timezone.utc)
    async with aiohttp.ClientSession() as session:

        async def resolve_pool(*, chain, contract_address):
            return await resolve_pool_address(
                session, chain=chain, contract_address=contract_address
            )

        async def fetch_ohlcv(*, network, pool_address):
            return await fetch_pool_ohlcv(
                session, network=network, pool_address=pool_address, limit=1
            )

        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            stats = await write_price_snapshots(
                conn,
                now=datetime.now(timezone.utc),
                resolve_pool=resolve_pool,
                fetch_ohlcv=fetch_ohlcv,
                horizon_hours=horizon_hours,
                max_identities_per_run=max_identities_per_run,
                max_requests_per_day=max_requests_per_day,
                max_cg_identities_per_run=max_cg_identities_per_run,
                max_price_cache_age_min=max_price_cache_age_min,
            )
            # C4: persist this cycle's counters so the coverage watchdogs can
            # read output rows (writer freshness + provider-error rate).
            await record_snapshot_run(
                conn,
                ran_at=datetime.now(timezone.utc).isoformat(),
                stats=stats,
            )
    finished = datetime.now(timezone.utc)
    return {
        "snapshots": stats,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_sec": round((finished - started).total_seconds(), 3),
    }


def _touch_heartbeat(path: Path) -> None:
    """Best-effort heartbeat touch — failures log but do not fail the writer.

    An absent / stale heartbeat surfaces to the (C4) freshness watchdog on its
    next tick; swallowing here would relocate the visible surface, not hide it.
    """
    log = structlog.get_logger()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    except OSError as err:
        log.warning(
            "scps_writer_heartbeat_touch_failed",
            path=str(path),
            errno=err.errno,
            error=str(err),
        )


def _configure_logging() -> None:
    """Route structlog to stderr so this CLI's stdout stays JSON-only for journal
    capture. Called ONLY from the ``__main__`` / cron entrypoint — NOT at import
    time (INF-03). Configuring structlog at module scope is a GLOBAL, process-wide
    mutation: importing this module in a unit test would reconfigure every other
    test's logger and silently empty their captured output. Keeping it here makes
    the import side-effect-free."""
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="scout.db")
    parser.add_argument(
        "--enabled",
        default="false",
        help=(
            "Truthy (1/true/yes/on) activates the writer. Wired from "
            "SOURCE_CALL_SNAPSHOT_WRITER_ENABLED by the .sh wrapper. Default "
            "false -> inert no-op (deploy-without-activate)."
        ),
    )
    parser.add_argument("--horizon-hours", type=int, default=28)
    parser.add_argument(
        "--max-identities-per-run",
        type=int,
        default=25,
        help=(
            "Hard per-run provider-request ceiling (identities x2 calls). "
            "Excess identities are deferred to the next run, not dropped. "
            "Wired from SOURCE_CALL_SNAPSHOT_MAX_IDENTITIES_PER_RUN."
        ),
    )
    parser.add_argument(
        "--max-requests-per-day",
        type=int,
        default=2000,
        help=(
            "Hard rolling-UTC-day provider-request ceiling, measured from the "
            "persisted run counters. Wired from "
            "SOURCE_CALL_SNAPSHOT_MAX_REQUESTS_PER_DAY."
        ),
    )
    parser.add_argument(
        "--max-cg-identities-per-run",
        type=int,
        default=32,
        help=(
            "Per-run ceiling for the CG coin_id lane. That lane spends no "
            "provider requests, so this bounds WRITE RATE, not budget. Wired "
            "from SOURCE_CALL_SNAPSHOT_MAX_CG_IDENTITIES_PER_RUN."
        ),
    )
    parser.add_argument(
        "--max-price-cache-age-min",
        type=int,
        default=90,
        help=(
            "price_cache rows older than this are not treated as "
            "observations. Wired from "
            "SOURCE_CALL_SNAPSHOT_MAX_PRICE_CACHE_AGE_MIN."
        ),
    )
    parser.add_argument("--heartbeat-file", default=None)
    args = parser.parse_args()

    # Deploy-without-activate gate FIRST: no DB, no aiohttp, no network.
    if not _is_enabled(args.enabled):
        print(json.dumps({"ok": True, "skipped": "writer_disabled"}, sort_keys=True))
        return 0

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(
            json.dumps(
                {"ok": False, "error": "db_not_found", "db": str(db_path)},
                sort_keys=True,
            )
        )
        return 1

    try:
        result = asyncio.run(
            _run(
                str(db_path),
                horizon_hours=args.horizon_hours,
                max_identities_per_run=args.max_identities_per_run,
                max_requests_per_day=args.max_requests_per_day,
                max_cg_identities_per_run=args.max_cg_identities_per_run,
                max_price_cache_age_min=args.max_price_cache_age_min,
            )
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "runtime_error",
                    "detail": str(exc)[:200],
                    "db": str(db_path),
                },
                sort_keys=True,
            )
        )
        return 1

    result["ok"] = True
    if args.heartbeat_file:
        _touch_heartbeat(Path(args.heartbeat_file).expanduser())
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    _configure_logging()
    sys.exit(main())
