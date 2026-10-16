from __future__ import annotations

import argparse
import asyncio
import os
import sys

import structlog

from gecko.jobs import JOBS, JobFn, register  # noqa: F401
from gecko.settings import GeckoSettings
from gecko.store import ForeignDatabase

log = structlog.get_logger("gecko.main")


def _load_jobs() -> None:
    # Importing registers jobs; later tasks add modules here.
    import gecko.backtest.runner  # noqa: F401
    import gecko.benchmark  # noqa: F401
    import gecko.check  # noqa: F401
    import gecko.collectors.coingecko  # noqa: F401
    import gecko.collectors.dexscreener  # noqa: F401
    import gecko.collectors.geckoterminal  # noqa: F401
    import gecko.collectors.goplus  # noqa: F401
    import gecko.freshness  # noqa: F401
    import gecko.ledger  # noqa: F401
    import gecko.prune  # noqa: F401
    import gecko.rank  # noqa: F401
    import gecko.report  # noqa: F401
    import gecko.telegram_reader  # noqa: F401


def configure_logging(log_format: str) -> None:
    """Render structlog events for journald: one JSON line each, or console."""
    renderer = (
        structlog.processors.JSONRenderer()
        if log_format == "json"
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            renderer,
        ],
        cache_logger_on_first_use=False,  # tests reconfigure per test
    )


def main(argv: list[str] | None = None) -> int:
    _load_jobs()
    p = argparse.ArgumentParser(
        prog="gecko",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="jobs:\n  "
        + "\n  ".join(sorted(JOBS))
        + "\n\nexit codes: 0 ok, 1 failure, 2 usage or foreign database",
    )
    p.add_argument("job", nargs="?")
    p.add_argument("--list", action="store_true")
    p.add_argument(
        "--as-of", default=None, help="ISO-8601 UTC override for point-in-time jobs"
    )
    p.add_argument("--rule", default="F4")
    p.add_argument("--study-dir", default="/root/xs_study")
    p.add_argument("--history-db", default="/root/gecko-history/scout.db")
    p.add_argument(
        "--from", dest="date_from", default=None, help="backtest: first t0 (inclusive)"
    )
    p.add_argument(
        "--to", dest="date_to", default=None, help="backtest: last t0 (exclusive)"
    )
    p.add_argument(
        "--send", action="store_true", help="check: post a test message to health"
    )
    ns = p.parse_args(argv)
    if ns.list:
        print("\n".join(sorted(JOBS)))
        return 0
    if ns.job not in JOBS:
        print(f"unknown job: {ns.job!r}; use --list", file=sys.stderr)
        return 2
    if ns.job in ("ledger", "prune", "enrich") and ns.as_of:
        # ledger fetches live prices; a future prune "now" deletes live rows;
        # enrich stamps today's GoPlus read at a past time (replay lookahead).
        print(f"{ns.job} is live-only: --as-of is refused", file=sys.stderr)
        return 2
    settings = GeckoSettings()
    configure_logging(settings.log_format)
    # check reports a relative path as a FAIL line instead of being refused here
    if ns.job != "check" and not os.path.isabs(settings.db_path):
        # A relative path silently forks the database per working directory.
        print(
            f"GECKO_DB_PATH must be absolute (got {settings.db_path!r})",
            file=sys.stderr,
        )
        return 2
    if ns.job.startswith("coingecko") and settings.coingecko_demo_api_key is None:
        print(
            f"job {ns.job!r} needs GECKO_COINGECKO_DEMO_API_KEY; refusing to run keyless",
            file=sys.stderr,
        )
        return 1
    try:
        return asyncio.run(JOBS[ns.job](settings, ns))
    except ForeignDatabase as exc:  # refused before any write: a config error
        print(f"job {ns.job!r} refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — a crash outside run_job still exits 1
        log.error("job_crashed", job=ns.job, error=f"{type(exc).__name__}: {exc}")
        print(f"job {ns.job!r} crashed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    # Run as `python -m gecko`, this file is `__main__`, a module object distinct
    # from `gecko.__main__`. Dispatch through the canonical module so there is one
    # import path for main() and its job loading.
    from gecko.__main__ import main as _canonical_main

    sys.exit(_canonical_main())
