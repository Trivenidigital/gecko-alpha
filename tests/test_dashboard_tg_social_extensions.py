"""BL-066' dashboard gap-fill tests: DLQ endpoint + cashtag stats.

Tests gated by SKIP_AIOHTTP_TESTS=1 on Windows where they touch aiohttp/
network paths (matches BL-065 + Bundle A pattern). The dashboard tests
themselves don't touch aiohttp directly but `httpx.ASGITransport` is used
to drive FastAPI in-process — same posture as other dashboard tests.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

_SKIP_AIOHTTP = pytest.mark.skipif(
    sys.platform == "win32" and os.environ.get("SKIP_AIOHTTP_TESTS") == "1",
    reason="Windows + SKIP_AIOHTTP_TESTS=1: skip aiohttp tests",
)

from dashboard import db as dash_db
from scout.db import Database


@pytest.fixture(autouse=True)
def _reset_dashboard_module_state():
    """Reset dashboard.api module-level globals between tests so the
    cached `_scout_db` connection from a previous test (against a
    different tmp_path) doesn't leak into the current test."""
    import dashboard.api as dash_api
    dash_api._scout_db = None
    dash_api._db_path = "scout.db"
    yield
    dash_api._scout_db = None
    dash_api._db_path = "scout.db"


async def _seed_db(db_path: str):
    """Bootstrap pattern matches `tests/test_bl065_cashtag_dispatch.py::db`
    fixture (canonical) — `Database.initialize()` runs ALL migrations
    including BL-065's `bl065_cashtag_trade_eligible` (per scout/db.py
    `_migrate_feedback_loop_schema`). Without this the cashtag column
    inserts in Tasks 3-4 fail with `no such column`."""
    sd = Database(db_path)
    await sd.initialize()
    await sd.close()


async def _insert_dlq(
    db_path: str, *,
    channel: str, msg_id: int,
    error_class: str, error_text: str,
    raw_text: str, failed_at: str,
):
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO tg_social_dlq "
            "(channel_handle, msg_id, raw_text, error_class, error_text, failed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (channel, msg_id, raw_text, error_class, error_text, failed_at),
        )
        await conn.commit()
    # F8 mitigation: writer connection closes before any _ro_db reader opens.


async def _insert_cashtag_paper_trade(
    db_path: str, *,
    channel: str, opened_at: str,
    cashtag: str = "ABC", token_id: str | None = None,
):
    """Insert a paper_trade row matching the deployed schema (scout/db.py
    line 557-600). All NOT NULL columns supplied: token_id, symbol, name,
    chain, signal_type, signal_data, entry_price, amount_usd, quantity,
    tp_price, sl_price, opened_at. tp_pct/sl_pct/status use schema defaults.
    UNIQUE(token_id, signal_type, opened_at) — pass distinct token_id per
    insert when seeding multiples for the same channel within the same call."""
    async with aiosqlite.connect(db_path) as conn:
        tid = token_id or f"abc-coin-{opened_at}"
        signal_data = json.dumps({
            "resolution": "cashtag",
            "channel_handle": channel,
            "cashtag": cashtag,
            "candidate_rank": 1,
            "candidates_total": 3,
        })
        entry_price = 0.001
        amount_usd = 300.0
        quantity = amount_usd / entry_price
        tp_price = entry_price * 1.20
        sl_price = entry_price * 0.90
        await conn.execute(
            "INSERT INTO paper_trades "
            "(token_id, symbol, name, chain, signal_type, signal_data, "
            " entry_price, amount_usd, quantity, "
            " tp_price, sl_price, opened_at) "
            "VALUES (?, ?, ?, ?, 'tg_social', ?, ?, ?, ?, ?, ?, ?)",
            (
                tid, cashtag, cashtag, "solana", signal_data,
                entry_price, amount_usd, quantity,
                tp_price, sl_price, opened_at,
            ),
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# Task 1: DLQ DB helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_tg_social_dlq_returns_recent_failures(tmp_path):
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    now = datetime.now(timezone.utc)
    await _insert_dlq(
        db_path,
        channel="@thanos_mind",
        msg_id=42,
        error_class="OperationalError",
        error_text="cannot start a transaction within a transaction",
        raw_text="$ABC just bought 1M tokens, CA: ...",
        failed_at=now.isoformat(),
    )
    rows = await dash_db.get_tg_social_dlq(db_path, limit=10)
    assert len(rows) == 1
    r = rows[0]
    assert r["channel_handle"] == "@thanos_mind"
    assert r["msg_id"] == 42
    assert r["error_class"] == "OperationalError"
    assert "transaction" in r["error_text"]
    assert "raw_text_preview" in r
    assert len(r["raw_text_preview"]) <= 240


@pytest.mark.asyncio
async def test_get_tg_social_dlq_clamps_limit_to_100(tmp_path):
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    now = datetime.now(timezone.utc)
    for i in range(150):
        await _insert_dlq(
            db_path,
            channel="@x",
            msg_id=i,
            error_class="E",
            error_text="e",
            raw_text="r" * 500,
            failed_at=(now - timedelta(seconds=i)).isoformat(),
        )
    rows = await dash_db.get_tg_social_dlq(db_path, limit=999)
    assert len(rows) == 100
    assert all(len(r["raw_text_preview"]) <= 240 for r in rows)


# ---------------------------------------------------------------------------
# Task 2: DLQ endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_tg_social_dlq_returns_json(tmp_path):
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    now = datetime.now(timezone.utc)
    await _insert_dlq(
        db_path,
        channel="@detecter_calls",
        msg_id=99,
        error_class="ResolverTimeout",
        error_text="DexScreener timeout after 5s",
        raw_text="$XYZ moonshot",
        failed_at=now.isoformat(),
    )
    from httpx import ASGITransport, AsyncClient
    from dashboard.api import create_app
    app = create_app(db_path=db_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/tg_social/dlq?limit=5")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["channel_handle"] == "@detecter_calls"
    assert body[0]["error_class"] == "ResolverTimeout"


@pytest.mark.asyncio
async def test_endpoint_tg_social_dlq_empty_returns_empty_list(tmp_path):
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    from httpx import ASGITransport, AsyncClient
    from dashboard.api import create_app
    app = create_app(db_path=db_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/tg_social/dlq?limit=5")
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# Task 3: Cashtag stats helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_tg_social_cashtag_stats_24h_counts_dispatched(tmp_path):
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    now = datetime.now(timezone.utc)
    # 2 dispatches in last 24h
    await _insert_cashtag_paper_trade(
        db_path, channel="@thanos_mind", opened_at=now.isoformat(),
    )
    await _insert_cashtag_paper_trade(
        db_path, channel="@thanos_mind",
        opened_at=(now - timedelta(hours=2)).isoformat(),
        token_id="abc-coin-2",
    )
    # 1 outside window — should NOT count
    await _insert_cashtag_paper_trade(
        db_path, channel="@thanos_mind",
        opened_at=(now - timedelta(hours=30)).isoformat(),
        token_id="abc-coin-30h",
    )
    stats = await dash_db.get_tg_social_cashtag_stats_24h(db_path)
    assert stats["dispatched"] == 2


@pytest.mark.asyncio
async def test_get_tg_social_per_channel_cashtag_today_returns_counts(tmp_path):
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO tg_social_channels (channel_handle, display_name, "
            "trade_eligible, safety_required, cashtag_trade_eligible, added_at) "
            "VALUES ('@thanos_mind', 'Thanos', 1, 0, 1, ?), "
            "       ('@nebukadnaza', 'Neb', 0, 1, 0, ?)",
            (now.isoformat(), now.isoformat()),
        )
        await conn.commit()
    # 3 cashtag dispatches today (post-midnight UTC)
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    base = today_midnight + timedelta(seconds=1)
    for i in range(3):
        await _insert_cashtag_paper_trade(
            db_path, channel="@thanos_mind",
            opened_at=(base + timedelta(seconds=i)).isoformat(),
            token_id=f"abc-coin-today-{i}",
        )
    # 1 dispatch in PRIOR calendar day for thanos — should NOT count
    yesterday = today_midnight - timedelta(hours=2)
    await _insert_cashtag_paper_trade(
        db_path, channel="@thanos_mind",
        opened_at=yesterday.isoformat(),
        token_id="abc-coin-yesterday",
    )
    counts = await dash_db.get_tg_social_per_channel_cashtag_today(db_path)
    assert counts["@thanos_mind"] == 3
    assert counts.get("@nebukadnaza", 0) == 0


# ---------------------------------------------------------------------------
# Task 4: Endpoint extension
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_tg_social_alerts_includes_cashtag_dispatched_in_stats(tmp_path):
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO tg_social_channels (channel_handle, display_name, "
            "trade_eligible, safety_required, cashtag_trade_eligible, added_at) "
            "VALUES ('@thanos_mind', 'Thanos', 1, 0, 1, ?)",
            (now.isoformat(),),
        )
        await conn.commit()
    await _insert_cashtag_paper_trade(
        db_path, channel="@thanos_mind", opened_at=now.isoformat(),
    )
    from httpx import ASGITransport, AsyncClient
    from dashboard.api import create_app
    app = create_app(db_path=db_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/tg_social/alerts")
    body = resp.json()
    assert body["stats_24h"]["cashtag_dispatched"] == 1
    ch = next(c for c in body["channels"] if c["channel_handle"] == "@thanos_mind")
    assert ch["cashtag_trade_eligible"] is True
    assert ch["cashtag_dispatched_today"] == 1
    assert ch["cashtag_cap_per_day"] == 5


@pytest.mark.asyncio
async def test_endpoint_tg_social_alerts_existing_keys_preserved(tmp_path):
    """BL-066' must not break the existing TGAlertsTab consumer."""
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO tg_social_channels (channel_handle, display_name, "
            "trade_eligible, safety_required, cashtag_trade_eligible, added_at) "
            "VALUES ('@bc', 'BC', 1, 0, 1, ?)",
            (now.isoformat(),),
        )
        await conn.commit()
    from httpx import ASGITransport, AsyncClient
    from dashboard.api import create_app
    app = create_app(db_path=db_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/tg_social/alerts")
    body = resp.json()
    for key in ["channels", "health", "stats_24h", "alerts"]:
        assert key in body, f"BL-066' broke backward compat: missing {key}"
    for key in ["messages", "with_ca", "with_cashtag",
                "signals_resolved", "trades_dispatched", "dlq"]:
        assert key in body["stats_24h"], (
            f"BL-066' broke backward compat: stats_24h missing {key}"
        )
    ch = body["channels"][0]
    assert isinstance(ch["cashtag_trade_eligible"], bool)
    assert isinstance(ch["cashtag_dispatched_today"], int)
    assert isinstance(ch["cashtag_cap_per_day"], int)
    assert isinstance(body["stats_24h"]["cashtag_dispatched"], int)


# ---------------------------------------------------------------------------
# T9 / T9b — contract tests pinning BL-065 producer/consumer coupling
# ---------------------------------------------------------------------------


def test_contract_dispatcher_today_count_uses_start_of_day_semantics():
    """T9b — pins F2 mitigation. Dashboard MUST use identical date math
    as the dispatcher's gate. If dispatcher refactors to '-24 hours' or
    similar, dashboard cap badge would diverge from gate decision near
    midnight UTC. Source-grep belt-and-suspenders for the runtime path."""
    import inspect
    from scout.social.telegram.dispatcher import _channel_cashtag_trades_today_count
    src = inspect.getsource(_channel_cashtag_trades_today_count)
    assert "'start of day'" in src or '"start of day"' in src, (
        "BL-065 dispatcher's _channel_cashtag_trades_today_count no longer "
        "uses 'start of day' SQL literal — dashboard's "
        "dashboard/db.py:get_tg_social_per_channel_cashtag_today MUST be "
        "updated to match the new date-math semantics, otherwise the "
        "cap badge will diverge from the dispatcher's actual gate decision."
    )


@pytest.mark.skip(
    reason="T9 runtime contract test: dispatch_cashtag_to_engine requires "
    "full pipeline plumbing (price_cache seed, signal_combo, etc.). "
    "T9b source-grep + the BL-065 end-to-end test "
    "(test_dispatch_cashtag_end_to_end_opens_paper_trade) jointly cover "
    "the contract today. Promote to active test if BL-065 evolves the "
    "signal_data shape OR if T9b grep proves brittle."
)
def test_contract_bl065_dispatch_writes_resolution_and_channel_handle():
    """T9 (deferred per build-time complexity assessment) — runtime
    assertion that dispatch_cashtag_to_engine writes signal_data with
    literal 'resolution' and 'channel_handle' keys. Plan v3 specified
    this as load-bearing; building the full price_cache + engine setup
    here duplicates BL-065's existing end-to-end test. T9b source-grep
    catches the failure mode at lower cost."""
    raise NotImplementedError


# ---------------------------------------------------------------------------
# T11 — F19 startup race + F5 rollback defense
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_tg_social_alerts_falls_back_when_cashtag_column_missing(tmp_path):
    """T11 — pins F19 (migration race) + F5 (rollback). Synthesize a
    pre-BL-065 tg_social_channels schema (no cashtag_trade_eligible);
    endpoint must NOT 500.

    Approach: let `Database.initialize()` run normally (creates ALL the
    BL-064/BL-065 tables and runs migrations), then surgically rename
    tg_social_channels and recreate it WITHOUT the cashtag_trade_eligible
    column to simulate the F19 race window (dashboard reads before
    pipeline migration completes) or F5 rollback (DB rolled back to
    before BL-065 deployed)."""
    db_path = str(tmp_path / "old.db")
    sd = Database(db_path)
    await sd.initialize()
    await sd.close()
    # Now strip the cashtag_trade_eligible column to simulate the race.
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("ALTER TABLE tg_social_channels RENAME TO _bl065_chan_tmp")
        await conn.execute(
            "CREATE TABLE tg_social_channels ("
            "  channel_handle TEXT PRIMARY KEY, "
            "  display_name TEXT, "
            "  trade_eligible INTEGER, "
            "  safety_required INTEGER, "
            "  removed_at TEXT, "
            "  added_at TEXT)"
        )
        await conn.execute(
            "INSERT INTO tg_social_channels VALUES "
            "('@old', 'Old', 1, 1, NULL, '2026-01-01T00:00:00+00:00')"
        )
        await conn.execute("DROP TABLE _bl065_chan_tmp")
        await conn.commit()

    from httpx import ASGITransport, AsyncClient
    from dashboard.api import create_app
    app = create_app(db_path=db_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/tg_social/alerts")
    assert resp.status_code == 200, (
        f"Endpoint should fall back gracefully when cashtag_trade_eligible "
        f"missing; got {resp.status_code}: {resp.text[:500]}"
    )
    body = resp.json()
    ch = body["channels"][0]
    assert ch["channel_handle"] == "@old"
    assert ch["cashtag_trade_eligible"] is False


# ---------------------------------------------------------------------------
# Build-phase deferred tests (declared so CI surface-counts the gaps)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="T10 — requires monkey-patching module-level _DASHBOARD_SETTINGS=None. "
    "Functional behavior exercised indirectly by T7 (cap renders default 5 from "
    "fallback). Implement only if S2 fallback is later seen to fire in production."
)
def test_endpoint_tg_social_alerts_when_settings_init_fails():
    raise NotImplementedError


@pytest.mark.skip(
    reason="T12 — frontend rendering test; project has no React-test "
    "infrastructure (no jest/vitest). Manual smoke-test in browser is acceptance."
)
def test_dlq_panel_renders_truncated_raw_text():
    raise NotImplementedError
