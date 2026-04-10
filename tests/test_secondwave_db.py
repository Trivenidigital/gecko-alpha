"""Tests for second-wave DB schema and query methods."""
from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


async def _insert_alert(db, contract, alerted_days_ago, market_cap, price, name="Tok", ticker="TK", chain="eth"):
    ts = (datetime.now(timezone.utc) - timedelta(days=alerted_days_ago)).isoformat()
    await db._conn.execute(
        """INSERT INTO alerts
           (contract_address, chain, conviction_score, alert_market_cap, price_usd, token_name, ticker, alerted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (contract, chain, 80.0, market_cap, price, name, ticker, ts),
    )
    await db._conn.commit()


async def _insert_score_history(db, contract, score, days_ago=5):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        (contract, score, ts),
    )
    await db._conn.commit()


async def test_second_wave_candidates_table_exists(db):
    cursor = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='second_wave_candidates'"
    )
    assert await cursor.fetchone() is not None


async def test_alerts_table_has_new_columns(db):
    cursor = await db._conn.execute("PRAGMA table_info(alerts)")
    cols = {row[1] for row in await cursor.fetchall()}
    assert {"price_usd", "token_name", "ticker"}.issubset(cols)


async def test_get_secondwave_scan_candidates_filters_by_window(db):
    # In window: alerted 5 days ago, peak score 75
    await _insert_alert(db, "0xin", alerted_days_ago=5, market_cap=2e6, price=1.0)
    await _insert_score_history(db, "0xin", score=75.0, days_ago=5)
    # Too fresh: 1 day ago
    await _insert_alert(db, "0xfresh", alerted_days_ago=1, market_cap=2e6, price=1.0)
    await _insert_score_history(db, "0xfresh", score=80.0, days_ago=1)
    # Too stale: 20 days ago
    await _insert_alert(db, "0xstale", alerted_days_ago=20, market_cap=2e6, price=1.0)
    await _insert_score_history(db, "0xstale", score=80.0, days_ago=20)
    # Weak prior: score 40
    await _insert_alert(db, "0xweak", alerted_days_ago=5, market_cap=2e6, price=1.0)
    await _insert_score_history(db, "0xweak", score=40.0, days_ago=5)

    rows = await db.get_secondwave_scan_candidates(
        min_age_days=3, max_age_days=14, min_peak_score=60, dedup_days=7
    )
    addrs = {r["contract_address"] for r in rows}
    assert "0xin" in addrs
    assert "0xfresh" not in addrs
    assert "0xstale" not in addrs
    assert "0xweak" not in addrs


async def test_get_secondwave_scan_candidates_excludes_already_detected(db):
    await _insert_alert(db, "0xdup", alerted_days_ago=5, market_cap=2e6, price=1.0)
    await _insert_score_history(db, "0xdup", score=80.0, days_ago=5)
    now = datetime.now(timezone.utc).isoformat()
    await db.insert_secondwave_candidate({
        "contract_address": "0xdup", "chain": "eth",
        "token_name": "Dup", "ticker": "DUP", "coingecko_id": None,
        "peak_quant_score": 80, "peak_signals_fired": [],
        "first_seen_at": now, "original_alert_at": None,
        "original_market_cap": 1e6, "alert_market_cap": 2e6,
        "days_since_first_seen": 5.0, "price_drop_from_peak_pct": -40.0,
        "current_price": 0.8, "current_market_cap": 1.2e6,
        "current_volume_24h": 5e5, "price_vs_alert_pct": 80.0,
        "volume_vs_cooldown_avg": 2.5, "price_is_stale": False,
        "reaccumulation_score": 85,
        "reaccumulation_signals": ["sufficient_drawdown", "price_recovery"],
        "detected_at": now, "alerted_at": now,
    })
    rows = await db.get_secondwave_scan_candidates(3, 14, 60, 7)
    assert all(r["contract_address"] != "0xdup" for r in rows)


async def test_was_secondwave_alerted(db):
    assert await db.was_secondwave_alerted("0xnew") is False
    now = datetime.now(timezone.utc).isoformat()
    await db.insert_secondwave_candidate({
        "contract_address": "0xnew", "chain": "eth",
        "token_name": "X", "ticker": "X", "coingecko_id": None,
        "peak_quant_score": 70, "peak_signals_fired": [],
        "first_seen_at": now, "original_alert_at": None,
        "original_market_cap": 1e6, "alert_market_cap": 2e6,
        "days_since_first_seen": 5.0, "price_drop_from_peak_pct": -40.0,
        "current_price": 0.8, "current_market_cap": 1.2e6,
        "current_volume_24h": 5e5, "price_vs_alert_pct": 80.0,
        "volume_vs_cooldown_avg": 2.5, "price_is_stale": False,
        "reaccumulation_score": 60, "reaccumulation_signals": [],
        "detected_at": now, "alerted_at": now,
    })
    assert await db.was_secondwave_alerted("0xnew") is True


async def test_get_volume_history(db):
    now = datetime.now(timezone.utc)
    for i, v in enumerate([100.0, 200.0, 300.0]):
        ts = (now - timedelta(days=i)).isoformat()
        await db._conn.execute(
            "INSERT INTO volume_snapshots (contract_address, volume_24h_usd, scanned_at) VALUES (?, ?, ?)",
            ("0xvh", v, ts),
        )
    await db._conn.commit()
    hist = await db.get_volume_history("0xvh", days=14)
    assert len(hist) == 3
    assert sorted(hist) == [100.0, 200.0, 300.0]


async def test_get_recent_secondwave_candidates(db):
    now = datetime.now(timezone.utc).isoformat()
    await db.insert_secondwave_candidate({
        "contract_address": "0xr", "chain": "eth",
        "token_name": "R", "ticker": "R", "coingecko_id": None,
        "peak_quant_score": 70, "peak_signals_fired": ["x"],
        "first_seen_at": now, "original_alert_at": None,
        "original_market_cap": 1e6, "alert_market_cap": 2e6,
        "days_since_first_seen": 5.0, "price_drop_from_peak_pct": -40.0,
        "current_price": 0.8, "current_market_cap": 1.2e6,
        "current_volume_24h": 5e5, "price_vs_alert_pct": 80.0,
        "volume_vs_cooldown_avg": 2.5, "price_is_stale": False,
        "reaccumulation_score": 77, "reaccumulation_signals": ["price_recovery"],
        "detected_at": now, "alerted_at": now,
    })
    rows = await db.get_recent_secondwave_candidates(days=7)
    assert len(rows) == 1
    assert rows[0]["reaccumulation_score"] == 77
    assert rows[0]["reaccumulation_signals"] == ["price_recovery"]
    assert rows[0]["peak_signals_fired"] == ["x"]


async def test_log_alert_persists_new_columns(db):
    """Ensure log_alert's extended signature round-trips price_usd/token_name/ticker."""
    await db.log_alert(
        contract_address="0xnewcols",
        chain="ethereum",
        conviction_score=72.5,
        alert_market_cap=1_500_000.0,
        price_usd=0.42,
        token_name="NewCol Token",
        ticker="NCT",
    )
    cursor = await db._conn.execute(
        """SELECT contract_address, chain, conviction_score, alert_market_cap,
                  price_usd, token_name, ticker
           FROM alerts WHERE contract_address = ?""",
        ("0xnewcols",),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["contract_address"] == "0xnewcols"
    assert row["chain"] == "ethereum"
    assert row["conviction_score"] == 72.5
    assert row["alert_market_cap"] == 1_500_000.0
    assert row["price_usd"] == 0.42
    assert row["token_name"] == "NewCol Token"
    assert row["ticker"] == "NCT"
