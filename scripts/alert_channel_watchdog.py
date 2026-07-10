#!/usr/bin/env python3
"""Alert-channel + daily-digest freshness watchdog (CLAUDE.md §12a).

Monitors TWO pipeline tables in ONE script (operator amendment):

  1. ``tg_alert_log`` — the latest ``outcome='sent'`` row must be newer than
     ``ALERT_SENT_SLO_HOURS`` (default 48). The Telegram alert channel went
     silent 2026-06-25 -> 07-08 (14 days, zero ``sent`` rows) and nobody
     noticed because no watchdog read this table.
  2. ``paper_daily_summary`` — ``MAX(date)`` must be within
     ``DIGEST_SUMMARY_SLO_DAYS`` (default 2; yesterday's row should land by
     ~02:00 UTC daily). The daily digest stopped writing after 2026-06-26.

On ANY breach the watchdog sends ONE plain-text Telegram message covering
every breached check (``parse_mode=None`` — §12b, table names contain ``_``
which MarkdownV1 would mangle), with §12b
``alert_channel_watchdog_alert_dispatched`` / ``_alert_delivered`` structured
logs around the send. A missing OR empty table is itself a breach with a
distinct message (silence is never ambiguous). Read-only on the DB.

DEPLOY-WITHOUT-ACTIVATE: an inert no-op unless ``--enabled`` is truthy (the
.sh wrapper wires it from the cron-env var ``ALERT_CHANNEL_WATCHDOG_ENABLED``
— NOT a Settings field, so this adds no config). ``--dry-run`` runs both
checks and prints the composed alert without sending (for tests / manual
verification); the disabled and dry-run paths never touch the network.

Exit codes:
  0 — ok (both fresh, or disabled no-op)
  5 — one or more freshness breaches (alert dispatched, or dry-run)
  1 — DB missing, runtime error, or alert-dispatch failure
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite
import structlog

structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))

_TRUTHY = {"1", "true", "yes", "on"}
_log = structlog.get_logger()


def _is_enabled(value: str) -> bool:
    return value.strip().lower() in _TRUTHY


def _parse_ts(raw: str) -> datetime:
    """Parse an ISO ``alerted_at`` value into a tz-aware UTC datetime."""
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _check_alert_sent_rate(
    conn: aiosqlite.Connection, slo_hours: int, now: datetime
) -> dict:
    """Latest tg_alert_log row with outcome='sent' must be within the SLO."""
    table = "tg_alert_log"
    try:
        cur = await conn.execute(
            "SELECT MAX(alerted_at) FROM tg_alert_log WHERE outcome = 'sent'"
        )
        row = await cur.fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return {
                "table": table,
                "status": "breach",
                "reason": "table_absent",
                "last_seen": None,
                "age_hours": None,
                "slo_hours": slo_hours,
            }
        raise
    last_seen = row[0] if row else None
    if last_seen is None:
        return {
            "table": table,
            "status": "breach",
            "reason": "no_sent_rows",
            "last_seen": None,
            "age_hours": None,
            "slo_hours": slo_hours,
        }
    age_hours = (now - _parse_ts(last_seen)).total_seconds() / 3600.0
    breached = age_hours > slo_hours
    return {
        "table": table,
        "status": "breach" if breached else "ok",
        "reason": "stale" if breached else "fresh",
        "last_seen": last_seen,
        "age_hours": round(age_hours, 2),
        "slo_hours": slo_hours,
    }


async def _check_digest_write_rate(
    conn: aiosqlite.Connection, slo_days: int, now: datetime
) -> dict:
    """MAX(date) in paper_daily_summary must be within the SLO (days)."""
    table = "paper_daily_summary"
    try:
        cur = await conn.execute("SELECT MAX(date) FROM paper_daily_summary")
        row = await cur.fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return {
                "table": table,
                "status": "breach",
                "reason": "table_absent",
                "last_seen": None,
                "age_days": None,
                "slo_days": slo_days,
            }
        raise
    last_seen = row[0] if row else None
    if last_seen is None:
        return {
            "table": table,
            "status": "breach",
            "reason": "no_summary_rows",
            "last_seen": None,
            "age_days": None,
            "slo_days": slo_days,
        }
    age_days = (now.date() - date.fromisoformat(last_seen[:10])).days
    breached = age_days > slo_days
    return {
        "table": table,
        "status": "breach" if breached else "ok",
        "reason": "stale" if breached else "fresh",
        "last_seen": last_seen,
        "age_days": age_days,
        "slo_days": slo_days,
    }


async def _evaluate(
    db_path: str, *, sent_slo_hours: int, digest_slo_days: int, now: datetime
) -> dict:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        alert = await _check_alert_sent_rate(conn, sent_slo_hours, now)
        digest = await _check_digest_write_rate(conn, digest_slo_days, now)
    return {"alert_sent_rate": alert, "digest_write_rate": digest}


def _compose_message(checks: dict) -> str:
    """Plain-text (no Markdown) operator alert naming each breached table."""
    lines = ["gecko-alpha alert-channel watchdog: freshness breach"]

    a = checks["alert_sent_rate"]
    if a["status"] == "breach":
        if a["reason"] == "table_absent":
            lines.append(
                "- tg_alert_log: table missing/absent — no alert-send audit "
                f"trail exists (SLO {a['slo_hours']}h)"
            )
        elif a["reason"] == "no_sent_rows":
            lines.append(
                "- tg_alert_log: NO 'sent' rows in table — the Telegram alert "
                f"channel has never sent or is fully dark (SLO {a['slo_hours']}h)"
            )
        else:
            lines.append(
                f"- tg_alert_log: last 'sent' alert at {a['last_seen']} "
                f"({a['age_hours']}h ago) exceeds SLO {a['slo_hours']}h — the "
                "alert channel is likely dead"
            )

    d = checks["digest_write_rate"]
    if d["status"] == "breach":
        if d["reason"] == "table_absent":
            lines.append(
                "- paper_daily_summary: table missing/absent — no daily-digest "
                f"audit trail exists (SLO {d['slo_days']}d)"
            )
        elif d["reason"] == "no_summary_rows":
            lines.append(
                "- paper_daily_summary: NO rows in table — the daily digest has "
                f"never been written (SLO {d['slo_days']}d)"
            )
        else:
            lines.append(
                f"- paper_daily_summary: last digest date {d['last_seen']} "
                f"({d['age_days']}d ago) exceeds SLO {d['slo_days']}d — the daily "
                "digest writer has stalled"
            )

    lines.append("Check the pipeline/digest cron and the Telegram delivery path.")
    return "\n".join(lines)


async def _dispatch_alert(text: str) -> None:
    """Send a plain-text operator alert. Lazy heavy imports (aiohttp + alerter)."""
    import aiohttp

    from scout.alerter import send_telegram_message
    from scout.config import Settings

    settings = Settings()
    _log.info("alert_channel_watchdog_alert_dispatched", chars=len(text))
    async with aiohttp.ClientSession() as session:
        await send_telegram_message(
            text,
            session,
            settings,
            parse_mode=None,
            source="alert_channel_watchdog",
        )
    _log.info("alert_channel_watchdog_alert_delivered")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="scout.db")
    parser.add_argument("--enabled", default="false")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sent-slo-hours", type=int, default=48)
    parser.add_argument("--digest-slo-days", type=int, default=2)
    args = parser.parse_args()

    enabled = _is_enabled(args.enabled)

    # Deploy-without-activate gate FIRST: no DB, no aiohttp, no network.
    # --dry-run bypasses it so the check logic can be exercised offline.
    if not enabled and not args.dry_run:
        print(json.dumps({"ok": True, "skipped": "watchdog_disabled"}, sort_keys=True))
        return 0

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(
            json.dumps(
                {"ok": False, "error": "db_not_found", "db": str(db_path)},
                sort_keys=True,
            )
        )
        return 1

    now = datetime.now(timezone.utc)
    try:
        checks = asyncio.run(
            _evaluate(
                str(db_path),
                sent_slo_hours=args.sent_slo_hours,
                digest_slo_days=args.digest_slo_days,
                now=now,
            )
        )
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": "runtime_error", "detail": str(exc)[:200]},
                sort_keys=True,
            )
        )
        return 1

    breaches = [k for k, v in checks.items() if v["status"] == "breach"]

    if not breaches:
        # Healthy: one-line OK log carrying both freshness ages.
        _log.info(
            "alert_channel_watchdog_ok",
            alert_last_seen=checks["alert_sent_rate"]["last_seen"],
            alert_age_hours=checks["alert_sent_rate"]["age_hours"],
            digest_last_seen=checks["digest_write_rate"]["last_seen"],
            digest_age_days=checks["digest_write_rate"]["age_days"],
        )
        print(
            json.dumps(
                {"ok": True, "breaches": 0, "checks": checks},
                sort_keys=True,
                default=str,
            )
        )
        return 0

    message = _compose_message(checks)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "ok": False,
                    "breaches": len(breaches),
                    "checks": checks,
                    "message": message,
                    "dry_run": True,
                    "sent": False,
                },
                sort_keys=True,
                default=str,
            )
        )
        return 5

    try:
        asyncio.run(_dispatch_alert(message))
    except Exception as exc:
        # Alert-send failure must surface, not be swallowed.
        _log.warning("alert_channel_watchdog_alert_failed", error=str(exc)[:200])
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "alert_dispatch_failed",
                    "breaches": len(breaches),
                    "checks": checks,
                    "detail": str(exc)[:200],
                },
                sort_keys=True,
                default=str,
            )
        )
        return 1

    print(
        json.dumps(
            {
                "ok": False,
                "breaches": len(breaches),
                "checks": checks,
                "message": message,
                "sent": True,
            },
            sort_keys=True,
            default=str,
        )
    )
    return 5


if __name__ == "__main__":
    sys.exit(main())
