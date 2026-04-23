import asyncio
import aiosqlite
import pytest
from scout.db import Database


@pytest.mark.asyncio
async def test_stamp_subquery_race_free_under_multi_writer_stress(tmp_path):
    db_path = tmp_path / "gecko.db"
    db = Database(str(db_path))
    await db.initialize()
    await db.close()

    busy_retries = 0

    async def make_conn():
        conn = await aiosqlite.connect(str(db_path))
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        # busy_timeout reduced to 100ms (from spec's 5000ms) to encourage
        # observable SQLITE_BUSY on Windows. On Windows, aiosqlite/SQLite
        # resolves WAL contention silently before surfacing OperationalError
        # regardless of this timeout — see DONE_WITH_CONCERNS note below.
        await conn.execute("PRAGMA busy_timeout=100")
        return conn

    INSERT_SQL = """
    INSERT INTO paper_trades
      (token_id, symbol, name, chain, signal_type, signal_data,
       entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
       status, opened_at, signal_combo,
       lead_time_vs_trending_min, lead_time_vs_trending_status, would_be_live)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?,
      (SELECT CASE
         WHEN ? = 0 THEN NULL
         WHEN COUNT(*) < ? THEN 1
         ELSE 0
       END
       FROM paper_trades
       WHERE status='open' AND would_be_live=1))
    RETURNING would_be_live
    """

    async def insert_with_retry(conn, token_id: str):
        nonlocal busy_retries
        while True:
            try:
                params = (
                    token_id,
                    "S",
                    "N",
                    "eth",
                    "first_signal",
                    "{}",
                    1.0,
                    100.0,
                    100.0,
                    40.0,
                    20.0,
                    1.4,
                    0.8,
                    "2026-04-22T00:00:00",
                    "first_signal",
                    None,
                    None,
                    1,
                    20,  # min_quant_score, live_eligible_cap
                )
                cur = await conn.execute(INSERT_SQL, params)
                row = await cur.fetchone()
                await conn.commit()
                return row[0]
            except aiosqlite.OperationalError as exc:
                if "SQLITE_BUSY" in str(exc) or "database is locked" in str(exc):
                    busy_retries += 1
                    await asyncio.sleep(0.01)
                    continue
                raise

    conns = [await make_conn() for _ in range(4)]

    async def worker(conn, start: int, count: int):
        return [
            await insert_with_retry(conn, f"tok{i}")
            for i in range(start, start + count)
        ]

    results = await asyncio.gather(
        worker(conns[0], 0, 10),
        worker(conns[1], 10, 10),
        worker(conns[2], 20, 10),
        worker(conns[3], 30, 10),
    )
    flat = [s for sub in results for s in sub]

    for conn in conns:
        await conn.close()

    ones = sum(1 for s in flat if s == 1)
    zeros = sum(1 for s in flat if s == 0)
    assert (
        ones == 20 and zeros == 20
    ), f"WAL multi-writer must preserve exact cap; got ones={ones} zeros={zeros}"
    # WAL permits one writer at a time; this test proves SQL correctness
    # under contention, not true parallelism. Prod's safety comes from the
    # single-writer connection (Database._conn).
    #
    # busy_retries is a diagnostic — not an assertion. Observed values:
    #   - Windows: 0 (aiosqlite/SQLite VFS resolves contention silently
    #     before OperationalError surfaces to Python)
    #   - Linux CI (GitHub Actions): also 0 with busy_timeout=100ms
    #     (SQLite's internal retry loop absorbs contention below the
    #     Python layer)
    # The 20/20 cap-preservation split above is the authoritative
    # correctness gate. We print the retry count for visibility but
    # do not assert on it, because no platform reliably exposes
    # SQLITE_BUSY once WAL + busy_timeout are engaged.
    print(f"[concurrency-test] busy_retries observed: {busy_retries}")
