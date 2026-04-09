"""LEARN phase — daily reflection, weekly consolidation, hit rates, circuit breaker.

Analyzes prediction outcomes and adjusts strategy parameters using LLM-guided
reflection.  Implements the feedback loop that makes the narrative agent adaptive.
"""

import json
import re
from datetime import datetime, timezone

import anthropic
import structlog

from scout.db import Database
from scout.narrative.prompts import (
    DAILY_REFLECTION_TEMPLATE,
    WEEKLY_CONSOLIDATION_TEMPLATE,
)
from scout.narrative.strategy import Strategy

log = structlog.get_logger()


# ------------------------------------------------------------------
# Hit-rate computation
# ------------------------------------------------------------------


async def compute_hit_rates(db: Database) -> dict:
    """Compute hit rates for agent and control predictions.

    Returns {"agent_hit_rate": float, "control_hit_rate": float, "true_alpha": float}.
    Excludes UNRESOLVED outcomes from counts.  Returns 0.0 for all values
    when no evaluated data exists.
    """
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized.")

    result: dict[str, float] = {
        "agent_hit_rate": 0.0,
        "control_hit_rate": 0.0,
        "true_alpha": 0.0,
    }

    for is_control, rate_key in [(0, "agent_hit_rate"), (1, "control_hit_rate")]:
        cursor = await conn.execute(
            """SELECT COUNT(*) FROM predictions
               WHERE is_control = ? AND outcome_class IS NOT NULL
               AND outcome_class != 'UNRESOLVED'""",
            (is_control,),
        )
        total = (await cursor.fetchone())[0]
        if total == 0:
            continue

        cursor = await conn.execute(
            """SELECT COUNT(*) FROM predictions
               WHERE is_control = ? AND outcome_class = 'HIT'""",
            (is_control,),
        )
        hits = (await cursor.fetchone())[0]
        result[rate_key] = round(hits / total * 100, 2)

    result["true_alpha"] = round(
        result["agent_hit_rate"] - result["control_hit_rate"], 2
    )
    return result


# ------------------------------------------------------------------
# Recent predictions
# ------------------------------------------------------------------


async def get_recent_predictions(db: Database, limit: int = 100) -> list[dict]:
    """Return the most recent evaluated predictions as a list of dicts."""
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized.")

    cursor = await conn.execute(
        """SELECT * FROM predictions
           WHERE outcome_class IS NOT NULL
           ORDER BY evaluated_at DESC
           LIMIT ?""",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


# ------------------------------------------------------------------
# Adjustment application
# ------------------------------------------------------------------


async def apply_adjustments(
    adjustments: list[dict],
    strategy: Strategy,
    db: Database,
    cycle_number: int = 0,
    min_sample: int = 100,
) -> int:
    """Apply LLM-suggested strategy adjustments if enough data exists.

    Returns the number of adjustments successfully applied.
    """
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized.")

    # Check total evaluated non-control, non-UNRESOLVED predictions
    cursor = await conn.execute(
        """SELECT COUNT(*) FROM predictions
           WHERE is_control = 0
           AND outcome_class IS NOT NULL
           AND outcome_class != 'UNRESOLVED'"""
    )
    total = (await cursor.fetchone())[0]

    if total < min_sample:
        log.info(
            "learn.skip_adjustments",
            total_evaluated=total,
            min_sample=min_sample,
        )
        return 0

    applied = 0
    for adj in adjustments:
        try:
            key = adj["key"]
            new_val = adj["new_value"]
            reason = adj.get("reason", "")
            await strategy.set(
                key,
                new_val,
                f"learn_cycle_{cycle_number}",
                reason,
            )
            applied += 1
            log.info("learn.adjustment_applied", key=key, new_val=new_val, reason=reason)
        except (ValueError, KeyError) as exc:
            log.warning("learn.adjustment_failed", adj=adj, error=str(exc))
    return applied


# ------------------------------------------------------------------
# Circuit breaker
# ------------------------------------------------------------------


def should_pause(
    daily_rates: list[float],
    threshold: float = 10.0,
    consecutive_days: int = 7,
) -> bool:
    """Return True if all of the last *consecutive_days* rates are below *threshold*."""
    if len(daily_rates) < consecutive_days:
        return False
    return all(r < threshold for r in daily_rates[-consecutive_days:])


# ------------------------------------------------------------------
# Daily reflection
# ------------------------------------------------------------------


def _parse_json_response(text: str) -> dict:
    """Extract the first JSON object from *text*, handling markdown fences."""
    # Strip markdown code fences if present
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        text = match.group(1)
    # Find first { ... } block
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in response")
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("Unterminated JSON object in response")


async def daily_learn(
    db: Database,
    strategy: Strategy,
    api_key: str,
    model: str = "claude-sonnet-4-20250514",
) -> dict | None:
    """Run a daily reflection cycle.

    1. Compute hit rates
    2. Fetch recent predictions
    3. Build regime breakdown
    4. Call Claude with DAILY_REFLECTION_TEMPLATE
    5. Parse JSON response
    6. Apply adjustments
    7. Log to learn_logs
    8. Return parsed result or None on error
    """
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized.")

    try:
        rates = await compute_hit_rates(db)
        predictions = await get_recent_predictions(db, limit=100)

        if not predictions:
            log.info("learn.daily_skip", reason="no evaluated predictions")
            return None

        # Build regime breakdown
        regime_counts: dict[str, dict[str, int]] = {}
        for p in predictions:
            regime = p.get("market_regime") or "UNKNOWN"
            oc = p.get("outcome_class") or "UNKNOWN"
            if regime not in regime_counts:
                regime_counts[regime] = {}
            regime_counts[regime][oc] = regime_counts[regime].get(oc, 0) + 1
        regime_breakdown = json.dumps(regime_counts, indent=2)

        # Determine cycle number
        cursor = await conn.execute(
            "SELECT MAX(cycle_number) FROM learn_logs"
        )
        row = await cursor.fetchone()
        cycle_number = (row[0] or 0) + 1

        # Format prompt
        prompt = DAILY_REFLECTION_TEMPLATE.format(
            sample_size=len(predictions),
            predictions_json=json.dumps(predictions[:20], indent=2, default=str),
            control_hit_rate=rates["control_hit_rate"],
            agent_hit_rate=rates["agent_hit_rate"],
            true_alpha=rates["true_alpha"],
            strategy_json=json.dumps(strategy.get_all(), indent=2, default=str),
            regime_breakdown=regime_breakdown,
        )

        # Call Claude
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=1500,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )

        raw_text = response.content[0].text
        parsed = _parse_json_response(raw_text)

        # Apply adjustments
        adjustments = parsed.get("adjustments", [])
        applied = await apply_adjustments(
            adjustments, strategy, db, cycle_number=cycle_number
        )

        # Insert learn_log row
        await conn.execute(
            """INSERT INTO learn_logs
               (cycle_number, cycle_type, reflection_text, changes_made,
                hit_rate_before, hit_rate_after)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                cycle_number,
                "daily",
                parsed.get("reflection", ""),
                json.dumps(adjustments),
                rates["agent_hit_rate"],
                None,
            ),
        )
        await conn.commit()

        log.info(
            "learn.daily_complete",
            cycle_number=cycle_number,
            adjustments_applied=applied,
            true_alpha=rates["true_alpha"],
        )
        return parsed

    except Exception:
        log.exception("learn.daily_error")
        return None


# ------------------------------------------------------------------
# Weekly consolidation
# ------------------------------------------------------------------


async def weekly_consolidate(
    db: Database,
    strategy: Strategy,
    api_key: str,
    model: str = "claude-sonnet-4-20250514",
) -> dict | None:
    """Run a weekly lesson consolidation cycle.

    1. Get current lessons and version from strategy
    2. Get last 7 daily reflections from learn_logs
    3. Call Claude with WEEKLY_CONSOLIDATION_TEMPLATE
    4. Archive current lessons
    5. Update lessons_learned and lessons_version
    6. Insert learn_log with cycle_type='weekly'
    7. Return parsed result or None on error
    """
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized.")

    try:
        current_lessons = str(strategy.get("lessons_learned"))
        lessons_version = int(strategy.get("lessons_version"))  # type: ignore[arg-type]
        next_version = lessons_version + 1

        # Last 7 daily reflections
        cursor = await conn.execute(
            """SELECT reflection_text FROM learn_logs
               WHERE cycle_type = 'daily'
               ORDER BY created_at DESC LIMIT 7"""
        )
        rows = await cursor.fetchall()
        weekly_reflections = "\n---\n".join(
            row[0] for row in rows if row[0]
        )

        if not weekly_reflections:
            log.info("learn.weekly_skip", reason="no daily reflections")
            return None

        # Determine cycle number
        cursor = await conn.execute(
            "SELECT MAX(cycle_number) FROM learn_logs"
        )
        row = await cursor.fetchone()
        cycle_number = (row[0] or 0) + 1

        # Format prompt
        prompt = WEEKLY_CONSOLIDATION_TEMPLATE.format(
            current_lessons=current_lessons or "(none yet)",
            weekly_reflections=weekly_reflections,
            hit_rate_per_lesson="(not yet tracked per-lesson)",
            next_version=next_version,
        )

        # Call Claude
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=1500,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )

        raw_text = response.content[0].text
        parsed = _parse_json_response(raw_text)

        # Archive current lessons
        if current_lessons:
            await strategy.set(
                f"lessons_v{lessons_version}",
                current_lessons,
                "weekly_consolidation",
                f"archived before v{next_version}",
            )

        # Update lessons_learned and lessons_version
        new_lessons = parsed.get("consolidated_lessons", "")
        await strategy.set(
            "lessons_learned",
            new_lessons,
            "weekly_consolidation",
            f"consolidated to v{next_version}",
        )
        await strategy.set(
            "lessons_version",
            next_version,
            "weekly_consolidation",
            f"bumped from v{lessons_version}",
        )

        # Insert learn_log
        await conn.execute(
            """INSERT INTO learn_logs
               (cycle_number, cycle_type, reflection_text, changes_made,
                hit_rate_before, hit_rate_after)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                cycle_number,
                "weekly",
                json.dumps(parsed.get("removed", []), default=str),
                json.dumps({"consolidated_lessons": new_lessons}),
                None,
                None,
            ),
        )
        await conn.commit()

        log.info(
            "learn.weekly_complete",
            cycle_number=cycle_number,
            lessons_version=next_version,
        )
        return parsed

    except Exception:
        log.exception("learn.weekly_error")
        return None
