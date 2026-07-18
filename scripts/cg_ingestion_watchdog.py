#!/usr/bin/env python3
"""CoinGecko-ingestion freshness + persistent-outage watchdog (CLAUDE.md §12a).

Monitors the OUTPUT of the PRIMARY ingestion source (CoinGecko) in ONE script,
reading actual writer-table rows — never heartbeats — so it fires even when the
emitting pipeline path is itself broken or the pipeline is down.

MOTIVATING INCIDENT (2026-07-14): the CG Demo key's quota exhausted ~01:53Z;
every pipeline cycle since logged ``cg_429_backoff`` (attempt 0) then
``coingecko_lanes_stopped_for_backoff``, and the CG-sourced ``trending_snapshots``
writer went dead 2026-07-13 16:12Z. 1,559 backoff events over 6 days, ZERO
operator alerts — a 3-day silent outage of the primary ingestion source,
discovered only by manual audit. The in-process ingest watchdog
(``scout/heartbeat.observe_ingest_sources``) did NOT catch it: the breaker trips
on the FIRST CG lane (``held_position_prices``, main.py:727) and short-circuits
the scanner lanes BEFORE any of them records a zero ``raw_count`` sample, so the
per-source ``consecutive_misses`` counter never accumulates (a §9c phantom — the
lever exists but the data path never reaches it). This watchdog closes that gap
by reading the CG writers' OUTPUT rows directly.

Two checks, both OUTPUT-state:

  1. ``trending_snapshots`` freshness — ``MAX(snapshot_at)`` must be newer than
     ``TRENDING_SNAPSHOT_STALENESS_ALERT_HOURS`` (default 3h; the trending writer
     runs every pipeline cycle, so a >3h gap means the specific writer stalled).
     A missing OR empty table is itself a breach (silence is never ambiguous).
  2. Persistent CG-outage — the "last successful CG fetch" is estimated as the
     freshest ``snapshot_at`` across the CG-sourced per-cycle snapshot writers
     (``trending_snapshots`` + ``gainers_snapshots`` + ``losers_snapshots`` — all
     driven off the same ``/coins/markets`` raw pull, so they go dark together
     during a backoff-stop). When that is older than ``CG_OUTAGE_ALERT_HOURS``
     (default 2h) the ENTIRE CG ingestion is dark → the primary source is down,
     almost always Demo-key quota exhaustion. Only tables that EXIST contribute
     to the MAX (a flag-disabled empty table neither rescues nor trips the
     check); if NONE of the three tables exists, or all exist but are empty,
     that is itself a breach. Distinct from check 1: a trending-writer-specific
     bug (CG healthy, other writers fresh) trips check 1 alone; a real CG outage
     trips both.

On ANY breach the watchdog sends ONE plain-text Telegram message covering every
breached check that is not inside its send cooldown (``parse_mode=None`` — §12b,
table/signal names contain ``_`` which MarkdownV1 would mangle), with §12b
``cg_ingestion_watchdog_alert_dispatched`` / ``_alert_delivered`` structured logs
around the send. The send passes ``raise_on_failure=True`` so a rejected page
raises (logged ``_alert_failed`` + exit 1) instead of the alerter's default
swallow-and-return — otherwise this watchdog's own page could die silently.
Read-only on the DB.

Per-check SEND cooldown (``CG_INGESTION_WATCHDOG_COOLDOWN_HOURS``, default 24;
state files under ``--state-dir``): at most one page per breached check per
window, so an hourly cron does not emit ~24 identical pages/day on a standing
outage. Cooldown suppresses the SEND ONLY — a breach still exits 5 (logged
``_alert_suppressed_by_cooldown``); detection is never suppressed.

DEPLOY-WITHOUT-ACTIVATE: an inert no-op unless ``--enabled`` is truthy (the .sh
wrapper wires it from the cron-env var ``CG_INGESTION_WATCHDOG_ENABLED`` — NOT a
Settings field, so activation cannot happen by an accidental .env edit).
``--dry-run`` runs both checks and prints the full composed alert without
sending or touching state (for tests / manual verification); the disabled and
dry-run paths never touch the network.

Exit codes:
  0 — ok (both checks fresh, OR disabled no-op)
  5 — one or more breaches (page dispatched and/or cooldown-suppressed,
      or dry-run preview)
  1 — DB missing, runtime error, or alert-dispatch failure
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite  # noqa: E402
import structlog  # noqa: E402

_TRUTHY = {"1", "true", "yes", "on"}
_log = structlog.get_logger()

# CG-sourced per-cycle snapshot writers. All three are driven off the same
# combined /coins/markets raw pull (`_raw_markets_combined` in main.py), so a
# backoff-stop that zeroes the CG lanes zeroes all three together. Only the
# tables that EXIST contribute to the "last successful CG fetch" estimate.
_CG_OUTPUT_TABLES = ("trending_snapshots", "gainers_snapshots", "losers_snapshots")


def _configure_logging() -> None:
    """Route structlog to stderr so stdout stays clean for the JSON result.

    Called ONLY from the ``__main__`` / cron entrypoint — NOT at import time.
    Configuring structlog at module scope is a GLOBAL, process-wide mutation:
    importing this module in-process (e.g. from a unit test) would reconfigure
    every other test's logger and silently empty their captured log output.
    """
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))


def _is_enabled(value: str) -> bool:
    return value.strip().lower() in _TRUTHY


def _parse_ts(raw: str) -> datetime:
    """Parse an ISO ``snapshot_at`` value into a tz-aware UTC datetime."""
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _max_snapshot_at(conn: aiosqlite.Connection, table: str) -> str | None:
    """MAX(snapshot_at) for ``table``, or None. Raises on a missing table."""
    cur = await conn.execute(f"SELECT MAX(snapshot_at) FROM {table}")
    row = await cur.fetchone()
    return row[0] if row else None


async def _check_trending_freshness(
    conn: aiosqlite.Connection, slo_hours: int, now: datetime
) -> dict:
    """``MAX(snapshot_at)`` in ``trending_snapshots`` must be within the SLO.

    A missing OR empty table is a breach (silence is never ambiguous), matching
    the other §12a freshness watchdogs."""
    table = "trending_snapshots"
    try:
        last_seen = await _max_snapshot_at(conn, table)
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
    if last_seen is None:
        return {
            "table": table,
            "status": "breach",
            "reason": "no_snapshot_rows",
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


async def _check_cg_outage(
    conn: aiosqlite.Connection, outage_hours: int, now: datetime
) -> dict:
    """Persistent CG-outage: freshest CG-sourced snapshot must be within the window.

    "Last successful CG fetch" = the MAX ``snapshot_at`` across the CG per-cycle
    snapshot writers. Only tables that EXIST contribute; a flag-disabled empty
    table contributes nothing (it can neither rescue nor trip the check). If none
    of the tables exists → ``table_absent``; if all exist but are empty →
    ``no_cg_output_rows``; otherwise the freshest row's age drives the verdict."""
    tables_present: list[str] = []
    last_seen_dt: datetime | None = None
    last_seen: str | None = None
    for table in _CG_OUTPUT_TABLES:
        try:
            value = await _max_snapshot_at(conn, table)
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                continue
            raise
        tables_present.append(table)
        if value is None:
            continue
        value_dt = _parse_ts(value)
        if last_seen_dt is None or value_dt > last_seen_dt:
            last_seen_dt = value_dt
            last_seen = value

    if not tables_present:
        return {
            "table": "coingecko_ingestion",
            "status": "breach",
            "reason": "table_absent",
            "last_seen": None,
            "age_hours": None,
            "outage_hours": outage_hours,
            "tables_present": [],
        }
    if last_seen_dt is None:
        return {
            "table": "coingecko_ingestion",
            "status": "breach",
            "reason": "no_cg_output_rows",
            "last_seen": None,
            "age_hours": None,
            "outage_hours": outage_hours,
            "tables_present": tables_present,
        }
    age_hours = (now - last_seen_dt).total_seconds() / 3600.0
    breached = age_hours > outage_hours
    return {
        "table": "coingecko_ingestion",
        "status": "breach" if breached else "ok",
        "reason": "stale" if breached else "fresh",
        "last_seen": last_seen,
        "age_hours": round(age_hours, 2),
        "outage_hours": outage_hours,
        "tables_present": tables_present,
    }


async def _evaluate(
    db_path: str,
    *,
    trending_slo_hours: int,
    cg_outage_hours: int,
    now: datetime,
) -> dict:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        trending = await _check_trending_freshness(conn, trending_slo_hours, now)
        outage = await _check_cg_outage(conn, cg_outage_hours, now)
    return {
        "trending_freshness": trending,
        "cg_outage": outage,
    }


def _compose_message(checks: dict, include: list[str]) -> str:
    """Plain-text (no Markdown) operator alert naming each included breached
    check. ``include`` is the subset of breached check keys the cooldown gate
    currently allows to page (per-check dedup)."""
    lines = ["gecko-alpha CG-ingestion watchdog: freshness/outage breach"]

    t = checks["trending_freshness"]
    if "trending_freshness" in include and t["status"] == "breach":
        if t["reason"] == "table_absent":
            lines.append(
                "- trending_snapshots: table missing/absent — no CG trending "
                f"output audit trail exists (SLO {t['slo_hours']}h)"
            )
        elif t["reason"] == "no_snapshot_rows":
            lines.append(
                "- trending_snapshots: NO rows in table — the CG trending "
                f"snapshot writer has never written (SLO {t['slo_hours']}h)"
            )
        else:
            lines.append(
                f"- trending_snapshots: last snapshot_at {t['last_seen']} "
                f"({t['age_hours']}h ago) exceeds SLO {t['slo_hours']}h — the CG "
                "trending snapshot writer has stalled"
            )

    o = checks["cg_outage"]
    if "cg_outage" in include and o["status"] == "breach":
        if o["reason"] == "table_absent":
            lines.append(
                "- coingecko ingestion: NONE of the CG snapshot tables "
                f"({', '.join(_CG_OUTPUT_TABLES)}) exist — cannot confirm any CG "
                f"ingestion output (window {o['outage_hours']}h)"
            )
        elif o["reason"] == "no_cg_output_rows":
            lines.append(
                "- coingecko ingestion: CG snapshot tables are all EMPTY — the "
                "primary ingestion source has produced no output at all "
                f"(window {o['outage_hours']}h)"
            )
        else:
            lines.append(
                "- coingecko ingestion: last successful CG fetch at "
                f"{o['last_seen']} ({o['age_hours']}h ago) exceeds "
                f"{o['outage_hours']}h — the PRIMARY CoinGecko ingestion source "
                "has been in backoff-stop / cg_ranked==0 continuously; the most "
                "likely cause is Demo API-key quota exhaustion (cg_429_backoff). "
                "Check the CoinGecko key quota / rotate the key."
            )

    return "\n".join(lines)


async def _send_via_alerter(text: str) -> None:
    """Real plain-text Telegram send. Lazy heavy imports (aiohttp + alerter).

    ``raise_on_failure=True`` is load-bearing (§12b): the default alerter SWALLOWS
    non-200 / network errors — it logs a warning and returns without raising.
    Without this flag the ``_alert_delivered`` log below would fire even when
    Telegram rejected the page, so in the exact outage scenario this watchdog
    exists to catch, its OWN alert would die silently while reporting success.
    With the flag, a failed send raises and the caller logs ``_alert_failed`` +
    exits 1 instead.
    """
    import aiohttp

    from scout.alerter import send_telegram_message
    from scout.config import Settings

    settings = Settings()
    async with aiohttp.ClientSession() as session:
        await send_telegram_message(
            text,
            session,
            settings,
            parse_mode=None,
            raise_on_failure=True,
            source="cg_ingestion_watchdog",
        )


# Indirection point so tests can stub the network send without importing aiohttp.
_SEND = _send_via_alerter


def _dispatch_alert(text: str) -> None:
    """§12b log triplet around the send. Propagates on delivery failure so the
    caller logs ``_alert_failed`` and exits non-zero — never a silent success."""
    _log.info("cg_ingestion_watchdog_alert_dispatched", chars=len(text))
    asyncio.run(_SEND(text))
    _log.info("cg_ingestion_watchdog_alert_delivered")


def _cooldown_state(
    state_dir: str, key: str, now: datetime, cooldown_hours: float
) -> tuple[bool, str | None]:
    """Per-check send cooldown. Returns (eligible, next_eligible_iso).

    A missing, corrupt, or expired state file means eligible (the cooldown gates
    the SEND only — the breach is always detected). The state file holds the ISO
    timestamp of the last dispatched page for this check.
    """
    sf = Path(state_dir) / f"last_alert_{key}"
    if not sf.exists():
        return True, None
    try:
        last = _parse_ts(sf.read_text().strip())
    except Exception:
        return True, None
    if (now - last).total_seconds() / 3600.0 >= cooldown_hours:
        return True, None
    return False, (last + timedelta(hours=cooldown_hours)).isoformat()


def _write_cooldown_state(state_dir: str, key: str, now: datetime) -> None:
    """Record a successful dispatch time for ``key`` (written AFTER send)."""
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"last_alert_{key}").write_text(now.isoformat())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="scout.db")
    parser.add_argument("--enabled", default="false")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--trending-slo-hours", type=int, default=3)
    parser.add_argument("--cg-outage-hours", type=int, default=2)
    parser.add_argument("--cooldown-hours", type=int, default=24)
    parser.add_argument(
        "--state-dir", default="/var/lib/gecko-alpha/cg-ingestion-watchdog"
    )
    args = parser.parse_args(argv)

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
                trending_slo_hours=args.trending_slo_hours,
                cg_outage_hours=args.cg_outage_hours,
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
        _log.info(
            "cg_ingestion_watchdog_ok",
            trending_last_seen=checks["trending_freshness"]["last_seen"],
            trending_age_hours=checks["trending_freshness"]["age_hours"],
            cg_last_seen=checks["cg_outage"]["last_seen"],
            cg_age_hours=checks["cg_outage"]["age_hours"],
        )
        print(
            json.dumps(
                {"ok": True, "breaches": 0, "checks": checks},
                sort_keys=True,
                default=str,
            )
        )
        return 0

    # dry-run: full preview of the current breach, no cooldown/state I/O, no send.
    if args.dry_run:
        print(
            json.dumps(
                {
                    "ok": False,
                    "breaches": len(breaches),
                    "checks": checks,
                    "message": _compose_message(checks, breaches),
                    "dry_run": True,
                    "sent": False,
                },
                sort_keys=True,
                default=str,
            )
        )
        return 5

    # Real path: per-check cooldown gates the SEND (never the detection).
    to_send: list[str] = []
    suppressed: list[str] = []
    for key in breaches:
        eligible, next_eligible = _cooldown_state(
            args.state_dir, key, now, args.cooldown_hours
        )
        if eligible:
            to_send.append(key)
        else:
            suppressed.append(key)
            _log.info(
                "cg_ingestion_watchdog_alert_suppressed_by_cooldown",
                check=key,
                next_eligible=next_eligible,
                cooldown_hours=args.cooldown_hours,
            )

    sent = False
    if to_send:
        message = _compose_message(checks, to_send)
        try:
            _dispatch_alert(message)
        except Exception as exc:
            # Alert-send failure must surface, not be swallowed (§12b). State is
            # NOT written, so the next run re-alerts.
            _log.warning("cg_ingestion_watchdog_alert_failed", error=str(exc)[:200])
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
        for key in to_send:
            _write_cooldown_state(args.state_dir, key, now)
        sent = True

    print(
        json.dumps(
            {
                "ok": False,
                "breaches": len(breaches),
                "checks": checks,
                "sent": sent,
                "sent_checks": to_send,
                "suppressed_by_cooldown": suppressed,
            },
            sort_keys=True,
            default=str,
        )
    )
    return 5


if __name__ == "__main__":
    _configure_logging()
    sys.exit(main())
