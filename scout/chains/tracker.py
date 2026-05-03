"""Chain tracker — pattern matching engine + main async loop + boost query."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import structlog

from scout.chains.events import (
    load_recent_events,
    prune_old_events,
    safe_emit,
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
    """Main chain tracking loop — runs forever."""
    await seed_built_in_patterns(db)
    logger.info(
        "chain_tracker_started",
        interval_sec=settings.CHAIN_CHECK_INTERVAL_SEC,
    )
    while True:
        try:
            await check_chains(db, settings)
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


async def check_chains(db: Database, settings: Settings) -> None:
    """One pass of the pattern matching engine, wrapped in a single
    transaction so that writes across helpers commit atomically."""
    patterns = await load_active_patterns(db)
    if not patterns:
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
            await _record_completion(db, chain, pattern, settings)

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
) -> None:
    """Write chain_matches row + emit chain_complete event + optional alert."""
    duration_h = (chain.last_step_time - chain.anchor_time).total_seconds() / 3600.0
    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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


async def update_chain_outcomes(db: Database) -> int:
    """Hydrate chain_matches.outcome_class from downstream outcome tables.

    For each completed chain_match that is older than 48h and still has
    outcome_class NULL, look up the token's realized outcome:

    * narrative pipeline  -> predictions.outcome_class (HIT / MISS / etc.)
    * memecoin pipeline   -> outcomes.price_change_pct (positive => hit)

    Returns the number of rows updated. Designed to be invoked once per
    daily LEARN cycle before compute_pattern_stats.
    """
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized")

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    async with conn.execute(
        """SELECT id, token_id, pipeline FROM chain_matches
           WHERE outcome_class IS NULL AND completed_at < ?""",
        (cutoff,),
    ) as cur:
        pending = await cur.fetchall()

    now_iso = datetime.now(timezone.utc).isoformat()
    updated = 0
    memecoin_unhydrateable = 0
    for row in pending:
        match_id = row["id"]
        token_id = row["token_id"]
        pipeline = row["pipeline"]
        outcome: str | None = None

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
                # Normalize to hit/miss so it matches compute_pattern_stats
                # which compares against 'hit'.
                if outcome not in ("hit", "miss"):
                    outcome = "hit" if outcome == "hit" else "miss"
        elif pipeline == "memecoin":
            # BL-071a partial (Bundle A 2026-05-03): prefer mcap_at_completion
            # (set by writer once BL-071a' wires them; today always NULL).
            # When populated, skip SILENTLY — BL-071a' will inline the
            # DexScreener fetch here. When NULL, fall back to legacy outcomes
            # table; if THAT is also empty, count for the aggregate warning
            # emitted once at end (not per-row, to avoid log spam).
            async with conn.execute(
                """SELECT mcap_at_completion FROM chain_matches WHERE id = ?""",
                (match_id,),
            ) as cur_m:
                mcap_row = await cur_m.fetchone()
            mcap_at_completion = mcap_row[0] if mcap_row else None

            if mcap_at_completion is not None and mcap_at_completion > 0:
                # Intentional silent skip — BL-071a' inlines the fetch here.
                continue

            async with conn.execute(
                """SELECT price_change_pct FROM outcomes
                   WHERE contract_address = ? AND price_change_pct IS NOT NULL
                   ORDER BY id DESC LIMIT 1""",
                (token_id,),
            ) as cur2:
                orow = await cur2.fetchone()
            if orow is not None and orow[0] is not None:
                outcome = "hit" if float(orow[0]) > 0 else "miss"
            else:
                memecoin_unhydrateable += 1

        if outcome is None:
            continue

        await conn.execute(
            """UPDATE chain_matches
               SET outcome_class = ?, evaluated_at = ?
               WHERE id = ?""",
            (outcome, now_iso, match_id),
        )
        updated += 1

    await conn.commit()
    if updated:
        logger.info("chain_outcomes_hydrated", count=updated)
    # BL-071a partial: aggregate warning per LEARN cycle (not per row) so
    # operators see the silent-failure surface without log spam. Will go
    # quiet once BL-071a' wires writers + adds DexScreener fetch.
    # Structured fields: explicit counts so operators can filter via
    # `jq '.total_unhydrateable'` etc. Carries expires_when + backlog_ref
    # so the deferral doesn't decay into known-noise.
    if memecoin_unhydrateable:
        logger.warning(
            "chain_outcomes_unhydrateable_memecoin",
            total_unhydrateable=memecoin_unhydrateable,
            mcap_at_completion_null_count=memecoin_unhydrateable,
            outcomes_table_empty_count=memecoin_unhydrateable,
            expires_when="BL-071a' ships (writers populate mcap_at_completion)",
            backlog_ref="BL-071a'",
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
