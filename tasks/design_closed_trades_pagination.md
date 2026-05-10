**New primitives introduced:** Design companion to `tasks/plan_closed_trades_pagination.md`. No new primitives beyond those already declared in the plan. Documents API-shape rationale, polling/race semantics, sessionStorage UX, accessibility, and reversibility model.

# Closed Trades Pagination — Design Document

## 1. Goals

Operator can browse all closed paper trades in 20-row pages with Prev/Next controls. Page state survives tab switches. No regression on the existing trading dashboard surface.

**Primary outcomes:**
1. `/api/trading/history?limit=20&offset=N*20` already supports server-side pagination
2. New `/api/trading/history/count` returns `{total: int}` for page math
3. Frontend renders Prev/Next + "Page X of Y" + "Showing X–Y of N" header

**Non-goals:**
- Server-side sort across the full closed-trades set
- Page-size selector
- Jump-to-page input
- Date-range or signal-type filters
- Loading spinner during page-change fetch

## 2. Architectural choices

### 2.1 Sibling count endpoint vs response-shape change

**Chosen:** sibling endpoint `GET /api/trading/history/count → {total: int}`.

**Why:**
- Existing `/api/trading/history` returns `list[dict]` — changing to `{trades: [...], total: N}` is a breaking response shape. Frontend would need to handle two shapes during deploy if dashboard caches/reloads at different times.
- Sibling endpoint composes cleanly with `Promise.all` — adds one parallel request, ~50-200ms additional latency, no critical path.
- Single API consumer of `/api/trading/history`: `dashboard/frontend/components/TradingTab.jsx:145` (R1 confirmed via grep). No external consumers to coordinate.

**Tradeoff:** two SQL queries per page-load instead of one. Both are sub-millisecond against a paper_trades table that's <10K rows; insignificant.

### 2.2 AbortController for stale-fetch guard

**Chosen:** AbortController wrapping all 5 parallel fetches; cancel-and-replace on each `fetchAll` call.

**Why:** R1-I1 fold. Without a guard:
1. User on page 0; polling timer fires `fetchAll` at t=29.9s
2. Fetch is in flight (50-200ms typical)
3. User clicks Next at t=30.0s → `closedPage` changes → `useCallback` recreates → useEffect re-runs → second `fetchAll` fires for page 1
4. Page 1 response arrives first; sets `history` to page-1 rows. Page 0 response arrives ~30ms later; sets `history` back to page-0 rows.
5. UI shows page 0 content with Prev/Next reflecting page=1 → operator confusion.

The AbortController short-circuits #4: when the second fetch starts, `abortRef.current.abort()` cancels the first, the first's awaits throw `AbortError`, which is caught and silently ignored.

**Tradeoff:** ~10 LOC; adds `useRef` import. Acceptable for a money-flow-adjacent UX surface.

### 2.3 Auto-clamp on `closedTotal` decrease

**Chosen:** `useEffect` watching `closedTotal` + `closedPage`; if `closedPage * 20 >= closedTotal && closedTotal > 0`, snap to `Math.ceil(closedTotal / 20) - 1`.

**Why:** R1-I2 + R2-I2 fold. Operator on page 5 of 6 (closedPage=4, total=120); a DB cleanup or retroactive close-classification reduces total to 80 (page 5 = offset 80). Server returns 0 rows for offset=80 (>=80). UI shows empty page with Next disabled. Auto-clamp silently snaps to last valid page.

**Why not error/dialog:** the most common cause is benign (concurrent write); silent snap matches operator expectation.

**Edge cases:**
- `closedTotal=0` → effect's guard `closedTotal > 0` short-circuits; controls hidden by `history.length === 0` branch
- Page 0 (closedPage=0) is always valid when total>0
- Effect runs after `setClosedTotal` updates, ensuring `closedPage` reflects the snap on next render

### 2.4 sessionStorage persistence

**Chosen:** wrap `setClosedPage` to also write `sessionStorage.setItem('gecko.closedPage', String(next))`. Read at component mount via `_readStoredPage()`.

**Why:** R2-I1 fold. `App.jsx:162` is `{activeTab === 'trading' && <TradingTab />}` — switching to another tab unmounts TradingTab. Without persistence, operator on page 5 → switch to Signals tab → return → page resets to 0. Annoying mid-investigation.

**Why sessionStorage (not localStorage):** sessionStorage clears on tab close; operator returning a day later doesn't get stuck on a stale page index where the underlying total may have shifted significantly.

**Why not Redux/Context lift:** lifting `closedPage` to App.jsx would cross-cut three tabs and require Provider plumbing. sessionStorage is 4 LOC and behaves identically for this single-component use case.

**Tradeoff:** `_readStoredPage` adds a try/catch (sessionStorage may be unavailable in private mode). Acceptable.

### 2.5 Visible disabled state via inline style

**Chosen:** inline `style={{ opacity: ... ? 0.4 : 1, cursor: ... ? 'not-allowed' : 'pointer' }}` on Prev/Next.

**Why:** R2-C1 fold. `dashboard/frontend/style.css` defines `.btn-generate:disabled` (line 492) but NOT `.btn:disabled`. With just `disabled={...}`, the button receives the HTML attribute but no visual cue. Operator clicks Prev on page 0 — nothing happens, no feedback.

**Why not add `.btn:disabled` to style.css:** would change disabled appearance for ALL `.btn` instances across the dashboard (potentially regressing other buttons). Inline style is scoped and reversible.

**Why not use `.btn-generate`:** semantically wrong (these aren't generate-actions); also `.btn-generate` may have other styles we don't want.

**Tradeoff:** inline styles are project-discouraged generally, but the alternative (CSS rule with broader blast radius) is worse for this targeted fix.

## 3. Polling content shift (acknowledged)

R2-I2 documented. When operator is on page 2+ (offset > 0) and a new trade closes during the 30s polling interval:
- `closed_at DESC` ordering means the newly-closed trade is at offset 0
- The trade previously at offset 19 shifts to offset 20 (now top of page 2)
- Operator's currently-visible page 2 (offset 20-39) is now showing rows that were on page 1 a moment ago

**Why not freeze pagination (cursor-based):** changing to cursor-based would require:
- Storing the operator's "view-time anchor" timestamp
- Querying `WHERE closed_at <= anchor`
- New count query per anchor
- More state + plumbing

At observed close cadence (often hours between closes per memory `feedback_trading_lessons.md`) the practical hit-rate is low. M2 if it becomes a real complaint.

**Mitigation in scope:** "Showing X–Y of N" header reflects post-shift reality so operator can see the count changed.

## 4. Accessibility

- `aria-label="Previous page"` / `aria-label="Next page"` on the buttons (text symbols `← Prev` / `Next →` are decorative-supplemented by the labels)
- `aria-live="polite"` on the "Page X of Y" indicator span — screen readers announce page changes
- Disabled state: HTML `disabled` attr + visible opacity/cursor
- No keyboard trap; focus order is natural

## 5. Reversibility

**Single PR.** `git revert <squash>` reverts:
- `dashboard/db.py` count helper (additive, no schema change)
- `dashboard/api.py` count endpoint (additive)
- `dashboard/frontend/components/TradingTab.jsx` pagination state (replaces existing `useState` block)
- `dashboard/frontend/dist/` rebuild artifact

No DB migration. No Settings change. No external callers depend on the new endpoint.

**Quirk:** `sessionStorage.setItem('gecko.closedPage', ...)` writes survive a revert (browser-side state). After revert, the storage key is harmless dead data. Not an issue.

## 6. Test strategy

**Backend** (3 tests in `tests/test_trading_dashboard.py`, using existing `client` fixture + `_insert_trade` helper):
1. `test_history_count_endpoint` — seeds 3 closed + 2 open, asserts count=3
2. `test_history_count_endpoint_empty` — no rows, asserts count=0
3. `test_history_offset_pagination` — seeds 25 closed; page 0 returns 20, page 1 returns 5, no `id` overlap

**Frontend:** project has no Vitest/Jest setup (R1 confirmed). Manual UI check + `npm run build` syntax validation.

**Regression:** existing `tests/test_trading_dashboard.py` tests must stay green (verifies the additive count endpoint doesn't break the others).

## 7. Open questions — resolved

**Q1 (R1):** API shape — sibling vs response-shape change?
- **Resolved:** sibling endpoint (§2.1). Cleanest non-breaking option.

**Q2 (R1):** Race between page-change refetch and 30s polling?
- **Resolved:** AbortController guard (§2.2).

**Q3 (R1):** Page-out-of-bounds auto-clamp?
- **Resolved:** useEffect snap-to-last-valid-page (§2.3).

**Q4 (R2):** Tab-switch state reset?
- **Resolved:** sessionStorage persistence (§2.4).

**Q5 (R2):** Polling content shift?
- **Resolved:** documented as accepted (§3); cursor-based is M2 if needed.

**Q6 (R1):** dist/ commit convention?
- **Resolved:** project convention is to commit `dashboard/frontend/dist/` (R1 confirmed via `.gitignore` allowlist).

## 8. Reviewer-fold summary (plan-stage)

| Finding | Reviewer | Severity | Status |
|---|---|---|---|
| `_seed_paper_trades` fictional helper | R1 | C1 | Folded — use `_insert_trade` |
| `.btn:disabled` no styling | R2 | C1 | Folded — inline opacity/cursor |
| Stale-fetch race | R1 | I1 | Folded — AbortController |
| Page-out-of-bounds | R1+R2 | I2 | Folded — useEffect clamp |
| Tab-switch state reset | R2 | I1 | Folded — sessionStorage |
| Polling content shift | R2 | I2 | Documented in "NOT do" |
| aria-live for screen readers | R2 | M | Folded |
| Loading indicator | R2 | M | Documented in "NOT do" |
| Mobile rendering | R2 | M | Skipped — operator-laptop-only |
| Server-side sort scope creep | R2 | M | Skipped — out of scope |

## 9. Design-stage reviewer folds (round 2)

| Finding | Reviewer | Severity | Status |
|---|---|---|---|
| `_insert_trade` ties on `closed_at` (Windows clock granularity 15.6ms) → flaky pagination test | R1 | C1 | **Folded** — test uses direct INSERT with staggered `base - timedelta(seconds=i)` |
| `signal.aborted` belt-and-braces semantics conflated with await throw | R1 | I1 | **Folded** — comment clarifies it catches "all 5 resolved + next fetchAll already aborted" timeline race |
| `closedSort` headers misleading when `closedTotal > 20` | R1 | I2 | **Folded** — header text appends "(sort applies to current page only)" when `closedTotal > CLOSED_PER_PAGE` |
| Missing `closed_at DESC` ordering assertion | R1 | I3 | **Folded** — test asserts `all_closed == sorted(all_closed, reverse=True)` |
| AbortController over-cancels stats/by-signal/positions on page change | R2 | I1 | **Doc-folded** — comment in `_dispatch_live` style explaining brief staleness, ≤200ms until next tick |
| Polling timer reset on rapid pagination | R2 | I2 | **Folded** — split into 2 useEffects + `fetchAllRef` so timer never resets |
| StrictMode double-fire | R1 | M2 | Verified absent (main.jsx doesn't wrap App) |
| Vite asset hashing for cache busting | R1 | M | Verified working (hashed filenames in dist/) |
| First-time stale page surprise | R2 | M3 | Documented — header N change self-indicates the shift |
| N-growth vs auto-clamp interaction | R2 | M4 | Verified — clamp is decrease-only |
| Browser back/forward | R2 | M2 | Verified — sessionStorage survives same-tab navigation |

## 10. Approval checklist

- [x] Plan-stage 2-reviewer pass complete (folded at `96aa9f0`)
- [x] Design-stage 2-reviewer pass complete (folds in this commit)
- [ ] All folds applied + test coverage verified
- [ ] Build → PR → 3-vector reviewer pass → merge → deploy
