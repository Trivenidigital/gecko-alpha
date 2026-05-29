**New primitives introduced:** alert-intent surface design (separate endpoint; no implementation in this PR); BL-NEW-TG-ALERT-OPERATOR-ACTION-TELEMETRY (filed as the prerequisite for live dispatch); BL-NEW-TG-ALERT-QUALIFICATION-DESIGN (this PR — design only, no dispatch).

# TG Alert Qualification — Design (Design-Only PR; No Dispatch)

**Status:** DESIGN ONLY. This PR ships no runtime change. No alerts are sent, no endpoints are added, no surfaces mutate. Deliverable is the design doc; build PR for the alert-intent surface is GATED on operator approval of the design AND on closing the UNKNOWN operator-action telemetry input (per findings doc §7).

**Findings basis:** `tasks/findings_tg_alert_qualification_baseline_2026_05_29.md` — soak gate cleared, current 14d sent volume ≈19.3/day vs operator-pinned 3-5/day target, operator-action baseline UNKNOWN.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Alert intent / triage queue | No Hermes skill owns gecko-alpha's per-signal alert qualification or its contract-firewall surface. | Build in-repo (when build PR is approved). |
| Operator-action telemetry | No Hermes skill exposes Telegram client-side action callbacks suitable for gecko-alpha's scope. | File `BL-NEW-TG-ALERT-OPERATOR-ACTION-TELEMETRY` as separate work. |

Awesome-hermes ecosystem: dashboard plugins exist but none own gecko-alpha's TG dispatch path or scarcity logic. Custom repo-local design warranted.

## Operator-Pinned Constraints (mandatory; enforce via reviewer checklist)

1. **Soak-clear is eligibility only, not approval to send alerts.** Build PR for live dispatch is separately gated.
2. **Baseline current TG alert volume by surface over 14 days.** ✓ Captured in findings doc §2.
3. **Explicitly label each PR #296 input as measured / unavailable / unknown.** ✓ Captured in findings doc §6.
4. **Default: missing inputs means blocked or dashboard-only, NOT alert launch.** Enforced via design's default-state below.
5. **Pin scarcity target: 3-5/day max.** ✓ Pinned at finding §5. Build PR must demonstrate compression.
6. **Include both corpora: paper-backed + tracker-promoted.** Design covers both; tracker-corpus alerts are GATED on a separate decision (currently 0 alerts/day per finding §3).
7. **NO urgency tiers, NO TRADE_NOW, NO ranking, NO live alert dispatch in this PR.** Design specifies the alert-intent surface; runtime dispatch is deferred to a separate build PR.
8. **Propose a separate future endpoint/surface for alert intent, NOT mutating `/api/trade_inbox` or `/api/todays_focus`.** Honored by Section "Alert-Intent Surface Shape" below.

## Alert-Intent Surface Shape (proposed)

The eventual build PR (separately gated) would introduce a new endpoint:

### `/api/alert_intent` (READ-ONLY; visibility-only; not-for-dispatch)

```json
{
  "meta": {
    "read_only": true,
    "not_trade_advice": true,
    "visibility_only": true,
    "not_for_alerting": true,
    "not_for_execution": true,
    "not_for_sizing": true,
    "not_for_source_ranking": true,
    "scarcity_target_per_day": 5,
    "scarcity_window_hours": 24,
    "generated_at": "<iso>",
    "source_endpoints": ["/api/trade_inbox", "/api/todays_focus"],
    "operator_action_telemetry_available": false,
    "default_dispatch_blocked": true,
    "rows_returned": <int>,
    "eligible_rows_considered": <int>,
    "empty_state": "<factual>"
  },
  "rows": [
    {
      "row_key": "<source>:<token_id>",
      "token_id": "<str>",
      "symbol": "<str>",
      "source_corpus": "paper" | "tracker",
      "qualifying_signal_type": "<str>",
      "qualifying_reasons": ["<factual reason 1>", ...],
      "would_dispatch_alert": false,
      "dispatch_blocked_reason": "operator_action_telemetry_unavailable"
    }
  ]
}
```

Key shape decisions and their rationale:

- **`not_for_alerting: true` meta flag**: the alert-intent endpoint MUST advertise itself as visibility-only. Dispatch happens (when the future telemetry PR closes the UNKNOWN gap) via a SEPARATE Telegram-dispatch endpoint or background job, never directly via this endpoint.
- **`default_dispatch_blocked: true` meta flag**: while operator-action telemetry is UNKNOWN, every row's `would_dispatch_alert` field is `false` AND `dispatch_blocked_reason` is set. The build PR cannot ship with a payload that has `would_dispatch_alert: true` until the telemetry gap closes.
- **`scarcity_target_per_day: 5`**: pinned by operator. The build PR's curation rule must ensure that, even with all inputs measured, `rows_returned` does not exceed this target over a 24h window.
- **`qualifying_signal_type` + `qualifying_reasons`**: factual per-row reason copy, same factual-only contract-firewall discipline as Today's Focus.
- **`source_corpus: paper | tracker`**: both corpora covered by the surface, but the build PR's curation rule may default to paper-only for the first iteration (operator decides at build time).
- **No `urgency`, `priority`, `rank`, `score`, `trade_now`, `act_now`, `tier` fields**: forbidden per the contract firewall (already in BANNED_PATTERNS / FORBIDDEN_KEYS).

### Why NOT mutate `/api/trade_inbox` or `/api/todays_focus`

- `/api/trade_inbox` is the cohort source for Today's Focus and was scoped (PR #273) as a per-token review queue. Adding alert-dispatch semantics here re-uses a surface whose anti-scope explicitly bans urgency/dispatch.
- `/api/todays_focus` is a scarce 5-row review queue with contract-firewall invariants (`not_for_alerting: true`). Mutating its meta or adding row fields would re-introduce the Pydantic envelope tension PR-C's hotfix series surfaced.
- A separate `/api/alert_intent` endpoint has its own contract firewall, its own response shape, and (per the new FastAPI wire-shape memory) ships with NO `response_model` to avoid envelope drift.

## Build-PR Anti-Scope (forward-binding constraint on the next PR)

The eventual build PR for `/api/alert_intent` MUST honor (each enforced via dedicated contract firewall + JSX/component anti-scope):

1. NO urgency tiers (no `TRADE_NOW`, `WATCH_BREAKOUT`, `RESEARCH_ONLY`, etc.).
2. NO ranking (no `score`, `rank`, `priority`, `top_pick`, `sort_key`).
3. NO interpretive copy (no `act now`, `consider`, `should`, `buy`, `sell`, `watch breakout`).
4. NO Telegram dispatch from `/api/alert_intent` itself — the endpoint is visibility-only.
5. NO `response_model` on the route (per memory `feedback_fastapi_wire_shape_reviewer_pattern`).
6. Default `would_dispatch_alert: false` for every row until operator-action telemetry exists.
7. Scarcity-target compression demonstrated against the 19.3/day baseline before any `would_dispatch_alert: true` is permitted in any row.

### Wire-shape binding (forecloses the PR-C 3-hotfix pattern explicitly)

The PR-C sparkline series required 3 hotfix PRs (#315/#316/#317) because the response envelope changed field semantics. The build PR MUST name and bind the three specific failure modes:

8. **Absence-vs-null binding.** Optional row fields (`dispatch_blocked_reason`, and any other field whose semantic is "absent = green-state"):
   - When present, the field carries factual value.
   - When green-state, the field MUST be **absent** (key not in row), NOT serialized as `null`.
   - Contract firewall asserts via `"dispatch_blocked_reason" not in row` when `row["would_dispatch_alert"] is True`.
   - Conversely, `dispatch_blocked_reason` MUST be present (and a known enum string) when `would_dispatch_alert is False`.

9. **Identity-True / Identity-False on booleans.** All boolean fields (`would_dispatch_alert`, `default_dispatch_blocked`, `operator_action_telemetry_available`, and any meta `not_for_*` flag):
   - MUST be exactly Python `True` or `False` literals on the server.
   - Contract firewall MUST use `is True` / `is False` identity checks, NOT truthiness. Reject `1`, `0`, `"true"`, `"false"`, `1.0`, `None`.
   - The check pattern mirrors `_check_sparkline_meta_flag` and `_check_market_benchmarks` from existing firewall.

10. **Numeric type fidelity.** Integer-semantic fields (`scarcity_target_per_day`, `scarcity_window_hours`, `rows_returned`, etc.):
    - MUST be Python `int` (not `float`). Pydantic `list[list[float]]`-style coercion is BANNED.
    - Contract firewall asserts `type(value) is int` for integer-semantic fields (NOT `isinstance(value, (int, float))` which would accept the float-coerced wire shape).
    - Floats are permitted only for inherently fractional fields (e.g., per-coin deltas like `btc_4h_pct`).

11. **No new fields added to `TodaysFocusMeta` or any other existing Pydantic model.** The alert-intent surface MUST be a NEW Pydantic model (if any) — and even then, per §5 above, ideally none.

12. **No re-decoration of `/api/todays_focus` or `/api/trade_inbox` with `response_model=...`.** PR #317 removed this; future PRs MUST NOT re-add it.

## Migration Plan (from current `tg_alert_log` dispatch to design)

The current dispatch path (`scout/alerter.py` + per-signal call sites) is NOT touched in this design. Migration is a deferred decision:

- **Option A** (recommended): build the new `/api/alert_intent` surface first as dashboard-only visibility. Once operator-action telemetry exists AND scarcity compression demonstrated, a SEPARATE dispatch-replacement PR retires the legacy paths.
- **Option B**: dual-run — keep legacy dispatch active while the new surface is observed. Risk: doubled alert volume during dual-run; ban this by default.
- **Option C**: gradual cutover per-signal — disable `narrative_prediction` legacy dispatch, route through new surface, observe; repeat per signal. Operationally complex; deferred.

Build PR's plan-doc must explicitly state which option is chosen and what gates it.

## Prerequisites for Build PR

The build PR (`/api/alert_intent` endpoint + dashboard surface) is gated on:

1. **`BL-NEW-TG-ALERT-OPERATOR-ACTION-TELEMETRY` shipped.** Without this, `default_dispatch_blocked: true` is permanent; the build PR's purpose evaporates.
2. **Scarcity compression algorithm pinned** in the build PR's plan doc: which signals, which gates, which dedup window. Must demonstrate ≤5/day on backtest against the 14-day baseline.
3. **Per-corpus decision pinned with concrete criterion**: paper-only first OR paper+tracker simultaneously. Pin via a measurable criterion in the build PR's plan-doc (e.g., "paper-only iff tracker-corpus alerts-per-day projected ≥1 would consume scarcity budget paper needs"). Operator approval before build.
4. **Contract firewall surface added** for `/api/alert_intent` (parallel structure to `check_todays_focus_contract.py`).
5. **All anti-scope items above re-validated** at build-PR plan-review phase.

## Scope of THIS PR

This PR delivers:

1. `tasks/findings_tg_alert_qualification_baseline_2026_05_29.md` — measurements ✓
2. `tasks/design_tg_alert_qualification_2026_05_29.md` — this document ✓
3. Backlog status updates in `tasks/todo.md`:
   - `BL-NEW-TG-ALERT-QUALIFICATION-DESIGN` → SHIPPED (this PR's deliverable)
   - `BL-NEW-TG-ALERT-OPERATOR-ACTION-TELEMETRY` → PROPOSED (new, gates the build PR)
   - `BL-NEW-TG-ALERT-INTENT-SURFACE-BUILD` → PROPOSED (the actual build PR, gated on above)

**No runtime changes. No endpoints. No alert dispatch. No telemetry. No tests beyond docs verification.**

## Anti-Scope (this PR — docs only)

1. NO new code, NO new endpoints, NO new tests beyond the diff being committed.
2. NO changes to `scout/alerter.py` or any per-signal dispatch path.
3. NO changes to `/api/trade_inbox` or `/api/todays_focus` contracts.
4. NO changes to existing Pydantic models or response_model decorations.
5. NO new BANNED_PATTERNS entries (no copy is being shipped).
6. NO Telegram dispatch path additions.
7. NO build approval for the alert-intent surface — that requires operator review + telemetry prerequisite closure.

## Merge Gate

This docs-only PR merges when:
1. CI green.
2. Both PR reviewers (anti-scope + wire-shape-discipline-on-future-build) return zero blocking findings.
3. Operator approves the design at PR-review.

Smoke after deploy:
- No live behavior change. The findings + design docs are committed; backlog reflects new gates.
