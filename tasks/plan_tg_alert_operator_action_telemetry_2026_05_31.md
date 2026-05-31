**New primitives introduced:** `tg_alert_operator_actions` table; `Database.record_tg_alert_operator_action`; `/api/tg_alerts/recent`; `/api/tg_alerts/{alert_id}/operator-action`; dashboard operator-action buttons on recent Telegram dispatches.

# TG Alert Operator-Action Telemetry Plan

## Goal

Capture factual operator labels on currently sent Telegram alerts so the TG alert-qualification work no longer has an UNKNOWN operator-action/noise input.

## Drift Check

- `tasks/todo.md` lists `BL-NEW-TG-ALERT-OPERATOR-ACTION-TELEMETRY` as PROPOSED and explicitly calls for "1 new table + 1 new minimal write path + dashboard surface".
- `tg_alert_log` already records dispatch attempts and outcomes, including `sent`, `tg_alert_dispatched`, and `tg_alert_delivered` side telemetry.
- No existing table or endpoint records whether the operator acted on, ignored, or rejected an alert.

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Operator feedback collection | none found in Hermes skill hub or installed Hermes plugin inventory for gecko-alpha-specific dashboard labels | build in repo |
| Telegram message reaction polling | none suitable for the existing bot-only dispatch path; would require a new Telegram client-side integration | do not build now |
| SQLite telemetry persistence | none covers gecko-alpha's `tg_alert_log` schema or dashboard API | build in repo |

Awesome-Hermes ecosystem check: no reusable Hermes capability provides a project-local operator feedback table, FastAPI endpoint, and dashboard surface bound to `tg_alert_log`.

## Scope

Ship a small, factual labeling path:

1. Add `tg_alert_operator_actions`.
2. Add a `Database.record_tg_alert_operator_action(...)` helper that validates action enum and upserts one current label per `tg_alert_log` row.
3. Add dashboard endpoints:
   - `GET /api/tg_alerts/recent?limit=...` returns recent `sent` TG alerts plus existing label if present.
   - `POST /api/tg_alerts/{alert_id}/operator-action` records a label.
4. Add a compact dashboard panel on the existing TG Alerts tab with buttons: `Acted`, `Useful`, `Ignored`, `Bad`.

## Anti-Scope

- No alert dispatch changes.
- No ranking, urgency, scarcity, TRADE_NOW, or recommendation copy.
- No Telegram reaction polling.
- No inference from paper-trade outcome to operator action.
- No mutation to `/api/trade_inbox`, `/api/todays_focus`, or `/api/alert_intent`.

## Data Shape

`tg_alert_operator_actions` stores one current operator label per alert:

- `tg_alert_log_id` unique, required.
- `paper_trade_id`, `token_id`, `signal_type`, and `alerted_at` copied from `tg_alert_log` at mark time for stable analysis even if the source row changes.
- `action` enum: `acted`, `useful`, `ignored`, `false_positive`.
- `note`, optional, capped by API.
- `source`: `dashboard` for V1.
- `marked_at` and `updated_at`.

## Tests

- Migration creates table, unique index, and paper_migrations marker.
- Recording an action upserts rather than duplicating.
- Invalid actions are rejected before SQL.
- API returns recent sent alerts with labels and records a label.
- Frontend static test proves the TG tab contains the feedback controls and no recommendation language.

## Rollback

Code rollback is enough. The table is additive and inert; leaving it in the DB is safe.
