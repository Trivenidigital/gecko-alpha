**New primitives introduced:** `/api/trade_inbox` read-only grouped opportunity endpoint; `TradeInboxTab.jsx` trader triage UI; trade-window labels (`open`, `closing`, `late`, `closed`, `unknown`); deterministic `trade_score`.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Trader triage dashboard UI | none found in Hermes Skills Hub; relevant built-ins are generic dashboards/design/code agents, not gecko-alpha trade triage | build project-native UI using existing dashboard patterns |
| Crypto trading/market-data skills | yes, ecosystem has crypto/trading skills such as CoinGecko/GoldRush/Binance/Kraken entries, but they fetch data or automate trading rather than classify gecko-alpha paper signals | do not integrate; this PR must stay read-only and source from existing DB state |
| Agent orchestration / alerts | Hermes supports cron/gateway/skills, but this feature is an operator dashboard surface, not a new Hermes job | defer Hermes alerting bridge to a separate operator-gated PR |

Awesome-hermes-agent ecosystem check: `0xNyk/awesome-hermes-agent` lists agent analytics/dashboard and trading ecosystem resources, but no drop-in replacement for gecko-alpha's durable paper-trade DB, `/api/live_candidates`, actionability fields, or dashboard contract. Verdict: KEEP_CUSTOM, reuse existing local cockpit endpoint semantics.

# Trade Opportunity Inbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a read-only trader-facing inbox that turns many early detections into a short ranked queue: Act Now, Watch, Already Ran, and Blocked.

**Architecture:** Extract a shared unsliced live-candidate row builder in `dashboard/db.py`, then expose a new grouped endpoint that scores/groups the broad bounded cohort before applying `limit_per_group`. The endpoint adds `trade_score`, `sort_key`, `window_state`, `action_label`, `block_reason_primary`, and `why_now`. The frontend adds a `Trade Inbox` tab focused on grouped action queues while preserving the existing Now Tradable evidence table.

**Tech Stack:** FastAPI, Pydantic v2 models, async SQLite via `aiosqlite`, React/Vite dashboard, pytest/httpx ASGI tests.

---

## Drift Check

Existing primitives found:

- `/api/live_candidates` exists in `dashboard/api.py` and returns deterministic read-only candidate rows.
- `dashboard/db.py:get_live_candidates` already enriches open paper trades with price, actionability, verdicts, risk reasons, source surfaces, and stale-price state, but it globally limits the returned page before any inbox grouping.
- `dashboard/frontend/components/NowTradableTab.jsx` renders the current visibility-only candidate table.
- Top Gainers Tracker already shows historical early-detection evidence, but not a current trader triage queue.

Residual gap:

- The UI answers "did we catch this early?" better than "what should I inspect right now?"
- Rows like TOES can be detected early and still be buried because historical gains, actionability labels, and evidence rows share the same flat table.
- There is no grouped, deterministic trader queue with window-state language (`OPEN`, `CLOSING`, `LATE`, `CLOSED`) and no score that penalizes lateness.
- Reusing a globally sliced `/api/live_candidates` page would reproduce the burial failure, so the inbox must score/group a broad bounded cohort first and slice per group second.

## Non-Goals

- No execution, order placement, sizing, pruning, auto-disable, or threshold changes.
- No Telegram/desktop alerting in this PR.
- No paid vendor calls or new external data sources.
- No persistent server-side reviewed/snooze/pin state in V1; client-local new/seen/dismissed-until-reload state is allowed because it avoids DB writes while making the inbox usable during a desk session.
- No changes to `paper_trades` or production schema.

## Files

- Modify `dashboard/models.py`
  - Add Pydantic response models for the inbox envelope and grouped rows.
- Modify `dashboard/db.py`
  - Extract a shared unsliced/broad live-candidate row builder so `/api/live_candidates` can keep its existing page contract while `/api/trade_inbox` can group before slicing.
  - Add pure helper functions for scoring/window classification.
  - Add `get_trade_inbox(db_path, limit_per_group, window_hours)`.
  - Reuse `get_live_candidates` rows to avoid duplicating SQL joins in V1.
- Modify `dashboard/api.py`
  - Add `GET /api/trade_inbox`.
- Create `dashboard/frontend/components/TradeInboxTab.jsx`
  - Render grouped trader queues with compact rows.
- Modify `dashboard/frontend/App.jsx`
  - Add top-level `Trade Inbox` tab.
- Update `dashboard/frontend/dist/`
  - Rebuild Vite output after frontend source changes.
- Modify `tests/test_live_candidates_endpoint.py` or create `tests/test_trade_inbox_endpoint.py`
  - Add focused ASGI tests for grouping, scoring, window states, and read-only meta.
- Modify `tasks/todo.md`
  - Record the autonomous work item and verification/review evidence.

## Task 1: Backend Contract And Scoring Tests

**Files:**
- Create: `tests/test_trade_inbox_endpoint.py`
- Modify later: `dashboard/models.py`, `dashboard/db.py`, `dashboard/api.py`

- [ ] **Step 1: Write failing ASGI tests for grouped inbox shape**

Create tests that seed open trades through the existing paper-trade helper pattern and assert:

```python
async def test_trade_inbox_groups_act_now_watch_already_ran_and_blocked(client):
    # candidate_review + fresh_entry + fresh price -> act_now
    # actionable but already_ran -> already_ran
    # stale-warning but not hard-stale -> watch with stale badge
    # actionable=0, missing price, hard-stale price, or bad timestamp -> blocked
    resp = await c.get("/api/trade_inbox?limit_per_group=10&window_hours=36")
    assert resp.status_code == 200
    payload = resp.json()
    assert set(payload["groups"]) == {"act_now", "watch", "already_ran", "blocked"}
    assert payload["meta"]["read_only"] is True
    assert payload["groups"]["act_now"][0]["action_label"] == "REVIEW_NOW"
```

- [ ] **Step 2: Run red test**

Run:

```powershell
C:/projects/gecko-alpha/.venv/Scripts/python.exe -m pytest -q tests/test_trade_inbox_endpoint.py
```

Expected: fails because `/api/trade_inbox` does not exist.

- [ ] **Step 3: Write failing deterministic scoring/window test**

Add tests that assert:

```python
assert row["window_state"] in {"open", "closing", "late", "closed", "unknown"}
assert row["sort_key"]
assert payload["groups"]["act_now"][0]["trade_score"] >= payload["groups"]["watch"][0]["trade_score"]
assert payload["groups"]["already_ran"][0]["window_state"] in {"late", "closed"}
```

- [ ] **Step 4: Run red test again**

Expected: still fails at missing route/model.

- [ ] **Step 5: Write failing broad-cohort TOES regression test**

Seed more than `limit_per_group` globally newer/non-actionable or already-ran rows plus one TOES-shaped candidate that is older/lower in raw live-candidate order but still fresh, actionable, and inside the open window. Assert TOES appears in `groups.act_now` after grouping-before-slicing.

## Task 2: Backend Implementation

**Files:**
- Modify: `dashboard/models.py`
- Modify: `dashboard/db.py`
- Modify: `dashboard/api.py`

- [ ] **Step 1: Add Pydantic models**

Add models with these fields:

```python
TradeInboxGroup = Literal["act_now", "watch", "already_ran", "blocked"]
TradeInboxWindowState = Literal["open", "closing", "late", "closed", "unknown"]
TradeInboxActionLabel = Literal["REVIEW_NOW", "WATCH_PULLBACK", "TOO_LATE", "BLOCKED", "DATA_MISSING"]

class TradeInboxRow(BaseModel):
    token_id: str
    symbol: str | None = None
    name: str | None = None
    chain: str | None = None
    group: TradeInboxGroup
    action_label: TradeInboxActionLabel
    window_state: TradeInboxWindowState
    trade_score: float
    sort_key: list[str | float | int] = Field(default_factory=list)
    why_now: list[str] = Field(default_factory=list)
    inclusion_reasons: list[str] = Field(default_factory=list)
    risk_reasons: list[str] = Field(default_factory=list)
    surfaces: list[str] = Field(default_factory=list)
    open_trade_ids: list[int] = Field(default_factory=list)
    recent_trade_ids: list[int] = Field(default_factory=list)
    actionable: int | None = None
    would_be_live: int | None = None
    block_reason_primary: str | None = None
    opened_at: str | None = None
    pct_from_entry: float | None = None
    price_change_24h: float | None = None
    market_cap: float | None = None
    current_price: float | None = None
    entry_quality: str | None = None
    verdict: str | None = None
    price_updated_at: str | None = None
    price_is_stale: bool = False
    price_staleness_minutes: float | None = None
    opened_age_hours: float | None = None

class TradeInboxMeta(BaseModel):
    read_only: bool = True
    not_trade_advice: bool = True
    experimental: bool = True
    generated_at: str
    window_hours: int
    limit_per_group: int
    rows_returned: int
    source_rows_considered: int
    open_trades_scanned: int
    group_counts: dict[str, int] = Field(default_factory=dict)
    source: str = "live_candidates"

class TradeInboxResponse(BaseModel):
    meta: TradeInboxMeta
    groups: dict[TradeInboxGroup, list[TradeInboxRow]]
```

- [ ] **Step 2: Extract broad candidate builder**

Refactor the row-building internals of `get_live_candidates` into a private helper that can return a broad bounded cohort before final page slicing. Preserve `/api/live_candidates` output and ordering. `get_trade_inbox` must request a source cap large enough to cover all groups, classify/score every source row, then apply `limit_per_group` independently to each group. Record `source_rows_considered` and `open_trades_scanned` in meta.

- [ ] **Step 3: Implement pure classifier helpers**

In `dashboard/db.py`, add:

```python
def _trade_window_state(row: dict) -> str:
    if row.get("current_price") is None:
        return "unknown"
    pct = row.get("pct_from_entry")
    if pct is None:
        return "unknown"
    if pct < -10:
        return "closed"
    if pct <= 8:
        return "open"
    if pct <= 25:
        return "closing"
    return "late"
```

Use these stale/unknown rules:

- `NO_PRICE`, missing `pct_from_entry`, or unparseable `opened_at` -> `blocked`.
- hard-stale price (`price_staleness_minutes >= 120`) -> `blocked` with `STALE_PRICE`.
- stale warning (`60 <= price_staleness_minutes < 120`) -> `watch` unless already late/closed; show stale badge and score penalty.
- low-movement rows older than the requested `window_hours` -> `watch` even if `window_state == "open"`; they are not `act_now`.

Add scoring as a transparent sort tuple first, numeric display second:

| Factor | Points / priority |
|---|---:|
| candidate_review verdict | +35 |
| actionable == 1 | +25 |
| would_be_live == 1 | +10 |
| entry_quality fresh_entry | +15 |
| entry_quality acceptable_pullback | +8 |
| window_state open | +15 |
| window_state closing | +6 |
| price fresh under 60m | +8 |
| each extra surface, max 3 extras | +2 |
| positive 24h momentum, capped | `min(10, price_change_24h / 5)` |
| stale warning | -12 |
| each risk reason, max 5 | -3 |
| late | -35 |
| closed | -50 |

Clamp to `0..100`, round to one decimal, and sort within each group by: `window_rank`, `trade_score DESC`, `opened_at DESC nulls last`, `token_id ASC`. Store a JSON-safe `sort_key` so contract tests can assert deterministic ordering. Generate deterministic `why_now` strings such as `window=open`, `fresh_entry`, `actionable=1`, `price_fresh`, `momentum_24h_positive`, and `surfaces=2`.

- [ ] **Step 4: Implement grouping**

Rules:

- `blocked`: missing data, hard-stale price, actionable `0`, unparseable timestamps, or `verdict == data_insufficient`. Include `block_reason_primary` as one of `NO_PRICE`, `STALE_PRICE`, `NOT_ACTIONABLE`, `BAD_TIMESTAMP`, `DATA_INSUFFICIENT`.
- `already_ran`: `window_state in {"late", "closed"}` and not blocked.
- `act_now`: `verdict == candidate_review`, `window_state in {"open", "closing"}`, price fresh, and not old-low-movement beyond `window_hours`. Open rows sort above closing rows.
- `watch`: everything else not blocked/already_ran.

- [ ] **Step 5: Add endpoint**

In `dashboard/api.py`:

```python
@app.get("/api/trade_inbox", response_model=TradeInboxResponse)
async def get_trade_inbox(
    limit_per_group: int = Query(10, ge=1, le=30),
    window_hours: int = Query(36, ge=6, le=72),
):
    return await db.get_trade_inbox(
        _db_path, limit_per_group=limit_per_group, window_hours=window_hours
    )
```

- [ ] **Step 6: Run backend tests**

Run:

```powershell
C:/projects/gecko-alpha/.venv/Scripts/python.exe -m pytest -q tests/test_trade_inbox_endpoint.py tests/test_live_candidates_endpoint.py
```

Expected: pass.

## Task 3: Frontend Trade Inbox

**Files:**
- Create: `dashboard/frontend/components/TradeInboxTab.jsx`
- Modify: `dashboard/frontend/App.jsx`
- Rebuild: `dashboard/frontend/dist/`

- [ ] **Step 1: Add `TradeInboxTab.jsx`**

Build a dense operator screen:

- Header: `Trade Inbox`
- Subtitle: `Read-only triage over open paper trades. Not execution advice.`
- Four sections in order: `Act Now`, `Watch`, `Already Ran`, `Blocked`
- Rows show: token link, action badge, window badge, new/seen/changed badge, score, from-entry pct, 24h pct, mcap, sources, why-now/risk text.
- Poll `/api/trade_inbox?limit_per_group=10&window_hours=36` every 30 seconds.
- Maintain client-local session state for `new`, `seen_this_session`, `changed_group`, and dismissed-until-reload rows using `sessionStorage`/component state only; do not write to the server.
- Include loading, fetch error, last refresh, manual refresh, pause/resume auto-refresh, stale API response, per-group empty states, and an explicit zero-act-now state.

- [ ] **Step 2: Add tab to `App.jsx`**

Add import, tab button, and render branch:

```jsx
import TradeInboxTab from './components/TradeInboxTab.jsx'
...
<button className={`tab-btn ${activeTab === 'trade_inbox' ? 'active' : ''}`} onClick={() => setActiveTab('trade_inbox')}>
  Trade Inbox
</button>
...
{activeTab === 'trade_inbox' && <TradeInboxTab />}
```

- [ ] **Step 3: Rebuild frontend**

Run:

```powershell
cd dashboard/frontend
npm.cmd run build:codex
```

Expected: Vite build succeeds and `dist/index.html` points at the new hash.

## Task 4: Static And Contract Verification

**Files:**
- Modify: `tasks/todo.md`

- [ ] **Step 1: Add static frontend regression test**

Add a small static assertion that `TradeInboxTab.jsx` exists, `App.jsx` wires the `trade_inbox` tab, and the component fetches `/api/trade_inbox`.

- [ ] **Step 2: Record run in `tasks/todo.md`**

Add top entry with:

- plan/design review folds
- backend/frontend verification commands
- PR link placeholder
- no execution/trading behavior changed

- [ ] **Step 3: Run final local verification**

Run:

```powershell
C:/projects/gecko-alpha/.venv/Scripts/python.exe -m pytest -q tests/test_trade_inbox_endpoint.py tests/test_live_candidates_endpoint.py
C:/projects/gecko-alpha/.venv/Scripts/python.exe -m pytest -q tests/test_dashboard_frontend_layout.py
C:/projects/gecko-alpha/.venv/Scripts/python.exe -m pytest --tb=short -q
npm.cmd run build:codex
git diff --check origin/master..HEAD
```

Expected: all pass/clean.

## Acceptance Criteria

- `/api/trade_inbox` returns a read-only envelope with deterministic groups.
- `act_now` contains candidates still in an open/closing window; `already_ran` removes late movers from the action queue.
- Rows include `trade_score`, `window_state`, `action_label`, and concise `why_now`.
- Dashboard has a top-level `Trade Inbox` tab.
- No production trading, sizing, pruning, or config behavior changes.
- Focused backend tests and frontend build pass.

## Review Gates

- Plan reviewed by two parallel agents; Critical/Important folds applied before design.
- Design reviewed by two parallel agents; Critical/Important folds applied before build.
- PR reviewed by two parallel agents; Critical/Important folds applied before deploy discussion.
