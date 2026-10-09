from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable

from gecko.settings import GeckoSettings

JobFn = Callable[[GeckoSettings, argparse.Namespace], Awaitable[int]]
JOBS: dict[str, JobFn] = {}


def register(name: str):
    def deco(fn: JobFn) -> JobFn:
        JOBS[name] = fn
        return fn
    return deco


def _load_jobs() -> None:
    # Importing registers jobs; later tasks add modules here.
    import gecko.freshness  # noqa: F401


def main(argv: list[str] | None = None) -> int:
    _load_jobs()
    p = argparse.ArgumentParser(prog="gecko")
    p.add_argument("job", nargs="?")
    p.add_argument("--list", action="store_true")
    p.add_argument("--as-of", default=None, help="ISO-8601 UTC override for point-in-time jobs")
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
    return asyncio.run(JOBS[ns.job](settings, ns))


if __name__ == "__main__":
    # Run as `python -m gecko`, this file is `__main__`, but job modules
    # register into `gecko.__main__` (a separate module object). Dispatch
    # through the canonical module so the registry it reads is the populated one.
    from gecko.__main__ import main as _canonical_main

    sys.exit(_canonical_main())
