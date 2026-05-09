"""BL-NEW-LIVE-HYBRID M1 v2.1: per-venue services framework tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.live.services.balance_snapshot import BalanceSnapshot
from scout.live.services.base import VenueService
from scout.live.services.dormancy import DormancyJob
from scout.live.services.health_probe import HealthProbe
from scout.live.services.rate_limit_stub import RateLimitAccountantStub
from scout.live.services.runner import ServiceRunner


class _StubAdapter:
    venue_name = "stub"

    def __init__(self, *, balance: float = 100.0, raise_kind: str | None = None):
        self._balance = balance
        self._raise_kind = raise_kind

    async def fetch_account_balance(self, asset: str = "USDT") -> float:
        if self._raise_kind == "not_implemented":
            raise NotImplementedError("stub")
        if self._raise_kind == "transient":
            raise RuntimeError("stub transient")
        return self._balance


def test_venue_service_abc_has_required_attrs():
    assert hasattr(VenueService, "run_once")
    assert hasattr(VenueService, "cadence_seconds")
    assert hasattr(VenueService, "name")


def test_health_probe_has_60s_cadence():
    assert HealthProbe().cadence_seconds == 60.0


def test_balance_snapshot_has_300s_cadence():
    assert BalanceSnapshot().cadence_seconds == 300.0


def test_rate_limit_stub_returns_50_pct_constant():
    assert RateLimitAccountantStub.HEADROOM_PCT == 50.0


def test_dormancy_has_24h_cadence():
    assert DormancyJob().cadence_seconds == 86400.0


@pytest.mark.asyncio
async def test_health_probe_writes_venue_health_row(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    adapter = _StubAdapter(balance=500.0)
    probe = HealthProbe()
    await probe.run_once(adapter=adapter, db=db, venue="binance")
    cur = await db._conn.execute(
        "SELECT auth_ok, rest_responsive, last_balance_fetch_ok "
        "FROM venue_health WHERE venue = ?",
        ("binance",),
    )
    row = await cur.fetchone()
    assert row is not None
    assert tuple(row) == (1, 1, 1)
    await db.close()


@pytest.mark.asyncio
async def test_health_probe_marks_unhealthy_on_not_implemented(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    adapter = _StubAdapter(raise_kind="not_implemented")
    probe = HealthProbe()
    await probe.run_once(adapter=adapter, db=db, venue="binance")
    cur = await db._conn.execute(
        "SELECT rest_responsive, last_balance_fetch_ok, error_text "
        "FROM venue_health WHERE venue = ?",
        ("binance",),
    )
    row = await cur.fetchone()
    assert row[0] == 0  # not rest_responsive
    assert row[1] == 0  # last_balance_fetch_ok
    assert "NotImplementedError" in (row[2] or "")
    await db.close()


@pytest.mark.asyncio
async def test_health_probe_marks_auth_failed_on_transient(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    adapter = _StubAdapter(raise_kind="transient")
    probe = HealthProbe()
    await probe.run_once(adapter=adapter, db=db, venue="binance")
    cur = await db._conn.execute(
        "SELECT auth_ok, rest_responsive FROM venue_health WHERE venue = ?",
        ("binance",),
    )
    row = await cur.fetchone()
    assert row[0] == 0  # auth_ok turned off on non-timeout exception
    assert row[1] == 0
    await db.close()


@pytest.mark.asyncio
async def test_balance_snapshot_writes_wallet_snapshots_row(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    adapter = _StubAdapter(balance=500.0)
    snap = BalanceSnapshot()
    await snap.run_once(adapter=adapter, db=db, venue="binance")
    cur = await db._conn.execute(
        "SELECT venue, asset, balance, balance_usd FROM wallet_snapshots"
    )
    row = await cur.fetchone()
    assert row is not None
    assert tuple(row) == ("binance", "USDT", 500.0, 500.0)
    await db.close()


@pytest.mark.asyncio
async def test_balance_snapshot_skips_on_not_implemented(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    adapter = _StubAdapter(raise_kind="not_implemented")
    snap = BalanceSnapshot()
    await snap.run_once(adapter=adapter, db=db, venue="binance")
    cur = await db._conn.execute("SELECT COUNT(*) FROM wallet_snapshots")
    assert (await cur.fetchone())[0] == 0
    await db.close()


@pytest.mark.asyncio
async def test_rate_limit_stub_writes_50_pct_headroom(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    stub = RateLimitAccountantStub()
    await stub.run_once(adapter=None, db=db, venue="binance")
    cur = await db._conn.execute(
        "SELECT headroom_pct FROM venue_rate_state WHERE venue = ?",
        ("binance",),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == 50.0
    await db.close()


@pytest.mark.asyncio
async def test_dormancy_flags_zero_fill_venues(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    job = DormancyJob()
    # No live_trades rows → fills_30d=0 → is_dormant=1
    await job.run_once(adapter=None, db=db, venue="binance")
    cur = await db._conn.execute(
        "SELECT is_dormant, fills_30d_count FROM venue_health WHERE venue = ?",
        ("binance",),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == 1
    assert row[1] == 0
    await db.close()


@pytest.mark.asyncio
async def test_dormancy_clears_when_recent_fills_exist(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Seed a paper_trade + closed live_trade in the lookback window
    cur = await db._conn.execute("""INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_price, sl_price,
            status, opened_at)
           VALUES ('tok', 'X', 'x', 'ethereum', 'first_signal', '{}',
                   100, 50, 0.5, 120, 80, 'closed_tp',
                   '2026-05-08T00:00:00+00:00')""")
    paper_id = cur.lastrowid
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    await db._conn.execute(
        """INSERT INTO live_trades
           (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
            size_usd, status, created_at)
           VALUES (?, 'x', 'X', 'binance', 'XUSDT', 'first_signal',
                   '50', 'closed_tp', ?)""",
        (paper_id, recent),
    )
    await db._conn.commit()
    job = DormancyJob()
    await job.run_once(adapter=None, db=db, venue="binance")
    cur = await db._conn.execute(
        "SELECT is_dormant, fills_30d_count FROM venue_health WHERE venue = ?",
        ("binance",),
    )
    row = await cur.fetchone()
    assert row[0] == 0
    assert row[1] == 1
    await db.close()


@pytest.mark.asyncio
async def test_runner_starts_and_stops_cleanly(tmp_path):
    """Smoke test: ServiceRunner.start() spawns N tasks; .stop() cancels."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    adapters = {"binance": _StubAdapter()}
    services = [HealthProbe()]
    # Override cadence so the loop sleeps a long time after first run
    services[0].cadence_seconds = 60.0
    runner = ServiceRunner(db=db, adapters=adapters, services=services)
    await runner.start()
    # Let the harness fire run_once at least once
    await asyncio.sleep(0.1)
    assert len(runner._tasks) == 1
    await runner.stop()
    assert runner._tasks == []
    await db.close()
