**New primitives introduced:** `/api/trade_inbox` read-only grouped opportunity endpoint; `TradeInboxTab.jsx` trader triage UI; trade-window labels (`open`, `closing`, `late`, `closed`, `unknown`); deterministic `trade_score`; client-local inbox state (`new`, `seen_this_session`, `changed_group`, dismissed-until-reload).

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Trader triage dashboard UI | none found in Hermes Skills Hub that can consume gecko-alpha paper-trade DB rows and return grouped operator queues | build project-native UI using existing dashboard patterns |
| Crypto trading/market data | yes, Hermes ecosystem has exchange/market-data skills, but this feature must be read-only over local paper-trade state and must not add execution/vendor calls | do not integrate |
| Agent alerting / workflow automation | Hermes supports automation primitives, but this PR is a dashboard visibility surface only | defer alerts/snooze persistence to a separate operator-gated PR |

Awesome-hermes-agent ecosystem check: no drop-in primitive covers gecko-alpha's durable open paper trades, local price cache, `/api/live_candidates` semantics, and dashboard contract. Verdict: KEEP_CUSTOM and reuse existing local cockpit data shaping.

# Trade Opportunity Inbox Design

## Problem

The current dashboard can prove early detection after the fact, but it does not present a trader's next inspection queue. A TOES-shaped token can be detected early and still be hard to use because the operator sees a flat evidence table sorted around historical gain or live-candidate verdicts, not an action queue.

## Design Goals

- Surface a short read-only queue with trader-facing labels `Review Now`, `Watch`, `Moved Already`, `Blocked` while keeping machine keys `act_now`, `watch`, `already_ran`, `blocked`.
- Avoid the live-candidates page-limit blind spot by grouping after broad cohort scoring.
- Avoid the second burial mode by exposing per-group overflow counts and a show-more path when a group has more rows than the initial display limit.
- Preserve auditability back to paper trades through IDs, actionability flags, inclusion reasons, and risk reasons.
- Use explicit, deterministic scoring and tie-breaks so UI ordering is explainable.
- Make the desk workflow usable with client-local `new`, `seen`, and `changed_group` states without adding DB writes.

## Backend Shape

Add `TradeInbox*` models in `dashboard/models.py` near the existing `LiveCandidate*` models.

`TradeInboxRow` fields:

- Identity: `token_id`, `symbol`, `name`, `chain`
- Audit: `open_trade_ids`, `recent_trade_ids`, `actionable`, `would_be_live`, `inclusion_reasons`, `risk_reasons`, `surfaces`
- Triage: `group`, `action_label`, `window_state`, `trade_score`, `sort_key`, `why_now`, `block_reason_primary`
- Market: `opened_at`, `opened_age_hours`, `pct_from_entry`, `price_change_24h`, `market_cap`, `current_price`, `entry_quality`, `verdict`, `price_updated_at`, `price_is_stale`, `price_staleness_minutes`

Use `Field(default_factory=list)` for all list fields. The response envelope is:

```python
{
    "meta": {
        "read_only": true,
        "not_trade_advice": true,
        "experimental": true,
        "generated_at": "...",
        "window_hours": 36,
        "limit_per_group": 10,
        "rows_returned": 12,
        "source_limit": 500,
        "source_rows_considered": 120,
        "open_trades_scanned": 400,
        "source_truncated": false,
        "group_counts": {"act_now": 2, "watch": 6, "already_ran": 3, "blocked": 1},
        "group_hidden_counts": {"act_now": 0, "watch": 0, "already_ran": 0, "blocked": 0},
        "block_reason_counts": {},
        "stale_warning_count": 0,
        "hard_stale_count": 0,
        "source": "live_candidates"
    },
    "groups": {"act_now": [], "watch": [], "already_ran": [], "blocked": []}
}
```

## Shared Candidate Builder

Refactor `dashboard/db.py:get_live_candidates` into:

- `_build_live_candidate_rows(db_path, limit, window_hours, now=None) -> dict`
- `get_live_candidates(...)` keeps the current public behavior: global page limit and existing sort contract.
- `get_trade_inbox(...)` calls the builder with a deterministic `source_limit = max(500, limit_per_group * 40)`, scores/groups all returned rows, then slices each group to `limit_per_group` for the initial payload.
- If the source scan reaches `source_limit` while more open trades exist, return `source_truncated=true`; the UI must warn that the inbox is incomplete.
- Group counts are pre-slice counts. `group_hidden_counts[group] = max(0, group_count - len(groups[group]))`.

The shared builder must preserve existing `/api/live_candidates` behavior. Existing tests remain the regression guard.

Builder counters:

- `open_trades_scanned`: raw open `paper_trades` rows scanned from SQLite before token de-duplication.
- `source_rows_considered`: unique token rows enriched and scored by the inbox before group slicing.
- `rows_returned`: total rows returned across visible sliced groups.
- `source_limit`: requested unique-token source cap for the inbox.
- `source_truncated`: true when the available unique-token cohort reaches `source_limit`, meaning older open trades may not have been considered.

Builder signature:

```python
async def _build_live_candidate_rows(
    db_path: str,
    *,
    source_token_limit: int,
    scan_cap: int,
    window_hours: int,
    now: datetime | None = None,
) -> dict:
    ...
```

`get_live_candidates` calls it with `source_token_limit=limit` and current `scan_cap=max(limit * 20, 400)`. `get_trade_inbox` calls it with `source_token_limit=max(500, limit_per_group * 40)` and `scan_cap=max(source_token_limit * 20, 400)`.

## Classification

`_trade_window_state(row)` uses `pct_from_entry` only after data is present:

- `current_price is None` or `pct_from_entry is None`: `unknown`
- `< -10`: `closed`
- `<= 8`: `open`
- `<= 25`: `closing`
- otherwise: `late`

Hard block routing. The first matching hard block in this order becomes `block_reason_primary`:

- no price or missing pct: `NO_PRICE`
- hard-stale price at `>= 120` minutes: `STALE_PRICE`
- `actionable == 0`: `NOT_ACTIONABLE`
- unparseable `opened_at`: `BAD_TIMESTAMP`
- `verdict == data_insufficient`: `DATA_INSUFFICIENT`

Stale boundaries are inbox-local and explicit: `price_staleness_minutes >= 60` is a warning; `price_staleness_minutes >= 120` is hard stale. Boundary tests cover exactly 60 and exactly 120 minutes.

Old-low-movement predicate:

```python
is_old_low_movement = opened_age_hours > window_hours and abs(pct_from_entry or 0) <= 8
```

Group routing:

- `blocked`: any hard block above.
- `already_ran`: `late` or `closed` and not blocked.
- `act_now`: `candidate_review`, fresh price, `open` or `closing`, and not old-low-movement beyond `window_hours`.
- `watch`: all remaining rows, including stale-warning rows from 60 to 119 minutes.

## Scoring

The numeric score is a display summary. Sorting uses an explicit tuple.

| Factor | Points |
|---|---:|
| `verdict == candidate_review` | +35 |
| `actionable == 1` | +25 |
| `would_be_live == 1` | +10 |
| `entry_quality == fresh_entry` | +15 |
| `entry_quality == acceptable_pullback` | +8 |
| `window_state == open` | +15 |
| `window_state == closing` | +6 |
| price fresh under 60 minutes | +8 |
| each extra surface, max 3 extras | +2 |
| positive 24h momentum | `min(10, price_change_24h / 5)` |
| stale warning | -12 |
| each risk reason, max 5 | -3 |
| `window_state == late` | -35 |
| `window_state == closed` | -50 |

Score pseudocode:

```python
score = 0.0
if row["verdict"] == "candidate_review": score += 35
if row["actionable"] == 1: score += 25
if row["would_be_live"] == 1: score += 10
if row["entry_quality"] == "fresh_entry": score += 15
elif row["entry_quality"] == "acceptable_pullback": score += 8
if window_state == "open": score += 15
elif window_state == "closing": score += 6
if price_staleness_minutes is not None and price_staleness_minutes < 60: score += 8
extra_surfaces = max(0, len(set(row.get("surfaces") or [])) - 1)
score += min(3, extra_surfaces) * 2
momentum = row.get("price_change_24h")
if momentum is not None and momentum > 0: score += min(10, momentum / 5)
if 60 <= price_staleness_minutes < 120: score -= 12
score -= min(5, len(set(row.get("risk_reasons") or []))) * 3
if window_state == "late": score -= 35
elif window_state == "closed": score -= 50
score = round(max(0, min(100, score)), 1)
```

Sort within each group by:

1. `window_rank`: open before closing before unknown before late before closed.
2. `trade_score DESC`
3. `opened_at DESC`, nulls last
4. `token_id ASC`

`sort_key` is returned as a JSON-safe list for contract tests:

```python
[window_rank, -trade_score, opened_missing_rank, -opened_epoch_seconds_or_0, token_id]
```

`opened_missing_rank` is `1` for null/unparseable timestamps and `0` otherwise.

`why_now` is deterministic and low-noise: include at most six strings from one leading priority reason (`open_window`, `closing_window`, `stale_warning`, `multi_surface`, `fresh_price`) plus `window=<state>`, `fresh_entry`, `acceptable_pullback`, `actionable=1`, `would_be_live=1`, `price_fresh`, `price_stale_warning`, `momentum_24h_positive`, and `surfaces=<n>`.

## API

Add near `/api/live_candidates`:

```python
@app.get("/api/trade_inbox", response_model=TradeInboxResponse)
async def get_trade_inbox(
    limit_per_group: int = Query(10, ge=1, le=100),
    window_hours: int = Query(36, ge=6, le=72),
):
    return await db.get_trade_inbox(
        _db_path, limit_per_group=limit_per_group, window_hours=window_hours
    )
```

This endpoint is read-only: no inserts, updates, deletes, execution calls, pruning, alerting, or config mutation.

## Frontend

Add `dashboard/frontend/components/TradeInboxTab.jsx` and wire `trade_inbox` in `App.jsx` near the Trading / Now Tradable tabs.

Layout:

- Header band with `Trade Inbox`, read-only/not-advice flags, last refresh, source rows considered, and manual refresh.
- Compact controls: pause/resume auto-refresh and clear dismissed rows.
- Four sections in order: `Review Now`, `Watch`, `Moved Already`, `Blocked`.
- Each section shows pre-slice count, hidden count, and empty state.
- If a section has hidden rows, show a compact `Show more` control that increases the client query limit for the next fetch, capped by the API max.
- If `source_truncated=true`, show a top warning that older open trades may not have been considered.
- Rows are compact one-line primary facts with capped reason chips and an expandable details row for full audit fields.
- Rows show token link, action badge (`Inspect`, `Monitor`, `Moved Already`, `Blocked`), window badge, new/changed/seen badge, score, from-entry pct, 24h pct, mcap, sources, and top `why_now` / risk text.

Client-local state:

- On first load, all returned row keys are `new`.
- Keep `new` until the next successful poll while the tab is visible or until explicit row interaction/clear; do not erase novelty just because the component rendered in the background.
- If a token's group changes, show `changed_group`.
- Track `first_seen_at`, `last_seen_group`, and `last_seen_score` in session state.
- Dismissed rows are hidden for the browser session; show dismissed count and a restore control. Never write dismissals to the backend.

Fetch behavior:

- Poll `/api/trade_inbox?limit_per_group=10&window_hours=36` every 30 seconds unless paused.
- Show loading, fetch error, stale response warning, last refresh timestamp, diagnostic empty states, and zero-review-now state.
- Diagnostic empty states use `block_reason_counts`, `stale_warning_count`, `hard_stale_count`, `source_truncated`, and `open_trades_scanned` to distinguish no open trades, no reviewable rows, stale price coverage, source truncation, and backend/API failure.

## Tests

Create `tests/test_trade_inbox_endpoint.py` using the existing ASGI client fixture pattern:

- shape/meta/read-only flags and all four group keys.
- TOES-shaped broad-cohort regression: older/lower raw row still appears in `act_now`.
- high-volume overflow regression: if a TOES-shaped row would be beyond the initial per-group display limit, the response exposes `group_hidden_counts.act_now > 0` and the frontend exposes a show-more path rather than silently hiding overflow.
- exact score and deterministic tie order.
- exact emitted `sort_key` equality, including null timestamp ordering.
- stale/missing data routing: no price, bad timestamp, stale 90m, stale 3h.
- stale boundary routing at exactly 60 minutes and exactly 120 minutes.
- audit fields preserved.
- source truncation meta and diagnostic counts.
- read-only safety: table row counts/selected values unchanged after endpoint call and monkeypatch write DB helpers to fail if reached.

Update `tests/test_dashboard_frontend_layout.py`:

- `TradeInboxTab.jsx` exists.
- `App.jsx` imports and renders `trade_inbox`.
- `TradeInboxTab.jsx` fetches `/api/trade_inbox`.

Existing `tests/test_live_candidates_endpoint.py` must continue to pass to prove the shared builder did not regress the old endpoint.

## Verification

Run:

```powershell
C:/projects/gecko-alpha/.venv/Scripts/python.exe -m pytest -q tests/test_trade_inbox_endpoint.py tests/test_live_candidates_endpoint.py
C:/projects/gecko-alpha/.venv/Scripts/python.exe -m pytest -q tests/test_dashboard_frontend_layout.py
C:/projects/gecko-alpha/.venv/Scripts/python.exe -m pytest --tb=short -q
cd dashboard/frontend
npm.cmd run build:codex
git diff --check origin/master..HEAD
```

## Rollback

Remove the new route import/model, delete `TradeInboxTab.jsx`, remove the tab wiring, and revert the shared-builder extraction. No DB/schema/runtime state cleanup is required because V1 has no writes.
