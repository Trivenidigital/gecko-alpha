import argparse
import datetime as dt

import pytest
from gecko.features import Features
from gecko.rank import disqualify, rule_f4, score, rank_once
from gecko.store import iso_plus

A = "2026-10-15T00:00:00Z"


def feat(**kw) -> Features:
    base = dict(
        token_id="solana:x",
        as_of=A,
        observed_at=A,
        liquidity_usd=300_000.0,
        liquidity_gate_usd=300_000.0,
        age_days=30.0,
        vol_7d_ratio=2.5,
        security_ok=True,
        bundle_flag=False,
    )
    base.update(kw)
    return Features(**base)


def test_disqualify_which_gate(settings):
    fail = feat(security_ok=False, security_structural=False)
    assert disqualify(fail, settings) == "security_fail"
    assert disqualify(feat(security_ok=False), settings) == "security_tax"
    assert disqualify(feat(top10_pct=61.0), settings) == "concentration"
    assert disqualify(feat(liquidity_gate_usd=100.0), settings) == "liquidity"
    # no fresh reading is absence, judged by "dead" only
    assert disqualify(feat(liquidity_gate_usd=None), settings) is None
    assert disqualify(feat(observed_at="2026-10-01T00:00:00Z"), settings) == "dead"
    assert disqualify(feat(observed_at=None), settings) == "dead"
    assert disqualify(feat(), settings) is None


def test_disqualify_boundaries(settings):
    assert disqualify(feat(top10_pct=60.0), settings) is None
    assert disqualify(feat(liquidity_gate_usd=250_000.0), settings) is None
    assert disqualify(feat(observed_at="2026-10-08T00:00:00Z"), settings) is None
    assert disqualify(feat(observed_at="2026-10-07T23:59:59Z"), settings) == "dead"


def test_disqualify_precedence(settings):
    everything = dict(
        security_ok=False,
        security_structural=False,
        top10_pct=99.0,
        liquidity_gate_usd=1.0,
        observed_at="2026-09-01T00:00:00Z",
    )
    assert disqualify(feat(**everything), settings) == "security_fail"
    everything["security_structural"] = True
    assert disqualify(feat(**everything), settings) == "concentration"
    everything["top10_pct"] = None
    assert disqualify(feat(**everything), settings) == "liquidity"
    everything["liquidity_gate_usd"] = 300_000.0
    assert disqualify(feat(**everything), settings) == "dead"
    everything["observed_at"] = A
    assert disqualify(feat(**everything), settings) == "security_tax"
    everything["security_ok"] = True
    assert disqualify(feat(**everything), settings) is None


def test_f4_boundaries(settings):
    assert rule_f4(feat(age_days=90.0, vol_7d_ratio=2.0), settings) is True
    assert rule_f4(feat(age_days=91.0), settings) is False
    assert rule_f4(feat(age_days=None), settings) is False
    assert rule_f4(feat(vol_7d_ratio=1.9), settings) is False
    assert rule_f4(feat(bundle_flag=True), settings) is False
    assert rule_f4(feat(security_ok=False), settings) is False


def test_f4_rejects_when_ratio_missing(settings):
    assert rule_f4(feat(vol_7d_ratio=None), settings) is False


def test_f4_security_unknown_policy(settings):
    assert rule_f4(feat(security_ok=None), settings) is True
    settings.require_security_known = True
    assert rule_f4(feat(security_ok=None), settings) is False
    assert rule_f4(feat(security_ok=True), settings) is True


def test_require_security_known_defaults_false(settings):
    assert settings.require_security_known is False


STATS = {
    "vol_7d_ratio": (2.0, 1.0),
    "holder_growth_7d": (0.1, 0.1),
    "smart_money_wallets": (2.0, 2.0),
    "caller_signal": (1.0, 1.0),
}


def test_score_missing_contributes_zero_and_bundle_penalty():
    # vol z=1 (w1.0); holder/smart missing -> 0; zero callers is a real
    # caller_signal 0 -> z=-1 (w0.5)
    s1 = score(feat(vol_7d_ratio=3.0), STATS)
    s2 = score(feat(vol_7d_ratio=3.0, bundle_flag=True), STATS)
    assert s1 == pytest.approx(1.0 - 0.5) and s2 == pytest.approx(1.0 - 0.5 - 1.0)


def test_score_weights_and_caller_signal():
    # holder z=1 (w1.0), smart z=1 (w0.5), caller 3*1.0=3 -> z=2 (w0.5)
    f = feat(
        vol_7d_ratio=None,
        holder_growth_7d=0.2,
        smart_money_wallets=4,
        callers_7d=3,
        caller_quality=1.0,
    )
    assert score(f, STATS) == pytest.approx(1.0 + 0.5 + 1.0)
    # caller_quality None counts as 0 -> caller_signal 0 -> z=-1 (w0.5)
    g = feat(vol_7d_ratio=None, callers_7d=3, caller_quality=None)
    assert score(g, STATS) == pytest.approx(-0.5)
    # zero callers is a real 0 too, not missing: same -0.5
    z = feat(vol_7d_ratio=None, callers_7d=0)
    assert score(z, STATS) == pytest.approx(-0.5)
    # zero std contributes nothing (only the zero-callers -0.5 term remains)
    flat = dict(STATS, vol_7d_ratio=(2.0, 0.0))
    assert score(feat(vol_7d_ratio=9.0), flat) == pytest.approx(-0.5)


async def seed_token(store, tid, *, age_days=30, liq=300_000.0, vols=None):
    as_of = dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)
    born = (as_of - dt.timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    chain, addr = tid.split(":")
    await store.upsert_token(
        tid, chain, addr, addr.upper(), addr, born, "dexscreener", pool_created_at=born
    )
    await store.set_token_status(tid, "active")
    await store.add_observation(
        tid,
        "dexscreener",
        "2026-10-14T23:00:00Z",
        {"price_usd": 1.0, "liquidity_usd": liq, "mcap_usd": 2e6, "vol_24h_usd": 1e5},
    )
    start = int((as_of - dt.timedelta(days=14)).timestamp())
    vols = vols or [100] * 7 + [300] * 7
    await store.add_observation(
        tid,
        "geckoterminal_ohlcv",
        "2026-10-15T00:00:00Z",
        {
            "pool_address": "p",
            "candles": [[start + i * 86400, 1, 1, 1, 1, v] for i, v in enumerate(vols)],
        },
    )


async def one(store, sql):
    return await (await store.conn.execute(sql)).fetchone()


async def test_rank_once_enters_schedules_and_exits(store, settings):
    await seed_token(store, "solana:good")
    await seed_token(store, "solana:flat", vols=[100] * 14)
    await seed_token(store, "solana:thin", liq=1000.0)
    r = await rank_once(store, settings, A)
    assert r.entered == ["solana:good"] and r.exited == []
    wl = await store.active_watchlist()
    assert [w.token_id for w in wl] == ["solana:good"] and wl[0].rank == 1
    assert wl[0].rule_id == "F4"
    assert wl[0].features_at_entry["vol_7d_ratio"] == pytest.approx(3.0)
    due = await store.due_returns("2028-01-01T00:00:00Z")
    assert sorted(d.horizon_days for d in due) == [1, 7, 30, 90, 180]
    row = await one(
        store,
        "SELECT status, disqualify_reason FROM tokens WHERE token_id='solana:thin'",
    )
    assert row == ("disqualified", "liquidity")
    row = await one(store, "SELECT status FROM tokens WHERE token_id='solana:flat'")
    assert row == ("active",)
    # later: good token loses liquidity -> exits with reason
    await store.add_observation(
        "solana:good",
        "dexscreener",
        "2026-10-20T00:00:00Z",
        {"price_usd": 1.0, "liquidity_usd": 10.0},
    )
    r2 = await rank_once(store, settings, "2026-10-20T01:00:00Z")
    assert r2.exited == ["solana:good"]
    row = await one(
        store, "SELECT exit_reason FROM watchlist WHERE token_id='solana:good'"
    )
    assert row[0] == "liquidity"


async def test_dead_token(store, settings):
    await seed_token(store, "solana:good")  # last DEX reading 2026-10-14T23:00Z
    await rank_once(store, settings, A)
    # 13 h without a DEX reading, inside dead_after_days: stays, not disqualified
    r = await rank_once(store, settings, "2026-10-15T12:00:00Z")
    assert r.exited == [] and r.ranked == 1
    row = await one(
        store,
        "SELECT status, disqualify_reason FROM tokens WHERE token_id='solana:good'",
    )
    assert row == ("active", None)
    # nothing within 7 days: dead
    r = await rank_once(store, settings, "2026-10-25T00:00:00Z")
    assert r.exited == ["solana:good"]
    row = await one(
        store,
        "SELECT status, disqualify_reason FROM tokens WHERE token_id='solana:good'",
    )
    assert row == ("dead", "dead")
    row = await one(
        store, "SELECT exit_reason FROM watchlist WHERE token_id='solana:good'"
    )
    assert row[0] == "dead"


async def test_rank_is_sticky_when_rule_lapses(store, settings):
    await seed_token(store, "solana:good")
    await rank_once(store, settings, A)
    # ratio collapses but no disqualifier: stays on the watchlist
    as_of = dt.datetime(2026, 10, 22, tzinfo=dt.timezone.utc)
    start = int((as_of - dt.timedelta(days=14)).timestamp())
    await store.add_observation(
        "solana:good",
        "geckoterminal_ohlcv",
        "2026-10-22T00:00:00Z",
        {
            "pool_address": "p",
            "candles": [[start + i * 86400, 1, 1, 1, 1, 100] for i in range(14)],
        },
    )
    await store.add_observation(
        "solana:good",
        "dexscreener",
        "2026-10-21T23:00:00Z",
        {"price_usd": 1.0, "liquidity_usd": 300_000.0},
    )
    from gecko.features import compute_features

    entry_sql = "SELECT entered_at, features_at_entry_json FROM watchlist"
    entry_before = await one(store, entry_sql)
    # negative control: F4 really has lapsed at the re-rank time
    f = await compute_features(store, settings, "solana:good", "2026-10-22T00:00:00Z")
    assert disqualify(f, settings) is None and rule_f4(f, settings) is False
    r = await rank_once(store, settings, "2026-10-22T00:00:00Z")
    assert r.exited == [] and r.entered == [] and r.ranked == 1
    assert [w.token_id for w in await store.active_watchlist()] == ["solana:good"]
    assert await one(store, entry_sql) == entry_before
    assert entry_before[0] == A


async def test_max_age_exit(store, settings):
    settings.watchlist_max_age_days = 3
    await seed_token(store, "solana:good")
    await rank_once(store, settings, A)
    await store.add_observation(
        "solana:good",
        "dexscreener",
        "2026-10-19T00:00:00Z",
        {"price_usd": 1.0, "liquidity_usd": 300_000.0},
    )
    r = await rank_once(store, settings, "2026-10-19T00:00:00Z")  # 4 days in
    assert r.exited == ["solana:good"]
    row = await one(
        store, "SELECT exit_reason FROM watchlist WHERE token_id='solana:good'"
    )
    assert row[0] == "max_age"


async def test_max_age_boundary_stays(store, settings):
    settings.watchlist_max_age_days = 3
    await seed_token(store, "solana:good")
    await rank_once(store, settings, A)
    await store.add_observation(
        "solana:good",
        "dexscreener",
        "2026-10-18T00:00:00Z",
        {"price_usd": 1.0, "liquidity_usd": 300_000.0},
    )
    r = await rank_once(store, settings, "2026-10-18T00:00:00Z")  # exactly 3 days
    assert r.exited == []


async def test_max_age_sub_day_excess_exits(store, settings):
    settings.watchlist_max_age_days = 3
    await seed_token(store, "solana:good")
    await rank_once(store, settings, A)
    await store.add_observation(
        "solana:good",
        "dexscreener",
        "2026-10-18T11:00:00Z",
        {"price_usd": 1.0, "liquidity_usd": 300_000.0},
    )
    r = await rank_once(store, settings, "2026-10-18T12:00:00Z")  # 3 d 12 h
    assert r.exited == ["solana:good"]
    row = await one(store, "SELECT exit_reason FROM watchlist")
    assert row[0] == "max_age"


async def test_max_age_one_second_over_exits(store, settings):
    settings.watchlist_max_age_days = 3
    await seed_token(store, "solana:good")
    await rank_once(store, settings, A)
    await store.add_observation(
        "solana:good",
        "dexscreener",
        "2026-10-18T00:00:00Z",
        {"price_usd": 1.0, "liquidity_usd": 300_000.0},
    )
    r = await rank_once(store, settings, "2026-10-18T00:00:01Z")
    assert r.exited == ["solana:good"]


async def test_exited_token_is_not_reentered(store, settings):
    from gecko.features import compute_features

    await seed_token(store, "solana:good")
    await rank_once(store, settings, A)
    await store.add_observation(
        "solana:good",
        "dexscreener",
        "2026-10-15T06:00:00Z",
        {"price_usd": 1.0, "liquidity_usd": 10.0},
    )
    r = await rank_once(store, settings, "2026-10-15T07:00:00Z")
    assert r.exited == ["solana:good"]
    # back to active, liquid, with a fresh 14-candle window that passes F4
    later = "2026-10-20T00:00:00Z"
    later_dt = dt.datetime(2026, 10, 20, tzinfo=dt.timezone.utc)
    start = int((later_dt - dt.timedelta(days=14)).timestamp())
    vols = [100] * 7 + [300] * 7
    await store.add_observation(
        "solana:good",
        "geckoterminal_ohlcv",
        later,
        {
            "pool_address": "p",
            "candles": [[start + i * 86400, 1, 1, 1, 1, v] for i, v in enumerate(vols)],
        },
    )
    await store.add_observation(
        "solana:good",
        "dexscreener",
        "2026-10-19T23:00:00Z",
        {"price_usd": 1.0, "liquidity_usd": 300_000.0},
    )
    await store.set_token_status("solana:good", "active")
    # positive control: without the no-re-entry rule this token WOULD be admitted
    f = await compute_features(store, settings, "solana:good", later)
    assert disqualify(f, settings) is None and rule_f4(f, settings) is True
    r2 = await rank_once(store, settings, later)
    assert r2.entered == [] and r2.ranked == 0
    assert await store.active_watchlist() == []
    row = await one(
        store,
        "SELECT exited_at, exit_reason, rank FROM watchlist WHERE token_id='solana:good'",
    )
    assert row == ("2026-10-15T07:00:00Z", "liquidity", 1)
    due = await store.due_returns("2028-01-01T00:00:00Z")
    assert {d.due_at[:10] for d in due if d.horizon_days == 1} == {"2026-10-16"}


async def test_watchlist_cap(store, settings):
    settings.watchlist_cap = 2
    for i in range(4):
        await seed_token(store, f"solana:t{i}", vols=[100] * 7 + [300 + i] * 7)
    r = await rank_once(store, settings, A)
    assert r.entered == ["solana:t3", "solana:t2"]
    assert [w.token_id for w in await store.active_watchlist()] == [
        "solana:t3",
        "solana:t2",
    ]


async def test_cap_counts_existing_members_and_reranks_them(store, settings):
    settings.watchlist_cap = 2
    await seed_token(store, "solana:old", vols=[100] * 7 + [210] * 7)
    await rank_once(store, settings, A)
    for i in range(3):
        await seed_token(store, f"solana:n{i}", vols=[100] * 7 + [400 + i] * 7)
    r = await rank_once(store, settings, A)
    # one free slot; the incumbent scores lowest but is still re-ranked
    assert r.entered == ["solana:n2"]
    wl = await store.active_watchlist()
    assert [(w.token_id, w.rank) for w in wl] == [("solana:n2", 1), ("solana:old", 2)]
    assert r.ranked == 2


async def test_cap_below_incumbent_count_admits_none_and_reranks_all(store, settings):
    for i in range(3):
        await seed_token(store, f"solana:i{i}", vols=[100] * 7 + [300 + i] * 7)
    r = await rank_once(store, settings, A)
    assert len(r.entered) == 3
    settings.watchlist_cap = 2  # below the incumbent count: slots <= 0
    # two eligible newcomers: an unclamped negative slot count would slice
    # [:-1] and admit one of them
    await seed_token(store, "solana:new", vols=[100] * 7 + [900] * 7)
    await seed_token(store, "solana:new2", vols=[100] * 7 + [800] * 7)
    r2 = await rank_once(store, settings, A)
    assert r2.entered == [] and r2.exited == [] and r2.ranked == 3
    wl = await store.active_watchlist()
    assert [(w.token_id, w.rank) for w in wl] == [
        ("solana:i2", 1),
        ("solana:i1", 2),
        ("solana:i0", 3),
    ]
    assert await one(
        store, "SELECT COUNT(*) FROM watchlist WHERE token_id LIKE 'solana:new%'"
    ) == (0,)


async def test_score_ties_break_by_token_id(store, settings):
    settings.watchlist_cap = 4
    names = [f"solana:{c}" for c in "fedcba"]  # inserted in reverse order
    for name in names:
        await seed_token(store, name)
    r = await rank_once(store, settings, A)
    assert r.entered == ["solana:a", "solana:b", "solana:c", "solana:d"]
    wl = await store.active_watchlist()
    assert [(w.token_id, w.rank) for w in wl] == [
        ("solana:a", 1),
        ("solana:b", 2),
        ("solana:c", 3),
        ("solana:d", 4),
    ]


def test_rank_job_registered():
    from gecko.__main__ import _load_jobs
    from gecko.jobs import JOBS

    _load_jobs()
    assert "rank" in JOBS


async def test_rank_job_runs_and_records(settings):
    from gecko.__main__ import _load_jobs
    from gecko.jobs import JOBS
    from gecko.store import Store

    _load_jobs()
    s = await Store.open(settings.db_path)
    try:
        await seed_token(s, "solana:good")
    finally:
        await s.close()
    rc = await JOBS["rank"](
        settings, argparse.Namespace(as_of="2026-10-15T00:00:00+00:00")
    )
    assert rc == 0
    s = await Store.open(settings.db_path)
    try:
        wl = await s.active_watchlist()
        assert [w.entered_at for w in wl] == ["2026-10-15T00:00:00Z"]
        row = await one(s, "SELECT job, ok, rows_written FROM runs")
        assert row == ("rank", 1, 1)
    finally:
        await s.close()


def test_unknown_quality_callers_do_not_rank_below_zero_callers():
    # both have caller_signal 0, so the caller term contributes equally
    some = feat(token_id="solana:some", callers_7d=3, caller_quality=None)
    none_ = feat(token_id="solana:none", callers_7d=0)
    assert score(some, STATS) == pytest.approx(score(none_, STATS))


async def test_rank_refuses_as_of_rewind_of_last_ranked(store, settings):
    await seed_token(store, "solana:good")
    await rank_once(store, settings, A)
    before = await one(store, "SELECT rank, score, last_ranked_at FROM watchlist")
    # an active token the rewound run WOULD disqualify: the refusal must
    # come before any status write
    await seed_token(store, "solana:thin", liq=1000.0)
    with pytest.raises(ValueError, match="as_of rewinds ranking state"):
        await rank_once(store, settings, "2026-10-14T23:00:00Z")
    thin = "SELECT status, disqualify_reason FROM tokens WHERE token_id='solana:thin'"
    assert await one(store, thin) == ("active", None)
    # equal as_of (any offset spelling) is allowed and idempotent
    r = await rank_once(store, settings, "2026-10-15T00:00:00+00:00")
    assert r.entered == [] and r.exited == [] and r.ranked == 1
    assert await one(store, thin) == ("disqualified", "liquidity")
    assert (
        await one(store, "SELECT rank, score, last_ranked_at FROM watchlist") == before
    )
    due = await store.due_returns("2028-01-01T00:00:00Z")
    assert len(due) == 5


async def test_rank_refuses_as_of_rewind_of_exit(store, settings):
    await seed_token(store, "solana:good")
    await rank_once(store, settings, A)
    await store.add_observation(
        "solana:good",
        "dexscreener",
        "2026-10-15T06:00:00Z",
        {"price_usd": 1.0, "liquidity_usd": 10.0},
    )
    await rank_once(store, settings, "2026-10-15T07:00:00Z")
    # last_ranked_at stays at A (the exited row is not re-ranked); exited_at is 07:00
    assert await one(store, "SELECT last_ranked_at, exited_at FROM watchlist") == (
        A,
        "2026-10-15T07:00:00Z",
    )
    status_before = await one(store, "SELECT status, disqualify_reason FROM tokens")
    with pytest.raises(ValueError, match="as_of rewinds ranking state"):
        await rank_once(store, settings, "2026-10-15T06:59:59Z")
    # refused before any write
    assert (
        await one(store, "SELECT status, disqualify_reason FROM tokens")
        == status_before
    )


async def test_rank_job_exits_1_on_rewind(settings):
    from gecko.__main__ import _load_jobs
    from gecko.jobs import JOBS
    from gecko.store import Store

    _load_jobs()
    s = await Store.open(settings.db_path)
    try:
        await seed_token(s, "solana:good")
        await rank_once(s, settings, A)
    finally:
        await s.close()
    rc = await JOBS["rank"](settings, argparse.Namespace(as_of="2026-10-14T00:00:00Z"))
    assert rc == 1
    s = await Store.open(settings.db_path)
    try:
        row = await one(s, "SELECT ok, error FROM runs WHERE job='rank'")
        assert row[0] == 0 and "as_of rewinds ranking state" in row[1]
    finally:
        await s.close()


async def test_activation_stamped_once_then_as_of_refused(
    settings, monkeypatch, capsys
):
    # F1: the first live run stamps activation:at once; replays are then refused
    from gecko.rank import ACTIVATION_KEY, rank_job
    from gecko.store import Store

    ns = argparse.Namespace
    assert await rank_job(settings, ns(as_of=A)) == 0  # before activation: allowed
    for t in ("2026-10-16T00:00:00Z", "2026-10-17T00:00:00Z", None):
        if t is None:
            assert await rank_job(settings, ns(as_of="2026-10-18T00:00:00Z")) == 2
            continue
        monkeypatch.setattr("gecko.rank.utcnow", lambda t=t: t)
        assert await rank_job(settings, ns(as_of=None)) == 0
    assert "--as-of is refused" in capsys.readouterr().err
    s = await Store.open(settings.db_path)
    try:
        assert await s.kv_get(ACTIVATION_KEY) == "2026-10-16T00:00:00Z"
        assert await one(s, "SELECT COUNT(*) FROM runs WHERE job='rank'") == (3,)
    finally:
        await s.close()


async def test_exit_reason_falls_back_to_stored_disqualify_reason(store, settings):
    await seed_token(store, "solana:good")
    await rank_once(store, settings, A)
    # the ledger finalized a total loss between ranks
    await store.set_token_status("solana:good", "dead", "no_price")
    r = await rank_once(store, settings, "2026-10-15T01:00:00Z")
    assert r.exited == ["solana:good"]
    row = await one(
        store, "SELECT exit_reason FROM watchlist WHERE token_id='solana:good'"
    )
    assert row == ("no_price",)


async def test_crash_before_scheduling_is_repaired_next_run(
    store, settings, monkeypatch
):
    await seed_token(store, "solana:good")
    real = store.repair_schedules

    async def boom(*a, **k):
        raise RuntimeError("crash between upsert and scheduling")

    monkeypatch.setattr(store, "repair_schedules", boom)
    with pytest.raises(RuntimeError, match="crash between"):
        await rank_once(store, settings, A)
    monkeypatch.setattr(store, "repair_schedules", real)
    assert (await one(store, "SELECT COUNT(*) FROM forward_returns")) == (0,)
    await rank_once(store, settings, "2026-10-15T00:30:00Z")
    rows = await (
        await store.conn.execute(
            "SELECT horizon_days, due_at FROM forward_returns ORDER BY horizon_days"
        )
    ).fetchall()
    assert [h for h, _ in rows] == [1, 7, 30, 90, 180]
    assert rows[0][1] == "2026-10-16T00:00:00Z"  # from the stored entered_at


async def test_crashed_entrant_that_exits_next_run_is_still_scheduled(
    store, settings, monkeypatch
):
    await seed_token(store, "solana:good")
    real = store.repair_schedules

    async def boom(*a, **k):
        raise RuntimeError("crash between upsert and scheduling")

    monkeypatch.setattr(store, "repair_schedules", boom)
    with pytest.raises(RuntimeError, match="crash between"):
        await rank_once(store, settings, A)
    monkeypatch.setattr(store, "repair_schedules", real)
    # liquidity collapses before the next rank: the entrant exits that run
    await store.add_observation(
        "solana:good",
        "dexscreener",
        "2026-10-15T00:20:00Z",
        {"price_usd": 1.0, "liquidity_usd": 10.0},
    )
    r = await rank_once(store, settings, "2026-10-15T00:30:00Z")
    assert r.exited == ["solana:good"]
    rows = await (
        await store.conn.execute(
            "SELECT horizon_days, due_at FROM forward_returns ORDER BY horizon_days"
        )
    ).fetchall()
    assert [h for h, _ in rows] == [1, 7, 30, 90, 180]
    assert rows[0][1] == "2026-10-16T00:00:00Z"


def test_disqualifier_order_structural_before_tax(settings):
    # both flags fired: structural is checked first and is terminal
    both = feat(security_ok=False, security_structural=False, top10_pct=99.0)
    assert disqualify(both, settings) == "security_fail"
    # tax-only (structural clean or unknown) and nothing else fires: security_tax
    tax = feat(security_ok=False, security_structural=True)
    assert disqualify(tax, settings) == "security_tax"
    assert disqualify(feat(security_ok=False), settings) == "security_tax"
    # a structural flag disqualifies even when security_ok is not False
    assert disqualify(feat(security_structural=False), settings) == "security_fail"


async def _status(store, tid):
    return await one(
        store, f"SELECT status, disqualify_reason FROM tokens WHERE token_id='{tid}'"
    )


async def test_tax_rise_exits_watchlist_with_security_tax(store, settings):
    await seed_token(store, "solana:good")
    await store.add_observation(
        "solana:good", "enrich", "2026-10-14T00:00:00Z", {"security_ok": True}
    )
    r = await rank_once(store, settings, A)
    assert r.entered == ["solana:good"] and r.exited_tax == 0
    # the newest enrich read is a tax-only failure
    await store.add_observation(
        "solana:good",
        "enrich",
        "2026-10-15T00:30:00Z",
        {"security_ok": False, "security_structural": True},
    )
    r = await rank_once(store, settings, "2026-10-15T01:00:00Z")
    assert r.exited == ["solana:good"] and r.exited_tax == 1
    row = await one(
        store, "SELECT exit_reason FROM watchlist WHERE token_id='solana:good'"
    )
    assert row == ("security_tax",)
    assert await _status(store, "solana:good") == ("candidate", "security_tax")
    assert await store.active_watchlist() == []


async def test_tax_fail_candidate_is_not_disqualified_by_rank(store, settings):
    await seed_token(store, "solana:taxed")
    await store.add_observation(
        "solana:taxed",
        "enrich",
        "2026-10-14T00:00:00Z",
        {"security_ok": False, "security_structural": True},
    )
    r = await rank_once(store, settings, A)
    # not admitted, not exited, and its status is left exactly as it was
    assert r.entered == [] and r.exited == [] and r.exited_tax == 0
    assert await store.active_watchlist() == []
    assert await _status(store, "solana:taxed") == ("active", None)


async def test_structural_flag_disqualifies(store, settings):
    await seed_token(store, "solana:good")
    await rank_once(store, settings, A)
    await store.add_observation(
        "solana:good",
        "enrich",
        "2026-10-15T00:30:00Z",
        {"security_ok": False, "security_structural": False},
    )
    r = await rank_once(store, settings, "2026-10-15T01:00:00Z")
    assert r.exited == ["solana:good"] and r.exited_tax == 0
    row = await one(
        store, "SELECT exit_reason FROM watchlist WHERE token_id='solana:good'"
    )
    assert row == ("security_fail",)
    assert await _status(store, "solana:good") == ("disqualified", "security_fail")


def test_dead_or_concentrated_beats_tax(settings):
    # a terminal gate retires a tax-flagged token instead of re-reading it forever
    tax = dict(security_ok=False, security_structural=True)
    assert disqualify(feat(top10_pct=99.0, **tax), settings) == "concentration"
    eight_days = "2026-10-07T00:00:00Z"  # 8 d before A
    assert disqualify(feat(observed_at=eight_days, **tax), settings) == "dead"


async def test_tax_demotion_never_overwrites_concurrent_enrich_verdict(
    store, settings, monkeypatch
):
    # B1: enrich commits a structural disqualification after rank read the
    # token as active and as tax-only; the demotion must not overwrite it.
    from gecko import rank

    await seed_token(store, "solana:good")
    await rank_once(store, settings, A)
    tax = {"security_ok": False, "security_structural": True}
    await store.add_observation("solana:good", "enrich", "2026-10-15T00:30:00Z", tax)
    real = rank.compute_features

    async def racing(store_, settings_, tid, as_of):
        f = await real(store_, settings_, tid, as_of)
        await store.set_token_status(tid, "disqualified", "security_fail")
        return f

    monkeypatch.setattr(rank, "compute_features", racing)
    r = await rank_once(store, settings, "2026-10-15T01:00:00Z")
    assert r.exited == ["solana:good"] and r.exited_tax == 0
    row = await one(store, "SELECT exit_reason FROM watchlist")
    assert row == ("security_fail",)
    assert await _status(store, "solana:good") == ("disqualified", "security_fail")


async def test_security_tax_crash_recovery_exits_next_run(store, settings):
    await seed_token(store, "solana:good")
    await rank_once(store, settings, A)
    # a crash landed between the status demotion and the watchlist exit
    await store.set_token_status("solana:good", "candidate", "security_tax")
    assert [w.token_id for w in await store.active_watchlist()] == ["solana:good"]
    r = await rank_once(store, settings, "2026-10-15T01:00:00Z")
    assert r.exited == ["solana:good"] and r.exited_tax == 1
    row = await one(
        store, "SELECT exit_reason FROM watchlist WHERE token_id='solana:good'"
    )
    assert row == ("security_tax",)
    assert await _status(store, "solana:good") == ("candidate", "security_tax")


async def test_security_tax_exit_keeps_forward_returns_schedule(
    store, settings, monkeypatch
):
    class _Crash(Exception):
        pass

    async def crash():
        raise _Crash

    await seed_token(store, "solana:good")
    # a crash right after the entry: the tax exit is the first chance to schedule
    monkeypatch.setattr(store, "repair_schedules", crash)
    with pytest.raises(_Crash):
        await rank_once(store, settings, A)
    monkeypatch.undo()
    assert await one(store, "SELECT COUNT(*) FROM forward_returns") == (0,)
    await store.add_observation(
        "solana:good",
        "enrich",
        "2026-10-15T00:30:00Z",
        {"security_ok": False, "security_structural": True},
    )
    r = await rank_once(store, settings, "2026-10-15T01:00:00Z")
    assert r.exited_tax == 1
    rows = await (
        await store.conn.execute(
            "SELECT horizon_days, due_at, measured_at, closed_reason "
            "FROM forward_returns WHERE token_id='solana:good' ORDER BY horizon_days"
        )
    ).fetchall()
    assert rows == [(h, iso_plus(A, h), None, None) for h in (1, 7, 30, 90, 180)]
