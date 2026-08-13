**New primitives introduced:** implements the seven primitives declared in `design_tg_signal_rehabilitation_2026_08_12.md` v1.4 (Stage A subset: items 1–4, 7) plus one PR1-scoped addition — the resolution-snapshot **builder lives in its own module** (`scout/social/telegram/snapshot.py`) so its source hash is fingerprint-bindable without hashing all of `listener.py`.

# PR1 Implementation Plan — TG Shadow Stage A (v1.4, APPROVED scope)

Authorization: `TG_REHABILITATION_V1_4 — SPEC_APPROVED / STAGE_A_IMPLEMENTATION_AUTHORIZED / NO_MERGE_OR_ACTIVATION` (operator, 2026-08-12). Branch `feat/tg-shadow-stage-a`; PR only; no merge, deploy, `.env` change, or control change. The four TG controls are untouched by every line of this PR.

## Hermes-first analysis

Covered by the approved design doc §Hermes-first (receipt refreshed 2026-08-12T19:34Z): one applicable skill (`gecko-solana-verify`, Stage C only — NOT in this PR); all PR1 work is in-tree per that analysis.

## Binding PR1 acceptance criteria (operator, verbatim intent)

1. **Writer lock**: `_persist_signal_row()` — snapshot serialized BEFORE lock; `db._txn_lock` held across INSERT + commit; safe rollback on failure; lock-placement/failure test. (Current master executes INSERT+commit unlocked; PR1 modifies this writer, so PR1 fixes it.)
2. **`decision_as_of = tg_social_signals.created_at`** for immediate AND catch-up/restart evaluation — never wall-clock now. Test: evaluate → advance wall time + add later caller evidence → replay → identical inputs/output.
3. **Fingerprint binds snapshot semantics**: add `resolution_snapshot_schema_version` + snapshot-producer semantic version + snapshot-producer module source hash to `config_fingerprint`.

## Work items (TDD: failing test first per module)

### W1 — Schema migration (`scout/db.py`)
- Additive nullable `ALTER TABLE tg_social_signals ADD COLUMN resolution_snapshot_json TEXT` (guarded by PRAGMA column check, same pattern as existing migrations; structured log action add/skip_exists).
- `CREATE TABLE IF NOT EXISTS tg_act_shadow` per spec SQL + `UNIQUE(signal_id, gate_version)` + index on `(gate_version, created_at)`.
- `CREATE TABLE IF NOT EXISTS tg_act_shadow_generations(gate_version TEXT PRIMARY KEY, activated_at TEXT NOT NULL)`.
- Index creation INSIDE the migration step (DDL-order lesson).
- Tests (`tests/test_tg_shadow_migration.py`): build the OLD tg_social_signals shape in tmp_path, run initialize(), assert column added, existing rows NULL, rowcount unchanged, idempotent re-run.

### W2 — Snapshot builder (`scout/social/telegram/snapshot.py`, NEW module)
- `SNAPSHOT_SCHEMA_VERSION = 1`; `SNAPSHOT_PRODUCER_SEMANTIC_VERSION = "tg-snap-v1"`.
- `build_resolution_snapshot(token: ResolvedToken) -> str`: canonical JSON (sorted keys, `repr` floats via the canonical-JSON helper shared with fingerprinting), fields: `snapshot_schema_version`, `price_usd`, `volume_24h_usd`, `age_days`, `liquidity_usd` (present only if `ResolvedToken` gains it — v1: emit `null`; NO new API calls), `safety_pass`, `safety_check_completed`, `safety_skipped_no_ca`. Unavailable value ⇒ JSON `null`.
- `parse_resolution_snapshot(text) -> dict | None` (None on unparseable — feeds `shadow_block_snapshot_missing`).
- Tests (`tests/test_tg_shadow_snapshot.py`): round-trip, null-vs-absent semantics, canonical determinism (two builds byte-identical).

### W3 — Writer hardening + persistence pass-through (`listener.py`)
- `_persist_signal_row(..., resolution_snapshot_json: str | None)` → returns `signal_id` (lastrowid). Serialization done by caller BEFORE lock; INSERT + commit under `async with db._txn_lock`; rollback + structured log on failure (match `_persist_message` pattern).
- Dispatcher `signal_data` widened: `price_usd`, `volume_24h_usd`, `age_days`, `safety_pass`, `safety_check_completed`, `safety_skipped_no_ca` (CA + cashtag paths).
- Tests: lock-placement test (acquire `db._txn_lock` externally, assert writer blocks until released — real seam, no mock of the lock); failure-rollback test; signal_data widening asserted on the engine call (spec= mock per safety-critical mock lesson).

### W4 — Shadow core (`scout/social/telegram/shadow.py`, NEW module)
- `CallerFeatureProvider` Protocol: `caller_feature_semantic_version: str`, `feature_schema() -> list[tuple[str, str]]`, `module_source_hash: str`, `features(channel_handle, decision_as_of, current_signal_id) -> dict` (two groups per spec — dict carries `history_*` / `event_*` key prefixes so the split is mechanical).
- Provider registry: `register_caller_feature_provider(p)` / `get_registered_provider()`; production wiring registers NOTHING in PR1.
- `compute_config_fingerprint(settings, provider) -> str`: SHA-256 over canonical JSON of {feature_schema, thresholds (all `TG_SHADOW_*` values read by the evaluator), evaluator_semantic_version, caller_feature_semantic_version, evaluator_module_hash, provider.module_source_hash, **resolution_snapshot_schema_version, snapshot_producer_semantic_version, snapshot_module_hash** (criterion 3)}. `gate_version = "tg-shadow-v1+" + fp[:16]`; full digest into `features_json`.
- `evaluate_tg_actionability_shadow(signal_row, snapshot, features, settings)` — deterministic check order:
  1. snapshot None/unparseable → `shadow_block_snapshot_missing`
  2. mcap missing/≤0 → `shadow_block_missing_mcap`
  3. `contract_address` None OR identity class in `TG_SHADOW_UNPRICEABLE_IDENTITY_CLASSES` (default empty; populated only after a PQ verdict) → `shadow_block_identity_unpriceable_class`
  4. NOT (safety_check_completed AND safety_pass) → `shadow_block_safety_not_passed`
  5. mcap outside [`TG_SHADOW_MCAP_MIN_USD`=10_000, `TG_SHADOW_MCAP_MAX_USD`=500_000_000] → `shadow_block_mcap_band`
  6. `TG_SHADOW_REQUIRE_LIQUIDITY` (default False) AND liquidity null → `shadow_block_liquidity_unknown`
  7. `history_eligible_distinct_clusters < TG_CALLER_MIN_ELIGIBLE_CLUSTERS` (10) → `shadow_block_caller_insufficient_n`
  8. `history_priceable_coverage_rate < TG_SHADOW_MIN_CALLER_COVERAGE` (0.5) → `shadow_block_caller_quality`
  9. `event_duplicate_rank_in_cluster > 1` → `shadow_block_duplicate_call`
  10. else `shadow_pass`
  All thresholds from Settings (no hardcodes); every threshold participates in the fingerprint.
- Writer: `write_shadow_decision(db, signal_id, gate_version, decision, features_json)` — `INSERT ... ON CONFLICT(signal_id, gate_version) DO NOTHING`, statement `rowcount` (0 ⇒ `tg_shadow_duplicate_skip` log; never `total_changes`); mutation+commit under `db._txn_lock`; all serialization pre-lock; any other IntegrityError propagates.
- Generation: `ensure_generation(db, gate_version)` — called ONLY from the enabled-startup path with a real provider; `ON CONFLICT DO NOTHING` + rowcount; never called when `TG_SHADOW_ENABLED=false`. No real provider + flag true ⇒ `tg_shadow_activation_refused_no_feature_provider` log, no row, no processing.
- Eligibility scan: signals with `resolution_state='RESOLVED' AND created_at >= activated_at(current)` lacking a current-gate_version row; per-signal: `decision_as_of = row.created_at` (criterion 2), snapshot from the persisted column ONLY (no refetch). Scan completion → structured log `tg_shadow_scan_complete{scanned,written}` + `tg_social_health` heartbeat row (`component='tg_shadow_writer'`) — health table is upsert-by-design, it is watchdog state, not evidence.
- Live hook: in the listener after `_persist_signal_row` returns, best-effort `evaluate+write` guarded by try/except → `tg_shadow_eval_failed` log; NEVER affects alerting/persistence/dispatch. Inert unless enabled+provider.
- Tests (`tests/test_tg_shadow_core.py`, `tests/test_tg_shadow_writer.py`): restart-replay idempotency (same signal twice ⇒ rowcount 1 — existence AND count); loud-failure (NOT NULL violation raises); no-generation-while-disabled; activation-refused-without-provider; generation-cutover (pre-activation RESOLVED rows excluded); **criterion-2 replay test** (evaluate, advance clock + add later evidence via fixture provider keyed on decision_as_of, replay ⇒ byte-identical features_json + decision); fingerprint sensitivity tests (threshold change, snapshot schema version change, provider hash change ⇒ new gate_version; unrelated setting ⇒ unchanged); evaluator reason-order table test.
- Fixture provider (`tests/fixtures` or conftest): deterministic, `features()` derives values ONLY from (channel_handle, decision_as_of, current_signal_id) — so replay determinism is real, marked test-only.

### W5 — Watchdog (`scripts/check_tg_shadow_lag.py`)
- Read-only; exits 0 silently when `TG_SHADOW_ENABLED` false or no generation row.
- Condition (a): eligible overdue rows (>`TG_SHADOW_LAG_THRESHOLD_MIN`=60) unshadowed AND health-row scan completed after they became overdue → page.
- Condition (b): eligible overdue rows AND no completed scan within `TG_SHADOW_SCAN_CADENCE_MIN`=30 → page (dead writer).
- Page via alerter with `parse_mode=None` + `*_alert_dispatched`/`*_alert_delivered` structured logs (§12b conventions). Cron registration is a DEPLOY/ACTIVATION step — documented in the runbook section of the PR description, NOT installed by the PR.
- Tests (`tests/test_tg_shadow_watchdog.py`): three-state matrix (quiet/failing/dead), disabled ⇒ silent, catch-up ⇒ no page until scan completes.

### W6 — Settings (`scout/config.py`)
`TG_SHADOW_ENABLED=False`, `TG_SHADOW_LAG_THRESHOLD_MIN=60`, `TG_SHADOW_SCAN_CADENCE_MIN=30`, `TG_SHADOW_MCAP_MIN_USD=10_000`, `TG_SHADOW_MCAP_MAX_USD=500_000_000`, `TG_SHADOW_REQUIRE_LIQUIDITY=False`, `TG_SHADOW_MIN_CALLER_COVERAGE=0.5`, `TG_CALLER_MIN_ELIGIBLE_CLUSTERS=10`, `TG_SHADOW_UNPRICEABLE_IDENTITY_CLASSES=[]`.

## Out of scope (PR2+/activation)
Real `tg_caller_features()` provider (incl. the CG-identity price-source reconciliation the operator flagged), PQ harness, cron installation, `.env` flips, any control change.

## Verification gate before PR
`uv run pytest --tb=short -q` full suite green locally (win32 skips per known constraints); `uv run black scout/ tests/`; scaffold tests must not regress; evaluator/evaluation never runs in dry-run pipeline unless enabled.
