**New primitives introduced:** `scripts/check_trade_inbox_contract.py` runtime/CI validator for `/api/trade_inbox`; additive Trade Inbox contract tests. No new DB table, no alerting primitive, no execution primitive, no ranking primitive.

# Trade Inbox Contract Firewall Design

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Dashboard API contract validation | None found for gecko-alpha's local Trade Inbox envelope and row semantics. | Build a repo-local pure-stdlib validator. |
| Entity matching / identity resolution | Not applicable after runtime baseline found zero current cross-id candidates. | Do not build resolver in this branch. |
| Alert qualification / urgency classification | Not applicable. | Defer until tracker-promotion soak gate and a separate design. |

Awesome-hermes-agent ecosystem check: no ecosystem skill replaces a local contract firewall over `TradeInboxResponse`.

Verdict: custom in-repo validator, modeled after `check_live_candidates_contract.py`, with no Hermes runtime dependency.

## Review Fold

| Reviewer finding | Fold |
|---|---|
| Validator too permissive without closed schema | Define exact top-level, meta, group, and row key sets. Unknown keys are CRITICAL. |
| Paper/tracker invariants incomplete | Add source-corpus bijections for `paper` and `tracker` rows. |
| Counter semantics under-pinned | Validate aggregate counter math over returned groups and full source counts. |
| Urgency/alert/ranking can be smuggled through fields | Add recursive field-name firewall for ranking, recommendation, urgency, and alert vocabulary. |
| Banned-language false positives likely without exemptions | Define `SCAN_EXEMPT_STRING_FIELDS` for identifiers and closed enums. |
| Endpoint coverage should span several fixtures | Import validator into multiple existing Trade Inbox endpoint tests. |

## Contract

### Top-Level

The only valid top-level keys are:

```python
{"meta", "groups"}
```

`groups` keys must be exactly:

```python
{"act_now", "watch", "already_ran", "blocked"}
```

Every row inside a group must have `row["group"] == group_name`.

### Meta Keys

The validator pins the current `TradeInboxMeta` public contract:

```python
EXPECTED_META_KEYS = {
    "read_only",
    "not_trade_advice",
    "experimental",
    "generated_at",
    "window_hours",
    "limit_per_group",
    "rows_returned",
    "source_limit",
    "source_rows_considered",
    "open_trades_scanned",
    "paper_rows_considered",
    "tracker_rows_considered",
    "tracker_rows_promoted",
    "tracker_source_truncated",
    "source_truncated",
    "group_counts",
    "group_hidden_counts",
    "block_reason_counts",
    "stale_warning_count",
    "hard_stale_count",
    "source",
}
```

`limit` is not accepted. This catches copy/paste drift from `/api/live_candidates`.

Required meta invariants:

- `read_only is True`
- `not_trade_advice is True`
- `experimental is True`
- `generated_at` is parseable ISO timestamp
- integer counters are non-bool integers >= 0
- `rows_returned == sum(len(rows) for rows in groups.values())`
- `group_counts.keys() == groups.keys()`
- `group_hidden_counts.keys() == groups.keys()`
- `group_hidden_counts[group] == group_counts[group] - len(groups[group])`
- `sum(group_counts.values()) == source_rows_considered`
- `sum(group_hidden_counts.values()) == source_rows_considered - rows_returned`
- `paper_rows_considered + tracker_rows_promoted == source_rows_considered`
- `tracker_rows_promoted >= returned_tracker_rows`
- `tracker_rows_considered >= tracker_rows_promoted`
- `open_trades_scanned >= paper_rows_considered`
- `paper_rows_considered >= returned_paper_rows`
- `source == "live_candidates"` until a coordinated schema bump changes the producer and validator together.

### Row Keys

The validator pins the current `TradeInboxRow` public contract:

```python
EXPECTED_ROW_KEYS = {
    "token_id",
    "symbol",
    "name",
    "chain",
    "source_corpus",
    "group",
    "action_label",
    "window_state",
    "trade_score",
    "sort_key",
    "why_now",
    "inclusion_reasons",
    "risk_reasons",
    "surfaces",
    "open_trade_ids",
    "recent_trade_ids",
    "actionable",
    "would_be_live",
    "block_reason_primary",
    "opened_at",
    "opened_age_hours",
    "pct_from_entry",
    "price_change_24h",
    "market_cap",
    "current_price",
    "entry_quality",
    "verdict",
    "price_updated_at",
    "price_is_stale",
    "price_staleness_minutes",
}
```

Closed sets:

- `source_corpus`: `paper`, `tracker`
- `group`: `act_now`, `watch`, `already_ran`, `blocked`
- `action_label`: `REVIEW_NOW`, `WATCH_PULLBACK`, `TOO_LATE`, `BLOCKED`, `DATA_MISSING`
- `window_state`: `open`, `closing`, `late`, `closed`, `unknown`
- `entry_quality`: `fresh_entry`, `acceptable_pullback`, `already_faded`, `already_ran`, `too_stale`, `data_insufficient`, or `None`
- `verdict`: `candidate_review`, `watch`, `blocked`, `data_insufficient`, or `None`

Type matrix:

- non-empty strings: `token_id`, `source_corpus`, `group`, `action_label`, `window_state`
- nullable strings: `symbol`, `name`, `chain`, `block_reason_primary`, `opened_at`, `price_updated_at`, `entry_quality`, `verdict`
- numeric or null, with bool rejected: `trade_score`, `opened_age_hours`, `pct_from_entry`, `price_change_24h`, `market_cap`, `current_price`, `price_staleness_minutes`
- bool only: `price_is_stale`
- integer lists, with bool rejected: `open_trade_ids`, `recent_trade_ids`
- string lists: `why_now`, `inclusion_reasons`, `risk_reasons`, `surfaces`
- scalar sort-key list: `sort_key` items must be `str|int|float`, with bool rejected
- nullable ints/bools constrained by existing model semantics: `actionable`, `would_be_live` must be `0`, `1`, or `None`
- timestamps: `opened_at` and `price_updated_at`, when non-null, must parse as ISO8601.

Response-level identity invariants:

- no duplicate `(source_corpus, token_id)` rows;
- no duplicate `token_id` rows across the returned payload;
- if a token appears as paper, no returned tracker row may share that token id.

### Source-Corpus Bijections

Paper row invariants:

- `source_corpus == "paper"`
- `open_trade_ids` is a non-empty list of ints
- `inclusion_reasons` includes `open_paper_trade`
- `risk_reasons` does not include `tracker_only_no_paper_trade`

Tracker row invariants:

- `source_corpus == "tracker"`
- `open_trade_ids == []`
- `recent_trade_ids == []`
- `surfaces` includes `top_gainers_tracker`
- `inclusion_reasons` includes both `tracker_promotion` and `top_gainers_tracker`
- `risk_reasons` includes `tracker_only_no_paper_trade`
- `actionable is None`
- `would_be_live is None`
- enclosing group is not `act_now`

These checks prevent a future producer from quietly relabeling tracker-only rows as paper rows or making tracker-only rows look executable.

## Field Firewalls

Unknown keys are already CRITICAL. The field-name firewall is defense in depth and runs recursively over every dict key, including nested list items:

- KOL/source ranking: `kol_rank`, `source_score`, `caller_weight`, `channel_trust`, `tweet_score`, `recommended*`, `top_pick*`, `weighted_by_kol`, etc.
- Urgency/alert leakage: `urgency*`, `priority*`, `alert*`, `notify*`, `operator_action`, `recommended_action`, `trade_now`, `watch_breakout`, `research_only`, `signal_to_send`.

String scan:

- Banned imperative/hype phrases are copied from the live-candidates checker shape.
- Exempt identifier/enum/timestamp fields include `token_id`, `symbol`, `name`, `chain`, `source_corpus`, `group`, `action_label`, `window_state`, `entry_quality`, `verdict`, `opened_at`, `price_updated_at`, `generated_at`, `source`, and `surfaces`.
- The scan still applies to `why_now`, `inclusion_reasons`, `risk_reasons`, and other non-exempt prose-like values.
- Ranking/urgency/alert vocabulary is also rejected in non-exempt string values. A producer must not smuggle `kol_rank_high`, `operator_priority_high`, `notify_candidate`, `recommended_by_kol`, `trade_now`, or equivalent semantics into `why_now`, `inclusion_reasons`, `risk_reasons`, `surfaces`, or nested prose values.

## CLI

`scripts/check_trade_inbox_contract.py` supports:

```bash
python scripts/check_trade_inbox_contract.py --url http://localhost:8000
python scripts/check_trade_inbox_contract.py --url http://localhost:8000 --limit-per-group 20 --window-hours 36
python scripts/check_trade_inbox_contract.py --url http://localhost:8000 --json
python scripts/check_trade_inbox_contract.py --url http://localhost:8000 --verbose
```

The HTTP target is `GET {url}/api/trade_inbox?limit_per_group={limit_per_group}&window_hours={window_hours}`. Defaults are `20` and `36`, with validator checks that `meta.limit_per_group` and `meta.window_hours` echo the request.

Exit codes mirror the live-candidates checker:

- `0`: all CRITICAL checks pass
- `1`: at least one CRITICAL failure
- `2`: HTTP error
- `3`: JSON parse error
- `4`: argparse/config error

## Tests

`tests/test_check_trade_inbox_contract.py` covers:

- clean mixed paper/tracker payload
- empty rows envelope
- missing/extra top-level, meta, group, and row keys
- `meta.limit` rejected and `limit_per_group` required
- bad group keys and row/group mismatch
- invalid closed-set values
- paper row mislabeled without open trade ids
- tracker row with paper ids/actionability or in `act_now`
- tracker row missing `tracker_only_no_paper_trade`
- duplicate `(source_corpus, token_id)` and duplicate `token_id` rows
- row type violations, including bool-as-int, non-scalar sort keys, malformed timestamps, and non-string list items
- counter math mismatch
- impossible source counters such as `tracker_rows_promoted > tracker_rows_considered` and `paper_rows_considered > open_trades_scanned`
- ranking/source/KOL field-name firewall, including nested fields
- urgency/alert field-name firewall, including nested fields
- ranking/urgency/alert vocabulary in non-exempt string values
- banned language in prose fields
- legitimate identifier strings such as token/name containing `moon` do not fail

`tests/test_trade_inbox_endpoint.py` imports the validator and asserts generated payloads are clean in representative fixtures:

- shape/read-only mixed groups
- tracker-only promotion
- tracker data-missing row
- source overflow/truncation fixture

## Deployment Smoke

After merge/deploy:

```bash
cd /root/gecko-alpha
.venv/bin/python scripts/check_trade_inbox_contract.py --url http://localhost:8000 --verbose
```

Acceptance: exit 0 with no CRITICAL failures.

## Anti-Scope

This branch must not modify `dashboard/db.py` cross-id dedupe behavior. If duplicate-looking rows appear during verification, the output is a finding/backlog update, not a resolver implementation. Producer fixes are allowed only for same-id/source-corpus contract violations exposed by this validator. The branch must not introduce fields or code paths named `resolved_coin_id`, `identity_resolver`, `cross_id`, or similar resolver behavior. Cross-identifier resolver remains runtime-gated until a real candidate cohort exists.
