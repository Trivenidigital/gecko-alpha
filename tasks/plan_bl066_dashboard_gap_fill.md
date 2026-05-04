# BL-066': TG-social dashboard gap-fill — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**New primitives introduced:** new endpoint `GET /api/tg_social/dlq` returning `list[dict]` with shape `{id, channel_handle, msg_id, raw_text_preview, error_class, error_text, failed_at, retried_at}`; extended response from existing `GET /api/tg_social/alerts` with one new `stats_24h` key (`cashtag_dispatched: int`) and three new keys per channel object (`cashtag_trade_eligible: bool`, `cashtag_dispatched_today: int`, `cashtag_cap_per_day: int`); new dashboard helper functions `dashboard/db.py::get_tg_social_dlq(db_path, limit)`, `dashboard/db.py::get_tg_social_cashtag_stats_24h(db_path)`, `dashboard/db.py::get_tg_social_per_channel_cashtag_today(db_path)`; module-level `dashboard/api.py::_DASHBOARD_SETTINGS` singleton cached at import (D2/A2 fix); new frontend component `dashboard/frontend/components/TGDLQPanel.jsx` rendered inside existing `TGAlertsTab.jsx`; no new DB tables, columns, or settings.

**Prerequisites:** master ≥ `835ce7f` (BL-065 deployed — `tg_social_channels.cashtag_trade_eligible` column exists; `paper_trades.signal_data` carries `{"resolution": "cashtag", "channel_handle": "@X", ...}` for cashtag dispatches).

**v2 changes from 2-agent plan-review feedback:**
- **MUST-FIX M1** (acc5aaa schema mismatch — hard blocker): rewrote `_insert_cashtag_paper_trade` test fixture to match deployed `paper_trades` schema (scout/db.py:557-600): all NOT NULL columns supplied (`symbol`, `name`, `amount_usd`, `quantity`, `tp_price`, `sl_price`); dropped non-existent `contract_address` column; renamed `qty` → `quantity`. UNIQUE(token_id, signal_type, opened_at) honored via per-row `token_id` parameter.
- **MUST-FIX A1/M2** (BOTH agents — calendar-day vs rolling-24h drift): `get_tg_social_per_channel_cashtag_today` now uses `opened_at >= datetime('now', 'start of day')` to mirror `dispatcher.py:_channel_cashtag_trades_today_count` exactly. Without this, the dashboard `N/cap` badge would diverge from the dispatcher's actual gate decision near midnight UTC. The 24h `stats_24h.cashtag_dispatched` rollup intentionally stays on rolling 24h — it's a different surface (bird's-eye recency, not cap enforcement).
- **MUST-FIX A2/M3** (BOTH agents — `Settings()` per request): hoisted to module-level `_DASHBOARD_SETTINGS` singleton at import time with defensive try/except so a misconfigured `.env` no longer 500s the existing alerts endpoint. Pydantic Settings re-reads `.env` on every instantiation; per-request was a regression on existing surface.
- **MUST-FIX M4** (acc5aaa — Self-Review #8 wrong about Task 4↔5 coupling): explicitly acknowledged in revised Self-Review #8; added `??` defaults in Task 5 JSX so frontend renders `–` not `undefined / undefined` if shipped against an unextended API.
- **MUST-FIX A3** (a2c188a — test bootstrap): docstring on `_seed_db` references the canonical `tests/test_bl065_cashtag_dispatch.py::db` fixture and confirms `Database.initialize()` runs the BL-065 migration.
- **SHOULD-FIX D2/S1** (BOTH agents — promised-but-undelivered primitive): removed `cashtag_blocked_by_gate: dict[str, int]` from new-primitives marker (logs-not-table; out of scope).
- **SHOULD-FIX D5** (a2c188a — signal_data contract test): added `test_contract_bl065_signal_data_shape_includes_resolution_and_channel` in Task 4 Step 6 — pins producer/consumer JSON-key coupling so a future BL-065 refactor fails loudly here, not silently in the dashboard.
- **SHOULD-FIX S2** (acc5aaa — DB rollback scenario): defensive try/except in Task 4 channels query falls back to old shape if `cashtag_trade_eligible` column missing (DB rolled back to pre-BL-065 while dashboard rolled forward).
- **SHOULD-FIX S4** (acc5aaa — type assertions): backward-compat regression test now `isinstance`-checks new keys (`bool`, `int`).
- **SHOULD-FIX D4** (a2c188a — broken-vs-no-data disambiguation): §5 step 6 verifies KEY PRESENCE not value; step 9 adds operator-driven one-shot end-to-end verify (flip channel → confirm badge → revert).
- **SHOULD-FIX D3** (a2c188a — atomic frontend bundle flip): §5 step 1 explicitly notes FastAPI serves `dist/` via StaticFiles from same uvicorn — no CDN drain needed.
- **SHOULD-FIX S6** (acc5aaa — error baseline capture): §5 step 0a captures pre-deploy error count; step 7 compares post-deploy delta.
- **SHOULD-FIX D1** (a2c188a — strengthen split justification): added 3-point rationale to `/api/tg_social/dlq` docstring (payload size, ?limit= ergonomics, refresh cadence asymmetry).
- **NIT N3** (acc5aaa — FastAPI Query bounds): `limit: int = Query(20, ge=1, le=100)` for the DLQ endpoint (matches existing handler idiom in api.py).
- **NIT N2** (acc5aaa): added Prerequisites line above.

## Hermes-first analysis

**Domains checked against the 671-skill hub at `hermes-agent.nousresearch.com/docs/skills` (verified 2026-05-04):**

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Dashboard endpoint generation / FastAPI scaffolding | None found (closest: `webhook-subscriptions` for event delivery, not REST scaffolding) | Build from scratch (extending existing `dashboard/api.py:create_app`) |
| Telegram-channel monitoring / status visualization | None found | Build from scratch (extending existing `TGAlertsTab.jsx`) |
| Dead-letter-queue UI / inspector pattern | None found | Build from scratch (new `TGDLQPanel.jsx` + new `/api/tg_social/dlq` endpoint) |
| Sqlite-to-API adapter / read-only query helpers | None found | Reuse existing `dashboard/db.py:_ro_db` async context manager pattern (mode=ro URI) |
| Signal-flow timeline / message stream display | None found | Reuse existing message table render in `TGAlertsTab.jsx`; no new component |
| Real-time message stream (poll vs websocket) | None found | Reuse existing 15s `setInterval` poll pattern in `TGAlertsTab.jsx` (websocket exists but only used for live candidate alerts) |

**Awesome-hermes-agent ecosystem check:** 4 dashboard repos exist (`hermes-workspace`, `mission-control`, `hermes-webui`, `hermes-ui`) and 1 monitoring toolkit (`hermes-ai-infrastructure-monitoring-toolkit`), but ALL are general-purpose agent fleet/infra dashboards, not Telegram-channel-status surfaces. None replace the existing FastAPI + Vite/React app already deployed at `dashboard/`.

**Verdict:** Pure internal dashboard extension with **no Hermes-skill replacement**. Building inline by extending the deployed `dashboard/api.py` + `TGAlertsTab.jsx` is the only path. Drift check confirms the existing codebase patterns are well-established (composite endpoint shape, `_ro_db` context manager, panel/table component layout) and the gap-fill should follow them, not introduce new infrastructure.

---

## Drift check (per alignment doc Part 3)

**Read before drafting (verified):**
- `dashboard/api.py:1-100` (factory pattern, `_get_scout_db` cache, REST decorator style)
- `dashboard/api.py:724-837` (existing `/api/tg_social/alerts` composite endpoint — the surface we're extending)
- `dashboard/db.py:25-58` (`_ro_db` async context manager — read-only mode=ro URI; the helper pattern we'll reuse)
- `dashboard/frontend/components/TGAlertsTab.jsx` (already shipped UI with channels, 24h stats, recent messages — the component we're extending)
- `dashboard/frontend/App.jsx:185` (tab routing — `tg` tab activates TGAlertsTab; no new tab needed)
- `tg_social_dlq` schema: `(id, channel_handle, msg_id, raw_text, error_class, error_text, failed_at, retried_at)` with `idx_tg_social_dlq_failed_at` index
- `tg_social_signals` schema: `(message_pk, token_id, symbol, mcap_at_sighting, resolution_state, source_channel_handle, alert_sent_at, paper_trade_id, created_at)`
- BL-065 cashtag dispatch shape: `paper_trades.signal_data` JSON contains `{"resolution": "cashtag", "channel_handle": "@X", "cashtag": "$Y", "candidate_rank": 1, ...}`; the cashtag-specific gates (`cashtag_disabled` / `cashtag_below_floor` / `cashtag_ambiguous` / `cashtag_no_candidates` / `cashtag_channel_rate_limited` / `cashtag_dispatch_exception`) are emitted as structured log events (`tg_social_cashtag_admission_blocked` with `gate_name=...`) — NOT persisted to a relational table

**Pattern conformance:**
- Single composite endpoint extension (preferred over 5-endpoint sprawl as the original BL-066 spec proposed) — matches existing `/api/tg_social/alerts` shape
- New focused endpoint `/api/tg_social/dlq` for the DLQ inspector (justified — DLQ rows have a different cardinality and refresh cadence than recent messages; coupling them inflates the composite payload unnecessarily)
- Read-only DB access via `_ro_db` (no mutation, no race risk)
- Frontend: extend existing `TGAlertsTab.jsx` rather than create a new tab (BL-066 dashboard scope already lives there)

---

**Goal:** Surface BL-065 cashtag-dispatch outcomes + DLQ detail in the dashboard so the operator can debug TG-social pipeline health without SSHing into the VPS.

**Architecture:** Two scoped extensions. (1) Existing `/api/tg_social/alerts` gains 2 cashtag-dispatch keys in `stats_24h` and 3 cashtag-related keys per channel — read directly from `paper_trades.signal_data` via `json_extract`. (2) New `/api/tg_social/dlq` endpoint reads `tg_social_dlq` table with limit + recency-ordered, returning truncated raw_text for at-a-glance debugging. Frontend extends `TGAlertsTab.jsx` with new columns + a new `<TGDLQPanel />` panel below recent messages.

**Tech Stack:** FastAPI (async), aiosqlite, React 18 (Vite), pytest + httpx for endpoint tests, `_ro_db` read-only sqlite URI mode.

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `dashboard/db.py` | Add 3 helpers: `get_tg_social_dlq`, `get_tg_social_cashtag_stats_24h`, `get_tg_social_per_channel_cashtag_today` | Modify |
| `dashboard/api.py` | Register `GET /api/tg_social/dlq`; extend handler at line 724-837 to merge cashtag stats into existing response | Modify |
| `dashboard/frontend/components/TGAlertsTab.jsx` | Add 3 new column headers + cells in Channels table; add `<TGDLQPanel />` mount point | Modify |
| `dashboard/frontend/components/TGDLQPanel.jsx` | New component fetching `/api/tg_social/dlq?limit=20`, rendering DLQ table | Create |
| `tests/test_dashboard_tg_social_extensions.py` | Endpoint tests + helper tests (TDD: failing first, then implement) | Create |

**Why split off `TGDLQPanel.jsx` rather than inline into `TGAlertsTab.jsx`:** TGAlertsTab is already 222 lines and getting busy. The DLQ panel has its own fetch + 15s poll lifecycle and a distinct empty-state ("no failures last 7d — pipeline healthy"). Splitting keeps each file focused; the panel renders inline inside TGAlertsTab via `<TGDLQPanel />`.

---

## Tasks

### Task 1: DLQ DB helper

**Files:**
- Modify: `dashboard/db.py` (add `get_tg_social_dlq` after existing `get_tg_social_*` helpers)
- Test: `tests/test_dashboard_tg_social_extensions.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dashboard_tg_social_extensions.py
"""BL-066' dashboard gap-fill tests: DLQ endpoint + cashtag stats."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from dashboard import db as dash_db
from scout.db import Database


async def _seed_db(db_path: str):
    """Bootstrap pattern matches `tests/test_bl065_cashtag_dispatch.py::db`
    fixture (canonical) — `Database.initialize()` runs ALL migrations
    including BL-065's `bl065_cashtag_trade_eligible` (per scout/db.py
    `_migrate_feedback_loop_schema`). Without this the cashtag column
    inserts in Tasks 3-4 fail with `no such column`."""
    sd = Database(db_path)
    await sd.initialize()
    return sd


async def _insert_dlq(db_path: str, *, channel: str, msg_id: int,
                      error_class: str, error_text: str,
                      raw_text: str, failed_at: str):
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO tg_social_dlq "
            "(channel_handle, msg_id, raw_text, error_class, error_text, failed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (channel, msg_id, raw_text, error_class, error_text, failed_at),
        )
        await conn.commit()


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
    # raw_text is truncated to 240 chars (matches alerts text_preview convention)
    assert "raw_text_preview" in r
    assert len(r["raw_text_preview"]) <= 240
```

- [ ] **Step 2: Run test to verify it fails**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_dashboard_tg_social_extensions.py::test_get_tg_social_dlq_returns_recent_failures -v
```

Expected: FAIL with `AttributeError: module 'dashboard.db' has no attribute 'get_tg_social_dlq'`

- [ ] **Step 3: Implement `get_tg_social_dlq` in `dashboard/db.py`**

Add after the existing `get_trading_stats_by_signal` function:

```python
async def get_tg_social_dlq(db_path: str, limit: int = 20) -> list[dict]:
    """Recent tg_social DLQ entries, ordered by failed_at DESC.

    raw_text is truncated to 240 chars (mirrors text_preview convention
    in get_tg_social_alerts handler) so the response stays under the
    payload budget — full text accessible by SSH if needed.
    """
    async with _ro_db(db_path) as conn:
        cur = await conn.execute(
            "SELECT id, channel_handle, msg_id, raw_text, "
            "error_class, error_text, failed_at, retried_at "
            "FROM tg_social_dlq "
            "ORDER BY failed_at DESC "
            "LIMIT ?",
            (max(1, min(limit, 100)),),
        )
        rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "channel_handle": r[1],
                "msg_id": r[2],
                "raw_text_preview": (r[3] or "")[:240],
                "error_class": r[4],
                "error_text": r[5],
                "failed_at": r[6],
                "retried_at": r[7],
            }
            for r in rows
        ]
```

- [ ] **Step 4: Run test to verify it passes**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_dashboard_tg_social_extensions.py::test_get_tg_social_dlq_returns_recent_failures -v
```

Expected: PASS

- [ ] **Step 5: Add bound-check test (limit clamping)**

Add to `tests/test_dashboard_tg_social_extensions.py`:

```python
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
    # raw_text truncation
    assert all(len(r["raw_text_preview"]) <= 240 for r in rows)
```

Run: `SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_dashboard_tg_social_extensions.py::test_get_tg_social_dlq_clamps_limit_to_100 -v` — Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add dashboard/db.py tests/test_dashboard_tg_social_extensions.py
git commit -m "feat(BL-066'): add get_tg_social_dlq DB helper with limit clamp + raw_text truncation"
```

---

### Task 2: DLQ endpoint registration

**Files:**
- Modify: `dashboard/api.py` (register `GET /api/tg_social/dlq` after existing `/api/tg_social/alerts`)
- Test: `tests/test_dashboard_tg_social_extensions.py`

- [ ] **Step 1: Write the failing endpoint test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_dashboard_tg_social_extensions.py::test_endpoint_tg_social_dlq_returns_json -v
```

Expected: FAIL with HTTP 404 (endpoint not registered)

- [ ] **Step 3: Register endpoint in `dashboard/api.py`**

Add immediately after the existing `get_tg_social_alerts` handler (after the closing `}` near line 837):

```python
    @app.get("/api/tg_social/dlq")
    async def get_tg_social_dlq_endpoint(
        limit: int = Query(20, ge=1, le=100),
    ):
        """BL-066' DLQ inspector. Recent failures with truncated raw_text.

        DLQ row schema: (channel_handle, msg_id, raw_text, error_class,
        error_text, failed_at, retried_at). Last entry as of 2026-05-04
        was 2026-04-28 (post-PR #55 listener resilience deploy stabilized
        the listener); empty-state expected to be the common case.

        Split from /api/tg_social/alerts (kept as separate endpoint) because:
        (1) DLQ rows carry ~240-char raw_text payloads — coupling them to
        the 15s-poll composite alerts response would inflate every poll
        with ~empty data; (2) ?limit= parameterization is natural here
        (operator scrolling failures) but awkward on the composite endpoint
        where alerts/channels/health/stats have different natural sizes;
        (3) DLQ refresh cadence is slower (30s in TGDLQPanel vs 15s in
        TGAlertsTab) — combining would force the slower cadence on the hot
        stats panel.
        """
        return await db.get_tg_social_dlq(_db_path, limit=limit)
```

- [ ] **Step 4: Run test to verify it passes**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_dashboard_tg_social_extensions.py::test_endpoint_tg_social_dlq_returns_json -v
```

Expected: PASS

- [ ] **Step 5: Add empty-state test**

```python
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
```

Run: `SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_dashboard_tg_social_extensions.py::test_endpoint_tg_social_dlq_empty_returns_empty_list -v` — Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add dashboard/api.py tests/test_dashboard_tg_social_extensions.py
git commit -m "feat(BL-066'): register /api/tg_social/dlq endpoint"
```

---

### Task 3: Cashtag stats helpers

**Files:**
- Modify: `dashboard/db.py` (add `get_tg_social_cashtag_stats_24h`, `get_tg_social_per_channel_cashtag_today`)
- Test: `tests/test_dashboard_tg_social_extensions.py`

- [ ] **Step 1: Write failing 24h stats test**

```python
async def _insert_cashtag_paper_trade(
    db_path: str, *, channel: str, opened_at: str,
    cashtag: str = "ABC", token_id: str | None = None,
):
    """Insert a paper_trade row matching the deployed schema (scout/db.py
    line 557-600). All NOT NULL columns supplied: token_id, symbol, name,
    chain, signal_type, signal_data, entry_price, amount_usd, quantity,
    tp_price, sl_price, opened_at. tp_pct/sl_pct/status use schema defaults.
    UNIQUE(token_id, signal_type, opened_at) — pass distinct token_id per
    insert when seeding multiples for the same channel within the same call."""
    async with aiosqlite.connect(db_path) as conn:
        # Use a unique-per-row token_id so the UNIQUE constraint doesn't
        # collide when seeding multiple cashtag dispatches for one channel.
        tid = token_id or f"abc-coin-{opened_at}"
        signal_data = (
            f'{{"resolution": "cashtag", "channel_handle": "{channel}", '
            f'"cashtag": "{cashtag}", "candidate_rank": 1, "candidates_total": 3}}'
        )
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


@pytest.mark.asyncio
async def test_get_tg_social_cashtag_stats_24h_counts_dispatched(tmp_path):
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    now = datetime.now(timezone.utc)
    # 2 dispatches in last 24h
    await _insert_cashtag_paper_trade(
        db_path, channel="@thanos_mind", opened_at=now.isoformat()
    )
    await _insert_cashtag_paper_trade(
        db_path, channel="@thanos_mind",
        opened_at=(now - timedelta(hours=2)).isoformat(),
    )
    # 1 outside window — should NOT count
    await _insert_cashtag_paper_trade(
        db_path, channel="@thanos_mind",
        opened_at=(now - timedelta(hours=30)).isoformat(),
    )
    stats = await dash_db.get_tg_social_cashtag_stats_24h(db_path)
    assert stats["dispatched"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_dashboard_tg_social_extensions.py::test_get_tg_social_cashtag_stats_24h_counts_dispatched -v
```

Expected: FAIL with `AttributeError`

- [ ] **Step 3: Implement `get_tg_social_cashtag_stats_24h`**

Add to `dashboard/db.py`:

```python
async def get_tg_social_cashtag_stats_24h(db_path: str) -> dict:
    """BL-066' cashtag-dispatch rollup: count of paper_trades opened in
    last 24h whose signal_data carries resolution=cashtag.

    Returns {"dispatched": int}. "blocked_by_gate" is a separate concern
    (logs not table) and intentionally not surfaced here — see Task 4
    where the API handler stitches it from a future log-tap if added,
    or returns None today.
    """
    async with _ro_db(db_path) as conn:
        cur = await conn.execute(
            """SELECT COUNT(*)
               FROM paper_trades
               WHERE signal_type = 'tg_social'
                 AND json_extract(signal_data, '$.resolution') = 'cashtag'
                 AND datetime(opened_at) >= datetime('now', '-24 hours')"""
        )
        row = await cur.fetchone()
        return {"dispatched": row[0] if row else 0}
```

- [ ] **Step 4: Run test to verify it passes**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_dashboard_tg_social_extensions.py::test_get_tg_social_cashtag_stats_24h_counts_dispatched -v
```

Expected: PASS

- [ ] **Step 5: Write failing per-channel cashtag-today test**

```python
@pytest.mark.asyncio
async def test_get_tg_social_per_channel_cashtag_today_returns_counts(tmp_path):
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    now = datetime.now(timezone.utc)
    # Seed channels first so the function returns rows even when count=0.
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO tg_social_channels (channel_handle, trade_eligible, "
            "safety_required, cashtag_trade_eligible, added_at) "
            "VALUES ('@thanos_mind', 1, 0, 1, ?), "
            "       ('@nebukadnaza', 0, 1, 0, ?)",
            (now.isoformat(), now.isoformat()),
        )
        await conn.commit()
    # 3 cashtag dispatches today for thanos (post-midnight today UTC)
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_2am = today_midnight + timedelta(hours=2)
    today_3am = today_midnight + timedelta(hours=3)
    today_4am = today_midnight + timedelta(hours=4)
    # Note: if `now` is itself before 02:00 UTC, push these into yesterday
    # so the test isn't flaky near midnight; pick three distinct seconds
    # in the same calendar day as `now`.
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
```

- [ ] **Step 6: Run test to verify it fails**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_dashboard_tg_social_extensions.py::test_get_tg_social_per_channel_cashtag_today_returns_counts -v
```

Expected: FAIL with `AttributeError`

- [ ] **Step 7: Implement `get_tg_social_per_channel_cashtag_today`**

Add to `dashboard/db.py`:

```python
async def get_tg_social_per_channel_cashtag_today(db_path: str) -> dict[str, int]:
    """BL-066' per-channel cashtag dispatches since UTC midnight.

    Mirrors the **calendar-day** semantics of the dispatcher's gate at
    `scout/social/telegram/dispatcher.py:_channel_cashtag_trades_today_count`
    (which uses `opened_at >= datetime('now', 'start of day')`). If we
    used a rolling 24h window instead, the dashboard would lie about cap
    utilization — at 06:00 UTC, a channel that hit cap=5 yesterday at
    23:00 would read `5/5 (warn)` here but `0/5` to the dispatcher, and
    the next dispatch would actually go through. **The two surfaces MUST
    use identical date math.**

    Returns dict keyed by channel_handle; channels with zero dispatches
    are omitted (frontend defaults missing keys to 0).
    """
    async with _ro_db(db_path) as conn:
        cur = await conn.execute(
            """SELECT json_extract(signal_data, '$.channel_handle') AS ch,
                      COUNT(*) AS n
               FROM paper_trades
               WHERE signal_type = 'tg_social'
                 AND json_extract(signal_data, '$.resolution') = 'cashtag'
                 AND opened_at >= datetime('now', 'start of day')
               GROUP BY ch"""
        )
        rows = await cur.fetchall()
        return {r[0]: r[1] for r in rows if r[0]}
```

- [ ] **Step 8: Run test to verify it passes**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_dashboard_tg_social_extensions.py::test_get_tg_social_per_channel_cashtag_today_returns_counts -v
```

Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add dashboard/db.py tests/test_dashboard_tg_social_extensions.py
git commit -m "feat(BL-066'): add cashtag stats DB helpers (24h dispatched + per-channel today)"
```

---

### Task 4: Extend `/api/tg_social/alerts` response

**Files:**
- Modify: `dashboard/api.py:724-837` (existing handler — append cashtag fields to response)
- Test: `tests/test_dashboard_tg_social_extensions.py`

- [ ] **Step 1: Write failing extension test**

```python
@pytest.mark.asyncio
async def test_endpoint_tg_social_alerts_includes_cashtag_dispatched_in_stats(tmp_path):
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO tg_social_channels (channel_handle, trade_eligible, "
            "safety_required, cashtag_trade_eligible, added_at) "
            "VALUES ('@thanos_mind', 1, 0, 1, ?)",
            (now.isoformat(),),
        )
        await conn.commit()
    await _insert_cashtag_paper_trade(
        db_path, channel="@thanos_mind", opened_at=now.isoformat()
    )
    from httpx import ASGITransport, AsyncClient
    from dashboard.api import create_app
    app = create_app(db_path=db_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/tg_social/alerts")
    body = resp.json()
    # New: cashtag_dispatched in stats_24h
    assert body["stats_24h"]["cashtag_dispatched"] == 1
    # New: per-channel cashtag_trade_eligible + cashtag_dispatched_today
    ch = next(c for c in body["channels"] if c["channel_handle"] == "@thanos_mind")
    assert ch["cashtag_trade_eligible"] is True
    assert ch["cashtag_dispatched_today"] == 1
    assert ch["cashtag_cap_per_day"] == 5  # PAPER_TG_SOCIAL_CASHTAG_MAX_PER_CHANNEL_PER_DAY default
```

- [ ] **Step 2: Run test to verify it fails**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_dashboard_tg_social_extensions.py::test_endpoint_tg_social_alerts_includes_cashtag_dispatched_in_stats -v
```

Expected: FAIL with `KeyError: 'cashtag_dispatched'` (or `cashtag_trade_eligible`)

- [ ] **Step 3: Extend `dashboard/api.py:724-837` handler**

Three minimal edits inside the existing `get_tg_social_alerts` handler:

(a-pre) Hoist `Settings()` to a module-level singleton at the **top of `dashboard/api.py`** (after existing imports). Pydantic Settings re-reads `.env` on every instantiation; calling `Settings()` per request burns a syscall + lets a mid-flight `.env` edit silently change served values mid-session, AND a `ValidationError` from a malformed `.env` would 500 the existing alerts endpoint that previously didn't depend on Settings (regression on existing surface):

```python
# Top of dashboard/api.py (after existing imports near line 20)
try:
    from scout.config import Settings as _ScoutSettings
    _DASHBOARD_SETTINGS = _ScoutSettings()
except Exception as _e:  # pragma: no cover — only on misconfigured .env
    # Defensive: a misconfigured .env must not 500 read-only dashboard endpoints.
    # Fall back to a sentinel so handlers can detect + show an honest "settings
    # unavailable" rather than crash. The pipeline service would have failed
    # at startup if Settings was actually broken; this is paranoia for the
    # dashboard-points-at-foreign-DB rollback case.
    _DASHBOARD_SETTINGS = None
    import structlog as _structlog
    _structlog.get_logger().error(
        "dashboard_settings_init_failed",
        err=str(_e),
    )
```

(a) Add `cashtag_trade_eligible` to the channels query and dict, with defensive try/except for the `cashtag_trade_eligible` column to handle the rollback-against-older-DB case (the column was added in BL-065 / `835ce7f`; a fresh dashboard pointing at a pre-BL-065 DB would 500 with `no such column`):

```python
        # Defensive: the cashtag_trade_eligible column was added in BL-065
        # migration. If the dashboard is rolled forward to BL-066' but the
        # underlying scout.db is from a pre-BL-065 snapshot (rollback
        # scenario), the SELECT below would 500. Try the new shape first;
        # fall back to the old shape with cashtag_trade_eligible defaulted
        # to 0 (safe — operator hasn't migrated the data yet either).
        try:
            ch_cur = await conn.execute(
                """SELECT channel_handle, trade_eligible, safety_required,
                          cashtag_trade_eligible, removed_at, added_at
                   FROM tg_social_channels ORDER BY added_at"""
            )
            ch_rows = await ch_cur.fetchall()
            _has_cashtag_col = True
        except aiosqlite.OperationalError as e:
            if "cashtag_trade_eligible" not in str(e):
                raise
            ch_cur = await conn.execute(
                """SELECT channel_handle, trade_eligible, safety_required,
                          removed_at, added_at
                   FROM tg_social_channels ORDER BY added_at"""
            )
            ch_rows = [(r[0], r[1], r[2], 0, r[3], r[4]) for r in await ch_cur.fetchall()]
            _has_cashtag_col = False

        # Per-channel cashtag dispatches today (BL-066') — calendar-day semantics
        # mirroring scout/social/telegram/dispatcher.py:_channel_cashtag_trades_today_count
        cashtag_today = (
            await db.get_tg_social_per_channel_cashtag_today(_db_path)
            if _has_cashtag_col else {}
        )
        # Read cap from cached module-level Settings singleton (NOT per-request).
        cap_per_day = (
            _DASHBOARD_SETTINGS.PAPER_TG_SOCIAL_CASHTAG_MAX_PER_CHANNEL_PER_DAY
            if _DASHBOARD_SETTINGS is not None else 5
        )
        channels = [
            {
                "channel_handle": r[0],
                "trade_eligible": bool(r[1]),
                "safety_required": bool(r[2]),
                "cashtag_trade_eligible": bool(r[3]),
                "cashtag_dispatched_today": cashtag_today.get(r[0], 0),
                "cashtag_cap_per_day": cap_per_day,
                "removed": r[4] is not None,
                "added_at": r[5],
            }
            for r in ch_rows
        ]
```

(b) Add cashtag stats merge below the existing `dlq` count:

```python
        cashtag_stats = await db.get_tg_social_cashtag_stats_24h(_db_path)
        # ... existing dlq count line stays ...
```

(c) Add `cashtag_dispatched` key inside `stats_24h`:

```python
            "stats_24h": {
                "messages": s[0] or 0,
                "with_ca": s[1] or 0,
                "with_cashtag": s[2] or 0,
                "signals_resolved": sig[0] or 0,
                "trades_dispatched": sig[1] or 0,
                "cashtag_dispatched": cashtag_stats["dispatched"],
                "dlq": dlq,
            },
```

- [ ] **Step 4: Run test to verify it passes**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_dashboard_tg_social_extensions.py::test_endpoint_tg_social_alerts_includes_cashtag_dispatched_in_stats -v
```

Expected: PASS

- [ ] **Step 5: Add backward-compat regression test (existing keys still present)**

```python
@pytest.mark.asyncio
async def test_endpoint_tg_social_alerts_existing_keys_preserved(tmp_path):
    """BL-066' must not break the existing TGAlertsTab consumer."""
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)
    # Seed one channel so isinstance type checks have a row to inspect.
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO tg_social_channels (channel_handle, trade_eligible, "
            "safety_required, cashtag_trade_eligible, added_at) "
            "VALUES ('@bc', 1, 0, 1, ?)",
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
    # Existing top-level keys must remain
    for key in ["channels", "health", "stats_24h", "alerts"]:
        assert key in body, f"BL-066' broke backward compat: missing {key}"
    # Existing stats_24h keys must remain
    for key in ["messages", "with_ca", "with_cashtag",
                "signals_resolved", "trades_dispatched", "dlq"]:
        assert key in body["stats_24h"], (
            f"BL-066' broke backward compat: stats_24h missing {key}"
        )
    # New keys must be the right TYPE (not just present-with-wrong-shape).
    # Catches a regression where a helper returns int 1 instead of bool True
    # — ?: 'yes' : 'no' would still render correctly for ints in JSX, but
    # downstream consumers (any future operator-typed code) would break.
    ch = body["channels"][0]
    assert isinstance(ch["cashtag_trade_eligible"], bool)
    assert isinstance(ch["cashtag_dispatched_today"], int)
    assert isinstance(ch["cashtag_cap_per_day"], int)
    assert isinstance(body["stats_24h"]["cashtag_dispatched"], int)
```

Run: `SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_dashboard_tg_social_extensions.py::test_endpoint_tg_social_alerts_existing_keys_preserved -v` — Expected: PASS

- [ ] **Step 6: Add contract test pinning BL-065 signal_data shape (D5)**

The dashboard hard-codes `json_extract(signal_data, '$.resolution')` and `'$.channel_handle'`. If BL-065's dispatcher ever renames those keys (`resolution` → `resolution_kind`), the dashboard silently shows zeros — no test fails. This contract test pins the producer/consumer coupling in one spot:

```python
@pytest.mark.asyncio
async def test_contract_bl065_signal_data_shape_includes_resolution_and_channel(tmp_path):
    """BL-066' depends on BL-065's signal_data shape. If BL-065 renames keys,
    THIS test fails loudly — without it, the dashboard would silently report
    0 cashtag dispatches and the operator would never know the contract drifted."""
    import inspect
    from scout.social.telegram import dispatcher as bl065_dispatcher
    src = inspect.getsource(bl065_dispatcher)
    # The dispatcher writes signal_data inside dispatch_cashtag_to_engine.
    # We assert the literal JSON-key names this dashboard reads via json_extract.
    assert '"resolution": "cashtag"' in src or "'resolution': 'cashtag'" in src, (
        "BL-065 dispatcher no longer writes 'resolution'='cashtag' literal — "
        "dashboard's json_extract($.resolution) will silently return 0. "
        "Update both dashboard/db.py:get_tg_social_per_channel_cashtag_today "
        "AND dashboard/db.py:get_tg_social_cashtag_stats_24h."
    )
    assert '"channel_handle"' in src or "'channel_handle'" in src, (
        "BL-065 dispatcher no longer includes 'channel_handle' in signal_data — "
        "dashboard per-channel rollup will return empty dict."
    )
```

Run: `SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_dashboard_tg_social_extensions.py::test_contract_bl065_signal_data_shape_includes_resolution_and_channel -v` — Expected: PASS (verifies current shape, fails fast if BL-065 dispatcher is refactored).

- [ ] **Step 7: Commit**

```bash
git add dashboard/api.py tests/test_dashboard_tg_social_extensions.py
git commit -m "feat(BL-066'): extend /api/tg_social/alerts with cashtag dispatch visibility + signal_data contract test"
```

---

### Task 5: Frontend — TGAlertsTab cashtag columns

**Files:**
- Modify: `dashboard/frontend/components/TGAlertsTab.jsx` (add 2 columns to channels table; add 1 stat card to stats row)

- [ ] **Step 1: Add `Cashtag` column header and cell to channels table**

In `TGAlertsTab.jsx`, find the channels table `<thead>` block and add a header:

```jsx
<thead>
  <tr>
    <th>Channel</th>
    <th>Trade-eligible</th>
    <th>Safety required</th>
    <th>Cashtag-eligible</th>
    <th>Cashtag today</th>
    <th>Listener</th>
    <th>Last message</th>
  </tr>
</thead>
```

In the `<tbody>` row generator, add the cells. The `cashtag today` cell renders as `N / cap`, with a warning style if N >= cap (operator near the per-channel cap). **Defensive**: missing keys (e.g., if Task 4's API extension is reverted while Task 5's frontend stays — Tasks 4↔5 are coupled per Self-Review #8 but not strictly atomic at deploy time) render `–` rather than `undefined / undefined`:

```jsx
<td>{c.cashtag_trade_eligible ? 'yes' : 'no'}</td>
<td>
  <span className={
    (c.cashtag_dispatched_today ?? 0) >= (c.cashtag_cap_per_day ?? Infinity)
      ? 'tg-badge tg-badge-warn'
      : 'tg-badge tg-badge-muted'
  }>
    {c.cashtag_dispatched_today ?? '–'} / {c.cashtag_cap_per_day ?? '–'}
  </span>
</td>
```

- [ ] **Step 2: Add `Cashtag dispatched` stat card to top stat row**

In `TGAlertsTab.jsx`, append a new `<div className="tg-stat">` to the `tg-stat-row` (immediately after the existing `Trades Dispatched` card):

```jsx
<div className="tg-stat">
  <div className="tg-stat-label">Cashtag Dispatched</div>
  <div className="tg-stat-value">{stats.cashtag_dispatched ?? 0}</div>
</div>
```

- [ ] **Step 3: Build the frontend**

```bash
cd dashboard/frontend && npm install && npm run build
```

Expected: build completes; `dist/` updated.

- [ ] **Step 4: Smoke-test in browser**

Run dashboard locally if uvicorn is set up; otherwise verify build artifacts:

```bash
ls -la dashboard/frontend/dist/
```

Expected: `index.html` exists with the new component bundle.

- [ ] **Step 5: Commit**

```bash
git add dashboard/frontend/components/TGAlertsTab.jsx dashboard/frontend/dist/
git commit -m "feat(BL-066'): TGAlertsTab cashtag columns + stat card"
```

---

### Task 6: Frontend — TGDLQPanel component

**Files:**
- Create: `dashboard/frontend/components/TGDLQPanel.jsx`
- Modify: `dashboard/frontend/components/TGAlertsTab.jsx` (mount `<TGDLQPanel />` below the recent-messages panel)

- [ ] **Step 1: Create `TGDLQPanel.jsx`**

```jsx
// dashboard/frontend/components/TGDLQPanel.jsx
import React, { useEffect, useState } from 'react'

function fmtTime(iso) {
  if (!iso) return '–'
  try {
    const d = new Date(iso)
    return d.toLocaleString([], {
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  } catch {
    return iso
  }
}

export default function TGDLQPanel() {
  const [rows, setRows] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const res = await fetch('/api/tg_social/dlq?limit=20')
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const json = await res.json()
        if (!cancelled) {
          setRows(json)
          setError(null)
        }
      } catch (e) {
        if (!cancelled) setError(String(e))
      }
    }
    load()
    const t = setInterval(load, 30_000)  // 30s — DLQ changes slowly
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [])

  if (error) {
    return (
      <div className="panel">
        <div className="panel-header">DLQ — recent failures</div>
        <div className="empty-state">Failed to load: {error}</div>
      </div>
    )
  }
  if (!rows) {
    return (
      <div className="panel">
        <div className="panel-header">DLQ — recent failures</div>
        <div className="empty-state">Loading…</div>
      </div>
    )
  }
  if (rows.length === 0) {
    return (
      <div className="panel">
        <div className="panel-header">DLQ — recent failures</div>
        <div className="empty-state">
          No DLQ entries — pipeline healthy
        </div>
      </div>
    )
  }
  return (
    <div className="panel">
      <div className="panel-header">DLQ — recent failures ({rows.length})</div>
      <table className="tg-table">
        <thead>
          <tr>
            <th>Failed at</th>
            <th>Channel</th>
            <th>Msg id</th>
            <th>Error class</th>
            <th>Error</th>
            <th>Raw text</th>
            <th>Retried at</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(r => (
            <tr key={r.id}>
              <td>{fmtTime(r.failed_at)}</td>
              <td>{r.channel_handle}</td>
              <td>{r.msg_id}</td>
              <td>
                <span className="tg-badge tg-badge-warn">
                  {r.error_class}
                </span>
              </td>
              <td className="tg-text-cell">{r.error_text}</td>
              <td className="tg-text-cell">
                {r.raw_text_preview || '(empty)'}
              </td>
              <td>{r.retried_at ? fmtTime(r.retried_at) : '–'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
```

- [ ] **Step 2: Mount `<TGDLQPanel />` in `TGAlertsTab.jsx`**

At the top of `TGAlertsTab.jsx` add the import:

```jsx
import TGDLQPanel from './TGDLQPanel.jsx'
```

Inside the `<div className="tg-alerts">` returned JSX, append `<TGDLQPanel />` AFTER the existing recent-messages panel (the one that closes after the `</tbody></table>` block):

```jsx
      {/* ... existing panels above ... */}
      <TGDLQPanel />
    </div>
  )
}
```

- [ ] **Step 3: Build the frontend**

```bash
cd dashboard/frontend && npm run build
```

Expected: build completes without errors.

- [ ] **Step 4: Smoke-test bundle**

```bash
ls -la dashboard/frontend/dist/assets/ | head -5
```

Expected: bundle hash changed.

- [ ] **Step 5: Commit**

```bash
git add dashboard/frontend/components/TGDLQPanel.jsx dashboard/frontend/components/TGAlertsTab.jsx dashboard/frontend/dist/
git commit -m "feat(BL-066'): add TGDLQPanel component to dashboard"
```

---

### Task 7: Final regression sweep + push

- [ ] **Step 1: Run BL-066' test file**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_dashboard_tg_social_extensions.py -v --tb=short
```

Expected: all tests PASS.

- [ ] **Step 2: Run targeted regression on adjacent test files**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_dashboard*.py tests/test_bl065_cashtag_dispatch.py tests/test_tg_social_dispatcher.py -q --tb=short
```

Expected: all PASS (no regression in dashboard or BL-065 tests).

- [ ] **Step 3: Push branch**

```bash
git push origin feat/bl-066-dashboard-gap-fill
```

Expected: branch published.

- [ ] **Step 4: Verify CI green (excluding pre-existing flake)**

`test_heartbeat_mcap_missing.py` is a known pre-existing flake (CI hits real api.coingecko.com from runners — same failure on master `cbb1e7f` and `b51324c`). Any other failure is a real regression and blocks the PR.

```bash
gh pr checks <PR#> --watch
```

Expected: only the heartbeat flake fails; everything else green.

---

## Deploy verification (§5 — operational verification post-deploy)

**Deploy sequence (deploy-stop-FIRST per BL-065 plan v3 §5 + BL-064 listener resilience pattern):**

0. **Pre-deploy backup:** `cp /root/gecko-alpha/scout.db /root/gecko-alpha/scout.db.bak.bl066.$(date +%s)`
0a. **Capture error baseline (S6):** `BASELINE_ERR=$(journalctl -u gecko-dashboard --since "10 minutes ago" --no-pager | grep -ciE "error|exception|traceback") ; echo "baseline=$BASELINE_ERR"` — record this number for step 7.
1. **Stop dashboard service FIRST:** `systemctl stop gecko-dashboard` — dashboard stops serving requests; pipeline (gecko-pipeline) does NOT need to stop because BL-066' touches no pipeline code. **Atomic frontend bundle flip (D3):** the FastAPI app serves `dashboard/frontend/dist/` via `StaticFiles` from the same uvicorn process — `systemctl stop` ensures the bundle and the API extension flip together; there is no CDN/edge cache to drain.
2. **Pull:** `cd /root/gecko-alpha && git pull origin master`
3. **Start dashboard:** `systemctl start gecko-dashboard`
4. **Service started cleanly:** `systemctl status gecko-dashboard` — active+running.
5. **New endpoint reachable + correct shape:**
   ```bash
   curl -s localhost:8000/api/tg_social/dlq?limit=5 | python3 -c "import sys, json; d=json.load(sys.stdin); assert isinstance(d, list), f'expected list, got {type(d)}'; print(f'dlq_count={len(d)}')"
   ```
   Expected: `dlq_count=0` (last DLQ entry was 2026-04-28, post-PR #55 stabilized listener).
6. **Existing endpoint extended — verify KEY PRESENCE not value (D4):**
   ```bash
   curl -s localhost:8000/api/tg_social/alerts | python3 -c "
   import sys, json
   d = json.load(sys.stdin)
   # Key presence checks — distinguish 'feature works, no data yet' from
   # 'wrong key name silently returns 0/missing'. On fresh deploy the
   # operator hasn't flipped any cashtag_trade_eligible=1, so the values
   # WILL legitimately be 0/False — but the keys MUST exist or the endpoint
   # is broken.
   assert 'cashtag_dispatched' in d['stats_24h'], 'stats_24h missing cashtag_dispatched key'
   ch = d['channels'][0] if d['channels'] else {}
   for k in ('cashtag_trade_eligible', 'cashtag_dispatched_today', 'cashtag_cap_per_day'):
       assert k in ch, f'channel object missing {k}'
   print(f'PASS: keys present. cashtag_dispatched={d[\"stats_24h\"][\"cashtag_dispatched\"]}, first_channel_cap={ch.get(\"cashtag_cap_per_day\")}')
   "
   ```
   Expected: `PASS: keys present. cashtag_dispatched=0, first_channel_cap=5` (or whatever cap_per_day is configured).
7. **No new exceptions vs baseline (S6):**
   ```bash
   POST_ERR=$(journalctl -u gecko-dashboard --since "3 minutes ago" --no-pager | grep -ciE "error|exception|traceback") ; echo "post=$POST_ERR baseline=$BASELINE_ERR"
   [ "$POST_ERR" -le "$BASELINE_ERR" ] && echo "OK: no new errors" || echo "REGRESSION: $((POST_ERR - BASELINE_ERR)) new error lines"
   ```
8. **Frontend bundle served:** `curl -sI localhost:8000/ | head -5` returns 200; visual confirm in browser at the public dashboard URL that the new "Cashtag Dispatched" stat card and DLQ panel render.
9. **Manual one-shot end-to-end verify (D4 cont'd) — run AFTER step 8 if all green:**
   ```bash
   # Pick a channel to opt in temporarily
   sqlite3 /root/gecko-alpha/scout.db "UPDATE tg_social_channels SET cashtag_trade_eligible=1 WHERE channel_handle='@thanos_mind'"
   # Wait ≤30s for next dashboard poll; visually confirm @thanos_mind row
   # now shows "Cashtag-eligible: yes" + "Cashtag today: 0 / 5" badge.
   # Then revert (operator will decide separately whether to keep enabled):
   sqlite3 /root/gecko-alpha/scout.db "UPDATE tg_social_channels SET cashtag_trade_eligible=0 WHERE channel_handle='@thanos_mind'"
   ```

**Revert path:** `cd /root/gecko-alpha && git checkout <previous-master-sha> && systemctl restart gecko-dashboard`. No DB rollback needed (no schema changes). API consumers see the absence of new keys gracefully (frontend defaults missing keys to 0 / empty list via `??` operators in Task 5 JSX).

---

## Self-Review

**1. Spec coverage:**
- DLQ inspector endpoint (`/api/tg_social/dlq`) → Tasks 1-2 ✓
- Cashtag-dispatch visibility in existing endpoint → Tasks 3-4 ✓
- Per-channel cashtag cap utilization → Task 3 (per-channel helper) + Task 4 (response field) + Task 5 (UI) ✓
- DLQ frontend panel → Task 6 ✓
- Backward compat preserved → Task 4 Step 5 regression test ✓

**2. Placeholder scan:** clean — every step has either exact code or exact command.

**3. Type consistency:**
- DB helpers all return `list[dict]` or `dict[str, int]` — matches existing dashboard/db.py convention.
- Endpoint response shape uses snake_case keys throughout, matches existing `tg_social/alerts` convention.
- New per-channel keys (`cashtag_trade_eligible`, `cashtag_dispatched_today`, `cashtag_cap_per_day`) are consistent across DB layer → API layer → frontend.

**4. New primitives marker:** present at top with all new endpoints/helpers/components/keys; no DB schema changes (verified — no migration needed).

**5. Hermes-first marker:** present immediately after new-primitives (per alignment doc 2026-05-04 convention) with 6/6 negative findings + verdict.

**6. Drift grounding:** explicit file:line references to all extended code, schema-verified DLQ shape, BL-065 signal_data shape verified against deployed code.

**7. TDD discipline:** every task starts with failing test → run → impl → run → commit. No "implement and add tests later".

**8. Cross-task coupling — honest accounting:** Tasks 1-3 (DB helpers) are independently revertable; they're additive to `dashboard/db.py`. Task 4 (endpoint extension) and Tasks 5-6 (frontend) are **coupled** — Task 5's JSX reads keys that only Task 4 produces. **Mitigation:** Task 5 uses `??` defaults (`c.cashtag_dispatched_today ?? '–'`) so a Task-4-reverted-Task-5-shipped scenario degrades to dashes, not undefined-rendering bugs. The Task 1+2 DLQ pair (helper + endpoint + panel — Tasks 1, 2, 6) is an INDEPENDENT shippable unit and could be split into its own PR if the operator prefers smaller deploys; the cashtag-visibility cluster (Tasks 3, 4, 5) is the second unit. Keeping them as one PR for this overnight run because both share the same dashboard service restart and the same review reviewers.

**9. Honest scope:** zero schema changes, zero new tables, zero new Settings; pure read-side composition over already-shipped data. The cashtag-blocked-by-gate breakdown is intentionally NOT included in this scope — it would require either log-tap infrastructure or a new audit table, both of which are bigger than gap-fill. Captured as **BL-066''-gate-stats** follow-up if the operator finds it valuable after BL-066' ships.
