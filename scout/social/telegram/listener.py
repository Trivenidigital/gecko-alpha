"""BL-064 Telethon listener — catchup + live event handler + FloodWait wrap.

`handle_new_message` is intentionally a free function (not a method) so
tests can build a `SimpleNamespace` event and call it without mocking
the entire `TelegramClient`. The async task `run_listener` glues
catchup + live handler together with the FloodWait circuit-break.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import aiohttp
import structlog
from telethon import events
from telethon.errors import (
    AuthKeyError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
)

from scout.alerter import send_telegram
from scout.config import Settings
from scout.db import Database
from scout.social.telegram.alerter import (
    format_candidates_alert,
    format_resolved_alert,
    format_unresolved_alert,
)
from scout.social.telegram.client import build_client, connect_and_verify
from scout.social.telegram.dispatcher import dispatch_to_engine
from scout.social.telegram.models import (
    ContractRef,
    ParsedMessage,
    ResolutionResult,
    ResolutionState,
)
from scout.social.telegram.parser import parse_message
from scout.social.telegram.resolver import resolve_and_enrich
from scout.trading.engine import TradingEngine

log = structlog.get_logger()


_WATERMARK_LOCKS: dict[str, asyncio.Lock] = {}
# Module-level set so fire-and-forget retry/silence tasks aren't GC'd.
_PENDING_TASKS: set[asyncio.Task] = set()


def _track_task(t: asyncio.Task) -> None:
    _PENDING_TASKS.add(t)
    t.add_done_callback(_PENDING_TASKS.discard)


def _schedule_retry(
    *,
    db: Database,
    settings: Settings,
    engine: TradingEngine,
    http_session: aiohttp.ClientSession,
    telegram_bot_token: str,
    telegram_chat_id: str,
    channel_handle: str,
    msg_id: int | None,
    message_pk: int,
    parsed: ParsedMessage,
    text: str | None,
) -> None:
    """Schedule a delayed retry of resolve+dispatch as a fire-and-forget task.
    Replaces the v1 inline asyncio.sleep that blocked the listener (devil's
    advocate SHOWSTOPPER #3)."""

    async def _delayed():
        await asyncio.sleep(settings.TG_SOCIAL_RESOLUTION_RETRY_DELAY_SEC)
        try:
            result = await resolve_and_enrich(
                parsed.contracts,
                parsed.cashtags,
                session=http_session,
                settings=settings,
                is_retry=True,
            )
        except Exception as e:
            log.warning(
                "tg_social_retry_resolver_error",
                channel_handle=channel_handle,
                msg_id=msg_id,
                error=str(e),
            )
            return
        await _replay_post_resolution(
            db=db,
            settings=settings,
            engine=engine,
            http_session=http_session,
            telegram_bot_token=telegram_bot_token,
            telegram_chat_id=telegram_chat_id,
            channel_handle=channel_handle,
            msg_id=msg_id,
            message_pk=message_pk,
            parsed=parsed,
            text=text,
            result=result,
            is_retry=True,
        )

    _track_task(asyncio.create_task(_delayed()))


async def _replay_post_resolution(
    *,
    db: Database,
    settings: Settings,
    engine: TradingEngine,
    http_session: aiohttp.ClientSession,
    telegram_bot_token: str,
    telegram_chat_id: str,
    channel_handle: str,
    msg_id: int | None,
    message_pk: int,
    parsed: ParsedMessage,
    text: str | None,
    result: ResolutionResult,
    is_retry: bool,
) -> None:
    """Post-resolution path: alert + (optional) dispatch + persist signal row.
    Called from the main handler AND from the delayed retry task.

    The retry task ONLY runs this when the retry succeeded (state=RESOLVED).
    A retry that still returns TRANSIENT/TERMINAL is logged but produces no
    alert — the main handler already surfaced [retry pending] for the user.
    """
    msg_link = (
        f"https://t.me/{channel_handle.lstrip('@')}/{msg_id}"
        if channel_handle.startswith("@") and msg_id
        else None
    )
    if is_retry and result.state != ResolutionState.RESOLVED:
        log.info(
            "tg_social_retry_no_resolution",
            channel_handle=channel_handle,
            msg_id=msg_id,
            final_state=result.state.value,
        )
        return

    if result.state in (
        ResolutionState.UNRESOLVED_TERMINAL,
        ResolutionState.UNRESOLVED_TRANSIENT,
    ):
        log.info(
            "tg_social_resolution_failed",
            channel_handle=channel_handle,
            msg_id=msg_id,
            final=(result.state == ResolutionState.UNRESOLVED_TERMINAL),
        )
        body = format_unresolved_alert(
            channel_handle=channel_handle,
            cashtags=parsed.cashtags,
            contracts=parsed.contracts,
            state=result.state,
            msg_link=msg_link,
        )
        try:
            await send_telegram(
                http_session, telegram_bot_token, telegram_chat_id, body
            )
        except Exception as e:
            log.warning("tg_social_alert_send_failed", error=str(e))
        await _persist_signal_row(
            db=db,
            message_pk=message_pk,
            token_id="(unresolved)",
            symbol="(unresolved)",
            contract_address=None,
            chain=None,
            mcap=None,
            resolution_state=result.state.value,
            channel_handle=channel_handle,
            paper_trade_id=None,
        )
        return

    # Cashtag-only candidates path
    if not result.tokens and result.candidates_top3:
        body = format_candidates_alert(
            channel_handle=channel_handle,
            cashtags=parsed.cashtags,
            candidates=result.candidates_top3,
            msg_link=msg_link,
        )
        try:
            await send_telegram(
                http_session, telegram_bot_token, telegram_chat_id, body
            )
        except Exception as e:
            log.warning("tg_social_alert_send_failed", error=str(e))
        top = result.candidates_top3[0]
        await _persist_signal_row(
            db=db,
            message_pk=message_pk,
            token_id=top.token_id,
            symbol=top.symbol,
            contract_address=None,
            chain=None,
            mcap=top.mcap,
            resolution_state=result.state.value,
            channel_handle=channel_handle,
            paper_trade_id=None,
        )
        return

    # Resolved-by-CA path
    for token in result.tokens:
        try:
            paper_trade_id, decision_blocked_gate = await dispatch_to_engine(
                db=db,
                settings=settings,
                engine=engine,
                token=token,
                channel_handle=channel_handle,
            )
        except Exception as e:
            await _append_dlq(db, channel_handle, msg_id or 0, text, e)
            continue
        body = format_resolved_alert(
            channel_handle=channel_handle,
            cashtags=parsed.cashtags,
            token=token,
            paper_trade_id=paper_trade_id,
            blocked_gate=decision_blocked_gate,
            msg_link=msg_link,
        )
        try:
            await send_telegram(
                http_session, telegram_bot_token, telegram_chat_id, body
            )
        except Exception as e:
            log.warning("tg_social_alert_send_failed", error=str(e))
        await _persist_signal_row(
            db=db,
            message_pk=message_pk,
            token_id=token.token_id,
            symbol=token.symbol,
            contract_address=token.contract_address,
            chain=token.chain,
            mcap=token.mcap,
            resolution_state=result.state.value,
            channel_handle=channel_handle,
            paper_trade_id=paper_trade_id,
        )
        log.info(
            "tg_social_alert_sent",
            token_id=token.token_id,
            symbol=token.symbol,
            provenance="curator",
            paper_trade_id=paper_trade_id,
        )


def _channel_lock(channel_handle: str) -> asyncio.Lock:
    """Per-channel lock to serialize transactional writes against concurrent
    catchup + live-event paths on the same channel. SQLite's connection-level
    serialization handles cross-channel safety; this lock additionally
    prevents interleaved BEGIN/commits within a single channel's pipeline."""
    lock = _WATERMARK_LOCKS.get(channel_handle)
    if lock is None:
        lock = asyncio.Lock()
        _WATERMARK_LOCKS[channel_handle] = lock
    return lock


async def _persist_message_with_watermark(
    *,
    db: Database,
    channel_handle: str,
    msg_id: int,
    posted_at: datetime,
    sender: str | None,
    text: str | None,
    parsed: ParsedMessage,
) -> int | None:
    """Single transaction: BEGIN IMMEDIATE → INSERT message + UPDATE watermark
    + UPDATE health → COMMIT (or ROLLBACK on failure).

    Watermark advances atomically with the message persist so a crash anywhere
    in the pipeline is safe: UNIQUE(channel_handle, msg_id) makes catchup
    replay idempotent and the watermark cannot lead OR trail the messages
    table out of sync. Closes silent-failure HIGH#2 properly.

    Returns the inserted row pk, or None if the message was a duplicate
    (UNIQUE conflict — normal during catchup re-run).
    """
    import aiosqlite

    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized.")
    now_iso = datetime.now(timezone.utc).isoformat()
    posted_iso = (
        posted_at.astimezone(timezone.utc).isoformat()
        if posted_at.tzinfo is not None
        else posted_at.replace(tzinfo=timezone.utc).isoformat()
    )

    async with _channel_lock(channel_handle):
        try:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cur = await conn.execute(
                    """INSERT INTO tg_social_messages
                       (channel_handle, msg_id, posted_at, sender, text,
                        cashtags, contracts, urls, parsed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        channel_handle,
                        msg_id,
                        posted_iso,
                        sender,
                        text,
                        json.dumps(parsed.cashtags),
                        json.dumps([c.model_dump() for c in parsed.contracts]),
                        json.dumps(parsed.urls),
                        now_iso,
                    ),
                )
                message_pk = cur.lastrowid
            except aiosqlite.IntegrityError as e:
                # Specifically check for the UNIQUE(channel_handle, msg_id)
                # conflict; any other IntegrityError (FK, NOT NULL) propagates.
                if "tg_social_messages" in str(e) and "UNIQUE" in str(e).upper():
                    await conn.execute("ROLLBACK")
                    log.info(
                        "tg_social_message_duplicate_skipped",
                        channel_handle=channel_handle,
                        msg_id=msg_id,
                    )
                    return None
                raise

            await conn.execute(
                """INSERT INTO tg_social_watermarks
                   (channel_handle, last_seen_msg_id, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(channel_handle) DO UPDATE SET
                     last_seen_msg_id = excluded.last_seen_msg_id,
                     updated_at = excluded.updated_at""",
                (channel_handle, msg_id, now_iso),
            )
            await conn.execute(
                """INSERT INTO tg_social_health
                   (component, listener_state, last_message_at, updated_at)
                   VALUES (?, 'running', ?, ?)
                   ON CONFLICT(component) DO UPDATE SET
                     last_message_at = excluded.last_message_at,
                     updated_at = excluded.updated_at""",
                (f"channel:{channel_handle}", now_iso, now_iso),
            )
            await conn.commit()
            return message_pk
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception:
                pass
            raise


async def _persist_signal_row(
    *,
    db: Database,
    message_pk: int,
    token_id: str,
    symbol: str,
    contract_address: str | None,
    chain: str | None,
    mcap: float | None,
    resolution_state: str,
    channel_handle: str,
    paper_trade_id: int | None,
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO tg_social_signals
           (message_pk, token_id, symbol, contract_address, chain,
            mcap_at_sighting, resolution_state, source_channel_handle,
            alert_sent_at, paper_trade_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            message_pk,
            token_id,
            symbol,
            contract_address,
            chain,
            mcap,
            resolution_state,
            channel_handle,
            now_iso,
            paper_trade_id,
            now_iso,
        ),
    )
    await db._conn.commit()


async def _append_dlq(
    db: Database,
    channel_handle: str,
    msg_id: int,
    raw_text: str | None,
    error: Exception,
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        await db._conn.execute(
            """INSERT INTO tg_social_dlq
               (channel_handle, msg_id, raw_text, error_class, error_text, failed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                channel_handle,
                msg_id,
                raw_text,
                type(error).__name__,
                str(error),
                now_iso,
            ),
        )
        await db._conn.commit()
        log.warning(
            "tg_social_dlq_appended",
            channel_handle=channel_handle,
            msg_id=msg_id,
            error_class=type(error).__name__,
        )
    except Exception:
        log.exception(
            "tg_social_dlq_append_failed", channel_handle=channel_handle, msg_id=msg_id
        )


async def handle_new_message(
    event: Any,
    *,
    db: Database,
    settings: Settings,
    engine: TradingEngine,
    http_session: aiohttp.ClientSession,
    telegram_bot_token: str,
    telegram_chat_id: str,
) -> None:
    """Free-function entry point — testable without a TelegramClient mock.

    `event` is a Telethon NewMessage event OR (in tests) a SimpleNamespace
    with the same attribute shape: event.message.id, event.message.message,
    event.message.date, event.chat.username, event.sender (optional).
    """
    msg = getattr(event, "message", event)
    chat = getattr(event, "chat", None) or getattr(event, "chat_or_self", None)
    # Channel handle resolution — explicit branches, no clever and/or chains.
    # Falls through to a DLQ append on truly missing chat metadata so we never
    # write a row under "unknown" (would cause UNIQUE collisions across distinct
    # broken events per code-review BLOCKER #2).
    if chat is None:
        log.warning(
            "tg_social_message_no_chat_metadata", msg_id=getattr(msg, "id", None)
        )
        await _append_dlq(
            db,
            "(no_chat)",
            getattr(msg, "id", None) or 0,
            getattr(msg, "message", None) or getattr(msg, "text", None),
            ValueError("event has no chat metadata"),
        )
        return
    if getattr(chat, "username", None):
        channel_handle = f"@{chat.username}"
    elif getattr(chat, "id", None) is not None:
        # Telethon supergroup ids are already raw; build the t.me-compatible
        # form. For private groups, str(id) suffices for our DB key.
        chat_id = chat.id
        # Negative ids (supergroups) come prefixed; positive ids are users.
        channel_handle = f"-100{chat_id}" if chat_id > 0 else str(chat_id)
    else:
        log.warning("tg_social_message_no_chat_id", msg_id=getattr(msg, "id", None))
        await _append_dlq(
            db,
            "(no_chat_id)",
            getattr(msg, "id", None) or 0,
            getattr(msg, "message", None) or getattr(msg, "text", None),
            ValueError("chat has no username and no id"),
        )
        return
    msg_id = getattr(msg, "id", None)
    text = getattr(msg, "message", None) or getattr(msg, "text", None)
    posted_at = getattr(msg, "date", None) or datetime.now(timezone.utc)
    sender_obj = getattr(event, "sender", None)
    sender = getattr(sender_obj, "username", None) or (
        getattr(sender_obj, "first_name", None) if sender_obj else None
    )

    log.info(
        "tg_social_message_received",
        channel_handle=channel_handle,
        msg_id=msg_id,
        sender=sender,
        text_len=len(text or ""),
    )

    parsed = parse_message(text)
    if parsed.is_empty:
        # Persist anyway so watermark advances, but emit no_signal log
        try:
            await _persist_message_with_watermark(
                db=db,
                channel_handle=channel_handle,
                msg_id=msg_id,
                posted_at=posted_at,
                sender=sender,
                text=text,
                parsed=parsed,
            )
        except Exception as e:
            await _append_dlq(db, channel_handle, msg_id or 0, text, e)
        log.info(
            "tg_social_no_signal_in_message",
            channel_handle=channel_handle,
            msg_id=msg_id,
        )
        return

    try:
        message_pk = await _persist_message_with_watermark(
            db=db,
            channel_handle=channel_handle,
            msg_id=msg_id,
            posted_at=posted_at,
            sender=sender,
            text=text,
            parsed=parsed,
        )
    except Exception as e:
        await _append_dlq(db, channel_handle, msg_id or 0, text, e)
        return
    if message_pk is None:
        return  # duplicate (UNIQUE conflict on catchup re-run) — not an error

    log.info(
        "tg_social_message_persisted",
        channel_handle=channel_handle,
        msg_id=msg_id,
        cashtag_count=len(parsed.cashtags),
        contract_count=len(parsed.contracts),
    )

    # Resolution — non-blocking transient retry. If the first attempt returns
    # TRANSIENT, we DO NOT block the listener for 60s here (devil's advocate
    # SHOWSTOPPER #3); we schedule a delayed retry as a fire-and-forget task
    # and surface a [retry pending] alert immediately. The retry task replays
    # the post-resolution pipeline when it fires.
    result = await resolve_and_enrich(
        parsed.contracts,
        parsed.cashtags,
        session=http_session,
        settings=settings,
        is_retry=False,
    )
    if result.state == ResolutionState.UNRESOLVED_TRANSIENT:
        log.info(
            "tg_social_resolution_retry_scheduled",
            channel_handle=channel_handle,
            msg_id=msg_id,
            delay_sec=settings.TG_SOCIAL_RESOLUTION_RETRY_DELAY_SEC,
        )
        _schedule_retry(
            db=db,
            settings=settings,
            engine=engine,
            http_session=http_session,
            telegram_bot_token=telegram_bot_token,
            telegram_chat_id=telegram_chat_id,
            channel_handle=channel_handle,
            msg_id=msg_id,
            message_pk=message_pk,
            parsed=parsed,
            text=text,
        )
        # Fall through — _replay_post_resolution surfaces the [retry pending]
        # alert NOW so the user sees the curator post immediately even if
        # the scheduled retry never resolves.

    await _replay_post_resolution(
        db=db,
        settings=settings,
        engine=engine,
        http_session=http_session,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        channel_handle=channel_handle,
        msg_id=msg_id,
        message_pk=message_pk,
        parsed=parsed,
        text=text,
        result=result,
        is_retry=False,
    )


async def _set_listener_state(
    db: Database, state: str, detail: str | None = None
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO tg_social_health (component, listener_state, updated_at, detail)
           VALUES ('listener', ?, ?, ?)
           ON CONFLICT(component) DO UPDATE SET
             listener_state = excluded.listener_state,
             updated_at = excluded.updated_at,
             detail = excluded.detail""",
        (state, now_iso, detail),
    )
    await db._conn.commit()


async def _catchup_channel(
    *,
    client,
    db: Database,
    settings: Settings,
    engine: TradingEngine,
    http_session: aiohttp.ClientSession,
    telegram_bot_token: str,
    telegram_chat_id: str,
    channel_handle: str,
) -> None:
    """Fetch missed messages since last_seen_msg_id and replay through pipeline."""
    cur = await db._conn.execute(
        "SELECT last_seen_msg_id FROM tg_social_watermarks WHERE channel_handle = ?",
        (channel_handle,),
    )
    row = await cur.fetchone()
    last_seen = int(row[0]) if row else 0

    fetched = 0
    try:
        async for msg in client.iter_messages(
            channel_handle, min_id=last_seen, limit=settings.TG_SOCIAL_CATCHUP_LIMIT
        ):
            ev = SimpleNamespace(
                message=msg,
                chat=await msg.get_chat(),
                sender=await msg.get_sender(),
            )
            try:
                await handle_new_message(
                    ev,
                    db=db,
                    settings=settings,
                    engine=engine,
                    http_session=http_session,
                    telegram_bot_token=telegram_bot_token,
                    telegram_chat_id=telegram_chat_id,
                )
            except FloodWaitError:
                # Re-raise so the caller can decide circuit-break vs. retry.
                raise
            except AuthKeyError:
                raise
            except Exception as e:
                # Any other failure during a single message gets DLQ'd; keep
                # the catchup loop moving so one poison pill doesn't block.
                await _append_dlq(db, channel_handle, getattr(msg, "id", 0), None, e)
            fetched += 1
    except (ChannelPrivateError, ChatAdminRequiredError) as e:
        log.warning(
            "tg_social_channel_access_error",
            channel_handle=channel_handle,
            error=type(e).__name__,
        )
        await db._conn.execute(
            "UPDATE tg_social_channels SET removed_at = ? WHERE channel_handle = ?",
            (datetime.now(timezone.utc).isoformat(), channel_handle),
        )
        await db._conn.commit()
        try:
            await send_telegram(
                http_session,
                telegram_bot_token,
                telegram_chat_id,
                f"⚠️ tg_social: lost access to {channel_handle} ({type(e).__name__}); "
                f"marked removed_at.",
            )
        except Exception:
            log.warning("tg_social_alert_send_failed_on_kicked")
    except FloodWaitError as fwe:
        # Catchup-time FloodWait: log and re-raise to caller (run_listener)
        # which decides whether to circuit-break (closes code-review BLOCKER #1).
        log.warning(
            "tg_social_catchup_floodwait",
            channel_handle=channel_handle,
            seconds=fwe.seconds,
        )
        raise
    except AuthKeyError as ake:
        log.error(
            "tg_social_catchup_auth_key_error",
            channel_handle=channel_handle,
            error=type(ake).__name__,
        )
        raise
    if fetched == settings.TG_SOCIAL_CATCHUP_LIMIT:
        log.warning(
            "tg_social_catchup_truncated",
            channel_handle=channel_handle,
            limit=settings.TG_SOCIAL_CATCHUP_LIMIT,
            last_seen_msg_id=last_seen,
        )
        await send_telegram(
            http_session,
            telegram_bot_token,
            telegram_chat_id,
            f"⚠️ tg_social: catchup hit limit ({settings.TG_SOCIAL_CATCHUP_LIMIT}) on "
            f"{channel_handle} — N messages may have been missed.",
        )


async def run_listener(
    *,
    db: Database,
    settings: Settings,
    engine: TradingEngine,
    http_session: aiohttp.ClientSession,
) -> None:
    """Main listener task. Launched alongside other long-running tasks in main.py.

    Steps:
      1. build_client + connect_and_verify (get_me() startup check)
      2. For each non-removed channel: catchup pass
      3. Attach NewMessage handler for those channels
      4. Run forever, with FloodWait wrap and AuthKeyError handling
    """
    if not settings.TG_SOCIAL_ENABLED:
        log.info("tg_social_listener_disabled")
        return

    client = await build_client(settings)
    try:
        info = await connect_and_verify(client)
    except Exception as e:
        await _set_listener_state(db, "auth_lost", detail=str(e))
        raise

    await _set_listener_state(db, "running")
    log.info("tg_social_listener_started", **info)

    # Load active channels
    cur = await db._conn.execute(
        "SELECT channel_handle FROM tg_social_channels WHERE removed_at IS NULL"
    )
    channels = [row[0] for row in await cur.fetchall()]

    # Catchup pass with FloodWait/Auth handling — same circuit-break as
    # live-event path so a startup FloodWait doesn't crash the listener
    # before the handler attaches (closes code-review BLOCKER #1).
    for ch in channels:
        try:
            await _catchup_channel(
                client=client,
                db=db,
                settings=settings,
                engine=engine,
                http_session=http_session,
                telegram_bot_token=settings.TELEGRAM_BOT_TOKEN,
                telegram_chat_id=settings.TELEGRAM_CHAT_ID,
                channel_handle=ch,
            )
        except FloodWaitError as fwe:
            cap = settings.TG_SOCIAL_FLOOD_WAIT_MAX_SEC
            if fwe.seconds > cap:
                await _set_listener_state(
                    db,
                    "disabled_floodwait",
                    detail=f"catchup FloodWait {fwe.seconds}s > cap {cap}s",
                )
                try:
                    await send_telegram(
                        http_session,
                        settings.TELEGRAM_BOT_TOKEN,
                        settings.TELEGRAM_CHAT_ID,
                        f"🛑 tg_social catchup circuit-broken on {ch} — "
                        f"FloodWait {fwe.seconds}s > cap {cap}s. Restart to resume.",
                    )
                except Exception:
                    pass
                raise
            await asyncio.sleep(min(fwe.seconds + 1, cap))
        except AuthKeyError as e:
            await _set_listener_state(db, "auth_lost", detail=type(e).__name__)
            raise

    # Channel-silence heartbeat task (per spec). Compares
    # tg_social_health.last_message_at against now() - silence_hours every
    # _CHECK_INTERVAL_SEC. Closes spec-vs-impl drift flagged by 4/5 reviewers.
    async def _silence_heartbeat():
        while True:
            try:
                await asyncio.sleep(
                    settings.TG_SOCIAL_CHANNEL_SILENCE_CHECK_INTERVAL_SEC
                )
                await _emit_silence_alerts(
                    db,
                    http_session,
                    settings,
                    settings.TELEGRAM_BOT_TOKEN,
                    settings.TELEGRAM_CHAT_ID,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tg_social_silence_heartbeat_error")

    silence_task = asyncio.create_task(_silence_heartbeat())
    _track_task(silence_task)

    @client.on(events.NewMessage(chats=channels))
    async def _on_new(event):
        try:
            await handle_new_message(
                event,
                db=db,
                settings=settings,
                engine=engine,
                http_session=http_session,
                telegram_bot_token=settings.TELEGRAM_BOT_TOKEN,
                telegram_chat_id=settings.TELEGRAM_CHAT_ID,
            )
        except FloodWaitError as fwe:
            cap = settings.TG_SOCIAL_FLOOD_WAIT_MAX_SEC
            sleep_for = min(fwe.seconds + 1, cap)
            log.warning(
                "tg_social_floodwait",
                seconds_requested=fwe.seconds,
                sleep_for=sleep_for,
            )
            if fwe.seconds > cap:
                await _set_listener_state(
                    db,
                    "disabled_floodwait",
                    detail=f"FloodWait {fwe.seconds}s > cap {cap}s — listener stopped",
                )
                log.error(
                    "tg_social_floodwait_circuit_break",
                    seconds_requested=fwe.seconds,
                    cap=cap,
                )
                try:
                    await send_telegram(
                        http_session,
                        settings.TELEGRAM_BOT_TOKEN,
                        settings.TELEGRAM_CHAT_ID,
                        f"🛑 tg_social listener circuit-broken — FloodWait "
                        f"{fwe.seconds}s exceeded cap {cap}s. Restart pipeline to resume.",
                    )
                except Exception:
                    pass
                raise
            await asyncio.sleep(sleep_for)
        except AuthKeyError as e:
            await _set_listener_state(db, "auth_lost", detail=type(e).__name__)
            log.error(
                "tg_social_auth_lost",
                error_class=type(e).__name__,
                bootstrap_command="python -m scout.social.telegram.cli bootstrap",
            )
            try:
                await send_telegram(
                    http_session,
                    settings.TELEGRAM_BOT_TOKEN,
                    settings.TELEGRAM_CHAT_ID,
                    f"🛑 tg_social: AuthKeyError — session revoked. "
                    f"Run: python -m scout.social.telegram.cli bootstrap",
                )
            except Exception:
                pass
            raise
        except Exception as e:
            # Catch-all so unanticipated bugs land in DLQ rather than
            # silently crashing the Telethon event loop (code-review #7).
            log.exception(
                "tg_social_handle_unexpected_error",
                msg_id=getattr(getattr(event, "message", None), "id", None),
                error=str(e),
            )
            await _append_dlq(
                db,
                "(unexpected)",
                0,
                None,
                e,
            )

    try:
        await client.run_until_disconnected()
    finally:
        await _set_listener_state(
            db, "stopped", detail="run_until_disconnected returned"
        )
        silence_task.cancel()


async def _emit_silence_alerts(
    db: Database,
    http_session: aiohttp.ClientSession,
    settings: Settings,
    telegram_bot_token: str,
    telegram_chat_id: str,
) -> None:
    """Per-channel silence check. Emits one Telegram alert per silent channel
    until activity resumes. State is implicit in tg_social_health.last_message_at
    so we don't need a separate 'last alert sent' table; the alert fires every
    _CHECK_INTERVAL_SEC while the channel remains silent — which is the right
    operator behaviour (a silent channel is a continuing problem).
    """
    threshold_seconds = settings.TG_SOCIAL_CHANNEL_SILENCE_ALERT_HOURS * 3600
    cur = await db._conn.execute(
        """SELECT component, last_message_at FROM tg_social_health
           WHERE component LIKE 'channel:%' AND last_message_at IS NOT NULL"""
    )
    rows = await cur.fetchall()
    now = datetime.now(timezone.utc)
    for component, last_at_str in rows:
        try:
            last_at = datetime.fromisoformat(last_at_str)
            if last_at.tzinfo is None:
                last_at = last_at.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        elapsed = (now - last_at).total_seconds()
        if elapsed >= threshold_seconds:
            channel = component.removeprefix("channel:")
            log.warning(
                "tg_social_channel_silenced",
                channel_handle=channel,
                last_message_at=last_at_str,
                silence_hours=round(elapsed / 3600, 1),
            )
            try:
                await send_telegram(
                    http_session,
                    telegram_bot_token,
                    telegram_chat_id,
                    f"⚠️ tg_social: no messages from {channel} in "
                    f"{round(elapsed / 3600, 1)}h "
                    f"(threshold {settings.TG_SOCIAL_CHANNEL_SILENCE_ALERT_HOURS}h). "
                    f"Check channel access.",
                )
            except Exception:
                log.warning("tg_social_silence_alert_send_failed", channel=channel)
