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


@pytest.mark.asyncio
async def test_chain_matches_has_mcap_at_completion_column(db):
    """BL-071a partial: schema check — column must exist after migrations run."""
    cur = await db._conn.execute("PRAGMA table_info(chain_matches)")
    cols = {row[1]: row[2] for row in await cur.fetchall()}
    assert "mcap_at_completion" in cols
    assert cols["mcap_at_completion"] == "REAL"


def _capture_chain_logs(monkeypatch):
    """Monkey-patch tracker module's logger to a list-capture stub.

    Mirrors tests/test_heartbeat.py's _capture_logs pattern — bypasses caplog,
    which doesn't reliably capture structlog event_dicts when structlog
    isn't bridged to stdlib in the test config.
    """
    captured: list[tuple[str, dict]] = []

    class _CapLogger:
        def info(self, event, **kwargs):
            captured.append(("INFO", event, kwargs))

        def warning(self, event, **kwargs):
            captured.append(("WARNING", event, kwargs))

        def error(self, event, **kwargs):
            captured.append(("ERROR", event, kwargs))

        def exception(self, event, **kwargs):
            captured.append(("ERROR", event, kwargs))

        def debug(self, event, **kwargs):
            captured.append(("DEBUG", event, kwargs))

    from scout.chains import tracker as tracker_module

    monkeypatch.setattr(tracker_module, "logger", _CapLogger())
    return captured


@pytest.mark.asyncio
async def test_hydrator_silent_skip_when_mcap_at_completion_populated(db, monkeypatch):
    """BL-071a partial: when memecoin chain_match has non-NULL
    mcap_at_completion, hydrator must skip silently — BL-071a' will add the
    DexScreener fetch later. No per-row warning (that would be permanent
    log noise)."""
    captured = _capture_chain_logs(monkeypatch)
    long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost, outcome_class, mcap_at_completion)
           VALUES ('0xdeadbeef','memecoin', 1, 'p', 1, 2, ?, ?, 0.0, 0, NULL, 1500000.0)""",
        (long_ago, long_ago),
    )
    await db._conn.commit()
    updated = await update_chain_outcomes(db)
    assert updated == 0
    # Must NOT emit per-row warning for the populated case (silent intentional skip).
    pending_warnings = [c for c in captured if "pending" in c[1]]
    assert (
        pending_warnings == []
    ), f"populated mcap_at_completion must skip silently; got {pending_warnings}"
    # Also: must NOT emit aggregate warning (the row was skipped, not unhydrateable).
    aggregate_warnings = [
        c for c in captured if c[1] == "chain_outcomes_unhydrateable_memecoin"
    ]
    assert (
        aggregate_warnings == []
    ), f"populated row should not count as unhydrateable; got {aggregate_warnings}"


@pytest.mark.asyncio
async def test_hydrator_aggregate_warning_when_no_source(db, monkeypatch):
    """BL-071a partial: when N memecoin rows lack BOTH mcap_at_completion
    AND outcomes-table data, hydrator emits ONE aggregate warning per
    LEARN cycle (not N).

    Includes T3.A coverage: mcap_at_completion=0.0 falls through to legacy
    path same as NULL (zero mcap is meaningless for hydration). Includes
    T3.B coverage: negative mcap_at_completion also falls through.
    """
    captured = _capture_chain_logs(monkeypatch)
    long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    rows = [
        # 3 NULL mcap rows
        (
            f"0xnosrc{i}",
            "memecoin",
            1,
            "p",
            1,
            2,
            long_ago,
            long_ago,
            0.0,
            0,
            None,
            None,
        )
        for i in range(3)
    ] + [
        # T3.A: explicit zero mcap — must be treated as no usable data
        ("0xzeroM", "memecoin", 1, "p", 1, 2, long_ago, long_ago, 0.0, 0, None, 0.0),
        # T3.B: negative mcap (impossible in practice but defensively handled)
        ("0xnegM", "memecoin", 1, "p", 1, 2, long_ago, long_ago, 0.0, 0, None, -1.0),
    ]
    await db._conn.executemany(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost, outcome_class, mcap_at_completion)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    await db._conn.commit()
    updated = await update_chain_outcomes(db)
    assert updated == 0
    aggregate = [c for c in captured if c[1] == "chain_outcomes_unhydrateable_memecoin"]
    assert len(aggregate) == 1, (
        f"expected exactly one aggregate warning for 5 unhydrateable rows, got "
        f"{len(aggregate)}: {captured}"
    )
    _, _, kwargs = aggregate[0]
    assert kwargs["total_unhydrateable"] == 5
    assert kwargs["backlog_ref"] == "BL-071a'"
    assert kwargs["expires_when"].startswith("BL-071a'")


@pytest.mark.asyncio
async def test_hydrator_memecoin_legacy_outcomes_path_hits(db):
    """PR-review R1 SHOULD-FIX: cover the memecoin POSITIVE path —
    NULL mcap_at_completion + populated outcomes-table row → "hit"/"miss".

    Without this test, a future refactor breaking the legacy outcomes
    lookup (column rename, query typo) would be silent because all
    existing tests cover only the no-source path.
    """
    long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    contract = "0xfeedface"
    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost, outcome_class, mcap_at_completion)
           VALUES (?, 'memecoin', 1, 'p', 1, 2, ?, ?, 0.0, 0, NULL, NULL)""",
        (contract, long_ago, long_ago),
    )
    # Seed legacy outcomes row — positive pct → 'hit'
    await db._conn.execute(
        """INSERT INTO outcomes
           (contract_address, alert_price, check_price, check_time, price_change_pct)
           VALUES (?, 100.0, 175.0, ?, 75.0)""",
        (contract, long_ago),
    )
    await db._conn.commit()
    updated = await update_chain_outcomes(db)
    assert updated == 1
    cur = await db._conn.execute(
        "SELECT outcome_class FROM chain_matches WHERE token_id = ?",
        (contract,),
    )
    row = await cur.fetchone()
    assert row[0] == "hit"


@pytest.mark.asyncio
async def test_hydrator_aggregate_does_not_count_narrative_rows(db, monkeypatch):
    """PR-review R1 SHOULD-FIX: narrative-pipeline rows that don't resolve
    must NOT increment the memecoin unhydrateable counter.

    Defends against a future refactor that extracts the elif into a shared
    helper and accidentally lets narrative rows bleed into the memecoin
    aggregate warning.
    """
    captured = _capture_chain_logs(monkeypatch)
    long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    # 2 narrative rows with no matching predictions, 2 memecoin rows with no
    # mcap_at_completion + no outcomes data.
    await db._conn.executemany(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost, outcome_class, mcap_at_completion)
           VALUES (?, ?, 1, 'p', 1, 2, ?, ?, 0.0, 0, NULL, NULL)""",
        [
            ("narr-no-pred-1", "narrative", long_ago, long_ago),
            ("narr-no-pred-2", "narrative", long_ago, long_ago),
            ("0xmemenosrc1", "memecoin", long_ago, long_ago),
            ("0xmemenosrc2", "memecoin", long_ago, long_ago),
        ],
    )
    await db._conn.commit()
    updated = await update_chain_outcomes(db)
    assert updated == 0
    aggregate = [c for c in captured if c[1] == "chain_outcomes_unhydrateable_memecoin"]
    assert len(aggregate) == 1
    _, _, kwargs = aggregate[0]
    # Counter must reflect ONLY memecoin rows (2), not narrative+memecoin (4)
    assert (
        kwargs["total_unhydrateable"] == 2
    ), f"narrative rows must NOT bleed into memecoin counter; got {kwargs}"


def test_chain_match_model_has_mcap_at_completion_field():
    """PR-review R1 SHOULD-FIX: lock down the Pydantic field contract.

    ChainMatch is currently documentation-only (no production code constructs
    it from rows), so this test is the only place the field's default and
    type contract is enforceable. If someone removes the field or changes
    the default, this test catches it.
    """
    from scout.chains.models import ChainMatch

    base_kwargs = dict(
        token_id="T1",
        pipeline="memecoin",
        pattern_id=1,
        pattern_name="p",
        steps_matched=1,
        total_steps=2,
        anchor_time=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        chain_duration_hours=0.0,
        conviction_boost=0,
    )
    # Default: mcap_at_completion is None (not required, model-mirrors-table invariant).
    cm_default = ChainMatch(**base_kwargs)
    assert cm_default.mcap_at_completion is None
    # Accepts a positive float (the BL-071a' writer-wiring shape).
    cm_populated = ChainMatch(**base_kwargs, mcap_at_completion=1_500_000.0)
    assert cm_populated.mcap_at_completion == 1_500_000.0
    # Accepts None explicitly (alternate construction path).
    cm_null = ChainMatch(**base_kwargs, mcap_at_completion=None)
    assert cm_null.mcap_at_completion is None
