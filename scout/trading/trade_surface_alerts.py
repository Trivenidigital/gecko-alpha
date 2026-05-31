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


async def _count_sent_today(db: Database) -> int:
    if db._conn is None:
        return 0
    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM tg_alert_log "
        "WHERE signal_type = ? AND outcome = 'sent' AND alerted_at >= ?",
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
    """Claim, send, and commit under one DB lock.

    This prevents a failed surface alert from briefly looking like a durable
    `sent` row to concurrent paper-trade dispatch. The lock is held across a
    Telegram call, but this opt-in lane is capped to 5/day.
    """
    if db._conn is None:
        return "dispatch_failed"
    detail = {
        "surface": candidate.surface,
        "source_corpus": candidate.source_corpus,
        "verdict": candidate.verdict,
        "reasons": list(candidate.reasons),
    }
    async with db._txn_lock:
        now_iso = datetime.now(timezone.utc).isoformat()
        sent_row_id: int | None = None
        if window_hours > 0:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=window_hours)
            ).isoformat()
            cur = await db._conn.execute(
                "INSERT INTO tg_alert_log "
                "(paper_trade_id, signal_type, token_id, alerted_at, outcome, detail) "
                "SELECT NULL, ?, ?, ?, 'sent', ? "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM tg_alert_log "
                "  WHERE token_id = ? AND outcome = 'sent' "
                "  AND alerted_at >= ?"
                ") RETURNING id",
                (
                    SIGNAL_TYPE,
                    candidate.token_id,
                    now_iso,
                    json.dumps(detail, sort_keys=True),
                    candidate.token_id,
                    cutoff,
                ),
            )
            row = await cur.fetchone()
            if row is None:
                await db._conn.execute(
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
                await db._conn.commit()
                return "blocked_dedup_24h"
            sent_row_id = int(row[0])
        else:
            cur = await db._conn.execute(
                "INSERT INTO tg_alert_log "
                "(paper_trade_id, signal_type, token_id, alerted_at, outcome, detail) "
                "VALUES (NULL, ?, ?, ?, 'sent', ?) RETURNING id",
                (
                    SIGNAL_TYPE,
                    candidate.token_id,
                    now_iso,
                    json.dumps(detail, sort_keys=True),
                ),
            )
            row = await cur.fetchone()
            sent_row_id = int(row[0]) if row else None

        try:
            body = format_trade_surface_alert(candidate)
            log.info(
                "trade_surface_alert_dispatched",
                tg_alert_log_id=sent_row_id,
                token_id=candidate.token_id,
                surface=candidate.surface,
            )
            await alerter.send_telegram_message(
                body,
                session,
                settings,
                parse_mode=None,
                raise_on_failure=True,
                source="trade_surface_alerts",
            )
            await db._conn.commit()
            log.info(
                "trade_surface_alert_delivered",
                tg_alert_log_id=sent_row_id,
                token_id=candidate.token_id,
                surface=candidate.surface,
            )
            return "sent"
        except asyncio.CancelledError:
            if sent_row_id is not None:
                await db._conn.execute(
                    "UPDATE tg_alert_log "
                    "SET outcome='dispatch_failed', detail=? WHERE id=?",
                    (
                        json.dumps(
                            {**detail, "error": "cancelled_during_telegram_send"},
                            sort_keys=True,
                        ),
                        sent_row_id,
                    ),
                )
                await db._conn.commit()
            raise
        except Exception as exc:
            if sent_row_id is not None:
                await db._conn.execute(
                    "UPDATE tg_alert_log "
                    "SET outcome='dispatch_failed', detail=? WHERE id=?",
                    (
                        json.dumps(
                            {**detail, "error": str(exc)[:200]},
                            sort_keys=True,
                        ),
                        sent_row_id,
                    ),
                )
            await db._conn.commit()
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
        sent_today = await _count_sent_today(db)
        remaining = max(0, settings.TRADE_SURFACE_TG_ALERTS_MAX_PER_DAY - sent_today)
        if remaining <= 0:
            log.info("trade_surface_alert_daily_cap_reached", sent_today=sent_today)
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
        for candidate in candidates:
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
        return counts
    except Exception:
        log.exception("trade_surface_alerts_unexpected_error")
        return counts
