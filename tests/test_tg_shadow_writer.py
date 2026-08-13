"""W4 — generation lifecycle, decision writer, and catch-up replay.

Binding criterion 2 lives here (`test_replay_after_later_evidence_is_byte_identical`):
`decision_as_of` is the signal's `created_at` on BOTH the live and the
catch-up path. A catch-up run that used wall clock would judge an old call
against a caller history that did not exist when the call was made, and the
resulting cohort would be a mix of two different rules wearing one
gate_version.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
import structlog

from scout.db import Database
from scout.social.telegram.shadow import (
    ShadowDecision,
    activate_shadow_generation,
    current_gate_version,
    ensure_generation,
    maybe_evaluate_signal,
    register_caller_feature_provider,
    scan_and_evaluate,
    scan_heartbeat_component,
    write_shadow_decision,
)
from scout.social.telegram.snapshot import build_resolution_snapshot
from scout.social.telegram.models import ResolvedToken


def _snapshot_json(**overrides) -> str:
    defaults = dict(
        token_id="tok",
        symbol="TOK",
        chain="solana",
        contract_address="0xabc",
        mcap=250_000.0,
        price_usd=1.0,
        volume_24h_usd=1000.0,
        age_days=5.0,
        safety_pass=True,
        safety_check_completed=True,
        safety_skipped_no_ca=False,
    )
    defaults.update(overrides)
    return build_resolution_snapshot(ResolvedToken(**defaults))


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "shadow.db")
    await database.initialize()
    now_iso = datetime.now(timezone.utc).isoformat()
    await database._conn.execute(
        "INSERT INTO tg_social_messages (id, channel_handle, msg_id, posted_at, "
        "parsed_at) VALUES (1, '@gem', 1, ?, ?)",
        (now_iso, now_iso),
    )
    await database._conn.commit()
    yield database
    await database.close()


async def _insert_signal(
    db: Database,
    *,
    created_at: datetime | None = None,
    resolution_state: str = "RESOLVED",
    snapshot_json: str | None = "__default__",
    mcap: float | None = 250_000.0,
    channel_handle: str = "@gem",
) -> int:
    created_iso = (created_at or datetime.now(timezone.utc)).isoformat()
    if snapshot_json == "__default__":
        snapshot_json = _snapshot_json()
    cur = await db._conn.execute(
        """INSERT INTO tg_social_signals
           (message_pk, token_id, symbol, contract_address, chain,
            mcap_at_sighting, resolution_state, source_channel_handle,
            alert_sent_at, paper_trade_id, created_at, resolution_snapshot_json)
           VALUES (1, 'tok', 'TOK', '0xabc', 'solana', ?, ?, ?, ?, NULL, ?, ?)""",
        (
            mcap,
            resolution_state,
            channel_handle,
            created_iso,
            created_iso,
            snapshot_json,
        ),
    )
    await db._conn.commit()
    return cur.lastrowid


async def _shadow_rows(db: Database) -> list[dict]:
    cur = await db._conn.execute(
        "SELECT signal_id, gate_version, actionable, reason, features_json "
        "FROM tg_act_shadow ORDER BY id"
    )
    return [dict(r) for r in await cur.fetchall()]


async def _backdate_generation(db: Database, gate_version: str, at: datetime) -> None:
    """Move a generation's `activated_at` into the past.

    TEST-ONLY. Real activation stamps `now`; a signal that predates activation
    is correctly outside the generation, so a catch-up test needs an
    activation that happened BEFORE the signal it is meant to pick up. This
    manufactures that history rather than sleeping through it.
    """
    await db._conn.execute(
        "UPDATE tg_act_shadow_generations SET activated_at = ? WHERE gate_version = ?",
        (at.isoformat(), gate_version),
    )
    await db._conn.commit()


# ---------------------------------------------------------------------------
# Generation lifecycle
# ---------------------------------------------------------------------------


async def test_no_generation_is_created_while_disabled(
    db, settings_factory, fixture_caller_feature_provider
):
    """The invariant that makes `activated_at` mean 'activation', not
    'install': a dark deploy writes nothing."""
    register_caller_feature_provider(fixture_caller_feature_provider())
    assert await activate_shadow_generation(db, settings_factory()) is None
    cur = await db._conn.execute("SELECT COUNT(*) FROM tg_act_shadow_generations")
    (count,) = await cur.fetchone()
    assert count == 0


async def test_enabled_without_provider_refuses_loudly(db, settings_factory):
    """Stage A ships no real provider. Arming against a placeholder feature
    set would change the cohort's semantics days later when Stage B lands."""
    with structlog.testing.capture_logs() as logs:
        result = await activate_shadow_generation(
            db, settings_factory(TG_SHADOW_ENABLED=True)
        )
    assert result is None
    events = [entry["event"] for entry in logs]
    assert "tg_shadow_activation_refused_no_feature_provider" in events

    cur = await db._conn.execute("SELECT COUNT(*) FROM tg_act_shadow_generations")
    (count,) = await cur.fetchone()
    assert count == 0


async def test_activation_creates_one_row_and_re_enable_resumes_it(
    db, settings_factory, fixture_caller_feature_provider
):
    """Disable/enable RESUMES the generation — the registry row is not
    rewritten, so signals from the disabled window stay eligible. A deliberate
    fresh start requires a new gate_version, not a flag cycle."""
    settings = settings_factory(TG_SHADOW_ENABLED=True)
    register_caller_feature_provider(fixture_caller_feature_provider())

    first = await activate_shadow_generation(db, settings)
    assert first is not None
    cur = await db._conn.execute(
        "SELECT gate_version, activated_at FROM tg_act_shadow_generations"
    )
    rows = [tuple(r) for r in await cur.fetchall()]
    assert len(rows) == 1

    second = await activate_shadow_generation(db, settings)
    assert second == first
    cur = await db._conn.execute(
        "SELECT gate_version, activated_at FROM tg_act_shadow_generations"
    )
    assert [
        tuple(r) for r in await cur.fetchall()
    ] == rows, "re-enable rewrote activated_at — that would silently re-cut the cohort"


async def test_ensure_generation_reports_creation_from_statement_rowcount(db):
    assert await ensure_generation(db, gate_version="gv-1") is True
    assert await ensure_generation(db, gate_version="gv-1") is False
    assert await ensure_generation(db, gate_version="gv-2") is True


# ---------------------------------------------------------------------------
# Decision writer
# ---------------------------------------------------------------------------


async def test_replay_collision_writes_nothing_and_leaves_one_row(db):
    """Restart replay: process the same signal twice, assert existence AND
    count. Counting only existence would pass with two rows."""
    signal_id = await _insert_signal(db)
    decision = ShadowDecision(True, "shadow_pass")

    assert (
        await write_shadow_decision(
            db,
            signal_id=signal_id,
            gate_version="gv",
            decision=decision,
            features_json="{}",
        )
        is True
    )
    with structlog.testing.capture_logs() as logs:
        second = await write_shadow_decision(
            db,
            signal_id=signal_id,
            gate_version="gv",
            decision=ShadowDecision(False, "shadow_block_duplicate_call"),
            features_json='{"different":1}',
        )
    assert second is False
    assert "tg_shadow_duplicate_skip" in [entry["event"] for entry in logs]

    rows = await _shadow_rows(db)
    assert len(rows) == 1
    # The FIRST decision stands. DO NOTHING must not become DO UPDATE.
    assert rows[0]["reason"] == "shadow_pass"
    assert rows[0]["features_json"] == "{}"


async def test_distinct_gate_versions_coexist_for_one_signal(db):
    """Multiple rows per signal exist only across gate_versions — that is the
    mechanism for comparing rule iterations."""
    signal_id = await _insert_signal(db)
    for gate_version in ("gv-a", "gv-b"):
        assert await write_shadow_decision(
            db,
            signal_id=signal_id,
            gate_version=gate_version,
            decision=ShadowDecision(True, "shadow_pass"),
            features_json="{}",
        )
    assert len(await _shadow_rows(db)) == 2


async def test_non_conflict_integrity_failure_propagates(db):
    """Only the ONE known-benign conflict is suppressed. A NOT NULL violation
    is a malformed writer, and swallowing it would lose decisions silently."""
    signal_id = await _insert_signal(db)
    with pytest.raises(aiosqlite.IntegrityError):
        await write_shadow_decision(
            db,
            signal_id=signal_id,
            gate_version="gv",
            decision=ShadowDecision(True, "shadow_pass"),
            features_json=None,
        )
    assert await _shadow_rows(db) == []
    assert not db._txn_lock.locked(), "_txn_lock stranded after a failed write"


async def test_writer_does_not_commit_while_txn_lock_is_held(db):
    """Same shared-connection invariant as every other writer. A feature flag
    does not make a malformed writer harmless."""
    signal_id = await _insert_signal(db)
    await db._txn_lock.acquire()
    task = asyncio.create_task(
        write_shadow_decision(
            db,
            signal_id=signal_id,
            gate_version="gv",
            decision=ShadowDecision(True, "shadow_pass"),
            features_json="{}",
        )
    )
    for _ in range(20):
        await asyncio.sleep(0)
    assert not task.done()
    assert await _shadow_rows(db) == []

    db._txn_lock.release()
    assert await asyncio.wait_for(task, timeout=5) is True
    assert len(await _shadow_rows(db)) == 1


# ---------------------------------------------------------------------------
# Eligibility scan
# ---------------------------------------------------------------------------


async def test_scan_is_inert_when_disabled(
    db, settings_factory, fixture_caller_feature_provider
):
    register_caller_feature_provider(fixture_caller_feature_provider())
    await _insert_signal(db)
    result = await scan_and_evaluate(db, settings_factory())
    assert result["armed"] is False
    assert await _shadow_rows(db) == []


async def test_scan_excludes_rows_that_predate_activation(
    db, settings_factory, fixture_caller_feature_provider
):
    """Generation cutover: first activation starts from zero eligible rows.
    Without this, flipping the flag on a database with months of history
    produces a page storm and a cohort nobody pre-registered."""
    settings = settings_factory(TG_SHADOW_ENABLED=True)
    register_caller_feature_provider(fixture_caller_feature_provider())
    now = datetime.now(timezone.utc)
    old_id = await _insert_signal(db, created_at=now - timedelta(hours=6))
    await activate_shadow_generation(db, settings)
    new_id = await _insert_signal(db, created_at=now + timedelta(seconds=1))

    result = await scan_and_evaluate(db, settings)
    assert result["scanned"] == 1
    assert [row["signal_id"] for row in await _shadow_rows(db)] == [new_id]
    assert old_id not in [row["signal_id"] for row in await _shadow_rows(db)]


async def test_scan_skips_unresolved_rows(
    db, settings_factory, fixture_caller_feature_provider
):
    settings = settings_factory(TG_SHADOW_ENABLED=True)
    register_caller_feature_provider(fixture_caller_feature_provider())
    gate_version = await activate_shadow_generation(db, settings)
    await _backdate_generation(
        db, gate_version, datetime.now(timezone.utc) - timedelta(hours=1)
    )
    await _insert_signal(db, resolution_state="UNRESOLVED_TERMINAL")
    await _insert_signal(db, resolution_state="UNRESOLVED_TRANSIENT")

    result = await scan_and_evaluate(db, settings)
    assert result["scanned"] == 0
    assert await _shadow_rows(db) == []


async def test_scan_picks_up_the_disabled_window_and_is_idempotent(
    db, settings_factory, fixture_caller_feature_provider
):
    """Signals that became RESOLVED while the flag was off satisfy
    `created_at >= activated_at` and are caught by the startup scan."""
    settings = settings_factory(TG_SHADOW_ENABLED=True)
    register_caller_feature_provider(fixture_caller_feature_provider())
    gate_version = await activate_shadow_generation(db, settings)
    await _backdate_generation(
        db, gate_version, datetime.now(timezone.utc) - timedelta(hours=2)
    )
    await _insert_signal(db, created_at=datetime.now(timezone.utc) - timedelta(hours=1))

    first = await scan_and_evaluate(db, settings)
    assert (first["scanned"], first["written"]) == (1, 1)

    second = await scan_and_evaluate(db, settings)
    assert (second["scanned"], second["written"]) == (0, 0)
    assert len(await _shadow_rows(db)) == 1


async def test_scan_completion_logs_and_stamps_the_health_row(
    db, settings_factory, fixture_caller_feature_provider
):
    """The watchdog needs a completed-scan timestamp to tell 'writer scanning
    but rows unshadowed' from 'writer not scanning at all'."""
    settings = settings_factory(TG_SHADOW_ENABLED=True)
    register_caller_feature_provider(fixture_caller_feature_provider())
    gate_version = await activate_shadow_generation(db, settings)
    await _backdate_generation(
        db, gate_version, datetime.now(timezone.utc) - timedelta(hours=2)
    )
    await _insert_signal(db, created_at=datetime.now(timezone.utc) - timedelta(hours=1))

    with structlog.testing.capture_logs() as logs:
        await scan_and_evaluate(db, settings)

    completions = [e for e in logs if e["event"] == "tg_shadow_scan_complete"]
    assert len(completions) == 1
    assert completions[0]["scanned"] == 1
    assert completions[0]["written"] == 1

    cur = await db._conn.execute(
        "SELECT updated_at, detail FROM tg_social_health WHERE component = ?",
        (scan_heartbeat_component(gate_version),),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row["updated_at"] is not None
    assert "scanned=1" in row["detail"]

    # No generic component: a shared row would let a retired generation's
    # heartbeat vouch for the current one.
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM tg_social_health WHERE component = 'tg_shadow_writer'"
    )
    (generic,) = await cur.fetchone()
    assert generic == 0


async def test_missing_snapshot_is_decided_not_skipped(
    db, settings_factory, fixture_caller_feature_provider
):
    """A post-cutover row with no snapshot appears in the reason distribution
    as `shadow_block_snapshot_missing`. Skipping it would leave the watchdog
    paging forever on a row nothing can decide."""
    settings = settings_factory(TG_SHADOW_ENABLED=True)
    register_caller_feature_provider(fixture_caller_feature_provider())
    gate_version = await activate_shadow_generation(db, settings)
    await _backdate_generation(
        db, gate_version, datetime.now(timezone.utc) - timedelta(hours=2)
    )
    await _insert_signal(
        db,
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        snapshot_json=None,
    )

    result = await scan_and_evaluate(db, settings)
    assert result["written"] == 1
    rows = await _shadow_rows(db)
    assert rows[0]["reason"] == "shadow_block_snapshot_missing"
    assert rows[0]["actionable"] == 0


# ---------------------------------------------------------------------------
# Live hook
# ---------------------------------------------------------------------------


async def test_live_hook_is_inert_without_flag_or_provider(db, settings_factory):
    signal_id = await _insert_signal(db)
    assert (
        await maybe_evaluate_signal(
            db=db, settings=settings_factory(), signal_id=signal_id
        )
        is False
    )
    assert (
        await maybe_evaluate_signal(
            db=db,
            settings=settings_factory(TG_SHADOW_ENABLED=True),
            signal_id=signal_id,
        )
        is False
    )
    assert await _shadow_rows(db) == []


async def test_live_hook_writes_one_decision(
    db, settings_factory, fixture_caller_feature_provider
):
    settings = settings_factory(TG_SHADOW_ENABLED=True)
    register_caller_feature_provider(fixture_caller_feature_provider())
    gate_version = await activate_shadow_generation(db, settings)
    signal_id = await _insert_signal(db)

    assert await maybe_evaluate_signal(db=db, settings=settings, signal_id=signal_id)
    rows = await _shadow_rows(db)
    assert len(rows) == 1
    assert rows[0]["gate_version"] == gate_version


async def test_live_hook_uses_created_at_not_wall_clock(
    db, settings_factory, fixture_caller_feature_provider
):
    """`decision_as_of` stamped into `features_json` is the row's own
    `created_at`, so the live write is reproducible by a later replay."""
    import json

    settings = settings_factory(TG_SHADOW_ENABLED=True)
    register_caller_feature_provider(fixture_caller_feature_provider())
    gate_version = await activate_shadow_generation(db, settings)
    await _backdate_generation(
        db, gate_version, datetime.now(timezone.utc) - timedelta(hours=2)
    )
    created_at = datetime.now(timezone.utc) - timedelta(hours=1)
    signal_id = await _insert_signal(db, created_at=created_at)

    assert await maybe_evaluate_signal(db=db, settings=settings, signal_id=signal_id)
    body = json.loads((await _shadow_rows(db))[0]["features_json"])
    assert body["decision_as_of"] == created_at.isoformat()


# ---------------------------------------------------------------------------
# BINDING CRITERION 2 — replay determinism
# ---------------------------------------------------------------------------


async def test_replay_after_later_evidence_is_byte_identical(
    db, settings_factory, fixture_caller_feature_provider
):
    """Evaluate, then add caller evidence dated AFTER the decision, then
    replay via catch-up: same features_json bytes, same decision, one row.

    The evidence is chosen so the two answers genuinely differ — at
    `created_at` the caller has 3 eligible clusters (below the min-N of 10, so
    `shadow_block_caller_insufficient_n`), and by wall clock it has 23 (which
    would pass). A replay that leaked wall clock would therefore flip the
    decision, not merely reorder a dict.
    """
    settings = settings_factory(TG_SHADOW_ENABLED=True)
    now = datetime.now(timezone.utc)
    created_at = now - timedelta(hours=2)

    early = [
        {
            "at": created_at - timedelta(hours=1),
            "cluster": f"c{i}",
            "priceable": True,
            "signal_id": None,
        }
        for i in range(3)
    ]
    later = [
        {
            "at": now - timedelta(minutes=30),
            "cluster": f"late{i}",
            "priceable": True,
            "signal_id": None,
        }
        for i in range(20)
    ]

    register_caller_feature_provider(fixture_caller_feature_provider(early))
    gate_version = await activate_shadow_generation(db, settings)
    await _backdate_generation(db, gate_version, created_at - timedelta(minutes=1))
    signal_id = await _insert_signal(db, created_at=created_at)

    assert await maybe_evaluate_signal(db=db, settings=settings, signal_id=signal_id)
    original = (await _shadow_rows(db))[0]
    assert original["reason"] == "shadow_block_caller_insufficient_n"

    # Wall clock has moved on and the caller has accumulated 20 more clusters.
    # Same module hash + schema, so the gate_version is unchanged: this is a
    # replay of the SAME generation, not a new one.
    grown = fixture_caller_feature_provider(early + later)
    register_caller_feature_provider(grown)
    assert current_gate_version(settings, grown)[0] == gate_version

    # The evidence really is discriminating: judged at wall clock, this caller
    # would now clear min-N.
    wall_clock_features = grown.features(
        channel_handle="@gem", decision_as_of=now, current_signal_id=signal_id
    )
    assert (
        wall_clock_features["history_eligible_distinct_clusters"]
        >= settings.TG_CALLER_MIN_ELIGIBLE_CLUSTERS
    )

    # Simulate the crash the catch-up scan exists for: the signal INSERT
    # landed, the tg_act_shadow write did not.
    await db._conn.execute(
        "DELETE FROM tg_act_shadow WHERE signal_id = ?", (signal_id,)
    )
    await db._conn.commit()

    result = await scan_and_evaluate(db, settings)
    assert (result["scanned"], result["written"]) == (1, 1)

    replayed = (await _shadow_rows(db))[0]
    assert replayed["features_json"] == original["features_json"]
    assert replayed["reason"] == original["reason"]
    assert replayed["actionable"] == original["actionable"]

    # And a further catch-up leaves exactly the one row.
    again = await scan_and_evaluate(db, settings)
    assert again["written"] == 0
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM tg_act_shadow WHERE signal_id = ? AND gate_version = ?",
        (signal_id, gate_version),
    )
    (count,) = await cur.fetchone()
    assert count == 1


async def test_scan_path_reads_decision_as_of_from_the_persisted_row(
    db, settings_factory, fixture_caller_feature_provider
):
    """Criterion 2, asserted directly on the CATCH-UP code path.

    `test_replay_after_later_evidence_is_byte_identical` above proves replay
    determinism end-to-end through `scan_and_evaluate`; this one states the
    underlying property in isolation so it cannot be satisfied by a helper that
    merely happens to be passed the right argument. Three rows with three
    distinct `created_at` values must produce three `decision_as_of` stamps
    equal to those values — a wall-clock implementation would stamp all three
    with (almost) the same instant.
    """
    import json

    settings = settings_factory(TG_SHADOW_ENABLED=True)
    register_caller_feature_provider(fixture_caller_feature_provider())
    gate_version = await activate_shadow_generation(db, settings)
    await _backdate_generation(
        db, gate_version, datetime.now(timezone.utc) - timedelta(hours=9)
    )

    created_ats = [datetime.now(timezone.utc) - timedelta(hours=h) for h in (8, 5, 2)]
    expected = {}
    for created_at in created_ats:
        signal_id = await _insert_signal(db, created_at=created_at)
        expected[signal_id] = created_at.isoformat()

    result = await scan_and_evaluate(db, settings)
    assert (result["scanned"], result["written"]) == (3, 3)

    stamped = {
        row["signal_id"]: json.loads(row["features_json"])["decision_as_of"]
        for row in await _shadow_rows(db)
    }
    assert stamped == expected
    assert len(set(stamped.values())) == 3, "all three rows stamped the same instant"


async def _active_marker(db: Database) -> tuple[str | None, str | None]:
    from scout.social.telegram.shadow import SHADOW_ACTIVE_GENERATION_COMPONENT

    cur = await db._conn.execute(
        "SELECT detail, updated_at FROM tg_social_health WHERE component = ?",
        (SHADOW_ACTIVE_GENERATION_COMPONENT,),
    )
    row = await cur.fetchone()
    return (None, None) if row is None else (row["detail"], row["updated_at"])


async def test_activation_publishes_the_active_generation_marker(
    db, settings_factory, fixture_caller_feature_provider
):
    """The watchdog cannot infer which generation is live from registry
    chronology (a resume does not rewrite `activated_at`), so the writer states
    it. Published at arm time, before any scan has run."""
    from scout.social.telegram.shadow import active_generation_detail

    settings = settings_factory(TG_SHADOW_ENABLED=True)
    register_caller_feature_provider(fixture_caller_feature_provider())

    assert await _active_marker(db) == (None, None), "marker exists before activation"

    gate_version = await activate_shadow_generation(db, settings)
    detail, updated_at = await _active_marker(db)
    assert detail == active_generation_detail(gate_version)
    assert updated_at is not None


async def test_disabled_activation_publishes_no_marker(
    db, settings_factory, fixture_caller_feature_provider
):
    """Same invariant as the generation row: a dark deploy announces nothing."""
    register_caller_feature_provider(fixture_caller_feature_provider())
    assert await activate_shadow_generation(db, settings_factory()) is None
    assert await _active_marker(db) == (None, None)


async def test_scan_refreshes_the_active_generation_marker(
    db, settings_factory, fixture_caller_feature_provider
):
    """Refreshed on every completed scan, in the SAME transaction as the scan
    heartbeat — a crash between the two would leave the watchdog reading a
    heartbeat for a generation the marker does not name."""
    from scout.social.telegram.shadow import active_generation_detail

    settings = settings_factory(TG_SHADOW_ENABLED=True)
    register_caller_feature_provider(fixture_caller_feature_provider())
    gate_version = await activate_shadow_generation(db, settings)
    await _backdate_generation(
        db, gate_version, datetime.now(timezone.utc) - timedelta(hours=2)
    )
    await _insert_signal(db, created_at=datetime.now(timezone.utc) - timedelta(hours=1))

    # Blank the marker so the refresh is observable rather than assumed.
    await db._conn.execute(
        "UPDATE tg_social_health SET detail = 'stale', updated_at = '2020-01-01' "
        "WHERE component = 'tg_shadow_active_generation'"
    )
    await db._conn.commit()

    await scan_and_evaluate(db, settings)
    detail, updated_at = await _active_marker(db)
    assert detail == active_generation_detail(gate_version)
    assert updated_at != "2020-01-01"

    # Scan heartbeat and marker agree on the generation and landed together.
    cur = await db._conn.execute(
        "SELECT updated_at FROM tg_social_health WHERE component = ?",
        (scan_heartbeat_component(gate_version),),
    )
    (scan_updated_at,) = await cur.fetchone()
    assert scan_updated_at == updated_at


async def test_generation_row_and_marker_are_armed_atomically(
    db, settings_factory, fixture_caller_feature_provider
):
    """Belt to the watchdog's suspenders.

    The registry row and the marker are written in ONE transaction, so the
    state the watchdog now fails closed on — a registered generation that no
    marker claims — is unreachable rather than merely alarmed.

    Proved by making the marker write fail: dropping `tg_social_health` breaks
    the second statement, and the assertion is that the FIRST one did not
    survive it. Asserting both rows exist on the happy path would not
    distinguish one transaction from two.
    """
    from scout.social.telegram.shadow import arm_generation

    await db._conn.execute("DROP TABLE tg_social_health")
    await db._conn.commit()

    with pytest.raises(Exception):
        await arm_generation(db, gate_version="tg-shadow-v1+atomic")

    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM tg_act_shadow_generations WHERE gate_version = ?",
        ("tg-shadow-v1+atomic",),
    )
    (rows,) = await cur.fetchone()
    assert rows == 0, "generation row survived a failed marker publish"
    assert not db._txn_lock.locked()
