from __future__ import annotations

import argparse
from collections.abc import Awaitable, Callable

from gecko.settings import GeckoSettings

JobFn = Callable[[GeckoSettings, argparse.Namespace], Awaitable[int]]
JOBS: dict[str, JobFn] = {}


def register(name: str):
    def deco(fn: JobFn) -> JobFn:
        JOBS[name] = fn
        return fn

    return deco
