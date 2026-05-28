**New primitives introduced:** `todayFocusFacts.js` factual translation helper; Today’s Focus row expand/collapse inspection packet.

# Today's Focus Inspection Packet Design

## Intent

Today's Focus should remain a scarce review queue, not a trade queue. The change adds an expandable fact packet so the trader can inspect a row faster without leaving the dashboard or decoding machine values. The packet reports state and history only; it does not interpret whether a row is good, late, urgent, or suitable.

## Data Flow

No backend schema change is allowed in this PR. If the existing payload lacks a required fact, the implementation stops and a separate schema-contract PR is scoped.

`/api/todays_focus` already returns the source data:
- identity: `token_id`, `symbol`, `name`, `chain`, `source_corpus`
- price facts: `current_price`, `market_cap`, `price_change_24h`, `price_updated_at`, `price_is_stale`, `price_staleness_minutes`
- timing/move facts: `opened_at`, `opened_age_hours`, `current_move_pct`, `move_basis`
- reason facts: `trade_inbox_group`, `window_state`, `verdict`, `entry_quality`, `block_reason_primary`, `block_cause`, `risk_reasons`, `inclusion_reasons`, `counter_flag_facts`

The frontend maps those values to readable factual labels. If a value is unknown, the detail row renders `-` rather than inventing interpretation.

## Frontend Modules

`dashboard/frontend/todayFocusFacts.js` owns all copy-producing helpers:
- `reasonLabel(reason)` maps known machine strings to factual labels and falls back to `Unmapped reason`; raw unknown machine strings are never rendered as visible copy.
- `blockCauseLabel(cause)` maps `data_quality`, `data_path`, and `unknown` to factual labels.
- `primaryBlockFacts(row)` returns up to two short factual lines for the compact row.
- `buildFocusDetailRows(row)` returns `{ label, value }` pairs for the expanded packet.

`TodayFocusPanel.jsx` owns UI state:
- `expandedRows` is local React state only.
- The main row gets a `Details` / `Hide` button.
- Expanded rows render a detail grid below the compact facts.
- Save/dismiss/note behavior remains localStorage-backed and unchanged.

## Copy Contract

The translation helper must not emit action language. The existing contract checker already bans key phrases from the API payload; this PR adds frontend helper tests because the new copy is client-produced.

Banned visible text includes:
- buy, sell, consider, should, target
- trade now, act now, action required
- watch breakout, entry is late, take profit
- urgency, priority, alert, notify

Allowed visible text is factual:
- `Price cache stale`
- `Tracker-only row; no open paper trade`
- `Actionability gate blocked`
- `Move basis: tracker_detection`
- `Source lane: tracker`
- `Unmapped reason`

Unknown source strings are not visible. This deny-by-default rule prevents future enum drift such as `act_now`, `watch_breakout`, `priority_*`, or `trade_now` from leaking through client-side fallback copy.

## Layout

Desktop:
- Keep the existing three-column row structure.
- Main row stays compact: identity, links, move/mcap, short facts, controls.
- Detail packet uses a dense two-column grid with stable labels.

Mobile at 375px:
- Two-column outer row: lane marker + content.
- Controls stack below the content.
- Details grid collapses to one column.
- Diagnostics and usage JSON remain below the main list, not above it.

## Testing

Tests added/updated:
- `tests/test_todays_focus_storage.py`: Node import of `todayFocusFacts.js`, translation mapping, detail rows, no banned copy in generated values.
- `tests/test_todays_focus_storage.py`: real-reason fixture covering the current Today's Focus reason constants in `dashboard/db.py`, with assertions that no raw `v1_`, `*_missing_or_invalid`, or action-language source strings render.
- `tests/test_dashboard_frontend_layout.py`: `TodayFocusPanel.jsx` imports helper, renders details toggle, has detail grid CSS, keeps local-only state, extends banned copy coverage to `todayFocusFacts.js`, and checks committed dist contains the new detail grid and known factual label.
- Browser/Playwright smoke: render a 375px-wide expanded row with a long token id and assert document width does not exceed viewport width.
- Existing endpoint/contract tests stay unchanged unless the implementation needs an API field; the planned design does not.

## Deployment Smoke

After deploy:
1. Fetch dashboard root and confirm HTTP 200.
2. Run `scripts/check_todays_focus_contract.py --url http://127.0.0.1:8000 --window-hours 36 --verbose`.
3. Fetch `/api/todays_focus?window_hours=36` and print row count plus `block_cause` / `block_reason_primary`.
4. Open the dashboard in a browser or verify the built bundle contains `todays-focus-detail-grid` and `Price snapshot missing`.
