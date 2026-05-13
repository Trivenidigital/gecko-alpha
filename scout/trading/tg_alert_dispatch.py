"""BL-NEW-TG-ALERT-ALLOWLIST: per-signal Telegram alert dispatch on
paper-trade open.

Architecture (see tasks/plan_tg_alert_allowlist.md + design_*.md):

- _check_eligibility: signal_params.tg_alert_eligible == 1
- format_paper_trade_alert: concise single-line body with per-signal
  field map (R2-C1 fold) + parse_mode=None caller (R1-C1 fold avoids
  Markdown 400 silent-fail on signal_type underscores)
- notify_paper_trade_opened: orchestrator with atomic check-then-write
  under db._txn_lock (R2-C2 fold) so concurrent dispatches for the same
  token serialize cleanly

Cooldown is per-token ACROSS signal types (R2-I1 fold). A single token
firing two different signals within TG_ALERT_PER_TOKEN_COOLDOWN_HOURS
only alerts once.

Failure isolation (3 layers):
  1. Outer try/except catches even logging failures
  2. Inner try/except catches dispatch failures, demotes pre-emptive
     'sent' row to 'dispatch_failed'
  3. Engine spawns dispatch as `asyncio.create_task` — caller returns
     immediately even if dispatch hangs

Mid-flight task loss on shutdown is acceptable — paper_trades row is
already committed; only the TG alert + tg_alert_log row is lost.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import structlog

from scout import alerter
from scout.config import Settings
from scout.db import Database

log = structlog.get_logger(__name__)

# Default-allow signals (used by auto_suspend revive helper to restore
# eligibility=1 if a signal in this set is revived after auto-suspension).
DEFAULT_ALLOW_SIGNALS = (
    "gainers_early",
    "narrative_prediction",
    "losers_contrarian",
    "volume_spike",
)


async def _demote_sent_row(
    db: Database,
    *,
    sent_row_id: int | None,
    detail: str,
    log_event: str,
) -> None:
    if sent_row_id is None or db._conn is None:
        return
    try:
        async with db._txn_lock:
            await db._conn.execute(
                "UPDATE tg_alert_log "
                "SET outcome='dispatch_failed', detail=? "
                "WHERE id=?",
                (detail, sent_row_id),
            )
            await db._conn.commit()
    except Exception:
        log.exception(log_event, sent_row_id=sent_row_id)


_SIGNAL_EMOJI = {
    "gainers_early": "📈",
    "losers_contrarian": "📉",
    "volume_spike": "⚡",
    "narrative_prediction": "🪙",
    "chain_completed": "🔗",
}


async def _check_eligibility(db: Database, signal_type: str) -> bool:
    if db._conn is None:
        return False
    cur = await db._conn.execute(
        "SELECT tg_alert_eligible FROM signal_params WHERE signal_type = ?",
        (signal_type,),
    )
    row = await cur.fetchone()
    return bool(row and row[0])


async def _check_cooldown(db: Database, settings: Settings, token_id: str) -> bool:
    """Returns True if cooldown is in effect (block the alert).

    R2-I1 fold: keyed on token_id ONLY (across all signal types) so a
    single token firing two different signals within the window only
    alerts once.

    Only counts 'sent' outcomes — transient failures don't suppress next
    legitimate fire.
    """
    if db._conn is None:
        return False
    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(hours=settings.TG_ALERT_PER_TOKEN_COOLDOWN_HOURS)
    ).isoformat()
    cur = await db._conn.execute(
        "SELECT 1 FROM tg_alert_log "
        "WHERE token_id = ? AND outcome = 'sent' "
        "AND alerted_at >= ? LIMIT 1",
        (token_id, cutoff),
    )
    return (await cur.fetchone()) is not None


def _fmt_mcap(mcap):
    if mcap is None:
        return "?"
    if mcap >= 1e9:
        return f"${mcap / 1e9:.1f}B"
    if mcap >= 1e6:
        return f"${mcap / 1e6:.1f}M"
    if mcap >= 1e3:
        return f"${mcap / 1e3:.1f}K"
    return f"${mcap:.0f}"


def _fmt_price(p):
    if p is None or p == 0:
        return "$0"
    if p >= 1:
        return f"${p:.2f}"
    if p >= 0.01:
        return f"${p:.4f}"
    if p >= 0.0001:
        return f"${p:.6f}"
    return f"${p:.8f}"


def format_paper_trade_alert(
    *,
    signal_type: str,
    symbol: str,
    coin_id: str,
    entry_price: float,
    amount_usd: float,
    signal_data: dict | None,
    minara_command: str | None = None,
) -> str:
    """Concise single-line + extras Telegram body for a paper-trade open.

    R1-C1 fold: caller MUST dispatch with parse_mode=None — signal_type
    contains underscores that Markdown parses as italic delimiters,
    producing a silent 400 BAD_REQUEST.

    R2-C1 fold: per-signal field maps verified against actual emissions
    in scout/trading/signals.py.

    R2-format fold: header line is single-line glanceable; per-signal
    detail follows; CoinGecko link last for one-tap research.

    BL-NEW-M1.5C: when `minara_command` is supplied (Solana-listed token),
    appends a `Run: <cmd>` line BEFORE the coingecko link for operator
    copy-paste into their local Minara CLI.
    """
    sd = signal_data or {}
    emoji = _SIGNAL_EMOJI.get(signal_type, "📊")
    header = (
        f"{emoji} {signal_type.upper().replace('_', ' ')} · {symbol} · "
        f"{_fmt_price(entry_price)} · ${amount_usd:.0f}"
    )
    extras = []
    if signal_type in ("gainers_early", "losers_contrarian"):
        if "price_change_24h" in sd:
            extras.append(f"24h: {sd['price_change_24h']:+.1f}%")
        if "mcap" in sd:
            extras.append(f"mcap {_fmt_mcap(sd['mcap'])}")
    elif signal_type == "volume_spike":
        if "spike_ratio" in sd:
            extras.append(f"vol×{sd['spike_ratio']:.1f}")
    elif signal_type == "narrative_prediction":
        if "category" in sd:
            extras.append(f"{sd['category']}")
        if "fit" in sd:
            extras.append(f"fit {sd['fit']}")
        if "mcap" in sd:
            extras.append(f"mcap {_fmt_mcap(sd['mcap'])}")
    detail = " · ".join(extras) if extras else None
    link = f"coingecko.com/en/coins/{coin_id}"
    parts = [header]
    if detail:
        parts.append(detail)
    if minara_command:
        # M1.5c: copy-paste shell command for Solana DEX-eligible tokens.
        # Inserted BEFORE the coingecko link so it's prominent.
        parts.append(f"Run: {minara_command}")
    parts.append(link)
    return "\n".join(parts)


async def notify_paper_trade_opened(
    db: Database,
    settings: Settings,
    session,
    *,
    paper_trade_id: int,
    signal_type: str,
    token_id: str,
    symbol: str,
    entry_price: float,
    amount_usd: float,
    signal_data: dict | None,
) -> None:
    """Fire a Telegram alert for a paper-trade open (best-effort).

    Never raises. Always writes a tg_alert_log row recording the outcome
    (sent / blocked_eligibility / blocked_cooldown / dispatch_failed) for
    audit.

    R2-C2 design-stage fold: atomic check-then-write under db._txn_lock.
    Cooldown check + pre-emptive 'sent' row INSERT happen under a single
    lock, so concurrent tasks for the same token serialize cleanly.

    Mid-flight task loss on shutdown is acceptable — paper_trades row is
    already committed; only the TG alert + tg_alert_log row is lost.
    """
    try:
        if not await _check_eligibility(db, signal_type):
            await _log_outcome(
                db,
                paper_trade_id=paper_trade_id,
                signal_type=signal_type,
                token_id=token_id,
                outcome="blocked_eligibility",
            )
            return

        # R2-C2 atomic claim
        sent_row_id = None
        if db._conn is None:
            return
        async with db._txn_lock:
            cutoff = (
                datetime.now(timezone.utc)
                - timedelta(hours=settings.TG_ALERT_PER_TOKEN_COOLDOWN_HOURS)
            ).isoformat()
            cur = await db._conn.execute(
                "SELECT 1 FROM tg_alert_log "
                "WHERE token_id = ? AND outcome = 'sent' "
                "AND alerted_at >= ? LIMIT 1",
                (token_id, cutoff),
            )
            if await cur.fetchone():
                await db._conn.execute(
                    "INSERT INTO tg_alert_log "
                    "(paper_trade_id, signal_type, token_id, alerted_at, "
                    " outcome, detail) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        paper_trade_id,
                        signal_type,
                        token_id,
                        datetime.now(timezone.utc).isoformat(),
                        "blocked_cooldown",
                        f"hours={settings.TG_ALERT_PER_TOKEN_COOLDOWN_HOURS}",
                    ),
                )
                await db._conn.commit()
                return
            cur = await db._conn.execute(
                "INSERT INTO tg_alert_log "
                "(paper_trade_id, signal_type, token_id, alerted_at, outcome) "
                "VALUES (?, ?, ?, ?, 'sent')",
                (
                    paper_trade_id,
                    signal_type,
                    token_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            sent_row_id = cur.lastrowid
            await db._conn.commit()

        # M1.5c BL-NEW-M1.5C: Minara DEX-eligibility check. After cooldown
        # claim (outside lock) so 100-500ms CG latency doesn't extend
        # lock-hold. Helper never raises Exception, but asyncio.CancelledError
        # propagates per asyncio convention.
        from scout.trading.minara_alert import (
            log_minara_alert_command_emitted,
            maybe_minara_command,
            minara_alert_amount_usd,
            minara_source_event_id,
            persist_minara_alert_emission,
        )

        # PR-V2-I1 fold: on asyncio.CancelledError mid-fetch, the
        # pre-emptive 'sent' row would otherwise block the per-token
        # cooldown for 6h. Demote to 'dispatch_failed' then re-raise to
        # honor cancellation semantics.
        try:
            minara_cmd = await maybe_minara_command(
                session,
                settings,
                coin_id=token_id,
                amount_usd=amount_usd,
            )
        except asyncio.CancelledError:
            await _demote_sent_row(
                db,
                sent_row_id=sent_row_id,
                detail="cancelled_during_minara_lookup",
                log_event="tg_alert_log_demote_failed_on_cancel",
            )
            raise

        # V3-C1 PR-stage fold: format + dispatch BOTH inside the try.
        # If format raises (string mcap, list signal_data), the
        # pre-emptive 'sent' row would otherwise persist -> cooldown
        # query suppresses next legitimate alert for 6h.
        try:
            body = format_paper_trade_alert(
                signal_type=signal_type,
                symbol=symbol,
                coin_id=token_id,
                entry_price=entry_price,
                amount_usd=amount_usd,
                signal_data=signal_data,
                minara_command=minara_cmd,
            )
            # R1-C1 fold: parse_mode=None to avoid Markdown 400 silent-fail
            await alerter.send_telegram_message(
                body,
                session,
                settings,
                parse_mode=None,
                raise_on_failure=True,
            )
        except asyncio.CancelledError:
            await _demote_sent_row(
                db,
                sent_row_id=sent_row_id,
                detail="cancelled_during_telegram_send",
                log_event="tg_alert_log_demote_failed_on_send_cancel",
            )
            raise
        except Exception as e:
            log.warning(
                "tg_alert_dispatch_failed",
                paper_trade_id=paper_trade_id,
                signal_type=signal_type,
                token_id=token_id,
                err=str(e),
            )
            # Demote pre-emptive 'sent' row to 'dispatch_failed'.
            # Cooldown query filters on outcome='sent', so demotion clears
            # the cooldown for the next legitimate fire.
            if sent_row_id is not None and db._conn is not None:
                try:
                    async with db._txn_lock:
                        await db._conn.execute(
                            "UPDATE tg_alert_log "
                            "SET outcome='dispatch_failed', detail=? "
                            "WHERE id=?",
                            (str(e)[:200], sent_row_id),
                        )
                        await db._conn.commit()
                except Exception:
                    log.exception(
                        "tg_alert_log_demote_failed",
                        sent_row_id=sent_row_id,
                    )
            return

        if minara_cmd is not None:
            minara_amount_usd = minara_alert_amount_usd(settings)
            source_event_id = minara_source_event_id(sent_row_id)
            log_minara_alert_command_emitted(
                coin_id=token_id,
                chain="solana",
                amount_usd=minara_amount_usd,
                source_event_id=source_event_id,
            )
            await persist_minara_alert_emission(
                db=db,
                paper_trade_id=paper_trade_id,
                signal_type=signal_type,
                tg_alert_log_id=sent_row_id,
                coin_id=token_id,
                chain="solana",
                amount_usd=minara_amount_usd,
                command_text=minara_cmd,
            )
    except Exception:
        # Belt-and-braces: even logging failures must not propagate up
        # to block paper-trade dispatch.
        log.exception(
            "tg_alert_notify_unexpected_error",
            paper_trade_id=paper_trade_id,
            signal_type=signal_type,
        )


async def _log_outcome(
    db: Database,
    *,
    paper_trade_id: int,
    signal_type: str,
    token_id: str,
    outcome: str,
    detail: str | None = None,
) -> None:
    if db._conn is None:
        return
    async with db._txn_lock:
        await db._conn.execute(
            "INSERT INTO tg_alert_log "
            "(paper_trade_id, signal_type, token_id, alerted_at, outcome, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                paper_trade_id,
                signal_type,
                token_id,
                datetime.now(timezone.utc).isoformat(),
                outcome,
                detail,
            ),
        )
        await db._conn.commit()
