"""Stuck-execution watchdog for the Solana lane (§12a, per-state shape).

*** A ROW-RATE SLO WOULD BE THE WRONG SHAPE HERE. ***
``solana_executions`` is only written when a trade runs, so silence is the
NORMAL state of a supervised lane and "no rows in the last hour" says nothing
at all. The analogue of a freshness SLO for a table like this is STUCK STATE: a
row that entered a non-terminal state and has not moved since. That is a real
failure — a process that died between two durable writes, an operator who
walked away from the prompt, a submission whose fate nobody resolved — and
unlike a row rate it is unambiguous.

Per-state thresholds, because the states mean different things
--------------------------------------------------------------
``awaiting_authorization`` is a human reading a screen and may legitimately sit
for a long time. ``submission_attempted`` is a POST that either returned or did
not, and a row still sitting there minutes later means a transaction may exist
that nobody is resolving — the single most expensive state to leave unattended.
One threshold across both would either page on every slow approval or stay
silent through the dangerous one, so the mapping from state to threshold is
explicit (``STATE_THRESHOLD_KEYS``) and every value is configuration.

The query is cheap by construction: ``idx_solana_executions_state`` is on
``(state, updated_at)``, so each state's scan is an indexed range on
``updated_at`` rather than a table scan with a filter.

Alerting follows CLAUDE.md §12b
-------------------------------
``parse_mode=None`` — the message body carries state names like
``submission_attempted`` and ``awaiting_authorization``, and Telegram's
MarkdownV1 parser reads those underscores as italics markers, mangling the
body while still returning HTTP 200. That is the documented worked example for
this exact failure, and this alert would hit it on every fire.

``*_alert_dispatched`` / ``*_alert_delivered`` are emitted around the call
because the default alerter logs only failures — without the pair, "no logs
about this alert" is ambiguous between "delivered cleanly" and "the alert was
never attempted".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import structlog

from scout.config import Settings

log = structlog.get_logger(__name__)

# State -> the Settings key holding its staleness threshold, in seconds.
#
# Deliberately a declared mapping rather than an if-chain: adding a state to
# the lifecycle without giving it a threshold should be a visible omission, and
# `unmonitored_states()` below turns it into an assertable one.
STATE_THRESHOLD_KEYS: dict[str, str] = {
    # A human is reading the decision screen. Generous on purpose.
    "awaiting_authorization": "SOLANA_STUCK_AWAITING_AUTHORIZATION_SEC",
    # Machine steps that take seconds. Sitting here means the process died.
    "quote_created": "SOLANA_STUCK_PRE_SUBMISSION_SEC",
    "transaction_built": "SOLANA_STUCK_PRE_SUBMISSION_SEC",
    "simulation_passed": "SOLANA_STUCK_PRE_SUBMISSION_SEC",
    "authorized": "SOLANA_STUCK_PRE_SUBMISSION_SEC",
    "signed": "SOLANA_STUCK_PRE_SUBMISSION_SEC",
    # *** THE ONE THAT MUST NOT SIT. *** A transaction may exist and nobody is
    # asking the cluster about it.
    "submission_attempted": "SOLANA_STUCK_SUBMISSION_SEC",
    # Landed, waiting on confirmation / finalization / reconciliation.
    "landed": "SOLANA_STUCK_POST_SUBMISSION_SEC",
    "confirmed": "SOLANA_STUCK_POST_SUBMISSION_SEC",
    "finalized": "SOLANA_STUCK_POST_SUBMISSION_SEC",
    # Already escalated and already blocking the lane. Alerted anyway, on a
    # longer fuse, because a blocked lane that nobody remembers is a lane that
    # silently stops trading.
    "submission_unknown": "SOLANA_STUCK_UNKNOWN_SUBMISSION_SEC",
}

# In-memory dedup, keyed by (decision_id, state). A row that MOVES to a new
# stuck state re-alerts — that is new information — while a row sitting in the
# same state does not page every run. Resets on restart, which is the
# conservative direction: a restart re-alerts about anything still stuck.
_alerted: set[tuple[str, str]] = set()


def _reset_for_tests() -> None:
    _alerted.clear()


@dataclass(frozen=True)
class StuckExecution:
    """One execution that has not moved inside its state's threshold."""

    decision_id: str
    state: str
    mode: str
    age_seconds: float
    threshold_seconds: float
    updated_at: str
    expected_signature: str | None
    live_trade_id: int | None

    @property
    def blocks_the_lane(self) -> bool:
        """Whether a transaction may exist. Drives how loudly this reads."""
        return self.state in (
            "submission_attempted",
            "landed",
            "confirmed",
            "finalized",
            "submission_unknown",
        )

    def as_evidence(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "state": self.state,
            "mode": self.mode,
            "age_seconds": round(self.age_seconds, 1),
            "threshold_seconds": self.threshold_seconds,
            "updated_at": self.updated_at,
            "expected_signature": self.expected_signature,
            "live_trade_id": self.live_trade_id,
            "blocks_the_lane": self.blocks_the_lane,
        }


def threshold_seconds(state: str, settings: Settings) -> float | None:
    """This state's staleness threshold, or None if it is not monitored."""
    key = STATE_THRESHOLD_KEYS.get(state)
    return None if key is None else float(getattr(settings, key))


def unmonitored_states(
    all_states: tuple[str, ...], terminal: tuple[str, ...]
) -> list[str]:
    """Non-terminal states with no threshold — a monitoring hole, by name.

    Exists so a future state added to the lifecycle cannot quietly become
    unwatched: a test asserts this returns empty, and adding a state without a
    threshold fails it.
    """
    return sorted(
        s for s in all_states if s not in terminal and s not in STATE_THRESHOLD_KEYS
    )


async def find_stuck_executions(
    db: Any, settings: Settings, *, now: datetime | None = None
) -> list[StuckExecution]:
    """Executions past their state's threshold, most-blocking first.

    One indexed query per monitored state rather than one scan with a CASE:
    ``idx_solana_executions_state`` is on ``(state, updated_at)``, so
    ``WHERE state = ? AND updated_at < ?`` is exactly the shape the index
    serves. Eleven small range scans beat one table scan on a table that will
    grow for the life of the lane.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    reference = now or datetime.now(timezone.utc)

    found: list[StuckExecution] = []
    for state, key in STATE_THRESHOLD_KEYS.items():
        limit = float(getattr(settings, key))
        cutoff = _iso(reference.timestamp() - limit)
        cur = await db._conn.execute(
            "SELECT decision_id, mode, updated_at, expected_signature, live_trade_id "
            "FROM solana_executions WHERE state = ? AND updated_at < ? "
            "ORDER BY updated_at",
            (state, cutoff),
        )
        for row in await cur.fetchall():
            age = _age_seconds(row[2], reference)
            if age is None:
                # An unparseable timestamp cannot be shown to be inside the
                # threshold, and this row already matched the cutoff string
                # comparison. Reported with an age of exactly the threshold so
                # it is never silently dropped.
                age = limit
            found.append(
                StuckExecution(
                    decision_id=str(row[0]),
                    state=state,
                    mode=str(row[1] or ""),
                    age_seconds=age,
                    threshold_seconds=limit,
                    updated_at=str(row[2]),
                    expected_signature=row[3],
                    live_trade_id=row[4],
                )
            )
    # Blocking states first, then oldest — the order an operator should read.
    found.sort(key=lambda s: (not s.blocks_the_lane, -s.age_seconds))
    return found


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _age_seconds(updated_at: Any, reference: datetime) -> float | None:
    try:
        stamped = datetime.fromisoformat(str(updated_at))
    except (TypeError, ValueError):
        return None
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=timezone.utc)
    return (reference - stamped).total_seconds()


async def check_stuck_executions(
    db: Any,
    session: Any,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Find stuck executions, log them, and alert the operator once each.

    Returns a summary rather than raising: this runs from a scheduler, and a
    watchdog that dies on the state it exists to report is worse than useless.
    """
    if not settings.SOLANA_EXECUTION_WATCHDOG_ENABLED:
        return {"enabled": False, "stuck": [], "alerted": 0}

    stuck = await find_stuck_executions(db, settings, now=now)
    if not stuck:
        log.info("solana_execution_watchdog", status="ok", stuck=0)
        return {"enabled": True, "stuck": [], "alerted": 0}

    for entry in stuck:
        log.warning("solana_execution_stuck", **entry.as_evidence())

    fresh = [e for e in stuck if (e.decision_id, e.state) not in _alerted]
    if fresh:
        await _alert(fresh, session, settings)
    return {
        "enabled": True,
        "stuck": [e.as_evidence() for e in stuck],
        "alerted": len(fresh),
    }


async def _alert(stuck: list[StuckExecution], session: Any, settings: Settings) -> None:
    """One plain-text operator alert covering everything newly stuck."""
    from scout import alerter  # lazy: keeps aiohttp off the import path

    blocking = [e for e in stuck if e.blocks_the_lane]
    header = f"ALERT Solana lane: {len(stuck)} execution(s) stuck" + (
        f", {len(blocking)} with a transaction possibly in flight" if blocking else ""
    )
    body_lines = [header]
    for entry in stuck:
        body_lines.append(
            f"- {entry.decision_id} in {entry.state} for "
            f"{entry.age_seconds / 60:.0f}m (threshold "
            f"{entry.threshold_seconds / 60:.0f}m)"
            + (f", sig {entry.expected_signature}" if entry.expected_signature else "")
        )
    if blocking:
        body_lines.append(
            "DO NOT rebuild or resubmit. Run: python -m scout.live.solana_lane "
            "resolve --decision-id <id>"
        )
    body = "\n".join(body_lines)

    keys = [(e.decision_id, e.state) for e in stuck]
    log.info(
        "solana_execution_watchdog_alert_dispatched",
        count=len(stuck),
        blocking=len(blocking),
        decision_ids=[e.decision_id for e in stuck],
    )
    try:
        await alerter.send_telegram_message(
            body,
            session,
            settings,
            # *** parse_mode=None IS LOAD-BEARING. *** Every state name in this
            # body contains an underscore; MarkdownV1 would eat them as italics
            # markers and Telegram would still answer 200.
            parse_mode=None,
            raise_on_failure=True,
            source="solana_execution_watchdog",
        )
    except Exception:
        log.exception("solana_execution_watchdog_alert_failed", count=len(stuck))
        # Deliberately NOT marked as alerted: a failed delivery must be retried
        # on the next run, or the one alert that mattered is lost silently.
        return
    _alerted.update(keys)
    log.info(
        "solana_execution_watchdog_alert_delivered",
        count=len(stuck),
        blocking=len(blocking),
    )
