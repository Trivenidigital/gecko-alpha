"""Weekly alerts scoreboard (ALR-04).

Closes the sent-alert -> outcome feedback loop that ``weekly_digest`` (combo-
oriented) does not cover. Joins ``tg_alert_log`` rows with ``outcome='sent'``
to their linked ``paper_trades`` (via ``paper_trade_id``) over a trailing
window and reports, per signal_type: sent-count, linked outcome distribution
(win/loss/open), total PnL of alerted trades, best hit, worst giveback, and the
unlinked count.

Read-only. n-gated: below ``min_linked`` linked trades the derived stats are
withheld behind an ``INSUFFICIENT_DATA`` line (the raw sent/linked/unlinked
counts are always shown). Plain-text send is gated behind
``WEEKLY_ALERTS_SCOREBOARD_ENABLED`` (default False) and wired on the same
weekly tick as ``weekly_digest`` (scout/main.py). parse_mode=None throughout —
signal names contain underscores that MarkdownV1 would mangle (CLAUDE.md §12b).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import aiohttp
import structlog

from scout import alerter
from scout.config import Settings
from scout.db import Database
from scout.trading.weekly_digest import _split_for_telegram

log = structlog.get_logger()

_TG_SPLIT_LIMIT = 4000  # headroom under Telegram's 4096 cap (mirrors weekly_digest)


def _fmt_signed_usd(value: float) -> str:
    return f"${value:+,.2f}"


def _fmt_signed_pct(value: float) -> str:
    return f"{value:+.0f}%"


async def build_alerts_scoreboard(
    db: Database,
    window_days: int = 7,
    *,
    min_linked: int = 5,
    now: datetime | None = None,
) -> str:
    """Build the weekly alerts scoreboard text.

    Joins ``tg_alert_log(outcome='sent')`` to ``paper_trades`` via
    ``paper_trade_id`` over the trailing ``window_days``. Always returns a
    string (the caller decides whether to send). When fewer than ``min_linked``
    sent alerts resolve to a paper trade, the per-signal / PnL / best-hit /
    worst-giveback stats are withheld behind an ``INSUFFICIENT_DATA`` line.

    ``now`` defaults to the current UTC time; tests pass a fixed value so the
    date-window header is deterministic (mirrors ``weekly_digest``'s
    ``end_date`` parameter).
    """
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=window_days)

    # alerted_at is stored as datetime.now(timezone.utc).isoformat() ('T' form)
    # at every writer (tg_alert_dispatch / trade_surface_alerts / main). The
    # bound is the same isoformat 'T' form, so lexicographic >= is a correct
    # chronological compare (no T-vs-space mixing — global CLAUDE.md INF-04).
    cur = await db._conn.execute(
        """
        SELECT
            a.signal_type   AS alert_signal,
            a.paper_trade_id AS ptid,
            p.id            AS pid,
            p.symbol        AS symbol,
            p.status        AS status,
            p.pnl_usd       AS pnl_usd,
            p.pnl_pct       AS pnl_pct,
            p.peak_pct      AS peak_pct
        FROM tg_alert_log a
        LEFT JOIN paper_trades p ON p.id = a.paper_trade_id
        WHERE a.outcome = 'sent'
          AND a.alerted_at >= ?
        ORDER BY a.id
        """,
        (start.isoformat(),),
    )
    rows = await cur.fetchall()

    sent = len(rows)
    linked_rows = [r for r in rows if r["pid"] is not None]
    unlinked = sent - len(linked_rows)
    linked = len(linked_rows)

    lines: list[str] = []
    lines.append(
        f"Weekly Alerts Scoreboard - {start.date().isoformat()} "
        f"to {now.date().isoformat()}"
    )
    lines.append(f"sent alerts -> paper-trade outcomes (last {window_days}d)")
    lines.append("")
    lines.append(f"Sent {sent}  |  Linked {linked}  |  Unlinked {unlinked}")

    if linked < min_linked:
        lines.append("")
        lines.append(
            f"INSUFFICIENT_DATA - {linked} linked < MIN {min_linked}; "
            "alert->outcome stats withheld."
        )
        return "\n".join(lines)

    # --- Per-signal aggregation (alert-row level for sent/linked counts) ---
    per_signal: dict[str, dict] = {}
    for r in rows:
        sig = r["alert_signal"] or "unknown"
        agg = per_signal.setdefault(sig, {"sent": 0, "linked": 0, "trades": {}})
        agg["sent"] += 1
        if r["pid"] is not None:
            agg["linked"] += 1
            # Dedupe outcome/PnL by paper_trade_id so a token with two sent
            # alerts (rare under 24h dedup) can't double-count its PnL.
            agg["trades"][r["pid"]] = r

    def _classify(row) -> str:
        status = (row["status"] or "").lower()
        if status.startswith("closed"):
            return "win" if (row["pnl_usd"] or 0) > 0 else "loss"
        return "open"

    lines.append("")
    lines.append("Per-signal (sent / linked / linked-PnL / W-L-O):")
    for sig in sorted(per_signal, key=lambda s: (-per_signal[s]["sent"], s)):
        agg = per_signal[sig]
        trades = list(agg["trades"].values())
        w = sum(1 for t in trades if _classify(t) == "win")
        lo = sum(1 for t in trades if _classify(t) == "loss")
        op = sum(1 for t in trades if _classify(t) == "open")
        pnl = sum((t["pnl_usd"] or 0.0) for t in trades if _classify(t) != "open")
        lines.append(
            "  {:<24s} {:>3d} / {:>3d}   {:>12s}   {}-{}-{}".format(
                sig, agg["sent"], agg["linked"], _fmt_signed_usd(pnl), w, lo, op
            )
        )

    # --- Totals over DISTINCT linked trades ---
    distinct: dict[int, object] = {}
    for r in linked_rows:
        distinct.setdefault(r["pid"], r)
    trades = list(distinct.values())

    closed = [t for t in trades if (t["status"] or "").lower().startswith("closed")]
    total_pnl = sum((t["pnl_usd"] or 0.0) for t in closed)

    # Best hit: highest peak_pct among linked trades that reached a peak.
    peaked = [t for t in trades if t["peak_pct"] is not None]
    best = (
        max(peaked, key=lambda t: (t["peak_pct"], t["symbol"] or ""))
        if peaked
        else None
    )

    # Worst giveback: largest (peak_pct - pnl_pct) among closed linked trades
    # that gave something back (peak strictly above the realized exit).
    givebacks = [
        t
        for t in closed
        if t["peak_pct"] is not None
        and t["pnl_pct"] is not None
        and t["peak_pct"] > t["pnl_pct"]
    ]
    worst = (
        max(givebacks, key=lambda t: (t["peak_pct"] - t["pnl_pct"], t["symbol"] or ""))
        if givebacks
        else None
    )

    lines.append("")
    lines.append("Totals (linked)")
    lines.append(f"  Closed: {len(closed)}   PnL: {_fmt_signed_usd(total_pnl)}")
    if best is not None:
        lines.append(
            f"  Best hit:       {best['symbol']} peak {_fmt_signed_pct(best['peak_pct'])}"
        )
    else:
        lines.append("  Best hit:       n/a")
    if worst is not None:
        gb = worst["peak_pct"] - worst["pnl_pct"]
        lines.append(
            "  Worst giveback: {} peak {} -> exit {} ({:.0f}pp given back)".format(
                worst["symbol"],
                _fmt_signed_pct(worst["peak_pct"]),
                _fmt_signed_pct(worst["pnl_pct"]),
                gb,
            )
        )
    else:
        lines.append("  Worst giveback: n/a")

    return "\n".join(lines)


async def send_alerts_scoreboard(db: Database, settings: Settings) -> None:
    """Orchestrator: build + send the scoreboard via alerter. Never silent.

    Mirrors ``weekly_digest.send_weekly_digest`` — opens one aiohttp session,
    sends plain-text (parse_mode=None) with §12b dispatched/delivered logs, and
    falls back to a plain error ping if the build/send raises.
    """
    corr = f"asb-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{secrets.token_hex(2)}"
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=15)
    ) as session:
        try:
            text = await build_alerts_scoreboard(
                db,
                window_days=settings.WEEKLY_ALERTS_SCOREBOARD_WINDOW_DAYS,
                min_linked=settings.WEEKLY_ALERTS_SCOREBOARD_MIN_LINKED,
            )
            chunks = _split_for_telegram(text, _TG_SPLIT_LIMIT)
            if not chunks:
                log.error("alerts_scoreboard_produced_no_chunks", text_len=len(text))
                return
            log.info("alerts_scoreboard_alert_dispatched", bytes=len(text))
            for chunk in chunks:
                await alerter.send_telegram_message(
                    chunk,
                    session,
                    settings,
                    parse_mode=None,
                    source="weekly_alerts_scoreboard",
                )
            log.info("alerts_scoreboard_alert_delivered", bytes=len(text))
        except Exception as e:
            log.exception("alerts_scoreboard_failed", corr=corr)
            try:
                await alerter.send_telegram_message(
                    f"Weekly alerts scoreboard failed: {type(e).__name__} "
                    f"[ref={corr}]. Check logs.",
                    session,
                    settings,
                    parse_mode=None,
                    source="weekly_alerts_scoreboard",
                )
            except Exception:
                log.exception("alerts_scoreboard_fallback_dispatch_error", corr=corr)
