import json
import sqlite3
from datetime import datetime, timezone

import pytest

from gecko.backtest.study import (
    apply_rule,
    battery,
    f4_divergence,
    load_study,
    study_vol_ratio,
)

DAY = 86400
BASE_SEC = 1_775_001_600  # 2026-04-01T00:00:00Z


def _iso(sec: float, z: bool = False) -> str:
    fmt = "%Y-%m-%dT%H:%M:%SZ" if z else "%Y-%m-%dT%H:%M:%S"
    return datetime.fromtimestamp(sec, tz=timezone.utc).strftime(fmt)


def _dump(obj, path) -> None:
    with open(path, "w") as fh:
        json.dump(obj, fh)


def write_study(tmp_path, coins: dict):
    """coins: cid -> dict(t0, prices:[(day_offset, price)], stored, age_days, vols)"""
    base_ms = BASE_SEC * 1000
    (tmp_path / "charts").mkdir()
    (tmp_path / "v2" / "coins").mkdir(parents=True)
    events = {}
    btc = {"prices": [[base_ms + d * DAY * 1000, 100.0 + d] for d in range(200)]}
    db = sqlite3.connect(tmp_path / "scout.db")
    db.execute(
        "CREATE TABLE predictions(coin_id TEXT, predicted_at TEXT, price_at_prediction REAL)"
    )
    for cid, c in coins.items():
        events[cid] = [[c["t0"], "prediction"]]
        _dump(
            {"prices": [[base_ms + d * DAY * 1000, p] for d, p in c["prices"]]},
            tmp_path / "charts" / f"{cid}.json",
        )
        if c["stored"] is not False:
            db.execute(
                "INSERT INTO predictions VALUES(?,?,?)", (cid, c["t0"], c["stored"])
            )
        t0_sec = BASE_SEC + c["t0_day"] * DAY
        pc = t0_sec - c["age_days"] * DAY
        start = t0_sec - 14 * DAY
        _dump(
            {
                "cid": cid,
                "t0": c["t0"],
                "platforms": ["solana"],
                "net": "solana",
                "pool_created_at": _iso(pc, z=True),
                "ohlcv": [
                    [start + i * DAY, 1, 1, 1, 1, v] for i, v in enumerate(c["vols"])
                ],
            },
            tmp_path / "v2" / "coins" / f"{cid}.json",
        )
    db.commit()
    db.close()
    _dump(events, tmp_path / "events.json")
    _dump(btc, tmp_path / "btc.json")


def coin(t0_day, p0, x, *, stored=None, age_days=30, vols=None, prices=None):
    return dict(
        t0=_iso(BASE_SEC + t0_day * DAY),
        t0_day=t0_day,
        prices=prices or [(t0_day, p0), (199, p0 * x)],
        stored=p0 if stored is None else stored,
        age_days=age_days,
        vols=vols or [100] * 7 + [300] * 7,
    )


@pytest.fixture
def study(tmp_path, settings):
    write_study(
        tmp_path,
        {
            "winner": coin(10, 1.0, 8.0),
            "loser": coin(12, 1.0, 0.4),
            "old": coin(14, 1.0, 3.0, age_days=200),
            "flat": coin(16, 1.0, 2.0, vols=[100] * 14),
            "badchart": coin(18, 1.0, 50.0, stored=0.01),
            "late": coin(120, 1.0, 2.5),
        },
    )
    return load_study(str(tmp_path), str(tmp_path / "scout.db"))


def test_load_excludes_chart_disagreement_and_sets_features(study):
    cids = {c.cid for c in study}
    assert "badchart" not in cids and len(study) == 5
    w = next(c for c in study if c.cid == "winner")
    assert (
        w.x == pytest.approx(8.0)
        and w.age_days == pytest.approx(30, abs=0.01)
        and w.vol_ratio == pytest.approx(3.0)
    )
    assert w.bx == pytest.approx((100 + 199) / (100 + 10))
    assert w.live_vol_ratio == pytest.approx(3.0)
    assert w.chain == "solana" and w.surf == {"prediction"}


def test_rules(study, settings):
    f4 = {c.cid for c in apply_rule(study, "F4", settings)}
    assert f4 == {"winner", "loser", "late"}
    assert {c.cid for c in apply_rule(study, "F1", settings)} == {
        "winner",
        "loser",
        "flat",
        "late",
    }
    assert {c.cid for c in apply_rule(study, "F3", settings)} == {
        "winner",
        "loser",
        "old",
        "late",
    }
    assert len(apply_rule(study, "R0", settings)) == 5


def test_battery_reports_which_criteria_fail(study, settings):
    sel = apply_rule(study, "F4", settings)
    b = battery(sel, study)
    assert b.n == 3 and b.mean == pytest.approx((8.0 + 0.4 + 2.5) / 3)
    assert b.passes[4] is False  # n < 20
    assert b.passed is False
    assert b.ex_best == pytest.approx((0.4 + 2.5) / 2)
    assert b.median == pytest.approx(2.5)
    assert b.pct_7x == pytest.approx(100 / 3) and b.pct_half == pytest.approx(100 / 3)


def test_battery_same_month_baseline_and_halves(study, settings):
    sel = apply_rule(study, "F4", settings)
    b = battery(sel, study)
    # April universe: winner, loser, old, flat; July: late
    april = (8.0 + 0.4 + 3.0 + 2.0) / 4
    assert b.same_month_r0 == pytest.approx((april * 2 + 2.5) / 3)
    assert b.halves.startswith("[median ")  # a half < 20 -> median split


def test_battery_passes_all_five_on_a_strong_basket():
    from gecko.backtest.study import Coin

    def mk(i, mo, x):
        return Coin(f"c{i}", f"2026-{mo}-15T00:00:00", False, p0=1.0, x=x, bx=1.0)

    sel = [mk(i, "05", 3.0) for i in range(20)] + [
        mk(100 + i, "08", 3.0) for i in range(20)
    ]
    universe = sel + [mk(200 + i, m, 0.5) for i, m in enumerate(["05", "08"] * 10)]
    b = battery(sel, universe)
    assert b.passes == [True] * 5 and b.passed is True
    assert b.halves.startswith("[2026-07-01] ")


def test_battery_median_split_when_a_half_is_small_and_size_cap():
    from gecko.backtest.study import Coin

    def mk(i, t0, x=3.0):
        return Coin(f"c{i}", t0, False, p0=1.0, x=x, bx=1.0)

    # 25 coins before 2026-07-01 and 19 after: the 19-coin half forces a median split
    sel = [mk(i, f"2026-05-{i + 1:02d}T00:00:00") for i in range(25)]
    sel += [mk(100 + i, f"2026-08-{i + 1:02d}T00:00:00") for i in range(19)]
    b = battery(sel, sel)
    assert b.halves.startswith("[median 2026-05-23] ")
    big = [mk(i, "2026-05-01T00:00:00") for i in range(201)]
    assert battery(big, big).passes[4] is False
    assert battery(big[:200], big[:200]).passes[4] is True


def test_rule_thresholds_are_inclusive(tmp_path, settings):
    write_study(
        tmp_path,
        {
            "edge": coin(20, 1.0, 2.0, age_days=90, vols=[100] * 7 + [200] * 7),
        },
    )
    coins = load_study(str(tmp_path), str(tmp_path / "scout.db"))
    assert coins[0].age_days == 90.0 and coins[0].vol_ratio == 2.0
    assert [c.cid for c in apply_rule(coins, "F4", settings)] == ["edge"]


def test_study_vol_ratio_matches_study_definition():
    t = BASE_SEC + 20 * DAY
    t0 = datetime.fromtimestamp(t, tz=timezone.utc)
    # no candle minimum and no dedupe; ts == t0 and ts < t0-14d excluded
    ohlcv = [
        [t - 10 * DAY, 1, 1, 1, 1, 50],
        [t - 2 * DAY, 1, 1, 1, 1, 100],
        [t - 2 * DAY, 1, 1, 1, 1, 100],
        [t, 1, 1, 1, 1, 9999],
        [t - 20 * DAY, 1, 1, 1, 1, 9999],
    ]
    assert study_vol_ratio(ohlcv, t0) == pytest.approx(200 / 50)
    assert study_vol_ratio([[t - 2 * DAY, 1, 1, 1, 1, 100]], t0) is None  # b == 0
    assert study_vol_ratio([], t0) is None


def test_live_definition_matches_study_on_sparse_pools(tmp_path, settings):
    write_study(
        tmp_path,
        {
            "full": coin(20, 1.0, 2.0),
            # 10 candles: no candle minimum in the prereg, so both compute
            "sparse": coin(22, 1.0, 2.0, vols=[100] * 7 + [500] * 3),
        },
    )
    coins = load_study(str(tmp_path), str(tmp_path / "scout.db"))
    sparse = next(c for c in coins if c.cid == "sparse")
    assert sparse.vol_ratio == pytest.approx(1500 / 700)
    assert sparse.live_vol_ratio == pytest.approx(1500 / 700)
    assert {c.cid for c in apply_rule(coins, "F4", settings)} == {"full", "sparse"}
    assert f4_divergence(coins, settings) == 0


def test_f4_divergence_counts_verdict_flips(settings):
    from gecko.backtest.study import Coin

    def mk(cid, vr, live, age=30.0):
        return Coin(
            cid,
            "2026-05-01T00:00:00",
            False,
            age_days=age,
            vol_ratio=vr,
            live_vol_ratio=live,
        )

    coins = [
        mk("same", 3.0, 3.0),
        mk("live_none", 3.0, None),
        mk("live_only", 1.0, 2.5),
        mk("both_fail", 1.0, None),
        mk("old", 3.0, None, age=200.0),  # fails F1 either way: no F4 change
    ]
    assert f4_divergence(coins, settings) == 2


def test_dead_and_unverifiable_coins(tmp_path):
    write_study(
        tmp_path,
        {
            # nearest chart point is 5 days from t0 -> dead
            "dead": coin(30, 1.0, 2.0, prices=[(35, 1.0), (199, 2.0)]),
            "zero": coin(32, 0.0, 1.0, prices=[(32, 0.0), (199, 1.0)]),
            # no stored price: kept (unverifiable), as in the study
            "nostored": coin(34, 1.0, 3.0, stored=False),
        },
    )
    coins = load_study(str(tmp_path), str(tmp_path / "scout.db"))
    assert [c.cid for c in coins] == ["nostored"]


def test_load_without_history_db_keeps_all(tmp_path):
    write_study(tmp_path, {"a": coin(10, 1.0, 2.0, stored=0.01)})
    assert [c.cid for c in load_study(str(tmp_path), None)] == ["a"]


def test_stored_price_is_earliest_across_tables(tmp_path):
    write_study(tmp_path, {"a": coin(10, 1.0, 2.0, stored=1.0)})
    db = sqlite3.connect(tmp_path / "scout.db")
    db.execute("CREATE TABLE volume_spikes(coin_id TEXT, detected_at TEXT, price REAL)")
    # earlier row with a price 100x off -> the coin must be excluded
    db.execute("INSERT INTO volume_spikes VALUES('a','2026-01-01 00:00:00',100.0)")
    db.commit()
    db.close()
    assert load_study(str(tmp_path), str(tmp_path / "scout.db")) == []


def test_history_db_open_leaves_no_sidecars(tmp_path):
    write_study(tmp_path, {"a": coin(10, 1.0, 2.0)})
    db = sqlite3.connect(tmp_path / "scout.db")
    db.execute("PRAGMA journal_mode=WAL")  # as prod scout.db
    db.close()
    before = (tmp_path / "scout.db").read_bytes()
    assert [c.cid for c in load_study(str(tmp_path), str(tmp_path / "scout.db"))] == [
        "a"
    ]
    assert (tmp_path / "scout.db").read_bytes() == before
    for ext in ("-wal", "-shm", "-journal"):
        assert not (tmp_path / f"scout.db{ext}").exists()


def test_backtest_job_prints_row_and_divergence(tmp_path, capsys, monkeypatch):
    from gecko.__main__ import main

    monkeypatch.setenv("GECKO_DB_PATH", str(tmp_path / "gecko.db"))

    write_study(tmp_path, {"a": coin(10, 1.0, 8.0), "b": coin(12, 1.0, 0.4)})
    rc = main(
        [
            "backtest",
            "--rule",
            "F4",
            "--study-dir",
            str(tmp_path),
            "--history-db",
            str(tmp_path / "scout.db"),
        ]
    )
    out = capsys.readouterr().out
    # n=2 fails the battery: a FAIL verdict exits 1
    assert rc == 1
    assert "\nF4 | 2 | 4.20x | " in out
    assert "live-definition divergence: 0 coins change F4 verdict" in out


def test_backtest_job_load_error_exits_1(tmp_path, capsys, monkeypatch):
    from gecko.__main__ import main

    monkeypatch.setenv("GECKO_DB_PATH", str(tmp_path / "gecko.db"))

    rc = main(["backtest", "--study-dir", str(tmp_path / "missing")])
    assert rc == 1
    assert "cannot load study" in capsys.readouterr().out


def test_path_like_coin_ids_are_rejected_and_counted(tmp_path):
    write_study(tmp_path, {"a": coin(10, 1.0, 2.0)})
    ev = json.loads((tmp_path / "events.json").read_text())
    ev["../escape"] = ev["a"]
    _dump(ev, tmp_path / "events.json")
    # the file the traversal would read, if it were allowed
    _dump(
        json.loads((tmp_path / "charts" / "a.json").read_text()),
        tmp_path / "escape.json",
    )
    stats: dict = {}
    coins = load_study(str(tmp_path), None, stats)
    assert [c.cid for c in coins] == ["a"]
    assert stats["rejected"] == 1


def _run_backtest(tmp_path, monkeypatch, *extra):
    from gecko.__main__ import main

    monkeypatch.setenv("GECKO_DB_PATH", str(tmp_path / "gecko.db"))
    return main(
        [
            "backtest",
            "--rule",
            "R0",
            "--study-dir",
            str(tmp_path),
            "--history-db",
            str(tmp_path / "scout.db"),
            *extra,
        ]
    )


def test_backtest_window_filters_coins(tmp_path, capsys, monkeypatch):
    write_study(tmp_path, {"a": coin(10, 1.0, 8.0), "b": coin(12, 1.0, 0.4)})
    _run_backtest(tmp_path, monkeypatch, "--from", "2026-04-12", "--to", "2026-04-14")
    out = capsys.readouterr().out
    assert (
        "\nR0 | 1 | " in out
        and "window 2026-04-12T00:00:00Z..2026-04-14T00:00:00Z" in out
    )


def test_backtest_pass_exits_0(tmp_path, capsys, monkeypatch):
    # 24 young, volume-accelerating winners at 3.0x pass F4; 6 old losers at
    # 0.5x are F4-rejected but sit in the same months, so the cohort beats its
    # same-month baseline. t0 spread over ~4 months gives a median halves split.
    # Verified PASS against the Plan 1 battery on 2026-10-14.
    hot = [1] * 7 + [10] * 7  # prior week 7, last week 70: ratio 10
    coins = {f"w{i}": coin(10 + 5 * i, 1.0, 3.0, vols=hot) for i in range(24)}
    coins |= {
        f"l{i}": coin(10 + 5 * i, 1.0, 0.5, age_days=200, vols=hot) for i in range(6)
    }
    write_study(tmp_path, coins)
    from gecko.__main__ import main

    monkeypatch.setenv("GECKO_DB_PATH", str(tmp_path / "gecko.db"))
    rc = main(
        [
            "backtest",
            "--rule",
            "F4",
            "--study-dir",
            str(tmp_path),
            "--history-db",
            str(tmp_path / "scout.db"),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "| PASS |" in out
