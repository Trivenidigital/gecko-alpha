**New primitives introduced:** Same set as `tasks/plan_score_volume_pruning_harden.md` — `SCORE_HISTORY_RETENTION_DAYS` + `VOLUME_SNAPSHOTS_RETENTION_DAYS` Settings fields (default 21), a `@model_validator(mode='after')` on Settings enforcing retention ≥ `SECONDWAVE_COOLDOWN_MAX_DAYS`, `Database.prune_score_history` and `Database.prune_volume_snapshots` methods, two split migrations `_migrate_score_history_scanned_at_index` + `_migrate_volume_snapshots_scanned_at_index` (each with `PRAGMA busy_timeout=90000`), `_run_hourly_maintenance(db, session, settings, logger)` helper extracted from main.py, `_run_extra_table_prune(db)` helper extracted from narrative agent, `try/except ValidationError` startup-guard wrappers around `Settings()` constructions, structured log events `score_history_pruned` / `volume_snapshots_pruned` / `score_history_prune_failed` / `volume_snapshots_prune_failed` / `extra_prune_table_error` / `settings_validation_failed`.

# Design: Harden score_history + volume_snapshots pruning

**Plan reference:** `tasks/plan_score_volume_pruning_harden.md` (commit `230002d`)
**Audit context:** `tasks/findings_backlog_drift_audit_2026_05_16.md` — closes BL-NEW-SCORE-HISTORY-PRUNING + BL-NEW-VOLUME-SNAPSHOTS-PRUNING per §7a residual-gap rule.
**Plan reviews folded:** V1 (code-correctness) + V2 (data-loss-risk) — see plan commit `230002d` body for detailed fold notes.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| SQLite retention / pruning primitives | None found (Hermes skill hub 689 skills, verified 2026-05-16) | Build in-tree. |
| Pydantic v2 model_validator patterns | N/A — project already uses Pydantic Settings | Extend existing. |
| Structured-log observability for prune passes | None — Hermes Observability/MLOps skills cover external telemetry | Build in-tree. |

awesome-hermes-agent ecosystem check: repo 404 on 2026-05-16. **Verdict:** Internal-DB maintenance; no Hermes capability applies.

---

## Design decisions

### D1. Default retention = 21d (V2#1 fold)

Existing in-tree pruning uses hardcoded 14d. The naive port would preserve 14d. But §9a verification + V2 review surfaced a race: at retention=14d with `<=` cutoff and an hourly prune, rows just over the boundary are deleted before `secondwave`'s scan can see them. `SECONDWAVE_COOLDOWN_MAX_DAYS=14` (default, confirmed live on srilu) defines the secondwave evidence window upper bound; retention must exceed it.

Default `21 = 14 + 7d buffer`. Operator can lower via `.env` but the model_validator (D2) blocks `retention < cooldown`.

**Disk impact:** at observed write rate (~17k/hr), 14d → 21d retention grows table from ~5.9M rows to ~8.8M rows. ~600MB extra at steady state. Acceptable.

**Behavior change vs current prod:** narrative's daily loop will continue pruning *its* 6 remaining tables. The score_history + volume_snapshots tables are removed from that loop. The hourly main.py loop now owns those two tables at the new default (21d). First hourly pass deletes 0 rows because the tables are already at the 14d boundary; row count grows over the next 7 days as the boundary shifts from 14d to 21d.

### D2. `@model_validator(mode='after')` enforcement + startup guard (V2#3 + V4#1 fold)

Silent retention-vs-cooldown mis-config is the failure mode. Validator runs at Settings construction and raises if `SCORE_HISTORY_RETENTION_DAYS < SECONDWAVE_COOLDOWN_MAX_DAYS` (same for volume).

Choice of `model_validator(mode='after')` over `field_validator(...)`: the cross-field dependency (retention depends on cooldown) is what `model_validator` is designed for. `field_validator` only sees one field at a time. Precedent in tree: `_validate_live_caps_relation` at `scout/config.py:766-781` (V3 review noted this — same shape).

**V4#1 fold — startup-failure guard:** systemd's `Restart=always` + `RestartSec=10` (verified on srilu via `/etc/systemd/system/gecko-pipeline.service` SSH inspection) means a `ValidationError` at `Settings()` construction causes the service to crash-loop every 10s. Without a guard, the raw Pydantic stack lands in journalctl but is easy to miss.

Mitigation in this PR: wrap `Settings()` construction at `scout/main.py:1384` (and the 3 other sites V3 identified — `main.py:1359` `--check-config`, `social/telegram/cli.py:45`, `trading/calibrate.py:495`) with `try/except ValidationError` to emit a structured `logger.error("settings_validation_failed", error=str(exc))` BEFORE re-raise. journalctl-grep-friendly + visible in `systemctl status`.

**Deferred to follow-up `BL-NEW-SETTINGS-VALIDATION-ALERT`:** curl-direct Telegram on startup-fail (the operator-active-alert side). A per-restart curl emits ~360 msg/hr without file-based dedup; doing it right (first-time-only marker, dedup window) is a focused PR of its own, not in scope here.

**V3 SHOULD-FIX fold — `--check-config` surface:** the new validator now affects the operator's `python -m scout.main --check-config` workflow (main.py:1359). Calling it out in PR description: this is a feature (`--check-config` becomes a pre-deploy validation surface) and matches the existing `_validate_live_caps_relation` behavior. No code change beyond the validator itself.

### D3. Indexes via SPLIT migration step (V1#5 / V2#5 / V4#3 fold + `feedback_ddl_before_alter.md` memory)

Existing `idx_score_hist_addr` and `idx_volume_snap_addr` use `(contract_address, scanned_at)` — leading column is wrong for `WHERE scanned_at <= ?`. SQLite cannot use these indexes for the prune DELETE, forcing a table scan over 6M rows hourly.

**Solution:** add `idx_score_history_scanned_at` and `idx_volume_snapshots_scanned_at` (single-column on `scanned_at`) via TWO independent migrations.

**Why split (V4#3 fold):** a single combined `BEGIN EXCLUSIVE ... CREATE INDEX ... CREATE INDEX ... COMMIT` rolls back BOTH indexes if the second one fails (e.g., disk pressure during the second build). Split into `_migrate_score_history_scanned_at_index` + `_migrate_volume_snapshots_scanned_at_index`, each with its own `paper_migrations` entry, so partial success is durable. Operator re-deploy retries only the failed second migration.

**Why a migration, not `_create_tables`:** per memory `feedback_ddl_before_alter.md` (BL-060 prod crash), `_create_tables` does not re-run on existing prod tables (the implicit guard via `CREATE TABLE IF NOT EXISTS` blocks the function), so any new `CREATE INDEX IF NOT EXISTS` placed inside it would never execute on srilu. The dedicated migration mechanism (mirroring `_migrate_minara_alert_emissions_v1` at db.py:3432) uses `paper_migrations(name, cutover_ts)` as an idempotency record and runs unconditionally on `initialize()`.

**V4#2 fold — `PRAGMA busy_timeout = 90000`:** the migration's `BEGIN EXCLUSIVE` holds a write lock through the O(N log N) index build (~30-60s on 6M rows). Concurrent readers from `gecko-dashboard.service` (separate process, same DB file) would error with `OperationalError: database is locked` without retry. Setting `busy_timeout = 90000` (90s, slightly above worst-case index-build duration) makes SQLite retry the lock internally instead of failing fast.

**EXPLAIN-QUERY-PLAN test** confirms the new index is used post-migration. Without this test, a future PR could rename the index and the prune would silently revert to table scan.

**Deploy note:** `CREATE INDEX` on a 6M-row table is O(N log N) ≈ 30-60 seconds wall time per index on the VPS. Total ~60-120s. Single short write lock per migration. Operator should be aware of brief pipeline + dashboard pause during initial start after deploy.

### D4. `_run_hourly_maintenance` extraction (V1#7 fold)

The inline hourly block at `scout/main.py:1702-1751` is not testable without `run_pipeline` time-mocking. Extracting to `_run_hourly_maintenance(db, session, settings, args, logger)` enables direct unit testing of the prune wiring — much cleaner than the hand-wavy "mock time and trigger the branch" alternative.

The extraction is committed as a separate refactor commit with NO behavior change. That commit is reviewed independently; downstream prune-wiring commits build on it.

### D5. `_run_extra_table_prune` extraction + drop outer try (V1#2 fold)

Mirror of D4 for the narrative side. The helper handles per-table errors via structured `logger.exception` — fault-isolated. The outer `try: ... except Exception: logger.exception("extra_prune_error")` wrapping the original loop is now unreachable except for import-resolution failures, so it's dropped. Call site becomes a single `await _run_extra_table_prune(db)` line.

### D6. Log-level discipline (V2#7 + V4#4 fold — REVISED)

V4#4 review verified `scout/main.py:1373-1382` has no `structlog.stdlib.filter_by_level` processor — `logger.debug(...)` would emit to journalctl identical to `info`. The original D6 ("debug suppresses noise") was structurally false.

Revised pattern follows the in-tree sibling at `main.py:1747-1748` (`prune_cryptopanic_posts`):

- `rows_deleted > 0` → `logger.info` (operationally interesting)
- `rows_deleted == 0` → **silent** (no emit; visible via `sqlite3 scout.db "SELECT COUNT(*) ..."` if operator wants to confirm)
- Any exception → `logger.exception` (always visible, structured stack)

Operator visibility of "did the prune mechanism work?" comes from migration-time logs + `info` lines after retention boundary shifts past current oldest row (~7d post-deploy at default 21d retention) + on-demand DB inspection.

**Trade-off vs §12a:** silent-when-zero loses an active "I ran" heartbeat. Mitigation deferred — see V4#6 fold under "Residual gaps" (`BL-NEW-SCORE-VOLUME-PRUNE-ALERT`).

### D7. Per-DELETE commit, NO batched transaction (V1#9 fold)

Each prune method commits inside itself. The two main.py calls do NOT wrap in a single transaction because:
- The operations are independent (different tables)
- Concurrent INSERTs from the scorer loop should interleave; batched transaction would hold writer lock for the full prune cycle
- SQLite WAL mode handles writer contention without batched-txn complexity

Documented inline in main.py to prevent a future reviewer from "fixing" this.

### D8. `<=` boundary semantic preserved (V1#3 fold + V1#11 tie-test)

Matches sibling `prune_cryptopanic_posts` (db.py:4754-4758) which documents the Windows-clock-tie rationale. Tie test (Task 2 step 2.1 in plan) locks in the semantic so a future PR can't silently flip to `<` without test failure.

### D9. Return type: `cur.rowcount or 0` (V1#3 fold)

Matches sibling `prune_perp_anomalies` (db.py:4699). Diverges from `prune_cryptopanic_posts` (db.py:4764) which omits the `or 0`. We follow the safer coalesce-to-zero form; docstring notes the divergence so a future reviewer doesn't "fix" it the wrong way.

---

## Cross-file invariants

| Invariant | Where enforced | Test |
|---|---|---|
| `SCORE_HISTORY_RETENTION_DAYS >= SECONDWAVE_COOLDOWN_MAX_DAYS` | `model_validator` in `scout/config.py` | `test_retention_must_exceed_secondwave_cooldown` |
| `VOLUME_SNAPSHOTS_RETENTION_DAYS >= SECONDWAVE_COOLDOWN_MAX_DAYS` | Same validator | Same test (per-field loop) |
| `score_history` table is pruned hourly when `_run_hourly_maintenance` runs | `scout/main.py` `_run_hourly_maintenance` body | `test_run_hourly_maintenance_calls_score_history_prune` |
| Index `idx_score_history_scanned_at` is used by prune DELETE | Migration `_migrate_score_volume_prune_indexes` + index covers WHERE clause | `test_prune_score_history_uses_scanned_at_index` (EXPLAIN) |
| Narrative loop no longer prunes score_history or volume_snapshots | `_run_extra_table_prune` list at `scout/narrative/agent.py` | implicit (table list length = 6, asserted in `test_narrative_extra_prune_logs_error_per_table`) |
| Silent-except removed across 6 narrative tables | `_run_extra_table_prune` body | `test_narrative_extra_prune_logs_error_per_table` (count == 6) |

---

## Sequence of commits within the PR

Each commit passes tests independently; reviewer can bisect cleanly.

1. `feat(config): SCORE_HISTORY_RETENTION_DAYS + VOLUME_SNAPSHOTS_RETENTION_DAYS + model_validator` (Task 1)
2. `feat(main): guard Settings() with ValidationError logger.error before re-raise` (V4#1 fold — applies to main.py:1384, main.py:1359, social/telegram/cli.py:45, trading/calibrate.py:495)
3. `feat(db): idx_score_history_scanned_at migration (with busy_timeout)` (Task 1.5 first half)
4. `feat(db): idx_volume_snapshots_scanned_at migration` (Task 1.5 second half)
5. `feat(db): prune_score_history method with TDD coverage` (Task 2)
6. `feat(db): prune_volume_snapshots method with TDD coverage` (Task 3)
7. `refactor(main): extract _run_hourly_maintenance helper (no behavior change)` (Task 4 step 4.1)
8. `feat(main): hourly prune of score_history + volume_snapshots via Settings` (Task 4 step 4.3)
9. `fix(narrative): structured logging for extra-prune errors + decouple score/volume` (Task 5)
10. `docs(backlog): file BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION + 3 review-deferred items` (Task 6 step 6.1-6.2)

Total: 10 commits. Average ~10-50 LOC per commit. Bisect-friendly.

---

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Migration fails on prod (e.g., disk pressure during index build) | Low | Medium | Split migrations (D3) per V4#3 — partial success is durable. `paper_migrations` idempotency record makes retry safe. srilu has 32GB free per V4 SSH check; comfortable headroom. |
| Field-validator + systemd `Restart=always` = silent crash-loop | Medium | High | V4#1 fold: try/except ValidationError around `Settings()` constructions emits structured `logger.error("settings_validation_failed")` to journalctl before re-raise. Loud failure surface for operator. Active-push Telegram deferred to `BL-NEW-SETTINGS-VALIDATION-ALERT`. |
| Field-validator breaks operator's existing `.env` | Low-medium | Medium | Default (21d) is above current cooldown (14d). Verified on srilu: no override of either field; validator passes. Only breaks if operator manually lowers retention below cooldown. |
| 21d retention causes meaningful disk pressure | Very low | Low | ~600MB extra at steady state. srilu has 32GB free. Backup rotation (memory `project_vps_backup_rotation_2026_05_09.md`) holds N=3 daily snapshots → 1.8GB additional in backup volume — comfortable. V4#5: re-evaluate if `du -sh /root/gecko-alpha/backups` exceeds 70% utilization. |
| First prune at 21d default discovers existing-pruning was stale | Refuted by §9a check | — | Verified on srilu: existing prune is firing, tables at 14d boundary. First hourly pass at 21d deletes 0 rows. |
| Index build holds write lock too long on 6M-row table | Low-medium | Low-medium | V4#2 fold: `PRAGMA busy_timeout = 90000` covers `gecko-dashboard.service` concurrent reads during build. 30-60s estimated per index, 60-120s total. Single one-time event per VPS at deploy. Pipeline + dashboard pause briefly but resume cleanly. |
| `_run_hourly_maintenance` extraction introduces subtle behavior change | Low | Medium | Step 4.1 is a no-behavior-change refactor commit with regression test before adding new prune calls. Bisect-friendly. V3 NICE-TO-HAVE fold: signature trimmed to `(db, session, settings, logger)` — `args` was unused. |
| Telemetry log volume + journalctl noise | Very low | Low | V4#4 fold: structlog at main.py:1373 has no `filter_by_level`. Revised D6 follows cryptopanic pattern — info-when-rows>0, silent-when-zero. ~2 info lines/hour at steady state. |
| Failed prune accumulates backlog silently until inspection | Low | Medium | V4#6 fold: filed as `BL-NEW-SCORE-VOLUME-PRUNE-ALERT` follow-up (evidence-gated on first prod failure). `logger.exception("score_history_prune_failed")` keeps the structured log visible; active-push alert deferred. |
| Hot-revert leaves indexes in prod permanently | — | None | V4#7 fold: indexes are harmless if .env-only rollback is used (other queries benefit). Documented in deployment verification — operator should NOT manually `DROP INDEX` on revert. |
| systemd unit lives only on srilu, not in repo | — | Medium (out of PR scope) | NOTE finding from V4: `/etc/systemd/system/gecko-pipeline.service` exists on VPS but not in `systemd/` of this repo. Separate §12-class risk — file as `BL-NEW-SYSTEMD-UNIT-IN-REPO` follow-up. Not in scope of this PR. |

---

## Out of scope (explicit non-goals — design-level)

- Per-token rolling retention (covered in plan)
- The other 6 narrative-pruned tables (filed as `BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION`)
- §12a freshness SLO / watchdog (blocked on daemon)
- Backfill / staged-delete for first prune pass (refuted by §9a)
- VPS deployment (operator-gated)
- Changing log-level convention elsewhere in the codebase (D6 applies only to new prune callsites; other callsites unchanged)
- Curl-direct Telegram on `settings_validation_failed` (V4#1 deferred — filed as `BL-NEW-SETTINGS-VALIDATION-ALERT`)
- Active push alert on `*_prune_failed` (V4#6 deferred — filed as `BL-NEW-SCORE-VOLUME-PRUNE-ALERT`)
- systemd unit committed to repo (V4 NOTE deferred — filed as `BL-NEW-SYSTEMD-UNIT-IN-REPO`)
- Parameterizing `outcome_check_interval=3600` hardcode at `main.py:1630` (V3#2 — flagged as drift, not in scope)

---

## Deployment verification (operator-gated)

Already documented in plan. Repeat the key checks here for the reviewer's convenience:

1. `journalctl -u gecko-pipeline | grep "score_volume_prune_indexes_migrated"` — confirm migration ran once at startup
2. `sqlite3 scout.db ".indexes score_history"` + `".indexes volume_snapshots"` — verify new indexes present
3. Wait 1-2h for first hourly maintenance loop firing
4. `journalctl -u gecko-pipeline --since "1 hour ago" | grep -E "score_history_pruned|volume_snapshots_pruned"` — expect at minimum a `debug` line each hour (or `info` once retention boundary shifts past current oldest row in ~7 days)
5. `journalctl -u gecko-pipeline | grep "_prune_failed"` — should be empty
6. `sqlite3 scout.db "SELECT COUNT(*) FROM score_history WHERE scanned_at < datetime('now', '-21 days')"` — should be 0 after first prune pass at 21d boundary
7. Operator-revert path if needed: `.env` `SCORE_HISTORY_RETENTION_DAYS=365` + `VOLUME_SNAPSHOTS_RETENTION_DAYS=365` (must stay ≥ cooldown per validator). Full rollback = revert PR.
