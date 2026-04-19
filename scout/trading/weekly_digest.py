"""Weekly digest builder + sender (spec §5.4)."""

from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta, timezone

import aiohttp
import structlog

from scout import alerter
from scout.db import Database
from scout.trading import analytics

log = structlog.get_logger()

_TG_SPLIT_LIMIT = 4000  # leave headroom under Telegram's 4096 cap


async def _try_section(section_name: str, coro):
    """Wrap one digest section so a failure in it can't kill the whole digest.

    Returns (content_lines, ok). On error, returns a single '(error: …)' line
    and logs with the section name so operators see which section failed."""
    try:
        return (await coro, True)
    except Exception as e:
        log.exception("weekly_digest_section_failed", section=section_name)
        return ([f"  (error)"], False)


async def build_weekly_digest(
    db: Database,
    end_date: date,
    settings,
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

    fallback_count = len(_supp._fallback_timestamps)
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

    return "\n".join(lines)


async def send_weekly_digest(db: Database, settings) -> None:
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

            for chunk in _split_for_telegram(text, _TG_SPLIT_LIMIT):
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
    """Split on newline boundaries. Never splits mid-line."""
    if len(text) <= limit:
        return [text]
    lines = text.split("\n")
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in lines:
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
    return chunks
