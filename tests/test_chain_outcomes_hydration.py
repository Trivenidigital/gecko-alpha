"""BL-071b + BL-071a (partial): chain_matches outcome hydration regression tests.

Uses local `db` fixture matching tests/test_chains_tracker.py pattern —
there is no global tmp_db fixture in conftest.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.chains.models import ActiveChain, ChainPattern, ChainStep
from scout.chains.patterns import seed_built_in_patterns
from scout.chains.tracker import _record_expired_chain, update_chain_outcomes
from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    """Per-test DB seeded with built-in chain patterns.

    Built-in patterns get IDs 1..N (autoincrement). Stub helpers below use
    pattern_id=1 which is guaranteed to exist post-seed (chain_matches.pattern_id
    has a FK to chain_patterns(id), so the row must exist).
    """
    d = Database(tmp_path / "test.db")
    await d.initialize()
    await seed_built_in_patterns(d)
    yield d
    await d.close()


def _stub_pattern() -> ChainPattern:
    """Construct a ChainPattern matching scout/chains/models.py constraints.

    Required fields: name, description, steps, min_steps_to_trigger,
    conviction_boost, alert_priority. ChainPattern has NO `pipeline` field
    — pipeline is on the events/chains, not the pattern definition.
    """
    return ChainPattern(
        id=1,
        name="test_pattern",
        description="test pattern for outcome-hydration tests",
        steps=[
            ChainStep(
                step_number=1,
                event_type="signal_a",
                max_hours_after_anchor=24.0,
            ),
            ChainStep(
                step_number=2,
                event_type="signal_b",
                max_hours_after_anchor=48.0,
                max_hours_after_previous=24.0,
            ),
        ],
        min_steps_to_trigger=2,
        conviction_boost=10,
        alert_priority="medium",
    )


def _stub_chain(token_id: str, anchor_offset_hours: float = 4.0) -> ActiveChain:
    """Construct an ActiveChain matching scout/chains/models.py constraints.

    Required fields include pattern_name, step_events (dict[int, int]),
    created_at. steps_matched is list[int] (step numbers), NOT list of tuples.
    """
    now = datetime.now(timezone.utc)
    anchor = now - timedelta(hours=anchor_offset_hours)
    last_step = now - timedelta(hours=anchor_offset_hours / 2)
    return ActiveChain(
        token_id=token_id,
        pipeline="narrative",
        pattern_id=1,
        pattern_name="test_pattern",
        steps_matched=[1],
        step_events={1: 1},
        anchor_time=anchor,
        last_step_time=last_step,
        is_complete=False,
        created_at=anchor,
    )


@pytest.mark.asyncio
async def test_expired_chain_writes_null_not_expired(db):
    """BL-071b regression: expired chains must write outcome_class=NULL,
    not 'EXPIRED'. Pre-stamped EXPIRED was a silent permanent skip
    because the hydrator filters WHERE outcome_class IS NULL."""
    pattern = _stub_pattern()
    chain = _stub_chain("TOKEN_A")
    await _record_expired_chain(db, chain, pattern, datetime.now(timezone.utc))
    await db._conn.commit()
    cur = await db._conn.execute(
        "SELECT outcome_class FROM chain_matches WHERE token_id='TOKEN_A'"
    )
    row = await cur.fetchone()
    assert row[0] is None, (
        "expected NULL, got %r — pre-stamped EXPIRED is the BL-071b bug" % row[0]
    )


@pytest.mark.asyncio
async def test_hydrator_picks_up_null_expired_chain(db):
    """End-to-end: write 'expired' chain (now NULL) -> insert matching
    predictions HIT -> run hydrator -> chain_match should be hit."""
    pattern = _stub_pattern()
    chain = _stub_chain("TOKEN_B", anchor_offset_hours=72.0)
    long_ago = datetime.now(timezone.utc) - timedelta(hours=72)
    await _record_expired_chain(db, chain, pattern, long_ago)
    await db._conn.execute(
        """INSERT INTO predictions
           (category_id, category_name, coin_id, symbol, name,
            market_cap_at_prediction, price_at_prediction,
            narrative_fit_score, staying_power, confidence, reasoning,
            strategy_snapshot, predicted_at, outcome_class)
           VALUES ('cat','Cat','TOKEN_B','T','TokenB',
                   1000.0, 1.0, 80, 'high','high','test',
                   '{}', ?, 'hit')""",
        (long_ago.isoformat(),),
    )
    await db._conn.commit()
    updated = await update_chain_outcomes(db)
    assert updated == 1
    cur = await db._conn.execute(
        "SELECT outcome_class FROM chain_matches WHERE token_id='TOKEN_B'"
    )
    row = await cur.fetchone()
    assert row[0] == "hit"


@pytest.mark.asyncio
async def test_migration_converts_expired_narrative_rows(tmp_path):
    """BL-071b migration:
    - converts EXPIRED narrative rows with NULL evaluated_at to NULL
    - leaves memecoin EXPIRED rows alone (different pipeline)
    - leaves rows with evaluated_at intact (already-hydrated rows)
    - records itself in paper_migrations (so second startup is a no-op)
    """
    db_path = tmp_path / "mig.db"
    d1 = Database(db_path)
    await d1.initialize()
    await seed_built_in_patterns(d1)  # FK target for chain_matches.pattern_id
    long_ago = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    await d1._conn.executemany(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost, outcome_class, evaluated_at)
           VALUES (?, ?, 1, 'p', 1, 2, ?, ?, 0.0, 0, ?, ?)""",
        [
            ("N1", "narrative", long_ago, long_ago, "EXPIRED", None),
            ("N2", "narrative", long_ago, long_ago, "EXPIRED", long_ago),
            ("M1", "memecoin", long_ago, long_ago, "EXPIRED", None),
        ],
    )
    # Simulate the actual prod scenario: rows exist BEFORE the new migration
    # has been applied. initialize() above already inserted the gate row, so
    # we delete it to mimic a fresh-deploy state where the migration hasn't
    # run yet against this body of pre-existing EXPIRED rows.
    await d1._conn.execute(
        "DELETE FROM paper_migrations WHERE name='bl071b_unstamp_expired_narrative'"
    )
    await d1._conn.commit()
    # Now re-run the migration — should convert N1 to NULL, leave N2 + M1 alone.
    await d1._migrate_feedback_loop_schema()

    cur = await d1._conn.execute(
        "SELECT token_id, outcome_class FROM chain_matches ORDER BY token_id"
    )
    rows = {r[0]: r[1] for r in await cur.fetchall()}
    assert rows == {"N1": None, "N2": "EXPIRED", "M1": "EXPIRED"}

    # Verify migration is recorded as applied (idempotency gate)
    cur2 = await d1._conn.execute(
        "SELECT name FROM paper_migrations WHERE name = ?",
        ("bl071b_unstamp_expired_narrative",),
    )
    assert (await cur2.fetchone()) is not None

    # Re-run: must be a no-op (second invocation hits the gate)
    await d1._migrate_feedback_loop_schema()
    await d1.close()
