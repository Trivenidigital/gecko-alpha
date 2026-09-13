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
  - heartbeat timestamp unparseable .................. breach (heartbeat_invalid)
  - heartbeat older than --staleness-hours ........... breach (stale)
  - heartbeat in the FUTURE beyond --clock-skew-seconds
    (named allowance, no embedded constant) .......... breach (future_invalid)
  - fresh heartbeat, however old the discoveries ..... ok (discovery_age logged)
RH/Pons additionally requires a fresh exact-primary deployment checkpoint with
known head_block. Missing head evidence, stale checkpoint evidence, or a lag
above --max-head-lag-blocks (default 2000) breaches even with a fresh heartbeat.
This head-coverage check does not apply to DEX discovery.
A malformed DIAGNOSTIC timestamp (last_new_discovery_at) never affects the
verdict: it is reported as ``discovery_timestamp_valid=false`` with
``discovery_age_hours=null`` and the primary verdict proceeds from the
heartbeat alone.
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
(``_alert_suppressed_by_cooldown``). Cooldown state uses the same named
clock-skew allowance: a cooldown timestamp in the future WITHIN the allowance
still counts as active; beyond it the state is treated as corrupted — logged
and IGNORED, so future-corrupted state can never suppress a current breach.
A malformed cooldown file is likewise eligible-to-send. A non-blocking
``flock`` on ``<state-dir>/lock`` prevents concurrent invocations from
double-sending (loser logs and exits 0). ``--dry-run`` runs the check and
prints the composed alert without sending, locking, or touching cooldown
state. The DB is opened read-only (sqlite ``mode=ro`` URI).

Runtime knobs are range-validated at the script boundary BEFORE any DB or
state access (finite values only; staleness-hours in [1, 168],
clock-skew-seconds in [0, 3600], cooldown-hours in (0, 168] — the 168h/one-
week upper bound keeps a typo'd cooldown from silencing the check
indefinitely). Invalid configuration → structured ``invalid_configuration``
output, exit 1.

Exit codes:
  0 — ok / disabled no-op / lock already held
  5 — breach (page dispatched, cooldown-suppressed, or dry-run preview)
  1 — invalid configuration, DB missing, runtime error, or dispatch failure
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
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
_DISCOVERY_TABLES = {
    "dex_discovery": "dex_pool_discoveries",
    "rh_pons": "curve_launch_discoveries",
}


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


# Malformed persisted timestamps must degrade per-field, never crash the run:
# a non-string or unparseable value raises one of these from _parse_ts.
_TS_PARSE_ERRORS = (ValueError, TypeError, AttributeError)

# Named bounds for the runtime knobs, enforced at the script boundary before
# any DB/state access. The cooldown upper bound (one week) exists so a typo'd
# value cannot silence the check indefinitely.
_STALENESS_HOURS_RANGE = (1.0, 168.0)
# RH/Pons passes every few seconds, so its SLO is minutes (1 min .. 1 week).
_STALENESS_MINUTES_RANGE = (1.0, 10080.0)
_RH_ATTEMPT_SOURCE = "rh_pons_attempt"
_CLOCK_SKEW_SECONDS_RANGE = (0.0, 3600.0)
_COOLDOWN_HOURS_MAX = 168.0


def _validate_config(args: argparse.Namespace) -> str | None:
    """Return a human-readable error for out-of-range knobs, else None."""
    if getattr(args, "max_head_lag_blocks", 2000) < 0:
        return "--max-head-lag-blocks must be nonnegative"
    minutes = getattr(args, "staleness_minutes", None)
    if minutes is not None:
        lo, hi = _STALENESS_MINUTES_RANGE
        if not math.isfinite(minutes) or not (lo <= minutes <= hi):
            return (
                f"--staleness-minutes must be finite and within [{lo}, {hi}], "
                f"got {minutes}"
            )
    # Unset (None, the argparse default) keeps the pre-existing behaviour: no
    # failure-streak check. Only an explicit value is range-checked.
    max_failed = getattr(args, "max_consecutive_failed_passes", None)
    if max_failed is not None and max_failed < 1:
        return "--max-consecutive-failed-passes must be >= 1"
    lo, hi = _STALENESS_HOURS_RANGE
    if not math.isfinite(args.staleness_hours) or not (
        lo <= args.staleness_hours <= hi
    ):
        return (
            f"--staleness-hours must be finite and within [{lo}, {hi}], "
            f"got {args.staleness_hours}"
        )
    lo, hi = _CLOCK_SKEW_SECONDS_RANGE
    if not math.isfinite(args.clock_skew_seconds) or not (
        lo <= args.clock_skew_seconds <= hi
    ):
        return (
            f"--clock-skew-seconds must be finite and within [{lo}, {hi}], "
            f"got {args.clock_skew_seconds}"
        )
    if not math.isfinite(args.cooldown_hours) or not (
        0.0 < args.cooldown_hours <= _COOLDOWN_HOURS_MAX
    ):
        return (
            f"--cooldown-hours must be finite and within (0, "
            f"{_COOLDOWN_HOURS_MAX}], got {args.cooldown_hours}"
        )
    return None


async def _read_state(
    db_path: str,
    now: datetime,
    staleness_hours: float,
    clock_skew_seconds: float,
    source: str = _HEARTBEAT_SOURCE,
    max_head_lag_blocks: int = 2000,
    staleness_seconds: float | None = None,
    max_failed_passes: int | None = None,
) -> dict:
    """Run the liveness check + gather diagnostic context.

    Opens the DB read-only (sqlite mode=ro URI) so this path structurally
    cannot mutate pipeline state. ``staleness_seconds`` (RH minutes SLO)
    overrides ``staleness_hours`` when given.
    """
    table = _DISCOVERY_TABLES[source]  # closed mapping, never caller-supplied SQL
    limit_s = (
        staleness_seconds if staleness_seconds is not None else staleness_hours * 3600.0
    )
    attempt = None
    async with aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        try:
            cur = await conn.execute(
                "SELECT updated_at FROM ingest_watchdog_state WHERE source = ?",
                (source,),
            )
            row = await cur.fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                row = None
            else:
                raise
        last_new_discovery_at: str | None = None
        try:
            cur = await conn.execute(f"SELECT MAX(first_seen_at) FROM {table}")
            drow = await cur.fetchone()
            last_new_discovery_at = drow[0] if drow else None
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise

        checkpoint = None
        if source == "rh_pons":
            try:
                cur = await conn.execute(
                    "SELECT consecutive_misses, updated_at FROM ingest_watchdog_state "
                    "WHERE source = ?",
                    (_RH_ATTEMPT_SOURCE,),
                )
                attempt = await cur.fetchone()
            except sqlite3.OperationalError as exc:
                if not any(
                    text in str(exc).lower()
                    for text in ("no such table", "no such column")
                ):
                    raise
            from scout.ingestion.rh_pons import PONS_DEPLOYMENTS

            primary = next(
                (d for d in PONS_DEPLOYMENTS if d.version == "pons_v2"), None
            )
            if primary is not None:
                try:
                    cur = await conn.execute(
                        "SELECT next_block, head_block, updated_at FROM curve_scan_checkpoints "
                        "WHERE chain_id=? AND protocol=? AND factory=?",
                        (primary.chain_id, primary.version, primary.factory.lower()),
                    )
                    checkpoint = await cur.fetchone()
                except sqlite3.OperationalError as exc:
                    if not any(
                        text in str(exc).lower()
                        for text in ("no such table", "no such column")
                    ):
                        raise

    # Diagnostic timestamp: malformed → flagged invalid, age null, verdict
    # UNAFFECTED (it proceeds from the heartbeat alone).
    discovery_age_hours = None
    discovery_timestamp_valid = True if last_new_discovery_at else None
    if last_new_discovery_at:
        try:
            discovery_age_hours = round(
                (now - _parse_ts(last_new_discovery_at)).total_seconds() / 3600.0, 2
            )
        except _TS_PARSE_ERRORS:
            discovery_timestamp_valid = False
            discovery_age_hours = None

    result = {
        "source": source,
        "check": _CHECK_KEY,
        "last_successful_poll_at": row[0] if row else None,
        "last_new_discovery_at": last_new_discovery_at,
        "poll_age_hours": None,
        "poll_age_seconds_signed": None,
        "discovery_age_hours": discovery_age_hours,
        "discovery_timestamp_valid": discovery_timestamp_valid,
        "staleness_hours": (
            staleness_hours if staleness_seconds is None else round(limit_s / 3600.0, 4)
        ),
        "clock_skew_seconds": clock_skew_seconds,
    }
    if source == "rh_pons":
        result.update(
            staleness_seconds=limit_s,
            last_attempt_at=attempt[1] if attempt else None,
            consecutive_failed_passes=attempt[0] if attempt else None,
            max_consecutive_failed_passes=max_failed_passes,
        )

    if row is None or not row[0]:
        result.update(status="breach", reason="heartbeat_absent")
        return result

    try:
        signed_age = (now - _parse_ts(row[0])).total_seconds()
    except _TS_PARSE_ERRORS:
        # A heartbeat that exists but cannot be parsed is corrupted liveness
        # state: page-worthy invalid-state breach, never a generic crash.
        result.update(status="breach", reason="heartbeat_invalid")
        return result
    result["poll_age_seconds_signed"] = round(signed_age, 1)
    result["poll_age_hours"] = round(signed_age / 3600.0, 2)
    if signed_age < -clock_skew_seconds:
        # Future beyond the named allowance: invalid state, never "healthy".
        result.update(status="breach", reason="future_invalid")
    elif signed_age > limit_s:
        result.update(status="breach", reason="stale")
    else:
        result.update(status="ok", reason="fresh")
    if source == "rh_pons":
        result.update(
            head_lag_blocks=None,
            max_head_lag_blocks=max_head_lag_blocks,
            checkpoint_updated_at=checkpoint[2] if checkpoint else None,
        )
        failed_passes = attempt[0] if attempt else None
        if (
            result["status"] == "ok"
            and max_failed_passes is not None
            and isinstance(failed_passes, int)
            and failed_passes >= max_failed_passes
        ):
            # A lane that keeps failing pages before its heartbeat goes stale.
            result.update(status="breach", reason="attempt_failures_exceeded")
        if result["status"] == "ok":
            if checkpoint is None or not all(
                isinstance(v, int) and v >= 0 for v in checkpoint[:2]
            ):
                result.update(status="breach", reason="head_lag_unknown")
            else:
                result["head_lag_blocks"] = max(0, checkpoint[1] - checkpoint[0] + 1)
                try:
                    checkpoint_age = (now - _parse_ts(checkpoint[2])).total_seconds()
                except _TS_PARSE_ERRORS:
                    result.update(status="breach", reason="checkpoint_invalid")
                else:
                    if checkpoint_age < -clock_skew_seconds:
                        result.update(
                            status="breach", reason="checkpoint_future_invalid"
                        )
                    elif checkpoint_age > limit_s:
                        result.update(status="breach", reason="checkpoint_stale")
                    elif result["head_lag_blocks"] > max_head_lag_blocks:
                        result.update(status="breach", reason="head_lag_exceeded")
    return result


def _compose_message(check: dict) -> str:
    source = check.get("source", _HEARTBEAT_SOURCE)
    label = "DEX-discovery" if source == "dex_discovery" else "RH/Pons"
    lines = [f"gecko-alpha {label} watchdog: poll-liveness breach"]
    reason = check["reason"]
    if reason == "enabled_gate_mismatch":
        lines.append(
            "- RH/Pons collector wrote a heartbeat or attempt record within the SLO, "
            "but RH_PONS_COLLECTOR_ENABLED reads false for this watchdog. Set the "
            "flag in .env (not only the service environment) or monitoring stays "
            "disarmed while the collector runs."
        )
    elif reason == "attempt_failures_exceeded":
        lines.append(
            f"- RH/Pons collector has failed {check.get('consecutive_failed_passes')} "
            f"consecutive passes (limit {check.get('max_consecutive_failed_passes')}); "
            f"last attempt {check.get('last_attempt_at')}. Coverage is not advancing "
            "(provider throttling or outage, missing RPC URL, or DB errors)."
        )
    elif reason == "head_lag_unknown":
        lines.append(
            "- RH/Pons primary deployment has no usable measured-head checkpoint; "
            "the poll heartbeat is fresh but current-head coverage is UNKNOWN."
        )
    elif reason == "head_lag_exceeded":
        lines.append(
            f"- RH/Pons primary deployment is {check['head_lag_blocks']} blocks behind "
            f"its measured head (limit {check['max_head_lag_blocks']}); "
            "successful historical scans are not current-head coverage."
        )
    elif reason in (
        "checkpoint_stale",
        "checkpoint_invalid",
        "checkpoint_future_invalid",
    ):
        lines.append(
            f"- RH/Pons primary deployment head checkpoint is {reason.removeprefix('checkpoint_')}: "
            f"{check.get('checkpoint_updated_at')!r}; "
            "a fresh lane heartbeat cannot establish fresh primary-deployment coverage."
        )
    elif reason == "heartbeat_absent":
        lines.append(
            f"- {source} heartbeat: NO successful-poll record exists in "
            "ingest_watchdog_state — the discovery lane has never completed a "
            f"valid poll (SLO {check['staleness_hours']}h)"
        )
    elif reason == "heartbeat_invalid":
        lines.append(
            f"- {source} heartbeat: last_successful_poll_at "
            f"{check['last_successful_poll_at']!r} is UNPARSEABLE — corrupted "
            "heartbeat state; liveness cannot be trusted"
        )
    elif reason == "future_invalid":
        lines.append(
            f"- {source} heartbeat: last_successful_poll_at "
            f"{check['last_successful_poll_at']} is in the FUTURE "
            f"(signed age {check['poll_age_seconds_signed']}s, allowance "
            f"{check['clock_skew_seconds']}s) — clock skew or corrupted state; "
            "liveness cannot be trusted"
        )
    else:
        lines.append(
            f"- {source} heartbeat: last successful poll at "
            f"{check['last_successful_poll_at']} ({check['poll_age_hours']}h ago) "
            f"exceeds SLO {check['staleness_hours']}h — the {label} poller "
            "has stalled (pipeline down, provider unreachable, or schema drift "
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
        + (
            " [timestamp unparseable]"
            if check.get("discovery_timestamp_valid") is False
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


def _cooldown_active(
    state_dir: str,
    now: datetime,
    cooldown_hours: float,
    clock_skew_seconds: float,
    reason: str | None = None,
) -> bool:
    """Whether the send-cooldown window is active.

    Uses the same named clock-skew allowance as the liveness check: a
    cooldown timestamp in the future WITHIN the allowance still counts as
    active; beyond it the state is corrupted — logged and ignored so it can
    never suppress a current breach. Malformed state is eligible-to-send.

    With ``reason`` (RH/Pons), the window only suppresses a breach whose
    reason matches the one last paged: a transient lag page must not silence
    a later outage. Missing/unreadable reason state is eligible-to-send.
    """
    if reason is not None:
        rf = Path(state_dir) / f"last_alert_{_CHECK_KEY}_reason"
        try:
            if rf.read_text().strip() != reason:
                return False
        except OSError:
            return False
    sf = Path(state_dir) / f"last_alert_{_CHECK_KEY}"
    if not sf.exists():
        return False
    try:
        last = _parse_ts(sf.read_text())
    except (OSError, *_TS_PARSE_ERRORS):
        return False
    delta = (now - last).total_seconds()
    if delta < -clock_skew_seconds:
        _log.warning(
            "dex_discovery_watchdog_cooldown_state_invalid_future",
            cooldown_recorded_at=last.isoformat(),
            signed_age_seconds=round(delta, 1),
            clock_skew_seconds=clock_skew_seconds,
        )
        return False
    return delta < cooldown_hours * 3600.0


def _recently_active(check: dict, now: datetime, clock_skew_seconds: float) -> bool:
    """RH heartbeat or attempt written within the SLO (and not future-corrupt)."""
    limit = check.get("staleness_seconds") or 0.0
    for key in ("last_successful_poll_at", "last_attempt_at"):
        raw = check.get(key)
        if not raw:
            continue
        try:
            age = (now - _parse_ts(raw)).total_seconds()
        except _TS_PARSE_ERRORS:
            continue
        if -clock_skew_seconds <= age <= limit:
            return True
    return False


def _write_cooldown_state(
    state_dir: str, now: datetime, reason: str | None = None
) -> None:
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    # Reason FIRST, then clock: a crash between the writes leaves the old clock
    # with the new reason (no suppression gained), never a fresh clock paired
    # with the previous reason, which would silence a real reason change.
    if reason is not None:
        (d / f"last_alert_{_CHECK_KEY}_reason").write_text(reason)
    (d / f"last_alert_{_CHECK_KEY}").write_text(now.isoformat())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--source", choices=tuple(_DISCOVERY_TABLES), default=_HEARTBEAT_SOURCE
    )
    ap.add_argument("--db", required=True)
    ap.add_argument("--max-head-lag-blocks", type=int, default=2000)
    ap.add_argument("--enabled", default="false")
    ap.add_argument("--discovery-enabled", default="false")
    ap.add_argument("--staleness-hours", type=float, default=2.0)
    ap.add_argument("--staleness-minutes", type=float, default=None)
    ap.add_argument("--max-consecutive-failed-passes", type=int, default=None)
    ap.add_argument("--clock-skew-seconds", type=float, default=300.0)
    ap.add_argument("--cooldown-hours", type=float, default=24.0)
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if args.state_dir is None:
        lane_dir = "dex-discovery" if args.source == "dex_discovery" else "rh-pons"
        args.state_dir = f"/var/lib/gecko-alpha/{lane_dir}-watchdog"
    event_prefix = f"{args.source}_watchdog"

    # Boundary validation BEFORE any gate/DB/state access: argparse accepts
    # any float (including nan/inf/negatives), so range-check here.
    config_error = _validate_config(args)
    if config_error is not None:
        _log.error(f"{event_prefix}_invalid_configuration", error=config_error)
        print(json.dumps({"status": "invalid_configuration", "error": config_error}))
        return 1

    if not _is_enabled(args.enabled):
        _log.info(f"{event_prefix}_disabled_noop")
        print(json.dumps({"status": "disabled_noop"}))
        return 0
    now = datetime.now(timezone.utc)
    limit_s = (
        args.staleness_minutes * 60.0 if args.staleness_minutes is not None else None
    )

    def read_state() -> dict:
        return asyncio.run(
            _read_state(
                args.db,
                now,
                args.staleness_hours,
                args.clock_skew_seconds,
                args.source,
                args.max_head_lag_blocks,
                limit_s,
                args.max_consecutive_failed_passes,
            )
        )

    if not _is_enabled(args.discovery_enabled):
        # The lane is intentionally OFF: a liveness page here would represent
        # disablement as failure. But if the RH collector is demonstrably
        # running (recent heartbeat/attempt), the gate this watchdog read is
        # wrong and monitoring would be silently disarmed: breach instead.
        gate_check = None
        if args.source == "rh_pons" and Path(args.db).exists():
            try:
                gate_check = read_state()
            except Exception as exc:
                _log.error(f"{event_prefix}_gate_check_error", error=str(exc))
        if gate_check is None or not _recently_active(
            gate_check, now, args.clock_skew_seconds
        ):
            _log.info(f"{event_prefix}_not_armed_discovery_disabled")
            print(json.dumps({"status": "not_armed_discovery_disabled"}))
            return 0
        gate_check.update(status="breach", reason="enabled_gate_mismatch")
        check = gate_check
    else:
        if not Path(args.db).exists():
            _log.error(f"{event_prefix}_db_missing", db=args.db)
            print(json.dumps({"status": "error", "error": "db_missing"}))
            return 1
        try:
            check = read_state()
        except Exception as exc:  # runtime error → exit 1, never a silent 0
            _log.error(f"{event_prefix}_runtime_error", error=str(exc))
            print(json.dumps({"status": "error", "error": str(exc)}))
            return 1

    _log.info(
        f"{event_prefix}_check",
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
    # Sending is deployed on Linux; read-only checks also run on Windows.
    import fcntl

    lock_dir = Path(args.state_dir)
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_fh = open(lock_dir / "lock", "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        _log.info(f"{event_prefix}_lock_held_skipping")
        print(json.dumps({"status": "lock_held_skipped"}))
        lock_fh.close()
        return 0

    # RH/Pons has several breach reasons under a minutes-scale SLO, so its
    # cooldown is reason-aware; DEX keeps one cooldown for every reason.
    cooldown_reason = check["reason"] if args.source == "rh_pons" else None
    try:
        if _cooldown_active(
            args.state_dir,
            now,
            args.cooldown_hours,
            args.clock_skew_seconds,
            cooldown_reason,
        ):
            _log.info(
                f"{event_prefix}_alert_suppressed_by_cooldown",
                check=_CHECK_KEY,
            )
            print(json.dumps({"status": "breach_cooldown_suppressed", "check": check}))
            return 5
        _log.info(f"{event_prefix}_alert_dispatched", chars=len(message))
        try:
            asyncio.run(_send_via_alerter(message))
        except Exception as exc:
            # Send failure: log + exit 1; cooldown state NOT written, so the
            # next run re-alerts instead of going quiet for a full window.
            _log.error(f"{event_prefix}_alert_failed", error=str(exc))
            print(json.dumps({"status": "error", "error": "alert_dispatch_failed"}))
            return 1
        _log.info(f"{event_prefix}_alert_delivered")
        _write_cooldown_state(args.state_dir, now, cooldown_reason)
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
