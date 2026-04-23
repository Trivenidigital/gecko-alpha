"""Weekly digest builder + sender (spec §5.4)."""

from __future__ import annotations

import math
import secrets
from datetime import date, datetime, timedelta, timezone

import aiohttp
import structlog

from scout import alerter
from scout.config import Settings
from scout.db import Database
from scout.trading import analytics

log = structlog.get_logger()

_TG_SPLIT_LIMIT = 4000  # leave headroom under Telegram's 4096 cap

CLOSED_COUNTABLE_STATUSES = (
    "closed_tp",
    "closed_sl",
    "closed_expired",
    "closed_trailing_stop",
)

# BL-060 A/B scope: signal types that participate in the live-eligible vs
# beyond-cap comparison. Must stay in sync with `execute_buy` call sites that
# pass a non-zero min_quant_score; add new signal types here when they start
# stamping would_be_live (rather than NULL).
BL060_AB_SIGNAL_TYPES = (
    "first_signal",
    "trending_catch",
    "volume_spike",
    "losers_contrarian",
    "gainers_early",
    "narrative_prediction",
    "chain_completed",
    "long_hold",
)


def _fmt_pct(x):
    return f"{x:+.1f}%" if x is not None else "-"


def _fmt_wr(x):
    return f"{x:.1f}%" if x is not None else "-"


def _fmt_sharpe(x, n):
    if x is None or n == 0:
        return "-"
    if n < 30:
        return f"{x:.2f} (n_closed={n}, noisy)"
    return f"{x:.2f}"


def _render_cohort(label, this_w, prev_w) -> str:
    lines = [f"{label}:"]
    lines.append(
        f"  Win-rate:  {_fmt_wr(this_w['win_rate'])} this week "
        f"| {_fmt_wr(prev_w['win_rate'])} last week   "
        f"(n_closed={this_w['n']} | {prev_w['n'] if prev_w['n'] else '-'})"
    )
    lines.append(
        f"  Avg P&L:   {_fmt_pct(this_w['avg_pnl'])} this week "
        f"| {_fmt_pct(prev_w['avg_pnl'])} last week   "
        f"(n_closed={this_w['n']} | {prev_w['n'] if prev_w['n'] else '-'})"
    )
    lines.append(
        f"  Sharpe:    {_fmt_sharpe(this_w['sharpe'], this_w['n'])} this week "
        f"| {_fmt_sharpe(prev_w['sharpe'], prev_w['n'])} last week"
    )
    return "\n".join(lines)


def _render_delta(live_t, live_p, beyond_t, beyond_p) -> str:
    lines = ["Delta (live-eligible minus beyond-cap):"]
    if live_t["n"] == 0 or beyond_t["n"] == 0:
        lines.append("  - insufficient data for delta")
        return "\n".join(lines)

    def _prev_delta(lp, bp, key):
        if not lp["n"] or not bp["n"]:
            return "-"
        return f"{lp[key] - bp[key]:+.1f}pp"

    lines.append(
        f"  Win-rate:  "
        f"{live_t['win_rate'] - beyond_t['win_rate']:+.1f}pp this week "
        f"| {_prev_delta(live_p, beyond_p, 'win_rate')} last week"
    )
    lines.append(
        f"  Avg P&L:   "
        f"{live_t['avg_pnl'] - beyond_t['avg_pnl']:+.1f}pp this week "
        f"| {_prev_delta(live_p, beyond_p, 'avg_pnl')} last week"
    )
    # Delta excludes Sharpe when either side has n_closed < 30 (BL-060 test #18)
    if live_t["n"] >= 30 and beyond_t["n"] >= 30:
        lines.append(
            f"  Sharpe:    " f"{live_t['sharpe'] - beyond_t['sharpe']:+.2f} this week"
        )
    return "\n".join(lines)


async def _render_per_path(db, start, end) -> str:
    placeholders = ",".join("?" * len(CLOSED_COUNTABLE_STATUSES))
    cur = await db._conn.execute(
        f"""
        SELECT signal_type,
               COUNT(*) AS n,
               AVG(CASE WHEN pnl_pct > 0 THEN 1.0 ELSE 0.0 END) * 100 AS wr,
               AVG(pnl_pct) AS avg
        FROM paper_trades
        WHERE status IN ({placeholders})
          AND would_be_live = 1
          AND opened_at >= ?
          AND opened_at < ?
        GROUP BY signal_type
        ORDER BY n DESC
        """,
        (*CLOSED_COUNTABLE_STATUSES, start.isoformat(), end.isoformat()),
    )
    rows = await cur.fetchall()
    if not rows:
        return "Per-path within live-eligible cohort: (no closed trades)"
    lines = ["Per-path within live-eligible cohort:"]
    for sig, n, wr, avg in rows:
        suffix = "  <- small-n caveat" if n < 20 else ""
        lines.append(
            f"  {sig:24s} {wr:.1f}% win, {avg:+.1f}% avg  (n_closed={n})" + suffix
        )
    return "\n".join(lines)


async def _build_bl060_ab(db, end_date, settings) -> str:
    """Two-week side-by-side A/B for BL-060 live-eligible cohort."""
    this_start = end_date - timedelta(days=7)
    prev_start = end_date - timedelta(days=14)
    live_eligible_cap = settings.PAPER_LIVE_ELIGIBLE_CAP

    async def cohort_stats(wbl: int, start, end):
        status_placeholders = ",".join("?" * len(CLOSED_COUNTABLE_STATUSES))
        signal_placeholders = ",".join("?" * len(BL060_AB_SIGNAL_TYPES))
        cur = await db._conn.execute(
            f"""
            SELECT pnl_pct FROM paper_trades
            WHERE signal_type IN ({signal_placeholders})
              AND status IN ({status_placeholders})
              AND would_be_live = ?
              AND opened_at >= ?
              AND opened_at < ?
            """,
            (
                *BL060_AB_SIGNAL_TYPES,
                *CLOSED_COUNTABLE_STATUSES,
                wbl,
                start.isoformat(),
                end.isoformat(),
            ),
        )
        rows = await cur.fetchall()
        pnls = [r[0] for r in rows if r[0] is not None]
        n = len(pnls)
        if n == 0:
            return {"n": 0, "win_rate": None, "avg_pnl": None, "sharpe": None}
        wins = sum(1 for p in pnls if p > 0)
        avg = sum(pnls) / n
        if n >= 2:
            var = sum((p - avg) ** 2 for p in pnls) / (n - 1)
            sd = math.sqrt(var) if var > 0 else 0.0
            sharpe = (avg / sd) if sd > 0 else 0.0
        else:
            sharpe = 0.0
        return {
            "n": n,
            "win_rate": wins / n * 100,
            "avg_pnl": avg,
            "sharpe": sharpe,
        }

    live_this = await cohort_stats(1, this_start, end_date)
    live_prev = await cohort_stats(1, prev_start, this_start)
    beyond_this = await cohort_stats(0, this_start, end_date)
    beyond_prev = await cohort_stats(0, prev_start, this_start)

    # Context counts (open rows)
    cur = await db._conn.execute(
        "SELECT "
        "SUM(CASE WHEN would_be_live=1 THEN 1 ELSE 0 END) AS live_open, "
        "SUM(CASE WHEN would_be_live=0 THEN 1 ELSE 0 END) AS beyond_open, "
        "SUM(CASE WHEN would_be_live IS NULL THEN 1 ELSE 0 END) AS null_open "
        "FROM paper_trades WHERE status='open'"
    )
    ctx = await cur.fetchone()
    live_open = ctx[0] or 0
    beyond_open = ctx[1] or 0
    null_open = ctx[2] or 0

    out = []
    out.append("BL-060 A/B - live-eligible vs beyond-cap")
    out.append("=" * 41)

    def _as_date(d):
        return d.date() if isinstance(d, datetime) else d

    out.append(
        f"Window:  this week ({_as_date(this_start)} -> {_as_date(end_date)}) "
        f"vs last week ({_as_date(prev_start)} -> {_as_date(this_start)})"
    )
    out.append(
        f"Context: {live_open}/{live_eligible_cap} live-eligible open "
        f"| {beyond_open} beyond-cap open | {null_open} unscoped"
    )
    out.append("")
    out.append(
        _render_cohort(
            "LIVE-ELIGIBLE (would_be_live=1, closed only)", live_this, live_prev
        )
    )
    out.append("")
    out.append(
        _render_cohort(
            "BEYOND-CAP (would_be_live=0, closed only)", beyond_this, beyond_prev
        )
    )
    out.append("")
    out.append(_render_delta(live_this, live_prev, beyond_this, beyond_prev))
    out.append("")
    out.append(await _render_per_path(db, this_start, end_date))
    return "\n".join(out)


async def _try_section(section_name: str, coro):
    """Wrap one digest section so a failure in it can't kill the whole digest.

    Returns (content_lines, ok). On error, returns a single '(error: …)' line
    and logs with the section name so operators see which section failed."""
    try:
        return (await coro, True)
    except Exception as e:
        log.exception(
            "weekly_digest_section_failed",
            section=section_name,
            err_id="WEEKLY_DIGEST_SECTION",
        )
        return ([f"  (error: {type(e).__name__})"], False)


async def build_weekly_digest(
    db: Database,
    end_date: date,
    settings: Settings,
) -> str | None:
    """Build the weekly digest text. Returns None if zero activity last 7d."""
    start = datetime.combine(
        end_date - timedelta(days=7), datetime.min.time(), tzinfo=timezone.utc
    )
    end = datetime.combine(end_date, datetime.max.time(), tzinfo=timezone.utc)

    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE opened_at >= ?",
        (start.isoformat(),),
    )
    activity = (await cur.fetchone())[0] or 0
    cur = await db._conn.execute("SELECT COUNT(*) FROM combo_performance")
    combos_present = (await cur.fetchone())[0] or 0
    if activity == 0 and combos_present == 0:
        log.info("weekly_digest_empty", start=start.isoformat())
        return None

    lines: list[str] = []
    lines.append(
        f"Weekly Feedback — {(end_date - timedelta(days=7)).isoformat()} "
        f"to {end_date.isoformat()}"
    )
    lines.append("")

    # 1. Combo leaderboard
    async def _build_leaderboard():
        board = await analytics.combo_leaderboard(
            db,
            "30d",
            min_trades=settings.FEEDBACK_MIN_LEADERBOARD_TRADES,
        )
        out = []
        if not board:
            out.append("  (not enough data yet)")
        else:
            out.append("Top 5:")
            for r in board[:5]:
                flag = "  [SUPPRESSED]" if r.get("suppressed") else ""
                out.append(
                    "  {:<28s} {:5.1f}%  WR  ({} trades, ${:+.2f}){}".format(
                        r["combo_key"],
                        r["win_rate_pct"],
                        r["trades"],
                        r["total_pnl_usd"],
                        flag,
                    )
                )
            if len(board) > 5:
                out.append("Bottom 5:")
                for r in board[-5:]:
                    flag = "  [SUPPRESSED]" if r.get("suppressed") else ""
                    out.append(
                        "  {:<28s} {:5.1f}%  WR  ({} trades, ${:+.2f}){}".format(
                            r["combo_key"],
                            r["win_rate_pct"],
                            r["trades"],
                            r["total_pnl_usd"],
                            flag,
                        )
                    )
        return out

    lines.append(
        "[Combo leaderboard — 30d, min {} trades]".format(
            settings.FEEDBACK_MIN_LEADERBOARD_TRADES
        )
    )
    section_lines, _ = await _try_section("combo_leaderboard", _build_leaderboard())
    lines.extend(section_lines)
    lines.append("")

    # 2. Missed winners
    async def _build_missed():
        audit = await analytics.audit_missed_winners(db, start, end, settings)
        den = audit["denominator"]
        out = [
            f"{den['winners_missed']} missed out of {den['winners_total']} "
            f"qualifying winners "
            f"(mcap ≥ ${settings.FEEDBACK_MISSED_WINNER_MIN_MCAP:,.0f})",
        ]
        for tier in ("disaster_miss", "major_miss", "partial_miss"):
            entries = audit["tiers"][tier]
            if not entries:
                continue
            label = tier.replace("_", " ")
            out.append(f"  {label}: {len(entries)}")
            for e in entries[:5]:
                out.append(
                    "    {:<10s} +{:.0f}%   crossed {}".format(
                        e["symbol"],
                        e["peak_change"],
                        e["crossed_at"],
                    )
                )
        if audit["uncovered_window"]:
            out.append(
                f"  ⚠ pipeline gap {den['pipeline_gap_hours']:.1f}h — "
                f"{len(audit['uncovered_window'])} winners in "
                f"uncovered_window excluded"
            )
        return out

    lines.append(f"[Missed winners — last 7d]")
    section_lines, _ = await _try_section("missed_winners", _build_missed())
    lines.extend(section_lines)
    lines.append("")

    # 3. Lead-time
    async def _build_lead():
        breakdown = await analytics.lead_time_breakdown(db, "30d")
        out = []
        if not breakdown:
            out.append("  (no trades)")
        else:
            for sig in sorted(breakdown):
                b = breakdown[sig]
                med_str = (
                    "n/a" if b["median_min"] is None else f"{b['median_min']:+.1f} min"
                )
                out.append(
                    "  {:<18s} median {:<12s} (ok={}, no_ref={}, err={})".format(
                        sig,
                        med_str,
                        b["count_ok"],
                        b["count_no_reference"],
                        b["count_error"],
                    )
                )
        return out

    lines.append("[Lead-time — 30d, signal_type medians, 'ok' only]")
    section_lines, _ = await _try_section("lead_time", _build_lead())
    lines.extend(section_lines)
    lines.append("")

    # 4. Suppression log
    async def _build_supp():
        log_rows = await analytics.suppression_log(db, start, end)
        out = []
        if not log_rows:
            out.append("  (none)")
        else:
            for r in log_rows:
                out.append(
                    "  {:<24s} SUPPRESSED {} — WR {:.1f}% ({} trades), "
                    "parole until {}".format(
                        r["combo_key"],
                        r["suppressed_at"][:10],
                        r["win_rate_pct"],
                        r["trades"],
                        (r["parole_at"] or "n/a")[:10],
                    )
                )
        return out

    lines.append("[Suppression log — this week]")
    section_lines, _ = await _try_section("suppression_log", _build_supp())
    lines.extend(section_lines)
    lines.append("")

    # 5. Fallback counters — conditional (elide when zero).
    from scout.trading import suppression as _supp

    fallback_count = _supp.get_fallback_count()
    if fallback_count > 0:
        lines.append("[Fallback counters]")
        lines.append(f"  Suppression fail-opens: {fallback_count}")
        lines.append("")

    # 6. Chronic refresh failures
    async def _build_chronic():
        cur = await db._conn.execute(
            "SELECT combo_key, refresh_failures FROM combo_performance "
            "WHERE refresh_failures >= ? ORDER BY refresh_failures DESC",
            (settings.FEEDBACK_CHRONIC_FAILURE_THRESHOLD,),
        )
        chronic = await cur.fetchall()
        out = []
        if not chronic:
            out.append("  None")
        else:
            for c in chronic:
                out.append(
                    f"  {c['combo_key']} — {c['refresh_failures']} consecutive failures"
                )
        return out

    lines.append("[Chronic refresh failures]")
    section_lines, _ = await _try_section("chronic_refresh", _build_chronic())
    lines.extend(section_lines)
    lines.append("")

    # 7. BL-060 A/B — live-eligible vs beyond-cap, two-week WoW layout.
    lines.append("[BL-060 A/B — live-eligible vs beyond-cap]")
    section_lines, _ = await _try_section(
        "bl060_ab", _build_bl060_ab(db, end_date, settings)
    )
    lines.extend(section_lines)

    return "\n".join(lines)


async def send_weekly_digest(db: Database, settings: Settings) -> None:
    """Orchestrator: build + send via alerter. Never silent on error.

    Opens a single aiohttp.ClientSession for the lifetime of this dispatch.
    Matches alerter.send_telegram_message(text, session, settings) signature."""
    corr = f"wd-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{secrets.token_hex(2)}"
    async with aiohttp.ClientSession() as session:
        try:
            text = await build_weekly_digest(db, date.today(), settings)
            if text is None:
                log.info("weekly_digest_skipped_empty")
                return

            chunks = _split_for_telegram(text, _TG_SPLIT_LIMIT)
            if not chunks:
                # build_weekly_digest returned a non-None string but every
                # chunk was whitespace-only. Should not reach here in practice
                # (digest content is never all-whitespace), but if it does we
                # would log "weekly_digest_sent" with non-zero bytes while
                # dispatching nothing — surface the anomaly loudly instead.
                log.error("weekly_digest_produced_no_chunks", text_len=len(text))
                return

            for chunk in chunks:
                await alerter.send_telegram_message(chunk, session, settings)
            log.info("weekly_digest_sent", bytes=len(text))
        except Exception as e:
            log.exception("weekly_digest_failed", corr=corr)
            try:
                await alerter.send_telegram_message(
                    f"Weekly digest failed: {type(e).__name__} [ref={corr}]. Check logs.",
                    session,
                    settings,
                )
            except Exception:
                log.exception("weekly_digest_fallback_dispatch_error", corr=corr)


def _split_for_telegram(text: str, limit: int) -> list[str]:
    """Split on newline boundaries. Never splits mid-line.

    If a single line exceeds limit, hard-truncate it to prevent Telegram 400s.
    Always drops empty/whitespace-only chunks — Telegram sendMessage rejects
    empty text with HTTP 400, which would poison the whole digest dispatch."""
    if len(text) <= limit:
        return [text] if text.strip() else []
    lines = text.split("\n")
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in lines:
        # If a line exceeds limit on its own, hard-truncate it.
        if len(line) > limit:
            log.warning(
                "weekly_digest_line_hard_truncated",
                original_len=len(line),
                limit=limit,
            )
            line = line[:limit]
        # +1 for the joining "\n"
        if buf and size + len(line) + 1 > limit:
            chunks.append("\n".join(buf))
            buf = [line]
            size = len(line)
        else:
            buf.append(line)
            size += len(line) + (1 if len(buf) > 1 else 0)
    if buf:
        chunks.append("\n".join(buf))
    # Drop empty/whitespace-only chunks. A trailing newline past the limit would
    # otherwise produce a '' chunk and Telegram sendMessage rejects empty text
    # with HTTP 400, which would poison the whole digest dispatch.
    return [c for c in chunks if c.strip()]
