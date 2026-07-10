#!/usr/bin/env python3
"""C4 coverage / silent-failure watchdog for the X price-snapshot pipeline (#392).

Evaluates ``scout.source_quality.watchdogs.evaluate_snapshot_watchdogs`` and,
when enabled, alerts the operator on any ``alert`` finding — plain text
(``parse_mode=None``, because check names contain ``_``) with §12b
``*_alert_dispatched`` / ``*_alert_delivered`` structured logs around the send.
Read-only on the DB.

DEPLOY-WITHOUT-ACTIVATE: an inert no-op unless ``--enabled`` is truthy (the .sh
wrapper wires it from the cron-env var ``SOURCE_CALL_COVERAGE_WATCHDOG_ENABLED``
— NOT a Settings field, so C4 changes no config). Evaluation itself is
aiohttp-free; the alert send lazily imports aiohttp + the alerter ONLY when an
alert must be dispatched, so the disabled and enabled-no-alert paths never touch
the network.

Exit codes:
  0 — ok (no alerts, or disabled no-op)
  5 — one or more watchdog alerts fired (and were dispatched)
  1 — DB missing, runtime error, or alert-dispatch failure
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite
import structlog

from scout.source_quality.watchdogs import evaluate_snapshot_watchdogs

_TRUTHY = {"1", "true", "yes", "on"}
_log = structlog.get_logger()


def _is_enabled(value: str) -> bool:
    return value.strip().lower() in _TRUTHY


async def _evaluate(db_path: str, **kw) -> list[dict]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        findings = await evaluate_snapshot_watchdogs(
            conn, now=datetime.now(timezone.utc), **kw
        )
    return [asdict(f) for f in findings]


async def _dispatch_alert(text: str) -> None:
    """Send a plain-text operator alert. Lazy heavy imports (aiohttp + alerter)."""
    import aiohttp

    from scout.alerter import send_telegram_message
    from scout.config import Settings

    settings = Settings()
    _log.info("source_call_coverage_watchdog_alert_dispatched", chars=len(text))
    async with aiohttp.ClientSession() as session:
        await send_telegram_message(
            text,
            session,
            settings,
            parse_mode=None,
            source="source_call_coverage_watchdog",
        )
    _log.info("source_call_coverage_watchdog_alert_delivered")


def _configure_logging() -> None:
    """Route structlog to stderr so this CLI's stdout stays JSON-only. Called
    ONLY from the ``__main__`` / cron entrypoint — NOT at import time (INF-03).
    Configuring structlog at module scope is a GLOBAL, process-wide mutation:
    importing this module in a unit test would reconfigure every other test's
    logger and silently empty their captured output. Keeping it here makes the
    import side-effect-free."""
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="scout.db")
    parser.add_argument("--enabled", default="false")
    parser.add_argument("--writer-staleness-min", type=int, default=30)
    parser.add_argument("--provider-error-rate-alert", type=float, default=0.5)
    parser.add_argument("--matured-all-null-alert", type=int, default=1)
    args = parser.parse_args()

    # Deploy-without-activate gate FIRST: no DB, no aiohttp, no network.
    if not _is_enabled(args.enabled):
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

    try:
        findings = asyncio.run(
            _evaluate(
                str(db_path),
                writer_staleness_min=args.writer_staleness_min,
                provider_error_rate_alert=args.provider_error_rate_alert,
                matured_all_null_alert=args.matured_all_null_alert,
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

    alerts = [f for f in findings if f["status"] == "alert"]
    if alerts:
        lines = ["X price-snapshot watchdog alerts:"]
        for a in alerts:
            lines.append(f"- {a['check']}: {json.dumps(a['detail'], sort_keys=True)}")
        try:
            asyncio.run(_dispatch_alert("\n".join(lines)))
        except Exception as exc:
            # Alert-send failure must surface, not be swallowed.
            _log.warning(
                "source_call_coverage_watchdog_alert_failed", error=str(exc)[:200]
            )
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "alert_dispatch_failed",
                        "alerts": alerts,
                        "detail": str(exc)[:200],
                    },
                    sort_keys=True,
                )
            )
            return 1

    print(
        json.dumps(
            {"ok": len(alerts) == 0, "alerts": len(alerts), "findings": findings},
            sort_keys=True,
            default=str,
        )
    )
    return 5 if alerts else 0


if __name__ == "__main__":
    _configure_logging()
    sys.exit(main())
