**New primitives introduced:** Same set as `tasks/plan_score_volume_pruning_harden.md` — `SCORE_HISTORY_RETENTION_DAYS` + `VOLUME_SNAPSHOTS_RETENTION_DAYS` Settings fields (default 21), a `@model_validator(mode='after')` on Settings enforcing retention ≥ `SECONDWAVE_COOLDOWN_MAX_DAYS`, `Database.prune_score_history` and `Database.prune_volume_snapshots` methods, `_migrate_score_volume_prune_indexes` migration adding `idx_score_history_scanned_at` and `idx_volume_snapshots_scanned_at`, `_run_hourly_maintenance(db, session, settings, args, logger)` helper extracted from main.py, `_run_extra_table_prune(db)` helper extracted from narrative agent, structured log events `score_history_pruned` / `volume_snapshots_pruned` / `score_history_prune_failed` / `volume_snapshots_prune_failed` / `extra_prune_table_error`.

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

### D2. `@model_validator(mode='after')` enforcement (V2#3 fold)

Silent retention-vs-cooldown mis-config is the failure mode. Validator runs at Settings construction (i.e., at startup) and raises if `SCORE_HISTORY_RETENTION_DAYS < SECONDWAVE_COOLDOWN_MAX_DAYS` (same for volume). Startup crash is the loud failure we want — operator sees it immediately.

Choice of `model_validator(mode='after')` over `field_validator('SCORE_HISTORY_RETENTION_DAYS')`: the cross-field dependency (retention depends on cooldown) is what `model_validator` is designed for. `field_validator` only sees one field at a time.

### D3. Indexes via migration step (V1#5 / V2#5 fold + `feedback_ddl_before_alter.md` memory)

Existing `idx_score_hist_addr` and `idx_volume_snap_addr` use `(contract_address, scanned_at)` — leading column is wrong for `WHERE scanned_at <= ?`. SQLite cannot use these indexes for the prune DELETE, forcing a table scan over 6M rows hourly.

**Solution:** add `idx_score_history_scanned_at` and `idx_volume_snapshots_scanned_at` (single-column on `scanned_at`).

**Why a migration, not `_create_tables`:** per memory `feedback_ddl_before_alter.md` (BL-060 prod crash), `_create_tables` does not re-run on existing prod tables (the implicit guard via `CREATE TABLE IF NOT EXISTS` blocks the function), so any new `CREATE INDEX IF NOT EXISTS` placed inside it would never execute on srilu. The dedicated migration mechanism (mirroring `_migrate_minara_alert_emissions_v1` at db.py:3432) uses `paper_migrations(name, cutover_ts)` as an idempotency record and runs unconditionally on `initialize()`.

**EXPLAIN-QUERY-PLAN test** confirms the new index is used post-migration. Without this test, a future PR could rename the index and the prune would silently revert to table scan.

**Deploy note:** `CREATE INDEX` on a 6M-row table is O(N log N) ≈ 30-60 seconds wall time on the VPS. Single short write lock during migration. Operator should be aware of brief pipeline pause during initial start after deploy.

### D4. `_run_hourly_maintenance` extraction (V1#7 fold)

The inline hourly block at `scout/main.py:1702-1751` is not testable without `run_pipeline` time-mocking. Extracting to `_run_hourly_maintenance(db, session, settings, args, logger)` enables direct unit testing of the prune wiring — much cleaner than the hand-wavy "mock time and trigger the branch" alternative.

The extraction is committed as a separate refactor commit with NO behavior change. That commit is reviewed independently; downstream prune-wiring commits build on it.

### D5. `_run_extra_table_prune` extraction + drop outer try (V1#2 fold)

Mirror of D4 for the narrative side. The helper handles per-table errors via structured `logger.exception` — fault-isolated. The outer `try: ... except Exception: logger.exception("extra_prune_error")` wrapping the original loop is now unreachable except for import-resolution failures, so it's dropped. Call site becomes a single `await _run_extra_table_prune(db)` line.

### D6. Log-level discipline (V2#7 fold)

- `rows_deleted > 0` → `logger.info` (operationally interesting)
- `rows_deleted == 0` → `logger.debug` (every hour; would spam info if not gated)
- Any exception → `logger.exception` (always visible, structured stack)

Keeps the operator's `journalctl ... grep` workflow clean while preserving full debug visibility behind `-l debug` if needed.

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

The plan organizes commits per task. The recommended commit sequence — designed so each commit passes tests independently and the reviewer can bisect cleanly:

1. `feat(config): SCORE_HISTORY_RETENTION_DAYS + VOLUME_SNAPSHOTS_RETENTION_DAYS + validator` (Task 1)
2. `feat(db): scanned_at indexes for score/volume prune coverage` (Task 1.5)
3. `feat(db): prune_score_history method with TDD coverage` (Task 2)
4. `feat(db): prune_volume_snapshots method with TDD coverage` (Task 3)
5. `refactor(main): extract _run_hourly_maintenance helper (no behavior change)` (Task 4 step 4.1)
6. `feat(main): hourly prune of score_history + volume_snapshots via Settings` (Task 4 step 4.3)
7. `fix(narrative): structured logging for extra-prune errors + decouple score/volume` (Task 5)
8. `docs(backlog): file BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION residual` (Task 6 step 6.1-6.2)

Total: 8 commits. Average ~10-50 LOC per commit. Bisect-friendly.

---

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Migration fails on prod (e.g., disk pressure during index build) | Low | Medium | Migration is idempotent; failure leaves table without new index but doesn't break existing functionality. Operator can re-run by restarting service. |
| Field-validator breaks operator's existing `.env` | Low-medium | Medium | Default (21d) is above current cooldown (14d). Only breaks if operator has manually lowered retention below cooldown. PR description should call out the new constraint loudly. |
| 21d retention causes meaningful disk pressure | Very low | Low | ~600MB extra at steady state. Operator can lower via `.env` if needed (must keep ≥ cooldown). |
| First prune at 21d default discovers existing-pruning was stale | Refuted by §9a check | — | Verified on srilu: existing prune is firing, tables at 14d boundary. First hourly pass at 21d deletes 0 rows. |
| Index build holds write lock too long on 6M-row table | Low-medium | Low-medium | 30-60s estimated. Single one-time event per VPS at deploy. Pipeline pauses but resumes cleanly. Operator should run deploy at low-activity window. |
| `_run_hourly_maintenance` extraction introduces subtle behavior change | Low | Medium | Step 4.1 is a no-behavior-change refactor commit with regression test before adding new prune calls. Bisect-friendly. |
| Telemetry log volume exceeds journalctl rotation | Very low | Low | `info` only when rows_deleted > 0 (D6); `debug` otherwise. At 21d steady state, info-level fires ~once/hour per table = 48 lines/day. Negligible. |

---

## Out of scope (explicit non-goals — design-level)

- Per-token rolling retention (covered in plan)
- The other 6 narrative-pruned tables (filed as `BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION`)
- §12a freshness SLO / watchdog (blocked on daemon)
- Backfill / staged-delete for first prune pass (refuted by §9a)
- VPS deployment (operator-gated)
- Changing log-level convention elsewhere in the codebase (D6 applies only to new prune callsites; other callsites unchanged)

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
