**New primitives introduced:** trade_surface_tg_alerts_dispatcher

# Trade Surface Telegram Alerts Plan

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Dashboard-derived Telegram alerts | none found in project Hermes conventions | build in gecko-alpha; it depends on local DB tables and TG audit schema |
| Generic messaging | Telegram sender already exists in `scout.alerter` | reuse existing sender, no custom transport |

awesome-hermes-agent ecosystem check: no reusable Hermes primitive replaces the project-specific Today Focus / Now Tradable row selection and `tg_alert_log` audit path.

## Runtime facts

- srilu is running master `a6dcd13`.
- `/api/todays_focus?window_hours=36` returns 5 rows: 3 paper + 2 tracker.
- `/api/live_candidates?limit=30&window_hours=36` returns 30 rows: 4 `candidate_review`, 19 `watch`, 6 `blocked`, 1 `data_insufficient`.
- Top 3 Now Tradable `candidate_review` rows overlap with the paper Today Focus rows.

## Scope

- Add an opt-in pipeline lane that reads the same underlying rows as Today Focus and Now Tradable.
- Select scarce alert candidates:
  - rows present in both Today Focus and Now Tradable `candidate_review` first;
  - remaining Now Tradable `candidate_review` rows next;
  - remaining Today Focus rows last.
- Enforce per-token dedup and daily cap before Telegram dispatch.
- Write `tg_alert_log` rows for `sent`, `blocked_dedup_24h`, and dispatch failure outcomes so the TG Alerts tab can collect operator labels.
- Use `parse_mode=None` and structured dispatched/delivered logs.

## Anti-scope

- Do not alter `/api/todays_focus` or `/api/live_candidates` contracts.
- Do not send every row on every refresh.
- Do not bypass existing paper-trade execution or eligibility rules.
- Do not add trading advice, urgency labels, sizing, or live-order commands.
