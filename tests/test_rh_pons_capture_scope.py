"""Bounded per-pass DB scope for sustained RH/Pons capture.

Scoped projection repair must publish exactly what the full rebuild would for
every token a pass touched, including NULL-token reorg markers and identities
whose token changed across a replacement, while reading only evidence tied to
those tokens. These tests use the DB layer only (no aiohttp import).
"""

import json

import pytest

from scout.db import Database

CHAIN = 1
PROTO = "pons_v2"


@pytest.fixture
async def db(tmp_path):
    instance = Database(tmp_path / "scope.db")
    await instance.initialize()
    yield instance
    await instance.close()


async def event(db, name="token_launched", block_hash="old", tx="tx", **kwargs):
    values = dict(
        chain_id=CHAIN,
        protocol=PROTO,
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
    return await db.record_curve_launch_event(**values)


async def marker(db, name, block_hash, tx="tx", log_index=0, **kwargs):
    # Collector-shaped markers: identity only, token and curve are NULL.
    return await event(
        db,
        name,
        block_hash,
        tx=tx,
        log_index=log_index,
        token_address=None,
        curve_address=None,
        payload_json=kwargs.pop("payload_json", None),
        **kwargs,
    )


async def discovery(db, token="token", status="on_curve", first_seen=None):
    await db.record_curve_launch_discovery(
        chain_id=CHAIN,
        network="robinhood",
        protocol=PROTO,
        token_address=token,
        curve_address="stale",
        deployer_address=None,
        pair_token_address=None,
        launch_config_id=None,
        graduation_threshold=None,
        lifecycle_status=status,
        event_time=None,
        first_seen_at=first_seen or "2026-09-14T00:00:00+00:00",
        source="fixture",
        provenance="fixture",
        execution_eligible=False,
        eligibility_reasons=[],
    )


async def snapshot(db):
    cur = await db._conn.execute(
        "SELECT * FROM curve_launch_discoveries ORDER BY token_address"
    )
    names = [c[0] for c in cur.description]
    return [dict(zip(names, row)) for row in await cur.fetchall()]


async def corrupt(db, tokens):
    # A projection that scoped repair skips would keep these sentinel values.
    for token in tokens:
        await db._conn.execute(
            "UPDATE curve_launch_discoveries SET lifecycle_status='rescued', "
            "curve_address='corrupt', event_time='corrupt' WHERE token_address=?",
            (token,),
        )
    await db._conn.commit()


async def assert_scoped_matches_full(db, identities, tokens):
    await corrupt(db, tokens)
    await db.reconcile_curve_launch_projection(CHAIN, PROTO, identities=identities)
    scoped = await snapshot(db)
    await db.reconcile_curve_launch_projection(CHAIN, PROTO)
    assert scoped == await snapshot(db)
    for row in scoped:
        if row["token_address"] in tokens:
            assert row["curve_address"] != "corrupt"


async def test_scoped_repair_matches_full_across_repeated_forks_with_null_markers(db):
    await discovery(db)
    for block_hash in ("a", "b", "a", "b", "a"):
        previous = await db.canonical_curve_event_block_hash(CHAIN, "tx", 0)
        if previous:
            await marker(db, "reorg_removed", previous)
            await marker(db, "reorg_removed", previous)  # idempotent retry
            await assert_scoped_matches_full(db, [("tx", 0)], {"token"})
            assert (await db.get_curve_launch(CHAIN, "token"))[
                "lifecycle_status"
            ] == "unknown"
        await event(db, block_hash=block_hash, curve_address=block_hash + "_curve")
        await assert_scoped_matches_full(db, [("tx", 0)], {"token"})
        row = await db.get_curve_launch(CHAIN, "token")
        assert row["curve_address"] == block_hash + "_curve"
        assert row["lifecycle_status"] == "on_curve"


async def test_scoped_repair_graduation_removed_by_null_marker(db):
    await discovery(db)
    await event(db)
    await event(db, "pool_graduated", tx="grad", block_number=11)
    await assert_scoped_matches_full(db, [("grad", 0)], {"token"})
    assert (await db.get_curve_launch(CHAIN, "token"))["lifecycle_status"] == "on_v4"
    # Only the marker's identity is passed: it carries no token of its own.
    await marker(db, "reorg_removed", "old", tx="grad", block_number=11)
    await assert_scoped_matches_full(db, [("grad", 0)], {"token"})
    assert (await db.get_curve_launch(CHAIN, "token"))["lifecycle_status"] == "on_curve"


async def test_scoped_repair_includes_old_and_new_token_on_replacement(db):
    await discovery(db, "token_a")
    await discovery(db, "token_b")
    await event(db, token_address="token_a", curve_address="curve_a")
    await db.reconcile_curve_launch_projection(CHAIN, PROTO)
    assert (await db.get_curve_launch(CHAIN, "token_a"))["curve_address"] == "curve_a"
    await marker(
        db,
        "reorg_replaced",
        "new",
        payload_json=json.dumps({"replaced_block_hash": "old"}),
    )
    await event(db, block_hash="new", token_address="token_b", curve_address="curve_b")
    await assert_scoped_matches_full(db, [("tx", 0)], {"token_a", "token_b"})
    assert (await db.get_curve_launch(CHAIN, "token_a"))[
        "lifecycle_status"
    ] == "unknown"
    assert (await db.get_curve_launch(CHAIN, "token_b"))["curve_address"] == "curve_b"


async def test_scoped_repair_restores_first_observation_after_interrupted_write(db):
    # Evidence committed, discovery write interrupted, replay inserted it later.
    await event(db, observed_at="2026-09-12T01:00:00+00:00")
    await discovery(db, first_seen="2026-09-13T05:00:00+00:00")
    await db.reconcile_curve_launch_projection(CHAIN, PROTO, identities=[("tx", 0)])
    row = await db.get_curve_launch(CHAIN, "token")
    assert row["first_seen_at"] == "2026-09-12T01:00:00+00:00"
    assert row["curve_address"] == "curve"


async def test_trade_identity_does_not_expand_scope(db, monkeypatch):
    await discovery(db)
    await event(db)
    await corrupt(db, {"token"})
    await event(db, "curve_buy", tx="buy", block_number=12)
    await db.reconcile_curve_launch_projection(CHAIN, PROTO, identities=[("buy", 0)])
    # Trades never change identity/lifecycle, so they select no projection work.
    assert (await db.get_curve_launch(CHAIN, "token"))["curve_address"] == "corrupt"


async def test_scoped_evidence_read_is_insensitive_to_unrelated_history(
    db, monkeypatch
):
    rows = []
    for i in range(5000):
        for name, log_index in (("token_launched", 0), ("curve_buy", 1)):
            rows.append(
                (
                    CHAIN,
                    PROTO,
                    name,
                    f"other{i}",
                    f"curve{i}",
                    f"tx{i}",
                    log_index,
                    i,
                    "h",
                    None,
                    "2026-09-13T00:00:00+00:00",
                    None,
                    "fixture",
                    "fixture",
                    "{}",
                )
            )
    await db._conn.executemany(
        """INSERT INTO curve_launch_events (chain_id, protocol, event_name,
        token_address, curve_address, transaction_hash, log_index, block_number,
        block_hash, event_time, observed_at, provider_available_at, source,
        provenance, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    await db._conn.commit()
    await discovery(db, "other7", status="rescued")
    await discovery(db)
    await event(db, block_number=9000)
    await marker(db, "reorg_removed", "old", block_number=9000)
    await event(db, block_hash="new", block_number=9000)
    # Same-token trades are high-volume history; they must not enter the fold.
    await event(db, "curve_buy", tx="buy", block_number=9001)

    seen = []
    original = Database._canonical_curve_hashes

    def spy(events):
        seen.append(len(events))
        return original(events)

    monkeypatch.setattr(Database, "_canonical_curve_hashes", staticmethod(spy))
    seen.clear()
    await db.reconcile_curve_launch_projection(CHAIN, PROTO, identities=[("tx", 0)])
    assert seen == [3]
    assert (await db.get_curve_launch(CHAIN, "token"))["lifecycle_status"] == "on_curve"
    # Unrelated projection rows are not rewritten by a scoped repair.
    assert (await db.get_curve_launch(CHAIN, "other7"))["lifecycle_status"] == "rescued"


async def test_empty_scope_touches_nothing(db):
    await discovery(db, status="rescued")
    await db.reconcile_curve_launch_projection(CHAIN, PROTO, identities=[])
    assert (await db.get_curve_launch(CHAIN, "token"))["lifecycle_status"] == "rescued"


async def test_curve_membership_is_emitter_scoped_and_lifecycle_filtered(db):
    for token, curve, status in (
        ("t1", "0xaa", "on_curve"),
        ("t2", "0xbb", "graduating"),
        ("t3", "0xcc", "on_v4"),
        ("t4", "0xdd", "unknown"),
    ):
        await db.record_curve_launch_discovery(
            chain_id=CHAIN,
            network="robinhood",
            protocol=PROTO,
            token_address=token,
            curve_address=curve,
            deployer_address=None,
            pair_token_address=None,
            launch_config_id=None,
            graduation_threshold=None,
            lifecycle_status=status,
            event_time=None,
            first_seen_at="2026-09-13T00:00:00+00:00",
            source="fixture",
            provenance="fixture",
            execution_eligible=False,
            eligibility_reasons=[],
        )
    members = await db.curve_launch_members(
        CHAIN, PROTO, ["0xAA", "0xbb", "0xcc", "0xdd", "0xee"]
    )
    assert members == {"0xaa", "0xbb"}
    assert await db.curve_launch_members(CHAIN, "other", ["0xaa"]) == set()
    assert await db.curve_launch_members(2, PROTO, ["0xaa"]) == set()
    assert await db.curve_launch_members(CHAIN, PROTO, []) == set()


async def test_range_and_membership_queries_use_indexes(db):
    cur = await db._conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM curve_launch_events WHERE chain_id=? "
        "AND protocol=? AND block_number BETWEEN ? AND ?",
        (CHAIN, PROTO, 1, 2),
    )
    plan = " ".join(str(r[-1]) for r in await cur.fetchall())
    assert "idx_curve_launch_ev_block" in plan
    cur = await db._conn.execute(
        "EXPLAIN QUERY PLAN SELECT curve_address FROM curve_launch_discoveries "
        "WHERE chain_id=? AND curve_address IN ('0xaa')",
        (CHAIN,),
    )
    plan = " ".join(str(r[-1]) for r in await cur.fetchall())
    assert "idx_curve_launch_disc_curve" in plan


async def test_block_index_survives_reopen(tmp_path):
    first = Database(tmp_path / "reopen.db")
    await first.initialize()
    await first.close()
    second = Database(tmp_path / "reopen.db")
    await second.initialize()
    cur = await second._conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='idx_curve_launch_ev_block'"
    )
    assert (await cur.fetchone())[0] == 1
    await second.close()
