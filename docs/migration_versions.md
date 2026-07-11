# Schema-version allocation record

Authoritative registry of every `schema_version` integer allocated by a
migration in `scout/db.py`. One number, one migration, forever.

**Why this file exists.** Migrations stamp their version with
`INSERT OR IGNORE INTO schema_version (version, applied_at, description) …`.
`version` is the primary key, so if a *new* migration reuses a number that is
already allocated, the insert is **silently a no-op** — the migration body runs
but its version stamp is dropped, and on any DB that already holds that row the
collision produces no error at all.

**Motivating incident.** The `#400` (live-trading M1) branch reused
`20260702`, which was already owned by `source_call_price_snapshot_runs_v1`
(shipped in #397). Three more queued migrations would have repeated the pattern.
`INSERT OR IGNORE` turned the collision into a silent stamp-drop rather than a
loud failure. `tests/test_schema_version_uniqueness.py` now fails the build the
instant two migrations claim the same number, and requires every allocated
number to appear in the table below.

## Allocating a new version

1. Pick the next unused `YYYYMMDD` (monotonic-forward; a same-day second
   migration takes the next free day, as `20260521` did — see notes).
2. Prefer the `#424`-style **bare-additive** migration shape where possible.
3. Add a row to the table below in the same PR that adds the migration.
4. `uv run pytest tests/test_schema_version_uniqueness.py` must stay green.

## Registry

| Version | Migration (`description` stamped) | PR | Notes |
|---|---|---|---|
| 20260418 | feedback_loop_v1 | #29 | |
| 20260423 | bl055_live_trading_v1 | #47 | |
| 20260429 | tier_1a_signal_params_v1 | #60 | |
| 20260505 | bl_hpf_v1_high_peak_fade | #78 | |
| 20260506 | bl_autosuspend_baseline_v1 | #79 | |
| 20260507 | bl_moonshot_opt_out_v1 | #82 | |
| 20260508 | bl_live_eligible_v1 | #84 | live-M1 multi-venue batch |
| 20260509 | bl_live_client_order_id_v1 | #84 | live-M1 multi-venue batch |
| 20260510 | bl_per_venue_services_v1 | #84 | live-M1 multi-venue batch |
| 20260511 | bl_live_trades_telemetry_v1 | #84 | live-M1 multi-venue batch |
| 20260512 | bl_reject_reason_extend_v1 | #84 | live-M1 multi-venue batch |
| 20260513 | bl_quote_pair_v1_quote_symbol_dex_id | #85 | |
| 20260514 | bl_reject_reason_extend_v2 | #86 | |
| 20260515 | bl_slow_burn_v1_slow_burn_candidates | #91 | |
| 20260516 | bl_tg_alert_eligible_v1 | #92 | |
| 20260517 | bl_tg_alert_log_m1_5c_outcome | ef68c6c7 | m1.5c; no PR number in commit subject |
| 20260519 | bl_minara_alert_emissions_v1 | 6e65e2e7 | no PR number in commit subject |
| 20260520 | bl_chain_pattern_provenance_v1 | #146 | |
| 20260521 | bl_actionability_entry_snapshot_v1 | #202 | re-bump from 20260520 to resolve a PK collision (both shipped 2026-05-20) |
| 20260522 | bl_source_calls_v1 | #206 | |
| 20260526 | trade_decision_events_v1 | #279 | |
| 20260529 | bl_new_liquidity_enrichment_v1_candidates_enrichment_cols | #324 | |
| 20260530 | bl_tg_alert_log_dedup_outcome | #336 | |
| 20260531 | bl_tg_alert_operator_actions_v1 | #344 | |
| 20260623 | bl_entry_snapshot_liquidity_provenance_v1 | #381 | |
| 20260629 | dex_instrumentation_v1 | #385 | |
| 20260630 | narrative_resolution_status_v1 | #390 | |
| 20260701 | source_call_price_snapshots_v1 | #395 | |
| 20260702 | source_call_price_snapshot_runs_v1 | #397 | motivating incident — later re-used by the #400 branch |
| 20260703 | ingest_watchdog_state_v1 | #402 | |
| 20260704 | signal_outcome_ledger_v1 | #406 | |
| 20260705 | price_provenance_v1 | #408 | |
| 20260710 | ledger_enrollment_evictions_v1 | #448 |

## Notes / gaps

- **20260518 is unused.** The `bl_minara_alert_emissions_v1` migration
  docstring reads "Schema version 20260518" but the actual
  `INSERT INTO schema_version` binds `20260519`. The docstring number was never
  allocated; the guard tracks the real write site (`20260519`), so `20260518`
  deliberately has no row here.
- PR provenance was backfilled by `git log -S<version> -- scout/db.py`; two
  early commits (`20260517`, `20260519`) landed without a PR number in their
  subject, so their commit SHA is cited instead. The **Version** and
  **Migration** columns are authoritative (derived from the source tree); the
  **PR** column is best-effort provenance.
