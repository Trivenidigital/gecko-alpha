"""Scarce Telegram alerts sourced from Today Focus + Now Tradable surfaces."""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import structlog

from dashboard import db as dashboard_db
from scout import alerter
from scout.config import Settings
from scout.db import Database
from scout.trading import alert_dispatch_lifecycle as lifecycle
from scout.trading.tg_alert_dispatch import _fmt_mcap, _fmt_price

log = structlog.get_logger(__name__)

SIGNAL_TYPE = "trade_surface"


@dataclass(frozen=True)
class TradeSurfaceAlertCandidate:
    token_id: str
    symbol: str
    name: str | None
    surface: str
    verdict: str | None
    market_cap: float | None
    current_price: float | None
    move_pct: float | None
    source_corpus: str
    surfaces: tuple[str, ...]
    reasons: tuple[str, ...]


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _symbol(row: dict[str, Any], token_id: str) -> str:
    value = row.get("symbol") or token_id
    return str(value).upper()


def _surfaces(row: dict[str, Any]) -> tuple[str, ...]:
    raw = row.get("surfaces")
    if not isinstance(raw, list):
        return ()
    return tuple(str(x) for x in raw if x)


def _candidate_from_row(
    row: dict[str, Any],
    *,
    surface: str,
    reasons: tuple[str, ...],
) -> TradeSurfaceAlertCandidate | None:
    token_id = str(row.get("token_id") or "").strip()
    if not token_id:
        return None
    if row.get("price_is_stale") is True:
        return None
    move_pct = _safe_float(row.get("current_move_pct"))
    if move_pct is None:
        move_pct = _safe_float(row.get("pct_from_entry"))
    return TradeSurfaceAlertCandidate(
        token_id=token_id,
        symbol=_symbol(row, token_id),
        name=str(row["name"]) if row.get("name") else None,
        surface=surface,
        verdict=str(row["verdict"]) if row.get("verdict") else None,
        market_cap=_safe_float(row.get("market_cap")),
        current_price=_safe_float(row.get("current_price")),
        move_pct=move_pct,
        source_corpus=str(row.get("source_corpus") or "paper"),
        surfaces=_surfaces(row),
        reasons=reasons,
    )


def select_trade_surface_alert_candidates(
    todays_focus_payload: dict[str, Any],
    now_tradable_payload: dict[str, Any],
    *,
    max_candidates: int,
) -> list[TradeSurfaceAlertCandidate]:
    """Return scarce, deterministic candidates from the two review surfaces.

    Priority favors rows confirmed by both surfaces, then additional Now
    Tradable candidate-review rows, then remaining Today Focus rows. This
    gives the operator alerts from both tabs without turning every dashboard
    refresh into Telegram noise.
    """
    focus_rows = [
        r for r in todays_focus_payload.get("rows", []) if isinstance(r, dict)
    ]
    now_rows = [r for r in now_tradable_payload.get("rows", []) if isinstance(r, dict)]

    focus_by_token = {
        str(r.get("token_id")): r
        for r in focus_rows
        if r.get("token_id") and r.get("price_is_stale") is not True
    }
    now_candidate_by_token = {
        str(r.get("token_id")): r
        for r in now_rows
        if r.get("token_id")
        and r.get("verdict") == "candidate_review"
        and r.get("price_is_stale") is not True
    }

    selected: list[TradeSurfaceAlertCandidate] = []
    seen: set[str] = set()

    def add(token_id: str, row: dict[str, Any], surface: str, reasons: tuple[str, ...]):
        if len(selected) >= max_candidates or token_id in seen:
            return
        candidate = _candidate_from_row(row, surface=surface, reasons=reasons)
        if candidate is None:
            return
        selected.append(candidate)
        seen.add(token_id)

    for token_id, row in focus_by_token.items():
        if token_id in now_candidate_by_token:
            merged = {**now_candidate_by_token[token_id], **row}
            add(
                token_id,
                merged,
                "todays_focus+now_tradable",
                ("todays_focus", "now_tradable_candidate_review"),
            )

    for token_id, row in now_candidate_by_token.items():
        add(token_id, row, "now_tradable", ("now_tradable_candidate_review",))

    for token_id, row in focus_by_token.items():
        add(token_id, row, "todays_focus", ("todays_focus",))

    return selected


async def _load_today_focus_alert_payload(db_path: str, *, window_hours: int) -> dict:
    """Build alert-source rows from Trade Inbox using Today's Focus recipe.

    This deliberately does not call the public Today Focus dashboard helper:
    that surface advertises `not_for_alerting=True`. The alert lane has its own
    opt-in policy and reuses only the fixed 3-paper/2-tracker row recipe.
    """
    max_rows = 5
    paper_target = 3
    tracker_target = 2
    trade_payload = await dashboard_db.get_trade_inbox(
        db_path, limit_per_group=20, window_hours=window_hours
    )
    source_rows = dashboard_db._today_focus_candidate_rows(trade_payload)
    selected: list[dict] = []
    seen: set[tuple[str, str]] = set()
    dashboard_db._take_rows(
        source_rows,
        selected=selected,
        seen=seen,
        limit=paper_target,
        source_corpus="paper",
        groups={"act_now", "watch"},
    )
    tracker_limit = len(selected) + tracker_target
    dashboard_db._take_rows(
        source_rows,
        selected=selected,
        seen=seen,
        limit=min(max_rows, tracker_limit),
        source_corpus="tracker",
    )
    dashboard_db._take_rows(source_rows, selected=selected, seen=seen, limit=max_rows)
    return {
        "rows": [
            dashboard_db._today_focus_row(r, price_path_points=None)
            for r in selected[:max_rows]
        ]
    }


def format_trade_surface_alert(candidate: TradeSurfaceAlertCandidate) -> str:
    surface_labels = {
        "todays_focus+now_tradable": "TODAY FOCUS + NOW TRADABLE",
        "todays_focus": "TODAY FOCUS",
        "now_tradable": "NOW TRADABLE",
    }
    header_surface = surface_labels.get(
        candidate.surface, candidate.surface.upper().replace("_", " ")
    )
    title = f"{header_surface} - {candidate.symbol}"
    if candidate.name and candidate.name.lower() != candidate.symbol.lower():
        title = f"{title} ({candidate.name})"
    facts = []
    if candidate.verdict:
        facts.append(f"verdict {candidate.verdict}")
    if candidate.move_pct is not None:
        facts.append(f"move {candidate.move_pct:+.2f}%")
    if candidate.market_cap is not None:
        facts.append(f"mcap {_fmt_mcap(candidate.market_cap)}")
    if candidate.current_price is not None:
        facts.append(f"price {_fmt_price(candidate.current_price)}")
    if candidate.surfaces:
        facts.append("surface " + ",".join(candidate.surfaces[:3]))
    body = [title]
    if facts:
        body.append(" | ".join(facts))
    body.append(f"coingecko.com/en/coins/{candidate.token_id}")
    return "\n".join(body)


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


async def _count_reserved_today(db: Database) -> int:
    """Count RESERVED daily send slots (P0-1 cap arithmetic) using the shared
    lifecycle's cap-reservation set: ``sent`` + ``dispatch_pending`` +
    ``dispatch_attempted`` + ``delivery_unknown_after_send`` (a confirmed
    ``dispatch_failed`` frees its slot). Cap-reservation ONLY — delivered
    reporting still keys strictly on ``outcome='sent'`` (alerts_scoreboard /
    operator-action consumers), so a pending/attempted/unknown row is NEVER
    counted as delivered.
    """
    if db._conn is None:
        return 0
    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM tg_alert_log "
        "WHERE signal_type = ? "
        f"  AND outcome IN {lifecycle.cap_reserved_in_clause()} "
        "  AND alerted_at >= ?",
        (SIGNAL_TYPE, start.isoformat()),
    )
    row = await cur.fetchone()
    return int(row[0] or 0)


async def _send_claimed_alert(
    db: Database,
    settings: Settings,
    session,
    *,
    candidate: TradeSurfaceAlertCandidate,
    window_hours: int,
) -> str:
    """Claim a RESERVED slot as ``dispatch_pending`` in one disciplined
    transaction, do preprocessing (format) while pending, mark
    ``dispatch_attempted`` IMMEDIATELY before the provider call, send OUTSIDE the
    lock, then promote to ``sent`` only after provider acceptance — the shared
    :mod:`scout.trading.alert_dispatch_lifecycle` contract.

    Crash distinguishability (P0-1): a crash during formatting leaves the row
    ``dispatch_pending`` (send never started); a crash during/after the send
    leaves it ``dispatch_attempted`` (reconciled to
    ``delivery_unknown_after_send``). Neither is ever a false ``sent``. Dedup
    treats ``sent`` + ``dispatch_pending`` + ``dispatch_attempted`` as claimed.
    Opt-in lane, 5/day.
    """
    if db._conn is None:
        return "dispatch_failed"
    detail = {
        "surface": candidate.surface,
        "source_corpus": candidate.source_corpus,
        "verdict": candidate.verdict,
        "reasons": list(candidate.reasons),
    }
    now_iso = datetime.now(timezone.utc).isoformat()
    # P0-2 ownership lease: stamped into the pending claim, then required by every
    # subsequent transition so a stale sweep can never demote THIS dispatch.
    lease = lifecycle.new_lease()
    pending_row_id: int | None = None
    async with db.transaction() as conn:
        if window_hours > 0:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=window_hours)
            ).isoformat()
            # Dedup treats RESERVED (sent + pending + attempted + unknown) as
            # claimed. The claim stamps the lease token + heartbeat.
            cur = await conn.execute(
                "INSERT INTO tg_alert_log "
                "(paper_trade_id, signal_type, token_id, alerted_at, outcome, "
                " detail, dispatch_state_updated_at, dispatch_lease_token) "
                "SELECT NULL, ?, ?, ?, 'dispatch_pending', ?, ?, ? "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM tg_alert_log "
                "  WHERE token_id = ? "
                f"    AND outcome IN {lifecycle.dedup_reserved_in_clause()} "
                "    AND alerted_at >= ?"
                ") RETURNING id",
                (
                    SIGNAL_TYPE,
                    candidate.token_id,
                    now_iso,
                    json.dumps(detail, sort_keys=True),
                    now_iso,
                    lease,
                    candidate.token_id,
                    cutoff,
                ),
            )
            row = await cur.fetchone()
            if row is None:
                await conn.execute(
                    "INSERT INTO tg_alert_log "
                    "(paper_trade_id, signal_type, token_id, alerted_at, outcome, detail) "
                    "VALUES (NULL, ?, ?, ?, 'blocked_dedup_24h', ?)",
                    (
                        SIGNAL_TYPE,
                        candidate.token_id,
                        datetime.now(timezone.utc).isoformat(),
                        json.dumps(
                            {**detail, "dedup_window_h": window_hours},
                            sort_keys=True,
                        ),
                    ),
                )
                return "blocked_dedup_24h"
            pending_row_id = int(row[0])
        else:
            cur = await conn.execute(
                "INSERT INTO tg_alert_log "
                "(paper_trade_id, signal_type, token_id, alerted_at, outcome, "
                " detail, dispatch_state_updated_at, dispatch_lease_token) "
                "VALUES (NULL, ?, ?, ?, 'dispatch_pending', ?, ?, ?) RETURNING id",
                (
                    SIGNAL_TYPE,
                    candidate.token_id,
                    now_iso,
                    json.dumps(detail, sort_keys=True),
                    now_iso,
                    lease,
                ),
            )
            row = await cur.fetchone()
            pending_row_id = int(row[0]) if row else None
    # Claim committed as dispatch_pending — a crash before mark_attempted below
    # leaves it pending (send never started), NEVER a false 'sent'.

    try:
        # Preprocessing (formatting) happens while the row is still pending.
        body = format_trade_surface_alert(candidate)
        log.info(
            "trade_surface_alert_dispatched",
            tg_alert_log_id=pending_row_id,
            token_id=candidate.token_id,
            surface=candidate.surface,
        )
        # Mark ATTEMPTED immediately before the provider call — a crash from here
        # on is reconciled to delivery_unknown_after_send, not a false 'sent'.
        # P0-2: the send happens ONLY if the pending->attempted CAS applied FOR
        # THIS lease. A False means a stale sweep reclaimed the row — abort the
        # provider call rather than send under a lost lease.
        if not await lifecycle.mark_attempted(db, pending_row_id, lease=lease):
            log.warning(
                "trade_surface_alert_lease_lost",
                tg_alert_log_id=pending_row_id,
                token_id=candidate.token_id,
                surface=candidate.surface,
            )
            return "dispatch_failed"
        await alerter.send_telegram_message(
            body,
            session,
            settings,
            parse_mode=None,
            raise_on_failure=True,
            source="trade_surface_alerts",
        )
        # Provider accepted — promote attempted -> sent (delivered) under our lease.
        # Check the CAS: a zero-row promote means a stale sweep reclaimed the row
        # mid-send (a >lease-age send); the provider DID accept, so it is not a
        # false 'sent', but the row is now delivery_unknown — surface it.
        if not await lifecycle.promote_sent(db, pending_row_id, lease=lease):
            log.warning(
                "trade_surface_alert_promote_noop_after_send",
                tg_alert_log_id=pending_row_id,
                token_id=candidate.token_id,
                surface=candidate.surface,
            )
        log.info(
            "trade_surface_alert_delivered",
            tg_alert_log_id=pending_row_id,
            token_id=candidate.token_id,
            surface=candidate.surface,
        )
        return "sent"
    except asyncio.CancelledError:
        # Interrupted after the send began — the provider result is unprovable.
        if not await lifecycle.mark_delivery_unknown(
            db,
            pending_row_id,
            lease=lease,
            detail=lifecycle.merge_error_detail(
                detail, "cancelled_during_telegram_send"
            ),
        ):
            log.warning(
                "trade_surface_alert_unknown_cas_noop",
                tg_alert_log_id=pending_row_id,
                token_id=candidate.token_id,
            )
        raise
    except Exception as exc:
        # Confirmed failure (format error or provider error return).
        if not await lifecycle.demote_failed(
            db,
            pending_row_id,
            lease=lease,
            detail=lifecycle.merge_error_detail(detail, str(exc)[:200]),
        ):
            log.warning(
                "trade_surface_alert_demote_cas_noop",
                tg_alert_log_id=pending_row_id,
                token_id=candidate.token_id,
            )
        log.warning(
            "trade_surface_alert_dispatch_failed",
            token_id=candidate.token_id,
            surface=candidate.surface,
            err=str(exc),
        )
        return "dispatch_failed"


async def send_trade_surface_alerts(
    db: Database,
    settings: Settings,
    session,
) -> dict[str, int]:
    """Best-effort surface-alert dispatcher.

    Never raises to the pipeline loop. The code path is opt-in, capped, and
    uses the same `tg_alert_log` + parse-mode discipline as paper-trade alerts.
    """
    counts = {"sent": 0, "blocked_dedup_24h": 0, "dispatch_failed": 0}
    if not settings.TRADE_SURFACE_TG_ALERTS_ENABLED:
        return counts
    try:
        # P0-1 claim-time stale-intent reconciliation via the shared lifecycle
        # (frozen STALE_RECONCILE_SECONDS). A prior crashed run's rows are swept
        # BEFORE counting reserved slots: stale dispatch_pending ->
        # dispatch_failed (send never started), stale dispatch_attempted ->
        # delivery_unknown_after_send (unprovable). Deterministic + self-owned.
        reconciled = await lifecycle.reconcile_stale(db, SIGNAL_TYPE)
        if reconciled["pending_failed"] or reconciled["attempted_unknown"]:
            log.info("trade_surface_pending_reconciled", **reconciled)
        # Cap arithmetic counts RESERVED slots (sent + pending + attempted + unknown).
        reserved_today = await _count_reserved_today(db)
        remaining = max(
            0, settings.TRADE_SURFACE_TG_ALERTS_MAX_PER_DAY - reserved_today
        )
        if remaining <= 0:
            log.info(
                "trade_surface_alert_daily_cap_reached", reserved_today=reserved_today
            )
            return counts

        max_candidates = min(settings.TRADE_SURFACE_TG_ALERTS_MAX_PER_RUN, remaining)
        focus_payload = await _maybe_await(
            _load_today_focus_alert_payload(
                str(Path(settings.DB_PATH)),
                window_hours=settings.TRADE_SURFACE_TG_ALERTS_WINDOW_HOURS,
            )
        )
        now_payload = await _maybe_await(
            dashboard_db.get_live_candidates(
                str(Path(settings.DB_PATH)),
                limit=30,
                window_hours=settings.TRADE_SURFACE_TG_ALERTS_WINDOW_HOURS,
            )
        )
        candidates = select_trade_surface_alert_candidates(
            focus_payload,
            now_payload,
            max_candidates=max_candidates,
        )
        for idx, candidate in enumerate(candidates):
            outcome = await _send_claimed_alert(
                db,
                settings,
                session,
                candidate=candidate,
                window_hours=settings.TRADE_SURFACE_TG_ALERTS_DEDUP_HOURS,
            )
            if outcome == "blocked_dedup_24h":
                counts["blocked_dedup_24h"] += 1
                log.info(
                    "trade_surface_alert_suppressed",
                    token_id=candidate.token_id,
                    surface=candidate.surface,
                    reason="dedup_24h",
                )
                continue
            if outcome == "sent":
                counts["sent"] += 1
            elif outcome == "dispatch_failed":
                counts["dispatch_failed"] += 1
            if (
                idx < len(candidates) - 1
                and outcome in {"sent", "dispatch_failed"}
                and settings.TRADE_SURFACE_TG_ALERTS_SEND_SPACING_SECONDS > 0
            ):
                await asyncio.sleep(
                    settings.TRADE_SURFACE_TG_ALERTS_SEND_SPACING_SECONDS
                )
        return counts
    except Exception:
        log.exception("trade_surface_alerts_unexpected_error")
        return counts
