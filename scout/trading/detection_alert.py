"""ALR-02: detection-time alert lane.

Fires an "early candidate detected" Telegram alert on the SCORING pass —
BEFORE the paper dispatch gate (which rejects ~99.99% of scored candidates)
decides. This reframes the operator alert from "the robot acted" (what the
paper-open lane in ``tg_alert_dispatch.py`` says) to "an early candidate is
here", serving the product's central promise: beat CoinGecko Highlights by
minutes.

Design (tasks/design_detection_time_alert_lane.md). The lane is a composition
of EXISTING primitives — no new table, no schema_version, no CHECK change:

- trigger  = CG-sourced + fresh (candidates.first_seen_at within
  DETECTION_ALERT_MAX_AGE_MIN) + early vs CG trending
  (engine._compute_lead_time_vs_trending: no_reference, or ok+negative lead).
- universe = tg_alert_dispatch._check_universe (reused verbatim).
- dedup    = per-token 24h over sent detection_lane rows
  (TG_ALERT_DEDUP_WINDOW_HOURS; 0 disables).
- gate     = ALR-02 quality gate (quant_score >= DETECTION_ALERT_MIN_QUANT_SCORE),
  applied BEFORE the daily cap.
- budget   = DETECTION_ALERT_MAX_PER_DAY sent rows / UTC day, spent
  highest-score-first (freshest breaks ties).
- audit    = one tg_alert_log row per decision, signal_type='detection_lane',
  detail='detection_lane[:reason]', paper_trade_id=NULL.

``notify_early_detections`` NEVER raises — a bug here can never break the
pipeline cycle. It is spawned fire-and-forget from scout/main.py::run_cycle.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

from scout import alerter
from scout.config import Settings
from scout.db import Database
from scout.trading.engine import _compute_lead_time_vs_trending

# Reuse the paper-lane formatters + universe guard verbatim (card-v2 parity).
from scout.trading.tg_alert_dispatch import (
    _check_universe,
    _fmt_mcap,
    _fmt_price,
)

log = structlog.get_logger(__name__)


def _detection_trigger(
    lead_time_min: float | None, lead_time_status: str | None
) -> bool:
    """True when the candidate is EARLY relative to CG trending.

    Sign convention (matches engine._compute_lead_time_vs_trending, and the
    codebase has a documented history of sign-flip bugs on this column):
    NEGATIVE lead_time = detected BEFORE the coin trended (early / good);
    POSITIVE = detected AFTER (late). So the lane fires when:

      - status == 'no_reference': the coin has NEVER appeared on CG trending
        (we are entirely ahead of the crossover), OR
      - status == 'ok' AND lead_time_min < 0: a trending crossover exists but
        it is LATER than the detection instant (still early).

    'ok' with lead_time >= 0 (already trending / late) and 'error' do NOT fire.
    """
    if lead_time_status == "no_reference":
        return True
    if lead_time_status == "ok" and lead_time_min is not None and lead_time_min < 0:
        return True
    return False


def _passes_quality_gate(cand, settings: Settings) -> bool:
    """True when a candidate's quant_score clears the ALR-02 quality bar.

    Applied BEFORE the scarce daily budget is spent, so the cap is filled with
    the highest-quality early candidates rather than merely the freshest. The
    ALR-02 evaluation (2026-07-11→07-14) found the ungated lane spent every
    slot on quant_score=0 candidates (0/20 ever trended) while genuine
    pre-trending catches — which DID fire scoring signals — were never sent.

    Single source of truth: quant_score >= DETECTION_ALERT_MIN_QUANT_SCORE.
    Because every scoring signal contributes positive points, quant_score == 0
    iff no signal fired, so the default bar of 1 is exactly "at least one
    scoring signal fired" (the validated coarse gate). A None score (un-scored
    candidate) reads as 0 and is blocked.
    """
    score = int(getattr(cand, "quant_score", None) or 0)
    return score >= settings.DETECTION_ALERT_MIN_QUANT_SCORE


def _fmt_detection_line(
    first_seen_min_ago: float | None,
    lead_time_min: float | None,
    lead_time_status: str | None,
) -> str:
    """Freshness + earliness line: 'first seen N min ago · <reference>'."""
    if first_seen_min_ago is None:
        seen = "first seen just now"
    else:
        seen = f"first seen {max(0, int(round(first_seen_min_ago)))} min ago"
    if lead_time_status == "ok" and lead_time_min is not None and lead_time_min < 0:
        ahead = abs(int(round(lead_time_min)))
        ref = f"{ahead} min ahead of CG trending"
    else:
        ref = "not yet on CG trending"
    return f"{seen} · {ref}"


def _build_token_deep_link(dashboard_base_url: str | None, coin_id: str) -> str | None:
    """ALR-09 dashboard deep link to the per-token page (no trade row exists).

    Returns None (line omitted) when the base URL is empty (operator
    off-switch).
    """
    if not dashboard_base_url:
        return None
    return f"{dashboard_base_url.rstrip('/')}/#/token/{coin_id}"


def format_detection_alert(
    *,
    symbol: str,
    coin_id: str,
    price: float | None,
    mcap: float | None,
    first_seen_min_ago: float | None,
    lead_time_min: float | None,
    lead_time_status: str | None,
    dashboard_base_url: str | None = None,
) -> str:
    """Plain-text Telegram body for an early-detection alert.

    Caller MUST dispatch with parse_mode=None (plain text; global CLAUDE.md
    §12b). Reuses _fmt_price / _fmt_mcap from the paper lane for card-v2
    parity.
    """
    header = f"🔎 EARLY DETECT · {symbol} · {_fmt_price(price)} · {_fmt_mcap(mcap)}"
    parts = [
        header,
        _fmt_detection_line(first_seen_min_ago, lead_time_min, lead_time_status),
        f"coingecko.com/en/coins/{coin_id}",
    ]
    deep_link = _build_token_deep_link(dashboard_base_url, coin_id)
    if deep_link is not None:
        parts.append(f"Dashboard: {deep_link}")
    return "\n".join(parts)


def _age_minutes(iso: str | None, now: datetime) -> float | None:
    """Minutes between an ISO timestamp and ``now``. None on parse failure."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 60.0


async def _fetch_first_seen_mcap(
    db: Database, token_id: str
) -> tuple[str | None, float | None]:
    """Authoritative first_seen_at + mcap from the candidates row. Fail-soft.

    The candidates upsert preserves the EARLIEST sighting (db.py:7153), so the
    persisted first_seen_at is the true detection time — unlike the in-memory
    CandidateToken, whose first_seen_at defaults to construction time.
    """
    try:
        cur = await db._conn.execute(
            "SELECT first_seen_at, market_cap_usd FROM candidates "
            "WHERE contract_address = ?",
            (token_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return (None, None)
        return (row[0], row[1])
    except Exception:
        log.exception("detection_alert_first_seen_fetch_failed", token_id=token_id)
        return (None, None)


async def _fetch_price_mcap(
    db: Database, token_id: str
) -> tuple[float | None, float | None]:
    """Current price + mcap from price_cache. Fail-soft (None → '$0'/'$?')."""
    try:
        cur = await db._conn.execute(
            "SELECT current_price, market_cap FROM price_cache WHERE coin_id = ?",
            (token_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return (None, None)
        return (row[0], row[1])
    except Exception:
        log.exception("detection_alert_price_fetch_failed", token_id=token_id)
        return (None, None)


async def _count_sent_today(db: Database, now: datetime) -> int:
    """Number of detection-lane alerts sent since UTC midnight."""
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM tg_alert_log "
        "WHERE outcome = 'sent' AND detail = 'detection_lane' "
        "AND alerted_at >= ?",
        (today_start,),
    )
    row = await cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


async def _check_detection_dedup(
    db: Database, settings: Settings, token_id: str, now: datetime
) -> bool:
    """True if a sent detection-lane alert exists for this token in the 24h
    window (block the re-send). Scoped to detail='detection_lane' so the lane
    is independent of the paper-open alert lane. window <= 0 disables dedup.
    """
    window = settings.TG_ALERT_DEDUP_WINDOW_HOURS
    if window <= 0:
        return False
    cutoff = (now - timedelta(hours=window)).isoformat()
    cur = await db._conn.execute(
        "SELECT 1 FROM tg_alert_log "
        "WHERE token_id = ? AND outcome = 'sent' AND detail = 'detection_lane' "
        "AND alerted_at >= ? LIMIT 1",
        (token_id, cutoff),
    )
    return (await cur.fetchone()) is not None


async def _log_detection_outcome(
    db: Database,
    *,
    token_id: str,
    outcome: str,
    detail: str,
    now: datetime | None = None,
) -> None:
    """Write one tg_alert_log audit row. signal_type='detection_lane',
    paper_trade_id=NULL (there is no trade yet — the point of the lane)."""
    if db._conn is None:
        return
    alerted_at = (now or datetime.now(timezone.utc)).isoformat()
    async with db._txn_lock:
        await db._conn.execute(
            "INSERT INTO tg_alert_log "
            "(paper_trade_id, signal_type, token_id, alerted_at, outcome, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (None, "detection_lane", token_id, alerted_at, outcome, detail),
        )
        await db._conn.commit()


async def notify_early_detections(
    db: Database,
    settings: Settings,
    session,
    *,
    candidates,
    now: datetime | None = None,
) -> None:
    """Fire early-detection alerts for a cycle's scored candidates (best-effort).

    Never raises. Spawned fire-and-forget from run_cycle. ``candidates`` is the
    cycle's list of scored CandidateToken objects; the lane reads the
    authoritative first_seen_at / price from the DB (not the in-memory model),
    but the quality gate reads the in-memory quant_score / signals_fired (the
    scores computed THIS cycle at detection). Candidates that clear the ALR-02
    quality gate are spent highest-score-first, bounded by
    DETECTION_ALERT_MAX_PER_DAY.
    """
    if not settings.DETECTION_ALERT_LANE_ENABLED:
        return
    if db._conn is None:
        log.warning("detection_alert_no_conn")
        return

    now = now or datetime.now(timezone.utc)
    try:
        remaining = settings.DETECTION_ALERT_MAX_PER_DAY - await _count_sent_today(
            db, now
        )
        if remaining <= 0:
            # Cap already spent — do NOT early-return: overflow candidates that
            # WOULD have fired are still audited as detection_lane:rate_limit so
            # the suppression is quantifiable.
            log.info(
                "detection_alert_daily_cap_reached",
                cap=settings.DETECTION_ALERT_MAX_PER_DAY,
            )

        # Collect CG-sourced, fresh candidates that clear the ALR-02 quality
        # gate; then spend the scarce daily budget HIGHEST-SCORE-FIRST (freshest
        # breaks ties). ``pool`` counts CG-sourced fresh candidates before the
        # gate so the pool→gated→sent funnel is queryable per run.
        pool = 0
        entries: list[tuple[int, float, object, float | None]] = []
        for cand in candidates:
            if getattr(cand, "chain", None) != "coingecko":
                continue
            token_id = getattr(cand, "contract_address", None)
            if not token_id:
                continue
            first_seen_iso, mcap_db = await _fetch_first_seen_mcap(db, token_id)
            age_min = _age_minutes(first_seen_iso, now)
            if age_min is None or age_min > settings.DETECTION_ALERT_MAX_AGE_MIN:
                continue
            pool += 1
            # ALR-02 quality gate — upstream of universe/trigger/dedup/cap.
            if not _passes_quality_gate(cand, settings):
                continue
            quant_score = int(getattr(cand, "quant_score", None) or 0)
            entries.append((quant_score, age_min, cand, mcap_db))
        # Highest score first; freshest (smallest age) breaks ties. NOTE: this
        # ordering only changes WHICH candidates win slots when the gated pool
        # exceeds the cap. At the default bar (>=1) the gated pool is ~4/day —
        # below DETECTION_ALERT_MAX_PER_DAY=5 — so ordering is not load-bearing
        # until the operator loosens the gate or CG detection volume rises.
        entries.sort(key=lambda e: (-e[0], e[1]))

        sent_count = 0
        for quant_score, age_min, cand, mcap_db in entries:
            token_id = cand.contract_address

            # Universe filter (reused verbatim). Off when the flag is off.
            pattern = _check_universe(settings, token_id)
            if pattern is not None:
                await _log_detection_outcome(
                    db,
                    token_id=token_id,
                    outcome="blocked_eligibility",
                    detail=f"detection_lane:universe_filter:{pattern}",
                    now=now,
                )
                log.info(
                    "detection_alert_blocked_universe",
                    token_id=token_id,
                    pattern=pattern,
                )
                continue

            # Trigger: early vs CG trending.
            lead_time_min, status = await _compute_lead_time_vs_trending(
                db, token_id, now
            )
            if not _detection_trigger(lead_time_min, status):
                continue

            # Per-token 24h dedup (scoped to the detection lane).
            if await _check_detection_dedup(db, settings, token_id, now):
                await _log_detection_outcome(
                    db,
                    token_id=token_id,
                    outcome="blocked_cooldown",
                    detail="detection_lane:dedup_24h",
                    now=now,
                )
                continue

            # Daily budget guard (freshest-first already ordered above).
            if remaining <= 0:
                await _log_detection_outcome(
                    db,
                    token_id=token_id,
                    outcome="blocked_cooldown",
                    detail="detection_lane:rate_limit",
                    now=now,
                )
                continue

            price, mcap_pc = await _fetch_price_mcap(db, token_id)
            mcap = mcap_db if mcap_db else mcap_pc
            body = format_detection_alert(
                symbol=getattr(cand, "ticker", "") or "",
                coin_id=token_id,
                price=price,
                mcap=mcap,
                first_seen_min_ago=age_min,
                lead_time_min=lead_time_min,
                lead_time_status=status,
                dashboard_base_url=settings.DASHBOARD_BASE_URL,
            )
            # §12b: bracket the send with dispatched/delivered logs so every
            # fire is traceable regardless of delivery outcome.
            log.info("detection_alert_dispatched", token_id=token_id)
            try:
                await alerter.send_telegram_message(
                    body,
                    session,
                    settings,
                    parse_mode=None,
                    raise_on_failure=True,
                    source="detection_alert",
                )
            except Exception as e:
                log.warning(
                    "detection_alert_dispatch_failed",
                    token_id=token_id,
                    err=str(e),
                )
                # A failed send neither burns budget nor claims dedup.
                await _log_detection_outcome(
                    db,
                    token_id=token_id,
                    outcome="dispatch_failed",
                    detail="detection_lane",
                    now=now,
                )
                continue
            log.info("detection_alert_delivered", token_id=token_id)
            await _log_detection_outcome(
                db,
                token_id=token_id,
                outcome="sent",
                detail="detection_lane",
                now=now,
            )
            remaining -= 1
            sent_count += 1

        # Queryable per-run funnel: how many CG-fresh candidates entered the
        # pool, how many the quality gate dropped, how many were eligible, and
        # how many were actually sent within the cap.
        log.info(
            "detection_alert_funnel",
            pool=pool,
            gated_out=pool - len(entries),
            eligible=len(entries),
            sent=sent_count,
            cap=settings.DETECTION_ALERT_MAX_PER_DAY,
        )
    except Exception:
        # Belt-and-braces: the lane must never break the pipeline cycle.
        log.exception("detection_alert_notify_unexpected_error")
