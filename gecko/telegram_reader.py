"""Telegram caller channels: a long-running poll loop over gecko's own session.

Each poll reads messages newer than the last one seen per channel, stores the
ones carrying a cashtag or contract address, resolves them to known tokens,
and writes one ``runs`` row (ok only if every channel was read), so a reader
that cannot poll pages through freshness even when the channels are silent.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from datetime import timedelta
from pathlib import Path

import aiosqlite
import structlog

from gecko.calls import event_id, insert_call, resolve_calls
from gecko.jobs import register
from gecko.runs import JobResult, run_job
from gecko.settings import GeckoSettings
from gecko.store import Store, canon_ts, parse_utc, utcnow

log = structlog.get_logger("gecko.telegram")
REPO = Path(__file__).resolve().parents[1]
RESOLVE_DAYS = 7


def session_problem(settings: GeckoSettings, *, must_exist: bool) -> str | None:
    if settings.tg_api_id <= 0 or settings.tg_api_hash is None:
        return "GECKO_TG_API_ID / GECKO_TG_API_HASH not set"
    if not settings.tg_channels:
        return "GECKO_TG_CHANNELS is empty"
    p = Path(settings.tg_session_path)
    if not p.is_absolute():
        return (
            f"GECKO_TG_SESSION_PATH must be absolute (got {settings.tg_session_path!r})"
        )
    if p.suffix != ".session":
        return "GECKO_TG_SESSION_PATH must end in .session"
    if REPO in p.resolve().parents:
        return (
            "GECKO_TG_SESSION_PATH is inside the repo; a session is an identity secret"
        )
    if must_exist:
        if not p.exists():
            return f"session file missing at {p}; run `python -m gecko tg-login`"
        if os.name == "posix" and stat.S_IMODE(p.stat().st_mode) & 0o077:
            return f"session file {p} must be mode 0600"
    return None


async def initial_marks(store: Store, handles: list[str]) -> dict[str, int]:
    marks: dict[str, int] = {}
    for h in handles:
        prefix = event_id(h, 0)[:-1]
        # substr equality, not LIKE: handles contain "_", a LIKE wildcard.
        row = await (
            await store.conn.execute(
                "SELECT MAX(CAST(substr(event_id, ?) AS INTEGER)) FROM source_calls "
                "WHERE source_type='telegram' AND substr(event_id, 1, ?)=?",
                (len(prefix) + 1, len(prefix), prefix),
            )
        ).fetchone()
        marks[h] = row[0] or 0
    return marks


async def poll_once(
    store, client, settings, entities, marks, *, now: str | None = None
) -> tuple[int, int]:
    written = failed = 0
    for handle, ent in entities.items():
        mark, limit = marks.get(handle, 0), settings.tg_catchup_limit
        try:
            # Resuming: oldest-first above the mark, so a backlog over `limit`
            # drains across polls with no gap. First start: newest only.
            msgs = await client.get_messages(
                ent, limit=limit, min_id=mark, reverse=mark > 0
            )
        except (
            Exception
        ) as exc:  # noqa: BLE001 — RPC/flood/transport: recorded, other channels go on
            failed += 1
            log.warning(
                "tg_poll_failed",
                channel=handle,
                error=type(exc).__name__,
                wait_sec=getattr(exc, "seconds", None),
            )
            continue
        if len(msgs) >= limit:
            log.warning("tg_catchup_truncated", channel=handle, limit=limit)
        for m in sorted(msgs, key=lambda m: m.id):
            marks[handle] = max(marks.get(handle, 0), m.id)
            if await insert_call(
                store,
                source_type="telegram",
                source_id=handle,
                event_id=event_id(handle, m.id),
                posted_at=m.date.isoformat(),
                text=m.message,
            ):
                written += 1
    now = canon_ts(now) or utcnow()  # test seam: a fixed clock, no date bomb
    since = canon_ts((parse_utc(now) - timedelta(days=RESOLVE_DAYS)).isoformat())
    await resolve_calls(store, now, since=since)
    return written, failed


async def run_reader(
    store, client, settings, *, sleep=asyncio.sleep, cycles=None
) -> None:
    entities = {
        h.lower(): await client.get_input_entity(h) for h in settings.tg_channels
    }
    marks = await initial_marks(store, list(entities))
    log.info("tg_reader_started", channels=len(entities), marks=marks)
    n = 0
    while cycles is None or n < cycles:
        started = utcnow()
        written, failed = await poll_once(store, client, settings, entities, marks)
        await store.record_run(
            "telegram-reader",
            started,
            utcnow(),
            failed == 0,
            rows_written=written,
            error=f"{failed} channel(s) failed" if failed else None,
        )
        n += 1
        if cycles is None or n < cycles:
            await sleep(settings.tg_poll_interval_sec)


def _client(settings: GeckoSettings):
    from telethon import TelegramClient  # lazy: every other job stays Telethon-free

    return TelegramClient(
        settings.tg_session_path.removesuffix(".session"),
        settings.tg_api_id,
        settings.tg_api_hash.get_secret_value(),
    )


@register("telegram-reader")
async def telegram_reader_job(settings: GeckoSettings, ns) -> int:
    if problem := session_problem(settings, must_exist=True):
        print(problem, file=sys.stderr)
        return 2
    client = _client(settings)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print(
                "telegram session not authorized; run `python -m gecko tg-login`",
                file=sys.stderr,
            )
            return 1
        store = await Store.open(settings.db_path)
        try:
            await run_reader(store, client, settings)
        finally:
            await store.close()
    finally:
        await client.disconnect()
    return 0


@register("tg-login")
async def tg_login_job(settings: GeckoSettings, ns) -> int:
    """Interactive, operator-run once: creates gecko's own session (a new device)."""
    if problem := session_problem(settings, must_exist=False):
        print(problem, file=sys.stderr)
        return 2
    old = os.umask(0o077)  # Telethon creates the session (and -journal) itself
    try:
        client = _client(settings)
        await client.start()  # prompts for phone and login code on this terminal
        await client.disconnect()
    finally:
        os.umask(old)
    os.chmod(settings.tg_session_path, 0o600)
    print(f"session written to {settings.tg_session_path} (0600)")
    return 0


@register("migrate-calls")
async def migrate_calls_job(settings: GeckoSettings, ns) -> int:
    path = Path(ns.history_db)
    if not path.is_file():
        print(f"history db not found: {path}", file=sys.stderr)
        return 1

    async def body(store: Store, session) -> JobResult:
        n = 0
        # immutable: the frozen legacy db is evidence; even WAL sidecars must not appear.
        async with aiosqlite.connect(
            path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
        ) as legacy:
            async with legacy.execute(
                "SELECT channel_handle, msg_id, posted_at, text FROM tg_social_messages ORDER BY id"
            ) as cur:
                async for handle, msg_id, posted_at, text in cur:
                    n += await insert_call(
                        store,
                        source_type="telegram",
                        source_id=handle.lower(),
                        event_id=event_id(handle, msg_id),
                        posted_at=posted_at,
                        text=text,
                    )
            chans = [
                r[0].lower()
                for r in await (
                    await legacy.execute(
                        "SELECT channel_handle FROM tg_social_channels WHERE removed_at IS NULL ORDER BY channel_handle"
                    )
                ).fetchall()
            ]
        resolved = await resolve_calls(store, utcnow(), since=None)
        print(f"migrated {n} call(s), resolved {resolved}")
        print(f"GECKO_TG_CHANNELS={json.dumps(chans)}")
        return JobResult(rows_written=n)

    return await run_job("migrate-calls", settings, body)
