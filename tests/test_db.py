"""Tests for scout.db module."""

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


# ---------------------------------------------------------------------------
# BL-NEW-SCORE-HISTORY-PRUNING + BL-NEW-VOLUME-SNAPSHOTS-PRUNING — index + prune
# ---------------------------------------------------------------------------


async def test_prune_score_history_uses_scanned_at_index(db):
    """V1#5 / V2#5 / V4#3 fold: DELETE WHERE scanned_at <= ? must use the
    new single-column idx_score_history_scanned_at, not table-scan.

    Existing idx_score_hist_addr (contract_address, scanned_at) cannot serve
    the time-only predicate because contract_address is the leading column.
    """
    cur = await db._conn.execute(
        "EXPLAIN QUERY PLAN DELETE FROM score_history WHERE scanned_at <= ?",
        ("2026-01-01T00:00:00+00:00",),
    )
    plan = await cur.fetchall()
    plan_str = " ".join(str(row[3]) for row in plan)
    assert (
        "idx_score_history_scanned_at" in plan_str
    ), f"Index not used: {plan_str}"


async def test_prune_volume_snapshots_uses_scanned_at_index(db):
    """Same as above for volume_snapshots."""
    cur = await db._conn.execute(
        "EXPLAIN QUERY PLAN DELETE FROM volume_snapshots WHERE scanned_at <= ?",
        ("2026-01-01T00:00:00+00:00",),
    )
    plan = await cur.fetchall()
    plan_str = " ".join(str(row[3]) for row in plan)
    assert (
        "idx_volume_snapshots_scanned_at" in plan_str
    ), f"Index not used: {plan_str}"


async def test_prune_score_history_keeps_recent(db):
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=5)).isoformat()
    old = (now - timedelta(days=20)).isoformat()
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        ("0xRECENT", 50.0, recent),
    )
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        ("0xOLD", 50.0, old),
    )
    await db._conn.commit()

    deleted = await db.prune_score_history(keep_days=14)

    assert deleted == 1
    cur = await db._conn.execute("SELECT contract_address FROM score_history")
    rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["0xRECENT"]


async def test_prune_score_history_empty_table_returns_zero(db):
    deleted = await db.prune_score_history(keep_days=14)
    assert deleted == 0


async def test_prune_score_history_keep_days_zero_deletes_all(db):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        ("0xANY", 50.0, now),
    )
    await db._conn.commit()
    deleted = await db.prune_score_history(keep_days=0)
    assert deleted == 1


async def test_prune_score_history_tie_on_cutoff_deletes(db):
    """V1#11 fold: lock in <= semantic. Row with scanned_at == cutoff must be pruned.

    Matches cryptopanic_posts boundary semantic at db.py:4754-4758 (Windows
    clock-tie). If a future PR flips to <, this test catches it.
    """
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=14)
    cutoff_iso = cutoff_dt.isoformat()
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        ("0xTIE", 50.0, cutoff_iso),
    )
    await db._conn.commit()
    deleted = await db.prune_score_history(keep_days=14)
    assert deleted == 1


async def test_prune_volume_snapshots_keeps_recent(db):
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=5)).isoformat()
    old = (now - timedelta(days=20)).isoformat()
    await db._conn.execute(
        "INSERT INTO volume_snapshots (contract_address, volume_24h_usd, scanned_at) VALUES (?, ?, ?)",
        ("0xRECENT", 100000.0, recent),
    )
    await db._conn.execute(
        "INSERT INTO volume_snapshots (contract_address, volume_24h_usd, scanned_at) VALUES (?, ?, ?)",
        ("0xOLD", 100000.0, old),
    )
    await db._conn.commit()

    deleted = await db.prune_volume_snapshots(keep_days=14)

    assert deleted == 1
    cur = await db._conn.execute("SELECT contract_address FROM volume_snapshots")
    rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["0xRECENT"]


async def test_prune_volume_snapshots_empty_table_returns_zero(db):
    deleted = await db.prune_volume_snapshots(keep_days=14)
    assert deleted == 0


async def test_prune_volume_snapshots_tie_on_cutoff_deletes(db):
    """V6 fold: parity with prune_score_history tie-on-cutoff test.

    Locks in <= semantic on volume side. Without this, a future PR could
    flip score to keep <= while flipping volume to <, and only the score
    test would catch it.
    """
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=14)
    cutoff_iso = cutoff_dt.isoformat()
    await db._conn.execute(
        "INSERT INTO volume_snapshots (contract_address, volume_24h_usd, scanned_at) VALUES (?, ?, ?)",
        ("0xTIE", 100000.0, cutoff_iso),
    )
    await db._conn.commit()
    deleted = await db.prune_volume_snapshots(keep_days=14)
    assert deleted == 1


async def test_prune_score_history_future_dated_rows_survive_keep_days_zero(db):
    """V6 fold: keep_days=0 must NOT delete future-dated rows (clock skew /
    test seed). cutoff = now, predicate is scanned_at <= cutoff — future
    scanned_at > now > cutoff → must survive.
    """
    now = datetime.now(timezone.utc)
    future = (now + timedelta(hours=1)).isoformat()
    past = (now - timedelta(seconds=1)).isoformat()
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        ("0xFUTURE", 50.0, future),
    )
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        ("0xPAST", 50.0, past),
    )
    await db._conn.commit()

    deleted = await db.prune_score_history(keep_days=0)

    assert deleted == 1
    cur = await db._conn.execute("SELECT contract_address FROM score_history")
    rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["0xFUTURE"]


@pytest.mark.parametrize(
    "prune_method,table,column,fk_seed_sql,fk_seed_params,insert_sql,insert_extra_cols",
    [
        (
            "prune_volume_spikes",
            "volume_spikes",
            "detected_at",
            None,
            None,
            (
                "INSERT INTO volume_spikes (coin_id, symbol, name, current_volume,"
                " avg_volume_7d, spike_ratio, detected_at) VALUES (?,?,?,?,?,?,?)"
            ),
            ("c-tie", "TIE", "TIE", 1000.0, 500.0, 2.0),
        ),
        (
            "prune_momentum_7d",
            "momentum_7d",
            "detected_at",
            None,
            None,
            "INSERT INTO momentum_7d (coin_id, symbol, name, price_change_7d, detected_at) VALUES (?,?,?,?,?)",
            ("c-tie", "TIE", "TIE", 100.0),
        ),
        (
            "prune_trending_snapshots",
            "trending_snapshots",
            "snapshot_at",
            None,
            None,
            "INSERT INTO trending_snapshots (coin_id, symbol, name, snapshot_at) VALUES (?,?,?,?)",
            ("c-tie", "TIE", "TIE"),
        ),
        # learn_logs intentionally OMITTED from this parametrize:
        # its DEFAULT format is SQLite-style (YYYY-MM-DD HH:MM:SS) while the
        # other 5 tables use ISO. The dedicated mixed-format regression test
        # `test_prune_learn_logs_mixed_format_boundary_regression` covers
        # learn_logs' boundary behavior in its native format.
        (
            "prune_chain_matches",
            "chain_matches",
            "completed_at",
            "INSERT INTO chain_patterns (name, description, steps_json, min_steps_to_trigger) VALUES (?,?,?,?)",
            ("tie_pattern", "tie", '["x"]', 1),
            (
                "INSERT INTO chain_matches (token_id, pipeline, pattern_id, pattern_name,"
                " steps_matched, total_steps, anchor_time, chain_duration_hours,"
                " conviction_boost, completed_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
            ),
            ("t-tie", "memecoin", 1, "tie_pattern", 1, 1, "2026-01-01T00:00:00+00:00", 1.0, 0),
        ),
        (
            "prune_holder_snapshots",
            "holder_snapshots",
            "scanned_at",
            None,
            None,
            "INSERT INTO holder_snapshots (contract_address, holder_count, scanned_at) VALUES (?,?,?)",
            ("0xTIE", 100),
        ),
    ],
)
async def test_narrative_prune_tie_on_cutoff_deletes(
    db,
    prune_method,
    table,
    column,
    fk_seed_sql,
    fk_seed_params,
    insert_sql,
    insert_extra_cols,
):
    """V11 PR-review SHOULD-FIX: parity with cycle 1's
    test_prune_score_history_tie_on_cutoff_deletes for all 6 cycle 2 tables.

    Locks in <= boundary semantic per V1#11 cycle 1 convention. Future PR
    flipping <= to < on any of the 6 tables gets caught here.
    """
    if fk_seed_sql:
        await db._conn.execute(fk_seed_sql, fk_seed_params)
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    await db._conn.execute(insert_sql, (*insert_extra_cols, cutoff_iso))
    await db._conn.commit()

    deleted = await getattr(db, prune_method)(keep_days=30)
    assert deleted == 1


async def test_migration_idempotency_records_all_seven_cycle_1_and_2(tmp_path):
    """V11 SHOULD-FIX: cycle 1's idempotency test asserts via set-equality
    (after_first == after_second) but doesn't EXPLICITLY assert all 7
    cycle-1+2 migration rows are present. If a cycle-2 migration silently
    failed to INSERT into paper_migrations, set-equality would pass vacuously
    (both empty/missing the row) and a subsequent restart would re-run the
    CREATE INDEX — blocking dashboard for 30-60s.
    """
    db = Database(str(tmp_path / "explicit_idempotency.db"))
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT name FROM paper_migrations WHERE name LIKE '%_idx_v1'"
    )
    names = {row[0] for row in await cur.fetchall()}
    await db.close()

    expected = {
        "score_history_scanned_at_idx_v1",
        "volume_snapshots_scanned_at_idx_v1",
        "volume_spikes_detected_at_idx_v1",
        "momentum_7d_detected_at_idx_v1",
        "trending_snapshots_snapshot_at_idx_v1",
        "learn_logs_created_at_idx_v1",
        "holder_snapshots_scanned_at_idx_v1",
        "chain_matches_completed_at_idx_v1",
    }
    missing = expected - names
    assert not missing, f"Missing paper_migrations rows after initialize(): {missing}"


@pytest.mark.parametrize(
    "table,column,index",
    [
        ("volume_spikes", "detected_at", "idx_volume_spikes_detected_at"),
        ("momentum_7d", "detected_at", "idx_momentum_7d_detected_at"),
        ("trending_snapshots", "snapshot_at", "idx_trending_snapshots_snapshot_at"),
        ("learn_logs", "created_at", "idx_learn_logs_created_at"),
        ("holder_snapshots", "scanned_at", "idx_holder_snapshots_scanned_at"),
        ("chain_matches", "completed_at", "idx_chain_matches_completed_at"),
    ],
)
async def test_narrative_table_prune_uses_new_index(db, table, column, index):
    """BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION (V9 plan-review fold): each
    of the 5 new tables must have a usable single-column index for the
    prune DELETE's time-only WHERE clause."""
    cur = await db._conn.execute(
        f"EXPLAIN QUERY PLAN DELETE FROM {table} WHERE {column} <= ?",
        ("2026-01-01T00:00:00+00:00",),
    )
    plan = await cur.fetchall()
    plan_str = " ".join(str(row[3]) for row in plan)
    assert index in plan_str, f"{index} not used: {plan_str}"


# ---------------------------------------------------------------------------
# BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION (cycle 2) prune-method tests
# ---------------------------------------------------------------------------


async def test_prune_volume_spikes_keeps_recent(db):
    now = datetime.now(timezone.utc)
    for tag, age_days in [("RECENT", 5), ("OLD", 50)]:
        await db._conn.execute(
            """INSERT INTO volume_spikes (coin_id, symbol, name, current_volume,
                avg_volume_7d, spike_ratio, detected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                f"c-{tag.lower()}",
                tag,
                tag,
                1000.0,
                500.0,
                2.0,
                (now - timedelta(days=age_days)).isoformat(),
            ),
        )
    await db._conn.commit()
    deleted = await db.prune_volume_spikes(keep_days=45)
    assert deleted == 1


async def test_prune_volume_spikes_empty_returns_zero(db):
    assert await db.prune_volume_spikes(keep_days=45) == 0


async def test_prune_momentum_7d_keeps_recent(db):
    now = datetime.now(timezone.utc)
    for tag, age_days in [("RECENT", 5), ("OLD", 50)]:
        await db._conn.execute(
            """INSERT INTO momentum_7d (coin_id, symbol, name, price_change_7d, detected_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                f"c-{tag.lower()}",
                tag,
                tag,
                100.0,
                (now - timedelta(days=age_days)).isoformat(),
            ),
        )
    await db._conn.commit()
    deleted = await db.prune_momentum_7d(keep_days=30)
    assert deleted == 1


async def test_prune_momentum_7d_empty_returns_zero(db):
    assert await db.prune_momentum_7d(keep_days=30) == 0


async def test_prune_trending_snapshots_keeps_recent(db):
    now = datetime.now(timezone.utc)
    for tag, age_days in [("RECENT", 5), ("OLD", 50)]:
        await db._conn.execute(
            """INSERT INTO trending_snapshots (coin_id, symbol, name, snapshot_at)
               VALUES (?, ?, ?, ?)""",
            (
                f"c-{tag.lower()}",
                tag,
                tag,
                (now - timedelta(days=age_days)).isoformat(),
            ),
        )
    await db._conn.commit()
    deleted = await db.prune_trending_snapshots(keep_days=30)
    assert deleted == 1


async def test_prune_trending_snapshots_empty_returns_zero(db):
    assert await db.prune_trending_snapshots(keep_days=30) == 0


async def test_prune_learn_logs_keeps_recent(db):
    """learn_logs.created_at uses SQLite-format DEFAULT (`YYYY-MM-DD HH:MM:SS`),
    so the test must seed in that format to mirror production rows."""
    now = datetime.now(timezone.utc)
    for n, age_days in [(1, 5), (2, 100)]:
        await db._conn.execute(
            """INSERT INTO learn_logs (cycle_number, cycle_type, reflection_text,
                changes_made, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                n,
                "daily",
                f"reflection {n}",
                f"changes {n}",
                (now - timedelta(days=age_days)).strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
    await db._conn.commit()
    deleted = await db.prune_learn_logs(keep_days=90)
    assert deleted == 1


async def test_prune_learn_logs_mixed_format_boundary_regression(db):
    """PR-review fold (user-found bug 2026-05-16): pre-fix, raw lexical
    comparison against an ISO cutoff would delete same-day rows because
    space (0x20) sorts before 'T' (0x54).

    Reproduction: insert two SQLite-format rows (matching production
    DEFAULT) — one well into "today" relative to "now - keep_days", and
    one comfortably old. With keep_days=1, only the old row should be
    deleted. Pre-fix bug: BOTH would be deleted because the today-23:59:59
    row lexically compares LESS than an ISO cutoff like 2026-05-15T<time>.
    Post-fix: cutoff in SQLite format → correct.
    """
    now = datetime.now(timezone.utc)
    # Row 1: SAME-DAY-LATE — would lexically compare < ISO cutoff (the bug)
    today_late = now.replace(hour=23, minute=59, second=59, microsecond=0)
    await db._conn.execute(
        """INSERT INTO learn_logs (cycle_number, cycle_type, reflection_text,
            changes_made, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            1,
            "daily",
            "today-23:59",
            "{}",
            today_late.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    # Row 2: comfortably old (10 days back) — should always be deleted
    old = now - timedelta(days=10)
    await db._conn.execute(
        """INSERT INTO learn_logs (cycle_number, cycle_type, reflection_text,
            changes_made, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            2,
            "daily",
            "10-days-old",
            "{}",
            old.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    await db._conn.commit()

    # keep_days=1: cutoff = now - 1 day. Today's row (newer than cutoff) must survive.
    deleted = await db.prune_learn_logs(keep_days=1)

    assert deleted == 1, f"Expected only the 10-day-old row deleted; got {deleted}"
    cur = await db._conn.execute(
        "SELECT reflection_text FROM learn_logs ORDER BY created_at DESC"
    )
    remaining = [row[0] for row in await cur.fetchall()]
    assert remaining == ["today-23:59"]


async def test_prune_learn_logs_uses_default_format_when_no_created_at_supplied(db):
    """Production rows are inserted via the DEFAULT (writers at
    ``scout/narrative/learner.py:291,436`` don't pass ``created_at``).
    Verify the prune still operates correctly on DEFAULT-formatted rows."""
    # No created_at supplied — SQLite fills via datetime('now') DEFAULT
    await db._conn.execute(
        """INSERT INTO learn_logs (cycle_number, cycle_type, reflection_text,
            changes_made) VALUES (?, ?, ?, ?)""",
        (99, "daily", "today-DEFAULT", "{}"),
    )
    await db._conn.commit()

    # keep_days=0 — cutoff is now, the just-inserted row should NOT yet be
    # past the cutoff (datetime('now') and Python now() are at-or-before the
    # cutoff by microseconds, but both formats agree at YYYY-MM-DD HH:MM:SS
    # granularity; this test asserts no false-deletion when same-second).
    # Use a higher keep_days for safety to assert non-deletion of fresh row.
    deleted = await db.prune_learn_logs(keep_days=1)
    assert deleted == 0
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM learn_logs WHERE reflection_text = 'today-DEFAULT'"
    )
    row = await cur.fetchone()
    assert row[0] == 1, "DEFAULT-formatted row should not be deleted with keep_days=1"


async def test_prune_learn_logs_empty_returns_zero(db):
    assert await db.prune_learn_logs(keep_days=90) == 0


async def test_prune_chain_matches_keeps_recent(db):
    now = datetime.now(timezone.utc)
    # chain_matches.pattern_id is FK to chain_patterns; seed parent row first.
    await db._conn.execute(
        """INSERT INTO chain_patterns (name, description, steps_json, min_steps_to_trigger)
           VALUES (?, ?, ?, ?)""",
        ("test_pattern", "test", '["x"]', 1),
    )
    pattern_id_row = await (
        await db._conn.execute("SELECT id FROM chain_patterns LIMIT 1")
    ).fetchone()
    pattern_id = pattern_id_row[0]
    for tag, age_days in [("RECENT", 5), ("OLD", 60)]:
        await db._conn.execute(
            """INSERT INTO chain_matches (token_id, pipeline, pattern_id, pattern_name,
                steps_matched, total_steps, anchor_time, completed_at,
                chain_duration_hours, conviction_boost)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"t-{tag.lower()}",
                "memecoin",
                pattern_id,
                "test_pattern",
                1,
                1,
                (now - timedelta(days=age_days, hours=1)).isoformat(),
                (now - timedelta(days=age_days)).isoformat(),
                1.0,
                0,
            ),
        )
    await db._conn.commit()
    deleted = await db.prune_chain_matches(keep_days=45)
    assert deleted == 1


async def test_prune_chain_matches_empty_returns_zero(db):
    assert await db.prune_chain_matches(keep_days=45) == 0


async def test_prune_holder_snapshots_keeps_recent(db):
    now = datetime.now(timezone.utc)
    for tag, age_days in [("RECENT", 5), ("OLD", 30)]:
        await db._conn.execute(
            """INSERT INTO holder_snapshots (contract_address, holder_count, scanned_at)
               VALUES (?, ?, ?)""",
            (f"0x{tag}", 100, (now - timedelta(days=age_days)).isoformat()),
        )
    await db._conn.commit()
    deleted = await db.prune_holder_snapshots(keep_days=14)
    assert deleted == 1


async def test_prune_holder_snapshots_empty_returns_zero(db):
    assert await db.prune_holder_snapshots(keep_days=14) == 0


async def test_migration_idempotent_on_second_initialize(tmp_path):
    """V6 fold: migration idempotency via paper_migrations row check.

    A future refactor breaking the SELECT 1 FROM paper_migrations guard
    would cause CREATE INDEX to re-run on every restart, blocking dashboard
    reads for 30-60s. This test calls initialize() twice on the same DB
    and asserts the second pass skips the CREATE INDEX execution.
    """
    db = Database(str(tmp_path / "idempotent.db"))
    await db.initialize()
    # Snapshot paper_migrations rows after first init
    cur = await db._conn.execute("SELECT name FROM paper_migrations")
    after_first = {row[0] for row in await cur.fetchall()}
    await db.close()

    # Re-init — second pass should skip the score/volume migrations
    db2 = Database(str(tmp_path / "idempotent.db"))
    await db2.initialize()
    cur = await db2._conn.execute("SELECT name FROM paper_migrations")
    after_second = {row[0] for row in await cur.fetchall()}
    await db2.close()

    assert "score_history_scanned_at_idx_v1" in after_first
    assert "volume_snapshots_scanned_at_idx_v1" in after_first
    assert after_first == after_second  # no rows added on re-init


async def test_upsert_and_retrieve(db, token_factory):
    token = token_factory(quant_score=75)
    await db.upsert_candidate(token)
    candidates = await db.get_candidates_above_score(60)
    assert len(candidates) == 1
    assert candidates[0]["contract_address"] == "0xtest"
    assert candidates[0]["quant_score"] == 75


async def test_upsert_updates_existing(db, token_factory):
    token = token_factory()
    await db.upsert_candidate(token)
    token2 = token_factory(volume_24h_usd=99999.0, quant_score=80)
    await db.upsert_candidate(token2)
    candidates = await db.get_candidates_above_score(0)
    assert len(candidates) == 1
    assert candidates[0]["volume_24h_usd"] == 99999.0


async def test_get_candidates_above_score_filters(db, token_factory):
    await db.upsert_candidate(token_factory(contract_address="0xa", quant_score=50))
    await db.upsert_candidate(token_factory(contract_address="0xb", quant_score=70))
    await db.upsert_candidate(token_factory(contract_address="0xc", quant_score=None))
    results = await db.get_candidates_above_score(60)
    assert len(results) == 1
    assert results[0]["contract_address"] == "0xb"


async def test_log_alert_and_daily_count(db):
    await db.log_alert("0xalert", "solana", 85.0)
    await db.log_alert("0xalert2", "ethereum", 72.0)
    count = await db.get_daily_alert_count()
    assert count == 2


async def test_log_mirofish_job_and_daily_count(db):
    await db.log_mirofish_job("0xjob1")
    await db.log_mirofish_job("0xjob2")
    await db.log_mirofish_job("0xjob3")
    count = await db.get_daily_mirofish_count()
    assert count == 3


async def test_get_recent_alerts(db):
    await db.log_alert("0xrecent", "solana", 90.0)
    alerts = await db.get_recent_alerts(days=30)
    assert len(alerts) == 1
    assert alerts[0]["contract_address"] == "0xrecent"


async def test_signals_fired_persisted(db, token_factory):
    """signals_fired list is stored as JSON and retrievable."""
    token = token_factory(
        quant_score=75,
        signals_fired=["vol_liq_ratio", "holder_growth", "market_cap_range"],
    )
    await db.upsert_candidate(token)
    candidates = await db.get_candidates_above_score(0)
    assert len(candidates) == 1
    import json

    signals = json.loads(candidates[0]["signals_fired"])
    assert signals == ["vol_liq_ratio", "holder_growth", "market_cap_range"]


async def test_signals_fired_none(db, token_factory):
    """signals_fired=None stores as NULL."""
    token = token_factory(quant_score=50)
    await db.upsert_candidate(token)
    candidates = await db.get_candidates_above_score(0)
    assert candidates[0]["signals_fired"] is None


async def test_holder_snapshots(db):
    """Log and retrieve holder count snapshots."""
    await db.log_holder_snapshot("0xtoken", 100)
    await db.log_holder_snapshot("0xtoken", 150)

    prev = await db.get_previous_holder_count("0xtoken")
    assert prev == 150  # most recent

    # Unknown contract returns None
    unknown = await db.get_previous_holder_count("0xunknown")
    assert unknown is None


async def test_score_history(db):
    """Log and retrieve score history (newest first)."""
    await db.log_score("0xtoken", 40.0)
    await db.log_score("0xtoken", 55.0)
    await db.log_score("0xtoken", 70.0)

    scores = await db.get_recent_scores("0xtoken", limit=3)
    assert scores == [70.0, 55.0, 40.0]  # newest first

    # Limit works
    scores_2 = await db.get_recent_scores("0xtoken", limit=2)
    assert len(scores_2) == 2
    assert scores_2 == [70.0, 55.0]

    # Unknown contract returns empty
    empty = await db.get_recent_scores("0xunknown")
    assert empty == []
