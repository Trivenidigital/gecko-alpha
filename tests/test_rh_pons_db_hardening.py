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
