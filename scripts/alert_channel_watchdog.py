#!/usr/bin/env python3
"""Alert-channel + digest + narrative + tg-channel freshness watchdog (CLAUDE.md §12a).

Monitors SIX pipeline surfaces in ONE script (operator amendment):

  1. ``tg_alert_log`` — the latest ``outcome='sent'`` row must be newer than
     ``ALERT_SENT_SLO_HOURS`` (default 48). The Telegram alert channel went
     silent 2026-06-25 -> 07-08 (14 days, zero ``sent`` rows) and nobody
     noticed because no watchdog read this table. ALR-08 qualifier: a
     stale/empty channel is only a REAL breach (page) when the pipeline
     demonstrably OPENED trades in the same window (``trade_decision_events``
     rows with ``decision='opened'`` > ``--dispatch-activity-threshold``, cheap
     COUNT). Under universe filter + 24h dedup + quarantine everything is
     BLOCKED, so zero sends across 48h with zero opens is LEGITIMATE, not a dead
     channel — that case is logged (``status='quiet_ok'``) not paged, so
     recurring false pages never train the operator to ignore the watchdog.
  2. ``paper_daily_summary`` — ``MAX(date)`` must be within
     ``DIGEST_SUMMARY_SLO_DAYS`` (default 2; yesterday's row should land by
     ~02:00 UTC daily). The daily digest stopped writing after 2026-06-26.
  3. ``narrative_alerts_inbound`` — ``MAX(received_at)`` must be within
     ``NARRATIVE_INBOUND_SLO_HOURS`` (default 72; the X/narrative inbound feed
     historically flowed daily). The feed went silently dead 2026-06-24 for
     16 days — invisible to the two-table lag-watchdog above because a
     both-sides-quiet feed produces no lag signal, only absence (NAR-02).
  4. ``tg_social_health`` — any configured tg_social channel (``component
     LIKE 'channel:%'``) whose ``last_message_at`` is older than
     ``TG_CHANNEL_STALE_DAYS`` (default 14) is flagged in ONE aggregated line.
     Of ~9 configured channels only 2-3 are active; @alohcooks went silent 72d
     with no cross-cutting alarm (NAR-07). This check is a set-scan, not a
     freshness gate: an absent/empty table is NOT a breach (tg_social is a
     default-off feature; paging while it is off would only train the operator
     to ignore the watchdog).
  5. ``alert_events`` — the newest ``event_type='refresh_completed'`` row must
     be within ``ALERT_EVENTS_SLO_HOURS`` (default 27; the heartbeat is written
     once per ``refresh_all`` run on a daily gate, so 27h tolerates gate jitter
     while still catching a missed day). This is the §12a pairing that ships
     WITH the F3 control-plane ledger: a ledger nobody watches reproduces the
     class it exists to close — the writer works at ship time, a later refactor
     disconnects it, and the gap surfaces months later via an unrelated audit.
     A missing OR empty table is a breach.
  6. ``offhost-last-ok`` — the off-host backup shipper's heartbeat FILE (not a
     table; the shipper does not touch the DB) must be within
     ``OFFHOST_BACKUP_SLO_HOURS`` (default 48). Written only after a copy is
     proven good off-box — under the s3/B2 transport, only after the uploaded
     object's size and content hash are read back and matched — so freshness
     here means "a VERIFIED off-site copy existed at that instant". Gated on
     ``OFFHOST_BACKUP_WATCH_ENABLED`` because off-host shipping is opt-in and a
     default-on check would page on every box that has not configured a
     destination. Once enabled, a missing or unreadable heartbeat IS a breach.

On ANY breach the watchdog sends ONE plain-text Telegram message covering
every breached check that is not inside its send cooldown (``parse_mode=None``
— §12b, table names contain ``_`` which MarkdownV1 would mangle), with §12b
``alert_channel_watchdog_alert_dispatched`` / ``_alert_delivered`` structured
logs around the send. The send passes ``raise_on_failure=True`` so a rejected
page raises (logged ``_alert_failed`` + exit 1) instead of the alerter's
default swallow-and-return — otherwise this watchdog's own page could die
silently. For freshness checks 2-3 (and a MISSING tg_alert_log) a missing OR
empty table is itself a breach with a distinct message (silence is never
ambiguous); check 1's stale/empty case is additionally gated by the ALR-08
dispatch-activity qualifier above (empty-but-no-opens is quiet-legitimate, not a
breach). Read-only on the DB.

Per-table SEND cooldown (``ALERT_CHANNEL_WATCHDOG_COOLDOWN_HOURS``, default 24;
state files under ``--state-dir``): at most one page per breached table per
window, so an hourly cron does not emit ~24 identical pages/day on a standing
breach. Cooldown suppresses the SEND ONLY — a breach still exits 5 (logged
``_alert_suppressed_by_cooldown``); detection is never suppressed.

DEPLOY-WITHOUT-ACTIVATE: an inert no-op unless ``--enabled`` is truthy (the
.sh wrapper wires it from the cron-env var ``ALERT_CHANNEL_WATCHDOG_ENABLED``
— NOT a Settings field, so this adds no config). ``--dry-run`` runs both
checks and prints the full composed alert without sending or touching state
(for tests / manual verification); the disabled and dry-run paths never touch
the network.

ACTIVATION PREREQUISITE (S2-3): activate this watchdog only AFTER PR #429
(daily-digest yesterday-fix) is deployed and has written >=1 fresh
``paper_daily_summary`` row — otherwise the first digest pages are for a
known-broken-being-fixed writer. The cooldown bounds the blast radius to one
page/table/window, but the ordering is still the correct sequence.

Exit codes:
  0 — ok (all checks fresh, disabled no-op, OR alert channel quiet-legitimate:
      0 sent but 0 dispatch activity — logged ``_alert_channel_quiet_legitimate``,
      never paged)
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite
import structlog

_TRUTHY = {"1", "true", "yes", "on"}
_log = structlog.get_logger()


def _configure_logging() -> None:
    """Route structlog to stderr so stdout stays clean for the JSON result.

    Called ONLY from the ``__main__`` / cron entrypoint — NOT at import time.
    Configuring structlog at module scope is a GLOBAL, process-wide mutation:
    importing this module in-process (e.g. from a unit test) would reconfigure
    every other test's logger and silently empty their captured log output.
    Keeping it here makes the import side-effect-free; the subprocess/cron path
    still gets stderr logging because it calls this first.
    """
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))


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


async def _count_dispatch_opens(conn: aiosqlite.Connection, since_iso: str) -> int:
    """Count trade opens (``decision='opened'``) since ``since_iso`` (ALR-08).

    An ``opened`` row in ``trade_decision_events`` is a paper-trade dispatch that
    SHOULD have produced a ``tg_alert_log`` ``'sent'`` row. ``blocked`` rows
    (universe-filtered / deduped / quarantined skips) are the NORMAL state during
    a legitimate quiet stretch and must NOT count — a plain all-rows count would
    false-positive on exactly the all-blocked scenario the qualifier exists to
    distinguish. A missing ``trade_decision_events`` table returns 0 (we cannot
    prove activity, so we fail safe toward silence, not toward a false page)."""
    try:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM trade_decision_events "
            "WHERE decision = 'opened' AND created_at > ?",
            (since_iso,),
        )
        row = await cur.fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return 0
        raise
    return int(row[0]) if row and row[0] is not None else 0


async def _check_alert_sent_rate(
    conn: aiosqlite.Connection,
    slo_hours: int,
    now: datetime,
    dispatch_threshold: int,
) -> dict:
    """Latest tg_alert_log row with outcome='sent' must be within the SLO.

    ALR-08 dispatch-activity qualifier: a stale/empty alert channel is only a
    REAL breach (page) when the pipeline demonstrably OPENED trades in the same
    window yet none was sent — that points at a broken send path. When no trade
    opened (the normal state under universe filter + 24h dedup + quarantine),
    48 quiet hours are LEGITIMATE: ``status='quiet_ok'`` (logged, not paged, exit
    0), so recurring false pages never train the operator to ignore the watchdog.
    A missing tg_alert_log table stays a hard breach (structural, not a quiet
    market)."""
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
                "dispatch_opens_count": None,
                "dispatch_activity_threshold": dispatch_threshold,
                "dispatch_window_hours": slo_hours,
            }
        raise
    last_seen = row[0] if row else None
    if last_seen is not None:
        age_hours = (now - _parse_ts(last_seen)).total_seconds() / 3600.0
        if age_hours <= slo_hours:
            return {
                "table": table,
                "status": "ok",
                "reason": "fresh",
                "last_seen": last_seen,
                "age_hours": round(age_hours, 2),
                "slo_hours": slo_hours,
                "dispatch_opens_count": None,
                "dispatch_activity_threshold": dispatch_threshold,
                "dispatch_window_hours": slo_hours,
            }
        reason = "stale"
    else:
        age_hours = None
        reason = "no_sent_rows"

    # 0 'sent' within the window. Qualify: page only if the pipeline opened
    # trades (dispatch activity) in the SAME window — otherwise quiet-is-legit.
    since = (now - timedelta(hours=slo_hours)).isoformat()
    opens = await _count_dispatch_opens(conn, since)
    real_death = opens > dispatch_threshold
    return {
        "table": table,
        "status": "breach" if real_death else "quiet_ok",
        "reason": reason,
        "last_seen": last_seen,
        "age_hours": round(age_hours, 2) if age_hours is not None else None,
        "slo_hours": slo_hours,
        "dispatch_opens_count": opens,
        "dispatch_activity_threshold": dispatch_threshold,
        "dispatch_window_hours": slo_hours,
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


async def _check_narrative_inbound_rate(
    conn: aiosqlite.Connection, slo_hours: int, now: datetime
) -> dict:
    """MAX(received_at) in narrative_alerts_inbound must be within the SLO (NAR-02).

    The X/narrative inbound feed historically flowed daily; a both-sides-quiet
    feed (Hermes X scanner cron dead) produces no lag signal, so absence is the
    only symptom. A missing OR empty table is a breach (silence is never
    ambiguous), matching the other freshness checks."""
    table = "narrative_alerts_inbound"
    try:
        cur = await conn.execute(
            "SELECT MAX(received_at) FROM narrative_alerts_inbound"
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
            "reason": "no_inbound_rows",
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


async def _check_alert_events_rate(
    conn: aiosqlite.Connection, slo_hours: int, now: datetime
) -> dict:
    """F3 control-plane ledger heartbeat. Four states, because "a row exists"
    and "the pipeline is working" are different claims.

    Keyed on ``refresh_completed`` and NOT on ``MAX(created_at)`` over the whole
    table: every other event type is conditional (a suppression transition, a
    parole refund, a page), so a table whose only rows are conditional events
    reads "fresh" during exactly the stretch in which the refresh pass has
    stopped running. A heartbeat has to be unconditional or it is a symptom.

    But ``refresh_all`` writes its heartbeat even when EVERY per-combo refresh
    FAILED (``refreshed=0, failed=N``) — verified empirically. A fresh row is
    therefore not evidence of health, and treating it as such would leave the
    watchdog reporting OK through exactly the outage it exists to catch. So a
    HEALTHY heartbeat is one whose payload carries ``refreshed > 0``; a fresh
    heartbeat with ``refreshed == 0`` breaches immediately, distinctly, and
    does not wait out the SLO.

    Before the first refresh ever runs there is no heartbeat at all, only the
    ``ledger_installed`` epoch row the migration seeds. Ageing from that row is
    what keeps the deploy boundary quiet WITHOUT waiving §12a: the first
    nightly refresh gets one full SLO to arrive, and if it never does, the
    epoch row goes stale and pages truthfully.

    A missing table, or a table with neither a heartbeat nor an epoch row, is a
    breach — silence is never ambiguous."""
    table = "alert_events"
    base = {"table": table, "slo_hours": slo_hours, "refreshed": None}
    try:
        cur = await conn.execute(
            "SELECT created_at, state_json FROM alert_events "
            "WHERE event_type = 'refresh_completed' "
            "ORDER BY created_at DESC LIMIT 1"
        )
        newest = await cur.fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return {
                **base,
                "status": "breach",
                "reason": "table_absent",
                "last_seen": None,
                "age_hours": None,
            }
        raise

    if newest is None:
        # No refresh has ever completed. Age from the install epoch instead.
        cur = await conn.execute(
            "SELECT MAX(created_at) FROM alert_events "
            "WHERE event_type = 'ledger_installed'"
        )
        row = await cur.fetchone()
        installed_at = row[0] if row else None
        if installed_at is None:
            return {
                **base,
                "status": "breach",
                "reason": "no_refresh_completed_rows",
                "last_seen": None,
                "age_hours": None,
            }
        age_hours = (now - _parse_ts(installed_at)).total_seconds() / 3600.0
        breached = age_hours > slo_hours
        return {
            **base,
            "status": "breach" if breached else "ok",
            "reason": (
                "no_successful_refresh_since_install"
                if breached
                else "awaiting_first_refresh"
            ),
            "last_seen": installed_at,
            "age_hours": round(age_hours, 2),
        }

    last_seen, state_json = newest[0], newest[1]
    age_hours = (now - _parse_ts(last_seen)).total_seconds() / 3600.0

    # AGE FIRST. The payload-quality reasons below all describe a pass that is
    # RUNNING, so they must not pre-empt staleness — a 100h-old heartbeat that
    # happens to say `refreshed: 0` is a stalled pass, not a failing one, and
    # paging "the refresh pass IS running" about it would be false.
    if age_hours > slo_hours:
        return {
            **base,
            "status": "breach",
            "reason": "stale",
            "last_seen": last_seen,
            "age_hours": round(age_hours, 2),
        }

    try:
        payload = json.loads(state_json) or {}
    except (ValueError, TypeError):
        payload = None
    refreshed = payload.get("refreshed") if isinstance(payload, dict) else None
    if not isinstance(refreshed, int):
        # Cannot PROVE the pass succeeded. Fail toward the page: an unreadable
        # heartbeat is a broken writer, not a healthy one.
        return {
            **base,
            "status": "breach",
            "reason": "heartbeat_unreadable",
            "last_seen": last_seen,
            "age_hours": round(age_hours, 2),
        }

    if refreshed <= 0:
        # Fresh but unproductive. Two very different causes, and the page has
        # to say which: an EMPTY enumeration means there was nothing to refresh
        # (a restored or freshly-seeded DB), while a non-empty enumeration that
        # refreshed nothing means every per-combo refresh failed. Claiming the
        # latter for the former sends the operator hunting a fault that does
        # not exist.
        enumerated = payload.get("combos_enumerated")
        empty_universe = enumerated == 0
        return {
            **base,
            "status": "breach",
            "reason": (
                "no_combos_enumerated" if empty_universe else "refresh_all_failing"
            ),
            "last_seen": last_seen,
            "age_hours": round(age_hours, 2),
            "refreshed": refreshed,
            "combos_enumerated": enumerated,
        }
    return {
        **base,
        "status": "ok",
        "reason": "fresh",
        "last_seen": last_seen,
        "age_hours": round(age_hours, 2),
        "refreshed": refreshed,
    }


def _check_offhost_backup_freshness(
    heartbeat_file: str, slo_hours: int, now: datetime, *, watch_enabled: bool
) -> dict:
    """Off-host backup freshness, read from the shipper's heartbeat FILE.

    The only check here that is not a DB table, because the thing being watched
    does not touch the DB: ``scripts/gecko-backup-offhost.sh`` writes a unix
    timestamp to ``offhost-last-ok`` after — and only after — the copy is
    proven good off-box (under the s3 transport, after the uploaded object's
    size and content hash are read back and matched). So a fresh heartbeat here
    means "a VERIFIED off-host copy of a real backup existed at that instant",
    which is the only claim worth monitoring: the local backup stack already
    has its own watchdog, and it is worthless in the failure mode this lane
    exists for (the VPS provider's failure domain taking the box with it).

    ``watch_enabled`` is the deploy-without-activate gate. Off-host shipping is
    opt-in and the destination is unset by default, so paging for a missing
    heartbeat before the operator has configured a bucket would be a guaranteed
    false page on every box — the fastest way to train an operator to ignore
    this watchdog. Once the operator turns the watch on, silence stops being
    ambiguous and a missing or unreadable heartbeat IS a breach, exactly like
    the DB-table checks: "the shipper has never run" and "the shipper broke"
    are both things to be woken for."""
    table = "offhost_backup"
    base = {
        "table": table,
        "slo_hours": slo_hours,
        "heartbeat_file": heartbeat_file,
    }
    if not watch_enabled:
        return {
            **base,
            "status": "ok",
            "reason": "watch_disabled",
            "last_seen": None,
            "age_hours": None,
        }

    path = Path(heartbeat_file).expanduser()
    if not path.exists():
        return {
            **base,
            "status": "breach",
            "reason": "heartbeat_absent",
            "last_seen": None,
            "age_hours": None,
        }
    try:
        epoch = int(path.read_text().strip())
    except (ValueError, OSError):
        # A truncated / empty / non-numeric heartbeat cannot prove anything.
        # Fail toward the page: an unreadable heartbeat is a broken shipper.
        return {
            **base,
            "status": "breach",
            "reason": "heartbeat_unreadable",
            "last_seen": None,
            "age_hours": None,
        }

    last_seen = datetime.fromtimestamp(epoch, tz=timezone.utc)
    age_hours = (now - last_seen).total_seconds() / 3600.0
    breached = age_hours > slo_hours
    return {
        **base,
        "status": "breach" if breached else "ok",
        "reason": "stale" if breached else "fresh",
        "last_seen": last_seen.isoformat(),
        "age_hours": round(age_hours, 2),
    }


async def _check_tg_channel_staleness(
    conn: aiosqlite.Connection, stale_days: int, now: datetime
) -> dict:
    """Per-channel tg_social staleness (NAR-07).

    Scans tg_social_health for configured channels (``component LIKE 'channel:%'``,
    the format the listener writes) and flags any whose ``last_message_at`` is
    older than ``stale_days`` in ONE aggregated breach line (@handle + age). This
    is a SET-SCAN, not a freshness gate: an absent/empty table or all-fresh
    channels is ``ok`` — tg_social is a default-off feature, and paging on its
    absence would only produce false pages that erode watchdog credibility."""
    table = "tg_social_health"
    try:
        cur = await conn.execute(
            "SELECT component, last_message_at FROM tg_social_health "
            "WHERE component LIKE 'channel:%'"
        )
        rows = await cur.fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return {
                "table": table,
                "status": "ok",
                "reason": "table_absent",
                "stale_channels": [],
                "stale_days": stale_days,
            }
        raise
    stale: list[dict] = []
    for component, last_at in rows:
        if last_at is None:
            continue
        try:
            age_days = (now - _parse_ts(last_at)).total_seconds() / 86400.0
        except (ValueError, TypeError):
            continue
        if age_days > stale_days:
            stale.append(
                {
                    "handle": component.removeprefix("channel:"),
                    "age_days": round(age_days, 1),
                }
            )
    stale.sort(key=lambda c: c["age_days"], reverse=True)
    return {
        "table": table,
        "status": "breach" if stale else "ok",
        "reason": "channels_stale" if stale else "all_fresh",
        "stale_channels": stale,
        "stale_days": stale_days,
    }


async def _evaluate(
    db_path: str,
    *,
    sent_slo_hours: int,
    digest_slo_days: int,
    narrative_slo_hours: int,
    tg_channel_stale_days: int,
    dispatch_activity_threshold: int,
    alert_events_slo_hours: int,
    offhost_backup_slo_hours: int,
    offhost_heartbeat_file: str,
    offhost_watch_enabled: bool,
    now: datetime,
) -> dict:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        alert = await _check_alert_sent_rate(
            conn, sent_slo_hours, now, dispatch_activity_threshold
        )
        digest = await _check_digest_write_rate(conn, digest_slo_days, now)
        narrative = await _check_narrative_inbound_rate(conn, narrative_slo_hours, now)
        tg_channel = await _check_tg_channel_staleness(conn, tg_channel_stale_days, now)
        alert_events = await _check_alert_events_rate(conn, alert_events_slo_hours, now)
    # Filesystem-only; no DB connection needed (see the check's docstring).
    offhost = _check_offhost_backup_freshness(
        offhost_heartbeat_file,
        offhost_backup_slo_hours,
        now,
        watch_enabled=offhost_watch_enabled,
    )
    return {
        "alert_sent_rate": alert,
        "digest_write_rate": digest,
        "narrative_inbound_rate": narrative,
        "tg_channel_staleness": tg_channel,
        "alert_events_rate": alert_events,
        "offhost_backup_freshness": offhost,
    }


def _compose_message(checks: dict, include: list[str]) -> str:
    """Plain-text (no Markdown) operator alert naming each included breached
    table. ``include`` is the subset of breached check keys the cooldown gate
    currently allows to page (per-table dedup, S2-2)."""
    lines = ["gecko-alpha alert-channel watchdog: freshness breach"]

    a = checks["alert_sent_rate"]
    if "alert_sent_rate" in include and a["status"] == "breach":
        if a["reason"] == "table_absent":
            lines.append(
                "- tg_alert_log: table missing/absent — no alert-send audit "
                f"trail exists (SLO {a['slo_hours']}h)"
            )
        elif a["reason"] == "no_sent_rows":
            lines.append(
                "- tg_alert_log: NO 'sent' rows in the last "
                f"{a['slo_hours']}h AND the pipeline opened "
                f"{a['dispatch_opens_count']} trade(s) in the same window "
                f"(> {a['dispatch_activity_threshold']}) — the alert-send path "
                "is likely broken (not a quiet market)"
            )
        else:
            lines.append(
                f"- tg_alert_log: last 'sent' alert at {a['last_seen']} "
                f"({a['age_hours']}h ago) exceeds SLO {a['slo_hours']}h AND the "
                f"pipeline opened {a['dispatch_opens_count']} trade(s) in the "
                f"same window (> {a['dispatch_activity_threshold']}) — the "
                "alert-send path is likely broken (not a quiet market)"
            )

    d = checks["digest_write_rate"]
    if "digest_write_rate" in include and d["status"] == "breach":
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

    n = checks["narrative_inbound_rate"]
    if "narrative_inbound_rate" in include and n["status"] == "breach":
        if n["reason"] == "table_absent":
            lines.append(
                "- narrative_alerts_inbound: table missing/absent — no X/narrative "
                f"inbound audit trail exists (SLO {n['slo_hours']}h)"
            )
        elif n["reason"] == "no_inbound_rows":
            lines.append(
                "- narrative_alerts_inbound: NO rows in table — the X/narrative "
                f"inbound feed has never delivered or is fully dark (SLO {n['slo_hours']}h)"
            )
        else:
            lines.append(
                f"- narrative_alerts_inbound: last inbound row at {n['last_seen']} "
                f"({n['age_hours']}h ago) exceeds SLO {n['slo_hours']}h — the "
                "X/narrative inbound feed (Hermes X scanner) is likely dead"
            )

    t = checks["tg_channel_staleness"]
    if "tg_channel_staleness" in include and t["status"] == "breach":
        listing = ", ".join(
            f"{c['handle']} ({c['age_days']}d)" for c in t["stale_channels"]
        )
        lines.append(
            f"- tg_social_health: {len(t['stale_channels'])} channel(s) silent "
            f"> {t['stale_days']}d — {listing}"
        )

    e = checks["alert_events_rate"]
    if "alert_events_rate" in include and e["status"] == "breach":
        if e["reason"] == "table_absent":
            lines.append(
                "- alert_events: table missing/absent — the control-plane event "
                f"ledger does not exist (SLO {e['slo_hours']}h)"
            )
        elif e["reason"] == "no_refresh_completed_rows":
            lines.append(
                "- alert_events: NO 'refresh_completed' rows AND no "
                "'ledger_installed' epoch row — the ledger is present but empty, "
                f"so its own install marker is missing too (SLO {e['slo_hours']}h)"
            )
        elif e["reason"] == "no_successful_refresh_since_install":
            lines.append(
                f"- alert_events: ledger installed at {e['last_seen']} "
                f"({e['age_hours']}h ago) and NO combo refresh has completed "
                f"successfully since (SLO {e['slo_hours']}h) — the nightly "
                "refresh has not run, or has failed every time"
            )
        elif e["reason"] == "refresh_all_failing":
            lines.append(
                f"- alert_events: the refresh pass ran {e['age_hours']}h ago "
                f"(within SLO) over {e['combos_enumerated']} combo(s) but "
                "refreshed 0 — every per-combo refresh is failing, so "
                "suppression economics are frozen while the heartbeat looks "
                "healthy"
            )
        elif e["reason"] == "no_combos_enumerated":
            lines.append(
                f"- alert_events: the refresh pass ran {e['age_hours']}h ago "
                "(within SLO) but enumerated 0 combos — there is nothing to "
                "refresh, which on a live box means the trade history it reads "
                "is empty (restored/reseeded DB?), not that refresh is broken"
            )
        elif e["reason"] == "heartbeat_unreadable":
            lines.append(
                f"- alert_events: the heartbeat at {e['last_seen']} is within "
                "SLO but carries no readable refreshed-count — the heartbeat "
                "writer is broken, so refresh health cannot be confirmed "
                "either way"
            )
        else:
            lines.append(
                f"- alert_events: last 'refresh_completed' at {e['last_seen']} "
                f"({e['age_hours']}h ago) exceeds SLO {e['slo_hours']}h — the combo "
                "refresh pass has stalled, so suppression/alert transitions are "
                "no longer being recorded"
            )

    o = checks["offhost_backup_freshness"]
    if "offhost_backup_freshness" in include and o["status"] == "breach":
        if o["reason"] == "heartbeat_absent":
            lines.append(
                "- offhost_backup: NO heartbeat at "
                f"{o['heartbeat_file']} — the off-host shipper has never "
                f"completed a verified upload (SLO {o['slo_hours']}h). Every "
                "backup that exists is on the same box as the DB."
            )
        elif o["reason"] == "heartbeat_unreadable":
            lines.append(
                f"- offhost_backup: the heartbeat at {o['heartbeat_file']} is "
                "empty or non-numeric — the off-host shipper is broken and the "
                "age of the last off-site copy cannot be established"
            )
        else:
            lines.append(
                f"- offhost_backup: last VERIFIED off-host upload at "
                f"{o['last_seen']} ({o['age_hours']}h ago) exceeds SLO "
                f"{o['slo_hours']}h — nothing has left the box since, so a "
                "provider-level loss would take the DB and every backup with it"
            )

    lines.append("Check the pipeline/digest cron and the Telegram delivery path.")
    return "\n".join(lines)


def _compose_quiet_message(check: dict) -> str:
    """ALR-08 distinct log-only note for a LEGITIMATELY quiet alert channel:
    0 'sent' within the SLO but the pipeline opened <= threshold trades in the
    same window (universe filter + 24h dedup + quarantine legitimately produce
    zero sends). Logged, never paged — protects watchdog credibility."""
    if check["reason"] == "no_sent_rows":
        why = f"NO 'sent' rows in the last {check['slo_hours']}h"
    else:
        why = (
            f"last 'sent' alert {check['age_hours']}h ago "
            f"(> {check['slo_hours']}h SLO)"
        )
    return (
        "tg_alert_log quiet but LEGITIMATE: "
        f"{why} AND the pipeline opened only {check['dispatch_opens_count']} "
        f"trade(s) in the window (<= {check['dispatch_activity_threshold']} "
        "dispatch-activity threshold) — the pipeline was idle, not the send "
        "path. No page (log-only)."
    )


async def _send_via_alerter(text: str) -> None:
    """Real plain-text Telegram send. Lazy heavy imports (aiohttp + alerter).

    ``raise_on_failure=True`` is load-bearing (§12b, S2-1): the default alerter
    SWALLOWS non-200 / network errors — it logs a warning and returns without
    raising (scout/alerter.py:257-278). Without this flag the ``_alert_delivered``
    log below would fire even when Telegram rejected the page, so in the exact
    dark-channel scenario this watchdog exists to catch, its OWN alert would die
    silently while reporting success. With the flag, a failed send raises and the
    caller logs ``_alert_failed`` + exits 1 instead.
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
            source="alert_channel_watchdog",
        )


# Indirection point so tests can stub the network send without importing aiohttp.
_SEND = _send_via_alerter


def _dispatch_alert(text: str) -> None:
    """§12b log triplet around the send. Propagates on delivery failure so the
    caller logs ``_alert_failed`` and exits non-zero — never a silent success."""
    _log.info("alert_channel_watchdog_alert_dispatched", chars=len(text))
    asyncio.run(_SEND(text))
    _log.info("alert_channel_watchdog_alert_delivered")


def _cooldown_state(
    state_dir: str, table: str, now: datetime, cooldown_hours: float
) -> tuple[bool, str | None]:
    """Per-table send cooldown (S2-2). Returns (eligible, next_eligible_iso).

    A missing, corrupt, or expired state file means eligible (the cooldown
    gates the SEND only — the breach is always detected). The state file holds
    the ISO timestamp of the last dispatched page for this table.
    """
    sf = Path(state_dir) / f"last_alert_{table}"
    if not sf.exists():
        return True, None
    try:
        last = _parse_ts(sf.read_text().strip())
    except Exception:
        return True, None
    if (now - last).total_seconds() / 3600.0 >= cooldown_hours:
        return True, None
    return False, (last + timedelta(hours=cooldown_hours)).isoformat()


def _write_cooldown_state(state_dir: str, table: str, now: datetime) -> None:
    """Record a successful dispatch time for ``table`` (written AFTER send)."""
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"last_alert_{table}").write_text(now.isoformat())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="scout.db")
    parser.add_argument("--enabled", default="false")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sent-slo-hours", type=int, default=48)
    parser.add_argument("--digest-slo-days", type=int, default=2)
    parser.add_argument("--narrative-inbound-slo-hours", type=int, default=72)
    parser.add_argument("--tg-channel-stale-days", type=int, default=14)
    # F3: 27h, not 24h — the ledger heartbeat is written once per refresh_all
    # run on a daily gate, so a 24h SLO would page on ordinary jitter in when
    # the gate fires. 27h tolerates the drift while still catching a missed day.
    parser.add_argument("--alert-events-slo-hours", type=int, default=27)
    # ALR-08: a stale/empty tg_alert_log breaches only when the pipeline opened
    # MORE than this many trades in the window (0 => any open with 0 sent pages;
    # raise it to tolerate the dedup tail). See the qualifier in
    # _check_alert_sent_rate.
    parser.add_argument("--dispatch-activity-threshold", type=int, default=0)
    # Off-host backup shipper freshness. 48h, not 27h: the shipper rides the
    # daily backup schedule, and one missed night on a lane whose purpose is
    # disaster recovery is not worth a page while two in a row is.
    parser.add_argument("--offhost-backup-slo-hours", type=int, default=48)
    parser.add_argument(
        "--offhost-heartbeat-file",
        default="/var/lib/gecko-alpha/backup-rotation/offhost-last-ok",
    )
    # Deploy-without-activate: off-host shipping is opt-in, so the watch stays
    # off until the operator has actually configured a destination.
    parser.add_argument("--offhost-backup-watch-enabled", default="false")
    parser.add_argument("--cooldown-hours", type=int, default=24)
    parser.add_argument(
        "--state-dir", default="/var/lib/gecko-alpha/alert-channel-watchdog"
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
                sent_slo_hours=args.sent_slo_hours,
                digest_slo_days=args.digest_slo_days,
                narrative_slo_hours=args.narrative_inbound_slo_hours,
                tg_channel_stale_days=args.tg_channel_stale_days,
                dispatch_activity_threshold=args.dispatch_activity_threshold,
                alert_events_slo_hours=args.alert_events_slo_hours,
                offhost_backup_slo_hours=args.offhost_backup_slo_hours,
                offhost_heartbeat_file=args.offhost_heartbeat_file,
                offhost_watch_enabled=_is_enabled(args.offhost_backup_watch_enabled),
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

    # ALR-08: a legitimately-quiet alert channel (0 sent + no dispatch activity)
    # is logged with a DISTINCT event and surfaced in the JSON, but never paged
    # (its status is 'quiet_ok', not 'breach', so it is absent from `breaches`).
    alert_check = checks["alert_sent_rate"]
    quiet_msg = None
    if alert_check["status"] == "quiet_ok":
        quiet_msg = _compose_quiet_message(alert_check)
        _log.info(
            "alert_channel_watchdog_alert_channel_quiet_legitimate",
            reason=alert_check["reason"],
            last_seen=alert_check["last_seen"],
            dispatch_opens_count=alert_check["dispatch_opens_count"],
            dispatch_activity_threshold=alert_check["dispatch_activity_threshold"],
            window_hours=alert_check["dispatch_window_hours"],
        )

    if not breaches:
        # Healthy: one-line OK log carrying every check's freshness age.
        _log.info(
            "alert_channel_watchdog_ok",
            alert_last_seen=checks["alert_sent_rate"]["last_seen"],
            alert_age_hours=checks["alert_sent_rate"]["age_hours"],
            digest_last_seen=checks["digest_write_rate"]["last_seen"],
            digest_age_days=checks["digest_write_rate"]["age_days"],
            narrative_last_seen=checks["narrative_inbound_rate"]["last_seen"],
            narrative_age_hours=checks["narrative_inbound_rate"]["age_hours"],
            tg_channel_stale_count=len(
                checks["tg_channel_staleness"]["stale_channels"]
            ),
            alert_events_last_seen=checks["alert_events_rate"]["last_seen"],
            alert_events_age_hours=checks["alert_events_rate"]["age_hours"],
            offhost_backup_last_seen=checks["offhost_backup_freshness"]["last_seen"],
            offhost_backup_age_hours=checks["offhost_backup_freshness"]["age_hours"],
            offhost_backup_reason=checks["offhost_backup_freshness"]["reason"],
        )
        out = {"ok": True, "breaches": 0, "checks": checks}
        if quiet_msg is not None:
            out["quiet_legitimate"] = quiet_msg
        print(json.dumps(out, sort_keys=True, default=str))
        return 0

    # dry-run: full preview of the current breach, no cooldown/state I/O, no send.
    if args.dry_run:
        out = {
            "ok": False,
            "breaches": len(breaches),
            "checks": checks,
            "message": _compose_message(checks, breaches),
            "dry_run": True,
            "sent": False,
        }
        if quiet_msg is not None:
            out["quiet_legitimate"] = quiet_msg
        print(json.dumps(out, sort_keys=True, default=str))
        return 5

    # Real path: per-table cooldown gates the SEND (never the detection, S2-2).
    to_send: list[str] = []
    suppressed: list[str] = []
    for key in breaches:
        table = checks[key]["table"]
        eligible, next_eligible = _cooldown_state(
            args.state_dir, table, now, args.cooldown_hours
        )
        if eligible:
            to_send.append(key)
        else:
            suppressed.append(key)
            _log.info(
                "alert_channel_watchdog_alert_suppressed_by_cooldown",
                table=table,
                next_eligible=next_eligible,
                cooldown_hours=args.cooldown_hours,
            )

    sent = False
    if to_send:
        message = _compose_message(checks, to_send)
        try:
            _dispatch_alert(message)
        except Exception as exc:
            # Alert-send failure must surface, not be swallowed (§12b, S2-1).
            # State is NOT written, so the next run re-alerts.
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
        for key in to_send:
            _write_cooldown_state(args.state_dir, checks[key]["table"], now)
        sent = True

    out = {
        "ok": False,
        "breaches": len(breaches),
        "checks": checks,
        "sent": sent,
        "sent_tables": [checks[k]["table"] for k in to_send],
        "suppressed_by_cooldown": [checks[k]["table"] for k in suppressed],
    }
    if quiet_msg is not None:
        out["quiet_legitimate"] = quiet_msg
    print(json.dumps(out, sort_keys=True, default=str))
    return 5


if __name__ == "__main__":
    _configure_logging()
    sys.exit(main())
