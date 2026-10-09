import json
import pytest


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
