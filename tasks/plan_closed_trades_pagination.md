**New primitives introduced:** New backend endpoint `GET /api/trading/history/count` returning `{total: int}` (count of closed paper trades, status != 'open'). New `dashboard/db.py` helper `get_trading_history_count(db_path) -> int`. New frontend pagination state in `dashboard/frontend/components/TradingTab.jsx` (`closedPage`, `closedTotal`, `CLOSED_PER_PAGE=20`) + pagination controls (Prev / page indicator / Next) rendered below the closed-trades table. No DB schema changes. No new Settings.

# Closed Trades Pagination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** Replace the current hardcoded "Last 20 completed trades" view with paginated access to all closed paper trades. 20 per page; Prev / Next controls.

**Architecture:** Backend `/api/trading/history?limit=20&offset=N*20` already supports pagination via existing `dashboard/db.py:get_trading_history`. Add a sibling count endpoint so the frontend can compute total pages. Frontend adds `closedPage` state, refetches history on page change with `offset=closedPage*20`, renders Prev/Next + "Page X of Y / N total" controls, and updates the section header.

**Tech Stack:** Python 3.12 + FastAPI (`dashboard/api.py`), aiosqlite (`dashboard/db.py`), React 18 (`dashboard/frontend/components/TradingTab.jsx`), Vite build (`dashboard/frontend/dist/`).

**Total scope:** ~25-30 steps across 5 tasks. Single PR; no schema migrations; no new Settings.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `dashboard/db.py` | Modify | Add `get_trading_history_count(db_path) -> int` (count where status != 'open') |
| `dashboard/api.py` | Modify | Add `GET /api/trading/history/count` endpoint returning `{total: int}` |
| `dashboard/frontend/components/TradingTab.jsx` | Modify | Add `closedPage` + `closedTotal` state; refetch on page change; render Prev/Next + page indicator; update section header text |
| `tests/test_dashboard_api.py` (or similar existing test file) | Modify | Tests for new count endpoint + offset pagination behavior |
| `dashboard/frontend/dist/` | Rebuild | `npm run build` from `dashboard/frontend/` to regenerate the deployed bundle |

---

## Task 0: Setup — branch verification

- [ ] **Step 1: Verify branch + clean tree**

```bash
git branch --show-current
# Expected: feat/closed-trades-pagination
git status --short scout/ dashboard/
# Expected: clean (no modifications to source dirs)
```

---

## Task 1: Backend — count endpoint + DB helper

**Files:**
- Modify: `dashboard/db.py`
- Modify: `dashboard/api.py`
- Modify: existing `tests/test_dashboard*.py` test file (find via `ls tests/ | grep -i dash`)

- [ ] **Step 1: Add `get_trading_history_count` to `dashboard/db.py`**

Just below `get_trading_history` (around line 985):

```python
async def get_trading_history_count(db_path: str) -> int:
    """Total count of closed paper trades (status != 'open').

    Read by /api/trading/history/count for frontend pagination math.
    Mirrors the WHERE clause of get_trading_history exactly so totals
    line up with the paginated rows.
    """
    async with _ro_db(db_path) as db:
        try:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE status != 'open'"
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0  # table doesn't exist yet
```

- [ ] **Step 2: Add `/api/trading/history/count` endpoint to `dashboard/api.py`**

Just below the `/api/trading/history` block (around line 204):

```python
    @app.get("/api/trading/history/count")
    async def get_trading_history_count_endpoint():
        """Total count of closed paper trades — for frontend pagination."""
        return {"total": await db.get_trading_history_count(_db_path)}
```

- [ ] **Step 3: Failing test in `tests/test_dashboard_*.py`**

Find an existing dashboard test file and append:

```python
@pytest.mark.asyncio
async def test_trading_history_count_endpoint(tmp_path, monkeypatch):
    """Count endpoint returns total closed paper trades."""
    # Use the existing test fixture pattern (build a temp scout.db, seed
    # paper_trades rows with mix of open + closed). Existing dashboard
    # tests use this pattern — copy the seed helper.
    db_path = str(tmp_path / "scout.db")
    await _seed_paper_trades(
        db_path,
        rows=[
            {"status": "closed_tp"},
            {"status": "closed_sl"},
            {"status": "closed_duration"},
            {"status": "open"},  # excluded
            {"status": "open"},  # excluded
        ],
    )
    from dashboard import db as ddb

    total = await ddb.get_trading_history_count(db_path)
    assert total == 3


@pytest.mark.asyncio
async def test_trading_history_count_endpoint_empty(tmp_path):
    """Count endpoint returns 0 when no trades."""
    db_path = str(tmp_path / "scout.db")
    # Don't seed paper_trades
    from dashboard import db as ddb

    total = await ddb.get_trading_history_count(db_path)
    assert total == 0


@pytest.mark.asyncio
async def test_trading_history_offset_pagination(tmp_path):
    """offset=20 returns trades 20-39 in closed_at DESC order. Confirms
    the existing get_trading_history offset arg works for our use case."""
    db_path = str(tmp_path / "scout.db")
    # Seed 50 closed trades with monotonically increasing closed_at.
    # Page 0 (offset=0, limit=20) → trades 50, 49, ..., 31
    # Page 1 (offset=20, limit=20) → trades 30, 29, ..., 11
    # Verify no overlap.
    ...
```

- [ ] **Step 4: Run + commit**

```bash
uv run --native-tls pytest tests/test_dashboard_*.py -q
git add dashboard/db.py dashboard/api.py tests/test_dashboard_*.py
git commit -m "feat(dashboard): /api/trading/history/count endpoint + DB helper (Task 1)"
```

---

## Task 2: Frontend — pagination state + controls

**Files:**
- Modify: `dashboard/frontend/components/TradingTab.jsx`

- [ ] **Step 1: Add pagination state at top of TradingTab component**

Around line 134 (with the other useState hooks):

```jsx
const CLOSED_PER_PAGE = 20  // module-level constant, NOT inside component

// Inside the component (replacing existing `const [history, ...]` line):
const [history, setHistory] = useState([])
const [closedPage, setClosedPage] = useState(0)
const [closedTotal, setClosedTotal] = useState(0)
```

- [ ] **Step 2: Update `fetchAll` to use `closedPage` + fetch count**

Replace the existing `fetchAll` `useCallback`:

```jsx
const fetchAll = useCallback(async () => {
  try {
    const offset = closedPage * CLOSED_PER_PAGE
    const [statsRes, sigRes, posRes, histRes, countRes] = await Promise.all([
      fetch('/api/trading/stats'),
      fetch('/api/trading/stats/by-signal'),
      fetch('/api/trading/positions'),
      fetch(`/api/trading/history?limit=${CLOSED_PER_PAGE}&offset=${offset}`),
      fetch('/api/trading/history/count'),
    ])
    if (statsRes.ok) setStats(await statsRes.json())
    if (sigRes.ok) {
      const sig = await sigRes.json()
      setBySignal(Array.isArray(sig) ? sig : Object.entries(sig).map(([k, v]) => ({ signal_type: k, ...v })))
    }
    if (posRes.ok) setPositions(await posRes.json())
    if (histRes.ok) setHistory(await histRes.json())
    if (countRes.ok) {
      const { total } = await countRes.json()
      setClosedTotal(total ?? 0)
    }
  } catch {
    // API not available yet
  }
}, [closedPage])
```

Note: `closedPage` is now in the `useCallback` dep array → page change triggers fetch.

- [ ] **Step 3: Update header + add pagination controls in Section 4**

Replace lines around 470-538:

```jsx
{/* Section 4: Recent Closed Trades */}
<div className="panel" style={{ marginBottom: 16 }}>
  <div className="panel-header" style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
    <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--color-text-primary)' }}>
      Closed Trades
    </span>
    <span style={{ fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 400 }}>
      {closedTotal === 0
        ? 'No closed trades yet'
        : `Showing ${closedPage * CLOSED_PER_PAGE + 1}–${Math.min((closedPage + 1) * CLOSED_PER_PAGE, closedTotal)} of ${closedTotal}`}
    </span>
  </div>
  {history.length === 0 ? (
    <div className="empty-state">No closed trades yet.</div>
  ) : (
    <>
      <div style={{ overflowX: 'auto' }}>
        {/* existing table — unchanged */}
      </div>
      {/* Pagination controls */}
      <div style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        padding: '12px 8px',
        borderTop: '1px solid var(--color-border)',
      }}>
        <button
          className="btn"
          disabled={closedPage === 0}
          onClick={() => setClosedPage(p => Math.max(0, p - 1))}
          aria-label="Previous page"
        >
          ← Prev
        </button>
        <span style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>
          Page {closedPage + 1} of {Math.max(1, Math.ceil(closedTotal / CLOSED_PER_PAGE))}
        </span>
        <button
          className="btn"
          disabled={(closedPage + 1) * CLOSED_PER_PAGE >= closedTotal}
          onClick={() => setClosedPage(p => p + 1)}
          aria-label="Next page"
        >
          Next →
        </button>
      </div>
    </>
  )}
</div>
```

- [ ] **Step 4: Verify component compiles via Vite**

```bash
cd dashboard/frontend
npm run build 2>&1 | tail -10
# Expected: dist/ regenerated; no errors
cd ../..
```

- [ ] **Step 5: Commit**

```bash
git add dashboard/frontend/components/TradingTab.jsx dashboard/frontend/dist/
git commit -m "feat(dashboard): closed-trades pagination — 20 per page + Prev/Next (Task 2)"
```

---

## Task 3: Full regression

```bash
uv run --native-tls pytest tests/test_dashboard*.py -q
# Expected: green
uv run --native-tls black dashboard/
git diff --stat dashboard/
git commit -am "chore(dashboard): black reformat" 2>&1 | tail -3
```

---

## Task 4: PR + 3-vector reviewers + merge + deploy

Per CLAUDE.md §8 (operator-visible UI change with money-flow indirect — closed trades surface):
- V1 — structural/code: API contract correctness, frontend pagination state composition
- V2 — UX/blast-radius: empty-state behavior, page-out-of-bounds handling, polling interaction with page changes
- V3 — silent-failure: count vs paginated-rows divergence under concurrent writes, accessibility of pagination controls

---

## Done criteria

- `/api/trading/history/count` endpoint returns `{total: int}` matching `paper_trades WHERE status != 'open'` count
- Frontend renders 20 closed trades per page with working Prev/Next
- "Showing X–Y of N" header reflects current page
- Empty state preserved (no controls when total=0)
- Polling refresh (30s) preserves current page
- Existing dashboard tests stay green
- dist/ rebuilt and committed

## What this milestone does NOT do

- Does NOT add per-page jump (skip-to-page-N) — only Prev/Next
- Does NOT add page-size selector (hardcoded 20)
- Does NOT add server-side sort (closedSort is still client-side, sorts only the visible 20)
- Does NOT add date-range or signal-type filters
- Does NOT cache previous pages (each page is a fresh fetch)

## Reversibility

Single PR; revert via `git revert <squash>` reverts both backend + frontend. Backend endpoint addition is additive (no breaking changes to existing `/api/trading/history`).
