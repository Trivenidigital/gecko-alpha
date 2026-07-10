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
from scout.token_ids import match_universe_exclude

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


async def _fetch_signal_sl_pct(db: Database, signal_type: str) -> float | None:
    """Per-signal configured stop-loss percent (signal_params.sl_pct).

    Reused for the ALR-01 SL-in-price line. Fail-soft — a read error must
    never break the alert; the risk block is simply omitted.
    """
    if db._conn is None:
        return None
    try:
        cur = await db._conn.execute(
            "SELECT sl_pct FROM signal_params WHERE signal_type = ?",
            (signal_type,),
        )
        row = await cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None
    except Exception:
        log.exception("tg_alert_sl_pct_fetch_failed", signal_type=signal_type)
        return None


async def _fetch_trade_lead_time(
    db: Database, paper_trade_id: int | None
) -> tuple[float | None, str | None]:
    """Lead-time-vs-trending (minutes, status) for the ALR-01 earliness line.

    Read from the just-opened paper_trades row. Fail-soft.
    """
    if db._conn is None or paper_trade_id is None:
        return (None, None)
    try:
        cur = await db._conn.execute(
            "SELECT lead_time_vs_trending_min, lead_time_vs_trending_status "
            "FROM paper_trades WHERE id = ?",
            (paper_trade_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return (None, None)
        mins = float(row[0]) if row[0] is not None else None
        return (mins, row[1])
    except Exception:
        log.exception("tg_alert_lead_time_fetch_failed", paper_trade_id=paper_trade_id)
        return (None, None)


async def _fetch_liquidity_enriched(db: Database, token_id: str) -> float | None:
    """Enriched liquidity for the token (candidates.liquidity_usd_enriched).

    Populated by the #382 enrichment cron; NULL until then, in which case
    the ALR-01 Liq line is skipped silently. Fail-soft.
    """
    if db._conn is None:
        return None
    try:
        cur = await db._conn.execute(
            "SELECT liquidity_usd_enriched FROM candidates "
            "WHERE contract_address = ?",
            (token_id,),
        )
        row = await cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None
    except Exception:
        log.exception("tg_alert_liquidity_fetch_failed", token_id=token_id)
        return None


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


def _check_universe(settings: Settings, token_id: str) -> str | None:
    """Return the first exclude-pattern matching token_id, else None.

    BL-NEW-ALERT-UNIVERSE-FILTER: keeps out-of-universe CoinGecko ids
    (tokenized equities / ETFs such as `spy-bstocks-tokenized-stock`) off
    the operator-facing alert path. Matching is a case-insensitive substring
    of token_id (the CoinGecko slug) against ALERT_UNIVERSE_EXCLUDE_ID_PATTERNS,
    first-match-wins in list order. Returns None (no block) when the flag is
    OFF. The paper ENGINE is unaffected — only the TG send is suppressed.
    """
    if not settings.ALERT_UNIVERSE_FILTER_ENABLED:
        return None
    return match_universe_exclude(settings.ALERT_UNIVERSE_EXCLUDE_ID_PATTERNS, token_id)


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


def _fmt_earliness(lead_time_min: float | None, lead_time_status: str | None) -> str:
    """ALR-01 earliness line — honest lead-time vs CoinGecko trending.

    Sign convention matches _compute_lead_time_vs_trending (engine.py):
    NEGATIVE lead_time = opened BEFORE the coin trended (beat CG);
    POSITIVE = opened AFTER (late). Any non-'ok' status (no trending
    snapshot for the token, or a compute error) renders as
    'no trending reference' rather than a fabricated number.
    """
    if lead_time_status == "ok" and lead_time_min is not None:
        mins = abs(int(round(lead_time_min)))
        direction = "before" if lead_time_min < 0 else "after"
        return f"{mins} min {direction} CG trending"
    return "no trending reference"


def _build_deep_link(
    dashboard_base_url: str | None, paper_trade_id: int | None
) -> str | None:
    """ALR-09 dashboard deep link: stable hash route to the trade's row.

    Returns None (line omitted) when the base URL is empty (operator
    off-switch) or the trade id is missing.
    """
    if not dashboard_base_url or paper_trade_id is None:
        return None
    return f"{dashboard_base_url.rstrip('/')}/#/trade/{paper_trade_id}"


def format_paper_trade_alert(
    *,
    signal_type: str,
    symbol: str,
    coin_id: str,
    entry_price: float,
    amount_usd: float,
    signal_data: dict | None,
    minara_command: str | None = None,
    sl_pct: float | None = None,
    lead_time_min: float | None = None,
    lead_time_status: str | None = None,
    paper_trade_id: int | None = None,
    dashboard_base_url: str | None = None,
    liquidity_usd_enriched: float | None = None,
) -> str:
    """Telegram body for a paper-trade open — ALR-01 actionable card.

    R1-C1 fold: caller MUST dispatch with parse_mode=None — signal_type
    contains underscores that Markdown parses as italic delimiters,
    producing a silent 400 BAD_REQUEST.

    R2-C1 fold: per-signal field maps verified against actual emissions
    in scout/trading/signals.py.

    ALR-01 alert-body-v2 adds (all opt-in via the new kwargs, so legacy
    callers that pass none get the pre-v2 body verbatim):
      - Entry / SL-in-price / invalidation risk block, derived from the
        per-signal `sl_pct` (SL price = entry × (1 − sl_pct/100)). The SL
        line states the level is PRE-slippage: configured stops fill worse
        than −sl_pct in practice, so the card must never imply the fill.
      - a Liquidity slot, rendered ONLY when candidates.liquidity_usd_enriched
        is populated for the token (#382 fills it later — skip silently now).
      - an earliness line vs CG trending (from lead_time_vs_trending_min).
      - the ALR-09 dashboard deep link as the final one-tap page→row CTA.

    BL-NEW-M1.5C: when `minara_command` is supplied (Solana-listed token),
    a `Run: <cmd>` line is inserted BEFORE the coingecko link for operator
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

    # ALR-01 risk block: explicit entry, stop-in-price, invalidation. The
    # SL level is the CONFIGURED stop before slippage — realized fills have
    # averaged worse, so the wording never implies the actual fill.
    if sl_pct is not None:
        sl_price = entry_price * (1.0 - sl_pct / 100.0)
        parts.append(f"Entry: {_fmt_price(entry_price)}")
        parts.append(f"SL: {_fmt_price(sl_price)} (-{sl_pct:.1f}% before slippage)")
        parts.append(f"Invalid below {_fmt_price(sl_price)}")

    # ALR-01 liquidity slot: only when #382 enrichment has populated it.
    if liquidity_usd_enriched is not None:
        parts.append(f"Liq: {_fmt_mcap(liquidity_usd_enriched)}")

    # ALR-01 earliness line: rendered whenever the caller supplies a
    # lead-time status (populated → before/after; else no-reference).
    if lead_time_status is not None:
        parts.append(_fmt_earliness(lead_time_min, lead_time_status))

    if minara_command:
        # M1.5c: copy-paste shell command for Solana DEX-eligible tokens.
        # Inserted BEFORE the coingecko link so it's prominent.
        parts.append(f"Run: {minara_command}")
    parts.append(link)

    # ALR-09 deep link appended last — the primary one-tap page→row CTA.
    deep_link = _build_deep_link(dashboard_base_url, paper_trade_id)
    if deep_link is not None:
        parts.append(f"Dashboard: {deep_link}")

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
    audit. The BL-NEW-ALERT-UNIVERSE-FILTER guard reuses the
    'blocked_eligibility' outcome with detail='universe_filter:<pattern>' when
    an out-of-universe token_id (e.g. a tokenized equity) is suppressed.

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

        # BL-NEW-ALERT-UNIVERSE-FILTER: suppress out-of-universe ids
        # (tokenized equities / ETFs) on the operator-facing path. Reuses the
        # 'blocked_eligibility' outcome (deliberate operator amendment — no new
        # CHECK-constraint value / migration) with a universe_filter:<pattern>
        # detail so the contamination is quantifiable for a later dispatch-layer
        # decision. Runs AFTER eligibility, BEFORE the atomic dedup claim.
        universe_pattern = _check_universe(settings, token_id)
        if universe_pattern is not None:
            await _log_outcome(
                db,
                paper_trade_id=paper_trade_id,
                signal_type=signal_type,
                token_id=token_id,
                outcome="blocked_eligibility",
                detail=f"universe_filter:{universe_pattern}",
            )
            log.info(
                "tg_alert_blocked_universe",
                token_id=token_id,
                signal_type=signal_type,
                pattern=universe_pattern,
            )
            return

        # R2-C2 atomic claim.
        # BL-NEW-TG-ALERT-NOISE-DEDUP: the live per-token window is now the
        # strict 24h dedup window (TG_ALERT_DEDUP_WINDOW_HOURS), which
        # SUPERSEDES the legacy TG_ALERT_PER_TOKEN_COOLDOWN_HOURS as the
        # single gate authority. `_check_cooldown` is retained (back-compat +
        # 4 live tests) but is no longer consulted here. window==0 disables
        # dedup entirely (clean revert), short-circuiting the prior-row
        # query so there is no off-by-one. See
        # tasks/design_tg_alert_24h_dedup_2026_05_30.md.
        sent_row_id = None
        if db._conn is None:
            # §12b: previously a silent early-exit. Make it auditable.
            log.warning(
                "tg_alert_no_conn",
                paper_trade_id=paper_trade_id,
                signal_type=signal_type,
                token_id=token_id,
            )
            return
        window_hours = settings.TG_ALERT_DEDUP_WINDOW_HOURS
        async with db._txn_lock:
            now_iso = datetime.now(timezone.utc).isoformat()
            if window_hours > 0:
                cutoff = (
                    datetime.now(timezone.utc) - timedelta(hours=window_hours)
                ).isoformat()
                cur = await db._conn.execute(
                    "INSERT INTO tg_alert_log "
                    "(paper_trade_id, signal_type, token_id, alerted_at, outcome) "
                    "SELECT ?, ?, ?, ?, 'sent' "
                    "WHERE NOT EXISTS ("
                    "  SELECT 1 FROM tg_alert_log "
                    "  WHERE token_id = ? AND outcome = 'sent' "
                    "  AND alerted_at >= ?"
                    ") "
                    "RETURNING id",
                    (
                        paper_trade_id,
                        signal_type,
                        token_id,
                        now_iso,
                        token_id,
                        cutoff,
                    ),
                )
                claimed = await cur.fetchone()
                if claimed is None:
                    cur = await db._conn.execute(
                        "SELECT alerted_at FROM tg_alert_log "
                        "WHERE token_id = ? AND outcome = 'sent' "
                        "AND alerted_at >= ? "
                        "ORDER BY alerted_at DESC LIMIT 1",
                        (token_id, cutoff),
                    )
                    prior = await cur.fetchone()
                    prior_alerted_at = prior[0] if prior is not None else None
                    await db._conn.execute(
                        "INSERT INTO tg_alert_log "
                        "(paper_trade_id, signal_type, token_id, alerted_at, "
                        " outcome, detail) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            paper_trade_id,
                            signal_type,
                            token_id,
                            datetime.now(timezone.utc).isoformat(),
                            "blocked_dedup_24h",
                            f"window_h={window_hours}",
                        ),
                    )
                    await db._conn.commit()
                    # §12b: audit the suppression. No TG send happens; the
                    # paper_trades row is unaffected (opened upstream).
                    log.info(
                        "tg_alert_suppressed",
                        token_id=token_id,
                        signal_type=signal_type,
                        window_hours=window_hours,
                        dedup_window_hours=window_hours,
                        prior_alerted_at=prior_alerted_at,
                        reason="dedup_24h",
                    )
                    return
                sent_row_id = claimed[0]
            else:
                # Dedup disabled: direct claim, preserving the clean revert path.
                cur = await db._conn.execute(
                    "INSERT INTO tg_alert_log "
                    "(paper_trade_id, signal_type, token_id, alerted_at, outcome) "
                    "VALUES (?, ?, ?, ?, 'sent') RETURNING id",
                    (
                        paper_trade_id,
                        signal_type,
                        token_id,
                        now_iso,
                    ),
                )
                claimed = await cur.fetchone()
                sent_row_id = claimed[0]
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
        # ALR-01 v2 card inputs (all fail-soft reads — a missing value just
        # omits its line): per-signal stop, lead-time-vs-trending, enriched
        # liquidity. Combined with the ALR-09 deep link from settings.
        sl_pct = await _fetch_signal_sl_pct(db, signal_type)
        lead_time_min, lead_time_status = await _fetch_trade_lead_time(
            db, paper_trade_id
        )
        liquidity_usd_enriched = await _fetch_liquidity_enriched(db, token_id)

        try:
            body = format_paper_trade_alert(
                signal_type=signal_type,
                symbol=symbol,
                coin_id=token_id,
                entry_price=entry_price,
                amount_usd=amount_usd,
                signal_data=signal_data,
                minara_command=minara_cmd,
                sl_pct=sl_pct,
                lead_time_min=lead_time_min,
                lead_time_status=lead_time_status,
                paper_trade_id=paper_trade_id,
                dashboard_base_url=settings.DASHBOARD_BASE_URL,
                liquidity_usd_enriched=liquidity_usd_enriched,
            )
            # §12b: emit a structured log BEFORE the send so every dispatch
            # is traceable in journalctl regardless of delivery outcome (the
            # default alerter logs only on failure — success was silent).
            log.info(
                "tg_alert_dispatched",
                paper_trade_id=paper_trade_id,
                signal_type=signal_type,
                token_id=token_id,
            )
            # R1-C1 fold: parse_mode=None to avoid Markdown 400 silent-fail
            await alerter.send_telegram_message(
                body,
                session,
                settings,
                parse_mode=None,
                raise_on_failure=True,
                source="tg_alert_dispatch",
            )
            # §12b: emit AFTER the send returns (delivery succeeded — the
            # call raises on failure). Together with tg_alert_dispatched this
            # makes "no logs" unambiguous between delivered vs skipped.
            log.info(
                "tg_alert_delivered",
                paper_trade_id=paper_trade_id,
                signal_type=signal_type,
                token_id=token_id,
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
