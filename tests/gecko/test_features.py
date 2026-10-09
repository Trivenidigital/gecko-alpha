import datetime as dt

import pytest
from gecko.features import compute_features, vol_ratio_from_candles

T0 = "2026-10-01T00:00:00Z"
CALL_SQL = (
    "INSERT INTO source_calls(token_id,source_type,source_id,event_id,posted_at,text_hash) "
    "VALUES(?,?,?,?,?,?)"
)
AS_OF_DT = dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)
START_14 = int((AS_OF_DT - dt.timedelta(days=14)).timestamp())


async def seed(store, *, pool_created="2026-09-10T00:00:00Z"):
    await store.upsert_token(
        "solana:abc",
        "solana",
        "abc",
        "ABC",
        "Abc",
        T0,
        "dexscreener",
        pool_created_at=pool_created,
    )


def candles(start_ts: int, vols: list[float]):
    return [[start_ts + i * 86400, 1, 1, 1, 1, v] for i, v in enumerate(vols)]


def iso(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


async def test_age_and_market_fields(store, settings):
    await seed(store)
    await store.add_observation(
        "solana:abc",
        "dexscreener",
        "2026-10-05T00:00:00Z",
        {"price_usd": 2.0, "mcap_usd": 1e6, "liquidity_usd": 3e5, "vol_24h_usd": 5e4},
    )
    f = await compute_features(store, settings, "solana:abc", "2026-10-05T12:00:00Z")
    assert f.price_usd == 2.0 and f.mcap_usd == 1e6 and f.liquidity_usd == 3e5
    assert f.age_days == pytest.approx(25.5, abs=0.01)
    assert f.observed_at == "2026-10-05T00:00:00Z"


async def test_no_lookahead(store, settings):
    await seed(store)
    await store.add_observation(
        "solana:abc", "dexscreener", "2026-10-05T00:00:00Z", {"price_usd": 2.0}
    )
    await store.add_observation(
        "solana:abc", "dexscreener", "2026-10-07T00:00:00Z", {"price_usd": 99.0}
    )
    f = await compute_features(store, settings, "solana:abc", "2026-10-06T00:00:00Z")
    assert f.price_usd == 2.0


async def test_vol_ratio_from_ohlcv(store, settings):
    await seed(store)
    # 14 daily candles ending the day before as_of: prior week 100/day, last week 250/day
    await store.add_observation(
        "solana:abc",
        "geckoterminal_ohlcv",
        "2026-10-15T00:00:00Z",
        {"pool_address": "p", "candles": candles(START_14, [100] * 7 + [250] * 7)},
    )
    f = await compute_features(store, settings, "solana:abc", "2026-10-15T00:00:00Z")
    assert f.vol_7d_ratio == pytest.approx(2.5)


async def test_vol_ratio_computed_from_short_history(store, settings):
    # Ruling 2026-10-09: prereg f7d38fa2 has no candle minimum. 10 candles from
    # t-10d: prior window holds t-10..t-8 (3 x 100), last window 7 x 100.
    await seed(store)
    start = int((AS_OF_DT - dt.timedelta(days=10)).timestamp())
    await store.add_observation(
        "solana:abc",
        "geckoterminal_ohlcv",
        "2026-10-15T00:00:00Z",
        {"pool_address": "p", "candles": candles(start, [100] * 10)},
    )
    f = await compute_features(store, settings, "solana:abc", "2026-10-15T00:00:00Z")
    assert f.vol_7d_ratio == pytest.approx(700 / 300)


async def test_vol_ratio_missing_when_prior_zero(store, settings):
    await seed(store)
    await store.add_observation(
        "solana:abc",
        "geckoterminal_ohlcv",
        "2026-10-15T00:00:00Z",
        {"pool_address": "p", "candles": candles(START_14, [0] * 7 + [250] * 7)},
    )
    f = await compute_features(store, settings, "solana:abc", "2026-10-15T00:00:00Z")
    assert f.vol_7d_ratio is None


async def test_vol_ratio_skips_empty_marker_to_older_candles(store, settings):
    await seed(store)
    await store.add_observation(
        "solana:abc",
        "geckoterminal_ohlcv",
        "2026-10-14T00:00:00Z",
        {"pool_address": "p", "candles": candles(START_14, [100] * 7 + [250] * 7)},
    )
    for i, status in enumerate(["empty", "not_found", "ok"]):
        await store.add_observation(
            "solana:abc",
            "geckoterminal_ohlcv",
            f"2026-10-14T0{i + 1}:00:00Z",
            {"pool_address": "p", "candles": [], "status": status},
        )
    f = await compute_features(store, settings, "solana:abc", "2026-10-15T00:00:00Z")
    assert f.vol_7d_ratio == pytest.approx(2.5)


async def test_vol_ratio_none_when_only_markers(store, settings):
    await seed(store)
    await store.add_observation(
        "solana:abc",
        "geckoterminal_ohlcv",
        "2026-10-14T00:00:00Z",
        {"pool_address": "p", "candles": [], "status": "not_found"},
    )
    f = await compute_features(store, settings, "solana:abc", "2026-10-15T00:00:00Z")
    assert f.vol_7d_ratio is None


def test_vol_ratio_skips_malformed_candles():
    good = candles(START_14, [100] * 7 + [250] * 7)
    bad = [
        [START_14 + 3 * 86400 + 60, 1, 1, 1, 1],  # 5 elements
        [START_14 + 10 * 86400 + 60, 1, 1, 1, 1, float("nan")],  # nan volume
    ]
    assert len(good + bad) == 16
    assert vol_ratio_from_candles(good + bad, AS_OF_DT) == pytest.approx(2.5)
    # 13 usable candles still compute (no minimum); the malformed ones add nothing
    assert vol_ratio_from_candles(good[1:] + bad, AS_OF_DT) == pytest.approx(1750 / 600)


def test_vol_ratio_skips_non_finite_timestamp_and_non_numeric():
    good = candles(START_14, [100] * 7 + [250] * 7)
    bad = [
        [float("inf"), 1, 1, 1, 1, 5],
        [START_14 + 60, 1, 1, 1, 1, "500"],
        [START_14 + 120, 1, 1, 1, 1, True],
        "not-a-candle",
    ]
    assert vol_ratio_from_candles(good + bad, AS_OF_DT) == pytest.approx(2.5)
    # the "500" and True volumes would land in the prior week if counted
    assert vol_ratio_from_candles(good[1:] + bad, AS_OF_DT) == pytest.approx(1750 / 600)


async def test_vol_ratio_survives_malformed_candle_in_store(store, settings):
    await seed(store)
    raw = candles(START_14, [100] * 7 + [250] * 7)
    raw.insert(5, [START_14 + 5 * 86400 + 60, 1, 1, 1, 1, float("nan")])
    await store.add_observation(
        "solana:abc",
        "geckoterminal_ohlcv",
        "2026-10-15T00:00:00Z",
        {"pool_address": "p", "candles": raw},
    )
    f = await compute_features(store, settings, "solana:abc", "2026-10-15T00:00:00Z")
    assert f.vol_7d_ratio == pytest.approx(2.5)


async def test_momentum_ctx_despite_dense_recent_obs(store, settings):
    await seed(store)
    await store.add_observation(
        "solana:abc", "dexscreener", "2026-10-01T00:00:00Z", {"price_usd": 1.0}
    )
    # 100 observations in the last day: more than any per-source row cap
    base = dt.datetime(2026, 10, 8, tzinfo=dt.timezone.utc)
    for i in range(100):
        ts = iso(base + dt.timedelta(minutes=10 * i))
        await store.add_observation("solana:abc", "dexscreener", ts, {"price_usd": 3.0})
    f = await compute_features(store, settings, "solana:abc", "2026-10-09T00:00:00Z")
    assert f.price_usd == 3.0
    assert f.momentum_ctx == pytest.approx(2.0)


async def test_momentum_ctx_none_without_week_old_price(store, settings):
    await seed(store)
    await store.add_observation(
        "solana:abc", "dexscreener", "2026-10-05T00:00:00Z", {"price_usd": 2.0}
    )
    f = await compute_features(store, settings, "solana:abc", "2026-10-09T00:00:00Z")
    assert f.momentum_ctx is None


async def test_holder_growth_and_bundle_flag(store, settings):
    await seed(store)
    await store.add_observation(
        "solana:abc",
        "enrich",
        "2026-10-01T00:00:00Z",
        {"holders": 1000, "top10_pct": 20},
    )
    await store.add_observation(
        "solana:abc",
        "enrich",
        "2026-10-08T00:00:00Z",
        {"holders": 1500, "top10_pct": 45, "dd_score": 80},
    )
    f = await compute_features(store, settings, "solana:abc", "2026-10-08T06:00:00Z")
    assert f.holders == 1500 and f.holder_growth_7d == pytest.approx(0.5)
    assert f.top10_pct == 45 and f.bundle_flag is True  # top10 > 40


async def test_holder_growth_despite_dense_recent_enrich(store, settings):
    await seed(store)
    await store.add_observation(
        "solana:abc", "enrich", "2026-10-01T00:00:00Z", {"holders": 1000}
    )
    base = dt.datetime(2026, 10, 7, tzinfo=dt.timezone.utc)
    for i in range(50):
        ts = iso(base + dt.timedelta(minutes=30 * i))
        await store.add_observation("solana:abc", "enrich", ts, {"holders": 2000})
    f = await compute_features(store, settings, "solana:abc", "2026-10-08T06:00:00Z")
    assert f.holders == 2000 and f.holder_growth_7d == pytest.approx(1.0)


async def test_bundle_flag_from_low_dd_score(store, settings):
    await seed(store)
    await store.add_observation(
        "solana:abc",
        "enrich",
        "2026-10-08T00:00:00Z",
        {"top10_pct": 10, "dd_score": 30, "security_ok": False},
    )
    f = await compute_features(store, settings, "solana:abc", "2026-10-08T06:00:00Z")
    assert f.bundle_flag is True and f.security_ok is False


async def test_security_unknown_when_no_enrich(store, settings):
    await seed(store)
    f = await compute_features(store, settings, "solana:abc", "2026-10-08T00:00:00Z")
    assert f.security_ok is None and f.holders is None and f.bundle_flag is False


async def test_callers_and_quality(store, settings):
    await seed(store)
    await store.upsert_token(
        "solana:old", "solana", "old", "OLD", "Old", "2026-08-01T00:00:00Z", "telegram"
    )
    # caller c1 called OLD on 08-01; OLD's 30d return beat BTC -> hit
    await store.conn.execute(
        CALL_SQL,
        ("solana:old", "telegram", "c1", "e1", "2026-08-01T00:00:00Z", "h"),
    )
    await store.schedule_returns("solana:old", "2026-08-01T00:00:00Z")
    await store.set_return(
        "solana:old", 30, 1.0, 2.0, 100.0, 110.0, "2026-08-31T00:00:00Z"
    )
    for i, src in enumerate(["c1", "c1", "c2"]):
        await store.conn.execute(
            CALL_SQL,
            ("solana:abc", "telegram", src, f"e{i+2}", "2026-10-03T00:00:00Z", "h"),
        )
    await store.conn.execute(
        CALL_SQL,
        ("solana:abc", "x", "kol1", "x1", "2026-10-04T00:00:00Z", "h"),
    )
    await store.conn.commit()
    f = await compute_features(store, settings, "solana:abc", "2026-10-05T00:00:00Z")
    assert f.callers_7d == 2  # c1, c2 (telegram only)
    assert f.caller_quality == pytest.approx(1.0)  # c1 hit rate 1.0; c2 no history
    assert f.narrative_mentions_7d == 1


async def test_caller_quality_counts_each_called_token_once(store, settings):
    await seed(store)
    for tid, price in (("solana:hit", 2.0), ("solana:miss", 0.5)):
        await store.upsert_token(
            tid, "solana", tid[7:], "X", "X", "2026-08-01T00:00:00Z", "telegram"
        )
        await store.schedule_returns(tid, "2026-08-01T00:00:00Z")
        await store.set_return(
            tid, 30, 1.0, price, 100.0, 100.0, "2026-08-31T00:00:00Z"
        )
    # c1 called the winner three times and the loser once: 1 hit out of 2 tokens
    for i, tid in enumerate(["solana:hit"] * 3 + ["solana:miss"]):
        await store.conn.execute(
            CALL_SQL, (tid, "telegram", "c1", f"h{i}", "2026-08-01T00:00:00Z", "h")
        )
    await store.conn.execute(
        CALL_SQL, ("solana:abc", "telegram", "c1", "now", "2026-10-03T00:00:00Z", "h")
    )
    await store.conn.commit()
    f = await compute_features(store, settings, "solana:abc", "2026-10-05T00:00:00Z")
    assert f.caller_quality == pytest.approx(0.5)


async def test_callers_no_lookahead(store, settings):
    await seed(store)
    await store.upsert_token(
        "solana:old", "solana", "old", "OLD", "Old", "2026-08-01T00:00:00Z", "telegram"
    )
    await store.conn.execute(
        CALL_SQL, ("solana:old", "telegram", "c1", "e1", "2026-08-01T00:00:00Z", "h")
    )
    await store.schedule_returns("solana:old", "2026-08-01T00:00:00Z")
    # measured after as_of: must not inform caller_quality
    await store.set_return(
        "solana:old", 30, 1.0, 2.0, 100.0, 100.0, "2026-10-06T00:00:00Z"
    )
    await store.conn.execute(
        CALL_SQL, ("solana:abc", "telegram", "c1", "e2", "2026-10-03T00:00:00Z", "h")
    )
    # posted after as_of: must not count
    await store.conn.execute(
        CALL_SQL, ("solana:abc", "telegram", "c2", "e3", "2026-10-05T00:00:01Z", "h")
    )
    await store.conn.execute(
        CALL_SQL, ("solana:abc", "x", "kol", "x1", "2026-10-06T00:00:00Z", "h")
    )
    await store.conn.commit()
    f = await compute_features(store, settings, "solana:abc", "2026-10-05T00:00:00Z")
    assert f.callers_7d == 1
    assert f.caller_quality is None
    assert f.narrative_mentions_7d == 0


async def test_young_chain_flag(store, settings):
    settings.young_chain_launch_dates = {"solana": "2026-09-01"}
    await seed(store)
    f = await compute_features(store, settings, "solana:abc", "2026-10-05T00:00:00Z")
    assert f.young_chain is True
    settings.young_chain_launch_dates = {}
    f = await compute_features(store, settings, "solana:abc", "2026-10-05T00:00:00Z")
    assert f.young_chain is False


async def test_unknown_token_returns_empty_features(store, settings):
    f = await compute_features(store, settings, "solana:nope", "2026-10-05T00:00:00Z")
    assert f.token_id == "solana:nope" and f.price_usd is None and f.age_days is None
    assert f.as_dict()["callers_7d"] == 0


# --- review round 1 ---------------------------------------------------------

DAY = 86400
T_SEC = int(AS_OF_DT.timestamp())


def test_vol_ratio_none_when_candles_end_before_window():
    # 30 candles, the last one 11 days before as_of: 4 fall in the prior week
    # and none in the last week -> None by the at-least-one-recent-candle rule
    assert (
        vol_ratio_from_candles(candles(T_SEC - 40 * DAY, [100] * 30), AS_OF_DT) is None
    )


def test_vol_ratio_ignores_old_history_and_has_no_minimum():
    # Ruling 2026-10-09: prereg f7d38fa2 defines vol_ratio as plain sums over the
    # two 7-day windows with no candle minimum, so one candle per week computes.
    # Old history outside [t-14d, t) contributes nothing.
    old = candles(T_SEC - 80 * DAY, [100] * 50)
    recent = [[T_SEC - 10 * DAY, 1, 1, 1, 1, 100], [T_SEC - 3 * DAY, 1, 1, 1, 1, 250]]
    assert vol_ratio_from_candles(old + recent, AS_OF_DT) == pytest.approx(2.5)


def test_vol_ratio_excludes_candle_stamped_at_as_of():
    # [t-7d, t): a candle stamped exactly at as_of is lookahead and must not count
    good = candles(START_14, [100] * 7 + [250] * 7)
    at_t = [T_SEC, 1, 1, 1, 1, 10_000]
    assert vol_ratio_from_candles(good + [at_t], AS_OF_DT) == pytest.approx(2.5)


def test_vol_ratio_duplicate_timestamps_do_not_inflate():
    good = candles(START_14, [100] * 7 + [250] * 7)
    dups = [list(c) for c in good[7:]]  # 7 duplicated last-week timestamps
    assert vol_ratio_from_candles(good + dups, AS_OF_DT) == pytest.approx(2.5)
    # 7 unique last-week timestamps, each twice, no prior week -> None
    assert vol_ratio_from_candles(good[7:] + dups, AS_OF_DT) is None


async def test_vol_ratio_scan_skips_all_malformed_candle_obs(store, settings):
    await seed(store)
    await store.add_observation(
        "solana:abc",
        "geckoterminal_ohlcv",
        "2026-10-14T00:00:00Z",
        {"pool_address": "p", "candles": candles(START_14, [100] * 7 + [250] * 7)},
    )
    await store.add_observation(
        "solana:abc",
        "geckoterminal_ohlcv",
        "2026-10-14T06:00:00Z",
        {"pool_address": "p", "candles": [[1, 2], [T_SEC - DAY, 1, 1, 1, 1, "x"]]},
    )
    f = await compute_features(store, settings, "solana:abc", "2026-10-15T00:00:00Z")
    assert f.vol_7d_ratio == pytest.approx(2.5)


async def test_holder_growth_none_when_then_value_too_old(store, settings):
    await seed(store)
    await store.add_observation(
        "solana:abc", "enrich", "2026-08-09T06:00:00Z", {"holders": 1000}
    )
    await store.add_observation(
        "solana:abc", "enrich", "2026-10-08T00:00:00Z", {"holders": 1500}
    )
    f = await compute_features(store, settings, "solana:abc", "2026-10-08T06:00:00Z")
    assert f.holders == 1500 and f.holder_growth_7d is None


async def test_holder_growth_computed_from_eight_day_old_value(store, settings):
    await seed(store)
    await store.add_observation(
        "solana:abc", "enrich", "2026-09-30T06:00:00Z", {"holders": 1000}
    )
    await store.add_observation(
        "solana:abc", "enrich", "2026-10-08T00:00:00Z", {"holders": 1500}
    )
    f = await compute_features(store, settings, "solana:abc", "2026-10-08T06:00:00Z")
    assert f.holder_growth_7d == pytest.approx(0.5)


async def test_holder_growth_none_when_now_value_stale(store, settings):
    await seed(store)
    await store.add_observation(
        "solana:abc", "enrich", "2026-10-01T00:00:00Z", {"holders": 1000}
    )
    await store.add_observation(
        "solana:abc", "enrich", "2026-10-05T00:00:00Z", {"holders": 1500}
    )
    f = await compute_features(store, settings, "solana:abc", "2026-10-08T06:00:00Z")
    assert f.holders == 1500 and f.holder_growth_7d is None


async def test_momentum_ctx_none_when_then_price_too_old(store, settings):
    await seed(store)
    await store.add_observation(
        "solana:abc", "dexscreener", "2026-08-09T00:00:00Z", {"price_usd": 1.0}
    )
    await store.add_observation(
        "solana:abc", "dexscreener", "2026-10-08T00:00:00Z", {"price_usd": 3.0}
    )
    f = await compute_features(store, settings, "solana:abc", "2026-10-08T06:00:00Z")
    assert f.price_usd == 3.0 and f.momentum_ctx is None


async def test_deepest_pool_represents_token_at_same_timestamp(store, settings):
    await seed(store)
    ts = "2026-10-05T00:00:00Z"
    for liq, price in ((1e3, 9.0), (3e5, 2.0)):
        await store.add_observation(
            "solana:abc",
            "geckoterminal",
            ts,
            {"liquidity_usd": liq, "price_usd": price, "mcap_usd": liq * 10},
        )
    await store.add_observation(
        "solana:abc", "geckoterminal", ts, {"price_usd": 5.0}
    )  # no liquidity: sorts last
    f = await compute_features(store, settings, "solana:abc", "2026-10-05T12:00:00Z")
    assert f.liquidity_usd == 3e5 and f.price_usd == 2.0 and f.mcap_usd == 3e6


async def test_caller_quality_excludes_the_scored_token(store, settings):
    await seed(store)
    await store.conn.execute(
        CALL_SQL, ("solana:abc", "telegram", "c1", "e1", "2026-08-01T00:00:00Z", "h")
    )
    await store.schedule_returns("solana:abc", "2026-08-01T00:00:00Z")
    await store.set_return(
        "solana:abc", 30, 1.0, 2.0, 100.0, 100.0, "2026-08-31T00:00:00Z"
    )
    await store.conn.execute(
        CALL_SQL, ("solana:abc", "telegram", "c1", "e2", "2026-10-03T00:00:00Z", "h")
    )
    await store.conn.commit()
    f = await compute_features(store, settings, "solana:abc", "2026-10-05T00:00:00Z")
    assert f.callers_7d == 1 and f.caller_quality is None


async def test_age_clamped_and_young_chain_false_before_launch(store, settings):
    settings.young_chain_launch_dates = {"solana": "2026-10-01"}
    await seed(store)  # first_seen 10-01, pool created 09-10
    f = await compute_features(store, settings, "solana:abc", "2026-09-05T00:00:00Z")
    assert f.age_days == 0.0
    assert f.young_chain is False


async def test_security_ok_only_from_real_bools(store, settings):
    await seed(store)
    for i, val in enumerate(["false", 1]):
        await store.add_observation(
            "solana:abc", "enrich", f"2026-10-0{i + 1}T00:00:00Z", {"security_ok": val}
        )
        f = await compute_features(
            store, settings, "solana:abc", f"2026-10-0{i + 1}T06:00:00Z"
        )
        assert f.security_ok is None


async def test_unparseable_numbers_become_none(store, settings):
    await seed(store)
    await store.add_observation(
        "solana:abc",
        "dexscreener",
        "2026-10-05T00:00:00Z",
        {"price_usd": "abc", "mcap_usd": None, "liquidity_usd": [1], "vol_24h_usd": 7},
    )
    await store.add_observation(
        "solana:abc",
        "enrich",
        "2026-10-05T00:00:00Z",
        {
            "holders": "lots",
            "top10_pct": "x",
            "dd_score": {},
            "smart_money_wallets": "n",
        },
    )
    f = await compute_features(store, settings, "solana:abc", "2026-10-05T06:00:00Z")
    # an unparseable price leaves the row unpriced: it describes nothing
    assert f.observed_at is None and f.price_usd is None and f.vol_24h_usd is None
    await store.add_observation(
        "solana:abc",
        "dexscreener",
        "2026-10-05T01:00:00Z",
        {"price_usd": "1.5", "mcap_usd": "x", "liquidity_usd": [1], "vol_24h_usd": 7},
    )
    f = await compute_features(store, settings, "solana:abc", "2026-10-05T06:00:00Z")
    assert f.price_usd == 1.5 and f.liquidity_usd is None and f.vol_24h_usd == 7.0
    assert f.mcap_usd is None
    assert f.holders is None and f.top10_pct is None and f.smart_money_wallets is None
    assert f.bundle_flag is False


async def test_gates_read_one_priced_row_not_per_key_fallthrough(store, settings):
    await seed(store)
    await store.add_observation(
        "solana:abc",
        "dexscreener",
        "2026-10-04T00:00:00Z",
        {"price_usd": 1.0, "liquidity_usd": 3e5, "mcap_usd": 1e6, "vol_24h_usd": 9e4},
    )
    # newest priced row lacks liquidity: an older healthy reading must not fill it
    await store.add_observation(
        "solana:abc", "geckoterminal", "2026-10-05T00:00:00Z", {"price_usd": 1.1}
    )
    f = await compute_features(store, settings, "solana:abc", "2026-10-05T06:00:00Z")
    assert f.observed_at == "2026-10-05T00:00:00Z" and f.price_usd == 1.1
    assert f.liquidity_usd is None and f.vol_24h_usd is None


async def test_trending_row_does_not_refresh_observed_at(store, settings):
    await seed(store)
    await store.add_observation(
        "solana:abc",
        "dexscreener",
        "2026-10-01T00:00:00Z",
        {"price_usd": 1.0, "liquidity_usd": 3e5},
    )
    await store.add_observation(
        "solana:abc", "coingecko", "2026-10-09T00:00:00Z", {"trending_rank": 1}
    )
    f = await compute_features(store, settings, "solana:abc", "2026-10-09T06:00:00Z")
    assert f.observed_at == "2026-10-01T00:00:00Z" and f.liquidity_usd == 3e5


async def test_cg_markets_row_never_gates_a_dex_token(store, settings):
    await seed(store)
    await store.add_observation(
        "solana:abc",
        "dexscreener",
        "2026-10-05T00:00:00Z",
        {"price_usd": 1.0, "liquidity_usd": 3e5},
    )
    await store.add_observation(
        "solana:abc",
        "coingecko",
        "2026-10-05T01:00:00Z",
        {"price_usd": 1.2, "mcap_usd": 4e6, "vol_24h_usd": 1.0},
    )
    f = await compute_features(store, settings, "solana:abc", "2026-10-05T06:00:00Z")
    assert f.observed_at == "2026-10-05T00:00:00Z" and f.liquidity_usd == 3e5
    assert f.price_usd == 1.0 and f.vol_24h_usd is None
    assert f.mcap_usd == 4e6  # mcap alone falls back to CoinGecko
    await store.add_observation(
        "solana:abc",
        "dexscreener",
        "2026-10-05T02:00:00Z",
        {"price_usd": 1.0, "liquidity_usd": 3e5, "mcap_usd": 2e6},
    )
    f = await compute_features(store, settings, "solana:abc", "2026-10-05T06:00:00Z")
    assert f.mcap_usd == 2e6  # the priced row's own mcap wins


async def test_cg_only_token_described_by_cg_row(store, settings):
    await seed(store)
    await store.add_observation(
        "solana:abc",
        "coingecko",
        "2026-10-05T00:00:00Z",
        {"price_usd": 1.2, "mcap_usd": 4e6},
    )
    f = await compute_features(store, settings, "solana:abc", "2026-10-05T06:00:00Z")
    assert f.price_usd == 1.2 and f.mcap_usd == 4e6 and f.liquidity_usd is None
