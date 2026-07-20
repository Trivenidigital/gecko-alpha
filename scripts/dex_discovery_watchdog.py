#!/usr/bin/env python3
"""DEX-discovery poll-liveness watchdog (CLAUDE.md §12a) — PR-C.

Monitors the GT new-pools research lane's durable POLL HEARTBEAT — the
``ingest_watchdog_state`` row ``source='dex_discovery'`` that the lane upserts
only after a SUCCESSFUL poll (>=1 network yielded a structurally valid pool
list; see gt_new_pools). Liveness therefore measures the POLLER, never market
activity: a quiet market with a fresh heartbeat is healthy.

``MAX(first_seen_at)`` from ``dex_pool_discoveries`` (last_new_discovery_at /
discovery_age) is reported as DIAGNOSTIC CONTEXT ONLY — it never drives the
paging decision (a healthy poller can legitimately find nothing new; a
future-corrupted discovery timestamp must not mask a dead poller).

Armed semantics (both gates on):
  - heartbeat row missing ............................ breach (heartbeat_absent)
  - heartbeat older than --staleness-hours ........... breach (stale)
  - heartbeat in the FUTURE beyond --clock-skew-seconds
    (named allowance, no embedded constant) .......... breach (future_invalid)
  - fresh heartbeat, however old the discoveries ..... ok (discovery_age logged)
Gate semantics:
  - --discovery-enabled falsy (lane intentionally off) → clean exit 0, no page
    (disablement is never represented as failure)
  - --enabled falsy (watchdog gate off) → clean exit 0 no-op (wrapper wires it
    from the CRON env var DEX_DISCOVERY_WATCHDOG_ENABLED, never .env)

Send path mirrors the CG watchdog: ONE plain-text Telegram page
(``parse_mode=None``, §12b), ``dex_discovery_watchdog_alert_dispatched`` /
``_alert_delivered`` / ``_alert_failed`` structured logs, send with
``raise_on_failure=True``. Per-check SEND cooldown (default 24h, state file
under --state-dir) — cooldown state is written ONLY after a successful send,
so a failed page re-alerts next run; a cooled breach still exits 5
(``_alert_suppressed_by_cooldown``). A non-blocking ``flock`` on
``<state-dir>/lock`` prevents concurrent invocations from double-sending
(loser logs and exits 0). ``--dry-run`` runs the check and prints the composed
alert without sending, locking, or touching cooldown state. Read-only on the
DB.

Exit codes:
  0 — ok / disabled no-op / lock already held
  5 — breach (page dispatched, cooldown-suppressed, or dry-run preview)
  1 — DB missing, runtime error, or alert-dispatch failure
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite  # noqa: E402
import structlog  # noqa: E402

_TRUTHY = {"1", "true", "yes", "on"}
_log = structlog.get_logger()

_CHECK_KEY = "poll_liveness"
_HEARTBEAT_SOURCE = "dex_discovery"


def _configure_logging() -> None:
    """Route structlog to stderr (main-entry only; never at import time)."""
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))


def _is_enabled(value: str) -> bool:
    return value.strip().lower() in _TRUTHY


def _parse_ts(raw: str) -> datetime:
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _read_state(
    db_path: str, now: datetime, staleness_hours: float, clock_skew_seconds: float
) -> dict:
    """Run the liveness check + gather diagnostic context. Read-only."""
    async with aiosqlite.connect(db_path) as conn:
        try:
            cur = await conn.execute(
                "SELECT updated_at FROM ingest_watchdog_state WHERE source = ?",
                (_HEARTBEAT_SOURCE,),
            )
            row = await cur.fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                row = None
            else:
                raise
        last_new_discovery_at: str | None = None
        try:
            cur = await conn.execute(
                "SELECT MAX(first_seen_at) FROM dex_pool_discoveries"
            )
            drow = await cur.fetchone()
            last_new_discovery_at = drow[0] if drow else None
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise

    discovery_age_hours = None
    if last_new_discovery_at:
        discovery_age_hours = round(
            (now - _parse_ts(last_new_discovery_at)).total_seconds() / 3600.0, 2
        )

    result = {
        "check": _CHECK_KEY,
        "last_successful_poll_at": row[0] if row else None,
        "last_new_discovery_at": last_new_discovery_at,
        "poll_age_hours": None,
        "poll_age_seconds_signed": None,
        "discovery_age_hours": discovery_age_hours,
        "staleness_hours": staleness_hours,
        "clock_skew_seconds": clock_skew_seconds,
    }

    if row is None or not row[0]:
        result.update(status="breach", reason="heartbeat_absent")
        return result

    signed_age = (now - _parse_ts(row[0])).total_seconds()
    result["poll_age_seconds_signed"] = round(signed_age, 1)
    result["poll_age_hours"] = round(signed_age / 3600.0, 2)
    if signed_age < -clock_skew_seconds:
        # Future beyond the named allowance: invalid state, never "healthy".
        result.update(status="breach", reason="future_invalid")
    elif signed_age > staleness_hours * 3600.0:
        result.update(status="breach", reason="stale")
    else:
        result.update(status="ok", reason="fresh")
    return result


def _compose_message(check: dict) -> str:
    lines = ["gecko-alpha DEX-discovery watchdog: poll-liveness breach"]
    reason = check["reason"]
    if reason == "heartbeat_absent":
        lines.append(
            "- dex_discovery heartbeat: NO successful-poll record exists in "
            "ingest_watchdog_state — the discovery lane has never completed a "
            f"valid poll (SLO {check['staleness_hours']}h)"
        )
    elif reason == "future_invalid":
        lines.append(
            "- dex_discovery heartbeat: last_successful_poll_at "
            f"{check['last_successful_poll_at']} is in the FUTURE "
            f"(signed age {check['poll_age_seconds_signed']}s, allowance "
            f"{check['clock_skew_seconds']}s) — clock skew or corrupted state; "
            "liveness cannot be trusted"
        )
    else:
        lines.append(
            "- dex_discovery heartbeat: last successful poll at "
            f"{check['last_successful_poll_at']} ({check['poll_age_hours']}h ago) "
            f"exceeds SLO {check['staleness_hours']}h — the GT new-pools poller "
            "has stalled (pipeline down, GT unreachable, or schema drift "
            "failing every pass)"
        )
    lines.append(
        "  context: last NEW discovery at "
        f"{check['last_new_discovery_at'] or 'never'}"
        + (
            f" ({check['discovery_age_hours']}h ago)"
            if check["discovery_age_hours"] is not None
            else ""
        )
        + " — diagnostic only, not the paging signal"
    )
    return "\n".join(lines)


async def _send_via_alerter(text: str) -> None:
    """Real plain-text Telegram send (lazy heavy imports)."""
    import aiohttp

    from scout.alerter import send_telegram_message
    from scout.config import Settings

    settings = Settings()
    async with aiohttp.ClientSession() as session:
        await send_telegram_message(
            text, session, settings, parse_mode=None, raise_on_failure=True
        )


def _cooldown_active(state_dir: str, now: datetime, cooldown_hours: float) -> bool:
    sf = Path(state_dir) / f"last_alert_{_CHECK_KEY}"
    if not sf.exists():
        return False
    try:
        last = _parse_ts(sf.read_text())
    except (ValueError, OSError):
        return False
    return (now - last).total_seconds() < cooldown_hours * 3600.0


def _write_cooldown_state(state_dir: str, now: datetime) -> None:
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"last_alert_{_CHECK_KEY}").write_text(now.isoformat())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--enabled", default="false")
    ap.add_argument("--discovery-enabled", default="false")
    ap.add_argument("--staleness-hours", type=float, default=2.0)
    ap.add_argument("--clock-skew-seconds", type=float, default=300.0)
    ap.add_argument("--cooldown-hours", type=float, default=24.0)
    ap.add_argument(
        "--state-dir", default="/var/lib/gecko-alpha/dex-discovery-watchdog"
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if not _is_enabled(args.enabled):
        _log.info("dex_discovery_watchdog_disabled_noop")
        print(json.dumps({"status": "disabled_noop"}))
        return 0
    if not _is_enabled(args.discovery_enabled):
        # The lane is intentionally OFF: a liveness page here would represent
        # disablement as failure. Clean exit, no page, explicit status.
        _log.info("dex_discovery_watchdog_not_armed_discovery_disabled")
        print(json.dumps({"status": "not_armed_discovery_disabled"}))
        return 0
    if not Path(args.db).exists():
        _log.error("dex_discovery_watchdog_db_missing", db=args.db)
        print(json.dumps({"status": "error", "error": "db_missing"}))
        return 1

    now = datetime.now(timezone.utc)
    try:
        check = asyncio.run(
            _read_state(args.db, now, args.staleness_hours, args.clock_skew_seconds)
        )
    except Exception as exc:  # runtime error → exit 1, never a silent 0
        _log.error("dex_discovery_watchdog_runtime_error", error=str(exc))
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 1

    _log.info(
        "dex_discovery_watchdog_check",
        **{k: v for k, v in check.items() if k != "check"},
    )

    if check["status"] == "ok":
        print(json.dumps({"status": "ok", "check": check}))
        return 0

    message = _compose_message(check)
    if args.dry_run:
        # Preview only: no send, no lock, no cooldown mutation.
        print(json.dumps({"status": "breach_dry_run", "check": check}))
        print(message)
        return 5

    # Non-blocking lock so concurrent invocations cannot double-send.
    lock_dir = Path(args.state_dir)
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_fh = open(lock_dir / "lock", "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        _log.info("dex_discovery_watchdog_lock_held_skipping")
        print(json.dumps({"status": "lock_held_skipped"}))
        lock_fh.close()
        return 0

    try:
        if _cooldown_active(args.state_dir, now, args.cooldown_hours):
            _log.info(
                "dex_discovery_watchdog_alert_suppressed_by_cooldown",
                check=_CHECK_KEY,
            )
            print(json.dumps({"status": "breach_cooldown_suppressed", "check": check}))
            return 5
        _log.info("dex_discovery_watchdog_alert_dispatched", chars=len(message))
        try:
            asyncio.run(_send_via_alerter(message))
        except Exception as exc:
            # Send failure: log + exit 1; cooldown state NOT written, so the
            # next run re-alerts instead of going quiet for a full window.
            _log.error("dex_discovery_watchdog_alert_failed", error=str(exc))
            print(json.dumps({"status": "error", "error": "alert_dispatch_failed"}))
            return 1
        _log.info("dex_discovery_watchdog_alert_delivered")
        _write_cooldown_state(args.state_dir, now)
        print(json.dumps({"status": "breach_paged", "check": check}))
        return 5
    finally:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
        finally:
            lock_fh.close()


if __name__ == "__main__":
    _configure_logging()
    sys.exit(main())
