from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import aiohttp
import structlog

from gecko.collectors.goplus import KNOWN_VERDICT
from gecko.jobs import register
from gecko.notify import send_telegram
from gecko.runs import JobResult, run_job
from gecko.settings import GeckoSettings
from gecko.store import Store, canon_ts, parse_utc, utcnow

log = structlog.get_logger("gecko.freshness")

SLO_MINUTES = {
    "observations": 30,
    "source_calls": 30,
    "watchlist": 120,
    "forward_returns": 1560,
    "research_notes": 1560,
    # per source, so one dead collector cannot hide behind a live one
    "observations:dexscreener": 30,
    # Row-anchored on purpose: a run that writes nothing (every pool out of
    # band after an upstream field rename) must still page.
    "observations:geckoterminal": 360,
    "observations:coingecko": 1560,
    "observations:geckoterminal_ohlcv": 1560,  # rows that carry candles only
    "observations:enrich": 1560,
    "benchmark:btc": 180,  # the ledger cannot measure without it
}
BTC_TOKEN_ID = "benchmark:btc"
BACKLOG = "forward_returns_backlog"
BACKLOG_HOURS = 72  # a due return this overdue means the ledger is not keeping up
# which job writes which table; a table is "bootstrapping" until its writer has one ok run
WRITER_JOBS = {
    "observations": (
        "dexscreener",
        "geckoterminal",
        "geckoterminal-ohlcv",
        "coingecko-markets",
        "coingecko-hourly",
        "enrich",
    ),
    "source_calls": ("telegram-reader",),  # an X lane gets its own key, never this one
    "watchlist": ("rank",),
    "forward_returns": ("ledger",),
    "research_notes": ("research",),
    "observations:dexscreener": ("dexscreener",),
    "observations:geckoterminal": ("geckoterminal",),
    "observations:coingecko": ("coingecko-markets",),
    "observations:geckoterminal_ohlcv": ("geckoterminal-ohlcv",),
    "observations:enrich": ("enrich",),
    "benchmark:btc": ("coingecko-hourly",),
}
# Writers outside Hermes cron: no incident path, so freshness pages their failures.
NON_CRON_WRITERS = frozenset({"telegram-reader"})
# Tables whose writer may legitimately write 0 rows (silent channels, an empty
# watchlist, nothing due); judged by its last ok run.
RUN_ANCHORED = frozenset(
    {"watchlist", "forward_returns", "research_notes", "source_calls"}
)
DEDUP_HOURS = 6


@dataclass
class Breach:
    table: str
    last_write: str | None
    slo_minutes: int


@dataclass
class AlertReport:
    sent: int = 0
    failed: int = 0
    skipped: int = 0


async def _writer_run(store: Store, table: str, agg: str) -> str | None:
    """MIN or MAX finished_at over the ok runs of the jobs that write ``table``."""
    q = ",".join("?" * len(WRITER_JOBS[table]))
    row = await (
        await store.conn.execute(
            f"SELECT {agg}(finished_at) FROM runs WHERE ok=1 AND job IN ({q})",
            WRITER_JOBS[table],
        )
    ).fetchone()
    return row[0]


async def _non_cron_first_failure(store: Store, table: str) -> str | None:
    """Earliest failed run of ``table``'s writers that no cron incident would page."""
    jobs = [j for j in WRITER_JOBS[table] if j in NON_CRON_WRITERS]
    if not jobs:
        return None
    row = await (
        await store.conn.execute(
            f"SELECT MIN(finished_at) FROM runs WHERE ok=0 AND job IN "
            f"({','.join('?' * len(jobs))})",
            jobs,
        )
    ).fetchone()
    return row[0]


async def _last_write(store: Store, key: str) -> str | None:
    """Newest write for a table, or for a per-source / benchmark check."""
    if key == BTC_TOKEN_ID:
        where, args = "token_id=?", (BTC_TOKEN_ID,)
    elif key.startswith("observations:"):
        src = key.split(":", 1)[1]
        where, args = "source=?", (src,)
        if src == "geckoterminal_ohlcv":  # status markers are not data
            where += " AND json_array_length(features_json, '$.candles') > 0"
        elif src == "enrich":  # unknown markers and verdict-less reads are not data
            where += " AND " + KNOWN_VERDICT
        elif (
            src == "coingecko"
        ):  # markets rows only: hourly BTC/trending mask a dead sweep
            where += (
                " AND token_id!='benchmark:btc'"
                " AND json_extract(features_json, '$.trending_rank') IS NULL"
            )
    else:
        return await store.last_write(key)
    row = await (
        await store.conn.execute(
            f"SELECT observed_at FROM observations WHERE {where} "
            "ORDER BY observed_at DESC LIMIT 1",
            args,
        )
    ).fetchone()
    return row[0] if row else None


async def _oldest_overdue(store: Store, now: str) -> str | None:
    cutoff = canon_ts((parse_utc(now) - timedelta(hours=BACKLOG_HOURS)).isoformat())
    row = await (
        await store.conn.execute(
            "SELECT MIN(due_at) FROM forward_returns "
            "WHERE measured_at IS NULL AND due_at<?",
            (cutoff,),
        )
    ).fetchone()
    return row[0]


async def check_freshness(store: Store, now: str) -> list[Breach]:
    out: list[Breach] = []
    now_dt = parse_utc(now)
    for table, slo in SLO_MINUTES.items():
        first_run = await _writer_run(store, table, "MIN")
        if first_run is None:
            # Bootstrapping, unless a non-cron writer has failed since deploy.
            failed = await _non_cron_first_failure(store, table)
            if failed and now_dt - parse_utc(failed) > timedelta(minutes=slo):
                out.append(Breach(table, None, slo))
            continue
        if table in RUN_ANCHORED:
            # Legitimately row-less runs (an empty watchlist, nothing due):
            # staleness is the writer's last ok run, not the table's last row.
            last = anchor = await _writer_run(store, table, "MAX")
        else:
            last = await _last_write(store, table)
            # An empty table is measured from the writer's first ok run, so a
            # writer that just started gets one SLO window to produce rows.
            anchor = last if last is not None else first_run
        if now_dt - parse_utc(anchor) > timedelta(minutes=slo):
            out.append(Breach(table, last, slo))
    # Needs no bootstrap: no scheduled row, no backlog. A ledger that never
    # succeeds is exactly what this catches. last_write is the oldest due_at.
    overdue = await _oldest_overdue(store, now)
    if overdue is not None:
        out.append(Breach(BACKLOG, overdue, BACKLOG_HOURS * 60))
    return out


async def _recently_alerted(store: Store, table: str, now: str) -> bool:
    cutoff = (parse_utc(now) - timedelta(hours=DEDUP_HOURS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    row = await (
        await store.conn.execute(
            "SELECT 1 FROM runs WHERE job=? AND finished_at>? LIMIT 1",
            (f"freshness-alert:{table}", cutoff),
        )
    ).fetchone()
    return row is not None


async def alert_breaches(
    store: Store, settings: GeckoSettings, session: aiohttp.ClientSession, now: str
) -> AlertReport:
    report = AlertReport()
    has_channel = bool(settings.telegram_bot_token and settings.telegram_health_chat_id)
    for b in await check_freshness(store, now):
        if await _recently_alerted(store, b.table, now):
            continue
        if not has_channel:
            # No dedup row: the alert retries once a channel is configured.
            log.warning("freshness_alert_skipped_no_channel", table=b.table)
            report.skipped += 1
            continue
        text = (
            f"gecko freshness breach: table {b.table} last activity {b.last_write or 'never'} "
            f"exceeds SLO {b.slo_minutes} min (checked {now})"
        )
        log.info(
            "freshness_alert_dispatched",
            table=b.table,
            slo=b.slo_minutes,
            last_write=b.last_write,
        )
        delivered = await send_telegram(
            session,
            settings.telegram_bot_token.get_secret_value(),
            settings.telegram_health_chat_id,
            text,
        )
        log.info("freshness_alert_delivered", table=b.table, delivered=delivered)
        if delivered:
            # Dedup only on confirmed delivery, so a failed page retries next pass.
            await store.record_run(f"freshness-alert:{b.table}", now, now, True)
            report.sent += 1
        else:
            report.failed += 1
    return report


@register("freshness")
async def freshness_job(settings: GeckoSettings, ns) -> int:
    now = canon_ts(ns.as_of) if ns.as_of else utcnow()

    async def body(store: Store, session: aiohttp.ClientSession) -> JobResult:
        report = await alert_breaches(store, settings, session, now)
        if report.failed > 0:
            raise RuntimeError(
                f"freshness alert delivery failed for {report.failed} table(s)"
            )
        if report.skipped > 0:
            raise RuntimeError(
                f"freshness breach on {report.skipped} table(s) but no alert channel "
                "configured (telegram_bot_token / telegram_health_chat_id)"
            )
        return JobResult(rows_written=report.sent)

    return await run_job("freshness", settings, body)
