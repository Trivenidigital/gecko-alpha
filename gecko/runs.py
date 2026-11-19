from __future__ import annotations

import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiohttp
import structlog

from gecko.settings import GeckoSettings
from gecko.store import Store, utcnow

log = structlog.get_logger("gecko.runs")


@dataclass
class JobResult:
    rows_written: int = 0
    credits_used: int = 0


class PartialFailure(Exception):
    """A job body failed after spending credits or writing rows; carries the counts."""

    def __init__(
        self, cause: BaseException, *, credits_used: int = 0, rows_written: int = 0
    ):
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.cause = cause
        self.credits_used = credits_used
        self.rows_written = rows_written


JobBody = Callable[[Store, aiohttp.ClientSession], Awaitable[JobResult]]


async def run_job(name: str, settings: GeckoSettings, fn: JobBody) -> int:
    started = utcnow()
    store = await Store.open(settings.db_path)
    timeout = aiohttp.ClientTimeout(total=settings.http_timeout_sec)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                result = await fn(store, session)
            except (
                Exception
            ) as exc:  # noqa: BLE001 — recorded, then re-raised as exit 1
                partial = exc if isinstance(exc, PartialFailure) else None
                cause = partial.cause if partial else exc
                err = (
                    f"{type(cause).__name__}: {cause}\n{traceback.format_exc()[-1500:]}"
                )
                await store.record_run(
                    name,
                    started,
                    utcnow(),
                    False,
                    rows_written=partial.rows_written if partial else 0,
                    credits_used=partial.credits_used if partial else 0,
                    error=err,
                )
                # the type only: the message can carry an upstream body or a URL;
                # the runs row (operator-readable) keeps the full text
                log.error("job_failed", job=name, error=type(cause).__name__)
                return 1
        await store.record_run(
            name,
            started,
            utcnow(),
            True,
            rows_written=result.rows_written,
            credits_used=result.credits_used,
        )
        log.info(
            "job_ok", job=name, rows=result.rows_written, credits=result.credits_used
        )
        return 0
    finally:
        await store.close()
