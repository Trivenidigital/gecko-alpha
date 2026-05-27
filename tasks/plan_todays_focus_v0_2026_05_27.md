**New primitives introduced:** `/api/todays_focus`, Today’s Focus dashboard panel, Today’s Focus localStorage V0 schema, Today’s Focus factual-copy contract firewall.

# Today's Focus V0 Plan — 2026-05-27

## Goal

Add a scarce, factual, dashboard-only review queue that helps the trader inspect
current rows without scanning the full inbox. This surface composes the
already-shipped Trade Inbox, tracker-promotion path, and counter-risk context.
It does not rank, alert, size, execute, or introduce urgency tiers.

## Drift Check

Existing primitives found:

| Primitive | Evidence | Decision |
|---|---|---|
| Trade Inbox source surface | `dashboard/db.py:get_trade_inbox`, `/api/trade_inbox`, `TradeInboxTab.jsx` | Compose; do not replace. |
| Tracker promotion | `get_trade_inbox` emits `source_corpus="tracker"` rows from `gainers_comparisons` | Use for the 2 fresh tracker slots. |
| Counter-risk context | Trade Inbox rows already expose `counter_flags` and `counter_risk_score` for paper rows | Surface facts only; no derived risk score. |
| Anti-scope contract pattern | `scripts/check_trade_inbox_contract.py`, `scripts/check_dashboard_contracts.py` | Extend with Today’s Focus-specific firewall. |

Residual gap: no scarce “what should I inspect first” queue exists. The current
dashboard still requires scanning Trade Inbox groups plus tracker rows manually.

## Hermes-First Analysis

Checked Hermes Agent skills hub / bundled catalog for dashboard curation,
decision-support UI, localStorage state, and factual-copy firewall domains.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Dashboard curation endpoint | none found | Build from scratch because this composes Gecko-specific Trade Inbox contracts. |
| Local browser review state | none found | Build from scratch in React/localStorage. |
| Contract firewall for factual copy | none found | Build from scratch using existing Gecko checker pattern. |
| Trading alert qualification | not applicable | Out of scope; deferred until soak inputs are complete. |

Awesome-Hermes ecosystem check: no reusable Gecko-dashboard or trading-review
queue primitive applies; this is product-specific glue over existing local APIs.

## Pinned Product Decisions

### Curation Rule

Return at most 5 rows:

1. Up to 3 paper-backed rows using a fixed, non-scoring display recipe over
   existing Trade Inbox buckets: `act_now` then `watch`. These bucket names are
   factual provenance from the source endpoint, not renamed urgency tiers and
   not calls to action.
2. Up to 2 fresh tracker-promoted rows from any Trade Inbox group, preserving
   `source_corpus="tracker"` and factual source group/state labels.
3. If either quota is underfilled, fill remaining slots from unused Trade Inbox
   rows using the existing Trade Inbox group order and sort order, preserving
   source labels.

No composite score is introduced. Ordering is deterministic by bucket order and
existing Trade Inbox recency/state data, not by a new ranking model.

Eligibility is closed: a row is eligible only if it is already returned by
`/api/trade_inbox`, has required display identity, and has not been dismissed in
browser-local state. No additional quality, future-runner, alert, buy/sell, or
source-ranking filters may be added in Today’s Focus.

### Refresh Cadence

The backend recomputes on request. The frontend pins the displayed payload for
60 minutes in `localStorage`, shows backend `generated_at` and frontend
`last_refreshed_at`, includes a manual refresh button, and force-refreshes after
watch/dismiss actions.

### Field Mappings

`entry_quality` facts:

- existing Trade Inbox `group`, `verdict`, `entry_quality`, `actionable`, and
  raw `actionability_reason` only if V0 explicitly queries it and it is
  firewall-clean;
- age since detection/entry;
- current move using explicit precedence:
  - paper rows: `pct_from_entry` means move since paper entry;
  - tracker rows: `pct_from_entry` means move since tracker detection price.

`current_risk` facts:

- sanitized `counter_flag_facts` generated from existing `counter_flags` for
  paper rows; severity words and advice-like labels are not rendered;
- `price_is_stale`, `price_staleness_minutes`, `risk_reasons`,
  `block_reason_primary`, and factual `price_change_24h`;
- no derived risk score, no sizing, no interpretive advice.

Raw source fields such as `entry_quality`, `risk_reasons`, and
`block_reason_primary` may be rendered only after firewall validation and must
not be mapped into labels like good entry, late, safe, risky, buyable, avoid,
act, or watch.

### Local State V0

Ship Today’s Focus plus Phase 3 V0 local state together:

- controls: `save_for_review`, `dismiss`, `note`;
- explicitly no “I’m in” state in V0;
- storage: browser `localStorage` only;
- usage counters: sessions, watch/dismiss actions, notes saved.
- local state is never used by backend curation, scoring, ordering, alerts, or
  alert qualification.

Storage key/state machine:

- key: `gecko.todaysFocus.v0`;
- fields: `schema_version`, `cached_payload`, `cached_at`,
  `last_refreshed_at`, `actions_by_row_key`, and `usage_counters`;
- schema mismatch or corrupt JSON clears cache and starts fresh;
- manual/forced refresh updates `cached_payload` and `last_refreshed_at` while
  preserving actions and notes;
- dismiss filters by stable row key after every refresh.

Future usage-read success criteria:

- at least 5 save/dismiss actions per week, or
- Today’s Focus opened in at least 5 sessions, or
- at least 3 notes saved.

### Factual-Copy Anti-Scope

Copy reports state and history only. It must not tell the trader what to do.

Allowed examples:

- `Source corpus: tracker`
- `Move since tracker detection: +29.4%`
- `Trade Inbox group: blocked`
- `Price cache age: 22.5 minutes`

Forbidden examples:

- `Entry is late unless it consolidates`
- `Watch breakout above $X`
- `Consider buying`
- `Trade now`

The endpoint payload, frontend rendered copy, empty states, controls, storage
labels, and committed dist artifacts must pass a contract firewall using
word/phrase boundary regexes, not substring matching, so `buy` is banned but
`buyer` and `buyback` are not.

The endpoint must not expose source fields that already encode action/urgency
semantics: `action_label`, `trade_score`, `sort_key`, or `why_now`. Use factual
aliases only: `trade_inbox_group`, `window_state`, `entry_quality`,
`source_corpus`, and `move_basis`.

## Contract Schema

Top-level keys: `meta`, `rows`.

Meta keys:

- `read_only`, `not_trade_advice`, `visibility_only`, `experimental`;
- `generated_at`, `source_endpoint`, `source_window_hours`;
- `max_rows`, `paper_target`, `tracker_target`, `cache_ttl_minutes`;
- `curation_policy`, `rows_returned`, `eligible_rows_considered`;
- `empty_state`.

Row keys:

- identity: `row_key`, `token_id`, `symbol`, `name`, `chain`,
  `source_corpus`;
- source state: `trade_inbox_group`, `window_state`, `verdict`,
  `entry_quality`, `surfaces`;
- price facts: `opened_at`, `opened_age_hours`, `current_price`, `market_cap`,
  `price_change_24h`, `price_updated_at`, `price_is_stale`,
  `price_staleness_minutes`, `current_move_pct`, `move_basis`;
- factual copy: `entry_quality_facts`, `current_risk_facts`,
  `counter_flag_facts`;
- diagnostics: `inclusion_reasons`, `risk_reasons`, `block_reason_primary`.

## Runtime-State Verification

Prod `actionability_reason` sample, 2026-05-27, via srilu:

- `v1_block_core_signal_mcap_below_10m`
- `v1_block_tg_social_low_n`
- `v1_pass_core_signal_mcap_10_50m`
- `v1_pass_core_signal_mcap_50m_plus`

No sampled value contains advice-like prose. The implementation will still
sanitize/withhold any future mapped value that violates the Today’s Focus
firewall before rendering.

## Implementation Tracks

1. Tests first:
   - endpoint shape and read-only behavior;
   - 3 paper + 2 tracker curation;
   - quota fallback;
   - paper/tracker current-move precedence;
   - factual-copy firewall catches forbidden phrases and avoids substring false
     positives;
   - `action_label`, `trade_score`, `sort_key`, and `why_now` are absent;
   - counter flag sanitizer with `severity="high"` and forbidden label text;
   - empty-state copy is factual;
   - frontend tab wiring, localStorage-only state, cache TTL/corrupt-schema
     handling, no “I’m in”, and mobile 375px layout marker.
2. Backend:
   - add Pydantic response models;
   - add `dashboard.db.get_todays_focus`;
   - add `/api/todays_focus`.
3. Contract:
   - add `scripts/check_todays_focus_contract.py`;
   - include Today’s Focus in `scripts/check_dashboard_contracts.py`.
4. Frontend:
   - add `TodayFocusPanel.jsx`;
   - add tab in `App.jsx`;
   - add compact mobile CSS;
   - rebuild committed `dist`.
5. Verification:
   - focused pytest;
   - dashboard contract pytest;
   - frontend build;
   - runtime contract smoke against local app if started.

## Review Plan

- Plan review by two parallel agents:
  - product/anti-scope vector;
  - backend/frontend contract vector.
- Design review by two parallel agents:
  - endpoint/schema/firewall vector;
  - React/mobile/localStorage vector.
- PR review by two parallel agents after implementation.

## Non-Scope

No Telegram alerts, alert qualification, urgency tiers, `TRADE_NOW`,
`WATCH_BREAKOUT`, buy/sell/consider language, composite score, future-runner
labels, source ranking, signal policy, paper-trade policy, live execution,
sizing, server-side personal-position storage, new DB table, cron, or watchdog.
