**New primitives introduced:** chain-pattern provenance columns (`is_protected_builtin`, `disabled_reason`, `disabled_at`), built-in pattern reconciliation, protected-retirement advisory log, chain-anchor health watchdog script/timer.

# Design: BL-NEW-CHAIN-ANCHOR-PIPELINE-FIX

## Goal

Restore `chain_completed` by repairing the chain-pattern lifecycle state that currently prevents the tracker from loading any active patterns, then add enough provenance and watchdog coverage to keep the same silent outage from recurring.

## Runtime Root Cause

Prod has live upstream events but no active patterns:

| Check | Result |
|---|---|
| `signal_events` narrative `category_heating` | live, last `2026-05-17T13:18:09Z` |
| `signal_events` memecoin `candidate_scored` / `conviction_gated` | live, last `2026-05-17T13:46Z+` |
| `active_chains` | stale, max `anchor_time=2026-05-11T16:42:03Z` |
| `chain_matches` | stale, narrative max `2026-05-11T16:43:05Z`, memecoin max `2026-05-04T00:51:02Z` |
| `signal_params.chain_completed` | `enabled=1` |
| `chain_patterns` | all three built-ins `is_active=0` |
| `.env CHAINS_ENABLED` | must be verified during deploy/preflight; tracker does not start if false |

`check_chains()` calls `load_active_patterns()` first and returns if the list is empty. Therefore no active patterns means no event scan, no anchor write, and no completion write.

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Chain-pattern lifecycle / retirement / reactivation over gecko-alpha SQLite tables | None installed on VPS. Installed relevant skills are `crypto_narrative_scanner`, `xurl`, and inbound `webhook-subscriptions`; none own gecko-alpha `chain_patterns` / `active_chains`. Public bundled catalog has no local DB pattern lifecycle skill: <https://hermes-agent.nousresearch.com/docs/reference/skills-catalog>. | Build inside existing gecko-alpha chain module. |
| On-chain Base/Solana token data | Optional Hermes [`blockchain/base` and `blockchain/solana`](https://hermes-agent.nousresearch.com/docs/reference/optional-skills-catalog) query chain data with USD pricing, but they are read-only data clients, not lifecycle or watchdog tools. | Not applicable to this bug; keep as future enrichment reference. |
| Webhook/event routing | Installed `webhook-subscriptions` is inbound agent-run plumbing, not a DB freshness watchdog or chain matcher. | Not applicable. |
| X/KOL narrative signals | Installed `xurl` + `crypto_narrative_scanner` cover X-side ingestion/classification, not chain pattern lifecycle. | Reuse existing X path; out of scope. |

Awesome-Hermes ecosystem check: [`0xNyk/awesome-hermes-agent`](https://github.com/0xNyk/awesome-hermes-agent) has blockchain-adjacent projects (`ripley-xmr-gateway`, `hermes-blockchain-oracle`, `mercury`) but no reusable primitive for gecko-alpha SQLite pattern retirement/revival or `active_chains` write-rate monitoring.

One-sentence verdict: Hermes has crypto query and agent-integration skills, but no installed or public Hermes capability replaces gecko-alpha's local chain-pattern lifecycle state, so custom in-tree repair is justified.

Deployed-surface evidence:

- `2026-05-17`: ran `find /home/gecko-agent/.hermes -maxdepth 6 -type f` on srilu and filtered for `chain|anchor|blockchain|crypto|solana|base|ethereum|goldrush|covalent|token|wallet|dex|webhook|xurl`.
- Matches were `social-media/xurl`, `devops/webhook-subscriptions`, `crypto_narrative_scanner`, and unrelated runtime files; no chain-pattern lifecycle/watchdog skill found.

## File Changes

| File | Change |
|---|---|
| `scout/db.py` | Add idempotent migration for `chain_patterns` provenance columns plus cutover marker. |
| `scout/chains/models.py` | Add optional model fields mirroring new columns. |
| `scout/chains/patterns.py` | Sync built-in definitions safely; preserve lifecycle fields; reactivate lifecycle-retired protected built-ins; block protected built-in retirement. |
| `scout/chains/tracker.py` | Log error when chain tracking is enabled but no active patterns load. |
| `scripts/check_chain_anchor_health.py` | New SQLite health checker for active protected patterns and `active_chains` freshness. |
| `scripts/chain-anchor-health-watchdog.sh` | Shell wrapper that reads `.env`, runs checker, and sends Telegram on failure. |
| `systemd/chain-anchor-health-watchdog.service` / `.timer` | Hourly watchdog unit. |
| `tests/test_chains_patterns.py` | Reconciliation/provenance tests. |
| `tests/test_chains_learn.py` | Protected retirement behavior tests. |
| `tests/test_chains_tracker.py` | Empty active-pattern log test. |
| `tests/test_chain_anchor_health_watchdog.py` | Watchdog unit tests. |
| `tasks/todo.md`, `backlog.md` | Progress and shipped-state updates. |

## Schema

Add columns to `chain_patterns`:

```sql
ALTER TABLE chain_patterns ADD COLUMN is_protected_builtin INTEGER NOT NULL DEFAULT 0;
ALTER TABLE chain_patterns ADD COLUMN disabled_reason TEXT;
ALTER TABLE chain_patterns ADD COLUMN disabled_at TEXT;
```

Allowed `disabled_reason` values:

| Value | Meaning | Startup reactivation? |
|---|---|---|
| `NULL` | active, or inactive with unknown/operator legacy state | no if inactive |
| `legacy_lifecycle_retired` | inactive built-in row from pre-provenance lifecycle retirement | yes |
| `lifecycle_retired` | non-protected lifecycle retirement | no for non-built-ins; protected built-ins should not enter this state after fix |
| `operator_disabled` | operator intentionally disabled the pattern | no |
| `code_disabled` | code definition intentionally disabled the pattern | no |

Migration backfill:

1. Mark rows whose `name` is in `BUILT_IN_PATTERNS` as `is_protected_builtin=1`.
2. For the exact known prod outage snapshot, set `disabled_reason='legacy_lifecycle_retired'` and `disabled_at=updated_at` if no reason exists:
   - all three built-ins inactive
   - all three built-ins have `updated_at='2026-05-17 01:24:59'`
   - all three built-ins have historical stats matching the outage-era pattern rows (`full_conviction` 52/2, `narrative_momentum` 58/2, `volume_breakout` 70/3)
3. Leave all other inactive built-ins as `disabled_reason=NULL`, so unknown pre-provenance/manual state is not silently reversed.
4. Record `paper_migrations.name='bl_chain_pattern_provenance_v1'` and `schema_version.version=20260520`.

This intentionally makes automatic legacy recovery narrow. If another environment has inactive built-ins but does not match the known prod snapshot, the migration preserves the state and the watchdog/logs make it visible for operator decision.

## Pattern Reconciliation

`seed_built_in_patterns(db)` keeps its public name but becomes insert-or-reconcile:

1. Insert missing built-ins with full code defaults.
2. For existing built-ins:
   - Sync `description`, `steps_json`, `min_steps_to_trigger`, `conviction_boost`, `is_protected_builtin`.
   - Preserve `alert_priority`, `historical_hit_rate`, `total_triggers`, `total_hits`, `created_at`.
   - Preserve inactive operator/code state.
   - Reactivate only when `is_protected_builtin=1 AND disabled_reason IN ('legacy_lifecycle_retired', 'lifecycle_retired')`.
3. On reactivation, set `is_active=1`, `disabled_reason=NULL`, `disabled_at=NULL`.
4. Emit `chain_patterns_seeded_or_synced` with counts: `inserted`, `synced`, `reactivated`.

This intentionally treats `alert_priority` as lifecycle-owned because `run_pattern_lifecycle()` promotes/graduates it today.

## Lifecycle Guard

In `run_pattern_lifecycle()`:

1. Compute stats as before.
2. When a pattern is below `_RETIREMENT_HIT_RATE`:
   - If a protected built-in is already inactive with `disabled_reason IN ('operator_disabled', 'code_disabled')`, preserve inactive state while updating stats; do not promote, graduate, or reactivate it.
   - If `is_protected_builtin=1` and the row is active, do not set `is_active=0`; update hit stats and emit `chain_pattern_retirement_blocked_protected`.
   - Else set `is_active=0`, `disabled_reason='lifecycle_retired'`, `disabled_at=datetime('now')`, and emit existing retirement log.
3. Promotion/graduation still changes `alert_priority` for protected built-ins.
4. Systemic-zero-hits guard stays in place and still short-circuits before any updates.

## Tracker Visibility

If `check_chains()` sees no active patterns:

```python
logger.error("chain_no_active_patterns", chains_enabled=settings.CHAINS_ENABLED)
return
```

No exception is raised; the tracker loop should stay alive. The error log exists so journalctl shows the silent short-circuit before the watchdog runs.

## Watchdog

`scripts/check_chain_anchor_health.py`:

Inputs:

- `--db PATH`
- `--env PATH` optional, default `.env`
- `--anchor-window-hours` default `24`
- `--active-stale-hours` default `24`

Logic:

1. If `.env` has `CHAINS_ENABLED=False`, return OK JSON with `status="disabled"`.
2. Count active protected built-ins.
3. Find recent anchor-eligible events by loading protected active patterns and applying their step-1 event type and condition with `evaluate_condition()`:
   - narrative examples: `category_heating`
   - memecoin example: `candidate_scored` with `signal_count >= 2`
4. Find `MAX(anchor_time)` from `active_chains` per `(pipeline, pattern_name)`.
5. Fail if active protected built-ins count is 0.
6. Fail if recent anchor events exist for a specific `(pipeline, pattern_name)` and that key's `active_chains` max is missing or older than `--active-stale-hours`.
7. Open SQLite read-only and return `status="schema_pending"` without alert if the provenance migration has not run yet; this avoids deploy-time false positives before `gecko-pipeline` applies migrations.
8. Return JSON with `ok`, `status`, `active_protected_patterns`, `recent_anchor_events`, `recent_anchor_event_keys`, `active_chains_max_anchor_time`, `active_chains_missing_keys`, `active_chains_stale_keys`, and `reasons`.

The watchdog deliberately does not alert on stale `chain_matches`. Completions require later step sequences and are a signal-quality metric, not a table-writer health SLO.

The shell wrapper follows the existing Minara watchdog pattern:

- runs the Python checker
- logs `OK: ...` on success
- loads Telegram env on failure
- sends plain text with `parse_mode=`
- exits nonzero on alert or diagnostic failure

`systemd/chain-anchor-health-watchdog.service` must include `SuccessExitStatus=1`, matching the existing Minara watchdog convention where exit 1 means the alert was delivered.

## Tests

TDD targets:

1. Pattern reconciliation:
   - inactive built-in with `disabled_reason='legacy_lifecycle_retired'` reactivates
   - inactive built-in with `disabled_reason='operator_disabled'` stays inactive
   - learned `alert_priority='high'` survives reconciliation
   - stale `steps_json` is synced to built-in definition

2. Lifecycle:
   - protected built-in with low-but-nonzero hit rate stays active and logs blocked-retirement
   - protected built-in with `disabled_reason='operator_disabled'` stays inactive through lifecycle
   - protected built-in with `disabled_reason='code_disabled'` stays inactive through lifecycle
   - non-built-in low-hit pattern still retires and stamps `disabled_reason='lifecycle_retired'`
   - existing systemic-zero-hits guard still prevents broad retirement

3. Migration:
   - fresh DB has provenance columns
   - old DB missing provenance columns is migrated during `initialize()`
   - exact prod-snapshot inactive built-ins are stamped `legacy_lifecycle_retired`
   - non-matching inactive built-in is not stamped as lifecycle-retired
   - migration is idempotent and records `paper_migrations` / `schema_version`

4. Tracker:
   - all patterns inactive => `check_chains()` logs `chain_no_active_patterns` and does not crash

5. Watchdog:
   - `CHAINS_ENABLED=False` exits OK disabled
   - active protected patterns + fresh active chain exits OK
   - zero active protected patterns fails
   - recent anchor-eligible events + stale/missing `active_chains` fails
   - raw memecoin `candidate_scored` with `signal_count < 2` is not counted as anchor-eligible

Focused verification:

```powershell
C:\projects\gecko-alpha\.venv\Scripts\python.exe -m pytest `
  tests/test_chains_patterns.py `
  tests/test_chains_learn.py `
  tests/test_chains_tracker.py `
  tests/test_chain_anchor_health_watchdog.py -q
```

Wider verification:

```powershell
C:\projects\gecko-alpha\.venv\Scripts\python.exe -m pytest `
  tests/test_chains_events.py `
  tests/test_chains_db.py `
  tests/test_chains_patterns.py `
  tests/test_chains_tracker.py `
  tests/test_chains_integration.py `
  tests/test_chains_learn.py `
  tests/test_chain_outcomes_hydration.py `
  tests/test_narrative_chain_coherence.py -q
```

## Deployment

1. Merge PR.
2. Pull on VPS.
3. Restart `gecko-pipeline`.
4. Install watchdog service/timer: `systemctl enable --now chain-anchor-health-watchdog.timer`.
5. Verify:

```bash
grep '^CHAINS_ENABLED=' /root/gecko-alpha/.env

sqlite3 /root/gecko-alpha/scout.db \
  "SELECT name,is_active,alert_priority,is_protected_builtin,disabled_reason FROM chain_patterns"

journalctl -u gecko-pipeline -g chain_tracker_started --since "10 minutes ago" --no-pager

journalctl -u gecko-pipeline -g chain_patterns_seeded_or_synced --since "10 minutes ago" --no-pager

sqlite3 /root/gecko-alpha/scout.db \
  "SELECT COUNT(*), MAX(anchor_time) FROM active_chains"
```

Expected first deploy result: built-ins active; `disabled_reason` clear; new `active_chains` rows once fresh anchor events arrive.

## Rollback

- Disable watchdog independently: `systemctl disable --now chain-anchor-health-watchdog.timer`.
- Coarse chain kill switch: set `CHAINS_ENABLED=False`, restart `gecko-pipeline`.
- Per-pattern operator disable: `UPDATE chain_patterns SET is_active=0, disabled_reason='operator_disabled', disabled_at=datetime('now') WHERE name='<pattern>';`
- Before deployment, capture rollback state:

```bash
sqlite3 /root/gecko-alpha/scout.db \
  ".mode insert chain_patterns" \
  "SELECT id,name,is_active,alert_priority,is_protected_builtin,disabled_reason,disabled_at,updated_at FROM chain_patterns" \
  > /root/gecko-alpha/chain_patterns_rollback_$(date +%Y%m%d%H%M%S).sql
```

- To restore only the pre-deploy active/provenance state after rollback:

```sql
UPDATE chain_patterns
SET is_active = 0,
    disabled_reason = NULL,
    disabled_at = NULL
WHERE name IN ('full_conviction', 'narrative_momentum', 'volume_breakout');
```

- Code rollback restores prior lifecycle behavior, but the provenance migration is forward-only.

## Deferred

- Replay/backfill May 11 to May 17 missed anchors. This PR is forward recovery plus recurrence prevention.
- A UI/operator command for per-pattern disable. Direct SQL is acceptable for V1 recovery.
- Completion-rate watchdog. Needs a measured completeable-step denominator to avoid false positives.

## Design Review Fold

- Reviewer A/B HIGH: legacy inactive-state inference can reverse operator intent. Folded with exact prod-snapshot-gated `legacy_lifecycle_retired` stamping; all other unknown inactive built-ins stay unknown/inactive.
- Reviewer A HIGH: lifecycle must preserve `operator_disabled` / `code_disabled`, not only startup reconciliation. Folded into lifecycle guard and test plan.
- Reviewer A MEDIUM: migration tests missing. Folded into explicit migration test group with cutover markers.
- Reviewer B MEDIUM: raw anchor event count can false-alert. Folded by applying active protected patterns' step-1 condition with the same evaluator.
- Reviewer B MEDIUM: verify `CHAINS_ENABLED` and tracker loop. Folded into runtime/deploy checks.
- Reviewer B LOW: Hermes deployed-surface evidence needs command/timestamp. Folded.
- Reviewer B LOW: systemd exit semantics need `SuccessExitStatus=1`. Folded.
