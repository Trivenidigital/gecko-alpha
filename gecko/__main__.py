from __future__ import annotations

import argparse
import asyncio
import sys

import structlog

from gecko.jobs import JOBS, JobFn, register  # noqa: F401
from gecko.settings import GeckoSettings

log = structlog.get_logger("gecko.main")


def _load_jobs() -> None:
    # Importing registers jobs; later tasks add modules here.
    import gecko.freshness  # noqa: F401


def main(argv: list[str] | None = None) -> int:
    _load_jobs()
    p = argparse.ArgumentParser(prog="gecko")
    p.add_argument("job", nargs="?")
    p.add_argument("--list", action="store_true")
    p.add_argument(
        "--as-of", default=None, help="ISO-8601 UTC override for point-in-time jobs"
    )
    p.add_argument("--rule", default="F4")
    p.add_argument("--study-dir", default="/root/xs_study")
    p.add_argument("--history-db", default="/root/gecko-history/scout.db")
    ns = p.parse_args(argv)
    if ns.list:
        print("\n".join(sorted(JOBS)))
        return 0
    if ns.job not in JOBS:
        print(f"unknown job: {ns.job!r}; use --list", file=sys.stderr)
        return 2
    settings = GeckoSettings()
    try:
        return asyncio.run(JOBS[ns.job](settings, ns))
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
