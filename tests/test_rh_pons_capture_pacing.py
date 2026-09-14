"""Pacing and header-budget truncation for sustained RH/Pons capture.

Motivated by investigation/rh_capacity_smoke_throttled_20260913.json: header
reads scale with active blocks, a doubled window produced a second ~100-call
burst seconds after the first, and the public RPC answered 429. Fixtures only;
no network. Quota is unknown and nothing here claims a provider limit.
"""

import asyncio
import random
import time

import pytest

from scout.db import Database
from scout.ingestion import rh_pons
from scout.ingestion.rh_pons import (
    _after_pass,
    _header_cutoff,
    _PassResult,
    _RpcPacer,
    _RpcStats,
    _ScanState,
)
from tests.test_rh_pons_collector import CURVE, _buy_log, _launch_log, _verified_dep
from tests.test_rh_pons_sustained_loop import (
    URL,
    FakeRpc,
    _drive_loop,
    _run,
    _settings,
)


@pytest.fixture(autouse=True)
def _verified_registry(monkeypatch):
    monkeypatch.setattr(rh_pons, "PONS_DEPLOYMENTS", (_verified_dep(),))
    yield


@pytest.fixture
async def db(tmp_path):
    instance = Database(tmp_path / "pacing.db")
    await instance.initialize()
    yield instance
    await instance.close()


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now
        self.sleeps = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(round(seconds, 6))
        self.now += seconds
        # A faulty acquire loop must FAIL the test, not spin forever: bound the
        # simulated sleeps and yield so wait_for/cancellation can take effect.
        if len(self.sleeps) > 1000:
            raise AssertionError("fake clock slept more than 1000 times")
        await asyncio.sleep(0)


def _pacer(clock, **overrides):
    values = dict(rate=10.0, burst=5, min_rate=1.0, max_hold=30.0)
    values.update(overrides)
    return _RpcPacer(clock=clock, sleep=clock.sleep, **values)


# ------------------------------------------------------------------ pacer


async def test_pacer_allows_burst_then_paces_logical_calls():
    clock = FakeClock()
    pacer = _pacer(clock)
    await pacer.acquire(5)
    assert clock.sleeps == []
    await pacer.acquire(3)
    assert clock.sleeps == [0.3]
    assert pacer.waited_s == pytest.approx(0.3)


async def test_pacer_oversized_call_borrows_instead_of_deadlocking():
    clock = FakeClock()
    pacer = _pacer(clock)
    await asyncio.wait_for(pacer.acquire(8), 1)
    assert clock.sleeps == []
    assert pacer.tokens == pytest.approx(-3)
    await pacer.acquire(1)
    assert clock.sleeps == [0.4]


async def test_pacer_serializes_concurrent_batches():
    clock = FakeClock()
    pacer = _pacer(clock)
    await asyncio.gather(pacer.acquire(5), pacer.acquire(5))
    assert clock.sleeps == [0.5]
    assert pacer.tokens == pytest.approx(0)


async def test_throttle_empties_bucket_halves_rate_and_holds_bounded():
    clock = FakeClock()
    pacer = _pacer(clock, max_hold=30.0)
    pacer.throttled(3.0)
    assert (pacer.tokens, pacer.rate) == (0.0, 5.0)
    await pacer.acquire(1)
    assert clock.sleeps == [3.0]
    for _ in range(5):
        pacer.throttled(None)
    assert pacer.rate == 1.0
    pacer.throttled(float("inf"))
    assert pacer.not_before == pytest.approx(clock.now + 30.0)


def test_recovery_is_additive_and_capped():
    clock = FakeClock()
    pacer = _pacer(clock, rate=8.0, min_rate=1.0)
    pacer.throttled(None)
    pacer.throttled(None)
    assert pacer.rate == 2.0
    pacer.recovered()
    assert pacer.rate == pytest.approx(2.8)
    for _ in range(20):
        pacer.recovered()
    assert pacer.rate == 8.0


def test_call_allowance_respects_tokens_rate_and_hold():
    clock = FakeClock()
    pacer = _pacer(clock)
    assert pacer.call_allowance(2.0) == 25
    pacer.not_before = clock.now + 1.0
    assert pacer.call_allowance(2.0) == 15
    assert pacer.call_allowance(0.5) == 5


def test_min_rate_never_exceeds_configured_rate():
    pacer = _pacer(FakeClock(), rate=2.0, min_rate=5.0)
    pacer.throttled(None)
    assert pacer.rate == 2.0


async def test_rate_limit_note_feeds_stats_and_pacer():
    clock = FakeClock()
    pacer = _pacer(clock)
    stats = _RpcStats()
    tokens = (rh_pons._RPC_STATS.set(stats), rh_pons._RPC_PACER.set(pacer))
    try:
        rh_pons._note_rate_limit(429, {"Retry-After": "4"})
        rh_pons._note_rate_limit(500, {"Retry-After": "99"})
    finally:
        rh_pons._RPC_STATS.reset(tokens[0])
        rh_pons._RPC_PACER.reset(tokens[1])
    assert (stats.rate_limited, stats.retry_after) == (True, 4.0)
    assert pacer.rate == 5.0
    assert pacer.not_before == pytest.approx(clock.now + 4.0)


async def test_transport_charges_logical_calls_including_batch_items():
    clock = FakeClock()
    pacer = _pacer(clock, burst=5)
    fake = FakeRpc()
    stats = _RpcStats()
    tokens = (rh_pons._RPC_STATS.set(stats), rh_pons._RPC_PACER.set(pacer))
    try:

        async def calls(session):
            headers = await rh_pons._fetch_block_headers_batched(
                session, URL, range(4), 2
            )
            await rh_pons._rpc(session, URL, "eth_chainId", [])
            await rh_pons._rpc(session, URL, "eth_chainId", [])
            return headers

        headers = await _run(fake, calls)
    finally:
        rh_pons._RPC_STATS.reset(tokens[0])
        rh_pons._RPC_PACER.reset(tokens[1])
    assert set(headers) == set(range(4))
    assert stats.calls == 6
    assert clock.sleeps == [0.1]  # 6 logical calls against a burst of 5


# ----------------------------------------------------------- header cutoff


def _brute_cutoff(fixed, events, low, high, overlap, floor_block, budget):
    best = low
    for c in range(low, high + 1):
        need = (
            set(fixed)
            | {e for e in events if e <= c}
            | set(range(max(floor_block, c - overlap), c + 1))
        )
        if len(need) <= budget:
            best = c
    return best


def test_header_cutoff_matches_brute_force():
    rng = random.Random(20260913)
    for _ in range(300):
        floor_block = 90
        overlap = rng.randint(0, 12)
        low = rng.randint(95, 150)
        high = low + rng.randint(-1, 120)
        fixed = rng.sample(range(max(floor_block, low - overlap - 1), low), k=0)
        if rng.random() < 0.7:
            span = list(range(max(floor_block, low - overlap - 1), low))
            fixed = rng.sample(span, k=rng.randint(0, len(span)))
        events = rng.sample(
            range(max(floor_block, low - overlap), max(low, high) + 1),
            k=rng.randint(0, max(0, high - low)),
        )
        budget = rng.randint(0, 80)
        args = dict(
            fixed=fixed,
            events=events,
            low=low,
            high=high,
            overlap=overlap,
            floor_block=floor_block,
            budget=budget,
        )
        expected = high if high < low else _brute_cutoff(**args)
        assert _header_cutoff(**args) == expected, args


def test_header_cutoff_keeps_minimal_progress_when_nothing_fits():
    assert (
        _header_cutoff(
            fixed=range(100, 113),
            events=range(113, 200),
            low=113,
            high=199,
            overlap=12,
            floor_block=90,
            budget=5,
        )
        == 113
    )


# ------------------------------------------------------ truncated coverage


def _trade(block, index):
    return _buy_log(block=block, tx="0x" + f"{index:064x}", log_index=1)


async def test_budget_truncates_to_verifiable_prefix_and_resumes_without_gaps(
    db, settings_factory
):
    trades = [_trade(block, i) for i, block in enumerate(range(101, 161), start=1)]
    fake = FakeRpc(head=200, factory_logs=[_launch_log(block=100)], trade_logs=trades)
    settings = _settings(settings_factory, RH_PONS_MAX_HEADERS_PER_PASS=30)
    state = _ScanState.for_loop(settings)
    covered, recorded = [], 0
    for _ in range(10):
        state.span = 200
        before = len(fake.requests)
        result = await _run(fake, lambda s: rh_pons._scan_pass(s, db, settings, state))
        assert result.status == "completed", result.reason
        header_calls = sum(
            1
            for method, _ in fake.requests[before:]
            if method == "eth_getBlockByNumber"
        )
        assert result.header_heights <= 30
        assert header_calls <= 30 + 1  # batch reads + final chain-movement check
        covered.append((result.from_block, result.to_block, result.truncated))
        recorded += result.recorded_events
        if result.to_block == 200:
            break
    assert covered[0] == (90, 129, True)
    assert covered[-1][1] == 200 and not covered[-1][2]
    assert all(b[0] <= a[1] + 1 for a, b in zip(covered, covered[1:]))
    checkpoint = await db.get_curve_scan_checkpoint(
        rh_pons.ROBINHOOD_CHAIN_ID, "pons_v2", _verified_dep().factory
    )
    assert checkpoint["next_block"] == 201
    cur = await db._conn.execute(
        "SELECT COUNT(DISTINCT transaction_hash), COUNT(*) FROM curve_launch_events "
        "WHERE event_name='curve_buy' AND curve_address=?",
        (CURVE,),
    )
    assert tuple(await cur.fetchone()) == (60, 60)
    assert recorded == 61


async def test_truncated_pass_never_writes_or_checkpoints_above_cutoff(
    db, settings_factory
):
    trades = [_trade(block, i) for i, block in enumerate(range(101, 161), start=1)]
    fake = FakeRpc(head=200, factory_logs=[_launch_log(block=100)], trade_logs=trades)
    settings = _settings(settings_factory, RH_PONS_MAX_HEADERS_PER_PASS=30)
    state = _ScanState.for_loop(settings)
    state.span = 200
    # Interrupted-write leftover: evidence above any checkpoint. The truncated
    # pass must not spend header reads verifying it before its turn.
    leftover = "0x" + "ee" * 32
    await db.record_curve_launch_event(
        chain_id=rh_pons.ROBINHOOD_CHAIN_ID,
        protocol="pons_v2",
        event_name="curve_buy",
        token_address=None,
        curve_address=CURVE,
        transaction_hash=leftover,
        log_index=0,
        block_number=195,
        block_hash="0x" + "bb" * 32,
        event_time=None,
        observed_at="2026-09-13T00:00:00+00:00",
        provider_available_at=None,
        source="fixture",
        provenance="fixture:source_derived",
        payload_json="{}",
    )
    result = await _run(fake, lambda s: rh_pons._scan_pass(s, db, settings, state))
    assert (result.to_block, result.truncated, result.header_budget) == (129, True, 30)
    assert result.header_heights <= 30
    cur = await db._conn.execute(
        "SELECT MAX(block_number) FROM curve_launch_events WHERE transaction_hash != ?",
        (leftover,),
    )
    assert (await cur.fetchone())[0] == 129
    headers = [
        int(params[0], 16)
        for method, params in fake.requests
        if method == "eth_getBlockByNumber"
    ]
    assert max(headers) == 129


async def test_pacer_allowance_shrinks_budget_below_configured_cap(
    db, settings_factory
):
    trades = [_trade(block, i) for i, block in enumerate(range(101, 161), start=1)]
    fake = FakeRpc(head=200, factory_logs=[_launch_log(block=100)], trade_logs=trades)
    settings = _settings(settings_factory, RH_PONS_MAX_HEADERS_PER_PASS=80)
    state = _ScanState.for_loop(settings)
    state.span = 200
    pacer = _RpcPacer(rate=0.5, burst=45, min_rate=0.5, max_hold=30.0)
    state.pass_deadline = time.monotonic() + 0.2
    token = rh_pons._RPC_PACER.set(pacer)
    try:
        # Bounded: a pass that ignored the pacer allowance would wait on a
        # 0.5 calls/s bucket in real time; that must fail, not hang.
        async with asyncio.timeout(5):
            result = await _run(
                fake, lambda s: rh_pons._scan_pass(s, db, settings, state)
            )
    finally:
        rh_pons._RPC_PACER.reset(token)
    assert result.status == "completed", result.reason
    # 45 tokens - 4 pre-header calls = 41 available, minus 2 tail calls.
    assert result.header_budget in (39, 40)
    assert result.truncated
    assert result.header_heights <= result.header_budget
    assert pacer.waited_s == 0.0


async def test_poll_once_is_not_budgeted(db, settings_factory):
    trades = [_trade(block, i) for i, block in enumerate(range(101, 161), start=1)]
    fake = FakeRpc(head=200, factory_logs=[_launch_log(block=100)], trade_logs=trades)
    settings = _settings(
        settings_factory,
        RH_PONS_MAX_HEADERS_PER_PASS=1,
        RH_PONS_BACKFILL_BLOCK_SPAN=200,
    )
    fake.trade_logs = []  # legacy address-batched mode: no known curves yet
    assert await _run(fake, lambda s: rh_pons.poll_once(s, db, settings)) == 1
    checkpoint = await db.get_curve_scan_checkpoint(
        rh_pons.ROBINHOOD_CHAIN_ID, "pons_v2", _verified_dep().factory
    )
    assert checkpoint["next_block"] == 201


def test_truncated_pass_sizes_window_to_verified_progress(settings_factory):
    settings = _settings(
        settings_factory,
        RH_PONS_MIN_SCAN_SPAN_BLOCKS=50,
        RH_PONS_BACKFILL_BLOCK_SPAN=2000,
    )
    state = _ScanState.for_loop(settings)
    state.span = 1600
    result = _PassResult(
        "completed",
        start_head=5000,
        to_block=1200,
        new_blocks=120,
        truncated=True,
        duration_s=1,
    )
    assert _after_pass(state, result, settings) == 0.0
    assert state.span == 240
    tiny = _PassResult(
        "completed", start_head=5000, to_block=1000, new_blocks=3, truncated=True
    )
    _after_pass(state, tiny, settings)
    assert state.span == 50


async def test_loop_throttle_lowers_rate_and_completed_pass_recovers(
    monkeypatch, settings_factory
):
    settings = _settings(
        settings_factory, RH_PONS_IDLE_SLEEP_SEC=1, RH_PONS_RPC_CALLS_PER_SEC=8
    )

    async def throttled(state):
        rh_pons._note_rate_limit(429, {})
        return _PassResult("failed", reason="headers_unavailable")

    async def caught_up(state):
        assert state.pass_deadline is not None
        return _PassResult("completed", start_head=5, to_block=5, completion_head=5)

    sleeps, passes, results = await _drive_loop(
        monkeypatch, settings, [throttled, caught_up], stop_after_sleeps=2
    )
    assert [p["rpc_rate"] for p in passes] == [4.0, 4.8]
    assert results[0].rate_limited and not results[1].rate_limited
    assert all("pacing_wait_s" in p and "rpc_s" in p for p in passes)


def test_pacing_settings_defaults_and_bounds(settings_factory):
    from pydantic import ValidationError

    s = settings_factory()
    assert (
        s.RH_PONS_RPC_CALLS_PER_SEC,
        s.RH_PONS_RPC_MIN_CALLS_PER_SEC,
        s.RH_PONS_RPC_BURST_CALLS,
        s.RH_PONS_MAX_HEADERS_PER_PASS,
    ) == (8.0, 1.0, 100, 80)
    for key, value in (
        ("RH_PONS_RPC_CALLS_PER_SEC", 0),
        ("RH_PONS_RPC_MIN_CALLS_PER_SEC", 0),
        ("RH_PONS_RPC_BURST_CALLS", 0),
        ("RH_PONS_MAX_HEADERS_PER_PASS", 0),
    ):
        with pytest.raises(ValidationError):
            settings_factory(**{key: value})
