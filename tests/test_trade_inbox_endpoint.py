"""Tests for the read-only trade opportunity inbox."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from dashboard.api import create_app
from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "test.db"
    d = Database(db_path)
    await d.initialize()
    yield d, str(db_path)
    await d.close()


@pytest.fixture
async def client(db):
    import dashboard.api as api_mod

    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None
    d, db_path = db
    app = create_app(db_path=db_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, d
    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None


async def _insert_open_trade(
    conn,
    *,
    token_id: str,
    symbol: str | None = None,
    entry_price: float = 100.0,
    current_price: float | None = 102.0,
    actionable: int | None = 1,
    would_be_live: int | None = 1,
    opened_at: str | None = None,
    updated_at: str | None = None,
    signal_type: str = "volume_spike",
    price_change_24h: float = 12.0,
):
    now = datetime.now(timezone.utc)
    opened = opened_at or (now - timedelta(hours=2)).isoformat()
    sym = symbol or token_id.upper()[:8]
    await conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity,
            tp_pct, sl_pct, tp_price, sl_price,
            status, opened_at,
            would_be_live, actionable)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            token_id,
            sym,
            token_id.title(),
            "coingecko",
            signal_type,
            json.dumps({}),
            entry_price,
            1000.0,
            10.0,
            20.0,
            10.0,
            entry_price * 1.2,
            entry_price * 0.9,
            "open",
            opened,
            would_be_live,
            actionable,
        ),
    )
    if current_price is not None:
        updated = updated_at or now.isoformat()
        await conn.execute(
            """INSERT OR REPLACE INTO price_cache
               (coin_id, current_price, price_change_24h, market_cap, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (token_id, current_price, price_change_24h, 50_000_000.0, updated),
        )
    await conn.commit()


async def _insert_gainers_comparison(
    conn,
    *,
    coin_id: str,
    symbol: str | None = None,
    name: str | None = None,
    appeared_at: str | None = None,
    price_change_24h: float = 24.0,
    detected_price: float | None = 100.0,
    current_price: float | None = 103.0,
    price_updated_at: str | None = None,
):
    now = datetime.now(timezone.utc)
    appeared = appeared_at or (now - timedelta(hours=1)).isoformat()
    sym = symbol or coin_id.upper()[:8]
    await conn.execute(
        """INSERT INTO gainers_comparisons
           (coin_id, symbol, name, price_change_24h, appeared_on_gainers_at,
            detected_by_narrative, detected_by_pipeline, detected_by_chains,
            detected_by_spikes, is_gap, detected_price)
           VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0, 1, ?)""",
        (
            coin_id,
            sym,
            name or coin_id.title(),
            price_change_24h,
            appeared,
            detected_price,
        ),
    )
    if current_price is not None:
        await conn.execute(
            """INSERT OR REPLACE INTO price_cache
               (coin_id, current_price, price_change_24h, market_cap, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                coin_id,
                current_price,
                price_change_24h,
                75_000_000.0,
                price_updated_at or now.isoformat(),
            ),
        )
    await conn.commit()


async def test_trade_inbox_groups_shape_and_read_only(client):
    c, db = client
    now = datetime.now(timezone.utc)
    await _insert_open_trade(db._conn, token_id="review", current_price=103.0)
    await _insert_open_trade(
        db._conn,
        token_id="moved",
        current_price=140.0,
        opened_at=(now - timedelta(hours=1)).isoformat(),
    )
    await _insert_open_trade(db._conn, token_id="blocked", actionable=0)
    await _insert_open_trade(
        db._conn,
        token_id="stale-warning",
        current_price=102.0,
        updated_at=(now - timedelta(minutes=90)).isoformat(),
    )

    before = await _paper_trade_count(db._conn)
    resp = await c.get("/api/trade_inbox?limit_per_group=10&window_hours=36")
    after = await _paper_trade_count(db._conn)

    assert resp.status_code == 200, resp.text
    assert after == before
    payload = resp.json()
    assert set(payload["groups"]) == {"act_now", "watch", "already_ran", "blocked"}
    assert payload["meta"]["read_only"] is True
    assert payload["meta"]["not_trade_advice"] is True
    assert payload["meta"]["experimental"] is True
    assert payload["meta"]["rows_returned"] == sum(
        len(v) for v in payload["groups"].values()
    )
    assert payload["groups"]["act_now"][0]["action_label"] == "REVIEW_NOW"
    assert payload["groups"]["watch"][0]["token_id"] == "stale-warning"
    assert payload["groups"]["already_ran"][0]["token_id"] == "moved"
    assert payload["groups"]["blocked"][0]["block_reason_primary"] == "NOT_ACTIONABLE"


async def test_trade_inbox_promotes_tracker_only_gainer_to_watch(client):
    c, db = client
    before = await _paper_trade_count(db._conn)
    await _insert_gainers_comparison(
        db._conn,
        coin_id="toes",
        symbol="TOES",
        detected_price=100.0,
        current_price=104.0,
    )

    resp = await c.get("/api/trade_inbox?limit_per_group=10&window_hours=36")
    after = await _paper_trade_count(db._conn)

    assert resp.status_code == 200, resp.text
    assert after == before
    payload = resp.json()
    rows = payload["groups"]["watch"]
    assert [r["token_id"] for r in rows] == ["toes"]
    row = rows[0]
    assert row["source_corpus"] == "tracker"
    assert row["open_trade_ids"] == []
    assert row["recent_trade_ids"] == []
    assert row["surfaces"] == ["top_gainers_tracker"]
    assert row["actionable"] is None
    assert row["would_be_live"] is None
    assert row["action_label"] == "WATCH_PULLBACK"
    assert "tracker_promotion" in row["inclusion_reasons"]
    assert "tracker_only_no_paper_trade" in row["risk_reasons"]
    assert payload["meta"]["tracker_rows_considered"] == 1
    assert payload["meta"]["tracker_rows_promoted"] == 1
    assert payload["meta"]["paper_rows_considered"] == 0


async def test_trade_inbox_dedupes_tracker_row_behind_open_paper_trade(client):
    c, db = client
    await _insert_open_trade(db._conn, token_id="toes", symbol="TOES")
    await _insert_gainers_comparison(
        db._conn,
        coin_id="toes",
        symbol="TOES",
        detected_price=100.0,
        current_price=104.0,
    )

    resp = await c.get("/api/trade_inbox?limit_per_group=10&window_hours=36")

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    all_rows = [r for rows in payload["groups"].values() for r in rows]
    toes_rows = [r for r in all_rows if r["token_id"] == "toes"]
    assert len(toes_rows) == 1
    assert toes_rows[0]["source_corpus"] == "paper"
    assert toes_rows[0]["open_trade_ids"]
    assert "top_gainers_tracker" in toes_rows[0]["surfaces"]
    assert payload["meta"]["tracker_rows_considered"] == 1
    assert payload["meta"]["tracker_rows_promoted"] == 0
    assert payload["meta"]["paper_rows_considered"] == 1


async def test_trade_inbox_suppresses_tracker_row_when_matching_open_paper_is_beyond_source_limit(
    client,
):
    c, db = client
    now = datetime.now(timezone.utc)
    for i in range(500):
        await _insert_open_trade(
            db._conn,
            token_id=f"paper-visible-{i:03d}",
            opened_at=(now - timedelta(minutes=i)).isoformat(),
        )
    await _insert_open_trade(
        db._conn,
        token_id="older-paper",
        symbol="OLDP",
        opened_at=(now - timedelta(hours=20)).isoformat(),
    )
    await _insert_gainers_comparison(
        db._conn,
        coin_id="older-paper",
        symbol="OLDP",
        detected_price=100.0,
        current_price=104.0,
    )

    resp = await c.get("/api/trade_inbox?limit_per_group=1&window_hours=36")

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    all_rows = [r for rows in payload["groups"].values() for r in rows]
    assert all(r["token_id"] != "older-paper" for r in all_rows)
    assert payload["meta"]["tracker_rows_considered"] == 1
    assert payload["meta"]["tracker_rows_promoted"] == 0
    assert payload["meta"]["source_truncated"] is True


async def test_trade_inbox_scans_past_tracker_duplicates_to_promote_tracker_only_rows(
    client,
):
    c, db = client
    now = datetime.now(timezone.utc)
    for i in range(500):
        token_id = f"paper-dup-{i:03d}"
        await _insert_open_trade(
            db._conn,
            token_id=token_id,
            opened_at=(now - timedelta(minutes=i)).isoformat(),
        )
        await _insert_gainers_comparison(
            db._conn,
            coin_id=token_id,
            appeared_at=(now - timedelta(minutes=i)).isoformat(),
        )
    await _insert_gainers_comparison(
        db._conn,
        coin_id="tracker-only-after-dupes",
        symbol="TOAD",
        appeared_at=(now - timedelta(hours=10)).isoformat(),
        detected_price=100.0,
        current_price=104.0,
    )

    resp = await c.get("/api/trade_inbox?limit_per_group=1&window_hours=36")

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    all_rows = [r for rows in payload["groups"].values() for r in rows]
    promoted = [r for r in all_rows if r["token_id"] == "tracker-only-after-dupes"]
    assert len(promoted) == 1
    assert promoted[0]["source_corpus"] == "tracker"
    assert payload["meta"]["tracker_rows_considered"] > 500
    assert payload["meta"]["tracker_rows_promoted"] == 1
    assert payload["meta"]["tracker_source_truncated"] is False


async def test_trade_inbox_tracker_row_without_price_is_data_missing(client):
    c, db = client
    await _insert_gainers_comparison(
        db._conn,
        coin_id="no-price-gainer",
        symbol="NPG",
        detected_price=None,
        current_price=None,
    )

    resp = await c.get("/api/trade_inbox?limit_per_group=10&window_hours=36")

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    row = payload["groups"]["blocked"][0]
    assert row["token_id"] == "no-price-gainer"
    assert row["source_corpus"] == "tracker"
    assert row["action_label"] == "DATA_MISSING"
    assert row["block_reason_primary"] == "NO_PRICE"
    assert "no_price_snapshot_for_token_id" in row["risk_reasons"]
    assert payload["meta"]["tracker_rows_promoted"] == 1


async def test_trade_inbox_tracker_row_with_current_price_but_no_detected_price_is_data_missing(
    client,
):
    c, db = client
    await _insert_gainers_comparison(
        db._conn,
        coin_id="missing-entry-price",
        symbol="MEP",
        detected_price=None,
        current_price=104.0,
    )

    resp = await c.get("/api/trade_inbox?limit_per_group=10&window_hours=36")

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    row = payload["groups"]["blocked"][0]
    assert row["token_id"] == "missing-entry-price"
    assert row["source_corpus"] == "tracker"
    assert row["action_label"] == "DATA_MISSING"
    assert row["block_reason_primary"] == "DATA_INSUFFICIENT"
    assert row["entry_quality"] == "data_insufficient"
    assert row["pct_from_entry"] is None
    assert "detected_price_missing_or_invalid" in row["risk_reasons"]


async def test_trade_inbox_broad_cohort_surfaces_toes_beyond_raw_limit(client):
    c, db = client
    now = datetime.now(timezone.utc)
    for i in range(14):
        await _insert_open_trade(
            db._conn,
            token_id=f"newer-blocked-{i:02d}",
            actionable=0,
            opened_at=(now - timedelta(minutes=i)).isoformat(),
        )
    await _insert_open_trade(
        db._conn,
        token_id="toes",
        symbol="TOES",
        current_price=103.0,
        opened_at=(now - timedelta(hours=6)).isoformat(),
        price_change_24h=18.0,
    )

    resp = await c.get("/api/trade_inbox?limit_per_group=5&window_hours=36")

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert [r["token_id"] for r in payload["groups"]["act_now"]] == ["toes"]
    assert payload["meta"]["source_rows_considered"] >= 15


async def test_trade_inbox_score_sort_key_and_why_now_are_deterministic(client):
    c, db = client
    opened = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    await _insert_open_trade(
        db._conn,
        token_id="alpha",
        entry_price=100.0,
        current_price=102.0,
        opened_at=opened,
        price_change_24h=10.0,
    )
    await _insert_open_trade(
        db._conn,
        token_id="zeta",
        entry_price=100.0,
        current_price=102.0,
        opened_at=opened,
        price_change_24h=10.0,
    )

    resp = await c.get("/api/trade_inbox?limit_per_group=10&window_hours=36")

    assert resp.status_code == 200, resp.text
    rows = resp.json()["groups"]["act_now"]
    assert [r["token_id"] for r in rows] == ["alpha", "zeta"]
    assert rows[0]["trade_score"] == 100.0
    assert rows[0]["sort_key"] == rows[1]["sort_key"][:-1] + ["alpha"]
    assert rows[0]["why_now"][:3] == ["open_window", "window=open", "fresh_entry"]


async def test_trade_inbox_stale_boundaries_and_bad_data(client):
    c, db = client
    now = datetime.now(timezone.utc)
    await _insert_open_trade(db._conn, token_id="no-price", current_price=None)
    await _insert_open_trade(
        db._conn,
        token_id="bad-time",
        opened_at="not-iso",
    )
    await _insert_open_trade(
        db._conn,
        token_id="stale-60",
        updated_at=(now - timedelta(minutes=60)).isoformat(),
    )
    await _insert_open_trade(
        db._conn,
        token_id="stale-120",
        updated_at=(now - timedelta(minutes=120)).isoformat(),
    )
    await _insert_open_trade(
        db._conn,
        token_id="bad-price-time",
        updated_at="not-iso",
    )

    resp = await c.get("/api/trade_inbox?limit_per_group=10&window_hours=36")

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    blocked = {r["token_id"]: r for r in payload["groups"]["blocked"]}
    watch = {r["token_id"]: r for r in payload["groups"]["watch"]}
    assert blocked["no-price"]["block_reason_primary"] == "NO_PRICE"
    assert blocked["bad-time"]["block_reason_primary"] == "BAD_TIMESTAMP"
    assert blocked["bad-price-time"]["block_reason_primary"] == "DATA_INSUFFICIENT"
    assert "price_timestamp_unparseable" in blocked["bad-price-time"]["risk_reasons"]
    assert blocked["stale-120"]["block_reason_primary"] == "STALE_PRICE"
    assert watch["stale-60"]["price_is_stale"] is True
    assert payload["meta"]["stale_warning_count"] >= 1
    assert payload["meta"]["hard_stale_count"] >= 1


async def test_trade_inbox_overflow_meta_exposes_hidden_rows(client):
    c, db = client
    now = datetime.now(timezone.utc)
    for i in range(7):
        await _insert_open_trade(
            db._conn,
            token_id=f"review-{i}",
            opened_at=(now - timedelta(minutes=i)).isoformat(),
        )

    resp = await c.get("/api/trade_inbox?limit_per_group=3&window_hours=36")

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert len(payload["groups"]["act_now"]) == 3
    assert payload["meta"]["group_counts"]["act_now"] == 7
    assert payload["meta"]["group_hidden_counts"]["act_now"] == 4


async def _paper_trade_count(conn) -> int:
    cursor = await conn.execute("SELECT COUNT(*) AS c FROM paper_trades")
    row = await cursor.fetchone()
    return int(row["c"])
