"""Tests for paper trading dashboard API endpoints."""

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


async def _insert_trade(
    conn,
    token_id,
    symbol,
    signal_type,
    status,
    pnl_usd=None,
    pnl_pct=None,
    would_be_live=None,
    conviction_locked_stack=None,
):
    now = datetime.now(timezone.utc)
    opened = (now - timedelta(hours=2)).isoformat()
    closed = now.isoformat() if status != "open" else None
    await conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
            status, pnl_usd, pnl_pct, opened_at, closed_at,
            would_be_live, conviction_locked_stack)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            token_id,
            symbol,
            token_id.title(),
            "coingecko",
            signal_type,
            json.dumps({}),
            100.0,
            1000.0,
            10.0,
            20.0,
            10.0,
            120.0,
            90.0,
            status,
            pnl_usd,
            pnl_pct,
            opened,
            closed,
            would_be_live,
            conviction_locked_stack,
        ),
    )
    await conn.commit()


async def test_get_positions(client):
    c, db = client
    await _insert_trade(db._conn, "bitcoin", "BTC", "volume_spike", "open")
    resp = await c.get("/api/trading/positions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["symbol"] == "BTC"


async def test_get_history(client):
    c, db = client
    await _insert_trade(
        db._conn, "bitcoin", "BTC", "volume_spike", "closed_tp", 200.0, 20.0
    )
    resp = await c.get("/api/trading/history")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1


async def test_get_stats(client):
    c, db = client
    await _insert_trade(
        db._conn, "bitcoin", "BTC", "volume_spike", "closed_tp", 200.0, 20.0
    )
    await _insert_trade(
        db._conn, "ethereum", "ETH", "narrative_prediction", "closed_sl", -50.0, -5.0
    )
    resp = await c.get("/api/trading/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_pnl_usd" in data
    assert "win_rate_pct" in data


async def test_get_stats_by_signal(client):
    c, db = client
    await _insert_trade(
        db._conn, "bitcoin", "BTC", "volume_spike", "closed_tp", 200.0, 20.0
    )
    await _insert_trade(
        db._conn, "ethereum", "ETH", "volume_spike", "closed_sl", -50.0, -5.0
    )
    resp = await c.get("/api/trading/stats/by-signal")
    assert resp.status_code == 200
    data = resp.json()
    assert "volume_spike" in data


# ---------------------------------------------------------------------------
# BL-NEW-LIVE-ELIGIBLE follow-up: cohort-comparison endpoint
# (tasks/plan_dashboard_live_eligible_view.md)
# ---------------------------------------------------------------------------


async def test_stats_by_signal_cohort_shape(client):
    """Endpoint returns the expected top-level keys.

    Vector B/C review folds added near_identical_cohorts +
    min_eligible_n_for_verdict + verdict_window_anchor — operator-visible
    contract that the dashboard reads to gate verdicts. Lock them in tests
    so a future refactor can't silently drop them.
    """
    c, _ = client
    resp = await c.get("/api/trading/stats/by-signal-cohort")
    assert resp.status_code == 200
    data = resp.json()
    for key in (
        "full_cohort",
        "eligible_cohort",
        "excluded_signal_types",
        "near_identical_cohorts",
        "min_eligible_n_for_verdict",
        "verdict_window_anchor",
        "small_n_caveat",
        "window_days",
    ):
        assert key in data, f"missing key: {key}"
    assert data["window_days"] == 7
    assert data["min_eligible_n_for_verdict"] == 10
    # chain_completed is structurally near-identical (Tier 1a) — must appear
    # in near_identical_cohorts unconditionally, not contingent on data.
    assert "chain_completed" in data["near_identical_cohorts"]


async def test_stats_by_signal_cohort_splits_eligible_from_full(client):
    """full_cohort includes all closes; eligible_cohort only would_be_live=1."""
    c, db = client
    # gainers_early: 2 closes, 1 eligible 1 ineligible
    await _insert_trade(
        db._conn,
        "btc",
        "BTC",
        "gainers_early",
        "closed_tp",
        pnl_usd=100.0,
        pnl_pct=10.0,
        would_be_live=1,
    )
    await _insert_trade(
        db._conn,
        "eth",
        "ETH",
        "gainers_early",
        "closed_sl",
        pnl_usd=-30.0,
        pnl_pct=-3.0,
        would_be_live=0,
    )
    resp = await c.get("/api/trading/stats/by-signal-cohort")
    assert resp.status_code == 200
    data = resp.json()
    full = {r["signal_type"]: r for r in data["full_cohort"]}
    eligible = {r["signal_type"]: r for r in data["eligible_cohort"]}
    assert full["gainers_early"]["trades"] == 2
    assert full["gainers_early"]["total_pnl_usd"] == 70.0
    assert eligible["gainers_early"]["trades"] == 1
    assert eligible["gainers_early"]["total_pnl_usd"] == 100.0

    # Ticker aggregation (Piece 1 of dashboard eligibility-visibility hybrid):
    # the cohort endpoint returns sorted ticker lists per signal_type for the
    # dashboard's inline-ticker display. Empty/NULL symbols filtered out.
    assert "symbols" in full["gainers_early"]
    assert sorted(full["gainers_early"]["symbols"]) == ["BTC", "ETH"]
    assert "symbols" in eligible["gainers_early"]
    assert eligible["gainers_early"]["symbols"] == ["BTC"]


async def test_positions_and_history_include_would_be_live(client):
    """Piece 2 of dashboard eligibility-visibility hybrid: the per-trade
    payloads from /positions and /history now expose would_be_live so the
    frontend can render the per-row Eligible column with ✓/✗/— icons.
    """
    c, db = client
    # One open trade, would_be_live=1
    await _insert_trade(
        db._conn,
        "open-eligible",
        "OPEN1",
        "gainers_early",
        "open",
        pnl_usd=None,
        pnl_pct=None,
        would_be_live=1,
    )
    # One closed trade, would_be_live=0
    await _insert_trade(
        db._conn,
        "closed-not-eligible",
        "CLOSED1",
        "gainers_early",
        "closed_tp",
        pnl_usd=10.0,
        pnl_pct=1.0,
        would_be_live=0,
    )
    # One closed trade pre-writer (would_be_live=None) — simulates pre-2026-05-11
    await _insert_trade(
        db._conn,
        "closed-pre-writer",
        "CLOSED2",
        "gainers_early",
        "closed_tp",
        pnl_usd=5.0,
        pnl_pct=0.5,
        would_be_live=None,
    )

    pos_resp = await c.get("/api/trading/positions")
    assert pos_resp.status_code == 200
    positions = pos_resp.json()
    by_token = {p["token_id"]: p for p in positions}
    assert "open-eligible" in by_token
    assert by_token["open-eligible"]["would_be_live"] == 1

    hist_resp = await c.get("/api/trading/history?limit=20&offset=0")
    assert hist_resp.status_code == 200
    history = hist_resp.json()
    by_token = {h["token_id"]: h for h in history}
    assert by_token["closed-not-eligible"]["would_be_live"] == 0
    assert by_token["closed-pre-writer"]["would_be_live"] is None


async def test_stats_by_signal_cohort_excludes_non_stackable(client):
    """A signal_type with MAX(conviction_locked_stack) < 3 AND not in Tier
    1a/2a/2b enumeration appears in excluded_signal_types with a structural
    reason. Visibility-not-hiding: trending_catch surfaces with its cap, not
    silently disappears.

    Vector C F-I1 fold: the reason string must clearly explain that the
    eligible subset is *structurally empty*, not just *small*. Lock the
    operator-facing language.
    """
    c, db = client
    # trending_catch — single-source, never stacks; should be excluded.
    await _insert_trade(
        db._conn,
        "tok1",
        "T1",
        "trending_catch",
        "closed_sl",
        pnl_usd=-10.0,
        pnl_pct=-1.0,
        conviction_locked_stack=None,
    )
    # volume_spike — tier-enumerated; should NEVER be excluded even if stack low.
    await _insert_trade(
        db._conn,
        "tok2",
        "T2",
        "volume_spike",
        "closed_tp",
        pnl_usd=50.0,
        pnl_pct=5.0,
        conviction_locked_stack=None,
    )
    resp = await c.get("/api/trading/stats/by-signal-cohort")
    data = resp.json()
    excluded_types = {e["signal_type"] for e in data["excluded_signal_types"]}
    assert "trending_catch" in excluded_types
    assert "volume_spike" not in excluded_types
    tc = next(
        e for e in data["excluded_signal_types"] if e["signal_type"] == "trending_catch"
    )
    assert tc["max_observed_stack"] == 0
    # Reason must use "structurally empty" framing (Vector C F-I1 fold),
    # not just "stack cap = 0" jargon that inverts on casual read.
    assert "structurally empty" in tc["reason"]
    assert "max stack" in tc["reason"]
    assert "Still paper-trading" in tc["reason"]


async def test_stats_by_signal_cohort_non_enum_signal_NOT_excluded_when_stack_ge_3(
    client,
):
    """A signal_type not in Tier 1a/2a/2b enumeration but with observed
    conviction_locked_stack >= 3 is NOT excluded — Tier 1b (stack-based)
    eligibility applies regardless of signal_type. Catches the regression
    where the exclusion list mistakenly uses hardcoded enumeration alone."""
    c, db = client
    # narrative_prediction is not in the tier enum but CAN stack to 3+.
    await _insert_trade(
        db._conn,
        "tok1",
        "T1",
        "narrative_prediction",
        "closed_tp",
        pnl_usd=80.0,
        pnl_pct=8.0,
        conviction_locked_stack=3,
        would_be_live=1,
    )
    resp = await c.get("/api/trading/stats/by-signal-cohort")
    data = resp.json()
    excluded_types = {e["signal_type"] for e in data["excluded_signal_types"]}
    assert "narrative_prediction" not in excluded_types


async def test_stats_by_signal_cohort_eligible_empty_when_no_writer_stamps(client):
    """Existing trades pre-2026-05-11 have would_be_live=NULL. The eligible
    cohort must return [] (or omit the signal_type), not silently include
    NULL rows."""
    c, db = client
    await _insert_trade(
        db._conn,
        "btc",
        "BTC",
        "gainers_early",
        "closed_tp",
        pnl_usd=100.0,
        pnl_pct=10.0,
        would_be_live=None,
    )
    resp = await c.get("/api/trading/stats/by-signal-cohort")
    data = resp.json()
    eligible_types = {r["signal_type"] for r in data["eligible_cohort"]}
    assert "gainers_early" not in eligible_types
    # but full cohort sees it
    full_types = {r["signal_type"] for r in data["full_cohort"]}
    assert "gainers_early" in full_types


async def test_stats_by_signal_cohort_carries_caveat_text(client):
    """The small_n_caveat must lead with the operator-actionable framing
    (Vector C F-N1/F-I3 folds): n-gate requirement, exploratory-not-
    confirmatory framing, and the explicit decision-lock date. Without
    these the operator anchors on the smaller-n verdict prematurely."""
    c, _ = client
    resp = await c.get("/api/trading/stats/by-signal-cohort")
    data = resp.json()
    caveat = data["small_n_caveat"]
    assert "INSUFFICIENT_DATA" in caveat
    assert "exploratory" in caveat
    assert "2026-06-08" in caveat
    # Decision-lock date duplicated in verdict_window_anchor so the
    # UI can render it independently from the caveat text.
    assert "2026-06-08" in data["verdict_window_anchor"]


async def test_positions_empty(client):
    c, _ = client
    resp = await c.get("/api/trading/positions")
    assert resp.status_code == 200
    assert resp.json() == []


async def _seed_price(conn, token_id, current_price):
    await conn.execute(
        "INSERT OR REPLACE INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        (token_id, current_price, datetime.now(timezone.utc).isoformat()),
    )
    await conn.commit()


async def test_unrealized_pnl_uses_remaining_qty_post_leg_1(client):
    """Post-leg-1 unrealized P&L must be computed on remaining_qty, not initial quantity."""
    c, db = client
    await _insert_trade(db._conn, "ladder-coin", "LDR", "first_signal", "open")
    await db._conn.execute(
        "UPDATE paper_trades SET remaining_qty = 7.0, leg_1_filled_at = ? "
        "WHERE token_id = 'ladder-coin'",
        (datetime.now(timezone.utc).isoformat(),),
    )
    await db._conn.commit()
    await _seed_price(db._conn, "ladder-coin", 110.0)

    resp = await c.get("/api/trading/positions")
    assert resp.status_code == 200
    pos = [p for p in resp.json() if p["token_id"] == "ladder-coin"][0]
    # entry=100, cp=110, remaining_qty=7 → (110-100)*7 = 70.00
    assert pos["unrealized_pnl_usd"] == 70.00
    assert pos["remaining_qty"] == 7.0


async def test_unrealized_pnl_falls_back_to_quantity_pre_cutover(client):
    """Pre-cutover trades have remaining_qty=NULL and must use initial quantity."""
    c, db = client
    await _insert_trade(db._conn, "legacy-coin", "LGC", "first_signal", "open")
    await _seed_price(db._conn, "legacy-coin", 110.0)

    resp = await c.get("/api/trading/positions")
    assert resp.status_code == 200
    pos = [p for p in resp.json() if p["token_id"] == "legacy-coin"][0]
    # remaining_qty is NULL, quantity=10 → (110-100)*10 = 100.00
    assert pos["unrealized_pnl_usd"] == 100.00
    assert pos["remaining_qty"] is None


async def test_total_pnl_combines_realized_and_unrealized_against_original_capital(
    client,
):
    """The dashboard's PnL$ and PnL% columns must reconcile against the
    trader's original `amount_usd` so a partially-filled ladder trade does
    NOT show a price-based +X% next to a smaller-than-expected $ figure
    (the bug observed on ZKJ #1357 with realized=$67, unrealized=$234,
    +195% price move on a 40% remainder).

    With realized_pnl_usd=$50 already booked from closed legs and
    unrealized=$70 on the open remainder, total must be $120 and percent
    must be 12% (against the original $1000 amount_usd).
    """
    c, db = client
    await _insert_trade(db._conn, "ladder-mix", "LMX", "first_signal", "open")
    await db._conn.execute(
        "UPDATE paper_trades SET remaining_qty = 7.0, realized_pnl_usd = 50.0, "
        "leg_1_filled_at = ? WHERE token_id = 'ladder-mix'",
        (datetime.now(timezone.utc).isoformat(),),
    )
    await db._conn.commit()
    await _seed_price(db._conn, "ladder-mix", 110.0)

    resp = await c.get("/api/trading/positions")
    assert resp.status_code == 200
    pos = [p for p in resp.json() if p["token_id"] == "ladder-mix"][0]
    # entry=100, cp=110, remaining_qty=7 → unrealized = $70
    assert pos["unrealized_pnl_usd"] == 70.00
    # realized=50, unrealized=70 → total=$120, 120/1000 = 12.0%
    assert pos["total_pnl_usd"] == 120.00
    assert pos["total_pnl_pct"] == 12.00


async def test_total_pnl_handles_null_realized(client):
    """When realized_pnl_usd is NULL (no ladder legs filled), total must
    equal unrealized — no NoneType arithmetic crash."""
    c, db = client
    await _insert_trade(db._conn, "no-fills", "NOF", "first_signal", "open")
    await _seed_price(db._conn, "no-fills", 110.0)

    resp = await c.get("/api/trading/positions")
    assert resp.status_code == 200
    pos = [p for p in resp.json() if p["token_id"] == "no-fills"][0]
    # quantity=10, no remaining_qty, no realized → unrealized = $100, total = $100
    assert pos["unrealized_pnl_usd"] == 100.00
    assert pos["total_pnl_usd"] == 100.00
    assert pos["total_pnl_pct"] == 10.00  # 100/1000


async def test_total_pnl_null_when_no_current_price(client):
    """No current_price → all PnL fields stay None (no NoneType crash)."""
    c, db = client
    await _insert_trade(db._conn, "no-price", "NOP", "first_signal", "open")
    # No price_cache row inserted

    resp = await c.get("/api/trading/positions")
    assert resp.status_code == 200
    pos = [p for p in resp.json() if p["token_id"] == "no-price"][0]
    assert pos["unrealized_pnl_usd"] is None
    assert pos["total_pnl_usd"] is None
    assert pos["total_pnl_pct"] is None


# --- Closed-trades pagination tests ---


async def test_history_count_endpoint(client):
    """Count endpoint returns total closed paper trades (status != 'open')."""
    c, db = client
    await _insert_trade(
        db._conn, "bitcoin", "BTC", "volume_spike", "closed_tp", 200.0, 20.0
    )
    await _insert_trade(
        db._conn, "ethereum", "ETH", "volume_spike", "closed_sl", -50.0, -5.0
    )
    await _insert_trade(
        db._conn, "solana", "SOL", "first_signal", "closed_duration", 0.0, 0.0
    )
    await _insert_trade(db._conn, "doge", "DOGE", "volume_spike", "open")
    await _insert_trade(db._conn, "shib", "SHIB", "volume_spike", "open")
    resp = await c.get("/api/trading/history/count")
    assert resp.status_code == 200
    assert resp.json() == {"total": 3}


async def test_history_count_endpoint_empty(client):
    """Count endpoint returns 0 when no closed trades."""
    c, _ = client
    resp = await c.get("/api/trading/history/count")
    assert resp.status_code == 200
    assert resp.json() == {"total": 0}


async def test_history_offset_pagination(client):
    """offset returns non-overlapping windows in closed_at DESC order.

    R1-C1 design-stage fold: stagger closed_at via direct INSERT (NOT
    _insert_trade's now.isoformat()) — Windows clock granularity (15.6ms)
    can produce ties under tight loops, making ORDER BY closed_at DESC
    non-deterministic on ties.

    R1-I3 design-stage fold: also asserts closed_at is monotonically
    non-increasing across page0+page1, not just len() and disjoint ids.
    """
    c, db = client
    base = datetime.now(timezone.utc)
    for i in range(25):
        opened = (base - timedelta(hours=2, seconds=i)).isoformat()
        closed = (base - timedelta(seconds=i)).isoformat()
        await db._conn.execute(
            """INSERT INTO paper_trades
               (token_id, symbol, name, chain, signal_type, signal_data,
                entry_price, amount_usd, quantity, tp_pct, sl_pct,
                tp_price, sl_price, status, pnl_usd, pnl_pct,
                opened_at, closed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"coin-{i}",
                f"C{i}",
                f"coin-{i}".title(),
                "coingecko",
                "volume_spike",
                json.dumps({}),
                100.0,
                1000.0,
                10.0,
                20.0,
                10.0,
                120.0,
                90.0,
                "closed_tp",
                float(i),
                float(i),
                opened,
                closed,
            ),
        )
    await db._conn.commit()
    page0 = (await c.get("/api/trading/history?limit=20&offset=0")).json()
    page1 = (await c.get("/api/trading/history?limit=20&offset=20")).json()
    assert len(page0) == 20
    assert len(page1) == 5
    ids0 = {r["id"] for r in page0}
    ids1 = {r["id"] for r in page1}
    assert ids0.isdisjoint(ids1)
    all_closed = [r["closed_at"] for r in (page0 + page1)]
    assert all_closed == sorted(
        all_closed, reverse=True
    ), "rows not in closed_at DESC order"
