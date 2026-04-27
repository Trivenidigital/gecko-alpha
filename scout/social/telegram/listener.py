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
    """Single transaction: INSERT tg_social_messages + UPDATE watermark.

    Watermark advances HERE — before resolver/alerter/trade — so a crash
    after this point is safe (UNIQUE(channel_handle, msg_id) makes replay
    idempotent). Closes silent-failure HIGH#2.

    Returns the inserted row pk, or None if the message was a duplicate
    (UNIQUE conflict — happens normally on catchup re-run after crash).
    """
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized.")
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        cur = await conn.execute(
            """INSERT INTO tg_social_messages
               (channel_handle, msg_id, posted_at, sender, text,
                cashtags, contracts, urls, parsed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                channel_handle,
                msg_id,
                posted_at.isoformat(),
                sender,
                text,
                json.dumps(parsed.cashtags),
                json.dumps([c.model_dump() for c in parsed.contracts]),
                json.dumps(parsed.urls),
                now_iso,
            ),
        )
        message_pk = cur.lastrowid
    except Exception as e:
        if "UNIQUE" in str(e):
            log.info(
                "tg_social_message_duplicate_skipped",
                channel_handle=channel_handle,
                msg_id=msg_id,
            )
            await conn.commit()
            return None
        raise

    await conn.execute(
        """INSERT INTO tg_social_watermarks (channel_handle, last_seen_msg_id, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(channel_handle) DO UPDATE SET
             last_seen_msg_id = excluded.last_seen_msg_id,
             updated_at = excluded.updated_at""",
        (channel_handle, msg_id, now_iso),
    )
    # Health row — last_message_at update for silence detection
    await conn.execute(
        """INSERT INTO tg_social_health (component, listener_state, last_message_at, updated_at)
           VALUES (?, 'running', ?, ?)
           ON CONFLICT(component) DO UPDATE SET
             last_message_at = excluded.last_message_at,
             updated_at = excluded.updated_at""",
        (f"channel:{channel_handle}", now_iso, now_iso),
    )
    await conn.commit()
    return message_pk


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
    channel_handle = (
        f"@{chat.username}"
        if chat and getattr(chat, "username", None)
        else getattr(chat, "id", None) and f"-100{chat.id}" or "unknown"
    )
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

    # Resolution (with one transient retry for brand-new tokens)
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
        await asyncio.sleep(settings.TG_SOCIAL_RESOLUTION_RETRY_DELAY_SEC)
        result = await resolve_and_enrich(
            parsed.contracts,
            parsed.cashtags,
            session=http_session,
            settings=settings,
            is_retry=True,
        )

    msg_link = (
        f"https://t.me/{channel_handle.lstrip('@')}/{msg_id}"
        if channel_handle.startswith("@") and msg_id
        else None
    )

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
        await send_telegram(http_session, telegram_bot_token, telegram_chat_id, body)
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

    # Cashtag-only path: top-3 candidates, never a trade
    if not result.tokens and result.candidates_top3:
        body = format_candidates_alert(
            channel_handle=channel_handle,
            cashtags=parsed.cashtags,
            candidates=result.candidates_top3,
            msg_link=msg_link,
        )
        await send_telegram(http_session, telegram_bot_token, telegram_chat_id, body)
        # Persist the top-1 candidate for analytics, no trade
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

    # Resolved-by-CA path: dispatch + alert per token
    from scout.trading.engine import TradingEngine as _TE  # local import for type hints

    for token in result.tokens:
        try:
            paper_trade_id = await dispatch_to_engine(
                db=db,
                settings=settings,
                engine=engine,
                token=token,
                channel_handle=channel_handle,
            )
            decision_blocked_gate = None
        except Exception as e:
            await _append_dlq(db, channel_handle, msg_id or 0, text, e)
            continue
        # If dispatch returned None, re-run evaluate to capture the gate name
        # for the alert badge. Cheap (in-memory + small DB lookup).
        if paper_trade_id is None:
            from scout.social.telegram.dispatcher import evaluate as _evaluate

            decision = await _evaluate(
                db=db,
                settings=settings,
                token=token,
                channel_handle=channel_handle,
            )
            decision_blocked_gate = decision.blocked_gate
        body = format_resolved_alert(
            channel_handle=channel_handle,
            cashtags=parsed.cashtags,
            token=token,
            paper_trade_id=paper_trade_id,
            blocked_gate=decision_blocked_gate,
            msg_link=msg_link,
        )
        await send_telegram(http_session, telegram_bot_token, telegram_chat_id, body)
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
            await handle_new_message(
                ev,
                db=db,
                settings=settings,
                engine=engine,
                http_session=http_session,
                telegram_bot_token=telegram_bot_token,
                telegram_chat_id=telegram_chat_id,
            )
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
        await send_telegram(
            http_session,
            telegram_bot_token,
            telegram_chat_id,
            f"⚠️ tg_social: lost access to {channel_handle} ({type(e).__name__}); "
            f"marked removed_at.",
        )
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

    for ch in channels:
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
                await send_telegram(
                    http_session,
                    settings.TELEGRAM_BOT_TOKEN,
                    settings.TELEGRAM_CHAT_ID,
                    f"🛑 tg_social listener circuit-broken — FloodWait {fwe.seconds}s "
                    f"exceeded cap {cap}s. Restart pipeline to resume.",
                )
                raise
            await asyncio.sleep(sleep_for)
        except AuthKeyError as e:
            await _set_listener_state(db, "auth_lost", detail=type(e).__name__)
            log.error(
                "tg_social_auth_lost",
                error_class=type(e).__name__,
                bootstrap_command="python -m scout.social.telegram.cli bootstrap",
            )
            await send_telegram(
                http_session,
                settings.TELEGRAM_BOT_TOKEN,
                settings.TELEGRAM_CHAT_ID,
                f"🛑 tg_social: AuthKeyError — session revoked. "
                f"Run: python -m scout.social.telegram.cli bootstrap",
            )
            raise

    await client.run_until_disconnected()
