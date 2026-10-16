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
    "prune": 1560,  # daily; a dead prune grows the database until the disk fills
    "digest": 1560,  # daily; the digest's footer in turn reports freshness's own age
    "scorecard": 11520,  # weekly
    # Row-anchored: ok polls of channels that never post a call still page.
    "source_calls:rows": 4320,
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
    "benchmark:btc": ("coingecko-hourly", "benchmark"),
    "prune": ("prune",),
    "digest": ("digest",),
    "scorecard": ("scorecard",),
    "source_calls:rows": ("telegram-reader",),
}
# Writers outside Hermes cron: no incident path, so freshness pages their failures.
NON_CRON_WRITERS = frozenset({"telegram-reader"})
# Freshness keys whose writer job may legitimately write 0 rows (silent
# channels, an empty watchlist, nothing due, nothing to prune); judged by the
# writer's last ok run, not by any table's newest row.
RUN_ANCHORED = frozenset(
    {"watchlist", "forward_returns", "research_notes", "source_calls", "prune"}
    | {"digest", "scorecard"}
)
DEDUP_HOURS = 6


@dataclass
class Breach:
    table: str
    last_write: str | None
    slo_minutes: int
    detail: str | None = None  # replaces the generic page text when set


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


async def _non_cron_first_failure(
    store: Store, table: str, expected: bool
) -> str | None:
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
    # An expected writer with no run at all (never installed or started) is
    # measured from the freshness job's first ok run, an anchor it cannot control.
    if row[0] is None and expected:
        row = await (
            await store.conn.execute(
                "SELECT MIN(finished_at) FROM runs WHERE ok=1 AND job='freshness'"
            )
        ).fetchone()
    return row[0]


async def _last_write(store: Store, key: str) -> str | None:
    """Newest write for a table, or for a per-source / benchmark check."""
    if key == BTC_TOKEN_ID:
        where, args = "token_id=?", (BTC_TOKEN_ID,)
    elif key.split(":", 1)[0] == "observations":
        # Benchmark BTC rows are not collector data: they never mask a dead one.
        where, args = "token_id!=?", (BTC_TOKEN_ID,)
        src = key.partition(":")[2]
        if src:
            where, args = where + " AND source=?", (*args, src)
        if src == "geckoterminal_ohlcv":  # status markers are not data
            where += " AND json_array_length(features_json, '$.candles') > 0"
        elif src == "enrich":  # unknown markers and verdict-less reads are not data
            where += " AND " + KNOWN_VERDICT
        elif src == "coingecko":  # markets rows only: trending rows mask a dead sweep
            where += " AND json_extract(features_json, '$.trending_rank') IS NULL"
    else:
        return await store.last_write(key.split(":", 1)[0])
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


async def _skip(store: Store, table: str, settings: GeckoSettings | None) -> bool:
    """Keys judged only once their lane is switched on."""
    if table == "research_notes" and not (
        settings
        and settings.anthropic_api_key
        and settings.anthropic_api_key.get_secret_value()
    ):
        log.debug("freshness_key_skipped", key=table)
        return True
    if table == "source_calls:rows":
        # Configured channels and a reader that has polled ok at least once;
        # before that the run-anchored source_calls key owns the page.
        return not (settings and settings.tg_channels) or (
            await _writer_run(store, table, "MIN") is None
        )
    return False


async def check_freshness(
    store: Store, now: str, settings: GeckoSettings | None = None
) -> list[Breach]:
    out: list[Breach] = []
    # the Telegram reader is expected once channels are configured
    reader_expected = bool(settings and settings.tg_channels)
    now_dt = parse_utc(now)
    for table, slo in SLO_MINUTES.items():
        if await _skip(store, table, settings):
            continue
        first_run = await _writer_run(store, table, "MIN")
        if first_run is None:
            # Bootstrapping, unless a non-cron writer has failed since deploy.
            failed = await _non_cron_first_failure(store, table, reader_expected)
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
            if table == "source_calls:rows":
                # Old history or catch-up rows must not page before the reader
                # has polled ok for a full SLO window.
                anchor = max(anchor, first_run, key=parse_utc)
        if now_dt - parse_utc(anchor) > timedelta(minutes=slo):
            out.append(Breach(table, last, slo))
    # Needs no bootstrap: no scheduled row, no backlog. A ledger that never
    # succeeds is exactly what this catches. last_write is the oldest due_at.
    overdue = await _oldest_overdue(store, now)
    if overdue is not None:
        n = await store.overdue_count(now, BACKLOG_HOURS)
        expired = await store.expired_count(
            canon_ts((now_dt - timedelta(days=7)).isoformat())
        )
        text = (
            f"{BACKLOG}: {n} row(s) > {BACKLOG_HOURS} h overdue (oldest {overdue}); "
            f"expired last 7 d: {expired}"
        )
        out.append(Breach(BACKLOG, overdue, BACKLOG_HOURS * 60, text))
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
    for b in await check_freshness(store, now, settings):
        if await _recently_alerted(store, b.table, now):
            continue
        if not has_channel:
            # No dedup row: the alert retries once a channel is configured.
            log.warning("freshness_alert_skipped_no_channel", table=b.table)
            report.skipped += 1
            continue
        text = (
            f"gecko freshness breach: {b.detail} (checked {now})"
            if b.detail
            else f"gecko freshness breach: table {b.table} last activity "
            f"{b.last_write or 'never'} exceeds SLO {b.slo_minutes} min (checked {now})"
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
