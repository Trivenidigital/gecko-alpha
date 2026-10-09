from gecko.runs import run_job, JobResult
from gecko.store import Store


async def test_run_job_records_success(settings):
    async def ok(store, session):
        return JobResult(rows_written=3, credits_used=2)

    assert await run_job("t", settings, ok) == 0
    s = await Store.open(settings.db_path)
    row = await (
        await s.conn.execute("SELECT job, ok, rows_written, credits_used FROM runs")
    ).fetchone()
    await s.close()
    assert row == ("t", 1, 3, 2)


async def test_run_job_records_failure_and_exits_1(settings):
    async def boom(store, session):
        raise RuntimeError("upstream 401")

    assert await run_job("t", settings, boom) == 1
    s = await Store.open(settings.db_path)
    row = await (await s.conn.execute("SELECT ok, error FROM runs")).fetchone()
    await s.close()
    assert row[0] == 0 and "upstream 401" in row[1]
