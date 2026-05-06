"""Built-in chain pattern definitions, condition evaluator, and DB seeding."""

from __future__ import annotations

import json
import operator
import re
from datetime import datetime

import structlog

from scout.chains.models import ChainPattern, ChainStep
from scout.config import Settings
from scout.db import Database

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Condition evaluator
# ---------------------------------------------------------------------------

_OPERATORS = {
    ">=": operator.ge,
    "<=": operator.le,
    "==": operator.eq,
    ">": operator.gt,
    "<": operator.lt,
}

# Tight grammar: `field OP NUMBER`, anchored, whitespace tolerant.
_CONDITION_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(>=|<=|==|>|<)\s*(-?\d+(?:\.\d+)?)\s*$"
)


def evaluate_condition(condition: str | None, event_data: dict) -> bool:
    """Evaluate a simple condition against event_data."""
    if condition is None:
        return True
    m = _CONDITION_RE.match(condition)
    if not m:
        raise ValueError(f"Invalid condition: {condition!r}")
    field, op_str, value_str = m.groups()
    if field not in event_data or event_data[field] is None:
        return False
    try:
        return _OPERATORS[op_str](float(event_data[field]), float(value_str))
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Built-in patterns
# ---------------------------------------------------------------------------

BUILT_IN_PATTERNS: list[ChainPattern] = [
    ChainPattern(
        name="full_conviction",
        description=(
            "Narrative heats -> laggard picked -> counter clean -> quant signals "
            "converge. The strongest pattern."
        ),
        steps=[
            ChainStep(
                step_number=1,
                event_type="category_heating",
                max_hours_after_anchor=0.0,
            ),
            ChainStep(
                step_number=2,
                event_type="laggard_picked",
                max_hours_after_anchor=6.0,
            ),
            ChainStep(
                step_number=3,
                event_type="counter_scored",
                condition="risk_score < 30",
                max_hours_after_anchor=8.0,
            ),
            ChainStep(
                step_number=4,
                event_type="candidate_scored",
                condition="signal_count >= 3",
                max_hours_after_anchor=12.0,
            ),
        ],
        min_steps_to_trigger=3,
        conviction_boost=25,
        # BL-NEW-CHAIN-COHERENCE 2026-05-06: bumped low→medium so the first
        # post-fix completion produces a Telegram alert. Pre-fix this
        # pattern matched 0 times in production despite 2,770 anchor
        # candidates (token_id keying bug). Operator needs ambient
        # confirmation that the per-laggard fix unblocked matching;
        # falls back to "low" once observability is no longer load-bearing.
        alert_priority="medium",
    ),
    ChainPattern(
        name="narrative_momentum",
        description=(
            "Heating category + clean counter + high narrative fit. Early "
            "alert before volume confirms."
        ),
        steps=[
            ChainStep(
                step_number=1,
                event_type="category_heating",
                max_hours_after_anchor=0.0,
            ),
            ChainStep(
                step_number=2,
                event_type="laggard_picked",
                max_hours_after_anchor=4.0,
            ),
            ChainStep(
                step_number=3,
                event_type="narrative_scored",
                condition="narrative_fit_score > 70",
                max_hours_after_anchor=4.0,
            ),
            ChainStep(
                step_number=4,
                event_type="counter_scored",
                condition="risk_score < 40",
                max_hours_after_anchor=6.0,
            ),
        ],
        min_steps_to_trigger=3,
        conviction_boost=15,
        # BL-NEW-CHAIN-COHERENCE 2026-05-06: bumped low→medium for the
        # same observability reason as full_conviction above.
        alert_priority="medium",
    ),
    ChainPattern(
        name="volume_breakout",
        description=(
            "Pure quant: successive candidate scores improve, counter is "
            "clean, and gate fires. Score velocity signal."
        ),
        steps=[
            ChainStep(
                step_number=1,
                event_type="candidate_scored",
                condition="signal_count >= 2",
                max_hours_after_anchor=0.0,
            ),
            ChainStep(
                step_number=2,
                event_type="candidate_scored",
                condition="signal_count >= 3",
                max_hours_after_anchor=4.0,
            ),
            ChainStep(
                step_number=3,
                event_type="counter_scored",
                condition="risk_score < 50",
                max_hours_after_anchor=6.0,
            ),
            ChainStep(
                step_number=4,
                event_type="conviction_gated",
                max_hours_after_anchor=8.0,
            ),
        ],
        min_steps_to_trigger=3,
        conviction_boost=20,
        alert_priority="low",
    ),
]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _pattern_to_row(p: ChainPattern) -> tuple:
    steps_json = json.dumps([s.model_dump() for s in p.steps])
    return (
        p.name,
        p.description,
        steps_json,
        p.min_steps_to_trigger,
        p.conviction_boost,
        p.alert_priority,
        1 if p.is_active else 0,
    )


def _row_to_pattern(row) -> ChainPattern:
    steps_raw = json.loads(row["steps_json"])
    steps = [ChainStep(**s) for s in steps_raw]
    return ChainPattern(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        steps=steps,
        min_steps_to_trigger=row["min_steps_to_trigger"],
        conviction_boost=row["conviction_boost"],
        alert_priority=row["alert_priority"],
        is_active=bool(row["is_active"]),
        historical_hit_rate=row["historical_hit_rate"],
        total_triggers=row["total_triggers"] or 0,
        total_hits=row["total_hits"] or 0,
        created_at=(
            datetime.fromisoformat(row["created_at"]) if row["created_at"] else None
        ),
        updated_at=(
            datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None
        ),
    )


async def seed_built_in_patterns(db: Database) -> int:
    """Insert BUILT_IN_PATTERNS if they are not already present. Idempotent."""
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized")
    async with conn.execute("SELECT name FROM chain_patterns") as cur:
        existing = {row["name"] for row in await cur.fetchall()}

    inserted = 0
    for pattern in BUILT_IN_PATTERNS:
        if pattern.name in existing:
            continue
        await conn.execute(
            """INSERT INTO chain_patterns
               (name, description, steps_json, min_steps_to_trigger,
                conviction_boost, alert_priority, is_active)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            _pattern_to_row(pattern),
        )
        inserted += 1
    await conn.commit()
    if inserted:
        logger.info("chains_seeded_built_in_patterns", count=inserted)
    return inserted


async def load_active_patterns(db: Database) -> list[ChainPattern]:
    """Load all active chain patterns from the database."""
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized")
    async with conn.execute(
        """SELECT id, name, description, steps_json, min_steps_to_trigger,
                  conviction_boost, alert_priority, is_active,
                  historical_hit_rate, total_triggers, total_hits,
                  created_at, updated_at
           FROM chain_patterns
           WHERE is_active = 1"""
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_pattern(r) for r in rows]


# ---------------------------------------------------------------------------
# LEARN phase: hit-rate computation + pattern lifecycle
# ---------------------------------------------------------------------------

_RETIREMENT_HIT_RATE = 0.20


async def compute_pattern_stats(db: Database, settings: Settings) -> list[dict]:
    """Compute hit rate per (pattern, pipeline) over evaluated chain_matches."""
    conn = db._conn
    rows: list[dict] = []
    async with conn.execute("""SELECT pattern_id, pattern_name, pipeline,
                  COUNT(*) AS total_evaluated,
                  SUM(CASE WHEN outcome_class='hit' THEN 1 ELSE 0 END) AS hits
           FROM chain_matches
           WHERE outcome_class IS NOT NULL
           GROUP BY pattern_id, pattern_name, pipeline""") as cur:
        for row in await cur.fetchall():
            total = row["total_evaluated"] or 0
            hits = row["hits"] or 0
            rate = (hits / total) if total > 0 else 0.0
            rows.append(
                {
                    "pattern_id": row["pattern_id"],
                    "pattern_name": row["pattern_name"],
                    "pipeline": row["pipeline"],
                    "total_evaluated": total,
                    "hits": hits,
                    "hit_rate": rate,
                    "sufficient": total >= settings.CHAIN_MIN_TRIGGERS_FOR_STATS,
                }
            )
    return rows


async def run_pattern_lifecycle(db: Database, settings: Settings) -> None:
    """Promote / graduate / retire chain patterns based on rolling stats."""
    stats = await compute_pattern_stats(db, settings)
    if not stats:
        return

    # Aggregate per pattern (best pipeline wins for promotion purposes)
    by_pattern: dict[int, dict] = {}
    for s in stats:
        pid = s["pattern_id"]
        cur_best = by_pattern.get(pid)
        if cur_best is None or s["hit_rate"] > cur_best["hit_rate"]:
            by_pattern[pid] = s

    # Systemic-zero-hits guard (BL-071, 2026-05-01).
    # When EVERY pattern shows 0 hits across N triggers, the cause is
    # almost certainly upstream outcome telemetry — not bad patterns.
    # Auto-retiring on this signal disabled all 3 patterns on 2026-05-01,
    # which silently killed chain_matches for ~17 days before the operator
    # noticed. Only retire when the system has demonstrated it CAN produce
    # hits somewhere; otherwise leave is_active alone and surface the
    # condition for investigation. See backlog BL-071a/b for the deeper
    # outcome-plumbing fixes (memecoin outcomes table empty; narrative
    # chain_matches start at outcome_class='EXPIRED' which the hydrator
    # filter `WHERE outcome_class IS NULL` skips entirely).
    total_hits_across_all = sum(s["hits"] for s in by_pattern.values())
    if total_hits_across_all == 0:
        logger.warning(
            "chain_pattern_retirement_skipped_systemwide_zero_hits",
            n_patterns=len(by_pattern),
            total_evaluated=sum(s["total_evaluated"] for s in by_pattern.values()),
        )
        return

    conn = db._conn
    for pid, s in by_pattern.items():
        async with conn.execute(
            "SELECT alert_priority, is_active FROM chain_patterns WHERE id = ?",
            (pid,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            continue
        prio = row["alert_priority"]
        is_active = bool(row["is_active"])
        new_prio = prio
        new_active = is_active

        if s["total_evaluated"] >= settings.CHAIN_MIN_TRIGGERS_FOR_STATS:
            if s["hit_rate"] < _RETIREMENT_HIT_RATE:
                new_active = False
                logger.info(
                    "chain_pattern_retired",
                    pattern=s["pattern_name"],
                    hit_rate=s["hit_rate"],
                )
            elif prio == "low" and s["hit_rate"] >= settings.CHAIN_PROMOTION_THRESHOLD:
                new_prio = "medium"
                logger.info(
                    "chain_pattern_promoted",
                    pattern=s["pattern_name"],
                    from_priority="low",
                    to_priority="medium",
                    hit_rate=s["hit_rate"],
                )

        if (
            prio == "medium"
            and s["total_evaluated"] >= settings.CHAIN_GRADUATION_MIN_TRIGGERS
            and s["hit_rate"] >= settings.CHAIN_GRADUATION_HIT_RATE
        ):
            new_prio = "high"
            logger.info(
                "chain_pattern_graduated",
                pattern=s["pattern_name"],
                hit_rate=s["hit_rate"],
            )

        await conn.execute(
            """UPDATE chain_patterns
               SET alert_priority = ?,
                   is_active      = ?,
                   historical_hit_rate = ?,
                   total_triggers = ?,
                   total_hits     = ?,
                   updated_at     = datetime('now')
               WHERE id = ?""",
            (
                new_prio,
                1 if new_active else 0,
                s["hit_rate"],
                s["total_evaluated"],
                s["hits"],
                pid,
            ),
        )
    await conn.commit()
