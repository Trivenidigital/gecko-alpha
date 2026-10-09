from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiohttp

from gecko.jobs import register
from gecko.notify import send_telegram
from gecko.runs import JobResult, run_job
from gecko.settings import GeckoSettings
from gecko.store import Store

SLO_MINUTES = {
    "observations": 30,
    "source_calls": 360,
    "watchlist": 120,
    "forward_returns": 1560,
    "research_notes": 1560,
}
# which job writes which table; a table is "bootstrapping" until its writer has one ok run
WRITER_JOBS = {
    "observations": (
        "dexscreener",
        "geckoterminal",
        "coingecko-markets",
        "coingecko-hourly",
        "enrich",
    ),
    "source_calls": ("telegram-reader", "xkol"),
    "watchlist": ("rank",),
    "forward_returns": ("ledger",),
    "research_notes": ("research",),
}
DEDUP_HOURS = 6


@dataclass
class Breach:
    table: str
    last_write: str | None
    slo_minutes: int


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


async def _writer_has_run(store: Store, table: str) -> bool:
    q = ",".join("?" * len(WRITER_JOBS[table]))
    row = await (
        await store.conn.execute(
            f"SELECT 1 FROM runs WHERE ok=1 AND job IN ({q}) LIMIT 1",
            WRITER_JOBS[table],
        )
    ).fetchone()
    return row is not None


async def check_freshness(store: Store, now: str) -> list[Breach]:
    out: list[Breach] = []
    now_dt = _parse(now)
    for table, slo in SLO_MINUTES.items():
        if not await _writer_has_run(store, table):
            continue
        last = await store.last_write(table)
        if last is None or now_dt - _parse(last) > timedelta(minutes=slo):
            out.append(Breach(table, last, slo))
    return out


async def _recently_alerted(store: Store, table: str, now: str) -> bool:
    cutoff = (_parse(now) - timedelta(hours=DEDUP_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = await (
        await store.conn.execute(
            "SELECT 1 FROM runs WHERE job=? AND finished_at>? LIMIT 1",
            (f"freshness-alert:{table}", cutoff),
        )
    ).fetchone()
    return row is not None


async def alert_breaches(
    store: Store, settings: GeckoSettings, session: aiohttp.ClientSession, now: str
) -> int:
    sent = 0
    for b in await check_freshness(store, now):
        if await _recently_alerted(store, b.table, now):
            continue
        text = (
            f"gecko freshness breach: table {b.table} last write {b.last_write or 'never'} "
            f"exceeds SLO {b.slo_minutes} min (checked {now})"
        )
        if settings.telegram_bot_token and settings.telegram_health_chat_id:
            await send_telegram(
                session,
                settings.telegram_bot_token.get_secret_value(),
                settings.telegram_health_chat_id,
                text,
            )
        await store.record_run(f"freshness-alert:{b.table}", now, now, True)
        sent += 1
    return sent


@register("freshness")
async def freshness_job(settings: GeckoSettings, ns) -> int:
    now = ns.as_of or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def body(store: Store, session: aiohttp.ClientSession) -> JobResult:
        n = await alert_breaches(store, settings, session, now)
        return JobResult(rows_written=n)

    return await run_job("freshness", settings, body)
