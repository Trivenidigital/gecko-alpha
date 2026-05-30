**New primitives introduced:** NONE

<!--
BL-NEW-DASHBOARD-WHAT-CHANGED-SINCE-LAST-VISIT — implementation plan.
Read-only, frontend-only, browser-localStorage "What Changed" dashboard
tab. No backend route, no schema change, no DB write. Mirrors the existing
Today's-Focus localStorage + contract-firewall conventions.
Worktree: C:/projects/gecko-alpha-wt/what-changed @ origin/master e8c21dc4.
-->

# Plan — Dashboard "What Changed" panel (session-aware delta since last visit)

## Hermes-first analysis

This is a project-specific React panel that diffs two existing gecko-alpha
dashboard REST endpoints client-side and persists snapshots in browser
localStorage. Hermes skills cover agent-side capabilities (notification,
data-fetch orchestration, narrative classification), not in-browser React
view state or client diffing of an app's own private REST surface.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| In-browser React view/component state | none found (skill hub `hermes-agent.nousresearch.com/docs/skills` is agent-side, not browser-UI) | build from scratch (project-local React, mirrors `TodayFocusPanel.jsx`) |
| Client-side diff of two JSON snapshots | none found | build from scratch (pure JS functions in `whatChangedStorage.js`; trivial set/map diff) |
| Browser localStorage persistence | none found | build from scratch (mirror existing `todayFocusStorage.js`) |
| Operator notification / alerting | exists (Hermes notification-style skills) but OUT OF SCOPE — panel is visibility-only, no alert/notify (would violate the factual-copy firewall) | do not use — intentionally excluded |

awesome-hermes-agent ecosystem check: scanned for browser-UI / dashboard-diff
/ localStorage-state libraries; none apply to an in-app React panel diffing
the app's own REST endpoints. Verdict: no Hermes capability applies; this is
correctly custom project-local frontend code, and the one adjacent Hermes
domain (notification) is deliberately excluded to preserve the read-only,
no-urgency contract.

---

## 0. Scope lock (operator-approved — 2 categories only)

On load, the panel shows a session-aware delta since the operator's last
visit, across EXACTLY 2 categories:

1. **Newly-closed trades since last visit** — diff `/api/trading/history`
   by trade id (closed-set now vs last snapshot). Show count + per-trade
   symbol / realized-PnL / closed-age; net realized since last visit.
2. **Unrealized-PnL changes on open positions** — diff `/api/trading/positions`
   unrealized-PnL per trade id since last snapshot; surface movers by
   absolute delta (both directions), largest first.

EXPLICITLY OUT OF SCOPE (do NOT implement):
- "new actionable trades" — already shipped via Today's-Focus `new since
  last view` (`countNewRowKeys` / `last_seen_row_keys`,
  `todayFocusStorage.js:46-56`). Do not re-implement.
- "new TG/X mentions on open positions" — deferred (brittle X-linkage).
- Category 3 (health regressions) — DESCOPED FROM MVP, see the dedicated
  subsection below.

---

## DESCOPED FROM MVP — Category 3 (health regressions)

A third category ("health regressions affecting trading") was in the
pre-review draft but is **descoped** because it is not buildable inside the
frontend-only, no-backend-route scope of this plan:

- `/api/system/health` (`get_system_health`, `dashboard/db.py:637`) returns
  per-table `{count, latest}` stats — there is **NO** `ok|degraded` status
  enum to diff. It cannot produce an OK→degraded transition signal without
  the panel inventing per-table freshness thresholds.
- The only place a real `status` string lives is the separate **`/health`**
  route (`api.py:1217`: `status` / `pipeline_running` / `db_reachable`).
  Consuming that is a *different endpoint* outside the 2-allowed-route scope
  of this panel (and the panel's anti-scope contract pins exactly the two
  trading routes).
- Building a proper health-regression delta therefore requires a NEW backend
  route exposing a per-subsystem status enum = out of this frontend-only
  plan's scope.

The real signals a future backend item would compose are documented for
reference (do NOT consume them in this plan):
- `/health` (`api.py:1217`) — static stub returning `status` /
  `pipeline_running` / `db_reachable`.
- `/api/source_calls/health` (`dashboard/db.py:4016`) — aggregate counters
  including `writer_freshness.minutes_since_last_observed` /
  `writer_freshness.lag_threshold_minutes` and an optional `schema_missing`.

## Follow-up to file

Propose NEW backlog item **`BL-NEW-API-SYSTEM-HEALTH-STATUS-ENUM`**
(status: PROPOSED) — add a backend `/api/system/health` (or sibling) route
exposing per-subsystem `status: ok|degraded|down` derived from heartbeat /
source-call / pipeline freshness signals (the freshness fields above are
the raw inputs). Once that backend status enum exists, a Cat-3
health-regression delta becomes a small frontend fast-follow on top of this
panel. **This plan does NOT build it** — it is recorded here as a deferred
follow-up only (no `backlog.md` edit in this PR).

---

## 1. Exact files to create / modify

CREATE:
- `dashboard/frontend/whatChangedStorage.js` — localStorage helper +
  PURE diff functions (the testable core). Mirrors `todayFocusStorage.js`.
- `dashboard/frontend/components/WhatChangedPanel.jsx` — the tab component.
  Mirrors `components/TodayFocusPanel.jsx` structure (load state on mount,
  fetch the 2 trading endpoints, render, mark-seen on engagement).
- `dashboard/frontend/whatChangedFacts.js` — (REQUIRED) factual copy/format
  helpers and the SINGLE copy chokepoint for this panel. Every rendered copy
  template string MUST originate here so the factual-copy firewall (§6) has
  one file to scan. It re-imports `BANNED_PATTERNS` from
  `todayFocusFacts.js` (the shared list — do NOT re-declare a subset).
  Parallel to `todayFocusFacts.js`.

MODIFY:
- `dashboard/frontend/App.jsx` — add tab button + conditional render
  (exact wiring in §1a).
- `dashboard/frontend/dist/index.html` AND `dashboard/frontend/dist/assets/
  index-*.js` — rebuilt bundle (Vite rewrites the hashed script src and the
  `index.html` reference on every build; both MUST be committed together —
  see memory `feedback_vite_dist_index_html_commit_discipline.md`).

DO NOT TOUCH: any `dashboard/*.py`, `dashboard/models.py`, `scout/**`,
`backlog.md`, or any DB/migration code. No new endpoint. No `response_model`.

### 1a. App.jsx tab wiring (exact insertion points)

`App.jsx` is the tab registry. Current pattern (verified):
- `const [activeTab, setActiveTab] = useState('signals')` — line 41.
- import block lines 1-18; add:
  `import WhatChangedPanel from './components/WhatChangedPanel.jsx'`
  alongside line 18 (`import TodayFocusPanel ...`).
- `.tab-bar` button block lines 128-195. Add a new `<button>` immediately
  AFTER the Today's-Focus button (lines 141-146), so it reads as a sibling
  of Today's Focus:
  ```jsx
  <button
    className={`tab-btn ${activeTab === 'what_changed' ? 'active' : ''}`}
    onClick={() => setActiveTab('what_changed')}
  >
    What Changed
  </button>
  ```
- Conditional render block lines 197-232. Add immediately AFTER line 201
  (`{activeTab === 'todays_focus' && <TodayFocusPanel />}`):
  ```jsx
  {activeTab === 'what_changed' && <WhatChangedPanel />}
  ```
- `activeTab` default stays `'signals'` — do NOT auto-open the new tab.

---

## 2. localStorage state shape — key `gecko.whatChanged.v0`

SEPARATE key from Today's-Focus. Do NOT bump or share `gecko.todaysFocus.v0`.
Mirror the `todayFocusStorage.js` shape conventions
(`schema_version` + `blankState()` + `load*/save*` + TTL-style guards):

```js
export const STORAGE_KEY = 'gecko.whatChanged.v0'
export const SCHEMA_VERSION = 1

function blankState(nowMs = Date.now()) {
  return {
    schema_version: SCHEMA_VERSION,
    last_visit_at: null,              // ISO; null = first visit (no baseline)
    snapshot: {                      // the last-seen baseline for diffing
      closed_trade_ids: [],          // category 1: array<string> of closed trade ids (the persisted closed-id set)
      open_unrealized_by_id: {},     // category 2: { [tradeId]: number }  (unrealized PnL usd)
      snapshot_at: null,             // ISO of when baseline was captured
    },
    usage_counters: { sessions: 0 }, // mirror Today's-Focus usage-evidence convention
  }
}
```

Loader behaviour mirrors `loadTodayFocusState` (`todayFocusStorage.js:66-96`):
- JSON.parse guarded in try/catch → `blankState()` on any failure.
- On `schema_version` mismatch, reset to `blankState()` (drop stale snapshot;
  safer than half-migrating diff baselines).

### 2b. When "last visit" / baseline updates (precise semantics)

Mirror Today's-Focus `markRowsSeen` semantics (`todayFocusStorage.js:37-44`,
called on engagement in `TodayFocusPanel.jsx:89-92`/`markCurrentRowsSeen`).
Two-phase, so the delta is visible the whole time the operator reads it:

1. On panel mount: load state, fetch the endpoints, compute the delta
   AGAINST the persisted `snapshot` (the prior baseline). RENDER the delta.
   Do NOT overwrite the baseline yet — otherwise the diff is always empty.
2. Commit a new baseline ("mark seen") ONLY on an explicit engagement event:
   - operator clicks the manual **Acknowledge / Mark seen** button, OR
   - operator clicks **Refresh** (force re-fetch),
   matching how Today's-Focus commits `last_seen_row_keys` on row-action /
   expand / refresh rather than silently on first paint.

   On commit: write `last_visit_at = now`, `snapshot = <freshly-fetched
   current values>`, `snapshot.snapshot_at = now`, persist via `saveState`.

Rationale: committing the baseline on first paint would make every revisit
show "nothing changed." Committing only on acknowledge keeps the delta
stable + intentional, and mirrors the existing, reviewed Today's-Focus rule.
Every-load reset is explicitly WRONG (it would erase the delta the operator
came to read) — keep the engagement-based commit.

**Baseline-age honesty label (mandatory).** Because the baseline commits on
acknowledge (not on every visit), "since last visit" honestly means "since
last acknowledge." Render a factual `baseline: {relative age} ago` label
next to the Acknowledge / Mark-seen control (relative age computed from
`snapshot.snapshot_at`) so the operator reads the delta against the correct
reference point. On first visit (no committed baseline) this label reads
`First visit — baseline recorded.`

---

## 3. Per-category client-side diff algorithm (pseudocode, real field names)

> NOTE ON FIELD NAMES — CONFIRMED (read byte-for-byte from `dashboard/db.py`
> on the worktree tree):
> - `/api/trading/positions` → `get_trading_positions` L912 /
>   `_get_trading_positions_inner` L921. Per-row keys CONFIRMED: trade id =
>   **`id`** (int PK), **`symbol`**, **`token_id`**, **`opened_at`** (ISO).
>   Unrealized PnL is COMPUTED in the enrichment loop (L972) =
>   **`unrealized_pnl_usd`** (float, rounded 2dp) — and is **`None`** when
>   `current_price` is missing/`entry_price` falsy (L989). Also present:
>   `total_pnl_usd` (L980, realized+unrealized) and `unrealized_pnl_pct`.
>   Use `unrealized_pnl_usd` for category 2.
> - `/api/trading/history` → `get_trading_history` L2634. Per-row keys
>   CONFIRMED: trade id = **`id`**, **`symbol`**, **`token_id`**, realized
>   PnL = **`pnl_usd`** (L2644), **`closed_at`** (L2648), **`status`**
>   (`closed_*`). `ORDER BY closed_at DESC` CONFIRMED (L2656) — newest first.
> - `/api/trading/history/count` → `get_trading_history_count` (api.py:474).
>   Returns the TOTAL closed-trade count `M`, used to render the
>   "Showing N of M" pagination footnote (§ Category 1 / R2).
>
> (Health endpoints `/api/system/health`, `/api/source_calls/health`,
> `/health` are NO LONGER consumed — Category 3 is DESCOPED, see the
> "DESCOPED FROM MVP" subsection above.)
>
> **§5 still mandates** that any unexpected mismatch degrades to
> "unavailable", never crashes.

### Category 1 — newly-closed trades (`/api/trading/history`)

Endpoint returns a list of CLOSED paper trades (status `closed_*`), paginated
(`limit`/`offset`). For the delta we fetch the most-recent page
(`?limit=50&offset=0&actionability=all`) — newly-closed trades appear at the
top (most-recent first; `ORDER BY closed_at DESC` confirmed at
`get_trading_history` L2656).

**MANDATORY pagination cap (contract, not a "decide").** The newly-closed
set is computed as a **pure set-membership diff**:
`newly-closed = (closed ids on the fetched page) − (prior-snapshot
closed-id set)`, bounded to a single `/api/trading/history?limit=50` page.
Use set-membership ONLY — do **NOT** add a `closed_at > last_visit`
timestamp gate (a timestamp gate would silently drop trades closed between
snapshots; set-membership keeps every id that wasn't in the prior baseline).
When the true total count (`/api/trading/history/count`, api.py:474) exceeds
the 50-row page, render a factual footnote: `Showing N of M, most recent 50`.
The prior-snapshot closed-id set is persisted in the snapshot
(`snapshot.closed_trade_ids`, §2).

Required fields per row (CONFIRMED; bind via tolerant getters, §5):
- trade id key — **`id`** (the paper_trades int PK).
- **`symbol`** (display).
- realized PnL — **`pnl_usd`** (db.py L2644).
- closed timestamp — **`closed_at`** (ISO, db.py L2648; for "closed age").

```
function diffClosedTrades(prevClosedIds: string[], currentRows: object[]):
  prevSet = new Set(prevClosedIds.map(String))
  newlyClosed = []
  for row in currentRows:
    id = tolerantId(row)                 // §5: missing id -> skip row, count "unavailable"
    if id == null: unavailableCount++; continue
    if not prevSet.has(id):
      newlyClosed.push({
        id,
        symbol: tolerantStr(row.symbol),         // '-' if missing
        realized_pnl: tolerantNum(row.pnl_usd),  // null -> 'unavailable'
        closed_at: tolerantIso(row.closed_at),   // null -> 'unavailable'
      })
  // net_realized_since SILENTLY DROPS rows whose pnl_usd is null/non-finite.
  // Count them so the header can disclose the partial-data caveat.
  summedRows = [n for n in newlyClosed if isFinite(n.realized_pnl)]
  net_realized_since = sum(n.realized_pnl for n in summedRows)
  netUnavailableCount = newlyClosed.length - summedRows.length   // closed rows excluded from the net
  return { count: newlyClosed.length, items: newlyClosed,
           net_realized_since, netUnavailableCount, unavailableCount }
```
**Net-realized disclosure (mandatory).** `net_realized_since` is the sum of
finite `pnl_usd` only — null-`pnl_usd` closed rows are silently dropped from
the sum. When `netUnavailableCount > 0`, the header MUST disclose it:
`net realized ±$X (excludes K unavailable)`. NEVER print a clean net over
partial data.
First-visit / empty baseline (`prevClosedIds` empty AND `last_visit_at`
null): DO NOT flood. Render "First visit — baseline recorded (N closed
trades known)." and treat nothing as "new." Baseline is committed per §2b
when the operator acknowledges.

### Category 2 — biggest PnL swings on open positions (`/api/trading/positions`)

Endpoint returns the list of OPEN paper trades. Per-row fields (CONFIRMED):
- trade id — **`id`** (int PK).
- **`symbol`**.
- unrealized PnL — **`unrealized_pnl_usd`** (computed in the enrichment loop
  at db.py L972; **`None`** when `current_price` missing or `entry_price`
  falsy, L989). `tolerantNum` returns null for those rows → counted into
  `unavailableCount`, excluded from ranking (§5). Note `total_pnl_usd`
  (realized+unrealized) also exists — but category 2 is specifically
  unrealized-PnL swings on OPEN positions, so bind `unrealized_pnl_usd`.

```
function diffPnlSwings(prevMap: {id: number}, currentRows: object[], topN = 5):
  movers = []
  for row in currentRows:
    id = tolerantId(row); if id == null: unavailableCount++; continue
    cur = tolerantNum(unrealizedPnl(row))    // null -> skip from ranking, count unavailable
    if cur == null: unavailableCount++; continue
    if id in prevMap and isFinite(prevMap[id]):
      delta = cur - prevMap[id]
      movers.push({ id, symbol, prev: prevMap[id], current: cur, delta })
    // ids not in prevMap = newly-opened since last visit; record with delta=null
    else:
      movers.push({ id, symbol, prev: null, current: cur, delta: null, newly_opened: true })
  ranked = movers.filter(m => m.delta != null)
                 .sort(by abs(delta) desc)   // both directions; NOT a buy/sell ranking — see §4
                 .slice(0, topN)
  return { movers: ranked, newly_opened_count, unavailableCount }
```
First visit: `prevMap` empty → all positions are `newly_opened` with
`delta=null`; render "First visit — baseline recorded (M open positions)."
Do not rank.

Sort note (firewall): sorting by absolute PnL delta is a factual magnitude
ordering of an observed number, NOT an action ranking/urgency. The SORT lives
in render logic only; the diff payload MUST NOT carry a `sort_key`-style
field (that key pattern is banned by the contract checker). Copy MUST NOT
label it "top picks"/"priority"/"act now" (see §4). The column header is
`Open-position unrealized-PnL changes since last visit — largest absolute
first`.

(Category 3 — health regressions — is DESCOPED; see the "DESCOPED FROM MVP"
subsection above. No health-regression diff helper, no health-status
snapshot key, and no health-endpoint consumption exist in this plan.)

---

## 4. Factual copy rules (firewall — counts + signed $ + relative age ONLY)

The panel copy MUST stay observational, mirroring the Today's-Focus factual
firewall (`todayFocusFacts.js` + `scripts/check_todays_focus_contract.py`
`BANNED_PATTERNS` L142-177). Allowed: integer counts, signed USD
(`+$1.2K` / `-$317.00`, reuse a `fmtUsd` like `TodayFocusPanel.jsx:20-30`),
relative age (`3h ago`, reuse `todayFocusAge.js` style), and the literal
`unavailable` sentinel for missing data. (No health status enums / transition
arrows — Category 3 is descoped.)

EXPLICITLY BANNED phrasings (these trip / would trip the firewall — same
list as the Python scanner): `buy`, `sell`, `consider`, `trade now`,
`watch breakout`, `entry is late`, `pullback`, `target`, `should`,
`recommend(ed/ation)`, `go long`, `enter here`, `take profit`, `strong buy`,
`must buy`, `act now`, `action required`, `acting`, `now tradeable`,
`tradeable now`, `urgency`, `priority`, `alert`, `notify`, `operator
priority`, `research only`. Also forbidden field/label semantics: `top pick`,
`recommend`, `urgency`, `priority`, `alert`, `notify`, ranking words.

Concrete copy templates (PINNED — these exact strings; firewall-safe). All
live in `whatChangedFacts.js`, the single copy chokepoint (§1):
- Panel / tab title: `What Changed`
- Cat-1 headline: `Closed since last visit: {N} · net realized {±$X}`
  (append ` (excludes {K} unavailable)` when `netUnavailableCount` K>0)
- Cat-1 row: `{SYMBOL}  {±$X}  closed {age} ago`
- Cat-2 headline: `Open-position unrealized-PnL changes since last visit — largest absolute first`
- Cat-2 row: `{SYMBOL}  {±$cur} (was {±$prev}, change {±$Δ})`
- Cat-2 newly-opened row: `{SYMBOL}  {±$cur} (opened since last visit — no prior baseline)`
- Cat-2 truncation footnote: `Showing {k} of {N}, sorted by absolute change`
- Cat-1 pagination footnote: `Showing N of M, most recent 50`
- Empty: `No changes since last visit.`
- First visit: `First visit — baseline recorded.`
- Unavailable (category banner): `Unavailable`
- Unavailable (rows footnote): `{K} rows unavailable`
- Baseline-age label (next to acknowledge control): `baseline: {relative age} ago`

The words "biggest" and "top movers" MUST NOT reach any rendered copy
string. The sort is by absolute Δ (a factual computed magnitude, not an
action ranking).

Do NOT use the literal banned substrings even in code comments/labels
(memory `feedback_static_grep_self_referential_pattern.md`): if a contract
checker is added (§6), paraphrase any prose that would otherwise quote a
banned token.

---

## 5. Diff keys + missing/renamed-field degradation

- Diff keys: category 1 & 2 key on the trade id (`id`, stringified).
- Tolerant getters (pure helpers in `whatChangedStorage.js`): `tolerantId`,
  `tolerantNum`, `tolerantStr`, `tolerantIso`, `unrealizedPnl(row)` (tries
  the candidate key list, returns the first finite number, else null).
  Every getter returns a sentinel (`null` / `'-'` / `'unavailable'`) on
  missing/renamed/NaN — NEVER throws.
- A row with no resolvable id is SKIPPED from the diff and counted into a
  per-category `unavailableCount`, surfaced as a factual footnote:
  `K rows unavailable (missing id)`.
- A whole-endpoint fetch failure (non-200 / network) renders a per-category
  `unavailable` banner (mirror `TodayFocusPanel.jsx` `error` handling,
  lines 59 / 74-77 / 167-170) and leaves that category's baseline UNCHANGED
  (so a transient outage doesn't corrupt the diff baseline). The other
  categories still render.

---

## 6. TDD test plan

### 6a. Repo's JS-test pattern (documented from tree)

The frontend has NO node/jest/vitest test runner: `package.json` declares
only `vite` build/dev scripts (no `test` script; no jest/vitest/mocha dep).
JS logic is exercised indirectly and the dashboard is asserted via **Python
static-assert tests over the source + built bundle** plus **Python httpx
contract/runtime tests** against the FastAPI app. Evidence:
- `tests/test_dashboard_frontend_layout.py` — Python static-asserts over the
  frontend source/bundle (mirror this for tab+panel wiring + dist-fresh).
- `tests/test_trading_dashboard.py` — httpx tests hitting `/api/trading/
  positions` + `/api/trading/history` + `/api/trading/history/count`
  (positions L96/262/308/..., history L108/269/793). These are the ONLY two
  trading routes this panel consumes.
- The factual-copy firewall is a standalone Python validator
  (`scripts/check_todays_focus_contract.py`) with `BANNED_PATTERNS` mirrored
  in JS (`todayFocusFacts.js` `BANNED_PATTERN_SHARDS`) and a list-equality
  drift test.

Decision: follow the established pattern — do NOT introduce a JS test
runner (would be net-new tooling/primitive). Test the pure diff helpers via
the same approach the repo already uses for JS-equivalent logic:

OPTION A (preferred, lowest-friction, no new tooling): keep the diff helpers
PURE and side-effect-free, then write a small Python harness test
`tests/test_what_changed_diff.py` that loads `whatChangedStorage.js`, strips
the `export`/`import` lines, and evaluates the pure functions via a JS engine
only IF one is already available — there is NOT one in-tree, so instead:

OPTION B (matches repo reality — RECOMMENDED): port the diff-helper LOGIC
spec into the Python test as a behavioural contract + assert the JS source
contains the required pure-function signatures via static regex
(parallel to how `todayFocusFacts.js`↔Python `BANNED_PATTERNS` drift is
list-equality tested). Concretely two Python test files:

1. `tests/test_what_changed_frontend_layout.py` (mirror
   `test_dashboard_frontend_layout.py`): assert
   - `App.jsx` imports `WhatChangedPanel` and contains the
     `activeTab === 'what_changed'` button + conditional render;
   - `whatChangedStorage.js` exists and exports `STORAGE_KEY ===
     'gecko.whatChanged.v0'`, `diffClosedTrades`, `diffPnlSwings`,
     `blankState`, `loadState`, `saveState` (NO health-regression diff
     helper — Category 3 descoped);
   - it does NOT reference `'gecko.todaysFocus.v0'` (no key collision);
   - dist freshness: `dist/index.html` references a hashed
     `assets/index-*.js` that exists on disk, and the bundle string-contains
     a `What Changed` marker (proves rebuild happened — same dist-fresh
     assertion convention).
2. `tests/test_what_changed_contract.py` (copy-firewall — mirror the
   factual-copy firewall): **IMPORT** the exported JS `BANNED_PATTERNS` from
   `dashboard/frontend/todayFocusFacts.js` (the full shared list — do NOT
   re-declare an inline subset; the existing
   `test_todays_focus_frontend_copy_stays_factual` at
   `tests/test_dashboard_frontend_layout.py:316` uses a weaker hardcoded
   list, and the new panel must bind to the full shared list). Assert every
   copy template string in `whatChangedFacts.js` / `WhatChangedPanel.jsx` is
   clean against the imported list. Per memory
   `feedback_static_grep_self_referential_pattern.md`, the test IMPORTS the
   banned literals — it must NEVER quote them inline (a quoted banned token
   would trip its own scanner).
3. `tests/test_what_changed_anti_scope.py` (NEW — enforceable anti-scope
   contract, replacing the prose-only claim). Assert:
   - (a) the new JS files contain no `fetch(`/XHR to any URL other than the
     two allowed existing GET routes `/api/trading/history` (incl.
     `/api/trading/history/count`) and `/api/trading/positions` — no
     POST/PUT/PATCH/DELETE verb, no new path;
   - (b) no new backend route, no `response_model`, no DB write is
     introduced (the PR touches ONLY the allowed frontend file set + tests +
     dist).
4. **paths allowlist (MANDATORY).** Add the new source files —
   `components/WhatChangedPanel.jsx`, `whatChangedStorage.js`, and the
   now-REQUIRED `whatChangedFacts.js` — to the `paths` allowlist at
   `tests/test_dashboard_frontend_layout.py:317`, or they are silently
   UNSCANNED by the copy-firewall sweep.
5. (Pure-logic coverage) Because there is no JS runner, encode the diff
   algorithm's first-visit / missing-field / both-direction / null-skip /
   pagination-cap cases as a docstring'd behavioural spec table in
   `test_what_changed_frontend_layout.py` AND assert the JS contains the
   sentinel-returning guards (`return null`, `'unavailable'`, try/catch in
   the loader) so the "never crash" contract is statically enforced.

   If the design phase decides pure-JS unit coverage is worth a runner,
   that is a SEPARATE primitive decision (adds vitest) and must go through
   drift+Hermes-first again — out of scope for this plan, which matches
   current repo tooling.

### 6b. Order (TDD)

Write `test_what_changed_frontend_layout.py` + `test_what_changed_contract.py`
+ `test_what_changed_anti_scope.py` FIRST (they fail: no panel/storage yet),
then implement `whatChangedStorage.js` → `whatChangedFacts.js` →
`WhatChangedPanel.jsx` → `App.jsx` wiring → add the three new source files to
the `paths` allowlist (`test_dashboard_frontend_layout.py:317`) →
`npm run build` → commit dist → tests pass.

Test-set summary (post-fold): closed-diff (set-only, pagination-cap) ·
pnl-swing-diff (null-skip) · first-visit-baseline · missing/null-field
tolerance (pure-helper static-asserts) · static frontend layout/wiring test
(tab+panel in built dist, paths-list updated) · the new anti-scope test ·
the copy-firewall test importing `BANNED_PATTERNS`. The health-regression
test is DROPPED (Category 3 descoped).

---

## 7. Anti-scope statement (contract)

- READ-ONLY. No new backend route, no FastAPI handler, no `response_model`.
- No DB schema change, no migration, no DB write. Consumes only the TWO
  existing trading GET endpoints client-side (`/api/trading/history` incl.
  `/api/trading/history/count`, and `/api/trading/positions`).
- Browser localStorage only (`gecko.whatChanged.v0`), separate key.
- Factual / observational copy only: counts + signed USD + relative age.
  NO ranking-as-advice, NO urgency, NO alert/notify, NO buy/sell/recommend
  language. The §4 banned list is the contract.
- EXACTLY 2 categories. No "new actionable trades" (Today's-Focus owns it),
  no TG/X mentions (deferred), no health regressions (Category 3 descoped).
- No new tooling/primitive (no JS test runner) — matches repo conventions.

**Enforcement is by tests, NOT prose.** The existing runtime copy-firewall
(`scripts/check_todays_focus_contract.py`) scans a live `/api/todays_focus`
JSON payload and therefore CANNOT cover this endpoint-less panel. The
anti-scope contract is instead enforced concretely by:
1. `tests/test_what_changed_anti_scope.py` — asserts (a) no `fetch(`/XHR to
   any URL beyond the two allowed GET routes (no POST/PUT/PATCH/DELETE, no
   new path); (b) no new backend route / `response_model` / DB write (PR
   touches only the allowed frontend file set + tests + dist).
2. `tests/test_what_changed_contract.py` — copy-firewall test that IMPORTS
   the shared `BANNED_PATTERNS` from `todayFocusFacts.js` and asserts every
   copy template in the new panel/helper is clean (never quotes banned
   literals inline — it imports them).
3. The three new source files are added to the `paths` allowlist at
   `tests/test_dashboard_frontend_layout.py:317` so they are actually
   scanned (otherwise silently skipped).
4. The diff payload carries NO `sort_key`-style field (banned key pattern);
   sort lives in render logic only.

---

## 8. Risks / edge cases for design + reviewer judgment

R1 (field names — RESOLVED). Trade-id (`id`), `symbol`, realized PnL
(`pnl_usd`), `closed_at`, and unrealized PnL (`unrealized_pnl_usd`,
computed) are now byte-confirmed from `dashboard/db.py` (positions L912/921,
history L2634). No residual field-name risk remains (the health derivation
risk is gone with Category 3 descoped). §5 still makes every binding degrade
to "unavailable" rather than crash; the contract test (§6a #1) MUST assert
each bound key appears in a recorded sample payload so a future rename
surfaces loudly rather than silently rendering "unavailable" forever
(Class-3 risk).

R2 (history pagination vs full closed-set) — RESOLVED to a MANDATORY
contract (no longer a "decide"). Category 1 diffs only the most-recent
`/api/trading/history?limit=50` page via pure set-membership
(`page closed ids − prior-snapshot closed-id set`); when the true total
(`/api/trading/history/count`, api.py:474) exceeds the page, render the
factual footnote `Showing N of M, most recent 50`. Set-only diff (NO
`closed_at > last_visit` timestamp gate, so trades closed between snapshots
are never lost). See § Category 1.

R3 (baseline-commit timing). §2b commits the baseline only on explicit
acknowledge/refresh. If the operator never clicks acknowledge, the delta
keeps growing across visits (intended), but "since last visit" then means
"since last acknowledge." Reviewer: confirm this matches operator mental
model, or add an auto-commit-on-unmount variant (NOT recommended — would
make revisits show empty, the exact bug §2b avoids).

R4 (newly-opened positions in category 2). A position present now but absent
from the prev snapshot has no prior unrealized value → `delta=null`. Plan
surfaces it as `newly_opened` (factual), excluded from the abs-delta ranking.
Reviewer: confirm newly-opened should be shown as a neutral line, not ranked.

R5/R8 (health degradation) — RESOLVED by DESCOPING Category 3. Neither
`/api/system/health` (per-table stats, db.py:637) nor
`/api/source_calls/health` (coverage/rankability rollup, db.py:4016)
returns a ready `ok/degraded` enum, and the only `status` string lives on
the separate `/health` route (api.py:1217) which is outside this panel's
two-route scope. Building an honest health-regression delta therefore needs
a new backend status-enum route = out of frontend-only scope. Category 3 is
descoped (see "DESCOPED FROM MVP" subsection) and the backend prerequisite
is filed as `BL-NEW-API-SYSTEM-HEALTH-STATUS-ENUM` ("Follow-up to file").
No health endpoints are consumed by this plan.

---

## Fold round 1 (post-review)

7 reviewers (4 subagents + 3 Codex) converged on the same folds; all applied:

1. **Descope Category 3 (health regressions)** — removed from §0, §3, §4, §6,
   §7 and the state shape (`health_status_by_key` deleted) plus the
   `diffHealthRegressions` helper/test. `/api/system/health` (db.py:637)
   returns per-table stats with no `ok|degraded` enum; the only `status`
   string is on the out-of-scope `/health` route (api.py:1217). Added a
   "DESCOPED FROM MVP" subsection + a "Follow-up to file" proposing
   `BL-NEW-API-SYSTEM-HEALTH-STATUS-ENUM`. Plan is now EXACTLY 2 categories.
2. **Enforceable anti-scope contract (not prose)** — §6/§7 now mandate
   `tests/test_what_changed_anti_scope.py` (no `fetch` beyond the two
   allowed GET routes, no new route/`response_model`/DB write) +
   `tests/test_what_changed_contract.py` that IMPORTS the shared
   `BANNED_PATTERNS` from `todayFocusFacts.js` (not an inline subset) +
   MANDATORY addition of the three new source files to the `paths` allowlist
   at `tests/test_dashboard_frontend_layout.py:317`. `whatChangedFacts.js`
   promoted from optional to REQUIRED (single copy chokepoint). Tests import
   banned literals, never quote them (`feedback_static_grep_self_referential_pattern.md`).
3. **Pagination cap mandatory** — Category 1 / R2 now a contract: set-only
   diff `page closed ids − prior-snapshot closed-id set`, bounded to
   `?limit=50`, with `Showing N of M, most recent 50` footnote via
   `/api/trading/history/count` (api.py:474). No `closed_at > last_visit`
   timestamp gate.
4. **Net-realized disclosure** — `net_realized_since` sums finite `pnl_usd`
   only; when `netUnavailableCount>0` the header MUST read
   `net realized ±$X (excludes K unavailable)`.
5. **Pinned neutral copy strings** — §4 now pins the exact factual templates;
   "biggest"/"top movers" banned from rendered copy; sort lives in render
   logic, payload carries no `sort_key`-style field.
6. **Baseline-age label + engagement-based reset** — kept engagement-based
   commit (every-load reset is wrong); added `baseline: {relative age} ago`
   label next to the acknowledge control (§2b).

First line remains `**New primitives introduced:** NONE` and the
Hermes-first table is intact.

R6 (localStorage quota / multi-tab). Snapshot is small (a closed-id array +
an open-position unrealized-PnL number map); quota is a non-issue. Multi-tab
race: last-write-wins on acknowledge, acceptable for a visibility panel
(same as Today's-Focus).

R7 (dist commit discipline). Forgetting to rebuild/commit
`dist/index.html` + hashed `dist/assets/index-*.js` together ships stale UI
(memory `feedback_vite_dist_index_html_commit_discipline.md`). The dist-fresh
assertion in §6a #1 guards this in CI.
