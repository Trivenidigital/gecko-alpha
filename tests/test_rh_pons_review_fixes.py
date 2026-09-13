"""Independent-review fixes for the sustained RH/Pons collector.

Covers the concurrency blocker (collector-owned connection, sibling
transaction interleaving, mid-write cancellation and replay) and ops findings
F1 (throttle memory), F2 (throttle/refusal classification), F6b (enabled
without URL), F7 (chain id per session) and F8 (timeout context). Fixtures
only; no network. No numeric provider quota is assumed anywhere.
"""

import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import aiohttp
import pytest
from aioresponses import CallbackResult, aioresponses

from scout.db import Database
from scout.ingestion import rh_pons
from scout.ingestion.rh_pons import _after_pass, _PassResult, _RpcStats, _ScanState
from tests.test_rh_pons_capture_pacing import _trade
from tests.test_rh_pons_collector import (
    CURVE,
    DEP,
    _buy_log,
    _launch_log,
    _verified_dep,
)
from tests.test_rh_pons_sustained_loop import (
    URL,
    FakeRpc,
    NullCollectorDb,
    _run,
    _settings,
    _use_owned_db,
)

FACTORY = _verified_dep().factory


@pytest.fixture(autouse=True)
def _verified_registry(monkeypatch):
    monkeypatch.setattr(rh_pons, "PONS_DEPLOYMENTS", (_verified_dep(),))


@pytest.fixture
async def db(tmp_path):
    instance = Database(tmp_path / "review.db")
    await instance.initialize()
    yield instance
    await instance.close()


async def _wait_for(predicate, timeout=15):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.01)


async def _stop(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ------------------------------------------- concurrency blocker (owned conn)


async def test_collector_never_commits_sibling_transaction_or_loses_its_writes(
    db, settings_factory
):
    fake = FakeRpc(head=100, factory_logs=[_launch_log(block=100)])
    settings = _settings(settings_factory, RH_PONS_IDLE_SLEEP_SEC=0.1).model_copy(
        update={"RH_PONS_POLL_TIMEOUT_SEC": 1.0}
    )
    results = []
    trade_tx = "0x" + "c5" * 32
    with aioresponses() as m:
        m.post(URL, callback=fake, repeat=True)
        async with aiohttp.ClientSession() as session:
            task = asyncio.create_task(
                rh_pons.run_rh_pons_loop(session, db, settings, on_pass=results.append)
            )
            try:
                await _wait_for(lambda: any(r.status == "completed" for r in results))
                # A chains-tracker-style sibling opens a bare transaction on the
                # SHARED pipeline connection and writes before a slow step.
                await db._conn.execute("BEGIN")
                await db._conn.execute(
                    "INSERT INTO ingest_watchdog_state "
                    "(source, consecutive_misses, updated_at) "
                    "VALUES ('sibling_sentinel', 1, 'x')"
                )
                seen = len(results)
                fake.trade_logs = [_buy_log(block=105, tx=trade_tx, log_index=0)]
                fake.head = 110
                # A pass that STARTED after the sibling's write has finished.
                await _wait_for(
                    lambda: any(r.start_head == 110 for r in results[seen:])
                )
                assert db._conn.in_transaction, "collector committed sibling unit"
                await db._conn.rollback()
                await _wait_for(
                    lambda: any(
                        r.status == "completed" and r.to_block == 110 for r in results
                    )
                )
            finally:
                await _stop(task)
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM ingest_watchdog_state WHERE source='sibling_sentinel'"
    )
    assert (await cur.fetchone())[0] == 0
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM curve_launch_events WHERE transaction_hash=?",
        (trade_tx,),
    )
    assert (await cur.fetchone())[0] == 1
    checkpoint = await db.get_curve_scan_checkpoint(DEP.chain_id, "pons_v2", FACTORY)
    assert checkpoint["next_block"] == 111


async def _snapshot(database):
    cur = await database._conn.execute(
        "SELECT event_name, token_address, curve_address, transaction_hash, "
        "log_index, block_number, block_hash, event_time FROM curve_launch_events "
        "WHERE event_name NOT LIKE 'reorg_%' ORDER BY block_number, log_index"
    )
    events = [tuple(r) for r in await cur.fetchall()]
    cur = await database._conn.execute(
        "SELECT token_address, curve_address, deployer_address, pair_token_address, "
        "launch_config_id, graduation_threshold, lifecycle_status, event_time, "
        "execution_eligible FROM curve_launch_discoveries ORDER BY token_address"
    )
    discoveries = [tuple(r) for r in await cur.fetchall()]
    members = await database.curve_launch_members(DEP.chain_id, "pons_v2", [CURVE])
    checkpoint = await database.get_curve_scan_checkpoint(
        DEP.chain_id, "pons_v2", FACTORY
    )
    return events, discoveries, members, checkpoint["next_block"]


async def test_pass_cancelled_mid_write_rolls_back_then_replays_exactly(
    tmp_path, settings_factory, monkeypatch
):
    logs = dict(
        factory_logs=[_launch_log(block=100)],
        trade_logs=[_buy_log(block=100, log_index=1)],
    )
    settings = _settings(settings_factory, RH_PONS_IDLE_SLEEP_SEC=0.1).model_copy(
        update={"RH_PONS_POLL_TIMEOUT_SEC": 1.0}
    )
    reference = Database(tmp_path / "reference.db")
    await reference.initialize()
    ref_state = _ScanState.for_loop(settings)
    ref = await _run(
        FakeRpc(head=100, **logs),
        lambda s: rh_pons._scan_pass(s, reference, settings, ref_state),
    )
    assert ref.status == "completed", ref.reason

    path = tmp_path / "pipeline.db"
    pipeline = Database(path)
    await pipeline.initialize()
    owned = Database(path)
    await owned.initialize()
    real_commit = owned._conn.commit
    commits = 0

    async def first_commit_never_lands():
        nonlocal commits
        commits += 1
        if commits == 1:
            await asyncio.Event().wait()  # cancelled by the pass deadline
        return await real_commit()

    monkeypatch.setattr(owned._conn, "commit", first_commit_never_lands)
    _use_owned_db(monkeypatch, owned)
    results, durable_after_timeout = [], []

    def observe(result):
        results.append(result)
        if result.status == "timeout":
            # Independent reader: the half-written evidence must NOT be durable
            # (rolled back, not swept in by the attempt-state commit).
            with closing(sqlite3.connect(path)) as reader:
                durable_after_timeout.append(
                    reader.execute(
                        "SELECT COUNT(*) FROM curve_launch_events"
                    ).fetchone()[0]
                )

    with aioresponses() as m:
        m.post(URL, callback=FakeRpc(head=100, **logs), repeat=True)
        async with aiohttp.ClientSession() as session:
            task = asyncio.create_task(
                rh_pons.run_rh_pons_loop(session, pipeline, settings, on_pass=observe)
            )
            try:
                await _wait_for(lambda: any(r.status == "completed" for r in results))
                open_after = owned._conn.in_transaction
            finally:
                await _stop(task)
    assert (results[0].status, results[0].stage) == ("timeout", "write")
    assert durable_after_timeout == [0]
    assert open_after is False
    assert await _snapshot(pipeline) == await _snapshot(reference)
    await pipeline.close()
    await reference.close()


async def test_loop_retries_owned_db_open_records_attempts_and_closes(
    monkeypatch, settings_factory
):
    settings = _settings(
        settings_factory, RH_PONS_IDLE_SLEEP_SEC=0.1, RH_PONS_FAILURE_BACKOFF_MAX_SEC=1
    )
    owned = NullCollectorDb(persisted={"rh_pons_attempt": 4})
    opens = 0

    async def open_db(db, settings_arg):
        nonlocal opens
        opens += 1
        if opens == 1:
            raise OSError("disk unavailable")
        return owned

    async def failing(session, db, settings_arg, state):
        assert db is owned
        return _PassResult(
            "failed", reason="headers_unavailable", start_head=500, stage="headers"
        )

    monkeypatch.setattr(rh_pons, "_open_collector_db", open_db)
    monkeypatch.setattr(rh_pons, "_scan_pass", failing)
    results = []
    task = asyncio.create_task(
        rh_pons.run_rh_pons_loop(None, object(), settings, on_pass=results.append)
    )
    await _wait_for(lambda: len(results) >= 3)
    await _stop(task)
    assert results[0].reason == "collector_db_unavailable"
    assert owned.closed
    # Streak resumes from the persisted count, and each attempt raises the head.
    assert owned.attempts[:2] == [("rh_pons_attempt", 5), ("rh_pons_attempt", 6)]
    assert owned.heads[0][-1] == 500


async def test_enabled_without_rpc_url_logs_once_and_counts_failed_attempts(
    monkeypatch, settings_factory
):
    settings = settings_factory(
        RH_PONS_COLLECTOR_ENABLED=True,
        RH_PONS_IDLE_SLEEP_SEC=0.1,
        RH_PONS_FAILURE_BACKOFF_MAX_SEC=1,
    )
    owned = _use_owned_db(monkeypatch)
    errors = []
    monkeypatch.setattr(
        rh_pons.logger, "error", lambda event, **fields: errors.append(event)
    )
    results = []
    task = asyncio.create_task(
        rh_pons.run_rh_pons_loop(None, object(), settings, on_pass=results.append)
    )
    await _wait_for(lambda: len(results) >= 3)
    await _stop(task)
    assert errors.count("rh_pons_enabled_without_rpc_url") == 1
    assert all(r.reason == "no_rpc_url_configured" for r in results)
    assert [n for _, n in owned.attempts[:3]] == [1, 2, 3]


async def test_timeout_result_keeps_pass_context(monkeypatch, settings_factory):
    settings = _settings(settings_factory, RH_PONS_IDLE_SLEEP_SEC=1).model_copy(
        update={"RH_PONS_POLL_TIMEOUT_SEC": 0.05}
    )
    owned = _use_owned_db(monkeypatch)

    async def stuck(session, db, settings_arg, state):
        state.pass_context = {
            "start_head": 7,
            "from_block": 1,
            "to_block": 5,
            "stage": "headers",
            "header_heights": 40,
        }
        await asyncio.Event().wait()

    async def stop(delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(rh_pons, "_scan_pass", stuck)
    monkeypatch.setattr(rh_pons, "_sleep", stop)
    results = []
    with pytest.raises(asyncio.CancelledError):
        await rh_pons.run_rh_pons_loop(None, object(), settings, on_pass=results.append)
    r = results[0]
    assert (r.status, r.stage, r.from_block, r.to_block, r.start_head) == (
        "timeout",
        "headers",
        1,
        5,
        7,
    )
    assert r.header_heights == 40
    assert owned.closed
    assert owned.attempts == [("rh_pons_attempt", 1)]
    assert owned.heads[0][-1] == 7


async def test_attempt_streak_never_hydrates_into_generic_starvation(
    settings_factory,
):
    from scout import heartbeat

    class PersistedDb:
        async def load_ingest_watchdog_state(self):
            return {"rh_pons_attempt": 99, "coingecko": 2}

    heartbeat._reset_heartbeat_stats()
    try:
        await heartbeat.hydrate_ingest_watchdog_state(PersistedDb(), settings_factory())
        assert "rh_pons_attempt" not in heartbeat._ingest_watchdog_state
        assert "coingecko" in heartbeat._ingest_watchdog_state
    finally:
        heartbeat._reset_heartbeat_stats()


# --------------------------------------------------- F2: throttle/refusal


class ChainFake(FakeRpc):
    def __init__(self, chain_payload, **kw):
        super().__init__(**kw)
        self.chain_payload = chain_payload

    def __call__(self, url, **kw):
        body = kw["json"]
        if isinstance(body, dict) and body["method"] == "eth_chainId":
            self.requests.append(("eth_chainId", []))
            return CallbackResult(
                payload={"jsonrpc": "2.0", "id": 1, **self.chain_payload}
            )
        return super().__call__(url, **kw)


@pytest.mark.parametrize(
    "payload,status,reason,rate_limited",
    [
        ({"error": {"code": -32603}}, "failed", "chain_id_unavailable", False),
        ({"error": {"code": -32005}}, "failed", "chain_id_unavailable", True),
        ({"result": "0x1"}, "refused", "chain_id_mismatch", False),
    ],
)
async def test_chain_id_unavailable_is_failure_mismatch_is_refusal(
    db, settings_factory, payload, status, reason, rate_limited
):
    fake = ChainFake(payload, head=100)
    settings = _settings(settings_factory)
    state = _ScanState.for_loop(settings)
    stats = _RpcStats()
    token = rh_pons._RPC_STATS.set(stats)
    try:
        result = await _run(fake, lambda s: rh_pons._scan_pass(s, db, settings, state))
    finally:
        rh_pons._RPC_STATS.reset(token)
    assert (result.status, result.reason) == (status, reason)
    assert stats.rate_limited is rate_limited
    assert fake.get_logs_queries() == []


async def test_chain_id_verified_once_per_session_and_rechecked_after_failure(
    db, settings_factory, monkeypatch
):
    fake = FakeRpc(head=100)
    settings = _settings(settings_factory)
    state = _ScanState.for_loop(settings)
    for head in (100, 101):
        fake.head = head
        result = await _run(fake, lambda s: rh_pons._scan_pass(s, db, settings, state))
        assert result.status == "completed", result.reason
    assert sum(1 for method, _ in fake.requests if method == "eth_chainId") == 1

    async def failing(session, db_arg, settings_arg, st):
        return _PassResult("failed", reason="headers_unavailable")

    async def no_sleep(delay):
        return None

    monkeypatch.setattr(rh_pons, "_scan_pass", failing)
    monkeypatch.setattr(rh_pons, "_sleep", no_sleep)
    pacer = rh_pons._RpcPacer(rate=8, burst=100, min_rate=1, max_hold=60)
    await rh_pons._run_loop_pass(None, NullCollectorDb(), settings, state, pacer, None)
    assert state.chain_verified is False
    # poll_once never caches: its state is fresh and cache_chain_id is False.
    assert _ScanState.legacy(settings).cache_chain_id is False


async def test_transient_batch_error_object_keeps_batching(settings_factory):
    fake = FakeRpc()
    fake.batch_override = lambda body: CallbackResult(
        payload={"jsonrpc": "2.0", "id": None, "error": {"code": -32603}}
    )
    state = _ScanState.for_loop(_settings(settings_factory))
    assert (
        await _run(fake, lambda s: rh_pons._scan_headers(s, URL, [10, 11], state))
        is None
    )
    assert state.batch_supported is True
    assert fake.single_header_requests() == 0
    fake.batch_override = None
    headers = await _run(fake, lambda s: rh_pons._scan_headers(s, URL, [10, 11], state))
    assert set(headers) == {10, 11}
    assert fake.batch_posts == 2


async def test_http_413_shrinks_batch_and_keeps_batching(settings_factory):
    fake = FakeRpc()
    fake.batch_override = lambda body: (
        CallbackResult(status=413)
        if len(body) > 2
        else CallbackResult(payload=fake.batch_items(body))
    )
    state = _ScanState.for_loop(
        _settings(settings_factory, RH_PONS_HEADER_BATCH_SIZE=8)
    )
    assert state.header_batch_size == 8
    for expected in (4, 2):
        assert (
            await _run(fake, lambda s: rh_pons._scan_headers(s, URL, range(8), state))
            is None
        )
        assert (state.header_batch_size, state.batch_supported) == (expected, True)
    headers = await _run(fake, lambda s: rh_pons._scan_headers(s, URL, range(8), state))
    assert set(headers) == set(range(8))
    assert fake.single_header_requests() == 0


async def test_batch_item_throttle_error_is_rate_limit_with_retry_after(
    settings_factory,
):
    fake = FakeRpc()

    def reply(body):
        items = fake.batch_items(body)
        items[0] = {"jsonrpc": "2.0", "id": 0, "error": {"code": 429}}
        return CallbackResult(payload=items, headers={"Retry-After": "6"})

    fake.batch_override = reply
    state = _ScanState.for_loop(_settings(settings_factory))
    stats = _RpcStats()
    token = rh_pons._RPC_STATS.set(stats)
    try:
        headers = await _run(
            fake, lambda s: rh_pons._scan_headers(s, URL, [10, 11], state)
        )
    finally:
        rh_pons._RPC_STATS.reset(token)
    assert headers is None
    assert (stats.rate_limited, stats.retry_after) == (True, 6.0)
    assert state.batch_supported is True


async def test_http_200_json_rpc_throttle_honours_http_date_retry_after():
    when = datetime.now(timezone.utc) + timedelta(seconds=40)

    def reply(url, **kw):
        return CallbackResult(
            payload={"jsonrpc": "2.0", "id": 1, "error": {"code": -32005}},
            headers={"Retry-After": format_datetime(when, usegmt=True)},
        )

    stats = _RpcStats()
    token = rh_pons._RPC_STATS.set(stats)
    try:
        with aioresponses() as m:
            m.post(URL, callback=reply, repeat=True)
            async with aiohttp.ClientSession() as session:
                result = await rh_pons._rpc(session, URL, "eth_blockNumber", [])
    finally:
        rh_pons._RPC_STATS.reset(token)
    assert result is None
    assert stats.rate_limited is True
    assert 30 < stats.retry_after <= 41
    assert stats.responses == 1


def test_retry_after_parsing_boundaries():
    past = format_datetime(datetime.now(timezone.utc) - timedelta(hours=1), usegmt=True)
    assert rh_pons._retry_after_seconds({"Retry-After": "12"}) == 12.0
    assert rh_pons._retry_after_seconds({"Retry-After": past}) == 0.0
    assert rh_pons._retry_after_seconds({"Retry-After": "garbage"}) is None
    assert rh_pons._retry_after_seconds({"Retry-After": "-5"}) is None
    assert rh_pons._retry_after_seconds({}) is None
    assert rh_pons._retry_after_seconds(None) is None


# ------------------------------------------------------ F1: throttle memory


def test_throttle_is_remembered_so_window_and_batch_do_not_bounce_back(
    settings_factory,
):
    settings = _settings(
        settings_factory,
        RH_PONS_MIN_SCAN_SPAN_BLOCKS=100,
        RH_PONS_BACKFILL_BLOCK_SPAN=2000,
        RH_PONS_HEADER_BATCH_SIZE=50,
    )
    state = _ScanState.for_loop(settings)
    state.span = 800
    throttled = _PassResult("failed", reason="headers_unavailable", rate_limited=True)
    fast = _PassResult("completed", start_head=10_000, to_block=1_000, duration_s=1)
    _after_pass(state, throttled, settings)
    assert (state.span, state.span_ceiling, state.header_batch_size) == (400, 400, 25)
    _after_pass(state, fast, settings)
    # Not back to 800: the ceiling rises additively first.
    assert (state.span, state.span_ceiling, state.header_batch_size) == (500, 500, 26)
    assert state.failures == 0
    _after_pass(state, throttled, settings)
    _after_pass(state, throttled, settings)
    assert state.failures == 2
    _after_pass(state, fast, settings)
    assert state.failures == 1  # decays, does not reset
    for _ in range(60):
        _after_pass(state, fast, settings)
    assert (state.span_ceiling, state.batch_ceiling) == (None, None)
    assert (state.span, state.header_batch_size, state.failures) == (2000, 50, 0)


async def test_throttling_provider_converges_without_repeated_bursts(
    db, settings_factory
):
    trades = [_trade(block, i) for i, block in enumerate(range(101, 400), start=1)]
    fake = FakeRpc(head=400, factory_logs=[_launch_log(block=100)], trade_logs=trades)
    # A synthetic provider that refuses any header batch above 20 calls. This is
    # a fixture shape, not a claim about the real endpoint's quota.
    fake.batch_override = lambda body: (
        CallbackResult(status=429)
        if len(body) > 20
        else CallbackResult(payload=fake.batch_items(body))
    )
    settings = _settings(
        settings_factory, RH_PONS_MAX_HEADERS_PER_PASS=60, RH_PONS_HEADER_BATCH_SIZE=50
    )
    state = _ScanState.for_loop(settings)
    outcomes = []
    for _ in range(80):
        stats = _RpcStats()
        token = rh_pons._RPC_STATS.set(stats)
        try:
            result = await _run(
                fake, lambda s: rh_pons._scan_pass(s, db, settings, state)
            )
        finally:
            rh_pons._RPC_STATS.reset(token)
        result.rate_limited = stats.rate_limited
        _after_pass(state, result, settings)
        outcomes.append((result.status, result.rate_limited))
        if result.status == "completed" and result.to_block == 400:
            break
    assert outcomes[-1][0] == "completed"
    assert sum(1 for _, limited in outcomes if limited) <= 3
    checkpoint = await db.get_curve_scan_checkpoint(DEP.chain_id, "pons_v2", FACTORY)
    assert checkpoint["next_block"] == 401
