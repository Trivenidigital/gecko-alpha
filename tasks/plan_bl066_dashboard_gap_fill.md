# BL-066': TG-social dashboard gap-fill — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**New primitives introduced:** new endpoint `GET /api/tg_social/dlq` returning `list[dict]` with shape `{id, channel_handle, msg_id, raw_text_preview, error_class, error_text, failed_at, retried_at}`; extended response from existing `GET /api/tg_social/alerts` with two new keys (`stats_24h.cashtag_dispatched`, `stats_24h.cashtag_blocked_by_gate: dict[str, int]`) and three new keys per channel object (`cashtag_trade_eligible: bool`, `cashtag_dispatched_today: int`, `cashtag_cap_per_day: int`); new dashboard helper functions `dashboard/db.py::get_tg_social_dlq(db_path, limit)`, `dashboard/db.py::get_tg_social_cashtag_stats_24h(db_path)`, `dashboard/db.py::get_tg_social_per_channel_cashtag_today(db_path)`; new frontend component `dashboard/frontend/components/TGDLQPanel.jsx` rendered inside existing `TGAlertsTab.jsx`; no new DB tables, columns, or settings.

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
    async def get_tg_social_dlq_endpoint(limit: int = 20):
        """BL-066' DLQ inspector. Recent failures with truncated raw_text.

        DLQ row schema: (channel_handle, msg_id, raw_text, error_class,
        error_text, failed_at, retried_at). Last entry as of 2026-05-04
        was 2026-04-28 (post-PR #55 listener resilience deploy stabilized
        the listener); empty-state expected to be the common case.
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
    cashtag: str = "ABC", trade_id: int | None = None,
):
    async with aiosqlite.connect(db_path) as conn:
        signal_data = (
            f'{{"resolution": "cashtag", "channel_handle": "{channel}", '
            f'"cashtag": "{cashtag}", "candidate_rank": 1, "candidates_total": 3}}'
        )
        await conn.execute(
            "INSERT INTO paper_trades "
            "(signal_type, contract_address, chain, token_id, "
            " entry_price, qty, opened_at, status, signal_data) "
            "VALUES "
            "('tg_social', 'ABC123', 'solana', 'abc-coin', "
            " 0.001, 100000, ?, 'open', ?)",
            (opened_at, signal_data),
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
    # 3 cashtag dispatches today for thanos
    for _ in range(3):
        await _insert_cashtag_paper_trade(
            db_path, channel="@thanos_mind", opened_at=now.isoformat()
        )
    # 1 dispatch yesterday for thanos — should NOT count toward "today"
    await _insert_cashtag_paper_trade(
        db_path, channel="@thanos_mind",
        opened_at=(now - timedelta(days=1, hours=2)).isoformat(),
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
    """BL-066' per-channel cashtag dispatches in the last 24h.

    Used by the dashboard to show how close each channel is to its
    PAPER_TG_SOCIAL_CASHTAG_MAX_PER_CHANNEL_PER_DAY cap (default 5).
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
                 AND datetime(opened_at) >= datetime('now', '-24 hours')
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

(a) Add `cashtag_trade_eligible` to the channels query and dict:

```python
        ch_cur = await conn.execute(
            """SELECT channel_handle, trade_eligible, safety_required,
                      cashtag_trade_eligible, removed_at, added_at
               FROM tg_social_channels ORDER BY added_at"""
        )
        # Per-channel cashtag dispatches today (BL-066')
        cashtag_today = await db.get_tg_social_per_channel_cashtag_today(_db_path)
        from scout.config import Settings
        cap_per_day = Settings().PAPER_TG_SOCIAL_CASHTAG_MAX_PER_CHANNEL_PER_DAY
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
            for r in await ch_cur.fetchall()
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
    from httpx import ASGITransport, AsyncClient
    from dashboard.api import create_app
    app = create_app(db_path=db_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/tg_social/alerts")
    body = resp.json()
    # Existing keys must remain
    for key in ["channels", "health", "stats_24h", "alerts"]:
        assert key in body, f"BL-066' broke backward compat: missing {key}"
    for key in ["messages", "with_ca", "with_cashtag",
                "signals_resolved", "trades_dispatched", "dlq"]:
        assert key in body["stats_24h"], (
            f"BL-066' broke backward compat: stats_24h missing {key}"
        )
```

Run: `SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_dashboard_tg_social_extensions.py::test_endpoint_tg_social_alerts_existing_keys_preserved -v` — Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add dashboard/api.py tests/test_dashboard_tg_social_extensions.py
git commit -m "feat(BL-066'): extend /api/tg_social/alerts with cashtag dispatch visibility"
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

In the `<tbody>` row generator, add the cells. The `cashtag today` cell renders as `N / cap`, with a warning style if N >= cap (operator near the per-channel cap):

```jsx
<td>{c.cashtag_trade_eligible ? 'yes' : 'no'}</td>
<td>
  <span className={
    c.cashtag_dispatched_today >= c.cashtag_cap_per_day
      ? 'tg-badge tg-badge-warn'
      : 'tg-badge tg-badge-muted'
  }>
    {c.cashtag_dispatched_today} / {c.cashtag_cap_per_day}
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
1. **Stop dashboard service FIRST:** `systemctl stop gecko-dashboard` — dashboard stops serving requests; pipeline (gecko-pipeline) does NOT need to stop because BL-066' touches no pipeline code.
2. **Pull:** `cd /root/gecko-alpha && git pull origin master`
3. **Start dashboard:** `systemctl start gecko-dashboard`
4. **Service started cleanly:** `systemctl status gecko-dashboard` — active+running.
5. **New endpoint reachable:** `curl -s localhost:8000/api/tg_social/dlq?limit=5 | head -200` returns JSON list (likely empty given last DLQ entry was 2026-04-28).
6. **Existing endpoint extended:** `curl -s localhost:8000/api/tg_social/alerts | python3 -c "import sys, json; d=json.load(sys.stdin); print('cashtag_dispatched=', d['stats_24h']['cashtag_dispatched']); print('first channel cashtag_trade_eligible=', d['channels'][0]['cashtag_trade_eligible'])"`
7. **No new exceptions:** `journalctl -u gecko-dashboard --since "3 minutes ago" --no-pager | grep -iE "error|exception|traceback" | head -10` — empty (or only pre-existing patterns).
8. **Frontend bundle served:** `curl -sI localhost:8000/ | head -5` returns 200; visual confirm in browser at the public dashboard URL that the new "Cashtag Dispatched" stat card and DLQ panel render.

**Revert path:** `cd /root/gecko-alpha && git checkout <previous-master-sha> && systemctl restart gecko-dashboard`. No DB rollback needed (no schema changes). API consumers see the absence of new keys gracefully (frontend defaults missing keys to 0 / empty list).

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

**8. No cross-task coupling:** Tasks 1-3 (DB helpers) → Task 4 (endpoint composition) → Tasks 5-6 (frontend); each task can be reverted independently because the DB helpers are additive, the endpoint extension is purely additive (existing keys preserved), and the frontend additions are gated by the new response keys (will degrade gracefully if API returns old shape).

**9. Honest scope:** zero schema changes, zero new tables, zero new Settings; pure read-side composition over already-shipped data. The cashtag-blocked-by-gate breakdown is intentionally NOT included in this scope — it would require either log-tap infrastructure or a new audit table, both of which are bigger than gap-fill. Captured as **BL-066''-gate-stats** follow-up if the operator finds it valuable after BL-066' ships.
