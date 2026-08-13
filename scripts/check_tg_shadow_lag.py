#!/usr/bin/env python3
"""§12a watchdog for the TG actionability shadow writer.

WHY THIS EXISTS. `tg_act_shadow` is a new pipeline table, and a pipeline
table shipped without a freshness alarm is a future silent-failure surface:
the writer works on day one, a later refactor disconnects it, and nobody
learns until an unrelated audit months later. The alarm ships with the writer.

WHAT IT SEPARATES — this is the load-bearing part. A count of unshadowed rows
cannot distinguish three very different situations, and the catch-up scan
makes the confusion worse rather than better:

  quiet          no eligible work is overdue, or a scan is legitimately
                 mid-flight over rows that only just crossed the threshold
  writer_failing rows were overdue, a scan COMPLETED after they became
                 overdue, and they are still unshadowed — the writer is alive
                 and losing work
  writer_dead    rows are overdue and NO scan has completed within the
                 cadence budget. Absence of scans is itself the failure;
                 catch-up suppression must never mask a crashed writer.

Everything is generation-aware: only signals at or after the current
generation's `activated_at` are eligible, so a dark deploy and a fresh
activation both start from zero eligible rows instead of a page storm.

SAFETY POSTURE.
  - Opens SQLite `mode=ro`. Writes are refused by SQLite, not merely avoided.
  - NEVER calls `Database.initialize()`. A read-only observer that enters the
    migration machinery is the failure class removed in PR #520 (~40
    migrations per cron tick against a live 6.8 GB database).
  - Sends at most one page per run. It does not disable anything.

Cron registration is a DEPLOY/ACTIVATION step, documented in the PR runbook.
This script installs nothing.

Exit codes:
  0 — quiet, no generation, or disabled
  1 — DB missing, config unresolvable, runtime error, or dispatch failure
  5 — paged (or would have paged, under --dry-run)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog

_log = structlog.get_logger()

_TRUTHY = {"1", "true", "yes", "on"}

PAGING_STATUSES = (
    "writer_failing",
    "writer_dead",
    "active_generation_inconsistent",
)

# Must equal `scout.social.telegram.shadow.SHADOW_SCAN_COMPONENT_PREFIX`. Kept
# as a local literal rather than an import so this read-only script stays on
# stdlib sqlite3 and never drags in the aiosqlite/migration machinery. A test
# pins the two together: if they drift, this reads a component nobody writes,
# so `last_scan_at` is None forever and every eligible row reports
# `writer_dead` — a watchdog that cries wolf until the operator stops reading
# it.
#
# The heartbeat is per-generation. Looking up the CURRENT gate_version's row
# is what stops a retired generation's fresh heartbeat from vouching for a
# writer that has not run once under the new rules.
SCAN_HEALTH_COMPONENT_PREFIX = "tg_shadow_scan:"


def scan_health_component(gate_version: str) -> str:
    return f"{SCAN_HEALTH_COMPONENT_PREFIX}{gate_version}"


# Must equal `shadow.SHADOW_ACTIVE_GENERATION_COMPONENT` /
# `shadow.ACTIVE_GENERATION_DETAIL_PREFIX`; pinned by a test.
ACTIVE_GENERATION_COMPONENT = "tg_shadow_active_generation"
ACTIVE_GENERATION_DETAIL_PREFIX = "gate_version="


def parse_active_gate_version(detail: str | None) -> str | None:
    """Pull the gate_version out of the active-generation marker's `detail`."""
    if not detail:
        return None
    text = str(detail).strip()
    if not text.startswith(ACTIVE_GENERATION_DETAIL_PREFIX):
        return None
    value = text[len(ACTIVE_GENERATION_DETAIL_PREFIX) :].strip()
    return value or None


@dataclass(frozen=True)
class _Config:
    enabled: bool
    lag_threshold_min: int
    scan_cadence_min: int


def _configure_logging() -> None:
    """stderr only, so stdout stays clean JSON. Called from __main__ ONLY —
    configuring structlog at import time is a process-wide mutation that would
    silently empty other tests' captured logs."""
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _active_generation(conn: sqlite3.Connection) -> dict[str, Any]:
    """Resolve the generation the WRITER says it is running.

    Deliberately NOT the newest registry row. Re-enable semantics resume an
    existing gate_version without rewriting its `activated_at`, so after
    v1 -> v2 -> (return to the v1 configuration) the newest row is v2 while the
    writer runs v1. Chronology would then page that v2 has no fresh heartbeat —
    true, irrelevant, and it leaves v1 unmonitored.

    Also deliberately NOT a recomputed fingerprint: that needs a registered
    feature provider, and this process is a read-only observer with none.

    Outcomes:
      ok                    marker names a gate_version that exists in the registry
      no_generation         no marker and an empty registry — never armed
      inconsistent          marker unusable, OR the registry holds generations
                            that no marker names — page, with the reason
    """
    marker = conn.execute(
        "SELECT detail FROM tg_social_health WHERE component = ?",
        (ACTIVE_GENERATION_COMPONENT,),
    ).fetchone()
    registry_row = conn.execute(
        "SELECT 1 FROM tg_act_shadow_generations LIMIT 1"
    ).fetchone()

    if marker is None:
        if registry_row is None:
            return {"state": "no_generation"}
        # Registry rows exist but nothing claims them. Reachable if a crash
        # lands between the generation insert and the marker publish (the
        # activation path now writes both in one transaction, so this should
        # be unreachable — but "should be unreachable" is not a monitoring
        # strategy). Staying quiet here would leave a genuinely armed
        # generation completely unwatched, which is the exact silent failure
        # this watchdog exists to prevent, so it fails CLOSED.
        return {
            "state": "inconsistent",
            "reason": "registry_nonempty_active_marker_missing",
        }

    gate_version = parse_active_gate_version(marker["detail"])
    if gate_version is None:
        return {"state": "inconsistent", "reason": "unparseable_marker"}

    row = conn.execute(
        "SELECT activated_at FROM tg_act_shadow_generations WHERE gate_version = ?",
        (gate_version,),
    ).fetchone()
    if row is None:
        return {
            "state": "inconsistent",
            "reason": "marker_names_a_gate_version_with_no_registry_row",
            "gate_version": gate_version,
        }
    return {
        "state": "ok",
        "gate_version": gate_version,
        "activated_at": str(row["activated_at"]),
    }


def evaluate_tg_shadow_lag(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    lag_threshold_min: int,
    scan_cadence_min: int,
) -> dict[str, Any]:
    """Classify writer health. Pure read; no side effects, no alerting."""
    idle = {
        "gate_version": None,
        "activated_at": None,
        "overdue_count": 0,
        "oldest_overdue_at": None,
        "last_scan_at": None,
    }
    resolved = _active_generation(conn)
    if resolved["state"] == "no_generation":
        return {"status": "no_generation", **idle}
    if resolved["state"] == "inconsistent":
        return {
            "status": "active_generation_inconsistent",
            **idle,
            "gate_version": resolved.get("gate_version"),
            "inconsistency": resolved["reason"],
        }
    gate_version = resolved["gate_version"]
    activated_at = resolved["activated_at"]
    overdue_cutoff = (now - timedelta(minutes=lag_threshold_min)).isoformat()

    # Eligible AND overdue AND unshadowed under the CURRENT gate_version.
    # A decision written under a previous generation is not this generation's
    # evidence, so it must not silence this one.
    row = conn.execute(
        """SELECT COUNT(*) AS n, MIN(s.created_at) AS oldest
             FROM tg_social_signals s
            WHERE s.resolution_state = 'RESOLVED'
              AND s.created_at >= ?
              AND s.created_at <= ?
              AND NOT EXISTS (
                    SELECT 1 FROM tg_act_shadow a
                     WHERE a.signal_id = s.id
                       AND a.gate_version = ?
                  )""",
        (activated_at, overdue_cutoff, gate_version),
    ).fetchone()
    overdue_count = int(row["n"] or 0)
    oldest_overdue_at = row["oldest"]

    scan_row = conn.execute(
        "SELECT updated_at FROM tg_social_health WHERE component = ?",
        (scan_health_component(gate_version),),
    ).fetchone()
    last_scan_at = None if scan_row is None else str(scan_row["updated_at"])

    state = {
        "status": "quiet",
        "gate_version": gate_version,
        "activated_at": activated_at,
        "overdue_count": overdue_count,
        "oldest_overdue_at": oldest_overdue_at,
        "last_scan_at": last_scan_at,
    }
    if overdue_count == 0:
        return state

    # (b) dead writer, checked FIRST so the more actionable diagnosis wins
    # when both conditions hold.
    last_scan = _parse_utc(last_scan_at)
    if last_scan is None or (now - last_scan) > timedelta(minutes=scan_cadence_min):
        state["status"] = "writer_dead"
        return state

    # (a) running but failing: the row survived a scan that completed AFTER it
    # became overdue. If the last completed scan predates that crossing, a
    # catch-up is simply still in flight and there is nothing to page about.
    became_overdue = _parse_utc(oldest_overdue_at)
    if became_overdue is not None and last_scan >= became_overdue + timedelta(
        minutes=lag_threshold_min
    ):
        state["status"] = "writer_failing"
    return state


def _compose(state: dict[str, Any], config: _Config) -> str:
    """Plain-text page body.

    Identifiers here (`tg_shadow_writer`, `writer_failing`, gate versions)
    carry underscores, which MarkdownV1 renders as italics while Telegram
    still answers HTTP 200 — the operator gets a mangled message and misses
    the alert. The send below therefore uses `parse_mode=None`, and this body
    stays deliberately free of Markdown syntax.
    """
    if state["status"] == "active_generation_inconsistent":
        return "\n".join(
            [
                "ALERT tg_shadow active-generation marker does not match the registry",
                f"status: {state['status']}",
                f"inconsistency: {state.get('inconsistency')}",
                f"gate_version named by marker: {state.get('gate_version')}",
                "",
                "The active-generation marker and tg_act_shadow_generations "
                "disagree, so shadow health cannot be evaluated. Either the "
                "marker names a generation that was never registered, or "
                "generations are registered that no marker claims.",
            ]
        )
    headline = (
        "ALERT tg_shadow writer not scanning"
        if state["status"] == "writer_dead"
        else "ALERT tg_shadow rows unshadowed after a completed scan"
    )
    return "\n".join(
        [
            headline,
            f"status: {state['status']}",
            f"gate_version: {state['gate_version']}",
            f"generation activated_at: {state['activated_at']}",
            f"eligible but unshadowed: {state['overdue_count']}",
            f"oldest overdue at: {state['oldest_overdue_at']}",
            f"last completed scan: {state['last_scan_at'] or 'never'}",
            (
                f"thresholds: lag {config.lag_threshold_min}m, "
                f"scan cadence {config.scan_cadence_min}m"
            ),
            "",
            "Shadow evidence is not accumulating. Check the pipeline listener "
            "and the tg_shadow_scan_complete log line.",
        ]
    )


async def _send_via_alerter(text: str) -> None:
    """Real plain-text Telegram send. Lazy heavy imports (aiohttp + alerter).

    `raise_on_failure=True` is LOAD-BEARING (§12b): the default alerter logs a
    warning and returns on non-200, so without it the `_alert_delivered` log
    below would fire for a page Telegram rejected.
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
            source="tg_shadow_lag_watchdog",
        )


# Indirection point so tests can exercise the dispatch seam without network.
_SEND = _send_via_alerter


async def _send(text: str) -> None:
    """Dispatched/delivered pair around the send.

    The alerter logs only on failure, so a successful delivery is otherwise
    silent — which makes "no logs about this alert" ambiguous between
    "delivered cleanly" and "the call was skipped".
    """
    _log.info("tg_shadow_lag_alert_dispatched", chars=len(text))
    await _SEND(text)
    _log.info("tg_shadow_lag_alert_delivered")


def _resolve_config(args) -> _Config:
    """Thresholds live in Settings; the CLI flags are overrides.

    Tests (and a one-off manual run) pass all three explicitly and never touch
    `.env`. Production cron passes none and gets the single source of truth.
    """
    if (
        args.enabled is not None
        and args.lag_threshold_min is not None
        and args.scan_cadence_min is not None
    ):
        return _Config(
            enabled=str(args.enabled).strip().lower() in _TRUTHY,
            lag_threshold_min=args.lag_threshold_min,
            scan_cadence_min=args.scan_cadence_min,
        )
    from scout.config import Settings

    settings = Settings()
    return _Config(
        enabled=(
            str(args.enabled).strip().lower() in _TRUTHY
            if args.enabled is not None
            else settings.TG_SHADOW_ENABLED
        ),
        lag_threshold_min=(
            args.lag_threshold_min
            if args.lag_threshold_min is not None
            else settings.TG_SHADOW_LAG_THRESHOLD_MIN
        ),
        scan_cadence_min=(
            args.scan_cadence_min
            if args.scan_cadence_min is not None
            else settings.TG_SHADOW_SCAN_CADENCE_MIN
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="scout.db")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--enabled", default=None)
    parser.add_argument("--lag-threshold-min", type=int, default=None)
    parser.add_argument("--scan-cadence-min", type=int, default=None)
    args = parser.parse_args(argv)

    try:
        config = _resolve_config(args)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"config: {exc}"[:300]}))
        return 1

    if not config.enabled:
        print(json.dumps({"ok": True, "disabled": True}))
        return 0

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(json.dumps({"ok": False, "error": "db_not_found", "db": str(db_path)}))
        return 1

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            state = evaluate_tg_shadow_lag(
                conn,
                now=datetime.now(timezone.utc),
                lag_threshold_min=config.lag_threshold_min,
                scan_cadence_min=config.scan_cadence_min,
            )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        _log.exception("tg_shadow_lag_watchdog_error")
        print(json.dumps({"ok": False, "error": str(exc)[:300]}))
        return 1

    firing = state["status"] in PAGING_STATUSES
    if firing and not args.dry_run:
        try:
            asyncio.run(_send(_compose(state, config)))
        except Exception as exc:  # noqa: BLE001
            _log.exception("tg_shadow_lag_dispatch_failed")
            print(json.dumps({"ok": False, "dispatch_error": str(exc)[:300], **state}))
            return 1

    print(json.dumps({"ok": not firing, **state}, sort_keys=True))
    return 5 if firing else 0


if __name__ == "__main__":
    _configure_logging()
    sys.exit(main())
