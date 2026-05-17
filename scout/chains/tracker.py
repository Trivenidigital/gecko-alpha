"""Chain tracker — pattern matching engine + main async loop + boost query."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from scout.chains.events import (
    load_recent_events,
    prune_old_events,
    safe_emit,
)
from scout.chains.mcap_fetcher import (
    FetchResult,
    FetchStatus,
    McapFetcher,
    fetch_token_fdv,
)
from scout.chains.models import ActiveChain, ChainEvent, ChainPattern
from scout.chains.patterns import (
    evaluate_condition,
    load_active_patterns,
    seed_built_in_patterns,
)
from scout.config import Settings
from scout.db import Database

logger = structlog.get_logger()

# BL-071a' v3 (R1-S3): persistent-failure ERROR is escalation-rate-limited
# to avoid log wallpaper. Module-level state tracks the previous alert's
# (count, oldest_age) so we only re-fire on (a) more stuck rows or
# (b) oldest_age increased ≥+24h since last alert. Reset to None when
# stuck-row count drops to zero (logged via *_cleared INFO event).
_persistent_failure_alert_state: dict | None = None


def _parse_time(value) -> datetime:
    """Coerce a value to a timezone-aware datetime.

    Accepts either ISO-formatted strings or datetime instances. Naive
    datetimes are assumed to be UTC. Avoids fragile string comparison
    of timestamps across the tracker.
    """
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def run_chain_tracker(db: Database, settings: Settings) -> None:
    """Main chain tracking loop — runs forever.

    BL-071a' v3 (2026-05-04): owns a single aiohttp session for the loop's
    lifetime. The session is threaded through check_chains -> _record_completion
    so memecoin chain completions can fetch FDV from DexScreener.
    """
    import aiohttp  # lazy import to avoid Windows OpenSSL DLL crash on test collection

    await seed_built_in_patterns(db)
    logger.info(
        "chain_tracker_started",
        interval_sec=settings.CHAIN_CHECK_INTERVAL_SEC,
    )
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await check_chains(db, settings, session=session)
            except Exception:
                logger.exception("chain_tracker_cycle_error")
            try:
                await asyncio.sleep(settings.CHAIN_CHECK_INTERVAL_SEC)
            except asyncio.CancelledError:
                logger.info("chain_tracker_cancelled")
                raise


# ---------------------------------------------------------------------------
# Core matching engine
# ---------------------------------------------------------------------------


async def check_chains(
    db: Database,
    settings: Settings,
    *,
    session: Any | None = None,
    mcap_fetcher: McapFetcher | None = None,
) -> None:
    """One pass of the pattern matching engine, wrapped in a single
    transaction so that writes across helpers commit atomically."""
    patterns = await load_active_patterns(db)
    if not patterns:
        logger.error("chain_no_active_patterns", chains_enabled=settings.CHAINS_ENABLED)
        return

    events = await load_recent_events(db, max_hours=settings.CHAIN_MAX_WINDOW_HOURS)

    conn = db._conn
    try:
        await conn.execute("BEGIN")
    except Exception:
        # Another BEGIN may already be in flight (test harness); fall back
        # to the existing transaction.
        pass

    try:
        if not events:
            await _prune_stale(db, settings)
            await conn.commit()
            return

        # Deterministic order: timestamp then id
        events.sort(key=lambda e: (e.created_at, e.id or 0))

        # Group by (token_id, pipeline)
        groups: dict[tuple[str, str], list[ChainEvent]] = {}
        for ev in events:
            groups.setdefault((ev.token_id, ev.pipeline), []).append(ev)

        active_by_key = await _load_active_chains(db)

        now = datetime.now(timezone.utc)
        completed_chains: list[tuple[ActiveChain, ChainPattern]] = []

        for (token_id, pipeline), token_events in groups.items():
            for pattern in patterns:
                key = (token_id, pipeline, pattern.id)
                chain = active_by_key.get(key)

                # Expiry check for pre-existing chain
                if chain is not None and not chain.is_complete:
                    age_h = (now - chain.anchor_time).total_seconds() / 3600.0
                    if age_h > settings.CHAIN_MAX_WINDOW_HOURS:
                        await _record_expired_chain(db, chain, pattern, now)
                        await _delete_active_chain(db, chain)
                        active_by_key.pop(key, None)
                        chain = None
                        logger.info(
                            "chain_expired",
                            token_id=token_id,
                            pattern=pattern.name,
                        )

                # Skip entirely if a recent completion exists (cooldown)
                if chain is None and await _in_cooldown(
                    db, token_id, pipeline, pattern, settings
                ):
                    continue

                # Advance or create
                chain, newly_complete = _advance_chain(
                    chain, pattern, token_id, pipeline, token_events, now
                )
                if chain is None:
                    continue

                active_by_key[key] = chain
                await _persist_active_chain(db, chain)

                if newly_complete:
                    completed_chains.append((chain, pattern))

        for chain, pattern in completed_chains:
            await _record_completion(
                db,
                chain,
                pattern,
                settings,
                session=session,
                mcap_fetcher=mcap_fetcher,
            )

        await _prune_stale(db, settings)
        await conn.commit()
    except Exception:
        try:
            await conn.rollback()
        except Exception:
            pass
        logger.exception("chain_check_failed")
        raise


def _advance_chain(
    chain: ActiveChain | None,
    pattern: ChainPattern,
    token_id: str,
    pipeline: str,
    events: list[ChainEvent],
    now: datetime,
) -> tuple[ActiveChain | None, bool]:
    """Try to advance (or start) a chain of the given pattern for this token."""
    steps_by_number = {s.step_number: s for s in pattern.steps}
    total_steps = len(pattern.steps)

    if chain is not None and chain.is_complete:
        return chain, False

    # If no chain yet, try to start one from the earliest matching anchor.
    if chain is None:
        anchor_step = steps_by_number[1]
        for ev in events:
            if ev.event_type != anchor_step.event_type:
                continue
            try:
                if not evaluate_condition(anchor_step.condition, ev.event_data):
                    continue
            except ValueError:
                logger.warning(
                    "chain_invalid_condition",
                    pattern=pattern.name,
                    step=1,
                    condition=anchor_step.condition,
                )
                continue
            chain = ActiveChain(
                token_id=token_id,
                pipeline=pipeline,
                pattern_id=pattern.id or 0,
                pattern_name=pattern.name,
                steps_matched=[1],
                step_events={1: ev.id or 0},
                anchor_time=ev.created_at,
                last_step_time=ev.created_at,
                created_at=now,
            )
            break
        if chain is None:
            return None, False

    # Walk events chronologically and try to advance successive steps.
    consumed_ids = set(chain.step_events.values())
    advanced = True
    while advanced:
        advanced = False
        for ev in events:
            if ev.id in consumed_ids:
                continue
            for step_num in sorted(steps_by_number.keys()):
                if step_num in chain.steps_matched:
                    continue
                step = steps_by_number[step_num]
                if step.event_type != ev.event_type:
                    continue

                hours_from_anchor = (
                    ev.created_at - chain.anchor_time
                ).total_seconds() / 3600.0
                if hours_from_anchor < 0:
                    continue
                if hours_from_anchor > step.max_hours_after_anchor:
                    continue

                if step.max_hours_after_previous is not None:
                    prior_event_id = chain.step_events.get(step_num - 1)
                    prior_ts: datetime | None = None
                    if prior_event_id is not None:
                        for prior_ev in events:
                            if prior_ev.id == prior_event_id:
                                prior_ts = prior_ev.created_at
                                break
                    if prior_ts is None:
                        continue
                    hours_from_prev = (
                        ev.created_at - prior_ts
                    ).total_seconds() / 3600.0
                    if (
                        hours_from_prev < 0
                        or hours_from_prev > step.max_hours_after_previous
                    ):
                        continue

                try:
                    if not evaluate_condition(step.condition, ev.event_data):
                        continue
                except ValueError:
                    logger.warning(
                        "chain_invalid_condition",
                        pattern=pattern.name,
                        step=step_num,
                    )
                    continue

                # Advance
                chain.steps_matched = sorted(chain.steps_matched + [step_num])
                chain.step_events[step_num] = ev.id or 0
                if ev.created_at > chain.last_step_time:
                    chain.last_step_time = ev.created_at
                consumed_ids.add(ev.id or 0)
                advanced = True
                logger.info(
                    "chain_step_matched",
                    token_id=token_id,
                    pattern=pattern.name,
                    step=step_num,
                )
                break
            if advanced:
                break

    newly_complete = False
    if (
        not chain.is_complete
        and len(chain.steps_matched) >= pattern.min_steps_to_trigger
    ):
        chain.is_complete = True
        chain.completed_at = now
        newly_complete = True
        logger.info(
            "chain_complete",
            token_id=chain.token_id,
            pattern=pattern.name,
            steps=len(chain.steps_matched),
            total=total_steps,
            duration_hours=round(
                (chain.last_step_time - chain.anchor_time).total_seconds() / 3600.0,
                2,
            ),
        )

    return chain, newly_complete


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


async def _load_active_chains(
    db: Database,
) -> dict[tuple[str, str, int], ActiveChain]:
    conn = db._conn
    async with conn.execute("""SELECT id, token_id, pipeline, pattern_id, pattern_name,
                  steps_matched, step_events, anchor_time, last_step_time,
                  is_complete, completed_at, created_at
           FROM active_chains
           WHERE is_complete = 0""") as cur:
        rows = await cur.fetchall()
    out: dict[tuple[str, str, int], ActiveChain] = {}
    for row in rows:
        chain = ActiveChain(
            id=row["id"],
            token_id=row["token_id"],
            pipeline=row["pipeline"],
            pattern_id=row["pattern_id"],
            pattern_name=row["pattern_name"],
            steps_matched=json.loads(row["steps_matched"]),
            step_events={int(k): v for k, v in json.loads(row["step_events"]).items()},
            anchor_time=_parse_time(row["anchor_time"]),
            last_step_time=_parse_time(row["last_step_time"]),
            is_complete=bool(row["is_complete"]),
            completed_at=(
                _parse_time(row["completed_at"]) if row["completed_at"] else None
            ),
            created_at=_parse_time(row["created_at"]),
        )
        out[(chain.token_id, chain.pipeline, chain.pattern_id)] = chain
    return out


async def _persist_active_chain(db: Database, chain: ActiveChain) -> None:
    conn = db._conn
    steps_json = json.dumps(chain.steps_matched)
    events_json = json.dumps({str(k): v for k, v in chain.step_events.items()})
    if chain.id is None:
        cursor = await conn.execute(
            """INSERT OR IGNORE INTO active_chains
               (token_id, pipeline, pattern_id, pattern_name,
                steps_matched, step_events, anchor_time, last_step_time,
                is_complete, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                chain.token_id,
                chain.pipeline,
                chain.pattern_id,
                chain.pattern_name,
                steps_json,
                events_json,
                chain.anchor_time.isoformat(),
                chain.last_step_time.isoformat(),
                1 if chain.is_complete else 0,
                chain.completed_at.isoformat() if chain.completed_at else None,
            ),
        )
        if cursor.lastrowid:
            chain.id = cursor.lastrowid
        else:
            # Conflict on UNIQUE(token_id, pipeline, pattern_id, anchor_time)
            # — fetch the existing row so subsequent updates apply correctly.
            async with conn.execute(
                """SELECT id FROM active_chains
                   WHERE token_id = ? AND pipeline = ? AND pattern_id = ?
                     AND anchor_time = ?""",
                (
                    chain.token_id,
                    chain.pipeline,
                    chain.pattern_id,
                    chain.anchor_time.isoformat(),
                ),
            ) as cur:
                row = await cur.fetchone()
            if row is not None:
                chain.id = row["id"]
                # Update the row we just discovered so new fields are persisted.
                await conn.execute(
                    """UPDATE active_chains
                       SET steps_matched = ?, step_events = ?,
                           last_step_time = ?, is_complete = ?,
                           completed_at = ?
                       WHERE id = ?""",
                    (
                        steps_json,
                        events_json,
                        chain.last_step_time.isoformat(),
                        1 if chain.is_complete else 0,
                        (
                            chain.completed_at.isoformat()
                            if chain.completed_at
                            else None
                        ),
                        chain.id,
                    ),
                )
            else:
                logger.warning(
                    "chain_insert_conflict_no_row",
                    token_id=chain.token_id,
                    pattern=chain.pattern_name,
                )
                return
    else:
        await conn.execute(
            """UPDATE active_chains
               SET steps_matched = ?, step_events = ?, last_step_time = ?,
                   is_complete = ?, completed_at = ?
               WHERE id = ?""",
            (
                steps_json,
                events_json,
                chain.last_step_time.isoformat(),
                1 if chain.is_complete else 0,
                chain.completed_at.isoformat() if chain.completed_at else None,
                chain.id,
            ),
        )


async def _delete_active_chain(db: Database, chain: ActiveChain) -> None:
    if chain.id is None:
        return
    await db._conn.execute("DELETE FROM active_chains WHERE id = ?", (chain.id,))


async def _in_cooldown(
    db: Database,
    token_id: str,
    pipeline: str,
    pattern: ChainPattern,
    settings: Settings,
) -> bool:
    """True if a completed chain for (token, pipeline, pattern) exists within cooldown."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=settings.CHAIN_COOLDOWN_HOURS)
    ).isoformat()
    async with db._conn.execute(
        """SELECT 1 FROM chain_matches
           WHERE token_id = ? AND pipeline = ? AND pattern_id = ?
             AND completed_at >= ?
           LIMIT 1""",
        (token_id, pipeline, pattern.id, cutoff),
    ) as cur:
        row = await cur.fetchone()
    return row is not None


async def _record_completion(
    db: Database,
    chain: ActiveChain,
    pattern: ChainPattern,
    settings: Settings,
    *,
    session: Any | None = None,
    mcap_fetcher: McapFetcher | None = None,
) -> None:
    """Write chain_matches row + emit chain_complete event + optional alert.

    BL-071a' v3 (2026-05-04): for memecoin pipeline, captures a DexScreener
    FDV snapshot in `mcap_at_completion` so the hydrator can later compute
    pct change vs current FDV. Narrative pipeline skips the fetch
    (token_id is a CoinGecko slug, not a contract; FDV lookup would fail).

    Enforces CHAIN_OUTCOME_MIN_MCAP_USD floor (per design-review R1-M2):
    dust mcap (e.g. 0.5 USD from pump.fun) would compute fake +500,000%
    hits at hydration time and poison the LEARN feedback loop. Below floor
    → write NULL, fall through to legacy outcomes path.

    Failures are graceful: row writes with mcap_at_completion=NULL.

    SCOPE NOTE (deferred to BL-071a''): the DS fetch happens INSIDE the
    check_chains transaction. SQLite write lock is held for up to 15s
    (DS timeout) per memecoin completion. Acceptable today (single-process
    pipeline, ~0-2 completions per cycle); pre-fetch outside transaction
    is a future optimization.
    """
    duration_h = (chain.last_step_time - chain.anchor_time).total_seconds() / 3600.0

    # BL-071a' v3: fetch FDV snapshot for memecoin chains, gated by floor.
    mcap_at_completion: float | None = None
    if chain.pipeline == "memecoin" and session is not None:
        fetcher = mcap_fetcher or fetch_token_fdv
        min_mcap = getattr(settings, "CHAIN_OUTCOME_MIN_MCAP_USD", 1000.0)
        try:
            result = await fetcher(session, chain.token_id)
        except Exception:
            # Fail-soft — never block chain write on the snapshot
            logger.exception(
                "mcap_at_completion_fetch_unexpected_error",
                token_id=chain.token_id,
            )
            result = None
        if result is not None and result.fdv is not None:
            if result.fdv >= min_mcap:
                mcap_at_completion = result.fdv
            else:
                logger.debug(
                    "chain_outcome_mcap_below_floor",
                    token_id=chain.token_id,
                    fdv=result.fdv,
                    floor=min_mcap,
                    note="writing NULL — dust mcap would produce fake hits",
                )

    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost, mcap_at_completion)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            chain.token_id,
            chain.pipeline,
            pattern.id,
            pattern.name,
            len(chain.steps_matched),
            len(pattern.steps),
            chain.anchor_time.isoformat(),
            (chain.completed_at or datetime.now(timezone.utc)).isoformat(),
            round(duration_h, 3),
            pattern.conviction_boost,
            mcap_at_completion,
        ),
    )
    await db._conn.execute(
        "UPDATE chain_patterns SET total_triggers = total_triggers + 1 WHERE id = ?",
        (pattern.id,),
    )

    await safe_emit(
        db,
        token_id=chain.token_id,
        pipeline=chain.pipeline,
        event_type="chain_complete",
        event_data={
            "pattern_name": pattern.name,
            "steps_matched": len(chain.steps_matched),
            "total_steps": len(pattern.steps),
            "conviction_boost": pattern.conviction_boost,
            "chain_duration_hours": round(duration_h, 3),
            "mcap_at_completion": mcap_at_completion,
        },
        source_module="chains.tracker",
    )

    if settings.CHAIN_ALERT_ON_COMPLETE and pattern.alert_priority in (
        "high",
        "medium",
    ):
        try:
            from scout.chains.alerts import send_chain_alert  # lazy import

            await send_chain_alert(db, chain, pattern, settings)
        except Exception:
            logger.exception("chain_alert_failed", pattern=pattern.name)


async def _record_expired_chain(
    db: Database,
    chain: ActiveChain,
    pattern: ChainPattern,
    now: datetime,
) -> None:
    """Record an expired (unresolved) chain as a pending miss in chain_matches.

    BL-071b fix (2026-05-03): writes outcome_class=NULL (not 'EXPIRED') so the
    hydrator `update_chain_outcomes` can later resolve the outcome from the
    predictions table. The previous behaviour pre-stamped 'EXPIRED' at write
    time, which the hydrator's `WHERE outcome_class IS NULL` filter then
    permanently skipped — a silent failure that caused 154 narrative
    chain_matches in prod to be stuck as EXPIRED with no evaluated_at.

    Verified safe: only patterns.py:263 reads chain_match outcomes for
    stats, and it tolerates NULL/EXPIRED equally (NULL rows simply don't
    contribute until the hydrator processes them).

    Only records if at least one step was matched — otherwise there is
    nothing meaningful for the LEARN phase to learn from.
    """
    steps_matched = len(chain.steps_matched)
    if steps_matched <= 0:
        return
    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost, outcome_class)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
        (
            chain.token_id,
            chain.pipeline,
            pattern.id,
            pattern.name,
            steps_matched,
            len(pattern.steps),
            chain.anchor_time.isoformat(),
            now.isoformat(),
            round(
                (chain.last_step_time - chain.anchor_time).total_seconds() / 3600.0,
                3,
            ),
            0,
        ),
    )


async def update_chain_outcomes(
    db: Database,
    *,
    settings: Settings | None = None,
    session: Any | None = None,
    mcap_fetcher: McapFetcher | None = None,
) -> int:
    """Hydrate chain_matches.outcome_class from downstream outcome tables.

    For each completed chain_match older than 48h with outcome_class NULL:
    * narrative pipeline → predictions.outcome_class (HIT/MISS/etc.)
    * memecoin pipeline → BL-071a' v3:
       - if mcap_at_completion populated, fetch current FDV via DexScreener,
         compute pct change; hit if >= CHAIN_OUTCOME_HIT_THRESHOLD_PCT
       - else fall back to legacy outcomes table for back-compat

    Defense-in-depth (R2-1, plan v3): if `session is None`, this function
    creates and closes its own aiohttp session for the cycle. Callers
    that don't have a session in scope (e.g., scout/narrative/learner.py:326
    LEARN cycle) get the BL-071a' resolution path automatically.

    Returns the number of rows updated. Designed for once-per-LEARN-cycle.
    """
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized")

    fetcher = mcap_fetcher or fetch_token_fdv

    # Defense-in-depth: self-create session if not injected (R2-1)
    own_session = session is None
    if own_session:
        import aiohttp  # lazy — Windows OpenSSL workaround

        session = aiohttp.ClientSession()
    try:
        return await _update_chain_outcomes_inner(
            conn,
            session,
            fetcher,
            settings,
        )
    finally:
        if own_session and session is not None:
            await session.close()


async def _update_chain_outcomes_inner(
    conn,
    session: Any,
    fetcher: McapFetcher,
    settings: Settings | None,
) -> int:
    """Inner body of update_chain_outcomes (split out so the session
    self-create wrapper stays small and the inner logic is testable)."""
    global _persistent_failure_alert_state

    hit_threshold_pct = (
        settings.CHAIN_OUTCOME_HIT_THRESHOLD_PCT if settings is not None else 50.0
    )
    min_mcap = settings.CHAIN_OUTCOME_MIN_MCAP_USD if settings is not None else 1000.0
    persistent_failure_age_hours = (
        settings.CHAIN_OUTCOME_PERSISTENT_FAILURE_HOURS if settings is not None else 1.0
    )
    unhealthy_failure_rate = (
        settings.CHAIN_TRACKER_UNHEALTHY_FAILURE_RATE if settings is not None else 0.5
    )
    unhealthy_min_attempts = (
        settings.CHAIN_TRACKER_UNHEALTHY_MIN_ATTEMPTS if settings is not None else 3
    )

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    async with conn.execute(
        """SELECT id, token_id, pipeline, completed_at, mcap_at_completion
           FROM chain_matches
           WHERE outcome_class IS NULL AND completed_at < ?""",
        (cutoff,),
    ) as cur:
        pending = await cur.fetchall()

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    updated = 0
    memecoin_unhydrateable = 0
    memecoin_ds_failures = 0
    memecoin_ds_attempts = 0
    memecoin_ds_rate_limited = 0
    memecoin_dust_abandoned = 0  # PR-review R2#1 (3rd pass): rows with mcap<floor written as 'dust_skipped' to exit pending set
    persistent_stuck_count = 0
    oldest_persistent_age_hours = 0.0

    for row in pending:
        match_id = row["id"]
        token_id = row["token_id"]
        pipeline = row["pipeline"]
        mcap_at_completion = row["mcap_at_completion"]
        outcome: str | None = None
        outcome_change_pct: float | None = None

        if pipeline == "narrative":
            async with conn.execute(
                """SELECT outcome_class FROM predictions
                   WHERE coin_id = ?
                     AND outcome_class IS NOT NULL
                     AND outcome_class != 'UNRESOLVED'
                   ORDER BY predicted_at DESC LIMIT 1""",
                (token_id,),
            ) as cur2:
                prow = await cur2.fetchone()
            if prow is not None and prow[0]:
                outcome = str(prow[0]).lower()
                if outcome not in ("hit", "miss"):
                    outcome = "hit" if outcome == "hit" else "miss"
        elif pipeline == "memecoin":
            # Defense-in-depth: even though writer enforces floor, double-check
            if mcap_at_completion is not None and mcap_at_completion >= min_mcap:
                # BL-071a' v3: active DexScreener resolution
                memecoin_ds_attempts += 1
                try:
                    result = await fetcher(session, token_id)
                except Exception:
                    logger.exception(
                        "chain_outcome_dexscreener_unexpected_error",
                        match_id=match_id,
                        token_id=token_id,
                    )
                    result = FetchResult(None, FetchStatus.TRANSIENT)

                if result.status == FetchStatus.RATE_LIMITED:
                    # R1-M1: distinct path — rate-limited rows do NOT count
                    # toward session-health failure rate.
                    memecoin_ds_rate_limited += 1
                    continue

                if result.fdv is None or result.fdv <= 0:
                    memecoin_ds_failures += 1
                    logger.debug(
                        "chain_outcome_dexscreener_failed",
                        match_id=match_id,
                        token_id=token_id,
                        mcap_at_completion=mcap_at_completion,
                        status=result.status.value,
                    )
                    # R1-1: track persistent stuck rows for aging ERROR
                    completed_at_str = row["completed_at"]
                    try:
                        completed_at = datetime.fromisoformat(
                            completed_at_str.replace("Z", "+00:00")
                        )
                        if completed_at.tzinfo is None:
                            completed_at = completed_at.replace(tzinfo=timezone.utc)
                        age_hours = (now - completed_at).total_seconds() / 3600.0
                        if age_hours > persistent_failure_age_hours:
                            persistent_stuck_count += 1
                            if age_hours > oldest_persistent_age_hours:
                                oldest_persistent_age_hours = age_hours
                    except (ValueError, AttributeError):
                        pass
                    continue

                # OK + valid fdv → resolve outcome
                outcome_change_pct = ((result.fdv / mcap_at_completion) - 1.0) * 100.0
                outcome = "hit" if outcome_change_pct >= hit_threshold_pct else "miss"
                logger.info(
                    "chain_outcome_resolved_via_dexscreener",
                    match_id=match_id,
                    token_id=token_id,
                    mcap_at_completion=mcap_at_completion,
                    current_fdv=result.fdv,
                    outcome_change_pct=round(outcome_change_pct, 2),
                    outcome=outcome,
                )
            elif mcap_at_completion is not None and mcap_at_completion < min_mcap:
                # Defense-in-depth: dust mcap row (writer floor failed or
                # row predates BL-071a'). PR-review R2#1 (3rd pass) fix:
                # write outcome_class='dust_skipped' so the row EXITS the
                # pending set — without this, the row re-enters the SELECT
                # every LEARN cycle forever (silent unbounded re-scan,
                # exact pattern Bundle A R2 flagged as antipattern).
                memecoin_dust_abandoned += 1
                logger.debug(
                    "chain_outcome_mcap_below_floor_at_hydrate",
                    match_id=match_id,
                    token_id=token_id,
                    mcap_at_completion=mcap_at_completion,
                    floor=min_mcap,
                )
                outcome = (
                    "dust_skipped"  # sentinel — patterns.py:263 tolerates non-hit/miss
                )
                outcome_change_pct = None  # no meaningful pct change for dust
            else:
                # mcap_at_completion is NULL — fall back to legacy outcomes
                async with conn.execute(
                    """SELECT price_change_pct FROM outcomes
                       WHERE contract_address = ? AND price_change_pct IS NOT NULL
                       ORDER BY id DESC LIMIT 1""",
                    (token_id,),
                ) as cur2:
                    orow = await cur2.fetchone()
                if orow is not None and orow[0] is not None:
                    outcome_change_pct = float(orow[0])
                    outcome = "hit" if outcome_change_pct > 0 else "miss"
                else:
                    memecoin_unhydrateable += 1

        if outcome is None:
            continue

        await conn.execute(
            """UPDATE chain_matches
               SET outcome_class = ?, outcome_change_pct = ?, evaluated_at = ?
               WHERE id = ?""",
            (outcome, outcome_change_pct, now_iso, match_id),
        )
        updated += 1

    await conn.commit()
    if updated:
        logger.info("chain_outcomes_hydrated", count=updated)

    # BL-071a' v3: aggregate WARNINGS distinguished by cause
    if memecoin_unhydrateable:
        logger.warning(
            "chain_outcomes_unhydrateable_memecoin",
            total_unhydrateable=memecoin_unhydrateable,
            cause="legacy_no_mcap_no_outcomes_row",
            note=(
                "These rows pre-date BL-071a' writer wiring AND have no legacy "
                "outcomes-table data. Properly-versioned migration backfill "
                "deferred to BL-071a''."
            ),
        )
    if memecoin_ds_failures:
        logger.warning(
            "chain_outcomes_ds_transient_failures",
            count=memecoin_ds_failures,
            cause="dexscreener_returned_no_data_or_error",
            note="Will retry next LEARN cycle.",
        )
    # R1-M1: rate-limited is NOT a session-health failure — separate WARNING
    # gives operators the right diagnosis path (upstream throttle vs local).
    if memecoin_ds_rate_limited:
        logger.warning(
            "chain_outcomes_ds_rate_limited",
            count=memecoin_ds_rate_limited,
            cause="dexscreener_429_throttle",
            note=(
                "DS free-tier rate limit hit. Rows will retry next LEARN "
                "cycle; consider widening CHAIN_CHECK_INTERVAL_SEC if persistent."
            ),
        )
    # PR-review R2#1 (3rd pass): visibility for dust-mcap rows that the
    # hydrator wrote off as 'dust_skipped'. Operators see growth and can
    # investigate whether the writer's floor is being bypassed.
    if memecoin_dust_abandoned:
        logger.warning(
            "chain_outcomes_dust_abandoned",
            count=memecoin_dust_abandoned,
            cause="mcap_at_completion_below_floor",
            note=(
                "Memecoin chain_matches with mcap_at_completion < "
                "CHAIN_OUTCOME_MIN_MCAP_USD floor were marked outcome_class="
                "'dust_skipped' to exit the pending set. If count is non-zero, "
                "investigate whether the writer's floor was bypassed (manual "
                "INSERT? row from before BL-071a' shipped?)."
            ),
        )
    # R1-1 + R1-S3: aging-aware ERROR with escalation rate-limiting
    if persistent_stuck_count:
        prev_state = _persistent_failure_alert_state
        should_alert = (
            prev_state is None
            or persistent_stuck_count > prev_state["count"]
            or (oldest_persistent_age_hours - prev_state["oldest_age"]) >= 24.0
        )
        if should_alert:
            logger.error(
                "chain_outcome_ds_persistent_failure",
                stuck_count=persistent_stuck_count,
                oldest_pending_age_hours=round(oldest_persistent_age_hours, 1),
                threshold_hours=round(persistent_failure_age_hours, 2),
                note=(
                    "Memecoin chain_matches with populated mcap_at_completion "
                    "but DS returned no FDV for >threshold. Investigate: "
                    "DS API status? rate-limited? contract delisted? Next "
                    "ERROR fires only on escalation (oldest age +>=24h) or "
                    "new stuck rows."
                ),
            )
            _persistent_failure_alert_state = {
                "count": persistent_stuck_count,
                "oldest_age": oldest_persistent_age_hours,
            }
    elif _persistent_failure_alert_state is not None:
        # Backlog cleared — reset alert state so next stuck-cluster fires.
        # PR-review R2#2 (3rd pass) fix: include current pending_count so
        # SRE can distinguish "DS recovered (rows actually resolved)" from
        # "table emptied (manual purge / pending=0 because rows gone)".
        logger.info(
            "chain_outcome_ds_persistent_failure_cleared",
            previous_count=_persistent_failure_alert_state["count"],
            pending_count=len(pending),
            note=(
                "Backlog cleared. If pending_count > 0 then DS recovered "
                "naturally (rows being resolved). If pending_count == 0 "
                "then either all rows resolved this cycle OR table was "
                "manually purged."
            ),
        )
        _persistent_failure_alert_state = None

    # R1-2: cycle-level session health (excludes rate-limited per R1-M1)
    non_rate_limited_attempts = memecoin_ds_attempts - memecoin_ds_rate_limited
    if non_rate_limited_attempts >= unhealthy_min_attempts:
        failure_rate = memecoin_ds_failures / non_rate_limited_attempts
        if failure_rate > unhealthy_failure_rate:
            logger.error(
                "chain_tracker_session_unhealthy",
                attempts=non_rate_limited_attempts,
                failures=memecoin_ds_failures,
                failure_rate_pct=round(failure_rate * 100, 1),
                threshold_pct=round(unhealthy_failure_rate * 100, 1),
                note=(
                    "Non-rate-limited DS fetch failure rate exceeds threshold "
                    "in this cycle. Long-lived aiohttp session may be degraded; "
                    "consider service restart to reset connector pool. "
                    "(Rate-limited responses excluded from this calculation.)"
                ),
            )
    return updated


async def _prune_stale(db: Database, settings: Settings) -> None:
    """Prune old signal_events and stale/completed active_chains."""
    deleted_events = await prune_old_events(
        db, retention_days=settings.CHAIN_EVENT_RETENTION_DAYS
    )
    if deleted_events:
        logger.debug("chain_events_pruned", count=deleted_events)

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(days=settings.CHAIN_ACTIVE_RETENTION_DAYS)
    ).isoformat()
    cursor = await db._conn.execute(
        """DELETE FROM active_chains
           WHERE (is_complete = 1 AND completed_at < ?)
              OR (is_complete = 0 AND anchor_time < ?)""",
        (cutoff, cutoff),
    )
    if cursor.rowcount:
        logger.debug("chain_active_pruned", count=cursor.rowcount)


# ---------------------------------------------------------------------------
# Boost query — consumed by the scoring pipeline
# ---------------------------------------------------------------------------


async def get_active_boosts(
    db: Database,
    token_id: str,
    pipeline: str,
    settings: Settings,
) -> int:
    """Return total conviction boost for a token, capped at CHAIN_TOTAL_BOOST_CAP."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=settings.CHAIN_COOLDOWN_HOURS)
    ).isoformat()
    async with db._conn.execute(
        """SELECT COALESCE(SUM(conviction_boost), 0) AS total
           FROM chain_matches
           WHERE token_id = ? AND pipeline = ? AND completed_at >= ?""",
        (token_id, pipeline, cutoff),
    ) as cur:
        row = await cur.fetchone()
    total = int(row[0] or 0)
    return min(total, settings.CHAIN_TOTAL_BOOST_CAP)
