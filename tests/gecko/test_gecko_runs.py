from gecko.runs import JobResult, PartialFailure, run_job
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


async def test_run_job_records_partial_failure_counts(settings):
    async def partial(store, session):
        raise PartialFailure(RuntimeError("x"), credits_used=3, rows_written=1)

    assert await run_job("t", settings, partial) == 1
    s = await Store.open(settings.db_path)
    row = await (
        await s.conn.execute("SELECT ok, credits_used, rows_written, error FROM runs")
    ).fetchone()
    await s.close()
    assert row[:3] == (0, 3, 1)
    assert "RuntimeError: x" in row[3]


async def test_job_failed_log_carries_the_type_never_the_message(settings):
    # E1: the message can carry an upstream body or a URL; the runs row keeps it
    from structlog.testing import capture_logs

    async def boom(store, session):
        raise PartialFailure(RuntimeError("https://x/?key=SECRET"), rows_written=1)

    with capture_logs() as logs:
        assert await run_job("t", settings, boom) == 1
    [ev] = [e for e in logs if e["event"] == "job_failed"]
    assert ev["error"] == "RuntimeError" and "SECRET" not in repr(logs)
    s = await Store.open(settings.db_path)
    (err,) = await (await s.conn.execute("SELECT error FROM runs")).fetchone()
    await s.close()
    assert "key=SECRET" in err


def test_partial_failure_message_and_fields():
    cause = ValueError("bad")
    pf = PartialFailure(cause, credits_used=2)
    assert str(pf) == "ValueError: bad"
    assert pf.cause is cause and pf.credits_used == 2 and pf.rows_written == 0
