**New primitives introduced:** `scripts/check_trade_inbox_contract.py` runtime/CI validator for `/api/trade_inbox`; additive Trade Inbox contract tests. No new DB table, no alerting primitive, no execution primitive, no ranking primitive.

# Trade Inbox Contract Firewall Plan

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Dashboard API contract validation | None found for validating gecko-alpha's local FastAPI/SQLite Trade Inbox envelope. | Build a small repo-local validator, mirroring `scripts/check_live_candidates_contract.py`. |
| Identity resolution / entity matching | No Hermes skill should be used for live token identity collapse without project data-path proof. | Do not build cross-id resolver now; prod baseline shows no current true cross-id duplicate cohort. |
| Alert qualification / urgency tiers | Not used. | Defer until tracker-promotion soak gate clears and design is pinned. |

Awesome-hermes-agent ecosystem check: no checked ecosystem primitive replaces a local validator over `TradeInboxResponse` semantics, group labels, source-corpus labels, and no-advice field firewalls.

Verdict: custom in-repo validation is warranted because this is a repo-specific public API contract.

## Runtime / Drift Findings

Cross-identifier resolver pre-work was run first, per `BL-NEW-CROSS-IDENTIFIER-RESOLVER-TRACKER-PAPER`:

- Prod `paper_trades.status='open'`: 144.
- Prod recent tracker universe, last 36h: 70 distinct `gainers_comparisons.coin_id`.
- Same-id paper/tracker overlap, last 36h: 10.
- Strict `symbol + name` open-paper/tracker overlap, last 36h: 10, but sample cross-id matches were empty.
- Symbol-only true cross-id candidates, last 36h: 0 raw pairs.

Decision: do not ship a cross-id resolver in this branch. It would have zero measured current duplicate reduction and would add false-merge risk. Keep `BL-NEW-CROSS-IDENTIFIER-RESOLVER-TRACKER-PAPER` as PROPOSED / runtime-gated until prod shows a real candidate cohort or a deterministic contract-to-CoinGecko mapping exists.

The ready residual from PR #281 is different: Trade Inbox row semantics are now operator-facing. `source_corpus`, group labels, tracker counters, and `top_gainers_tracker` provenance need a contract firewall before alert design or future dashboard refactors consume them.

## Scope

1. Add `scripts/check_trade_inbox_contract.py`, modeled after `scripts/check_live_candidates_contract.py`, with pure-stdlib HTTP fetch and `validate_payload(payload)` unit-test surface.
2. Validate the public `/api/trade_inbox` envelope:
   - exact top-level keys `meta` and `groups`;
   - exact `TradeInboxMeta` and `TradeInboxRow` key sets from `dashboard/models.py`;
   - `meta.read_only`, `meta.not_trade_advice`, and `meta.experimental` must be true;
   - `meta.limit_per_group` must exist; copied `meta.limit` / `?limit=` live-candidates semantics must fail tests;
   - `rows_returned` must equal returned rows across groups;
   - group keys must be exactly `act_now`, `watch`, `already_ran`, `blocked`;
   - `group_counts[group] >= len(groups[group])`;
   - `group_hidden_counts[group] == group_counts[group] - len(groups[group])`.
   - `sum(group_counts.values()) == source_rows_considered`;
   - `sum(group_hidden_counts.values()) == source_rows_considered - rows_returned`;
   - `paper_rows_considered + tracker_rows_promoted == source_rows_considered`;
   - `tracker_rows_promoted >= returned tracker rows`.
3. Validate row semantics:
   - every row has a valid `source_corpus` in `paper|tracker`;
   - paper rows must have non-empty `open_trade_ids`, include `open_paper_trade`, and must not carry `tracker_only_no_paper_trade`;
   - tracker rows must have no open/recent paper trade ids, must include `tracker_promotion` and `top_gainers_tracker`, must carry `tracker_only_no_paper_trade`, and must have `actionable is None` / `would_be_live is None`;
   - tracker rows must not be in `act_now`;
   - paper rows must not carry `tracker_only_no_paper_trade`;
   - row `group` must match the enclosing group;
   - `action_label`, `window_state`, and `entry_quality` must be closed sets.
4. Preserve no-advice / no-ranking boundaries:
   - recursively scan non-identifier prose for banned imperative/hype terms, using an explicit `SCAN_EXEMPT_STRING_FIELDS` set for identifiers and closed enums (`token_id`, `symbol`, `name`, `chain`, `source_corpus`, `group`, `action_label`, `window_state`, `entry_quality`, `verdict`, timestamp fields, etc.);
   - reject stealth KOL/source ranking fields such as `source_score`, `kol_rank`, `caller_weight`, and recommendation fields;
   - reject urgency/alert fields such as `urgency_tier`, `alert_level`, `operator_priority`, `recommended_action`, `trade_now`, `watch_breakout`, `research_only`, `notify`, and nested variants.
5. Add endpoint regression coverage proving generated `/api/trade_inbox` payloads pass the validator.
6. Add script unit tests for clean payloads, exact-key failures, tracker invariant failures, paper invariant failures, meta mismatch failures, banned language, false-positive exemptions for identifier fields, KOL/ranking/urgency/alert field firewall, and group-count mismatch.
7. Do not change Trade Inbox producer behavior unless tests expose an existing contract violation.

## Non-Scope

- No Telegram alert sends or TG alert qualification.
- No urgency tiers such as `TRADE_NOW`, `WATCH_BREAKOUT`, or `RESEARCH_ONLY`.
- No cross-identifier resolver implementation in this branch.
- No execution, sizing, paper-trade dispatch, signal enablement, or source pruning.
- No new durable pipeline table, so AGENTS §12a freshness watchdog does not apply.

## Files

- Create: `scripts/check_trade_inbox_contract.py`
- Create: `tests/test_check_trade_inbox_contract.py`
- Modify: `tests/test_trade_inbox_endpoint.py`
- Modify: `tasks/todo.md`

## Build Steps

- [ ] Write failing unit tests for the Trade Inbox contract validator.
- [ ] Implement `scripts/check_trade_inbox_contract.py` with `validate_payload`, CLI HTTP mode, JSON/verbose output, and deterministic exit codes.
- [ ] Run validator unit tests and confirm they pass.
- [ ] Add `/api/trade_inbox` endpoint tests that import the validator and assert generated payloads are clean for the shape/read-only fixture, tracker-only fixture, tracker data-missing fixture, and overflow/truncation metadata fixture.
- [ ] Run focused endpoint tests and fix producer issues only if the contract reveals real drift.
- [ ] Update `tasks/todo.md` with the cross-id no-build runtime finding and contract-firewall run record.
- [ ] Open PR and request two parallel reviews:
  - Product/contract vector: checks scope, no-advice, no-ranking, no urgency-tier leakage.
  - Code/API vector: checks validator correctness, false-positive risk, and endpoint contract coverage.

## Verification

- `uv run pytest -q tests/test_check_trade_inbox_contract.py tests/test_trade_inbox_endpoint.py`
- `python scripts/check_trade_inbox_contract.py --url http://localhost:8000 --verbose` against the deployed dashboard after merge/deploy.
