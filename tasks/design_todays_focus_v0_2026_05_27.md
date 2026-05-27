**New primitives introduced:** `/api/todays_focus`, Today’s Focus dashboard panel, Today’s Focus localStorage V0 schema, Today’s Focus factual-copy contract firewall.

# Today's Focus V0 Design — 2026-05-27

## Goal

Create a scarce, factual dashboard review queue over existing Trade Inbox rows.
The implementation is intentionally read-only on the server and local-only in
the browser. It exists to reduce scanning effort, not to advise, rank, alert,
size, or execute.

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Gecko dashboard endpoint composition | none found | Build from scratch using existing `dashboard.db` patterns. |
| Browser localStorage review state | none found | Build a small local helper; no server persistence. |
| Contract firewall for factual copy | none found | Build from scratch by adapting Gecko's existing dashboard contract checker pattern. |

Awesome-Hermes ecosystem check: no reusable component fits this repo-specific
Trade Inbox composition and contract-firewall requirement.

## Source Composition

`get_todays_focus(db_path, window_hours=36, max_rows=5)` calls
`get_trade_inbox(db_path, limit_per_group=20, window_hours=window_hours)` and
curates only from rows already returned by `/api/trade_inbox`.

Eligibility:

- row has non-empty `token_id`;
- row has at least one display identity field: `symbol` or `name`;
- no additional future-runner, alert, source-rank, quality-score, or buy/sell
  filters.

Curation:

1. Paper quota: take up to 3 paper rows from existing Trade Inbox groups in this
   fixed order: `act_now`, then `watch`.
2. Tracker quota: take up to 2 tracker rows from existing Trade Inbox groups in
   fixed group order: `act_now`, `watch`, `already_ran`, `blocked`. Existing
   Trade Inbox already prevents tracker rows from `act_now`; the order remains
   explicit for contract clarity.
3. Fill any remaining slots, up to 5 total, from unused eligible rows in the
   same fixed group order and existing Trade Inbox row order.
4. Deduplicate by `(source_corpus, token_id)`.

Today’s Focus inherits Trade Inbox’s existing sorted group order as source
order; it does not read or expose `trade_score`, `sort_key`, or `why_now`.
Tests must statically assert `get_todays_focus` does not access those fields.

## Response Schema

Top-level keys: `meta`, `rows`.

Meta:

```json
{
  "read_only": true,
  "not_trade_advice": true,
  "visibility_only": true,
  "experimental": true,
  "not_for_alerting": true,
  "not_for_execution": true,
  "not_for_sizing": true,
  "not_for_source_ranking": true,
  "generated_at": "...",
  "source_endpoint": "/api/trade_inbox",
  "source_window_hours": 36,
  "source_limit_per_group": 20,
  "source_rows_considered": 0,
  "source_group_counts": {"act_now": 0, "watch": 0, "already_ran": 0, "blocked": 0},
  "source_truncated": false,
  "tracker_source_truncated": false,
  "max_rows": 5,
  "paper_target": 3,
  "tracker_target": 2,
  "cache_ttl_minutes": 60,
  "curation_policy": "fixed_recipe_3_paper_2_tracker_no_score",
  "rows_returned": 0,
  "eligible_rows_considered": 0,
  "empty_state": "No eligible Trade Inbox rows are available for Today's Focus. Source window: 36h."
}
```

Row:

```json
{
  "row_key": "paper:token-id",
  "token_id": "token-id",
  "symbol": "TOK",
  "name": "Token",
  "chain": "coingecko",
  "source_corpus": "paper",
  "trade_inbox_group": "act_now",
  "window_state": "open",
  "verdict": "candidate_review",
  "entry_quality": "fresh_entry",
  "surfaces": ["gainers_early"],
  "opened_at": "...",
  "opened_age_hours": 2.0,
  "current_price": 1.23,
  "market_cap": 1000000.0,
  "price_change_24h": 12.5,
  "price_updated_at": "...",
  "price_is_stale": false,
  "price_staleness_minutes": 5.0,
  "current_move_pct": 3.0,
  "move_basis": "paper_entry",
  "entry_quality_facts": ["Trade Inbox group: act_now"],
  "current_risk_facts": ["Price cache stale: false"],
  "counter_flag_facts": [],
  "inclusion_reasons": ["open_paper_trade"],
  "risk_reasons": [],
  "block_reason_primary": null
}
```

Forbidden source fields:

- `action_label`
- `trade_score`
- `sort_key`
- `why_now`

The contract checker rejects those keys anywhere in the payload.

## Factual Copy Builder

Backend helpers build lists of factual strings:

- `entry_quality_facts`
- `current_risk_facts`
- `counter_flag_facts`

All strings pass a shared `_today_focus_clean_text` guard. Values failing the
guard are withheld and replaced only by a neutral diagnostic such as
`Source text withheld by factual-copy firewall`.

The scanner uses the existing dashboard-contract normalization pattern: NFKC,
casefold, strip Unicode format/control characters, collapse whitespace, then
match boundary-aware expressions.

Forbidden patterns use word/phrase boundaries:

- `\bbuy\b`, `\bsell\b`, `\bconsider\b`
- `\btrade[\s_-]*now\b`
- `\bwatch[\s_-]*breakout\b`
- `\bentry[\s_-]*is[\s_-]*late\b`
- `\bpullback\b`, `\btarget\b`, `\bshould\b`
- `\brecommend(?:ed|ation)?\b`
- `\bgo[\s_-]*long\b`
- `\benter[\s_-]*here\b`
- `\btake[\s_-]*profit\b`
- `\bstrong[\s_-]*buy\b`
- `\bmust[\s_-]*buy\b`
- `\bact[\s_-]*now\b`
- `\baction[\s_-]*required\b`
- `\bacting\b`
- `\bnow[\s_-]*tradeable\b`
- `\btradeable[\s_-]*now\b`
- ranking/alert tokens already guarded by the Trade Inbox checker, including
  `urgency`, `priority`, `alert`, `notify`, `operator_priority`,
  `trade_now`, `watch_breakout`, and `research_only`.

Regression tests must prove `buyer` and `buyback` are not false positives.
The copy scanner applies to user-visible copy fields (`entry_quality_facts`,
`current_risk_facts`, `counter_flag_facts`, `empty_state`) and frontend visible
labels. Enum/provenance fields such as `entry_quality` are checked against
allowlists instead of free-text scanned, so source values like
`acceptable_pullback` do not fail solely because they contain `pullback`.
Internal source coverage maps may still use canonical Trade Inbox keys such as
`act_now` when they are not rendered as row labels or factual copy.

Counter flags are not rendered as-is. The builder accepts strings or dicts,
extracts neutral fields (`label`, `type`, `name`, `reason`, `detail`), strips
severity words, enforces the firewall, and caps each fact at 80 characters.

## Move Basis

Use the existing `pct_from_entry` field but make basis explicit:

- paper rows: `current_move_pct = pct_from_entry`,
  `move_basis = "paper_entry"`;
- tracker rows: `current_move_pct = pct_from_entry`,
  `move_basis = "tracker_detection"`.

The frontend renders the basis label verbatim. No row can infer an entry
recommendation from the move value.

## Frontend

Files:

- `dashboard/frontend/components/TodayFocusPanel.jsx`
- `dashboard/frontend/todayFocusStorage.js`
- `dashboard/frontend/App.jsx`
- `dashboard/frontend/style.css`

Behavior:

- new dashboard tab label: `Today's Focus`;
- fetch path: `/api/todays_focus?window_hours=36`;
- storage key: `gecko.todaysFocus.v0`;
- storage schema fields:
  - `schema_version`;
  - `cached_payload`;
  - `cached_at`;
  - `last_refreshed_at`;
  - `usage_started_at`;
  - `actions_by_row_key`;
  - `usage_counters`.
- cache TTL: 60 minutes;
- manual refresh bypasses TTL and preserves actions/notes;
- `save_for_review` and `dismiss` update localStorage and force-refresh;
- `note` persists local text only;
- usage evidence is rendered from localStorage as sanitized JSON with
  `usage_started_at`, refresh/cache timestamps, counters, row-state counts, and
  cached row count. It deliberately omits note text and is never sent to the
  backend.
- `notes_saved` counts first non-empty note creation per row, not each
  keystroke; `row_state_counts.notes` exposes current non-empty note count.
- no `I’m in` state, server write, alert hook, or backend effect.

Client overlay:

- the endpoint returns all eligible source rows;
- the panel applies `actions_by_row_key[row_key].dismissed === true` after every
  fetch;
- saved rows remain visible with local marker text;
- dismissed rows are hidden until local state is restored;
- fetch failures keep the last valid cached payload visible with a factual
  stale/error banner;
- corrupt JSON/schema mismatch clears cache fields while preserving
  actions/notes when possible.

Mobile:

- 375px portrait is the minimum target;
- the panel uses compact rows and avoids wide tables;
- static tests assert source constraints: no table layout, compact row class,
  bounded row padding/font sizes, wrapped/clamped text, and controls that can
  wrap without overflow;
- browser/Playwright smoke at a 375px viewport is required before claiming the
  visual fit if a local app is started in this session.

## Contract Checker

Add `scripts/check_todays_focus_contract.py`:

- fetches `/api/todays_focus`;
- validates exact top-level/meta/row keys;
- validates read-only/not-trade-advice/visibility flags;
- validates anti-scope flags: `not_for_alerting`, `not_for_execution`,
  `not_for_sizing`, `not_for_source_ranking`;
- validates source diagnostics: `source_rows_considered`,
  `source_group_counts`, `source_truncated`, `tracker_source_truncated`,
  `source_limit_per_group`;
- rejects forbidden keys and forbidden value/copy patterns;
- validates max rows and stable row keys;
- scans `empty_state`;
- uses boundary regexes to avoid `buyer`/`buyback` false positives.

Update `scripts/check_dashboard_contracts.py` to include the new checker.

## Tests

Add focused tests:

- `tests/test_todays_focus_endpoint.py`
  - response envelope and read-only flags;
  - 3 paper + 2 tracker curation;
  - underfilled quota fallback;
  - paper/tracker move basis;
  - forbidden source fields absent;
  - actionability/counter text sanitizer;
  - empty state.
- `tests/test_check_todays_focus_contract.py`
  - clean payload passes;
  - banned phrases fail;
  - `buyer`/`buyback` pass;
  - forbidden keys fail.
- `tests/test_check_dashboard_contracts.py`
  - aggregate checker runs Today’s Focus alongside existing checks.
- `tests/test_dashboard_frontend_layout.py`
  - tab wired;
  - fetch path present;
  - storage key/schema present;
  - localStorage only for save/dismiss/note;
  - no `I'm in`;
  - no table layout;
  - compact 375px CSS constraints;
  - no POST/PUT/PATCH/delete fetch paths;
  - static frontend/dist copy scan for forbidden phrases and forbidden
    affordances.

## Verification

Focused:

```bash
uv run pytest -q tests/test_todays_focus_endpoint.py tests/test_check_todays_focus_contract.py tests/test_check_dashboard_contracts.py tests/test_dashboard_frontend_layout.py
npm.cmd --prefix dashboard/frontend run build:codex
```

Broader dashboard:

```bash
uv run pytest --tb=short -q tests/test_trade_inbox_endpoint.py tests/test_check_trade_inbox_contract.py tests/test_check_live_candidates_contract.py tests/test_live_candidates_endpoint.py tests/test_todays_focus_endpoint.py tests/test_check_todays_focus_contract.py tests/test_check_dashboard_contracts.py tests/test_dashboard_frontend_layout.py
```

## Deployment Smoke

After merge/deploy:

1. Run aggregate dashboard contract checker against prod.
2. Fetch `/api/todays_focus?window_hours=36`.
3. Confirm rows include only factual copy and no forbidden keys.
4. Check currently fresh tracker rows, or motivating tokens if inside the
   freshness window, are visible when they meet the fixed recipe.

## Non-Scope

No Telegram alerts, alert qualification, urgency tiers, `TRADE_NOW`,
`WATCH_BREAKOUT`, buy/sell/consider language, composite score, future-runner
labels, source ranking, signal policy, paper-trade policy, live execution,
sizing, server-side personal-position storage, new DB table, cron, or watchdog.
