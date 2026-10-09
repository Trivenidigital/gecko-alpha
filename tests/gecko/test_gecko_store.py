import json
import pytest

from gecko.store import ReturnAlreadyMeasured, canon_ts


async def test_schema_has_seven_tables(store):
    rows = await (
        await store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ).fetchall()
    assert {r[0] for r in rows} == {
        "tokens",
        "observations",
        "source_calls",
        "watchlist",
        "forward_returns",
        "research_notes",
        "runs",
    }


async def test_upsert_token_is_idempotent_and_fills_nulls(store):
    await store.upsert_token(
        "solana:abc",
        "solana",
        "ABC",
        "ABC",
        "Abc",
        "2026-10-01T00:00:00Z",
        "dexscreener",
    )
    await store.upsert_token(
        "solana:abc",
        "solana",
        "ABC",
        "ABC2",
        "Abc2",
        "2026-10-02T00:00:00Z",
        "geckoterminal",
        cg_id="abc-coin",
    )
    row = await (
        await store.conn.execute("SELECT symbol, first_source, cg_id FROM tokens")
    ).fetchone()
    assert row == ("ABC", "dexscreener", "abc-coin")


async def test_observations_respect_as_of(store):
    await store.upsert_token(
        "solana:abc",
        "solana",
        "ABC",
        "ABC",
        "Abc",
        "2026-10-01T00:00:00Z",
        "dexscreener",
    )
    await store.add_observation(
        "solana:abc", "dexscreener", "2026-10-01T00:00:00Z", {"price": 1.0}
    )
    await store.add_observation(
        "solana:abc", "dexscreener", "2026-10-03T00:00:00Z", {"price": 9.0}
    )
    obs = await store.observations("solana:abc", as_of="2026-10-02T00:00:00Z")
    assert [o.features["price"] for o in obs] == [1.0]


async def test_watchlist_entry_row_is_immutable(store):
    await store.upsert_token(
        "solana:abc",
        "solana",
        "ABC",
        "ABC",
        "Abc",
        "2026-10-01T00:00:00Z",
        "dexscreener",
    )
    await store.watchlist_upsert(
        "solana:abc", "2026-10-01T00:00:00Z", "F4", 1, 2.5, {"age_days": 10}
    )
    await store.watchlist_upsert(
        "solana:abc", "2026-10-05T00:00:00Z", "F4", 3, 1.0, {"age_days": 14}
    )
    row = await (
        await store.conn.execute(
            "SELECT entered_at, rank, score, features_at_entry_json FROM watchlist"
        )
    ).fetchone()
    assert row[0] == "2026-10-01T00:00:00Z" and row[1] == 3 and row[2] == 1.0
    assert json.loads(row[3]) == {"age_days": 10}


async def test_schedule_and_due_returns(store):
    await store.upsert_token(
        "solana:abc",
        "solana",
        "ABC",
        "ABC",
        "Abc",
        "2026-10-01T00:00:00Z",
        "dexscreener",
    )
    await store.schedule_returns("solana:abc", "2026-10-01T00:00:00Z")
    due = await store.due_returns("2026-10-09T00:00:00Z")
    assert [r.horizon_days for r in due] == [1, 7]
    await store.set_return(
        "solana:abc", 1, 1.0, 1.5, 60000.0, 61000.0, "2026-10-02T01:00:00Z"
    )
    due = await store.due_returns("2026-10-09T00:00:00Z")
    assert [r.horizon_days for r in due] == [7]


async def test_last_write(store):
    assert await store.last_write("observations") is None
    await store.record_run("x", "2026-10-01T00:00:00Z", "2026-10-01T00:00:05Z", True)
    assert await store.last_write("runs") == "2026-10-01T00:00:05Z"


async def _token_with_returns(store):
    await store.upsert_token(
        "solana:abc",
        "solana",
        "ABC",
        "ABC",
        "Abc",
        "2026-10-01T00:00:00Z",
        "dexscreener",
    )
    await store.schedule_returns("solana:abc", "2026-10-01T00:00:00Z")


async def test_set_return_arithmetic(store):
    await _token_with_returns(store)
    await store.set_return(
        "solana:abc", 1, 1.0, 1.5, 60000.0, 61000.0, "2026-10-02T01:00:00Z"
    )
    ret, ret_vs_btc, measured_at = await (
        await store.conn.execute(
            "SELECT ret, ret_vs_btc, measured_at FROM forward_returns "
            "WHERE horizon_days=1"
        )
    ).fetchone()
    assert ret == pytest.approx(0.5)
    assert ret_vs_btc == pytest.approx(0.5 - (61000 / 60000 - 1))
    assert measured_at == "2026-10-02T01:00:00Z"


async def test_set_return_raises_on_missing_btc_and_leaves_row_due(store):
    await _token_with_returns(store)
    with pytest.raises(ValueError, match="required") as ei:
        await store.set_return(
            "solana:abc", 1, 1.0, 1.5, 60000.0, None, "2026-10-02T01:00:00Z"
        )
    assert not isinstance(ei.value, ReturnAlreadyMeasured)
    due = await store.due_returns("2026-10-09T00:00:00Z")
    assert [r.horizon_days for r in due] == [1, 7]


async def test_set_return_is_write_once(store):
    await _token_with_returns(store)
    await store.set_return(
        "solana:abc", 1, 1.0, 1.5, 60000.0, 61000.0, "2026-10-02T01:00:00Z"
    )
    with pytest.raises(ReturnAlreadyMeasured, match="already measured"):
        await store.set_return(
            "solana:abc", 1, 1.0, 9.0, 60000.0, 61000.0, "2026-10-03T01:00:00Z"
        )
    row = await (
        await store.conn.execute(
            "SELECT price, measured_at FROM forward_returns WHERE horizon_days=1"
        )
    ).fetchone()
    assert row == (1.5, "2026-10-02T01:00:00Z")


async def test_set_return_missing_row_raises(store):
    await _token_with_returns(store)
    with pytest.raises(ReturnAlreadyMeasured, match="row missing"):
        await store.set_return(
            "solana:abc", 2, 1.0, 1.5, 60000.0, 61000.0, "2026-10-02T01:00:00Z"
        )


async def test_set_return_zero_entry_price_finalizes_with_null_ret(store):
    await _token_with_returns(store)
    await store.set_return(
        "solana:abc", 1, 0.0, 1.5, 60000.0, 61000.0, "2026-10-02T01:00:00Z"
    )
    row = await (
        await store.conn.execute(
            "SELECT ret, ret_vs_btc, measured_at FROM forward_returns "
            "WHERE horizon_days=1"
        )
    ).fetchone()
    assert row == (None, None, "2026-10-02T01:00:00Z")
    due = await store.due_returns("2026-10-09T00:00:00Z")
    assert [r.horizon_days for r in due] == [7]


def test_canon_ts_forms():
    assert canon_ts(None) is None
    assert canon_ts("2026-10-02T00:00:00Z") == "2026-10-02T00:00:00Z"
    assert canon_ts("2026-10-02T00:00:00+00:00") == "2026-10-02T00:00:00Z"
    assert canon_ts("2026-10-01T22:00:00-02:00") == "2026-10-02T00:00:00Z"
    assert canon_ts("2026-10-02T00:00:01.500Z") == "2026-10-02T00:00:01Z"
    assert canon_ts("2026-10-02T00:00:00") == "2026-10-02T00:00:00Z"


async def test_observations_as_of_with_mixed_iso_formats(store):
    await store.upsert_token(
        "solana:abc",
        "solana",
        "ABC",
        "ABC",
        "Abc",
        "2026-10-01T00:00:00Z",
        "dexscreener",
    )
    await store.add_observation(
        "solana:abc", "dexscreener", "2026-10-02T00:00:01.500Z", {"n": "late"}
    )
    await store.add_observation(
        "solana:abc", "dexscreener", "2026-10-02T00:00:00+00:00", {"n": "utc"}
    )
    await store.add_observation(
        "solana:abc", "geckoterminal", "2026-10-01T22:00:00-02:00", {"n": "offset"}
    )
    obs = await store.observations("solana:abc", as_of="2026-10-02T00:00:00Z")
    assert sorted(o.features["n"] for o in obs) == ["offset", "utc"]
    stored = await (
        await store.conn.execute("SELECT observed_at FROM observations ORDER BY id")
    ).fetchall()
    assert [r[0] for r in stored] == [
        "2026-10-02T00:00:01Z",
        "2026-10-02T00:00:00Z",
        "2026-10-02T00:00:00Z",
    ]
    # an offset-form as_of is canonicalized too
    obs = await store.observations("solana:abc", as_of="2026-10-01T21:00:00-03:00")
    assert sorted(o.features["n"] for o in obs) == ["offset", "utc"]


async def test_stored_timestamps_are_canonical(store):
    await store.upsert_token(
        "solana:abc",
        "solana",
        "ABC",
        "ABC",
        "Abc",
        "2026-10-01T00:00:00.250+00:00",
        "dexscreener",
        pool_created_at="2026-09-30T20:00:00-04:00",
    )
    await store.record_run(
        "x", "2026-10-01T00:00:00+00:00", "2026-10-01T02:00:05+02:00", True
    )
    await store.watchlist_upsert(
        "solana:abc", "2026-10-01T00:00:00.9+00:00", "F4", 1, 2.5, {}
    )
    await store.watchlist_exit("solana:abc", "2026-10-03T00:00:00+00:00", "x")
    await store.schedule_returns("solana:abc", "2026-10-01T00:00:00+00:00")
    tok = await (
        await store.conn.execute("SELECT first_seen_at, pool_created_at FROM tokens")
    ).fetchone()
    assert tok == ("2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z")
    run = await (
        await store.conn.execute("SELECT started_at, finished_at FROM runs")
    ).fetchone()
    assert run == ("2026-10-01T00:00:00Z", "2026-10-01T00:00:05Z")
    wl = await (
        await store.conn.execute(
            "SELECT entered_at, last_ranked_at, exited_at FROM watchlist"
        )
    ).fetchone()
    assert wl == (
        "2026-10-01T00:00:00Z",
        "2026-10-01T00:00:00Z",
        "2026-10-03T00:00:00Z",
    )
    due = await store.due_returns("2026-10-02T02:00:00+02:00")
    assert [(r.horizon_days, r.due_at) for r in due] == [(1, "2026-10-02T00:00:00Z")]


async def test_observations_source_filter(store):
    await store.upsert_token(
        "solana:abc",
        "solana",
        "ABC",
        "ABC",
        "Abc",
        "2026-10-01T00:00:00Z",
        "dexscreener",
    )
    await store.add_observation("solana:abc", "dexscreener", "2026-10-01T00:00:00Z", {})
    await store.add_observation(
        "solana:abc", "geckoterminal", "2026-10-01T00:00:00Z", {}
    )
    obs = await store.observations(
        "solana:abc", as_of="2026-10-02T00:00:00Z", source="geckoterminal"
    )
    assert [o.source for o in obs] == ["geckoterminal"]


async def test_journal_mode_is_wal(store):
    row = await (await store.conn.execute("PRAGMA journal_mode")).fetchone()
    assert row[0] == "wal"


async def test_source_calls_has_caller_time_index(store):
    rows = await (
        await store.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type=? AND name=?",
            ("index", "idx_calls_source_time"),
        )
    ).fetchall()
    assert len(rows) == 1
    assert "source_calls(source_id, posted_at)" in rows[0][0]


async def test_upsert_token_keeps_earliest_pool_created_at(store):
    async def put(pool):
        await store.upsert_token(
            "solana:a",
            "solana",
            "a",
            "A",
            "A",
            "2026-10-01T00:00:00Z",
            "x",
            pool_created_at=pool,
        )
        row = await (
            await store.conn.execute("SELECT pool_created_at FROM tokens")
        ).fetchone()
        return row[0]

    assert await put(None) is None
    assert await put("2026-09-10T00:00:00Z") == "2026-09-10T00:00:00Z"
    assert await put("2026-09-01T00:00:00+00:00") == "2026-09-01T00:00:00Z"
    assert await put("2026-09-20T00:00:00Z") == "2026-09-01T00:00:00Z"
    assert await put(None) == "2026-09-01T00:00:00Z"


async def test_set_entry_price_is_write_once(store):
    await store.watchlist_upsert("solana:a", "2026-10-01T00:00:00Z", "F4", 1, 1.0, {})
    await store.set_entry_price("solana:a", 1.5)
    await store.set_entry_price("solana:a", 9.0)
    row = await (
        await store.conn.execute("SELECT entry_price FROM watchlist")
    ).fetchone()
    assert row == (1.5,)


OLD_WATCHLIST = (
    "CREATE TABLE watchlist (token_id TEXT PRIMARY KEY, entered_at TEXT NOT NULL, "
    "rule_id TEXT NOT NULL, rank INTEGER NOT NULL, score REAL NOT NULL, "
    "features_at_entry_json TEXT NOT NULL, last_ranked_at TEXT NOT NULL, "
    "exited_at TEXT, exit_reason TEXT)"
)


async def test_open_adds_entry_price_to_an_old_watchlist(tmp_path):
    import aiosqlite

    from gecko.store import Store

    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as c:
        await c.execute(OLD_WATCHLIST)
        await c.execute(
            "INSERT INTO watchlist VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "s:a",
                "2026-10-01T00:00:00Z",
                "F4",
                1,
                1.0,
                "{}",
                "2026-10-01",
                None,
                None,
            ),
        )
        await c.commit()
    for _ in range(2):  # idempotent
        s = await Store.open(path)
        info = await (await s.conn.execute("PRAGMA table_info(watchlist)")).fetchall()
        assert [r[1] for r in info].count("entry_price") == 1
        await s.set_entry_price("s:a", 2.0)
        row = await (
            await s.conn.execute("SELECT entry_price FROM watchlist")
        ).fetchone()
        assert row == (2.0,)
        await s.close()


async def test_set_return_refusal_leaves_no_open_transaction(store):
    await store.schedule_returns("solana:a", "2026-10-01T00:00:00Z")
    await store.set_return("solana:a", 1, 1.0, 2.0, 1.0, 1.0, "2026-10-02T00:00:00Z")
    with pytest.raises(ReturnAlreadyMeasured):
        await store.set_return(
            "solana:a", 1, 1.0, 3.0, 1.0, 1.0, "2026-10-02T01:00:00Z"
        )
    assert store.conn.in_transaction is False


async def test_open_sets_busy_timeout_and_tolerates_a_racing_alter(
    tmp_path, monkeypatch
):
    import sqlite3

    import aiosqlite

    from gecko.store import Store

    path = str(tmp_path / "race.db")
    async with aiosqlite.connect(path) as c:
        await c.execute(OLD_WATCHLIST)
        await c.commit()
    real_execute = aiosqlite.Connection.execute

    async def racing(self, sql, *a, **k):
        if sql.startswith("ALTER TABLE watchlist"):
            await real_execute(self, sql, *a, **k)  # another opener won the race
            raise sqlite3.OperationalError("duplicate column name: entry_price")
        return await real_execute(self, sql, *a, **k)

    monkeypatch.setattr(aiosqlite.Connection, "execute", racing)
    s = await Store.open(path)
    try:
        row = await (await s.conn.execute("PRAGMA busy_timeout")).fetchone()
        assert row == (30000,)
        info = await (
            await s.conn.execute("PRAGMA table_info(forward_returns)")
        ).fetchall()
        assert "last_attempt_at" in {r[1] for r in info}
    finally:
        await s.close()


async def test_open_reraises_other_alter_errors(tmp_path, monkeypatch):
    import sqlite3

    import aiosqlite

    from gecko.store import Store

    path = str(tmp_path / "bad.db")
    async with aiosqlite.connect(path) as c:
        await c.execute(OLD_WATCHLIST)
        await c.commit()
    real_execute = aiosqlite.Connection.execute

    async def broken(self, sql, *a, **k):
        if sql.startswith("ALTER TABLE"):
            raise sqlite3.OperationalError("database is locked")
        return await real_execute(self, sql, *a, **k)

    monkeypatch.setattr(aiosqlite.Connection, "execute", broken)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        await Store.open(path)


async def test_revive_candidate_only_touches_stale_candidates(store):
    for tid, reason in (("s:a", "stale_candidate"), ("s:b", "liquidity")):
        await store.upsert_token(
            tid, "solana", tid, "A", "A", "2026-09-01T00:00:00Z", "x"
        )
        await store.set_token_status(tid, "disqualified", reason)
    assert await store.revive_candidate("s:a", "2026-10-09T00:00:00Z") is True
    assert await store.revive_candidate("s:b", "2026-10-09T00:00:00Z") is False
    rows = await (
        await store.conn.execute(
            "SELECT token_id, status, disqualify_reason, first_seen_at FROM tokens "
            "ORDER BY token_id"
        )
    ).fetchall()
    assert rows == [
        ("s:a", "candidate", None, "2026-10-09T00:00:00Z"),
        ("s:b", "disqualified", "liquidity", "2026-09-01T00:00:00Z"),
    ]


async def test_open_adds_last_attempt_at_to_an_old_forward_returns(tmp_path):
    import aiosqlite

    from gecko.store import Store

    path = str(tmp_path / "old_fr.db")
    async with aiosqlite.connect(path) as c:
        await c.execute(
            "CREATE TABLE forward_returns (token_id TEXT NOT NULL, "
            "horizon_days INTEGER NOT NULL, due_at TEXT NOT NULL, entry_price REAL, "
            "price REAL, btc_entry REAL, btc_price REAL, ret REAL, ret_vs_btc REAL, "
            "attempts INTEGER NOT NULL DEFAULT 0, measured_at TEXT, "
            "PRIMARY KEY (token_id, horizon_days))"
        )
        await c.commit()
    s = await Store.open(path)
    try:
        await s.schedule_returns("s:a", "2026-10-01T00:00:00Z", horizons=(1,))
        await s.bump_return_attempt("s:a", 1, attempted_at="2026-10-02T00:00:00Z")
        (due,) = await s.due_returns("2026-10-03T00:00:00Z")
        assert due.last_attempt_at == "2026-10-02T00:00:00Z"
    finally:
        await s.close()


async def test_failed_open_closes_its_connection(tmp_path, monkeypatch):
    import sqlite3

    import aiosqlite

    from gecko.store import Store

    path = str(tmp_path / "closed.db")
    async with aiosqlite.connect(path) as c:
        await c.execute(OLD_WATCHLIST)
        await c.commit()
    real_execute, real_close = aiosqlite.Connection.execute, aiosqlite.Connection.close
    closed = []

    async def broken(self, sql, *a, **k):
        if sql.startswith("ALTER TABLE"):
            raise sqlite3.OperationalError("database is locked")
        return await real_execute(self, sql, *a, **k)

    async def spy_close(self):
        closed.append(True)
        await real_close(self)

    monkeypatch.setattr(aiosqlite.Connection, "execute", broken)
    monkeypatch.setattr(aiosqlite.Connection, "close", spy_close)
    with pytest.raises(sqlite3.OperationalError):
        await Store.open(path)
    assert closed == [True]
