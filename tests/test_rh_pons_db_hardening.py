"""Durable cursor and canonical projection recovery regressions."""

import json

import pytest

from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    instance = Database(tmp_path / "curve.db")
    await instance.initialize()
    yield instance
    await instance.close()


async def event(db, name="token_launched", block_hash="old", tx="tx", **kwargs):
    values = dict(
        chain_id=1,
        protocol="pons_v2",
        event_name=name,
        token_address="token",
        curve_address="curve",
        transaction_hash=tx,
        log_index=0,
        block_number=10,
        block_hash=block_hash,
        event_time=None,
        observed_at="2026-09-13T01:00:00+00:00",
        provider_available_at=None,
        source="fixture",
        provenance="fixture",
        payload_json=json.dumps({"fields": {"deployer_address": "deployer"}}),
    )
    values.update(kwargs)
    await db.record_curve_launch_event(**values)


async def discovery(db):
    await db.record_curve_launch_discovery(
        chain_id=1,
        network="eth",
        protocol="pons_v2",
        token_address="token",
        curve_address="curve",
        deployer_address="deployer",
        pair_token_address="pair",
        launch_config_id="1",
        graduation_threshold="2",
        lifecycle_status="on_v4",
        event_time=None,
        first_seen_at="2026-09-13T00:00:00+00:00",
        source="fixture",
        provenance="fixture",
        execution_eligible=False,
        eligibility_reasons=[],
    )


async def test_checkpoint_durable_and_scoped(db):
    assert await db.get_curve_scan_checkpoint(1, "pons_v2", "factory") is None
    await db.save_curve_scan_checkpoint(
        1, "pons_v2", "factory", 500, {498: "a", 499: "b"}
    )
    await db.close()
    await db.initialize()
    row = await db.get_curve_scan_checkpoint(1, "pons_v2", "factory")
    assert row["next_block"] == 500
    assert json.loads(row["block_hashes_json"]) == {"498": "a", "499": "b"}
    assert await db.get_curve_scan_checkpoint(2, "pons_v2", "factory") is None


async def test_removed_launch_clears_identity_without_erasing_first_observation(db):
    await discovery(db)
    await event(db)
    await event(db, "reorg_removed")
    assert await db.canonical_curve_event_block_hash(1, "tx", 0) is None
    await db.reconcile_curve_launch_projection(1, "pons_v2")
    row = await db.get_curve_launch(1, "token")
    assert row["lifecycle_status"] == "unknown"
    assert row["curve_address"] is None
    assert row["deployer_address"] is None
    assert row["first_seen_at"] == "2026-09-13T00:00:00+00:00"
    assert await db.list_curve_launch_curves(1, "pons_v2") == []
    assert len(await db.curve_events_in_range(1, "pons_v2", 10, 10)) == 1


async def test_removed_graduation_reverts_to_surviving_launch(db):
    await discovery(db)
    await event(db)
    await event(db, "pool_graduated", tx="graduation", block_number=11)
    await event(db, "reorg_removed", tx="graduation", block_number=11)
    await db.reconcile_curve_launch_projection(1, "pons_v2")
    assert (await db.get_curve_launch(1, "token"))["lifecycle_status"] == "on_curve"
    assert await db.list_curve_launch_curves(1, "pons_v2") == ["curve"]


async def test_replacement_excludes_old_hash_and_overwrites_identity(db):
    await discovery(db)
    await event(db)
    await event(
        db,
        "reorg_replaced",
        "new",
        payload_json=json.dumps({"replaced_block_hash": "old"}),
    )
    assert await db.canonical_curve_event_block_hash(1, "tx", 0) is None
    await event(db, block_hash="new", curve_address="new_curve")
    await db.reconcile_curve_launch_projection(1, "pons_v2")
    assert await db.canonical_curve_event_block_hash(1, "tx", 0) == "new"
    assert (await db.get_curve_launch(1, "token"))["curve_address"] == "new_curve"


async def test_crash_replay_keeps_original_evidence_observation(db):
    await event(db, observed_at="2026-09-12T01:00:00+00:00")
    await discovery(db)  # Replay repaired the missing row a day later.
    await db.reconcile_curve_launch_projection(1, "pons_v2")
    assert (await db.get_curve_launch(1, "token"))[
        "first_seen_at"
    ] == "2026-09-12T01:00:00+00:00"


async def test_reconcile_survives_other_writer_committing_shared_connection(
    db, monkeypatch
):
    await discovery(db)
    await event(db)
    execute = db._conn.execute

    async def interleave_commit(sql, *args, **kwargs):
        result = await execute(sql, *args, **kwargs)
        if "curve_launch_discoveries" in sql and "UPDATE" in sql:
            # A different pipeline writer commits between awaited SQL statements.
            await db._conn.commit()
        return result

    monkeypatch.setattr(db._conn, "execute", interleave_commit)
    await db.reconcile_curve_launch_projection(1, "pons_v2")
    assert (await db.get_curve_launch(1, "token"))["lifecycle_status"] == "on_curve"


async def test_repeated_fork_restoration_and_removal_are_ordered(db):
    await discovery(db)
    for block_hash in ("old", "new", "old", "new", "old"):
        previous = await db.canonical_curve_event_block_hash(1, "tx", 0)
        if previous:
            await event(db, "reorg_removed", previous)
            await event(db, "reorg_removed", previous)  # Idempotent poll retry.
        await event(db, block_hash=block_hash, curve_address=block_hash + "_curve")
        await event(db, block_hash=block_hash, curve_address=block_hash + "_curve")
        await db.reconcile_curve_launch_projection(1, "pons_v2")
        assert await db.canonical_curve_event_block_hash(1, "tx", 0) == block_hash
        assert (await db.get_curve_launch(1, "token"))[
            "curve_address"
        ] == block_hash + "_curve"
    cur = await db._conn.execute(
        "SELECT event_name, COUNT(*) FROM curve_launch_events GROUP BY event_name"
    )
    assert dict(await cur.fetchall()) == {
        "token_launched": 2,
        "reorg_removed": 4,
        "reorg_restored": 3,
    }


async def test_graduated_curve_not_polled_for_curve_trades(db):
    await discovery(db)
    assert await db.list_curve_launch_curves(1, "pons_v2") == []


async def test_reorg_migration_preserves_existing_evidence_ids(db):
    await event(db)
    cur = await db._conn.execute("SELECT * FROM curve_launch_events")
    original = await cur.fetchall()
    await db._conn.execute("DELETE FROM schema_version WHERE version=20260915")
    await db._conn.execute("DROP INDEX idx_curve_launch_ev_evidence_identity")
    await db._conn.execute(
        "ALTER TABLE curve_launch_events RENAME TO curve_launch_events_new_shape"
    )
    # Recreate the shipped v1 shape and seed its exact durable evidence.
    await db._conn.commit()
    await db._migrate_rh_pons_discovery_v1()
    await db._conn.execute(
        "INSERT INTO curve_launch_events SELECT * FROM curve_launch_events_new_shape"
    )
    await db._conn.execute("DROP TABLE curve_launch_events_new_shape")
    await db._conn.commit()
    await db._migrate_curve_reorg_markers_v1()
    cur = await db._conn.execute("SELECT * FROM curve_launch_events")
    assert await cur.fetchall() == original
    await db._migrate_curve_reorg_markers_v1()  # Idempotent after completed upgrade.
