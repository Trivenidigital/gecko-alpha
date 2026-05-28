**New primitives introduced:** `todayFocusFacts.js` factual translation helper; Today’s Focus row expand/collapse inspection packet.

# Today's Focus Inspection Packet Plan

**Goal:** Reduce trader inspection time inside Today’s Focus by adding a factual per-row details packet and readable block reason copy, without adding ranking, urgency, advice, alerts, sizing, or execution.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Dashboard UI composition | Hermes dashboard plugins exist for extending the Hermes dashboard, but they do not apply to gecko-alpha's in-repo React dashboard or `/api/todays_focus` contract. | Build in existing React dashboard. |
| Trading decision support / factual row packets | No Hermes skill found that owns gecko-alpha's Trade Inbox / Today's Focus schema, anti-scope firewall, or localStorage usage evidence. | Build repo-local factual renderer. |
| Copy contract / banned action language | Hermes skill docs describe skill safety scanning generally, but no installed/public skill enforces this endpoint's trader-surface invariants. | Extend existing contract/static tests. |

Awesome-Hermes ecosystem check: general dashboards and agent-operation UIs exist, but no reusable primitive covers gecko-alpha Today's Focus rows, factual-only trader copy, or the existing contract firewall. Custom repo-local work is warranted.

## Drift-check

Already shipped:
- `/api/todays_focus` returns a fixed scarce review queue with read-only / not-for-alerting / not-for-execution metadata.
- `TodayFocusPanel.jsx` renders the five rows, localStorage save/dismiss/note state, explicit chart/research chips, and `block_cause`.
- `scripts/check_todays_focus_contract.py` enforces closed schema plus banned urgency/advice language.
- PR #304 added mechanical `block_cause` classification and explicit research links.

Residual gap:
- The row still forces the trader to mentally decode machine values such as `NO_PRICE`, `STALE_PRICE`, `tracker_only_no_paper_trade`, and `v1_block_*`.
- Dense diagnostics are visible on the main row, which hurts mobile scanability.
- The trader lacks a single expanded row packet with current price, mcap, 24h move, source lane, price timestamp, block cause, and factual reason details.

## Scope

Build:
1. Add `dashboard/frontend/todayFocusFacts.js` with pure helpers (explicit signatures):
   - `reasonLabel(reason: string|null|undefined) -> string`
     Deny-by-default finite factual mapping for known machine reason strings.
     Unknown/empty/null reason returns the literal string `"Unmapped reason"`.
     Never returns raw machine text (no `v1_*`, `*_missing_or_invalid`, action-language fallback).
   - `blockCauseLabel(cause: string|null|undefined) -> string`
     Mechanical mapping: `data_quality` -> `"Data quality"`, `data_path` -> `"Data path"`,
     `unknown` / null / unmapped -> `"Unknown"`.
   - `primaryBlockFacts(row: object) -> Array<string>`
     Returns 0-2 short factual lines for the compact main row.
     Each line is already factual-labeled (no raw machine text).
     Empty array if no block reason present (row is fully actionable).
   - `buildFocusDetailRows(row: object) -> Array<{label: string, value: string}>`
     Returns an ordered list of factual key-value pairs for the expanded packet.
     `value` is `"-"` when the source field is null/undefined/empty.
     Order is stable across calls for the same input shape.
     Empty array is permitted but expand-view UI must handle it explicitly (empty-state copy).

Empty-state behavior (row with no block reason):
- `primaryBlockFacts(row)` returns `[]`.
- `buildFocusDetailRows(row)` still returns the identity/price/timing fact rows; only block-reason rows are omitted.
- Expand-view UI shows the detail grid plus an explicit factual note: `"No block reason recorded"` (this string must also pass the contract firewall).
2. Update `TodayFocusPanel.jsx`:
   - Add row expand/collapse state using React local state only.
   - Keep main row compact on mobile.
   - Move wide diagnostics into the expanded details packet.
   - Render factual block/reason chips using the helper.
3. Update CSS for a compact details grid that works at 375px portrait.
4. Extend frontend static/Node tests for helper behavior and banned-language coverage.
5. Refresh dashboard frontend dist.

Non-scope:
- No ranking within the five rows.
- No urgency tiers, alert qualification, or Telegram sends.
- No buy/sell/consider/watch-breakout/entry-late language.
- No sizing, execution, Kraken routing, or order-state changes.
- No server-side personal position state.
- No `/api/todays_focus` schema changes in this PR. If a field is missing, stop and open a separate schema-contract design that explicitly bans ranking, urgency, alerting, sizing, execution, position, score, and recommendation fields.

## Factual Translation Rules

Allowed examples:
- `NO_PRICE` -> `Price snapshot missing`
- `STALE_PRICE` -> `Price cache stale`
- `NOT_ACTIONABLE` -> `Actionability gate blocked`
- `BAD_TIMESTAMP` -> `Timestamp unavailable`
- `DATA_INSUFFICIENT` -> `Data insufficient`
- `tracker_only_no_paper_trade` -> `Tracker-only row; no open paper trade`
- `detected_price_missing_or_invalid` -> `Detected price missing`
- `price_timestamp_unparseable` -> `Price timestamp unparseable`
- `entry_price_missing_or_invalid` -> `Entry price missing`
- `no_price_snapshot_for_token_id` -> `Price snapshot missing`
- unmapped reason string -> `Unmapped reason` (do not render raw machine text)

Forbidden examples (imperative + interpretive siblings — extended after design review):
- `Entry is late unless it consolidates`
- `Watch for breakout above X`
- `Consider entry`
- `Buy`, `sell`, `act now`, `trade now`, `action required`
- `recommend`, `suggest`, `looks like`, `appears`
- `good entry`, `bad entry`, `opportunity`
- `ripe`, `ready`, `primed`
- `soon`, `imminent`, `expected to`, `likely to`
- `optimal`, `favorable`
- `take profit`, `target`, `should`
- `urgency`, `priority`, `alert`, `notify`
- `watch breakout`, `entry is late`, `pullback`

### Scope clarification (2026-05-28 follow-up to PR #307 review)

The "Forbidden examples" list above is BROADER than the regex-enforced
`BANNED_PATTERNS` list in `scripts/check_todays_focus_contract.py`. Two
distinct surfaces, intentionally:

1. **Regex-enforced `BANNED_PATTERNS`** (canonical, runtime-asserted):
   - Source of truth: `scripts/check_todays_focus_contract.py` lines 124-153
     (26 entries today).
   - Mirrored verbatim in JS at `dashboard/frontend/todayFocusFacts.js` via
     the shard array. `test_banned_patterns_python_and_js_lists_stay_in_sync`
     enforces exact equality at runtime.
   - These are the entries the contract firewall trips on (both server-side
     JSON scan and client-side helper-output scan).
   - Words on this list have low false-positive risk on factual phrases —
     e.g., `\bbuy\b`, `\btrade[\s_-]*now\b`, `\bact[\s_-]*now\b`.

2. **Plan-doc "Forbidden examples"** (human-review guidance, NOT regex-enforced):
   - Includes interpretive siblings such as `recommend`, `suggest`,
     `looks like`, `appears`, `opportunity`, `ripe`, `ready`, `primed`,
     `soon`, `imminent`, `expected to`, `likely to`, `optimal`, `favorable`,
     `good entry`, `bad entry`.
   - Several of these have legitimate factual uses ("ready to query",
     "soon after", "appears in tracker", "favorable spread") that would
     generate false-positive scanner trips if regex-enforced.
   - These are guardrails for content reviewers and PR reviewers: copy
     under review SHOULD avoid these words unless the surrounding context
     is unambiguously factual.

**Promotion path:** if a new variant on the human-review list demonstrates
low false-positive risk and high signal value (i.e., the word's
interpretive use dominates its factual use in this codebase), it can be
promoted to the regex-enforced list. Add it to BOTH
`scripts/check_todays_focus_contract.py` AND
`dashboard/frontend/todayFocusFacts.js` shard array in the same PR; the
list-equality test catches one-sided drift.

## Single Source of Truth for Banned Tokens (design review blocker fold)

The Python contract checker at `scripts/check_todays_focus_contract.py` only scans the `/api/todays_focus` JSON response. The new JS helpers produce CLIENT-side copy that never traverses that scanner. To prevent drift between the two banned lists:

1. Implementation must define `BANNED_PATTERNS` in `dashboard/frontend/todayFocusFacts.js` as a JS constant matching the Python list verbatim (case-insensitive, word-boundary regex).
2. Add a Node/static test that asserts the JS list equals the Python list (parse both, compare set-equality). New entries to one MUST be added to the other; this test catches drift.
3. Add a test that runs the banned scan against every helper-produced fixture output (every machine reason in the translation table + the unmapped fallback + the empty-state copy `"No block reason recorded"`).

## `surfaces` Field Handling (design review blocker fold)

The `/api/todays_focus` payload includes a `surfaces: list[str]` field on each row (e.g., `["top_gainers_tracker", "narrative_momentum"]`). The expanded inspection packet MUST include this as a factual detail row:

- Label: `Surfaces`
- Value: comma-joined sorted list; `"-"` if empty/null.
- No interpretation, no ordering by "importance," no source-quality weighting.

## UI Discipline (non-blocking folds)

- **Row sort order:** frontend MUST NOT re-sort rows. Order is endpoint-provided and rendered as-is. No client-side sorting on score, conviction, freshness, or any derived field.
- **Visual uniformity:** chip and button styling is uniform across rows. No severity coloring (red/amber/green), no icon weight differences, no font-weight ranking. Color is allowed only for non-semantic state (e.g., expanded vs collapsed indicator).
- **Expand state persistence:** `expandedRows` lives in React local state only. NEVER persist to localStorage. Each page load starts with all rows collapsed.
- **Truncation policy:** `buildFocusDetailRows` returns the full ordered fact list. The expanded packet renders all of them; no truncation. The existing 3-fact cap in the compact main row stays unchanged.
- **Empty-state copy `"No block reason recorded"`** must be added to the banned-language scan fixture (verifies it doesn't contain banned tokens).

## Implementation Checklist

- [ ] Write failing Node tests for `todayFocusFacts.js` translations, detail rows, and banned-copy scan.
- [ ] Add a fixture covering all currently known Today's Focus reason constants and assert no raw `v1_`, `*_missing_or_invalid`, or action-language fallback leaks.
- [ ] Write failing static layout test that requires details toggle and detail-grid CSS.
- [ ] Add a committed-dist assertion for `todays-focus-detail-grid`, the details toggle, and at least one helper-produced label.
- [ ] Add a 375px render smoke with an expanded long-token row and assert no horizontal overflow.
- [ ] Implement `todayFocusFacts.js`.
- [ ] Wire expand/collapse UI in `TodayFocusPanel.jsx`.
- [ ] Compact mobile layout and details grid CSS.
- [ ] Update plan/design review notes in `tasks/todo.md`.
- [ ] Run focused Python/Node tests and dashboard build.
- [ ] Create PR, get three orthogonal reviews, fold findings, merge, deploy, and smoke the dashboard plus contract checker.

## Merge Gate

PR merges only when ALL three of the following hold:

1. CI is green (GitHub Actions `test` job conclusion = SUCCESS).
2. All three orthogonal reviewers (product/anti-scope, frontend/mobile/runtime, contract/test/dist drift) return EITHER zero findings OR only non-blocking findings.
3. Every blocking finding (if any) has been folded into the PR and the affected reviewer re-confirms or new commit verification passes.

If any reviewer flags a blocker that requires design-level adjustment (not a code fix), the autonomous chain pauses and operator decides whether to re-design or descope. Non-blocking suggestions become fast follow-ups filed in `tasks/todo.md`, not merge blockers.

Deploy proceeds only after merge; smoke must demonstrate the new expand-view functionality (not just the unchanged endpoint contract):
- Render Today's Focus and click `Details` on at least one row with a block reason.
- Verify expanded panel renders factual-only copy (no banned tokens, no raw machine text).
- Render at 375px portrait and confirm no horizontal overflow with an expanded long-token row.
