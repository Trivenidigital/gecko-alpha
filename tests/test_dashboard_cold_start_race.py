"""Cold-start race on the dashboard's cached ScoutDatabase singleton.

``_get_scout_db`` published the instance into the module global BEFORE awaiting
``initialize()``. Any request that arrived inside that await window took the
"already cached" branch and got a Database whose ``_conn`` is still None. The
handlers query through ``sdb._conn`` directly, so that request died on
``AttributeError: 'NoneType' object has no attribute 'execute'`` and FastAPI
returned HTTP 500. Single-request smoke tests never see it:
the window only exists while the FIRST caller is still initializing, i.e. on the
very first request after a dashboard restart, and only when a second request
overlaps it.
"""

from __future__ import annotations

import asyncio

from dashboard import api as dashboard_api


async def test_concurrent_cold_start_never_returns_an_uninitialized_db(
    tmp_path, monkeypatch
):
    """Two callers racing the cold start must both get a USABLE handle.

    The handle has to be exercised at the moment it is returned, not after
    ``gather`` settles: both callers share one instance, so by the time the
    slow initializer finishes, a handle that was unusable when it was handed
    out looks perfectly healthy. That is exactly why the defect survived — it
    is invisible to any assertion made after the fact.

    The slow ``initialize`` makes the window deterministic rather than
    timing-dependent: caller B is guaranteed to arrive while caller A is still
    inside it.
    """
    from scout.db import Database as ScoutDatabase

    monkeypatch.setattr(dashboard_api, "_scout_db", None, raising=False)

    real_initialize = ScoutDatabase.initialize
    initialize_calls: list[int] = []

    async def _slow_initialize(self, **kwargs):
        initialize_calls.append(1)
        # Hand control back to the loop so the racing caller runs while this
        # instance is constructed-but-not-initialized.
        await asyncio.sleep(0.05)
        await real_initialize(self, **kwargs)

    monkeypatch.setattr(ScoutDatabase, "initialize", _slow_initialize)

    db_path = str(tmp_path / "scout.db")
    handles: list[ScoutDatabase] = []

    async def _endpoint_call():
        """What every `/api/...` handler does: take the handle and immediately
        query through ``sdb._conn``. Pre-fix the racer got ``_conn is None``,
        so this raised AttributeError and FastAPI returned HTTP 500."""
        sdb = await dashboard_api._get_scout_db(db_path)
        handles.append(sdb)
        cur = await sdb._conn.execute("SELECT 1")
        return (await cur.fetchone())[0]

    results = await asyncio.gather(
        _endpoint_call(), _endpoint_call(), return_exceptions=True
    )

    try:
        failures = [r for r in results if isinstance(r, BaseException)]
        assert not failures, (
            "a concurrent cold-start request was handed an uninitialized "
            f"Database and blew up mid-query: {failures!r}"
        )
        assert results == [1, 1], results
        assert (
            handles[0] is handles[1]
        ), "both callers must share the one cached instance"
        # A second initialize would open a second connection and leak the
        # first, so the guard has to be a lock, not just a reordered assignment.
        assert (
            len(initialize_calls) == 1
        ), f"initialize() must run exactly once, ran {len(initialize_calls)}x"
    finally:
        for handle in set(handles):
            await handle.close()


async def test_cached_instance_is_reused_without_re_initializing(tmp_path, monkeypatch):
    """The fast path still has to be fast — the lock must not serialize every
    request behind an already-warm singleton."""
    from scout.db import Database as ScoutDatabase

    monkeypatch.setattr(dashboard_api, "_scout_db", None, raising=False)

    real_initialize = ScoutDatabase.initialize
    initialize_calls: list[int] = []

    async def _counting_initialize(self, **kwargs):
        initialize_calls.append(1)
        await real_initialize(self, **kwargs)

    monkeypatch.setattr(ScoutDatabase, "initialize", _counting_initialize)

    db_path = str(tmp_path / "scout.db")
    first = await dashboard_api._get_scout_db(db_path)
    try:
        for _ in range(3):
            assert await dashboard_api._get_scout_db(db_path) is first
        assert len(initialize_calls) == 1
    finally:
        await first.close()
