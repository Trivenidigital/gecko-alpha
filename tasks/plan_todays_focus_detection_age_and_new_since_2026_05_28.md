**New primitives introduced:** `todayFocusAge.js` factual relative-age formatter; localStorage `last_seen_row_keys` set + `markRowsSeen()` storage helper; Today's Focus main-row detection-age column + "new since last view" header counter + uniform per-row new marker.

# Today's Focus PR-A: Detection Age + New Since Last View

**Goal:** Reduce external-tab dependency by surfacing two factual data points already in the payload — detection age (relative timestamp from `opened_age_hours`) and a delta marker showing which rows entered the queue since the operator last engaged. Closes a portion of the "decide in 5 minutes" friction without opening the ranking/advice door.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Relative timestamp formatting | Hermes generic time/date plugins exist; none owns gecko-alpha factual-only operator copy. | Build in-repo factual formatter. |
| LocalStorage delta tracking | No reusable primitive for "row-key set diff since last user interaction." | Build a small storage helper in `todayFocusStorage.js`. |
| Trader UI scanning aids | Hermes dashboard plugins target Hermes dashboards, not gecko-alpha React. | Build in existing React panel. |

Awesome-hermes ecosystem: no reusable component owns gecko-alpha's Today's Focus row contract or its localStorage shape. Custom repo-local work.

## Drift / Runtime Findings

Already shipped (post #281, #297, #307, #308):
- Both paper and tracker rows expose `opened_at` (ISO timestamp) and `opened_age_hours` (numeric, server-computed, rounded to 2 decimals). Source-of-truth `dashboard/db.py:1481-1482` (paper) and `dashboard/db.py:1647-1648` (tracker).
- LocalStorage state schema `gecko.todaysFocus.v0` includes `cached_payload`, `actions_by_row_key`, `usage_counters`, `usage_started_at`. Source `dashboard/frontend/todayFocusStorage.js`.
- Header already renders `{meta.rows_returned} rows from {meta.source_rows_considered} candidates` plus a "refreshed HH:MM" indicator.
- Contract firewall in `scripts/check_todays_focus_contract.py` bans 26 patterns from API JSON; JS mirror in `dashboard/frontend/todayFocusFacts.js` covers client-produced copy.

Residual gap PR-A closes:
- The main row exposes `move_basis` (a mechanical label like `tracker_detection`) but no compact relative-age summary. Trader must expand Details to learn whether they are 1h or 13h late on a candidate.
- The header has no signal about which rows are new since the operator last engaged with the panel. Repeated returns to the tab require re-scanning every row paranoidly.

## Scope

Build:

1. Add `dashboard/frontend/todayFocusAge.js` with one pure helper:
   - `formatDetectionAge(hours: number|null|undefined) -> string`
     Returns a factual relative-age string using the pinned format below.
     Returns `'-'` for null/undefined/non-finite input.
2. Extend `dashboard/frontend/todayFocusStorage.js`:
   - Add `last_seen_row_keys: string[]` to the localStorage schema, defaulting to `[]` for first-time users.
   - Export `markRowsSeen(state, rowKeys)` that replaces `last_seen_row_keys` with the supplied set. Pure / no other side effects.
   - Export `countNewRowKeys(state, currentRowKeys)` returning an integer count.
3. Update `TodayFocusPanel.jsx`:
   - Add a `Detected` column on the main row displaying `formatDetectionAge(row.opened_age_hours)`.
   - Header gets a third meta span: `Y new since last view` (omit if zero).
   - Each new row gets a tiny uniform `new` text marker (no color, no bold, no icon, no badge prominence beyond existing chips).
   - On first user action against any row (save / dismiss / expand / note edit), call `markRowsSeen(currentRowKeys)`. Subsequent unengaged returns continue to show "new" counts; engagement resets the baseline.
4. Update `dashboard/frontend/style.css`:
   - Add `.todays-focus-detected` (compact uniform style matching `.todays-focus-meta`).
   - Add `.todays-focus-new-marker` (small, uniform color, no severity, no animation).
   - 480px portrait: detected age stays inline on row; new-marker remains visible without overflow.
5. Refresh dist; ship factual-only tests (helper formatter coverage, storage helper coverage, JSX integration markers, dist freshness assertions).

## Detection-Age Display Format (pinned)

Single-source rules for `formatDetectionAge(hours)`:

| Input range (hours) | Output |
|---|---|
| `null` / `undefined` / non-finite | `'-'` |
| `< 0` (future-dated; should not occur) | `'-'` |
| `< 1` (< 60 min) | `'{minutes}m ago'` where minutes = `round(hours * 60)`; clamp 0→`'< 1m ago'` |
| `>= 1` and `< 24` | `'{hours.toFixed(1)}h ago'` (e.g., `1.4h ago`, `13.7h ago`) |
| `>= 24` and `< 168` (7d) | `'{days.toFixed(1)}d ago'` where days = `hours / 24` |
| `>= 168` | `'7d+ ago'` (caps coarsely; precise age past 7d is not operator-actionable) |

Examples:
- `0.0` → `'< 1m ago'`
- `0.5` → `'30m ago'`
- `1.0` → `'1.0h ago'` (boundary; do NOT render as `'60m ago'`)
- `1.4` → `'1.4h ago'`
- `13.74` → `'13.7h ago'`
- `25.0` → `'1.0d ago'`
- `38.0` → `'1.6d ago'`
- `200.0` → `'7d+ ago'`

**Rounded-input contract:** `opened_age_hours` is rounded to 2 decimals server-side in `dashboard/db.py:1382` and `:1559-1561`. Therefore `< 1` ↔ `<= 0.99` and `>= 1` ↔ `>= 1.00`. No floating-point boundary handling required at the helper level; the server contract guarantees the range partition is clean.

**Clamp safety:** `round(0.0 * 60) === 0`, so the `'< 1m ago'` branch triggers via `if (minutes <= 0) return '< 1m ago'`. Helper tests MUST cover input `0.0` to prevent accidental `'0m ago'` output.

## "New Since Last View" Semantics (pinned)

- **Definition of "last view":** the most recent moment the operator engaged with ANY row in the panel via save / dismiss / expand-toggle / note-edit. Auto-refresh and passive scrolling do NOT count as engagement.
- **Initial state:** `last_seen_row_keys = []` for first-time users. All currently-rendered rows render as `new` on first load (factual: they are all new to this operator).
- **Update trigger:** on any of the following user actions, call `markRowsSeen(currentRowKeys)` to update `last_seen_row_keys` to the snapshot of row_keys present at action time:
  - `handleAction(row.row_key, {save_for_review|dismissed|note: ...})` — save / dismiss / note edit
  - `toggleExpanded(row.row_key)` — Details / Hide
  - `refreshFocus(true)` — manual Refresh button (operator is explicitly re-scanning)
  - `restoreDismissed()` — operator is re-curating the queue
  Auto-refresh (the `useEffect`-driven background refresh) and passive scrolling do NOT count as engagement.
- **Header counter:** `Y new since last view` where Y = `countNewRowKeys(state, currentRowKeys)`. Omit span when Y is 0.
- **Per-row marker:** rows whose key is NOT in `last_seen_row_keys` get a small uniform `new` text marker. Marker disappears for that session as soon as engagement triggers `markRowsSeen`.
- **localStorage only:** never persisted to server; never sent to any endpoint.
- **Storage schema migration:** `last_seen_row_keys: string[]` defaults to `[]` via the existing `blankState` spread merge in `dashboard/frontend/todayFocusStorage.js`. NO `SCHEMA_VERSION` bump required; missing-field defaults flow through the existing loader pattern.
- **`markRowsSeen` save semantics:** matches sibling helpers (`updateRowAction`, `clearDismissed`) — returns new state AND persists via `saveTodayFocusState`. Called from React via `setState(prev => markRowsSeen(prev, keys))`.

## Anti-Scope (firewall-equivalent at plan level)

1. **No ranking semantics introduced by detection age.** The age column is rendered as a uniform factual cell. No color severity (no green for fresh / red for stale), no font-weight emphasis, no icon weight, no row reorder. Same `--color-text-secondary` as other meta cells.
2. **No ranking semantics introduced by `new` marker.** The marker is uniform-styled text. No bold, no color severity (no orange/red highlight), no animation, no icon. Same font-size as other chips.
3. **No sort changes.** Frontend MUST NOT re-sort rows by age, freshness, or new-status. Order is endpoint-provided.
4. **No backend schema changes.** PR-A consumes existing `opened_age_hours` and `opened_at`. If a future audit shows `opened_age_hours` is stale or wrong, that becomes a separate schema-contract design.
5. **No interpretive copy.** "Detected 1.4h ago" is factual. "Detected recently" or "Fresh entry" or "Late entry" are interpretive — banned at the contract firewall.
6. **No urgency tiers, TG alerts, sizing, execution, Kraken routing, server-side personal position state.**
7. **No advice on what to do with new rows.** No "Review these first" copy. The counter and marker are observational, not directive.
8. **Engagement-update is one-way (set, not append).** `markRowsSeen` REPLACES `last_seen_row_keys` with the current snapshot. Never accumulates across sessions; never grows unboundedly.
9. **No tooltip / title / aria-label interpretive copy.** The age cell renders only the literal `formatDetectionAge` output. No `title="X minutes more precise"` or `aria-label="Fresh entry"` hover/screen-reader copy beyond the visible factual string. Same rule applies to the `new` marker.
10. **Chip ordering pinned (no implicit ranking via DOM order).** Inside `.todays-focus-token`, child order is fixed: `[TokenLink] [name] [chain-badge] [link-chips] [block-cause chip if any] [new marker if any] [saved badge if any]`. Implementer MUST follow this order; reviewers MUST flag deviations.
11. **Header counter rendered with existing `todays-focus-meta` class.** `Y new since last view` uses the same className, font-weight, and color as the existing `{rows_returned} rows from {source_rows_considered} candidates` span. No bold, no color severity, no badge wrapper.

## Verification

- Failing Node tests for `formatDetectionAge` covering each range boundary + null/undefined/non-finite.
- Failing Node tests for `markRowsSeen` and `countNewRowKeys` covering: empty state, partial overlap, full rotation, persistence across cached_payload changes.
- Failing JSX layout test asserting `import.*formatDetectionAge.*from '../todayFocusAge.js'`, `todays-focus-detected` CSS class, `todays-focus-new-marker` class, `last_seen_row_keys` in storage source.
- **Banned-copy scan list extension (Reviewer B blocker fold):** add `dashboard/frontend/todayFocusAge.js` to the `paths` list in `test_todays_focus_frontend_copy_stays_factual` (`tests/test_dashboard_frontend_layout.py:186-189`). Without this, banned tokens leaked into the formatter source won't be caught.
- Negative-assertion test for formatter output: assert no `formatDetectionAge` output string for inputs covering `[0, 0.5, 1.0, 1.4, 13.74, 25.0, 38.0, 200.0]` matches BANNED_PATTERNS imported from `todayFocusFacts.js`.
- Dist freshness asserts new bundle contains literal `'m ago'`, `'h ago'`, `'d ago'`, and `todays-focus-new-marker`.

## Merge Gate

PR merges only when ALL three hold:
1. CI green (`Tests` workflow conclusion = SUCCESS).
2. All three orthogonal reviewers (product/anti-scope, frontend/a11y/mobile, smoke/test/dist-drift) return zero findings OR only non-blocking findings.
3. Every blocking finding folded and reviewer re-confirms or new-commit verification passes.

Smoke after deploy:
1. Render Today's Focus; verify each main row shows a `Detected: Xh ago` style cell.
2. Verify header shows `Y new since last view` (Y depends on first-load semantics; should be `5 new` on a fresh browser).
3. Click expand on any row → reload the panel → header should show `0 new since last view` (engagement reset the baseline).
4. Aggregate contract firewall: `scripts/check_dashboard_contracts.py --url http://127.0.0.1:8000 --window-hours 36 --verbose` returns 0 criticals.
5. 375px portrait: detected-age cell + new marker remain on row without horizontal overflow.
