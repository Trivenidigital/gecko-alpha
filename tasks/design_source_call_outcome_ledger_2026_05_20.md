**New primitives introduced:** `source_calls` SQLite table; `scout/source_quality/` backfill/outcome/summary helpers; `Database._migrate_source_calls_v1`; read-only source-call ledger lag watchdog.

# Design: BL-NEW-SOURCE-CALL-OUTCOME-LEDGER

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| X/KOL collection | Yes. Installed stock `social-media/xurl` plus gecko-alpha-owned `kol_watcher`, `narrative_classifier`, `narrative_alert_dispatcher`, `coin_resolver`, and `crypto_narrative_scanner`. | `USE_HERMES` for X/KOL collection and classification. Do not rebuild this in gecko-alpha. |
| TG source ingestion | No stock Hermes skill owns the current curated-channel listener. HermesHub `relay-for-telegram` is not the in-tree Telethon listener replacement. | `KEEP_CUSTOM`. Existing `tg_social_messages` / `tg_social_signals` remain the source of truth. |
| Source-call outcome attribution | No installed or public Hermes skill ties TG/X source calls to forward returns, paper-trade linkage, duplicate clusters, and missing-field coverage. | `KEEP_CUSTOM`. Durable attribution belongs in gecko-alpha DB. |
| Source-quality analytics | Generic Hermes analytics/dashboard tools exist, but none owns gecko-alpha source-call outcomes. | `KEEP_CUSTOM` for ledger and summary helper. |
| Historical price expansion | GoldRush/Covalent can provide OHLCV pair data, but it does not map TG/X source events to gecko-alpha trades or preserve local audit semantics. | `DEFER / USE_AS_REFERENCE`. This PR uses existing price-bearing CG snapshot tables only. |
| Dashboard/reporting | No Hermes dashboard primitive replaces gecko-alpha dashboard. | `DEFER`. This PR ships helper/report, not HTTP endpoint or frontend. |

Awesome-hermes-agent / HermesHub fresh check: no source-quality or outcome-attribution skill was found that replaces the durable gecko-alpha ledger. `agent-analytics-hermes-plugin` is Hermes-activity analytics, not crypto call performance.

Verdict: Hermes owns collection/classification for X. gecko-alpha owns durable source-call attribution, outcome computation, missing-data accounting, and strategy linkage.

## Goal

Create a measurement substrate for TG and X source quality without changing trading behavior.

The operator should be able to answer, later through reports and dashboards:

- Which TG channels and X handles surface useful tokens?
- Which sources repeatedly spam the same token?
- Which calls were early, late, unresolved, or unrankable?
- Which source calls later overlapped with paper trades?
- Which source rankings are evidence-backed versus low-n or low-coverage?

## Non-goals

- No actionability rule changes.
- No paper-trade open/exit changes.
- No X polling cadence changes.
- No KOL/channel removal.
- No dashboard endpoint or frontend in this PR.
- No normal `paper_trades` rows for every KOL call.
- No composite source-quality score.

## Data model

### Table

```sql
CREATE TABLE IF NOT EXISTS source_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL CHECK (source_type IN ('tg', 'x')),
    source_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    token_id TEXT,
    symbol TEXT,
    contract_address TEXT,
    chain TEXT,
    call_ts TEXT NOT NULL,
    observed_at TEXT,
    ingest_delay_sec INTEGER,
    call_kind TEXT NOT NULL CHECK (
        call_kind IN ('first_mention','repeat_mention','ca_call','cashtag_only','unknown')
    ),
    cluster_identity TEXT NOT NULL,
    cluster_identity_kind TEXT NOT NULL CHECK (
        cluster_identity_kind IN ('token_id','contract','symbol','source_event')
    ),
    duplicate_cluster_key TEXT NOT NULL,
    duplicate_rank_in_cluster INTEGER NOT NULL DEFAULT 1,
    resolved_state TEXT NOT NULL,
    price_at_call REAL,
    price_at_call_snapshot_at TEXT,
    price_source TEXT,
    price_age_sec INTEGER,
    forward_30m_snapshot_at TEXT,
    forward_30m_observed_horizon_sec INTEGER,
    forward_1h_snapshot_at TEXT,
    forward_1h_observed_horizon_sec INTEGER,
    forward_6h_snapshot_at TEXT,
    forward_6h_observed_horizon_sec INTEGER,
    forward_24h_snapshot_at TEXT,
    forward_24h_observed_horizon_sec INTEGER,
    mcap_at_call REAL,
    forward_30m_pct REAL,
    forward_1h_pct REAL,
    forward_6h_pct REAL,
    forward_24h_pct REAL,
    max_favorable_pct_24h REAL,
    max_adverse_pct_24h REAL,
    time_to_peak_min REAL,
    linked_paper_trade_id INTEGER,
    linkage_candidate_count INTEGER NOT NULL DEFAULT 0,
    linkage_conflict_count INTEGER NOT NULL DEFAULT 0,
    linkage_method TEXT NOT NULL DEFAULT 'none'
        CHECK (linkage_method IN ('none','direct_tg','heuristic_x')),
    linkage_confidence TEXT NOT NULL DEFAULT 'none'
        CHECK (linkage_confidence IN ('none','direct','heuristic','conflict')),
    outcome_status TEXT NOT NULL
        CHECK (outcome_status IN ('pending','partial','complete','unresolvable')),
    missing_fields TEXT NOT NULL
        CHECK (json_valid(missing_fields) AND json_type(missing_fields) = 'array'),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (linked_paper_trade_id) REFERENCES paper_trades(id) ON DELETE RESTRICT,
    UNIQUE (source_type, source_event_id)
);
```

Indexes:

```sql
CREATE INDEX IF NOT EXISTS idx_source_calls_source_ts
    ON source_calls(source_type, source_id, call_ts);
CREATE INDEX IF NOT EXISTS idx_source_calls_token_ts
    ON source_calls(token_id, call_ts);
CREATE INDEX IF NOT EXISTS idx_source_calls_cluster
    ON source_calls(duplicate_cluster_key);
CREATE INDEX IF NOT EXISTS idx_source_calls_outcome
    ON source_calls(outcome_status, call_ts);
```

Migration sentinels:

- `paper_migrations.name = 'bl_source_calls_v1'`
- `schema_version.version = 20260522`
- Rollback deletes `schema_version` by version primary key, not text description.

## Source mapping

### TG

Input tables:

- `tg_social_signals`
- `tg_social_messages`

Mapping:

- `source_type = 'tg'`
- `source_id = tg_social_signals.source_channel_handle`
- `source_event_id = CAST(tg_social_signals.id AS TEXT)`
- `call_ts = tg_social_messages.posted_at`
- `observed_at = tg_social_signals.created_at`
- `ingest_delay_sec = observed_at - call_ts`
- `token_id`, `symbol`, `contract_address`, `chain`, `mcap_at_call` from `tg_social_signals`
- `linked_paper_trade_id = tg_social_signals.paper_trade_id`
- `linkage_method = 'direct_tg'` when linked
- `linkage_confidence = 'direct'` when linked

### X

Input table:

- `narrative_alerts_inbound`

Mapping:

- `source_type = 'x'`
- `source_id = narrative_alerts_inbound.tweet_author`
- `source_event_id = narrative_alerts_inbound.event_id`
- `call_ts = narrative_alerts_inbound.tweet_ts`
- `observed_at = narrative_alerts_inbound.received_at`
- `ingest_delay_sec = observed_at - call_ts`
- `token_id = resolved_coin_id`
- `symbol = extracted_cashtag` normalized without `$` when unresolved
- `contract_address = extracted_ca`
- `chain = extracted_chain`

Paper-trade linkage is heuristic only:

- require `resolved_coin_id IS NOT NULL`
- require `paper_trades.token_id = resolved_coin_id`
- require `narrative_alerts_inbound.received_at <= paper_trades.opened_at`
- require `paper_trades.opened_at <= narrative_alerts_inbound.received_at + 1 hour`
- choose the smallest matching `paper_trades.id`
- set `linkage_method = 'heuristic_x'`
- set `linkage_confidence = 'heuristic'` only when exactly one candidate trade exists
- store `linkage_candidate_count`
- if more than one candidate trade exists, leave `linked_paper_trade_id` null,
  set `linkage_confidence = 'conflict'`, and store
  `linkage_conflict_count = candidate_count - 1`

Discovery-quality windows use `call_ts`. Strategy-linkage uses `received_at`, because gecko-alpha cannot act before it receives the alert.

Heuristic X rows with `linkage_conflict_count > 0` are non-rankable for strategy-PnL summaries and do not store a concrete `linked_paper_trade_id`. Direct TG links remain separate from heuristic X links.

## Duplicate clusters

Cluster identity fallback:

1. `token_id`
2. `chain|contract_address`
3. normalized `symbol`
4. `source_event_id`

The cluster key is:

```text
sha256(source_type | source_id | cluster_identity | yyyy-mm-dd(call_ts))
```

`duplicate_rank_in_cluster` is assigned by call time inside each cluster. Summaries default to distinct eligible clusters, not raw calls. Raw call count and duplicate rate remain visible.

## Outcome computation

Supported price source in this PR:

- `gainers_snapshots`
- `losers_snapshots`

Not supported for price:

- `trending_snapshots` because it has no price column.
- `score_history` because it has no price column.
- `price_cache` for historical outcomes because it is current-state only.

All timestamp comparisons parse source strings to UTC datetimes in Python before comparison. The implementation must not rely on lexical ordering because the DB contains mixed formats: `...Z`, `+00:00`, and SQLite `YYYY-MM-DD HH:MM:SS`.

At-call price:

- choose most recent snapshot where `snapshot_at <= call_ts`
- require horizon-specific freshness:
  - 30m metric: price age <= 15 minutes
  - 1h metric: price age <= 30 minutes
  - 6h, 24h, and extrema metrics: price age <= 60 minutes
- store `price_at_call_snapshot_at`, `price_source`, `price_age_sec`
- if stale or absent, suppress only the affected metric(s) and record structured missing reasons

Forward windows:

| Metric | Eligible snapshot window |
|---|---|
| `forward_30m_pct` | `[T+30m, T+45m]` |
| `forward_1h_pct` | `[T+1h, T+90m]` |
| `forward_6h_pct` | `[T+6h, T+7h]` |
| `forward_24h_pct` | `[T+24h, T+28h]` |

24h extrema:

- use only rows in `[T, T+24h]`
- rows after 24h are not eligible

If no bounded window row exists, leave that field null and list it in `missing_fields`.

For every populated forward field, store its `forward_*_snapshot_at` and `forward_*_observed_horizon_sec`. This makes the actual observed horizon auditable and prevents a sparse 30m metric from quietly becoming a 6h metric.

## Coverage contract

`missing_fields` is a JSON array of structured objects and is internally consistent with null outcome fields:

```json
[{"field": "forward_30m_pct", "reason": "pending_window"}]
```

Allowed reason examples:

- `identity_unresolved`
- `no_time_series`
- `stale_at_call`
- `pending_window`
- `sparse_forward_window`
- `not_applicable`

Rules:

- `pending`: the call is too recent for one or more forward windows
- `partial`: all required windows are mature, at least one forward field is present, and at least one field is missing due to coverage
- `unresolvable`: no supported price timeline exists for the call
- `complete`: all mature required forward fields exist and `missing_fields = []`

Summaries must show:

- all calls
- raw calls
- distinct clusters
- eligible distinct clusters
- duplicate rate
- resolvable coverage rate
- unresolvable rate
- missing counts by reason (`identity_unresolved`, `no_time_series`, `stale_at_call`, `pending_window`, `sparse_forward_window`)
- per-horizon eligible counts

Forward-return ranking is labeled `resolvable_cg_board_cohort` until broader historical price coverage exists.

## Summary helper

`scout/source_quality/summary.py` exposes a helper similar to:

```python
compute_source_quality_summary(
    conn,
    *,
    min_sample: int = 10,
    min_coverage_rate: float = 0.50,
    source_type: str | None = None,
) -> list[SourceQualityRow]
```

The helper:

- groups by `source_type, source_id`
- uses eligible distinct clusters for sample-size gates
- exposes raw calls and duplicate rate
- joins `paper_trades` for linked strategy PnL at read time
- does not denormalize PnL into `source_calls`
- marks low-n rows as `insufficient_sample`
- marks rows below coverage threshold as `biased_low_coverage`
- only ranks rows with `rank_status = 'rankable_resolvable_cg_board_cohort'`
- does not rank heuristic X strategy PnL when `linkage_conflict_count > 0`
- defers global cross-source ranking until broad historical pricing exists (for example, GoldRush/pair-mapped OHLCV) or the per-source coverage gates prove cohort adequacy

No HTTP endpoint ships in this PR.

## Backfill / update flow

This PR ships an idempotent helper, not a scheduled job.

Passes:

1. create or ignore `source_calls` rows from TG/X upstream tables
2. recompute duplicate clusters and ranks
3. update bounded price/forward outcome fields
4. update paper-trade linkage
5. emit `source_calls_backfill_summary`

Rerun behavior:

- row creation uses `INSERT ... ON CONFLICT(source_type, source_event_id) DO UPDATE SET ...` for mutable source-derived fields, preserving `created_at`
- outcome/linkage updates are deterministic refreshes
- `updated_at` changes when a row is refreshed

## Source-call lag watchdog

Because `source_calls` is a pipeline table, the PR includes a read-only watchdog.

Initial form:

```text
scripts/check_source_calls_lag.py --db scout.db --threshold-minutes 30
scripts/source-calls-lag-watchdog.sh [--db scout.db] [--threshold-minutes 30]
```

The check fails when an upstream source row older than the threshold lacks a corresponding `source_calls` row:

- TG: `tg_social_signals.id` -> `source_type='tg'`, `source_event_id=CAST(id AS TEXT)`
- X: `narrative_alerts_inbound.event_id` -> `source_type='x'`, `source_event_id=event_id`

Quiet periods do not fail. This is upstream-to-ledger lag, not `MAX(updated_at)` staleness.

The PR does not install a cron line unless the operator explicitly asks.

## Test plan

Focused tests:

1. migration idempotency and sentinel uniqueness
2. CHECK constraints for enums and `missing_fields` JSON
3. TG backfill synthetic rows
4. X backfill synthetic rows
5. `UNIQUE(source_type, source_event_id)` rerun idempotency
6. duplicate identity fallback and duplicate rank
7. stale at-call price rejected
8. mixed timestamp formats normalize to UTC before comparison
9. horizon-specific stale at-call price suppression
10. bounded 30m/1h/6h/24h windows
11. 24h extrema do not read beyond 24h
12. pending/partial/complete/unresolvable precedence
13. TG direct paper-trade linkage
14. X heuristic linkage with conflict counts
15. summary low-n uses eligible distinct clusters and coverage-rate gates
16. summary exposes coverage, duplicate, and missing-reason denominators
17. lag watchdog fails/pass/quiet-period cases
18. adjacent TG/X/paper-trading tests continue to pass

## Rollback

```sql
DROP TABLE IF EXISTS source_calls;
DELETE FROM paper_migrations WHERE name = 'bl_source_calls_v1';
DELETE FROM schema_version WHERE version = 20260522;
```

The table has no downstream runtime dependency in this PR, so rollback does not affect paper-trade opens, TG ingestion, or X ingestion.

## Reviewer notes from plan stage

Plan reviewers found and this design folds:

- sparse forward-window leakage
- survivorship bias from covered-only rankings
- duplicate spam inflating sample size
- mixed timestamp-format comparisons
- pending/partial ambiguity
- nullable cluster identity collapse
- stale at-call prices
- §12a watchdog deferral
- endpoint/read-model scope contradiction
- migration sentinel ambiguity
- optional FK semantics
- X heuristic linkage conflict counts
- denormalized PnL conflict
- live row counts in unit-test criteria
