"""BL-071b + BL-071a (partial) + BL-071a' v3: chain_matches outcome hydration tests.

Uses local `db` fixture matching tests/test_chains_tracker.py pattern —
there is no global tmp_db fixture in conftest.

Tests that verify the BL-071a' hydrator session-self-create path require
real aiohttp; gated by SKIP_AIOHTTP_TESTS=1 on Windows due to OpenSSL
DLL conflict (matches Bundle A pattern in tests/test_heartbeat_mcap_missing.py).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

_SKIP_AIOHTTP = pytest.mark.skipif(
    sys.platform == "win32" and os.environ.get("SKIP_AIOHTTP_TESTS") == "1",
    reason="Windows + SKIP_AIOHTTP_TESTS=1: skip aiohttp self-create path",
)

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
    updated = await update_chain_outcomes(db, session=object())
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
@pytest.mark.skip(
    reason=(
        "Superseded by BL-071a' (commit b51324c+next): silent-skip semantics "
        "are gone — populated mcap_at_completion is now actively resolved via "
        "DexScreener fetch. Test preserved as documentation of Bundle A "
        "intermediate behaviour and as a guard if someone later removes the "
        "BL-071a' resolution path. Per plan v3 R2-4."
    )
)
async def test_hydrator_silent_skip_when_mcap_at_completion_populated(db, monkeypatch):
    """[SUPERSEDED] BL-071a partial: when memecoin chain_match has non-NULL
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
    updated = await update_chain_outcomes(db, session=object())
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
    updated = await update_chain_outcomes(db, session=object())
    assert updated == 0
    aggregate = [c for c in captured if c[1] == "chain_outcomes_unhydrateable_memecoin"]
    assert len(aggregate) == 1, (
        f"expected exactly one aggregate warning for unhydrateable rows, got "
        f"{len(aggregate)}: {captured}"
    )
    _, _, kwargs = aggregate[0]
    # Post-BL-071a' v3 semantic: 'unhydrateable' = NULL mcap + no legacy
    # outcomes row. Dust mcap (0.0/-1.0) is a separate concern handled by
    # the chain_outcome_mcap_below_floor_at_hydrate DEBUG log, NOT counted
    # as 'unhydrateable'. So this test now expects 3 (the 3 NULL-mcap rows
    # from the for-loop), not 5.
    assert kwargs["total_unhydrateable"] == 3
    assert kwargs["cause"] == "legacy_no_mcap_no_outcomes_row"


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
    updated = await update_chain_outcomes(db, session=object())
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
    updated = await update_chain_outcomes(db, session=object())
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


# ---------------------------------------------------------------------------
# BL-071a' v3 tests: writer wiring + DexScreener-resolved hydrator
# ---------------------------------------------------------------------------


def _make_active_chain_for_completion(
    token_id: str, pipeline: str = "memecoin"
) -> ActiveChain:
    """Build a chain that's at completion state (is_complete=True)."""
    now = datetime.now(timezone.utc)
    anchor = now - timedelta(hours=2)
    return ActiveChain(
        token_id=token_id,
        pipeline=pipeline,
        pattern_id=1,
        pattern_name="test_pattern",
        steps_matched=[1, 2],
        step_events={1: 1, 2: 2},
        anchor_time=anchor,
        last_step_time=now - timedelta(hours=1),
        is_complete=True,
        completed_at=now,
        created_at=anchor,
    )


@pytest.mark.asyncio
async def test_record_completion_populates_mcap_at_completion(db, settings_factory):
    """BL-071a': _record_completion captures FDV at write time
    via the injected fetcher, stores it in mcap_at_completion."""
    from scout.chains.mcap_fetcher import FetchResult, FetchStatus
    from scout.chains.tracker import _record_completion

    pattern = _stub_pattern()
    chain = _make_active_chain_for_completion("0xtoken1")

    async def _stub_fetcher(session, contract):
        assert contract == "0xtoken1"
        return FetchResult(2_500_000.0, FetchStatus.OK)

    s = settings_factory(
        CHAIN_ALERT_ON_COMPLETE=False,
        CHAIN_OUTCOME_MIN_MCAP_USD=1000.0,
    )

    await _record_completion(
        db,
        chain,
        pattern,
        s,
        session=object(),  # any non-None — fetcher is stubbed
        mcap_fetcher=_stub_fetcher,
    )
    await db._conn.commit()
    cur = await db._conn.execute(
        "SELECT mcap_at_completion FROM chain_matches WHERE token_id='0xtoken1'"
    )
    row = await cur.fetchone()
    assert row[0] == 2_500_000.0


@pytest.mark.asyncio
async def test_record_completion_leaves_mcap_null_when_fetcher_returns_no_data(
    db, settings_factory
):
    """Graceful degradation: NO_DATA → row writes with mcap NULL."""
    from scout.chains.mcap_fetcher import FetchResult, FetchStatus
    from scout.chains.tracker import _record_completion

    pattern = _stub_pattern()
    chain = _make_active_chain_for_completion("0xtoken2")

    async def _none_fetcher(session, contract):
        return FetchResult(None, FetchStatus.NO_DATA)

    s = settings_factory(
        CHAIN_ALERT_ON_COMPLETE=False,
        CHAIN_OUTCOME_MIN_MCAP_USD=1000.0,
    )

    await _record_completion(
        db,
        chain,
        pattern,
        s,
        session=object(),
        mcap_fetcher=_none_fetcher,
    )
    await db._conn.commit()
    cur = await db._conn.execute(
        "SELECT mcap_at_completion FROM chain_matches WHERE token_id='0xtoken2'"
    )
    row = await cur.fetchone()
    assert row[0] is None


@pytest.mark.asyncio
async def test_record_completion_skips_fetcher_for_narrative_pipeline(
    db, settings_factory
):
    """Narrative pipeline doesn't use FDV-based outcome — token_id is a
    CoinGecko slug, not a contract address. Fetcher MUST NOT be called."""
    from scout.chains.tracker import _record_completion

    pattern = _stub_pattern()
    chain = _make_active_chain_for_completion("boba-network", pipeline="narrative")

    fetcher_calls = []

    async def _spy_fetcher(session, contract):
        fetcher_calls.append(contract)
        from scout.chains.mcap_fetcher import FetchResult, FetchStatus

        return FetchResult(999_999.0, FetchStatus.OK)

    s = settings_factory(
        CHAIN_ALERT_ON_COMPLETE=False,
        CHAIN_OUTCOME_MIN_MCAP_USD=1000.0,
    )

    await _record_completion(
        db,
        chain,
        pattern,
        s,
        session=object(),
        mcap_fetcher=_spy_fetcher,
    )
    await db._conn.commit()
    assert (
        fetcher_calls == []
    ), f"narrative pipeline must NOT call DS fetcher; got {fetcher_calls}"
    cur = await db._conn.execute(
        "SELECT mcap_at_completion FROM chain_matches WHERE token_id='boba-network'"
    )
    row = await cur.fetchone()
    assert row[0] is None


@pytest.mark.asyncio
async def test_record_completion_enforces_mcap_floor(db, settings_factory):
    """R1-M2: writer enforces CHAIN_OUTCOME_MIN_MCAP_USD floor.
    Dust mcap (e.g. 0.5 USD from pump.fun) → write NULL, NOT the dust value
    (which would compute fake +500,000% hits at hydration)."""
    from scout.chains.mcap_fetcher import FetchResult, FetchStatus
    from scout.chains.tracker import _record_completion

    pattern = _stub_pattern()
    chain = _make_active_chain_for_completion("0xpumpfundust")

    async def _dust_fetcher(session, contract):
        return FetchResult(0.5, FetchStatus.OK)  # below 1000 floor

    s = settings_factory(
        CHAIN_ALERT_ON_COMPLETE=False,
        CHAIN_OUTCOME_MIN_MCAP_USD=1000.0,
    )

    await _record_completion(
        db,
        chain,
        pattern,
        s,
        session=object(),
        mcap_fetcher=_dust_fetcher,
    )
    await db._conn.commit()
    cur = await db._conn.execute(
        "SELECT mcap_at_completion FROM chain_matches WHERE token_id='0xpumpfundust'"
    )
    row = await cur.fetchone()
    assert row[0] is None, (
        f"dust mcap below floor must write NULL, not {row[0]} — "
        "would produce fake +500,000% hits at hydration time"
    )


# ---------------------------------------------------------------------------
# BL-071a' v3 hydrator tests (DexScreener resolution + aggregate signals)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hydrator_resolves_memecoin_via_dexscreener_hit(db, settings_factory):
    """BL-071a': memecoin chain_match with populated mcap_at_completion
    + current FDV >+50% → outcome_class='hit'."""
    from scout.chains.mcap_fetcher import FetchResult, FetchStatus

    long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost, outcome_class, mcap_at_completion)
           VALUES ('0xwinner','memecoin', 1, 'p', 1, 2, ?, ?, 0.0, 0, NULL, 1000000.0)""",
        (long_ago, long_ago),
    )
    await db._conn.commit()

    async def _stub_fetcher(session, contract):
        assert contract == "0xwinner"
        return FetchResult(2_000_000.0, FetchStatus.OK)  # +100%

    s = settings_factory(CHAIN_OUTCOME_HIT_THRESHOLD_PCT=50.0)
    updated = await update_chain_outcomes(
        db, settings=s, session=object(), mcap_fetcher=_stub_fetcher
    )
    assert updated == 1
    cur = await db._conn.execute(
        "SELECT outcome_class, outcome_change_pct FROM chain_matches WHERE token_id='0xwinner'"
    )
    row = await cur.fetchone()
    assert row[0] == "hit"
    assert row[1] == pytest.approx(100.0, rel=0.01)


@pytest.mark.asyncio
async def test_hydrator_resolves_memecoin_via_dexscreener_miss(db, settings_factory):
    """+0% to +50% → 'miss'."""
    from scout.chains.mcap_fetcher import FetchResult, FetchStatus

    long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost, outcome_class, mcap_at_completion)
           VALUES ('0xflat','memecoin', 1, 'p', 1, 2, ?, ?, 0.0, 0, NULL, 1000000.0)""",
        (long_ago, long_ago),
    )
    await db._conn.commit()

    async def _stub_fetcher(session, contract):
        return FetchResult(1_200_000.0, FetchStatus.OK)  # +20%

    s = settings_factory(CHAIN_OUTCOME_HIT_THRESHOLD_PCT=50.0)
    updated = await update_chain_outcomes(
        db, settings=s, session=object(), mcap_fetcher=_stub_fetcher
    )
    assert updated == 1
    cur = await db._conn.execute(
        "SELECT outcome_class FROM chain_matches WHERE token_id='0xflat'"
    )
    assert (await cur.fetchone())[0] == "miss"


@pytest.mark.asyncio
async def test_hydrator_skips_on_dexscreener_no_data(db, monkeypatch, settings_factory):
    """When DS returns NO_DATA: row stays UNRESOLVED, retries next cycle.
    Per-row log is DEBUG (not WARNING) — antipattern Bundle A R2 flagged."""
    from scout.chains.mcap_fetcher import FetchResult, FetchStatus

    captured = _capture_chain_logs(monkeypatch)
    long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost, outcome_class, mcap_at_completion)
           VALUES ('0xunavail','memecoin', 1, 'p', 1, 2, ?, ?, 0.0, 0, NULL, 1000000.0)""",
        (long_ago, long_ago),
    )
    await db._conn.commit()

    async def _none_fetcher(session, contract):
        return FetchResult(None, FetchStatus.NO_DATA)

    s = settings_factory(CHAIN_OUTCOME_HIT_THRESHOLD_PCT=50.0)
    updated = await update_chain_outcomes(
        db, settings=s, session=object(), mcap_fetcher=_none_fetcher
    )
    assert updated == 0
    cur = await db._conn.execute(
        "SELECT outcome_class FROM chain_matches WHERE token_id='0xunavail'"
    )
    assert (await cur.fetchone())[0] is None


@pytest.mark.asyncio
async def test_hydrator_rate_limited_excluded_from_session_health(
    db, monkeypatch, settings_factory
):
    """R1-M1 critical: 429 (RATE_LIMITED) does NOT count toward session
    failure rate. Routine DS rate-limiting must NOT trigger
    chain_tracker_session_unhealthy ERROR with 'restart service' guidance."""
    from scout.chains.mcap_fetcher import FetchResult, FetchStatus

    captured = _capture_chain_logs(monkeypatch)
    long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    rows = [
        (
            f"0xlimited{i}",
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
            1_000_000.0,
        )
        for i in range(5)
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

    async def _ratelimit_fetcher(session, contract):
        return FetchResult(None, FetchStatus.RATE_LIMITED)

    s = settings_factory(CHAIN_OUTCOME_HIT_THRESHOLD_PCT=50.0)
    await update_chain_outcomes(
        db, settings=s, session=object(), mcap_fetcher=_ratelimit_fetcher
    )
    # Session-health ERROR must NOT fire — rate-limited rows are excluded
    unhealthy = [c for c in captured if c[1] == "chain_tracker_session_unhealthy"]
    assert (
        unhealthy == []
    ), f"rate-limited rows should not flag session unhealthy; got {unhealthy}"
    # Aggregate rate-limit WARNING SHOULD fire
    rate_limited = [c for c in captured if c[1] == "chain_outcomes_ds_rate_limited"]
    assert len(rate_limited) == 1
    _, _, kwargs = rate_limited[0]
    assert kwargs["count"] == 5


@pytest.mark.asyncio
async def test_hydrator_session_unhealthy_fires_on_high_failure_rate(
    db, monkeypatch, settings_factory
):
    """T3 (design gap): session-health ERROR fires when >50% of NON-rate-
    limited attempts fail (with floor of 3 attempts)."""
    from scout.chains.mcap_fetcher import FetchResult, FetchStatus

    captured = _capture_chain_logs(monkeypatch)
    long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    rows = [
        (
            f"0xfail{i}",
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
            1_000_000.0,
        )
        for i in range(5)
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

    async def _all_fail(session, contract):
        return FetchResult(None, FetchStatus.TRANSIENT)

    s = settings_factory(
        CHAIN_OUTCOME_HIT_THRESHOLD_PCT=50.0,
        CHAIN_TRACKER_UNHEALTHY_FAILURE_RATE=0.5,
        CHAIN_TRACKER_UNHEALTHY_MIN_ATTEMPTS=3,
    )
    await update_chain_outcomes(
        db, settings=s, session=object(), mcap_fetcher=_all_fail
    )
    unhealthy = [c for c in captured if c[1] == "chain_tracker_session_unhealthy"]
    assert (
        len(unhealthy) == 1
    ), f"expected 1 unhealthy ERROR; got {len(unhealthy)}: {captured}"
    _, _, kwargs = unhealthy[0]
    assert kwargs["failure_rate_pct"] == 100.0
    assert kwargs["attempts"] == 5


@pytest.mark.asyncio
async def test_hydrator_session_health_does_not_fire_below_threshold(
    db, monkeypatch, settings_factory
):
    """T4 (design gap, negative case): session-health ERROR must NOT fire
    when failure rate is below threshold."""
    from scout.chains.mcap_fetcher import FetchResult, FetchStatus

    captured = _capture_chain_logs(monkeypatch)
    long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    rows = [
        (
            f"0xmix{i}",
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
            1_000_000.0,
        )
        for i in range(3)
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

    call_count = {"n": 0}

    async def _mostly_ok(session, contract):
        call_count["n"] += 1
        # 1 of 3 fails (33% failure rate, below 50% threshold)
        if call_count["n"] == 1:
            return FetchResult(None, FetchStatus.TRANSIENT)
        return FetchResult(2_000_000.0, FetchStatus.OK)

    s = settings_factory(
        CHAIN_OUTCOME_HIT_THRESHOLD_PCT=50.0,
        CHAIN_TRACKER_UNHEALTHY_FAILURE_RATE=0.5,
        CHAIN_TRACKER_UNHEALTHY_MIN_ATTEMPTS=3,
    )
    await update_chain_outcomes(
        db, settings=s, session=object(), mcap_fetcher=_mostly_ok
    )
    unhealthy = [c for c in captured if c[1] == "chain_tracker_session_unhealthy"]
    assert (
        unhealthy == []
    ), f"33% failure rate is BELOW 50% threshold; ERROR must not fire. Got {unhealthy}"


@pytest.mark.asyncio
async def test_hydrator_coupling_guard(db, settings_factory):
    """BL-071a' acceptance: after hydration, NO chain_match should have
    populated mcap AND unresolved outcome AND completed_at < now-48h.
    This is the canary that detects writer-wiring shipped without
    fetcher-wiring (or vice versa)."""
    from scout.chains.mcap_fetcher import FetchResult, FetchStatus

    long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    await db._conn.executemany(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost, outcome_class, mcap_at_completion)
           VALUES (?, 'memecoin', 1, 'p', 1, 2, ?, ?, 0.0, 0, NULL, ?)""",
        [
            ("0xpop1", long_ago, long_ago, 1_000_000.0),
            ("0xpop2", long_ago, long_ago, 500_000.0),
        ],
    )
    await db._conn.commit()

    async def _stub_fetcher(session, contract):
        return FetchResult(2_000_000.0, FetchStatus.OK)

    s = settings_factory(CHAIN_OUTCOME_HIT_THRESHOLD_PCT=50.0)
    await update_chain_outcomes(
        db, settings=s, session=object(), mcap_fetcher=_stub_fetcher
    )

    cur = await db._conn.execute("""SELECT COUNT(*) FROM chain_matches
           WHERE pipeline='memecoin'
             AND mcap_at_completion IS NOT NULL
             AND outcome_class IS NULL
             AND completed_at < datetime('now','-48 hours')""")
    leftover = (await cur.fetchone())[0]
    assert leftover == 0, (
        f"BL-071a' coupling-guard FAILED: {leftover} memecoin chain_matches "
        f"have populated mcap_at_completion AND unresolved outcome_class"
    )


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_hydrator_self_creates_session_when_none_provided(db, settings_factory):
    """T1 (design gap, R2-1 defense-in-depth): hydrator self-creates an
    aiohttp session if none is injected. Without this, callers like
    scout/narrative/learner.py:326 (no session in scope) would silently
    fall through to legacy path and BL-071a' would be dead-on-arrival."""
    from scout.chains.mcap_fetcher import FetchResult, FetchStatus

    long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost, outcome_class, mcap_at_completion)
           VALUES ('0xnosess','memecoin', 1, 'p', 1, 2, ?, ?, 0.0, 0, NULL, 1_000_000.0)""",
        (long_ago, long_ago),
    )
    await db._conn.commit()

    fetcher_calls = []

    async def _spy_fetcher(session, contract):
        # session must be NOT None — verifies self-creation worked
        assert (
            session is not None
        ), "hydrator should self-create session if none injected"
        fetcher_calls.append(contract)
        return FetchResult(2_000_000.0, FetchStatus.OK)

    s = settings_factory(CHAIN_OUTCOME_HIT_THRESHOLD_PCT=50.0)
    # Note: NO session kwarg — hydrator must self-create one
    updated = await update_chain_outcomes(db, settings=s, mcap_fetcher=_spy_fetcher)
    assert updated == 1
    assert len(fetcher_calls) == 1
    cur = await db._conn.execute(
        "SELECT outcome_class FROM chain_matches WHERE token_id='0xnosess'"
    )
    assert (await cur.fetchone())[0] == "hit"


@pytest.mark.asyncio
async def test_hydrator_persistent_failure_error_escalation_rate_limited(
    db, monkeypatch, settings_factory
):
    """T2 (design gap, R1-S3): persistent-failure ERROR fires once on
    initial alert + only re-fires on escalation (oldest_age increased
    >=24h OR new stuck rows). Without this, ERROR fires every LEARN
    cycle = wallpaper antipattern."""
    from scout.chains import tracker as tracker_module
    from scout.chains.mcap_fetcher import FetchResult, FetchStatus

    # Reset module state for test isolation
    tracker_module._persistent_failure_alert_state = None

    captured = _capture_chain_logs(monkeypatch)
    # Old enough to trigger persistent threshold (default 1.0 hour)
    long_ago = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost, outcome_class, mcap_at_completion)
           VALUES ('0xstuck','memecoin', 1, 'p', 1, 2, ?, ?, 0.0, 0, NULL, 1_000_000.0)""",
        (long_ago, long_ago),
    )
    await db._conn.commit()

    async def _all_fail(session, contract):
        return FetchResult(None, FetchStatus.TRANSIENT)

    s = settings_factory(
        CHAIN_OUTCOME_HIT_THRESHOLD_PCT=50.0,
        CHAIN_OUTCOME_PERSISTENT_FAILURE_HOURS=1.0,
    )

    # Cycle 1: ERROR should fire (initial alert)
    await update_chain_outcomes(
        db, settings=s, session=object(), mcap_fetcher=_all_fail
    )
    persistent_1 = [
        c for c in captured if c[1] == "chain_outcome_ds_persistent_failure"
    ]
    assert len(persistent_1) == 1, f"cycle 1 expected 1 ERROR; got {len(persistent_1)}"

    # Cycle 2 (immediately): ERROR should NOT fire — same row, age delta < 24h
    captured.clear()
    await update_chain_outcomes(
        db, settings=s, session=object(), mcap_fetcher=_all_fail
    )
    persistent_2 = [
        c for c in captured if c[1] == "chain_outcome_ds_persistent_failure"
    ]
    assert (
        persistent_2 == []
    ), f"cycle 2 must not re-fire ERROR (no escalation); got {persistent_2}"

    # Cleanup module state
    tracker_module._persistent_failure_alert_state = None
