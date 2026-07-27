"""F2 corrective — chain-tracker transaction atomicity + isolation (defects 2-4).

These prove the reviewer's chain-specific guarantees for check_chains():
- a completed-chain write is ALL-OR-NOTHING (chain_matches row + chain_complete
  event row commit together or not at all);
- the in-transaction rows are NOT visible to a SEPARATE connection before COMMIT
  (proves the event insert does not commit the outer transaction early);
- a post-commit alert delivery failure cannot roll back committed chain state;
- the transaction lock is NEVER held across the DexScreener network fetch.

Interleavings are event-ordered (asyncio.Event); no timing sleeps.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from scout.chains import tracker
from scout.chains.events import emit_event
from scout.chains.patterns import seed_built_in_patterns
from scout.chains.tracker import check_chains
from scout.db import Database

_CHAIN_SETTINGS_KW = dict(
    CHAIN_CHECK_INTERVAL_SEC=300,
    CHAIN_MAX_WINDOW_HOURS=24.0,
    CHAIN_COOLDOWN_HOURS=12.0,
    CHAIN_EVENT_RETENTION_DAYS=14,
    CHAIN_ACTIVE_RETENTION_DAYS=7,
    CHAIN_ALERT_ON_COMPLETE=False,
    CHAIN_TOTAL_BOOST_CAP=30,
    CHAINS_ENABLED=True,
    CHAIN_OUTCOME_MIN_MCAP_USD=1000.0,
)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "atomic.db")
    await d.initialize()
    await seed_built_in_patterns(d)
    yield d
    await d.close()


async def _emit_completing_events(db, token_id, pipeline):
    """Emit the full_conviction step events (standalone, disciplined)."""
    await emit_event(
        db,
        token_id,
        pipeline,
        "category_heating",
        {"acceleration": 8.0, "volume_growth_pct": 40.0, "market_regime": "BULL"},
        "narrative.observer",
    )
    await emit_event(
        db,
        token_id,
        pipeline,
        "laggard_picked",
        {"narrative_fit_score": 80, "confidence": "High", "trigger_count": 2},
        "narrative.predictor",
    )
    await emit_event(
        db,
        token_id,
        pipeline,
        "counter_scored",
        {
            "risk_score": 20,
            "flag_count": 0,
            "high_severity_count": 0,
            "data_completeness": "full",
        },
        "counter.scorer",
    )


async def test_chain_completion_atomic_rollback_on_event_failure(
    db, settings_factory, monkeypatch
):
    """If the chain_complete event insert fails mid-write-phase, the WHOLE
    completion rolls back: NO chain_matches row and NO chain_complete event row
    (all-or-nothing)."""
    await _emit_completing_events(db, "0xatomic", "memecoin")

    real_emit = tracker.emit_event

    async def _failing_emit(*args, conn=None, **kwargs):
        # Only the in-transaction completion emit (conn set) fails; standalone
        # seeding emits (conn None) are untouched.
        if conn is not None:
            raise RuntimeError("injected event-insert failure")
        return await real_emit(*args, conn=conn, **kwargs)

    monkeypatch.setattr(tracker, "emit_event", _failing_emit)

    with pytest.raises(RuntimeError):
        await check_chains(db, settings_factory(**_CHAIN_SETTINGS_KW))

    # The completion's chain_matches row was rolled back with the failed event.
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM chain_matches WHERE token_id = '0xatomic'"
    )
    assert (await cur.fetchone())[0] == 0, "chain_matches must roll back with the event"
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM signal_events "
        "WHERE token_id = '0xatomic' AND event_type = 'chain_complete'"
    )
    assert (await cur.fetchone())[0] == 0, "no chain_complete event may be committed"


async def test_chain_completion_invisible_to_separate_conn_until_commit(
    db, tmp_path, settings_factory, monkeypatch
):
    """The in-transaction completion rows are NOT visible to a SEPARATE
    connection before COMMIT (proves the event insert does not early-commit the
    outer transaction), and ARE visible after COMMIT."""
    await _emit_completing_events(db, "0xiso", "memecoin")

    in_txn = asyncio.Event()
    may_finish = asyncio.Event()
    real_prune = tracker._prune_stale

    async def _paused_prune(db_, settings_, *, conn):
        # Called LAST in the write phase — chain_matches + chain_complete event
        # are already written on the open transaction, not yet committed.
        in_txn.set()
        await may_finish.wait()
        await real_prune(db_, settings_, conn=conn)

    monkeypatch.setattr(tracker, "_prune_stale", _paused_prune)

    task = asyncio.create_task(check_chains(db, settings_factory(**_CHAIN_SETTINGS_KW)))
    await in_txn.wait()

    # A SEPARATE connection (independent reader) must NOT see the uncommitted rows.
    reader = sqlite3.connect(str(tmp_path / "atomic.db"))
    try:
        n = reader.execute(
            "SELECT COUNT(*) FROM chain_matches WHERE token_id = '0xiso'"
        ).fetchone()[0]
        assert n == 0, "separate connection must NOT see uncommitted completion"

        may_finish.set()
        await task

        # After COMMIT the separate connection sees the committed row(s). The
        # token completes >=1 built-in pattern; the point is 0 before / >0 after.
        n2 = reader.execute(
            "SELECT COUNT(*) FROM chain_matches WHERE token_id = '0xiso'"
        ).fetchone()[0]
        assert n2 >= 1, "separate connection must see the row(s) after commit"
    finally:
        reader.close()


async def test_post_commit_alert_failure_does_not_roll_back_chain_state(
    db, settings_factory, monkeypatch
):
    """A chain-completion alert is delivered AFTER commit; a send failure must
    NOT roll back the committed chain_matches row (defect 3)."""
    # Promote full_conviction to a high-priority pattern so the alert phase fires.
    await db.execute_write(
        "UPDATE chain_patterns SET alert_priority = 'high' WHERE name = 'full_conviction'"
    )
    await _emit_completing_events(db, "0xalert", "memecoin")

    async def _boom_alert(*a, **k):
        raise RuntimeError("telegram down")

    monkeypatch.setattr("scout.chains.alerts.send_chain_alert", _boom_alert)

    settings = settings_factory(
        **{**_CHAIN_SETTINGS_KW, "CHAIN_ALERT_ON_COMPLETE": True}
    )
    # check_chains swallows alert failures (phase 4) — must NOT raise.
    await check_chains(db, settings)

    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM chain_matches WHERE token_id = '0xalert'"
    )
    assert (await cur.fetchone())[
        0
    ] >= 1, "committed chain state must survive alert failure"


async def test_txn_lock_not_held_across_dexscreener_fetch(db, settings_factory):
    """The DexScreener FDV fetch happens in the network phase, OUTSIDE the write
    transaction — the fetcher observes _txn_lock UNHELD (defect 4)."""
    from scout.chains.mcap_fetcher import FetchResult, FetchStatus

    lock_states: list[bool] = []

    async def _observing_fetcher(session, contract):
        # If the fetch were inside the write transaction, _txn_lock would be held.
        lock_states.append(db._txn_lock.locked())
        return FetchResult(2_000_000.0, FetchStatus.OK)

    await _emit_completing_events(db, "0xnolock", "memecoin")
    await check_chains(
        db,
        settings_factory(**_CHAIN_SETTINGS_KW),
        session=object(),
        mcap_fetcher=_observing_fetcher,
    )

    assert lock_states, "fetcher was not called — completion did not fire"
    assert all(
        held is False for held in lock_states
    ), f"_txn_lock must be UNHELD during the DexScreener fetch; observed {lock_states}"
    # And the fetched mcap was still written (network result applied in write phase).
    cur = await db._conn.execute(
        "SELECT mcap_at_completion FROM chain_matches WHERE token_id = '0xnolock'"
    )
    assert (await cur.fetchone())[0] == 2_000_000.0


async def test_txn_lock_not_held_during_chain_alert_send(
    db, settings_factory, monkeypatch
):
    """Runtime guard (defect 4): the post-commit chain-completion alert send
    observes `_txn_lock` UNHELD — alert delivery is phase 4, outside the write
    transaction."""
    # Promote full_conviction to high so the alert phase fires.
    await db.execute_write(
        "UPDATE chain_patterns SET alert_priority='high' WHERE name='full_conviction'"
    )
    await _emit_completing_events(db, "0xalertlock", "memecoin")

    lock_states: list[bool] = []

    async def _guarded_alert(db_, chain, pattern, settings):
        # If the alert ran inside the transaction, _txn_lock would be held.
        lock_states.append(db._txn_lock.locked())

    monkeypatch.setattr("scout.chains.alerts.send_chain_alert", _guarded_alert)

    settings = settings_factory(
        **{**_CHAIN_SETTINGS_KW, "CHAIN_ALERT_ON_COMPLETE": True}
    )
    await check_chains(db, settings)

    assert lock_states, "alert was not sent — completion did not fire"
    assert all(
        held is False for held in lock_states
    ), f"_txn_lock must be UNHELD during the chain alert send; observed {lock_states}"


async def test_txn_lock_not_held_during_outcome_hydration_fetch(db, settings_factory):
    """Runtime guard (defect 4): update_chain_outcomes fetches DexScreener FDV
    with `_txn_lock` UNHELD — the network gather runs before the write
    transaction."""
    from scout.chains.mcap_fetcher import FetchResult, FetchStatus
    from scout.chains.tracker import update_chain_outcomes

    long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    await db.execute_write(
        "INSERT INTO chain_matches "
        "(token_id, pipeline, pattern_id, pattern_name, steps_matched, total_steps, "
        " anchor_time, completed_at, chain_duration_hours, conviction_boost, "
        " outcome_class, mcap_at_completion) "
        "VALUES ('0xhydlock','memecoin',1,'p',1,2,?,?,0.0,0,NULL,1000000.0)",
        (long_ago, long_ago),
    )

    lock_states: list[bool] = []

    async def _guarded_fetcher(session, contract):
        lock_states.append(db._txn_lock.locked())
        return FetchResult(2_000_000.0, FetchStatus.OK)

    s = settings_factory(
        **{**_CHAIN_SETTINGS_KW, "CHAIN_OUTCOME_HIT_THRESHOLD_PCT": 50.0}
    )
    updated = await update_chain_outcomes(
        db, settings=s, session=object(), mcap_fetcher=_guarded_fetcher
    )
    assert updated == 1
    assert lock_states, "fetcher was not called"
    assert all(
        held is False for held in lock_states
    ), f"_txn_lock must be UNHELD during the hydration fetch; observed {lock_states}"


# ---------------------------------------------------------------------------
# P0-2a/b: fetch-then-write revalidation + cycle-level single-flight.
# Event-ordered (asyncio.Event); no timing sleeps.
# ---------------------------------------------------------------------------


async def test_p0_2a_completion_revalidated_against_concurrent_completion(
    db, settings_factory
):
    """Pause the phase-2 fetch, insert a competing completion for the SAME
    (token,pipeline,pattern) (trips cooldown), resume → phase-3 revalidation
    skips the stale duplicate: no second full_conviction completion is written."""
    from scout.chains import tracker as _t
    from scout.chains.mcap_fetcher import FetchResult, FetchStatus

    _t._check_chains_inflight = False
    await _emit_completing_events(db, "0xreval", "memecoin")

    fetch_started = asyncio.Event()
    may_resume = asyncio.Event()

    async def _paused_fetcher(session, contract):
        fetch_started.set()
        await may_resume.wait()
        return FetchResult(2_000_000.0, FetchStatus.OK)

    settings = settings_factory(**_CHAIN_SETTINGS_KW)
    task = asyncio.create_task(
        check_chains(db, settings, session=object(), mcap_fetcher=_paused_fetcher)
    )
    await fetch_started.wait()
    # Concurrently commit a full_conviction (pattern_id=1) completion for the
    # SAME token, within cooldown — mimics a completion landing during the fetch.
    now_iso = datetime.now(timezone.utc).isoformat()
    await db.execute_write(
        "INSERT INTO chain_matches "
        "(token_id, pipeline, pattern_id, pattern_name, steps_matched, total_steps, "
        " anchor_time, completed_at, chain_duration_hours, conviction_boost) "
        "VALUES ('0xreval','memecoin',1,'full_conviction',3,4,?,?,0.0,25)",
        (now_iso, now_iso),
    )
    may_resume.set()
    await task

    # The pass's own full_conviction completion was revalidation-skipped (cooldown
    # now trips) → exactly ONE full_conviction row, the concurrently-inserted one.
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM chain_matches WHERE token_id='0xreval' AND pattern_id=1"
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()


async def test_p0_2b_outcome_not_overwritten_when_concurrently_resolved(
    db, settings_factory
):
    """Pause update_chain_outcomes' fetch, concurrently resolve the pending row's
    outcome, resume → the ``AND outcome_class IS NULL`` guard prevents overwrite
    and ``updated`` reports 0 actual rows changed."""
    from scout.chains.mcap_fetcher import FetchResult, FetchStatus
    from scout.chains.tracker import update_chain_outcomes

    long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    await db.execute_write(
        "INSERT INTO chain_matches "
        "(token_id, pipeline, pattern_id, pattern_name, steps_matched, total_steps, "
        " anchor_time, completed_at, chain_duration_hours, conviction_boost, "
        " outcome_class, mcap_at_completion) "
        "VALUES ('0xrace','memecoin',1,'p',1,2,?,?,0.0,0,NULL,1000000.0)",
        (long_ago, long_ago),
    )

    fetch_started = asyncio.Event()
    may_resume = asyncio.Event()

    async def _paused(session, contract):
        fetch_started.set()
        await may_resume.wait()
        return FetchResult(2_000_000.0, FetchStatus.OK)  # +100% -> would be 'hit'

    s = settings_factory(
        **{**_CHAIN_SETTINGS_KW, "CHAIN_OUTCOME_HIT_THRESHOLD_PCT": 50.0}
    )
    task = asyncio.create_task(
        update_chain_outcomes(db, settings=s, session=object(), mcap_fetcher=_paused)
    )
    await fetch_started.wait()
    # Concurrently resolve to a DIFFERENT value while the fetch is paused.
    await db.execute_write(
        "UPDATE chain_matches SET outcome_class='miss', outcome_change_pct=-10 "
        "WHERE token_id='0xrace'"
    )
    may_resume.set()
    updated = await task

    cur = await db._conn.execute(
        "SELECT outcome_class FROM chain_matches WHERE token_id='0xrace'"
    )
    assert (await cur.fetchone())[0] == "miss"  # concurrent value NOT overwritten
    assert updated == 0  # actual affected rows (revalidation miss reported truthfully)
    await db.close()


async def test_p0_2a_single_flight_second_pass_does_not_interleave(
    db, settings_factory
):
    """While one check_chains pass is mid-fetch, a second concurrent invocation
    returns immediately (single-flight) instead of interleaving its phase-1 reads
    with the first pass's phase-3 writes."""
    from scout.chains import tracker as _t
    from scout.chains.mcap_fetcher import FetchResult, FetchStatus

    _t._check_chains_inflight = False
    await _emit_completing_events(db, "0xsf", "memecoin")

    fetch_started = asyncio.Event()
    may_resume = asyncio.Event()
    fetch_calls: list[str] = []

    async def _paused(session, contract):
        fetch_calls.append(contract)
        fetch_started.set()
        await may_resume.wait()
        return FetchResult(2_000_000.0, FetchStatus.OK)

    s = settings_factory(**_CHAIN_SETTINGS_KW)
    first = asyncio.create_task(
        check_chains(db, s, session=object(), mcap_fetcher=_paused)
    )
    await fetch_started.wait()  # first pass is provably mid-fetch (inflight)
    before = len(fetch_calls)

    # Second concurrent pass must SKIP immediately — no fetch, no interleave.
    await check_chains(db, s, session=object(), mcap_fetcher=_paused)
    assert len(fetch_calls) == before, "single-flight breached — second pass fetched"

    may_resume.set()
    await first  # first pass finishes its own completions (fetch count may rise)
    await db.close()
