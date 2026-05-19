**New primitives introduced:** NONE. This is a read-only design doc. It defines the minimum schema/fields/joins needed so X and TG inbound rows can be linked to their `paper_trades` outcome, plus a backfill strategy and dashboard/API surfacing plan. No code change, no schema migration, no behavior change.

# Design: TG/X Outcome Linkage — 2026-05-19

## Guardrail

This design is produced while the 24h actionability observation window is still
accumulating. It must NOT change trade suppression, entry rules, sizing, or
capital allocation. No suppression, no disabling exploratory trades, no capital
weighting, no live-trading change, no gate-threshold change. The output is a
design doc only; any implementation waits on the actionability validation
runbook and a separate approval.

Scope is explicitly **outcome linkage**, not source ranking. We do not rank
KOLs, X handles, TG channels, or detected_by combos in this pass. We make the
joins possible; ranking is a future ledger that consumes these joins.

## Problem Statement

The peak-giveback / freshness audit
(`tasks/findings_peak_giveback_freshness_audit_2026_05_19.md`) and the
actionability work both depend on attributing `paper_trades` outcomes back to
the originating inbound social signal. Today:

- **TG side** structurally links inbound → paper_trade via
  `tg_social_signals.paper_trade_id` (FK with `ON DELETE RESTRICT`). The join
  works; the gap is dashboard/API surfacing and backfill for rows where
  `paper_trade_id IS NULL` because dispatch happened before the FK landed or
  was blocked by a gate.
- **X side** has `narrative_alerts_inbound` (Hermes scanner → HMAC →
  append-only) but **no `paper_trade_id` column** and no documented join path
  to `paper_trades`. Inbound X events and `paper_trades` of
  `signal_type='narrative_prediction'` are not the same population — the
  internal narrative predictor opens trades from its own model, not from
  inbound Hermes alerts. Linking inbound X alerts to outcomes requires either
  a new linkage table or an additive nullable column on
  `narrative_alerts_inbound`.

The audit's strongest V2 candidate (`pre_entry_peak_gain_pct >= 40%` AND
`pre_entry_giveback_ratio >= 0.50`) plus future source-quality work both need
this linkage to attribute correctly. Without it, every "did the X handle's
alert make money" question requires reconciling on coin_id + a fragile time
window heuristic.

## Drift Check

Per global CLAUDE.md §7a, before scoping new primitives.

### Tables that already exist

| Table | Role | Has paper_trade_id? | Source |
|---|---|---|---|
| `paper_trades` | Outcome of record (open/closed, peak, PnL, actionability) | — (it IS the outcome table) | `scout/db.py:778` |
| `tg_social_messages` | Raw TG message ingest, append-only | No (message-level) | `scout/db.py:1188` |
| `tg_social_signals` | Resolved TG cashtag/CA → token, with dispatch state | **Yes, FK ON DELETE RESTRICT** | `scout/db.py:1203-1222` |
| `narrative_alerts_inbound` | X-alert ingest from Hermes scanner, append-only | **No** | `scout/db.py:3416` |
| `alerts` | Conviction-score Telegram alerts dispatched to operator | No (alert-level, not source-level) | `scout/db.py:339` |
| `social_signals` | Older social-signal scaffold (pre-BL-064) | No | `scout/db.py:860` |
| `tg_social_health` / `tg_social_dlq` | TG operational state | — | `scout/db.py:1225` / `1234` |

### Dispatch / linkage paths that already exist

- TG cashtag/CA dispatch:
  `scout/social/telegram/listener.py` → `scout/social/telegram/dispatcher.py`
  → `TradingEngine.open_trade(signal_type='tg_social', ...)` → returns
  `trade_id` → written back to `tg_social_signals.paper_trade_id`
  (`scout/social/telegram/dispatcher.py:230,503,514,527`).
- TG outcome JOIN exists in operational queries:
  `JOIN paper_trades p ON s.paper_trade_id = p.id WHERE s.source_channel_handle = ?`
  (`scout/social/telegram/dispatcher.py:87,314`).
- Internal narrative predictor (NOT inbound X) opens trades:
  `scout/trading/signals.py:763` with `signal_type='narrative_prediction'`.
  These are unrelated to `narrative_alerts_inbound` rows.
- X operator alert path (BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE): writes to
  `narrative_alerts_inbound`, emits operator Telegram message, **does not
  open a paper_trade**. No linkage is required because no trade exists.

### Residual gap

1. `narrative_alerts_inbound` has **no `paper_trade_id` link.** If/when a
   future PR wires inbound X events to auto-open paper_trades, the back-link
   is missing.
2. `tg_social_signals.paper_trade_id IS NULL` rows can mean three different
   things (gate-blocked, mcap-blocked, dispatch error). Today only the
   structured logs disambiguate; no schema column makes this queryable.
3. Outcome JOINs are scattered across `dispatcher.py`; no unified read-only
   view/API for both TG and X.
4. There is no canonical place to record `entry_price`, `peak_price`, and
   `pnl_usd` against the **source** (channel/handle) for dashboard rendering.
   These come from `paper_trades` via JOIN, but the dashboards need a
   stable, simple endpoint.

## Hermes-first analysis

Per global CLAUDE.md §7b, before scoping custom code, check the Hermes
ecosystem for an existing capability.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Trade outcome linkage / attribution | **None.** Hermes skill hub returned no matches for trade outcome linkage, alert→paper-trade attribution, or PnL attribution. | Build inside `gecko-alpha`: tables, joins, and dashboard live in the project DB; not a Hermes-shape problem. |
| X/Twitter outcome tracking | **None.** `Hermes-Function-Calling` repo has yfinance examples but no trade-tracking or social-attribution skills. | Reuse the existing `narrative_alerts_inbound` pipeline (already a Hermes producer) and only add a back-link on the gecko-alpha side. |
| TG channel outcome tracking | **None.** No Hermes-side TG tracker; the listener is gecko-alpha-local (`scout/social/telegram/listener.py`). | Stay in-project. |
| Source-quality ledger | **None.** No Hermes ledger primitive. | Out of scope for this design; future work consumes the joins this design enables. |

`awesome-hermes-agent` ecosystem check: repo URL returned 404 from the
default location; no community package surfaced. Verdict: no Hermes-side
primitive applies; the linkage is a gecko-alpha-internal DB+API concern.

## Goals and Non-goals

### Goals

1. Make every inbound X alert (`narrative_alerts_inbound`) and every TG
   signal (`tg_social_signals`) joinable to its `paper_trades` row when one
   exists.
2. Make a `paper_trade_id IS NULL` row's reason queryable, not just visible
   in logs.
3. Define backfill strategy for historical rows where the FK column did not
   exist yet.
4. Define the minimum read-only API/dashboard surface that exposes
   per-source outcomes (so the source-quality ledger can later consume it).

### Non-goals

- Source-quality ranking (handle/channel scoring) — separate design.
- Auto-opening paper_trades from inbound X alerts — separate proposal.
- Schema migrations, code change, or behavior change in this PR.
- Live-trade outcome linkage (BL-055-scoped; same shape applies via
  `live_trades.paper_trade_id`).

## Minimum Fields

User-supplied requirement, refined against the drift-check.

### X side — `narrative_alerts_inbound` join surface

| Field | Already exists? | Source | Notes |
|---|---|---|---|
| `id` (PK) | yes | `narrative_alerts_inbound.id` | Stable identity. |
| `event_id` | yes | `narrative_alerts_inbound.event_id` | Hermes-side dedup key. |
| `resolved_coin_id` | yes | `narrative_alerts_inbound.resolved_coin_id` | Join key to `paper_trades.token_id`. |
| `x_handle` | yes | `narrative_alerts_inbound.tweet_author` | Rename in API as `x_handle`; no DB rename. |
| `tweet_id` / `post_id` | yes | `narrative_alerts_inbound.tweet_id` | |
| `tweet_ts` | yes | `narrative_alerts_inbound.tweet_ts` | Temporal anchor for backfill matching. |
| `received_at` | yes | `narrative_alerts_inbound.received_at` | Server-side ingest time. |
| `alert_id` | partial | `narrative_alerts_inbound.id` doubles as alert id today; operator-alert dispatch is in TG bot logs, not DB | If/when operator-alert dispatch becomes a row, add a separate `narrative_operator_alerts` table; for now, `narrative_alerts_inbound.id` is the alert_id. |
| **`paper_trade_id`** | **NO — gap** | new nullable column on `narrative_alerts_inbound` (proposed; not implemented in this design) | Mirror TG: `INTEGER` nullable, FK `paper_trades(id) ON DELETE RESTRICT`. |
| `entry_price` | derived | `paper_trades.entry_price` via JOIN | |
| `peak_price`, `peak_pct` | derived | `paper_trades.peak_price`, `peak_pct` via JOIN | |
| `current_price` | derived | `price_cache.price` keyed on `paper_trades.token_id` | |
| `pnl_usd` (size-normalized) | derived | `paper_trades.pnl_usd` (closed) OR `(current_price - entry_price) * quantity` (open) via JOIN | User's "$300 P&L" framing maps to `pnl_usd`; trade size is `paper_trades.amount_usd` (default $1000, configurable). |
| `pnl_pct` | derived | `paper_trades.pnl_pct` via JOIN | |
| `outcome_status` | derived | `paper_trades.status` + `paper_trades.exit_reason` | `open` / `closed_tp` / `closed_sl` / `closed_trail` / `expired` / `null_if_unlinked`. |
| `actionable` / `actionability_reason` | derived | `paper_trades.actionable`, `paper_trades.actionability_reason` via JOIN | Surfaces the v1 gate output per BL-NEW-ACTIONABILITY. |

### TG side — `tg_social_signals` join surface

| Field | Already exists? | Source | Notes |
|---|---|---|---|
| `id` (PK) | yes | `tg_social_signals.id` | |
| `tg_channel` | yes | `tg_social_signals.source_channel_handle` | |
| `message_id` | yes | `tg_social_messages.msg_id` via FK `tg_social_signals.message_pk` | |
| `posted_at` | yes | `tg_social_messages.posted_at` via FK | |
| `sender` | yes | `tg_social_messages.sender` via FK | Per-message author within channel. |
| `token_id` | yes | `tg_social_signals.token_id` | Join key. |
| `contract_address`, `chain` | yes | `tg_social_signals.contract_address`, `chain` | |
| `mcap_at_sighting` | yes | `tg_social_signals.mcap_at_sighting` | |
| `resolution_state` | yes | `tg_social_signals.resolution_state` | |
| `alert_sent_at` | yes | `tg_social_signals.alert_sent_at` | |
| `paper_trade_id` | yes | `tg_social_signals.paper_trade_id` (FK) | Already structurally complete. |
| `entry_price`, `peak_price`, `current_price`, `pnl_usd`, `pnl_pct` | derived | `paper_trades` via JOIN | Same as X. |
| `outcome_status` | derived | `paper_trades.status` + `paper_trades.exit_reason` | |
| `actionable`, `actionability_reason` | derived | `paper_trades.actionable`, `paper_trades.actionability_reason` via JOIN | |

### Disambiguating `paper_trade_id IS NULL`

`tg_social_signals.paper_trade_id` is nullable today; the same will be true
for any future X-side back-link. NULL means one of:

- gate-blocked at dispatch (e.g., mcap ceiling, exposure cap, dedup);
- safety-blocked (GoPlus / contract gate);
- error during dispatch;
- dispatch never attempted (resolution_state never reached `dispatched`).

The structured logs already record the discriminator
(`dispatch_decision_blocked_gate`, `safety_check_failed`, `dispatch_error`).
Proposal (for future implementation, NOT now): add a nullable
`linkage_state` column on `tg_social_signals` and the future X linkage
column, valued from a small enum (`linked`, `gate_blocked`, `safety_blocked`,
`dispatch_error`, `not_attempted`, `unknown`). This is documented here so
the source-quality ledger can later filter on it.

## Paper_trade Linkage Approach

Two implementation options, **stated for design completeness only**. Pick at
implementation time, after the actionability runbook is reviewed.

### Option A: nullable FK column on the inbound table

Mirror the TG pattern. Add `paper_trade_id INTEGER` (nullable) to
`narrative_alerts_inbound` with FK `paper_trades(id) ON DELETE RESTRICT`.

- Pros: symmetric to TG; smallest schema; standard SQL JOIN.
- Cons: changes shape of the append-only inbound table; may need to relax
  "append-only" contract to "append-only on identity columns, mutable on
  paper_trade_id and linkage_state."
- Migration: ALTER TABLE add column; index on `paper_trade_id` partial WHERE
  NOT NULL; gated behind `paper_migrations` sentinel.

### Option B: separate linkage table

Create `narrative_alert_paper_trade_links` with `(narrative_alert_id,
paper_trade_id, linkage_state, linked_at)`. `narrative_alerts_inbound` stays
strictly append-only.

- Pros: preserves append-only contract on inbound; supports 1-to-many if a
  single tweet eventually opens multiple trades on different signal types.
- Cons: one extra JOIN; another table to monitor (freshness SLO per §12a).

Recommendation (non-binding): Option A for symmetry with TG, unless 1-to-many
becomes a real requirement. Decision is deferred to implementation PR.

### Same shape on TG side

TG already has Option A wired. For the `linkage_state` column, prefer adding
it to `tg_social_signals` (Option A on TG) — symmetric, queryable, no extra
JOIN.

## Backfill Strategy

Backfill is two-sided. Backfill is **read-only and idempotent**; it modifies
only nullable linkage columns and never `paper_trades` itself.

### TG backfill

Population: `tg_social_signals` rows where `paper_trade_id IS NULL` AND
`resolution_state IN ('dispatched','resolved')`.

Match procedure (per row):

1. Candidate join key: `(token_id, signal_type='tg_social', dispatch_ts ± window)`
   where `dispatch_ts` defaults to `tg_social_signals.alert_sent_at` (falls
   back to `tg_social_messages.posted_at` via FK).
2. Look up `paper_trades` rows where `token_id` matches, `signal_type =
   'tg_social'`, `opened_at` within window (proposed window: ±15 min from
   `dispatch_ts`, tunable).
3. If exactly one match: set `paper_trade_id` to that match, set
   `linkage_state = 'linked'`.
4. If zero matches: leave `paper_trade_id` NULL, set `linkage_state` from
   structured-log evidence (`gate_blocked`, `safety_blocked`,
   `dispatch_error`, `not_attempted`, or `unknown` if logs are insufficient).
5. If multiple matches: leave NULL, set `linkage_state = 'ambiguous'`,
   record candidate ids in a sidecar audit table for manual review.

Pre-registered acceptance criteria for the TG backfill run:

- ≥ 95% of rows with `resolution_state='dispatched'` resolve to either
  `linked` or a definite blocked state (`gate_blocked`, `safety_blocked`,
  `dispatch_error`).
- ≤ 1% `ambiguous` outcome (multi-match).
- 0% of `linked` rows have `paper_trades.signal_type != 'tg_social'` — sanity
  check that the join key is correct.

### X backfill

Population: `narrative_alerts_inbound` rows. Today the answer is "no
paper_trade exists for these," because no auto-trade pipeline runs from
inbound X. Backfill is:

1. For every existing inbound row, set `paper_trade_id = NULL`,
   `linkage_state = 'not_attempted'`.
2. From the day the auto-trade path lands (separate proposal), backfill
   becomes "match on `(resolved_coin_id, signal_type, received_at ± window)`"
   following the TG procedure.

Pre-registered: the X backfill is trivial today (`not_attempted` for all
rows). When auto-trade lands, re-state the criteria.

### Backfill safety

- Read-write but bounded: only nullable linkage columns are mutated.
- Idempotent: re-running the backfill on already-linked rows is a no-op.
- Rate-limited: backfill in batches of 200 rows with a sleep between, to
  avoid lock contention with the live writer.
- Auditable: emit `tg_outcome_backfill_*` and `x_outcome_backfill_*`
  structured logs with batch ids; counts go to a `*_backfill_runs` table
  (out of scope here; design only).

## Dashboard / API Impact

### New read-only views (proposed, not implemented)

1. `v_tg_outcomes` — JOIN of `tg_social_signals` + `tg_social_messages` +
   `paper_trades` + `price_cache`. Exposes the full TG join surface above as
   a single rowset; one row per signal.
2. `v_x_outcomes` — JOIN of `narrative_alerts_inbound` + `paper_trades` +
   `price_cache`. Exposes the X join surface. Today most rows have
   `paper_trade_id IS NULL`; the view still surfaces the inbound metadata
   plus a NULL outcome block.
3. `v_unified_source_outcomes` — UNION ALL of the two views with a
   `source_kind` discriminator (`'tg'` or `'x'`). This is the view the
   future source-quality ledger queries.

### API endpoints (proposed, not implemented)

- `GET /api/internal/outcomes/tg?since=...&channel=...` — paginated
  read-only join surface for TG.
- `GET /api/internal/outcomes/x?since=...&handle=...` — same for X.
- `GET /api/internal/outcomes/by-source?source_kind=...&id=...` — unified
  per-source query. Returns the same fields regardless of source.

All endpoints are read-only, HMAC-authed (reuse
`scout/api/narrative._verify_hmac` per BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE).
No write endpoints. No execution endpoints. No live-trade endpoints.

### Dashboard impact

- The actionability dashboard (BL-NEW-ACTIONABILITY) already groups by
  `signal_type`. Add an optional per-source breakout when `signal_type IN
  ('tg_social','narrative_prediction')` using the linkage columns.
- New cohort view (out of scope here): per-channel / per-handle outcomes
  with n-gate ≥ 10 and INSUFFICIENT_DATA fallback (per
  `feedback_n_gate_verdicts_against_dashboard_noise.md`).

## Failure Modes To Pre-empt

- **Class-3 silent rendering corruption (CLAUDE.md §12b).** Any future
  per-channel / per-handle Telegram digest of these outcomes must default
  to `parse_mode=None` for system-health alerts because channel handles and
  signal names contain underscores.
- **§12a freshness SLO.** Any new linkage table or view must ship with a
  freshness watchdog: "TG backfill last-run-at older than N hours" and
  "v_x_outcomes row count delta zero for N hours after a known X-trade
  cutover."
- **§9c attribution discipline.** Before claiming "channel X has higher win
  rate," verify the data path: did inbound rows from channel X actually
  reach `paper_trade_id`, or were they gate-blocked upstream? `linkage_state
  != 'linked'` rows must be excluded (or counted separately) in any
  per-source ranking.
- **§7a drift surprise.** TG linkage is already 100% present at the
  structural layer. A future PR that "adds" `paper_trade_id` to
  `tg_social_signals` is redundant; the work is the linkage_state column,
  views, and API surface.

## Open Questions

1. Trade size convention for cross-source PnL comparison. Today PAPER
   amount is $1000 default, with TG variants
   (`PAPER_TG_SOCIAL_TRADE_AMOUNT_USD`,
   `PAPER_TG_SOCIAL_CASHTAG_TRADE_AMOUNT_USD`). The user mentioned "$300
   P&L" — interpret as `pnl_usd` size-normalized? Or fixed $300 cohort? If
   the latter, this is a separate calibration question, not a linkage
   design question.
2. 1-to-many linkage: can one inbound event open more than one paper_trade
   (e.g., narrative + actionability spawning separate rows)? If yes, Option
   B (separate linkage table) becomes the right choice.
3. `narrative_operator_alerts` table: should we add a row per outbound
   operator alert (so `alert_id` becomes a distinct entity from
   `narrative_alerts_inbound.id`)? Today only structured logs record it.

## Decision Recommendation

After the 24h actionability validation runbook is reviewed:

- If actionability gate clearly separates quality and the operator wants
  source-attribution next, implement Option A on both sides plus
  `v_unified_source_outcomes` and one paginated API endpoint as the MVP.
- If actionability data is inconclusive, do **not** implement source
  ranking yet — but it is still safe to implement the linkage (Option A)
  and backfill, because both are non-suppressing and produce data for
  future analysis.

No runtime change should be made from this design alone.
