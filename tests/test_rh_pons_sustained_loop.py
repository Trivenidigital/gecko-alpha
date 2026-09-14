"""Sustained RH/Pons capture: strict batch headers, topic-only trades, loop control.

Synthetic, provenance-tagged fixtures only; nothing here is a live check.
"""

import asyncio
import json
import random
import time

import aiohttp
import pytest
from aioresponses import CallbackResult, aioresponses

from scout.db import Database
from scout.ingestion import rh_pons
from scout.ingestion.rh_pons import (
    TOPIC_CURVE_BUY,
    _after_pass,
    _BatchUnsupported,
    _PassResult,
    _RpcStats,
    _ScanState,
)
from tests.test_rh_pons_collector import (
    BUYER,
    CURVE,
    DEP,
    TOKEN,
    _buy_log,
    _graduated_log,
    _launch_log,
    _log,
    _verified_dep,
    _word_addr,
    _word_int,
)

URL = "https://rpc.invalid/key/SECRET"
FOREIGN = "0x" + "98" * 20
MIMIC = "0x" + "99" * 20
CURVE2 = "0x" + "66" * 20
TOKEN2 = "0x" + "77" * 20


class FakeRpc:
    """JSON-RPC fixture accepting single and batch POSTs."""

    def __init__(self, *, head=100, factory_logs=(), trade_logs=(), hash_for=None):
        self.head = head
        self.factory_logs = list(factory_logs)
        self.trade_logs = list(trade_logs)
        self.hash_for = hash_for or (lambda height: "0x" + "bb" * 32)
        self.requests = []
        self.single_posts = 0
        self.batch_posts = 0
        self.batch_override = None
        self.log_error = False

    def result(self, body):
        method, params = body["method"], body["params"]
        self.requests.append((method, params))
        if method == "eth_chainId":
            return hex(DEP.chain_id)
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getBlockByNumber":
            height = int(params[0], 16)
            return {
                "number": hex(height),
                "hash": self.hash_for(height),
                "timestamp": hex(1700000000),
            }
        query = params[0]
        low, high = int(query["fromBlock"], 16), int(query["toBlock"], 16)

        def in_range(logs):
            return [x for x in logs if low <= int(x["blockNumber"], 16) <= high]

        if isinstance(query.get("address"), str):
            return in_range(self.factory_logs)
        if "address" in query:
            return [
                x for x in in_range(self.trade_logs) if x["address"] in query["address"]
            ]
        return in_range(self.trade_logs)

    def batch_items(self, body):
        return [
            {"jsonrpc": "2.0", "id": item["id"], "result": self.result(item)}
            for item in body
        ]

    def __call__(self, url, **kw):
        body = kw["json"]
        if isinstance(body, list):
            self.batch_posts += 1
            if self.batch_override is not None:
                return self.batch_override(body)
            return CallbackResult(payload=self.batch_items(body))
        self.single_posts += 1
        if self.log_error and body["method"] == "eth_getLogs":
            return CallbackResult(payload={"jsonrpc": "2.0", "id": 1, "error": {}})
        return CallbackResult(
            payload={"jsonrpc": "2.0", "id": 1, "result": self.result(body)}
        )

    def get_logs_queries(self):
        return [
            params[0] for method, params in self.requests if method == "eth_getLogs"
        ]

    def single_header_requests(self):
        return self.single_posts - sum(
            1 for method, _ in self.requests if method != "eth_getBlockByNumber"
        )


@pytest.fixture(autouse=True)
def _verified_registry(monkeypatch):
    monkeypatch.setattr(rh_pons, "PONS_DEPLOYMENTS", (_verified_dep(),))
    rh_pons._poll_cycle_counter = 0
    yield
    rh_pons._poll_cycle_counter = 0


@pytest.fixture
async def db(tmp_path):
    instance = Database(tmp_path / "loop.db")
    await instance.initialize()
    yield instance
    await instance.close()


def _settings(settings_factory, **overrides):
    values = dict(RH_PONS_COLLECTOR_ENABLED=True, RH_PONS_RPC_URL=URL)
    values.update(overrides)
    return settings_factory(**values)


async def _run(fake, coro_factory):
    with aioresponses() as m:
        m.post(URL, callback=fake, repeat=True)
        async with aiohttp.ClientSession() as session:
            return await coro_factory(session)


async def _discovery(db, token, curve, status="on_curve"):
    await db.record_curve_launch_discovery(
        chain_id=DEP.chain_id,
        network=DEP.network,
        protocol=DEP.version,
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
        provenance="fixture:source_derived",
        execution_eligible=False,
        eligibility_reasons=[],
    )


# ------------------------------------------------------- strict batch headers


async def test_batch_headers_unique_normalized_and_keyed_by_id():
    fake = FakeRpc(hash_for=lambda height: "0x" + "AB" * 32)

    def reversed_reply(body):
        return CallbackResult(payload=list(reversed(fake.batch_items(body))))

    fake.batch_override = reversed_reply
    headers = await _run(
        fake,
        lambda s: rh_pons._fetch_block_headers_batched(s, URL, [5, 3, 5, 4, 6], 2),
    )
    assert set(headers) == {3, 4, 5, 6}
    assert all(h["hash"] == "0x" + "ab" * 32 for h in headers.values())
    assert fake.batch_posts == 2
    assert fake.single_posts == 0


def _mutate_first(fn):
    def apply(items):
        fn(items)
        return items

    return apply


MALFORMED = {
    "duplicate_id": _mutate_first(lambda items: items[1].update(id=items[0]["id"])),
    # Right length, every item individually valid, but id 1 never answered:
    # only the duplicate-id guard can refuse this (height checks all pass).
    "duplicate_id_valid_result": _mutate_first(
        lambda items: items.__setitem__(1, json.loads(json.dumps(items[0])))
    ),
    # A valid header result must not launder a per-item error.
    "result_and_error": _mutate_first(
        lambda items: items[0].update(error={"code": -32000})
    ),
    "bool_id": _mutate_first(lambda items: items[1].update(id=True)),
    "string_id": _mutate_first(lambda items: items[0].update(id="0")),
    "unknown_id": _mutate_first(lambda items: items[0].update(id=99)),
    "missing_item": _mutate_first(lambda items: items.pop()),
    "extra_item": _mutate_first(lambda items: items.append(dict(items[0], id=2))),
    "item_error": _mutate_first(
        lambda items: items.__setitem__(
            0, {"jsonrpc": "2.0", "id": 0, "error": {"code": -32000}}
        )
    ),
    "missing_result": _mutate_first(lambda items: items[0].pop("result")),
    "wrong_height": _mutate_first(
        lambda items: items[0]["result"].update(number="0x3e7")
    ),
    "bad_hash": _mutate_first(lambda items: items[0]["result"].update(hash="0x1234")),
    "zero_timestamp": _mutate_first(
        lambda items: items[0]["result"].update(timestamp="0x0")
    ),
    "int_timestamp": _mutate_first(
        lambda items: items[0]["result"].update(timestamp=1700000000)
    ),
    "not_list": lambda items: {"jsonrpc": "2.0", "id": 0, "result": items},
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
async def test_malformed_batch_fails_closed_without_downgrade(settings_factory, name):
    fake = FakeRpc()
    fake.batch_override = lambda body: CallbackResult(
        payload=MALFORMED[name](fake.batch_items(body))
    )
    state = _ScanState.for_loop(_settings(settings_factory))
    state.header_batch_size = 10
    headers = await _run(fake, lambda s: rh_pons._scan_headers(s, URL, [10, 11], state))
    assert headers is None
    assert state.batch_supported is True
    assert fake.single_header_requests() == 0


@pytest.mark.parametrize(
    "reply",
    [
        CallbackResult(status=405),
        CallbackResult(status=400),
        CallbackResult(
            payload={"jsonrpc": "2.0", "id": None, "error": {"code": -32600}}
        ),
    ],
)
async def test_batch_refusal_falls_back_to_bounded_single_requests(
    settings_factory, reply
):
    fake = FakeRpc()
    fake.batch_override = lambda body: reply
    state = _ScanState.for_loop(_settings(settings_factory))
    headers = await _run(
        fake, lambda s: rh_pons._scan_headers(s, URL, [10, 11, 12], state)
    )
    assert set(headers) == {10, 11, 12}
    assert state.batch_supported is False
    assert fake.batch_posts == 1
    assert fake.single_header_requests() == 3
    # Later passes use the fallback directly.
    await _run(fake, lambda s: rh_pons._scan_headers(s, URL, [13], state))
    assert fake.batch_posts == 1


@pytest.mark.parametrize(
    "reply,retry_after",
    [
        (CallbackResult(status=429, headers={"Retry-After": "7"}), 7.0),
        (
            CallbackResult(
                payload={"jsonrpc": "2.0", "id": None, "error": {"code": -32005}}
            ),
            None,
        ),
    ],
)
async def test_batch_throttling_is_transient_not_refusal(
    settings_factory, reply, retry_after
):
    fake = FakeRpc()
    fake.batch_override = lambda body: reply
    state = _ScanState.for_loop(_settings(settings_factory))
    stats = _RpcStats()
    token = rh_pons._RPC_STATS.set(stats)
    try:
        headers = await _run(fake, lambda s: rh_pons._scan_headers(s, URL, [10], state))
    finally:
        rh_pons._RPC_STATS.reset(token)
    assert headers is None
    assert state.batch_supported is True
    assert stats.rate_limited is True
    assert stats.retry_after == retry_after
    assert stats.calls == 1


async def test_malformed_batch_wins_over_refusal_in_same_round(monkeypatch):
    async def post(session, url, heights):
        if heights[0] == 0:
            raise _BatchUnsupported("http_405")
        return None

    monkeypatch.setattr(rh_pons, "_post_header_batch", post)
    assert await rh_pons._fetch_block_headers_batched(None, URL, range(4), 2) is None


async def test_batch_posts_in_flight_are_bounded(monkeypatch):
    active = peak = 0

    async def post(session, url, heights):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        return {h: {"hash": "0x" + "bb" * 32} for h in heights}

    monkeypatch.setattr(rh_pons, "_post_header_batch", post)
    headers = await rh_pons._fetch_block_headers_batched(None, URL, range(70), 10)
    assert set(headers) == set(range(70))
    assert peak == 2


# --------------------------------------------------------- topic-only trades


def _mimic_log(**kw):
    # Same topic0, different indexing: undecodable if it were ever trusted.
    return _log(
        address=MIMIC,
        topics=[TOPIC_CURVE_BUY, "0x" + _word_addr(BUYER)],
        data="0x" + _word_int(1),
        **kw,
    )


async def test_topic_only_pass_excludes_foreign_emitters_and_bounds_queries(
    db, settings_factory
):
    rows = [
        (
            DEP.chain_id,
            DEP.network,
            DEP.version,
            f"0x{i:040x}",
            f"0x{i + 10**6:040x}",
            "on_curve",
            "2026-09-13T00:00:00+00:00",
            "fixture",
            "fixture",
        )
        for i in range(1, 501)
    ]
    await db._conn.executemany(
        """INSERT INTO curve_launch_discoveries (chain_id, network, protocol,
        token_address, curve_address, lifecycle_status, first_seen_at, source,
        provenance) VALUES (?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    await db._conn.commit()
    await _discovery(db, TOKEN2, CURVE2)
    fake = FakeRpc(
        head=100,
        factory_logs=[_launch_log(block=100)],
        trade_logs=[
            _buy_log(block=100, log_index=1),
            _log(
                address=CURVE2,
                topics=_buy_log()["topics"],
                data=_buy_log()["data"],
                tx="0x" + "c2" * 32,
                block=99,
            ),
            _mimic_log(tx="0x" + "c3" * 32, block=98),
            _log(
                address=FOREIGN,
                topics=_buy_log()["topics"],
                data=_buy_log()["data"],
                tx="0x" + "c4" * 32,
                block=97,
            ),
        ],
    )
    settings = _settings(settings_factory)
    state = _ScanState.for_loop(settings)
    result = await _run(fake, lambda s: rh_pons._scan_pass(s, db, settings, state))
    assert result.status == "completed", result.reason
    assert result.recorded_events == 3
    assert result.excluded_foreign_logs == 2
    assert result.trade_logs == 4
    assert result.trade_emitters == 4
    queries = fake.get_logs_queries()
    assert len(queries) == 2  # factory + one topic-only trade query
    assert "address" not in queries[1]
    assert fake.batch_posts >= 1
    assert fake.single_header_requests() == 1  # final chain-movement check only
    cur = await db._conn.execute(
        "SELECT DISTINCT curve_address FROM curve_launch_events "
        "WHERE event_name='curve_buy'"
    )
    assert {r[0] for r in await cur.fetchall()} == {CURVE, CURVE2}
    checkpoint = await db.get_curve_scan_checkpoint(
        DEP.chain_id, DEP.version, DEP.factory
    )
    assert checkpoint["next_block"] == 101


async def test_member_emitter_malformed_trade_still_fails_scan(db, settings_factory):
    await _discovery(db, TOKEN, CURVE)
    bad = _log(address=CURVE, topics=[TOPIC_CURVE_BUY, "0x" + _word_addr(BUYER)])
    fake = FakeRpc(head=100, trade_logs=[bad])
    settings = _settings(settings_factory)
    state = _ScanState.for_loop(settings)
    result = await _run(fake, lambda s: rh_pons._scan_pass(s, db, settings, state))
    assert (result.status, result.reason) == ("failed", "malformed_log")
    assert (
        await db.get_curve_scan_checkpoint(DEP.chain_id, DEP.version, DEP.factory)
        is None
    )
    assert "rh_pons" not in await db.load_ingest_watchdog_state()


async def test_malformed_provider_log_identity_fails_before_exclusion(
    db, settings_factory
):
    stray = _log(address=FOREIGN, topics=_buy_log()["topics"], block=5000)
    fake = FakeRpc(head=100, trade_logs=[])
    fake.trade_logs = [stray]
    settings = _settings(settings_factory)
    state = _ScanState.for_loop(settings)

    original = fake.result

    def no_range_filter(body):
        if body["method"] == "eth_getLogs" and "address" not in body["params"][0]:
            fake.requests.append((body["method"], body["params"]))
            return [stray]
        return original(body)

    fake.result = no_range_filter
    result = await _run(fake, lambda s: rh_pons._scan_pass(s, db, settings, state))
    assert (result.status, result.reason) == ("failed", "malformed_trade_log")


async def test_topic_only_rechecks_curve_restored_by_orphaned_graduation(
    db, settings_factory
):
    from tests.test_rh_pons_collector import _collect

    await _collect(
        [_launch_log(), _graduated_log(block=199, log_index=1)], db, settings_factory()
    )
    assert (await db.get_curve_launch(DEP.chain_id, TOKEN))[
        "lifecycle_status"
    ] == "on_v4"
    await db.save_curve_scan_checkpoint(
        DEP.chain_id,
        DEP.version,
        DEP.factory,
        next_block=201,
        block_hashes={str(h): "0x" + "bb" * 32 for h in range(197, 201)},
    )
    fake = FakeRpc(
        head=201,
        trade_logs=[_buy_log(block=200, block_hash="0x" + "cc" * 32, log_index=2)],
        hash_for=lambda h: "0x" + ("cc" if h >= 199 else "bb") * 32,
    )
    settings = _settings(settings_factory, RH_PONS_REORG_OVERLAP_BLOCKS=3)
    state = _ScanState.for_loop(settings)
    result = await _run(fake, lambda s: rh_pons._scan_pass(s, db, settings, state))
    assert result.status == "completed", result.reason
    assert result.recorded_events == 1
    assert result.excluded_foreign_logs == 0
    assert (await db.get_curve_launch(DEP.chain_id, TOKEN))[
        "lifecycle_status"
    ] == "on_curve"


# ----------------------------------------------- coverage and stale provider


@pytest.mark.parametrize("entry", ["poll_once", "loop_pass"])
async def test_stale_provider_head_never_regresses_checkpoint(
    db, settings_factory, entry
):
    hashes = {str(h): "0x" + "bb" * 32 for h in range(107, 120)}
    await db.save_curve_scan_checkpoint(
        DEP.chain_id, DEP.version, DEP.factory, 120, hashes, head_block=125
    )
    before = await db.get_curve_scan_checkpoint(DEP.chain_id, DEP.version, DEP.factory)
    fake = FakeRpc(head=110)
    settings = _settings(settings_factory)

    async def call(session):
        if entry == "poll_once":
            return await rh_pons.poll_once(session, db, settings)
        return await rh_pons._scan_pass(
            session, db, settings, _ScanState.for_loop(settings)
        )

    outcome = await _run(fake, call)
    if entry == "loop_pass":
        assert (outcome.status, outcome.reason) == (
            "head_behind",
            "provider_head_behind_checkpoint",
        )
    else:
        assert outcome == 0
    assert fake.get_logs_queries() == []
    assert (
        await db.get_curve_scan_checkpoint(DEP.chain_id, DEP.version, DEP.factory)
        == before
    )
    assert "rh_pons" not in await db.load_ingest_watchdog_state()


async def test_head_at_checkpoint_reverifies_overlap_without_regression(
    db, settings_factory
):
    hashes = {str(h): "0x" + "bb" * 32 for h in range(107, 120)}
    await db.save_curve_scan_checkpoint(
        DEP.chain_id, DEP.version, DEP.factory, 120, hashes
    )
    fake = FakeRpc(head=119)
    settings = _settings(settings_factory)
    state = _ScanState.for_loop(settings)
    result = await _run(fake, lambda s: rh_pons._scan_pass(s, db, settings, state))
    assert result.status == "completed"
    assert result.new_blocks == 0
    assert result.caught_up
    assert (await db.get_curve_scan_checkpoint(DEP.chain_id, DEP.version, DEP.factory))[
        "next_block"
    ] == 120


async def test_cold_start_is_pinned_across_failed_and_shrunk_retry(
    db, settings_factory
):
    settings = _settings(
        settings_factory,
        RH_PONS_INITIAL_LOOKBACK_BLOCKS=50,
        RH_PONS_MIN_SCAN_SPAN_BLOCKS=100,
    )
    state = _ScanState.for_loop(settings)
    state.span = 400
    failing = FakeRpc(head=1000)
    failing.log_error = True
    first = await _run(failing, lambda s: rh_pons._scan_pass(s, db, settings, state))
    assert (first.status, first.reason) == ("failed", "factory_logs_unavailable")
    assert state.cold_start_block == 951
    assert _after_pass(state, first, settings) > 0
    assert state.span == 200
    later = FakeRpc(head=1500)
    second = await _run(later, lambda s: rh_pons._scan_pass(s, db, settings, state))
    assert second.status == "completed", second.reason
    assert int(later.get_logs_queries()[0]["fromBlock"], 16) == 951
    assert (second.from_block, second.to_block, second.new_blocks) == (951, 1150, 200)
    assert not second.caught_up
    assert (await db.get_curve_scan_checkpoint(DEP.chain_id, DEP.version, DEP.factory))[
        "next_block"
    ] == 1151


# --------------------------------------------------------------- loop control


def test_window_adapts_within_bounds_and_failures_never_busy_loop(settings_factory):
    settings = _settings(
        settings_factory,
        RH_PONS_MIN_SCAN_SPAN_BLOCKS=100,
        RH_PONS_BACKFILL_BLOCK_SPAN=800,
        RH_PONS_IDLE_SLEEP_SEC=1,
        RH_PONS_FAILURE_BACKOFF_MAX_SEC=5,
    )
    state = _ScanState.for_loop(settings)
    assert (state.span, state.min_span, state.max_span) == (100, 100, 800)
    behind = _PassResult("completed", start_head=1000, to_block=500, duration_s=1)
    assert [_after_pass(state, behind, settings) for _ in range(4)] == [0.0] * 4
    assert state.span == 800
    slow = _PassResult("completed", start_head=1000, to_block=500, duration_s=20)
    assert _after_pass(state, slow, settings) == 0.0 and state.span == 800
    delays = [_after_pass(state, _PassResult("timeout"), settings) for _ in range(6)]
    assert delays == [1, 2, 4, 5, 5, 5]
    assert state.span == 100
    caught_up = _PassResult("completed", start_head=500, to_block=500, duration_s=1)
    assert _after_pass(state, caught_up, settings) == 1
    # Failures decay by one per completed pass instead of resetting (F1).
    assert state.failures == 5 and state.span == 100


def test_refusal_backs_off_without_shrinking_and_head_behind_idles(settings_factory):
    settings = _settings(
        settings_factory, RH_PONS_IDLE_SLEEP_SEC=2, RH_PONS_FAILURE_BACKOFF_MAX_SEC=60
    )
    state = _ScanState.for_loop(settings)
    state.span = 400
    assert _after_pass(state, _PassResult("refused"), settings) == 2
    assert _after_pass(state, _PassResult("refused"), settings) == 4
    assert state.span == 400
    assert _after_pass(state, _PassResult("head_behind"), settings) == 2
    assert state.failures == 2


@pytest.mark.parametrize("retry_after,expected", [(30.0, 30.0), (1000.0, 60.0)])
def test_rate_limit_honours_bounded_retry_after(
    settings_factory, retry_after, expected
):
    settings = _settings(
        settings_factory, RH_PONS_IDLE_SLEEP_SEC=1, RH_PONS_FAILURE_BACKOFF_MAX_SEC=60
    )
    state = _ScanState.for_loop(settings)
    result = _PassResult("failed", rate_limited=True, retry_after=retry_after)
    assert _after_pass(state, result, settings) == expected


def test_min_span_is_capped_at_configured_maximum(settings_factory):
    settings = _settings(settings_factory, RH_PONS_BACKFILL_BLOCK_SPAN=10)
    state = _ScanState.for_loop(settings)
    assert (state.span, state.min_span, state.max_span) == (10, 10, 10)


class NullCollectorDb:
    """Collector-owned DB stand-in for loop-control tests (records, no SQL)."""

    def __init__(self, persisted=None):
        self._conn = None
        self.persisted = dict(persisted or {})
        self.attempts = []
        self.heads = []
        self.closed = False

    async def load_ingest_watchdog_state(self):
        return dict(self.persisted)

    async def upsert_ingest_watchdog_state(self, source, count):
        self.attempts.append((source, count))

    async def record_curve_scan_attempt_head(self, *args):
        self.heads.append(args)

    async def close(self):
        self.closed = True


def _use_owned_db(monkeypatch, owned=None):
    owned = owned if owned is not None else NullCollectorDb()

    async def open_owned(db, settings):
        return owned

    monkeypatch.setattr(rh_pons, "_open_collector_db", open_owned)
    return owned


async def _drive_loop(monkeypatch, settings, passes, *, stop_after_sleeps=1):
    sleeps, events, results = [], [], []

    async def fake_sleep(delay):
        sleeps.append(delay)
        if len(sleeps) >= stop_after_sleeps:
            raise asyncio.CancelledError

    queue = list(passes)

    async def fake_pass(session, db, settings_arg, state):
        step = queue.pop(0) if len(queue) > 1 else queue[0]
        return await step(state)

    monkeypatch.setattr(rh_pons, "_sleep", fake_sleep)
    monkeypatch.setattr(rh_pons, "_scan_pass", fake_pass)
    _use_owned_db(monkeypatch)
    monkeypatch.setattr(
        rh_pons.logger,
        "info",
        lambda event, **fields: events.append((event, fields)),
    )
    with pytest.raises(asyncio.CancelledError):
        await rh_pons.run_rh_pons_loop(None, None, settings, on_pass=results.append)
    loop_passes = [fields for event, fields in events if event == "rh_pons_loop_pass"]
    return sleeps, loop_passes, results


async def test_loop_survives_refusal_and_errors_with_bounded_backoff(
    monkeypatch, settings_factory
):
    settings = _settings(
        settings_factory, RH_PONS_IDLE_SLEEP_SEC=1, RH_PONS_FAILURE_BACKOFF_MAX_SEC=3
    )

    async def refused(state):
        return _PassResult("refused", reason="no_rpc_url_configured")

    async def boom(state):
        raise RuntimeError(URL)

    sleeps, passes, _ = await _drive_loop(
        monkeypatch, settings, [refused, boom, refused, refused], stop_after_sleeps=4
    )
    assert sleeps == [1, 2, 3, 3]
    assert [p["status"] for p in passes] == ["refused", "error", "refused", "refused"]
    assert passes[1]["reason"] == "RuntimeError"
    assert all("SECRET" not in str(p) for p in passes)


async def test_loop_does_not_sleep_while_draining_backlog(
    monkeypatch, settings_factory
):
    settings = _settings(
        settings_factory,
        RH_PONS_MIN_SCAN_SPAN_BLOCKS=100,
        RH_PONS_BACKFILL_BLOCK_SPAN=400,
        RH_PONS_IDLE_SLEEP_SEC=2,
    )
    spans = []

    def behind(state):
        async def step(state):
            spans.append(state.span)
            return _PassResult(
                "completed", start_head=10_000, to_block=5000, completion_head=10_010
            )

        return step

    async def caught_up(state):
        spans.append(state.span)
        return _PassResult(
            "completed", start_head=5000, to_block=5000, completion_head=5003
        )

    sleeps, passes, _ = await _drive_loop(
        monkeypatch, settings, [behind(None), behind(None), behind(None), caught_up]
    )
    assert sleeps == [2]
    assert spans == [100, 200, 400, 400]
    assert [p["sleep_s"] for p in passes] == [0.0, 0.0, 0.0, 2]
    assert passes[0]["lag_blocks"] == 5010


async def test_loop_timeout_cancels_pass_and_backs_off(monkeypatch, settings_factory):
    settings = _settings(settings_factory, RH_PONS_IDLE_SLEEP_SEC=1).model_copy(
        update={"RH_PONS_POLL_TIMEOUT_SEC": 0.02}
    )
    cancelled = asyncio.Event()

    async def stuck(state):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    sleeps, passes, results = await _drive_loop(monkeypatch, settings, [stuck])
    assert cancelled.is_set()
    assert [p["status"] for p in passes] == ["timeout"]
    assert sleeps == [1]
    assert results[0].status == "timeout"


async def test_loop_reports_pass_rpc_stats_from_child_tasks(
    monkeypatch, settings_factory
):
    settings = _settings(settings_factory, RH_PONS_IDLE_SLEEP_SEC=1)

    async def throttled(state):
        async def child():
            rh_pons._count_rpc_calls(3)
            rh_pons._note_rate_limit(429, {"Retry-After": "9"})

        await asyncio.gather(child())
        return _PassResult("failed", reason="headers_unavailable")

    sleeps, passes, results = await _drive_loop(monkeypatch, settings, [throttled])
    assert (results[0].rpc_calls, results[0].rate_limited) == (3, True)
    assert results[0].retry_after == 9.0
    assert sleeps == [9.0]


async def test_loop_cancellation_during_idle_sleep_is_prompt(
    monkeypatch, settings_factory
):
    settings = _settings(settings_factory, RH_PONS_IDLE_SLEEP_SEC=300)
    entered = asyncio.Event()

    async def caught_up(session, db, settings_arg, state):
        entered.set()
        return _PassResult("completed", start_head=1, to_block=1, completion_head=1)

    monkeypatch.setattr(rh_pons, "_scan_pass", caught_up)
    owned = _use_owned_db(monkeypatch)
    task = asyncio.create_task(rh_pons.run_rh_pons_loop(None, None, settings))
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert owned.closed  # the owned connection is closed on shutdown


async def test_loop_disabled_returns_without_scanning(monkeypatch, settings_factory):
    async def forbidden(*args):
        raise AssertionError("scanned while disabled")

    monkeypatch.setattr(rh_pons, "_scan_pass", forbidden)
    await asyncio.wait_for(
        rh_pons.run_rh_pons_loop(
            None, None, settings_factory(RH_PONS_COLLECTOR_ENABLED=False)
        ),
        1,
    )


async def test_real_loop_pass_completes_against_fixture_rpc(db, settings_factory):
    fake = FakeRpc(
        head=100, factory_logs=[_launch_log()], trade_logs=[_buy_log(log_index=1)]
    )
    settings = _settings(settings_factory, RH_PONS_IDLE_SLEEP_SEC=300)
    results = []
    first = asyncio.Event()

    def observe(result):
        results.append(result)
        first.set()

    with aioresponses() as m:
        m.post(URL, callback=fake, repeat=True)
        async with aiohttp.ClientSession() as session:
            task = asyncio.create_task(
                rh_pons.run_rh_pons_loop(session, db, settings, on_pass=observe)
            )
            await asyncio.wait_for(first.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert results[0].status == "completed", results[0].reason
    assert results[0].recorded_events == 2
    assert results[0].rpc_calls >= 5
    assert (await db.load_ingest_watchdog_state())["rh_pons"] == 0


def test_new_settings_defaults_and_bounds(settings_factory):
    from pydantic import ValidationError

    s = settings_factory()
    assert not s.RH_PONS_COLLECTOR_ENABLED
    assert s.RH_PONS_MIN_SCAN_SPAN_BLOCKS == 100
    assert s.RH_PONS_IDLE_SLEEP_SEC == 2.0
    assert s.RH_PONS_FAILURE_BACKOFF_MAX_SEC == 60.0
    assert s.RH_PONS_HEADER_BATCH_SIZE == 50
    assert s.RH_PONS_TOPIC_ONLY_TRADE_QUERY is True
    for key, value in (
        ("RH_PONS_MIN_SCAN_SPAN_BLOCKS", 0),
        ("RH_PONS_IDLE_SLEEP_SEC", 0),
        ("RH_PONS_FAILURE_BACKOFF_MAX_SEC", 0.5),
        ("RH_PONS_HEADER_BATCH_SIZE", 0),
        ("RH_PONS_HEADER_BATCH_SIZE", 101),
    ):
        with pytest.raises(ValidationError):
            settings_factory(**{key: value})
