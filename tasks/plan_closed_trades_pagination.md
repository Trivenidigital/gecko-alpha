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

- [ ] **Step 3: Failing tests in `tests/test_trading_dashboard.py`**

R1-C1 fold: use the EXISTING `_insert_trade` helper at `tests/test_trading_dashboard.py:39` (NOT a fictional `_seed_paper_trades`). Existing fixtures: `db` (line 13) and `client` (line 22). Append at end of file:

```python
async def test_history_count_endpoint(client):
    """Count endpoint returns total closed paper trades."""
    c, db = client
    await _insert_trade(db._conn, "bitcoin", "BTC", "volume_spike", "closed_tp", 200.0, 20.0)
    await _insert_trade(db._conn, "ethereum", "ETH", "volume_spike", "closed_sl", -50.0, -5.0)
    await _insert_trade(db._conn, "solana", "SOL", "first_signal", "closed_duration", 0.0, 0.0)
    await _insert_trade(db._conn, "doge", "DOGE", "volume_spike", "open")  # excluded
    await _insert_trade(db._conn, "shib", "SHIB", "volume_spike", "open")  # excluded
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
    from datetime import datetime, timedelta, timezone
    import json
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
                f"coin-{i}", f"C{i}", f"coin-{i}".title(), "coingecko",
                "volume_spike", json.dumps({}),
                100.0, 1000.0, 10.0, 20.0, 10.0, 120.0, 90.0,
                "closed_tp", float(i), float(i),
                opened, closed,
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
    assert all_closed == sorted(all_closed, reverse=True), (
        "rows not in closed_at DESC order"
    )
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

R2-I1 fold: persist closedPage to `sessionStorage` so tab-switch unmount (App.jsx:162 conditional render) doesn't reset operator's position mid-investigation.

```jsx
const CLOSED_PER_PAGE = 20  // module-level constant, NOT inside component

function _readStoredPage() {
  try {
    const v = sessionStorage.getItem('gecko.closedPage')
    const n = v == null ? 0 : parseInt(v, 10)
    return Number.isFinite(n) && n >= 0 ? n : 0
  } catch { return 0 }
}

// Inside the component (replacing existing `const [history, ...]` line):
const [history, setHistory] = useState([])
const [closedPage, setClosedPageState] = useState(_readStoredPage)
const [closedTotal, setClosedTotal] = useState(0)
const setClosedPage = useCallback((v) => {
  setClosedPageState(prev => {
    const next = typeof v === 'function' ? v(prev) : v
    try { sessionStorage.setItem('gecko.closedPage', String(next)) } catch {}
    return next
  })
}, [])
```

- [ ] **Step 2: Update `fetchAll` to use `closedPage` + fetch count + race guard**

R1-I1 fold: AbortController prevents stale-page fetches from overwriting current-page response when user clicks Next during a poll-fired in-flight request.

R2-I1 design-stage doc-fold: aborting on page-change ALSO cancels stats / by-signal / positions in-flight responses (they share the AbortController). Brief staleness (≤200ms typically) until the next fetchAll completes; resolves on next tick. Acceptable for a money-flow-adjacent UX where page-change frequency is low.

R1-I1 design-stage clarification: the `signal.aborted` belt-and-braces guard is load-bearing for the timeline race where 5 fetches all resolve cleanly BEFORE the next `fetchAll` invocation calls `ac.abort()`. In that case `Promise.all` doesn't reject, but our state writes would still be stale. The `signal.aborted` check after `Promise.all` catches this.

```jsx
// Module-level OR useRef inside component — track the latest in-flight
// AbortController so we can cancel before launching a new fetch.
const abortRef = useRef(null)

const fetchAll = useCallback(async () => {
  // R1-I1: cancel any prior in-flight fetch so its (stale) response
  // can't overwrite the current page after a page change. R2-I1
  // acknowledged: this also briefly stales stats/positions/by-signal
  // (~200ms until next tick).
  if (abortRef.current) abortRef.current.abort()
  const ac = new AbortController()
  abortRef.current = ac
  const signal = ac.signal
  try {
    const offset = closedPage * CLOSED_PER_PAGE
    const [statsRes, sigRes, posRes, histRes, countRes] = await Promise.all([
      fetch('/api/trading/stats', { signal }),
      fetch('/api/trading/stats/by-signal', { signal }),
      fetch('/api/trading/positions', { signal }),
      fetch(`/api/trading/history?limit=${CLOSED_PER_PAGE}&offset=${offset}`, { signal }),
      fetch('/api/trading/history/count', { signal }),
    ])
    // R1-I1 timeline-race guard: catches "5 resolved cleanly + next
    // fetchAll fired ac.abort() before we wrote state".
    if (signal.aborted) return
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
  } catch (e) {
    if (e?.name === 'AbortError') return  // expected — don't log
    // API not available yet
  }
}, [closedPage])
```

Note: `closedPage` is in the `useCallback` dep array → page change triggers fetch.

- [ ] **Step 2.5: Auto-clamp on count decrease (R1-I2 + R2-I2 fold)**

Add a `useEffect` that watches `closedTotal` + `closedPage`:

```jsx
useEffect(() => {
  // If closedTotal shrinks (DB cleanup, retroactive close-classification,
  // etc.), snap to the last valid page so operator doesn't land on an
  // empty Page-N-of-fewer view.
  if (closedTotal > 0 && closedPage * CLOSED_PER_PAGE >= closedTotal) {
    const lastPage = Math.max(0, Math.ceil(closedTotal / CLOSED_PER_PAGE) - 1)
    setClosedPage(lastPage)
  }
}, [closedTotal, closedPage, setClosedPage])
```

- [ ] **Step 2.6: Decouple polling timer from `closedPage` (R2-I2 design-stage fold)**

R2-I2 design-stage finding: existing pattern is `useEffect(() => { fetchAll(); const poll = setInterval(fetchAll, 30000); return () => clearInterval(poll) }, [fetchAll])`. Adding `closedPage` to fetchAll's dep array would CLEAR + RECREATE the timer on every page change — operator clicking through 10 pages over 90s never gets a polling tick (interval keeps resetting; stats / positions go > 30s stale).

Decouple: split into TWO useEffects. One fires `fetchAll` when `closedPage` changes (immediate refetch). The other manages the 30s polling timer using a ref-based latest-callback so the timer never restarts.

```jsx
// Latest fetchAll ref — timer reads from this so we don't reset the
// interval when closedPage changes.
const fetchAllRef = useRef(fetchAll)
useEffect(() => { fetchAllRef.current = fetchAll }, [fetchAll])

// Effect 1: immediate refetch on page change.
useEffect(() => {
  fetchAll()
}, [fetchAll])

// Effect 2: 30s polling — runs once at mount, never resets.
useEffect(() => {
  const poll = setInterval(() => fetchAllRef.current(), 30000)
  return () => clearInterval(poll)
}, [])
```

Trade-off: 2 useEffects + 1 ref vs. 1 useEffect that resets on page change. The decoupled version is the standard React pattern for "interval that should call latest callback without restarting" — see also `useEvent` proposal.

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
        : `Showing ${closedPage * CLOSED_PER_PAGE + 1}–${Math.min((closedPage + 1) * CLOSED_PER_PAGE, closedTotal)} of ${closedTotal}${closedTotal > CLOSED_PER_PAGE ? ' (sort applies to current page only)' : ''}`}
    </span>
  </div>
  {history.length === 0 ? (
    <div className="empty-state">No closed trades yet.</div>
  ) : (
    <>
      <div style={{ overflowX: 'auto' }}>
        {/* existing table — unchanged */}
      </div>
      {/* Pagination controls — R2-C1 fold: inline disabled style because
          plain .btn class has no :disabled rule (only .btn-generate does).
          Without inline opacity/cursor, disabled buttons look identical to
          enabled and clicking Prev on page 0 silently does nothing. */}
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
          style={{
            opacity: closedPage === 0 ? 0.4 : 1,
            cursor: closedPage === 0 ? 'not-allowed' : 'pointer',
          }}
        >
          ← Prev
        </button>
        <span
          style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}
          aria-live="polite"  // R2-M fold: announce page change to screen readers
        >
          Page {closedPage + 1} of {Math.max(1, Math.ceil(closedTotal / CLOSED_PER_PAGE))}
        </span>
        <button
          className="btn"
          disabled={(closedPage + 1) * CLOSED_PER_PAGE >= closedTotal}
          onClick={() => setClosedPage(p => p + 1)}
          aria-label="Next page"
          style={{
            opacity: (closedPage + 1) * CLOSED_PER_PAGE >= closedTotal ? 0.4 : 1,
            cursor: (closedPage + 1) * CLOSED_PER_PAGE >= closedTotal ? 'not-allowed' : 'pointer',
          }}
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
- Does NOT add server-side sort (closedSort is still client-side, sorts only the visible 20). R1-I2 design-stage: when the operator is on page 1+ OR closedTotal > 20, the section header tag note should change from `Last 20 completed trades` to `Showing X–Y of N (sort applies to current page only)` so operator understands the sort scoping. This is a single string-edit, not a code change. Append `(sort applies to current page only)` to the count text when closedTotal > CLOSED_PER_PAGE.
- Does NOT add date-range or signal-type filters
- Does NOT cache previous pages (each page is a fresh fetch)
- Does NOT freeze pagination across the 30s polling refresh: if a new trade closes mid-read on a non-first page, the offset-based query window shifts (newest closed_at pushes existing rows down by one). At observed close cadence (often hours between closes) this is rare; cursor-based pagination is M2 if needed (R2-I2 fold)
- Does NOT show a loading indicator during page-change fetch — local fetch latency is 50-200ms; render flicker would be worse than no indicator (R2-Q9 minor)

## Reversibility

Single PR; revert via `git revert <squash>` reverts both backend + frontend. Backend endpoint addition is additive (no breaking changes to existing `/api/trading/history`).
