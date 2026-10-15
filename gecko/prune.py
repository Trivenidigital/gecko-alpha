from __future__ import annotations

from datetime import timedelta

import aiohttp
import structlog

from gecko.jobs import register
from gecko.runs import JobResult, run_job
from gecko.settings import GeckoSettings
from gecko.store import Store, canon_ts, parse_utc, utcnow

log = structlog.get_logger("gecko.prune")
BTC = "benchmark:btc"  # the 180 d horizon reads BTC at entered_at: never pruned


def _ago(now: str, days: int) -> str:
    return canon_ts((parse_utc(now) - timedelta(days=days)).isoformat())


async def _delete_batches(
    store: Store, table: str, where: str, args: tuple, batch: int
) -> int:
    total = 0
    while True:
        cur = await store.conn.execute(
            f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE {where} LIMIT ?)",
            (*args, batch),
        )
        await store.conn.commit()  # short transactions: hourly writers are not blocked
        total += cur.rowcount
        if cur.rowcount < batch:
            return total


async def prune(
    store: Store, settings: GeckoSettings, now: str, *, batch: int = 5000
) -> tuple[int, int]:
    obs_cut = _ago(now, settings.obs_retention_days)
    runs_cut = _ago(now, settings.runs_retention_days)
    obs_where = "observed_at<? AND token_id!=?"
    runs_where = (
        "finished_at<? AND id NOT IN (SELECT MIN(id) FROM runs WHERE ok=1 GROUP BY job)"
    )
    obs = await _delete_batches(store, "observations", obs_where, (obs_cut, BTC), batch)
    runs = await _delete_batches(store, "runs", runs_where, (runs_cut,), batch)
    log.info("prune_done", observations=obs, runs=runs)
    return obs, runs


@register("prune")
async def prune_job(settings: GeckoSettings, ns) -> int:
    now = utcnow()  # live-only: __main__ refuses --as-of

    async def body(store: Store, session: aiohttp.ClientSession) -> JobResult:
        obs, runs = await prune(store, settings, now)
        return JobResult(rows_written=obs + runs)

    return await run_job("prune", settings, body)
