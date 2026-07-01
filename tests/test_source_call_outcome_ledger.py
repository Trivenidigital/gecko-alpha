import json
import subprocess
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import pytest

from scout.db import Database
from scout.source_quality.ledger import (
    backfill_source_calls,
    check_source_calls_lag,
    compute_source_quality_summary,
    refresh_source_call_outcomes,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "source_calls.db")
    await d.initialize()
    yield d
    await d.close()


async def _fetchone(conn, sql, params=()):
    cur = await conn.execute(sql, params)
    return await cur.fetchone()


async def _insert_trade(conn, token_id, opened_at, *, pnl_usd=None):
    await conn.execute(
        "INSERT INTO paper_trades (token_id, symbol, name, chain, "
        "signal_type, signal_data, entry_price, amount_usd, quantity, "
        "tp_pct, sl_pct, tp_price, sl_price, status, opened_at, pnl_usd, "
        "signal_combo, remaining_qty) "
        "VALUES (?, 'TOK', 'Token', 'coingecko', 'narrative_prediction', "
        "'{}', 1, 100, 100, 20, 10, 1.2, 0.9, 'open', ?, ?, "
        "'narrative_prediction', 100)",
        (token_id, opened_at, pnl_usd),
    )
    cur = await conn.execute("SELECT last_insert_rowid()")
    return (await cur.fetchone())[0]


async def _insert_gainer_price(conn, coin_id, price, snapshot_at):
    await conn.execute(
        "INSERT INTO gainers_snapshots "
        "(coin_id, symbol, name, market_cap, volume_24h, price_change_24h, "
        "price_at_snapshot, snapshot_at) "
        "VALUES (?, 'TOK', 'Token', 10000000, 100000, 0, ?, ?)",
        (coin_id, price, snapshot_at),
    )


async def test_migration_creates_source_calls_and_sentinels(db):
    row = await _fetchone(
        db._conn,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='source_calls'",
    )
    assert row is not None

    await db._migrate_source_calls_v1()
    await db._migrate_source_calls_v1()

    assert (
        await _fetchone(
            db._conn,
            "SELECT COUNT(*) FROM paper_migrations WHERE name='bl_source_calls_v1'",
        )
    )[0] == 1
    assert (
        await _fetchone(
            db._conn,
            "SELECT COUNT(*) FROM schema_version WHERE version=20260522 "
            "AND description='bl_source_calls_v1'",
        )
    )[0] == 1

    with pytest.raises(sqlite3.IntegrityError):
        await db._conn.execute(
            "INSERT INTO source_calls "
            "(source_type, source_id, source_event_id, call_ts, call_kind, "
            "cluster_identity, cluster_identity_kind, duplicate_cluster_key, "
            "resolved_state, outcome_status, missing_fields) "
            "VALUES ('bad', 's', 'e', '2026-05-20T00:00:00+00:00', "
            "'unknown', 'e', 'source_event', 'k', 'unresolved', 'pending', '[]')"
        )
        await db._conn.commit()
    await db._conn.rollback()


async def test_migration_fails_on_schema_version_collision(tmp_path):
    d = Database(tmp_path / "collision.db")
    d._conn = await aiosqlite.connect(d._db_path)
    d._conn.row_factory = aiosqlite.Row
    try:
        await d._conn.execute(
            "CREATE TABLE paper_migrations (name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
        )
        await d._conn.execute(
            "CREATE TABLE schema_version "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, description TEXT NOT NULL)"
        )
        await d._conn.execute(
            "INSERT INTO schema_version VALUES "
            "(20260522, '2026-05-20T00:00:00+00:00', 'different_migration')"
        )
        await d._conn.commit()
        with pytest.raises(RuntimeError, match="schema_version collision"):
            await d._migrate_source_calls_v1()
    finally:
        await d._conn.close()


async def test_migration_backfills_sentinel_when_schema_version_already_applied(
    tmp_path,
):
    """Recover historical partial state: schema_version present, sentinel absent."""
    d = Database(tmp_path / "partial_source_calls.db")
    await d.initialize()
    await d._conn.execute(
        "DELETE FROM paper_migrations WHERE name='bl_source_calls_v1'"
    )
    await d._conn.commit()
    await d.close()

    d2 = Database(tmp_path / "partial_source_calls.db")
    await d2.initialize()
    cur = await d2._conn.execute(
        "SELECT COUNT(*) FROM paper_migrations WHERE name='bl_source_calls_v1'"
    )
    assert (await cur.fetchone())[0] == 1
    await d2.close()


async def test_source_calls_json_and_fk_constraints(db):
    with pytest.raises(sqlite3.IntegrityError):
        await db._conn.execute(
            "INSERT INTO source_calls "
            "(source_type, source_id, source_event_id, call_ts, call_kind, "
            "cluster_identity, cluster_identity_kind, duplicate_cluster_key, "
            "resolved_state, outcome_status, missing_fields) "
            "VALUES ('tg', '@a', 'bad-json', '2026-05-20T00:00:00+00:00', "
            "'unknown', 'bad-json', 'source_event', 'k', 'unresolved', "
            "'pending', '{}')"
        )
        await db._conn.commit()

    trade_id = await _insert_trade(db._conn, "coin-fk", "2026-05-20T00:00:00+00:00")
    await db._conn.execute(
        "INSERT INTO source_calls "
        "(source_type, source_id, source_event_id, token_id, call_ts, call_kind, "
        "cluster_identity, cluster_identity_kind, duplicate_cluster_key, "
        "resolved_state, linked_paper_trade_id, outcome_status, missing_fields) "
        "VALUES ('tg', '@a', 'fk-row', 'coin-fk', '2026-05-20T00:00:00+00:00', "
        "'unknown', 'coin-fk', 'token_id', 'fk-cluster', 'resolved', ?, "
        "'pending', '[]')",
        (trade_id,),
    )
    await db._conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        await db._conn.execute("DELETE FROM paper_trades WHERE id=?", (trade_id,))
        await db._conn.commit()
    await db._conn.rollback()


async def test_backfill_tg_and_x_is_idempotent_and_links_without_future_leak(db):
    tg_call_ts = "2026-05-20T00:00:00Z"
    await db._conn.execute(
        "INSERT INTO tg_social_messages "
        "(channel_handle, msg_id, posted_at, text, cashtags, contracts, parsed_at) "
        "VALUES ('@alpha', 1, ?, 'call $TOK', '[\"TOK\"]', '[]', ?)",
        (tg_call_ts, "2026-05-20T00:00:10+00:00"),
    )
    tg_msg_id = (await _fetchone(db._conn, "SELECT last_insert_rowid()"))[0]
    tg_trade_id = await _insert_trade(
        db._conn, "coin-tok", "2026-05-20T00:05:00+00:00", pnl_usd=12.0
    )
    await db._conn.execute(
        "INSERT INTO tg_social_signals "
        "(message_pk, token_id, symbol, contract_address, chain, mcap_at_sighting, "
        "resolution_state, source_channel_handle, paper_trade_id, created_at) "
        "VALUES (?, 'coin-tok', 'TOK', '0xabc', 'eth', 12300000, "
        "'resolved', '@alpha', ?, '2026-05-20 00:00:15')",
        (tg_msg_id, tg_trade_id),
    )

    await _insert_trade(db._conn, "coin-x", "2026-05-20T00:30:00+00:00", pnl_usd=-3.0)
    await db._conn.execute(
        "INSERT INTO narrative_alerts_inbound "
        "(event_id, tweet_id, tweet_author, tweet_ts, tweet_text, tweet_text_hash, "
        "extracted_cashtag, resolved_coin_id, narrative_theme, urgency_signal, "
        "classifier_version, received_at) "
        "VALUES ('evt-1', 'tw-1', 'kol_x', '2026-05-20T00:01:00+00:00', "
        "'watch $X', 'hash', 'X', 'coin-x', 'ai', 'high', 'v1', "
        "'2026-05-20T00:02:00+00:00')"
    )
    await db._conn.commit()

    first = await backfill_source_calls(db._conn)
    second = await backfill_source_calls(db._conn)
    assert first["inserted"] == 2
    assert second["inserted"] == 0

    assert (await _fetchone(db._conn, "SELECT COUNT(*) FROM source_calls"))[0] == 2
    tg = await _fetchone(
        db._conn,
        "SELECT source_id, call_ts, observed_at, ingest_delay_sec, "
        "linked_paper_trade_id, linkage_method, duplicate_rank_in_cluster "
        "FROM source_calls WHERE source_type='tg'",
    )
    assert tg["source_id"] == "@alpha"
    assert tg["call_ts"] == tg_call_ts
    assert tg["linked_paper_trade_id"] == tg_trade_id
    assert tg["linkage_method"] == "direct_tg"
    assert tg["ingest_delay_sec"] == 15
    assert tg["duplicate_rank_in_cluster"] == 1

    x = await _fetchone(
        db._conn,
        "SELECT source_id, linked_paper_trade_id, linkage_method, "
        "linkage_confidence, linkage_candidate_count, linkage_conflict_count "
        "FROM source_calls WHERE source_type='x'",
    )
    assert x["source_id"] == "kol_x"
    assert x["linked_paper_trade_id"] is not None
    assert x["linkage_method"] == "heuristic_x"
    assert x["linkage_confidence"] == "heuristic"
    assert x["linkage_candidate_count"] == 1
    assert x["linkage_conflict_count"] == 0


async def test_outcomes_parse_mixed_timestamps_and_bound_forward_windows(db):
    await db._conn.execute(
        "INSERT INTO narrative_alerts_inbound "
        "(event_id, tweet_id, tweet_author, tweet_ts, tweet_text, tweet_text_hash, "
        "extracted_cashtag, resolved_coin_id, narrative_theme, urgency_signal, "
        "classifier_version, received_at) "
        "VALUES ('evt-outcome', 'tw-2', 'kol_x', '2026-05-20T00:00:00Z', "
        "'watch $TOK', 'hash2', 'TOK', 'coin-tok', 'ai', 'high', 'v1', "
        "'2026-05-20 00:01:00')"
    )
    await _insert_gainer_price(db._conn, "coin-tok", 1.0, "2026-05-19 23:50:00")
    await _insert_gainer_price(db._conn, "coin-tok", 1.9, "2026-05-20T00:29:00+00:00")
    await _insert_gainer_price(db._conn, "coin-tok", 1.5, "2026-05-20T00:35:00+00:00")
    await _insert_gainer_price(db._conn, "coin-tok", 2.0, "2026-05-20T01:15:00+00:00")
    await _insert_gainer_price(db._conn, "coin-tok", 3.0, "2026-05-21T00:00:00+00:00")
    await _insert_gainer_price(db._conn, "coin-tok", 10.0, "2026-05-21T00:01:00+00:00")
    await db._conn.commit()

    await backfill_source_calls(db._conn)
    stats = await refresh_source_call_outcomes(
        db._conn, now=datetime(2026, 5, 22, tzinfo=timezone.utc)
    )
    assert stats["updated"] == 1

    row = await _fetchone(
        db._conn,
        "SELECT price_at_call, price_age_sec, forward_30m_pct, "
        "forward_30m_observed_horizon_sec, forward_1h_pct, forward_24h_pct, "
        "max_favorable_pct_24h, outcome_status, missing_fields "
        "FROM source_calls WHERE source_event_id='evt-outcome'",
    )
    assert row["price_at_call"] == 1.0
    assert row["price_age_sec"] == 600
    assert row["forward_30m_pct"] == 50.0
    assert row["forward_30m_observed_horizon_sec"] == 2100
    assert row["forward_1h_pct"] == 100.0
    assert row["forward_24h_pct"] == 200.0
    assert row["max_favorable_pct_24h"] == 200.0
    assert row["outcome_status"] == "partial"
    missing = json.loads(row["missing_fields"])
    assert {m["field"] for m in missing} == {"forward_6h_pct"}
    assert missing[0]["reason"] == "sparse_forward_window"


async def test_backfill_rerun_preserves_refreshed_outcome_state(db):
    await db._conn.execute(
        "INSERT INTO narrative_alerts_inbound "
        "(event_id, tweet_id, tweet_author, tweet_ts, tweet_text, tweet_text_hash, "
        "extracted_cashtag, resolved_coin_id, narrative_theme, urgency_signal, "
        "classifier_version, received_at) "
        "VALUES ('evt-preserve', 'tw-preserve', 'kol_x', '2026-05-20T00:00:00Z', "
        "'watch $TOK', 'hash-preserve', 'TOK', 'coin-preserve', 'ai', 'high', "
        "'v1', '2026-05-20T00:01:00+00:00')"
    )
    for price, snapshot_at in (
        (1.0, "2026-05-19T23:59:00+00:00"),
        (1.5, "2026-05-20T00:35:00+00:00"),
        (2.0, "2026-05-20T01:15:00+00:00"),
        (2.5, "2026-05-20T06:15:00+00:00"),
        (3.0, "2026-05-21T00:00:00+00:00"),
    ):
        await _insert_gainer_price(db._conn, "coin-preserve", price, snapshot_at)
    await db._conn.commit()

    await backfill_source_calls(db._conn)
    await refresh_source_call_outcomes(
        db._conn, now=datetime(2026, 5, 22, tzinfo=timezone.utc)
    )
    before = await _fetchone(
        db._conn,
        "SELECT outcome_status, missing_fields, forward_30m_pct, created_at "
        "FROM source_calls WHERE source_event_id='evt-preserve'",
    )
    assert before["outcome_status"] == "complete"
    assert before["missing_fields"] == "[]"
    assert before["forward_30m_pct"] == 50.0

    await backfill_source_calls(db._conn)
    after = await _fetchone(
        db._conn,
        "SELECT outcome_status, missing_fields, forward_30m_pct, created_at "
        "FROM source_calls WHERE source_event_id='evt-preserve'",
    )
    assert after["outcome_status"] == "complete"
    assert after["missing_fields"] == "[]"
    assert after["forward_30m_pct"] == 50.0
    assert after["created_at"] == before["created_at"]


async def test_stale_at_call_price_suppresses_short_horizons(db):
    await db._conn.execute(
        "INSERT INTO narrative_alerts_inbound "
        "(event_id, tweet_id, tweet_author, tweet_ts, tweet_text, tweet_text_hash, "
        "extracted_cashtag, resolved_coin_id, narrative_theme, urgency_signal, "
        "classifier_version, received_at) "
        "VALUES ('evt-stale', 'tw-3', 'kol_x', '2026-05-20T00:00:00+00:00', "
        "'watch $TOK', 'hash3', 'TOK', 'coin-stale', 'ai', 'high', 'v1', "
        "'2026-05-20T00:01:00+00:00')"
    )
    await _insert_gainer_price(db._conn, "coin-stale", 1.0, "2026-05-19T23:35:00+00:00")
    await _insert_gainer_price(db._conn, "coin-stale", 1.5, "2026-05-20T00:35:00+00:00")
    await _insert_gainer_price(db._conn, "coin-stale", 2.0, "2026-05-20T01:15:00+00:00")
    await db._conn.commit()

    await backfill_source_calls(db._conn)
    await refresh_source_call_outcomes(
        db._conn, now=datetime(2026, 5, 21, 2, tzinfo=timezone.utc)
    )
    row = await _fetchone(
        db._conn,
        "SELECT forward_30m_pct, forward_1h_pct, missing_fields "
        "FROM source_calls WHERE source_event_id='evt-stale'",
    )
    assert row["forward_30m_pct"] is None
    assert row["forward_1h_pct"] == 100.0
    missing = json.loads(row["missing_fields"])
    assert {"field": "forward_30m_pct", "reason": "stale_at_call"} in missing


async def test_stale_at_call_price_suppresses_24h_extrema(db):
    await db._conn.execute(
        "INSERT INTO narrative_alerts_inbound "
        "(event_id, tweet_id, tweet_author, tweet_ts, tweet_text, tweet_text_hash, "
        "extracted_cashtag, resolved_coin_id, narrative_theme, urgency_signal, "
        "classifier_version, received_at) "
        "VALUES ('evt-stale-extrema', 'tw-extrema', 'kol_x', "
        "'2026-05-20T00:00:00+00:00', 'watch $TOK', 'hash-extrema', 'TOK', "
        "'coin-extrema', 'ai', 'high', 'v1', '2026-05-20T00:01:00+00:00')"
    )
    await _insert_gainer_price(
        db._conn, "coin-extrema", 1.0, "2026-05-19T22:30:00+00:00"
    )
    await _insert_gainer_price(
        db._conn, "coin-extrema", 4.0, "2026-05-20T03:00:00+00:00"
    )
    await db._conn.commit()

    await backfill_source_calls(db._conn)
    await refresh_source_call_outcomes(
        db._conn, now=datetime(2026, 5, 21, 2, tzinfo=timezone.utc)
    )
    row = await _fetchone(
        db._conn,
        "SELECT max_favorable_pct_24h, max_adverse_pct_24h, time_to_peak_min, "
        "missing_fields FROM source_calls WHERE source_event_id='evt-stale-extrema'",
    )
    assert row["max_favorable_pct_24h"] is None
    assert row["max_adverse_pct_24h"] is None
    assert row["time_to_peak_min"] is None
    missing = json.loads(row["missing_fields"])
    assert {"field": "max_favorable_pct_24h", "reason": "stale_at_call"} in missing


async def _insert_inbound(
    conn, *, event_id, cashtag=None, ca=None, chain=None, coin_id=None
):
    await conn.execute(
        "INSERT INTO narrative_alerts_inbound "
        "(event_id, tweet_id, tweet_author, tweet_ts, tweet_text, tweet_text_hash, "
        "extracted_cashtag, extracted_ca, extracted_chain, resolved_coin_id, "
        "narrative_theme, urgency_signal, classifier_version, received_at) "
        "VALUES (?, ?, 'kol_x', '2026-05-20T00:00:00Z', 'tweet', ?, ?, ?, ?, ?, "
        "'ai', 'high', 'v1', '2026-05-20T00:01:00+00:00')",
        (event_id, f"tw-{event_id}", f"hash-{event_id}", cashtag, ca, chain, coin_id),
    )


async def test_c1_priceable_identity_classification():
    # coin_id wins; else contract; cashtag-only is NOT priceable (design #392 §4.0).
    from scout.source_quality.ledger import _priceable_identity

    assert _priceable_identity({"token_id": "coin-x"}) == ("token_id", "coin-x")
    assert _priceable_identity(
        {"token_id": None, "contract_address": "0xAbC", "chain": "base"}
    ) == ("contract", "base|0xabc")
    assert (
        _priceable_identity(
            {"token_id": None, "contract_address": None, "symbol": "MEME"}
        )
        is None
    )


async def test_c1_ca_only_call_marked_eligible_not_skipped(db):
    # Prior bug: a falsy token_id short-circuited CA-only calls to unresolvable.
    # C1: CA identity is eligible for later pricing, not silently skipped, and no
    # performance field is written (C2 does the pricing).
    await _insert_inbound(
        db._conn, event_id="evt-ca", ca="0xDEADbeef", chain="base", coin_id=None
    )
    await db._conn.commit()
    await backfill_source_calls(db._conn)
    stats = await refresh_source_call_outcomes(
        db._conn, now=datetime(2026, 5, 22, tzinfo=timezone.utc)
    )

    row = await _fetchone(
        db._conn,
        "SELECT token_id, contract_address, resolved_state, price_at_call, "
        "forward_24h_pct, max_favorable_pct_24h FROM source_calls "
        "WHERE source_event_id='evt-ca'",
    )
    assert row["token_id"] is None
    assert row["contract_address"] == "0xDEADbeef"
    assert row["resolved_state"] == "eligible_contract"
    assert row["price_at_call"] is None
    assert row["forward_24h_pct"] is None
    assert row["max_favorable_pct_24h"] is None
    assert stats["eligible_contract"] == 1
    assert stats["updated"] == 0


async def test_c1_cashtag_only_call_unresolved_not_priceable(db):
    await _insert_inbound(db._conn, event_id="evt-cash", cashtag="MEME", coin_id=None)
    await db._conn.commit()
    await backfill_source_calls(db._conn)
    stats = await refresh_source_call_outcomes(
        db._conn, now=datetime(2026, 5, 22, tzinfo=timezone.utc)
    )

    row = await _fetchone(
        db._conn,
        "SELECT resolved_state, price_at_call FROM source_calls "
        "WHERE source_event_id='evt-cash'",
    )
    assert row["resolved_state"] == "unresolved"
    assert row["price_at_call"] is None
    assert stats["unresolved_identity"] == 1
    assert stats["eligible_contract"] == 0


async def test_c1_token_id_call_still_priced_not_regressed(db):
    await _insert_inbound(
        db._conn, event_id="evt-coin", cashtag="TOK", coin_id="coin-tok"
    )
    await _insert_gainer_price(db._conn, "coin-tok", 1.0, "2026-05-19 23:59:00")
    await _insert_gainer_price(db._conn, "coin-tok", 1.5, "2026-05-20T00:35:00+00:00")
    await db._conn.commit()
    await backfill_source_calls(db._conn)
    stats = await refresh_source_call_outcomes(
        db._conn, now=datetime(2026, 5, 22, tzinfo=timezone.utc)
    )

    row = await _fetchone(
        db._conn,
        "SELECT token_id, price_at_call, forward_30m_pct FROM source_calls "
        "WHERE source_event_id='evt-coin'",
    )
    assert row["token_id"] == "coin-tok"
    assert row["price_at_call"] == 1.0
    assert row["forward_30m_pct"] == 50.0
    assert stats["updated"] == 1


async def test_summary_uses_distinct_eligible_clusters_and_coverage_gate(db):
    call_ts = datetime(2026, 5, 20, tzinfo=timezone.utc)
    for idx in range(12):
        await db._conn.execute(
            "INSERT INTO source_calls "
            "(source_type, source_id, source_event_id, token_id, symbol, call_ts, "
            "call_kind, cluster_identity, cluster_identity_kind, "
            "duplicate_cluster_key, duplicate_rank_in_cluster, resolved_state, "
            "forward_30m_pct, linked_paper_trade_id, linkage_method, "
            "linkage_confidence, outcome_status, missing_fields) "
            "VALUES ('tg', '@quality', ?, ?, 'TOK', ?, 'ca_call', ?, 'token_id', "
            "?, 1, 'resolved', ?, NULL, 'none', 'none', ?, ?)",
            (
                f"evt-{idx}",
                f"coin-{idx}",
                (call_ts + timedelta(minutes=idx)).isoformat(),
                f"coin-{idx}",
                f"cluster-{idx}",
                10.0 if idx < 10 else None,
                "complete" if idx < 10 else "partial",
                (
                    "[]"
                    if idx < 10
                    else '[{"field":"forward_30m_pct","reason":"no_time_series"}]'
                ),
            ),
        )
    for idx in range(9):
        await db._conn.execute(
            "INSERT INTO source_calls "
            "(source_type, source_id, source_event_id, token_id, symbol, call_ts, "
            "call_kind, cluster_identity, cluster_identity_kind, "
            "duplicate_cluster_key, duplicate_rank_in_cluster, resolved_state, "
            "forward_30m_pct, linkage_method, linkage_confidence, outcome_status, "
            "missing_fields) "
            "VALUES ('x', 'small_kol', ?, ?, 'SMOL', ?, 'cashtag_only', ?, "
            "'token_id', ?, 1, 'resolved', 5.0, 'none', 'none', 'complete', '[]')",
            (
                f"small-{idx}",
                f"small-{idx}",
                (call_ts + timedelta(minutes=idx)).isoformat(),
                f"small-{idx}",
                f"small-cluster-{idx}",
            ),
        )
    await db._conn.commit()

    rows = await compute_source_quality_summary(
        db._conn, min_sample=10, min_coverage_rate=0.75
    )
    by_source = {(r.source_type, r.source_id): r for r in rows}
    assert (
        by_source[("tg", "@quality")].rank_status
        == "rankable_resolvable_cg_board_cohort"
    )
    assert by_source[("tg", "@quality")].eligible_distinct_clusters == 10
    assert by_source[("tg", "@quality")].coverage_rate == pytest.approx(10 / 12)
    assert by_source[("x", "small_kol")].rank_status == "insufficient_sample"


async def test_summary_averages_use_first_duplicate_cluster_only(db):
    trade_1 = await _insert_trade(
        db._conn, "coin-dup", "2026-05-20T00:05:00+00:00", pnl_usd=10.0
    )
    trade_2 = await _insert_trade(
        db._conn, "coin-dup", "2026-05-20T00:10:00+00:00", pnl_usd=-90.0
    )
    await db._conn.execute(
        "INSERT INTO source_calls "
        "(source_type, source_id, source_event_id, token_id, symbol, call_ts, "
        "call_kind, cluster_identity, cluster_identity_kind, duplicate_cluster_key, "
        "duplicate_rank_in_cluster, resolved_state, forward_30m_pct, "
        "linked_paper_trade_id, linkage_method, linkage_confidence, outcome_status, "
        "missing_fields) VALUES "
        "('tg', '@dup', 'dup-1', 'coin-dup', 'DUP', '2026-05-20T00:00:00+00:00', "
        "'ca_call', 'coin-dup', 'token_id', 'dup-cluster', 1, 'resolved', 10, ?, "
        "'direct_tg', 'direct', 'complete', '[]'), "
        "('tg', '@dup', 'dup-2', 'coin-dup', 'DUP', '2026-05-20T00:01:00+00:00', "
        "'ca_call', 'coin-dup', 'token_id', 'dup-cluster', 2, 'resolved', 1000, ?, "
        "'direct_tg', 'direct', 'complete', '[]')",
        (trade_1, trade_2),
    )
    await db._conn.commit()

    rows = await compute_source_quality_summary(
        db._conn, min_sample=1, min_coverage_rate=0.0
    )
    row = {(r.source_type, r.source_id): r for r in rows}[("tg", "@dup")]
    assert row.eligible_distinct_clusters == 1
    assert row.avg_forward_30m_pct == 10.0
    assert row.avg_strategy_pnl_usd == 10.0


async def test_x_conflict_linkage_does_not_choose_concrete_trade(db):
    await _insert_trade(
        db._conn, "coin-conflict", "2026-05-20T00:10:00+00:00", pnl_usd=10.0
    )
    await _insert_trade(
        db._conn, "coin-conflict", "2026-05-20T00:20:00+00:00", pnl_usd=20.0
    )
    await db._conn.execute(
        "INSERT INTO narrative_alerts_inbound "
        "(event_id, tweet_id, tweet_author, tweet_ts, tweet_text, tweet_text_hash, "
        "extracted_cashtag, resolved_coin_id, narrative_theme, urgency_signal, "
        "classifier_version, received_at) "
        "VALUES ('evt-conflict', 'tw-conflict', 'kol_x', "
        "'2026-05-20T00:00:00+00:00', 'watch $TOK', 'hash-conflict', 'TOK', "
        "'coin-conflict', 'ai', 'high', 'v1', '2026-05-20T00:00:00+00:00')"
    )
    await db._conn.commit()

    await backfill_source_calls(db._conn)
    row = await _fetchone(
        db._conn,
        "SELECT linked_paper_trade_id, linkage_candidate_count, "
        "linkage_conflict_count, linkage_method, linkage_confidence "
        "FROM source_calls WHERE source_event_id='evt-conflict'",
    )
    assert row["linked_paper_trade_id"] is None
    assert row["linkage_candidate_count"] == 2
    assert row["linkage_conflict_count"] == 1
    assert row["linkage_method"] == "heuristic_x"
    assert row["linkage_confidence"] == "conflict"


async def test_source_calls_lag_watchdog_fails_matches_and_passes_quiet_period(db):
    old_ts = "2026-05-20T00:00:00+00:00"
    now = datetime(2026, 5, 20, 1, tzinfo=timezone.utc)
    await db._conn.execute(
        "INSERT INTO tg_social_messages "
        "(channel_handle, msg_id, posted_at, text, parsed_at) "
        "VALUES ('@lag', 9, ?, 'late', ?)",
        (old_ts, old_ts),
    )
    msg_id = (await _fetchone(db._conn, "SELECT last_insert_rowid()"))[0]
    await db._conn.execute(
        "INSERT INTO tg_social_signals "
        "(message_pk, token_id, symbol, resolution_state, source_channel_handle, created_at) "
        "VALUES (?, 'coin-lag', 'LAG', 'resolved', '@lag', ?)",
        (msg_id, old_ts),
    )
    await db._conn.commit()

    failing = await check_source_calls_lag(db._conn, now=now, threshold_minutes=30)
    assert failing.ok is False
    assert failing.unledgered_tg == 1

    await backfill_source_calls(db._conn)
    passing = await check_source_calls_lag(db._conn, now=now, threshold_minutes=30)
    assert passing.ok is True
    assert passing.unledgered_tg == 0

    empty = Database(":memory:")
    await empty.initialize()
    try:
        quiet = await check_source_calls_lag(empty._conn, now=now, threshold_minutes=30)
        assert quiet.ok is True
    finally:
        await empty.close()


async def test_lag_watchdog_script_refuses_missing_db(tmp_path):
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "check_source_calls_lag.py"
    )
    missing_db = tmp_path / "does-not-exist.db"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--db",
            str(missing_db),
            "--threshold-minutes",
            "30",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 3
    assert not missing_db.exists()
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"] == "db_not_found"
