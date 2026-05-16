**New primitives introduced:** Same set as `tasks/plan_narrative_prune_scope_expansion.md` (cycle 2 plan, commit `ed2c566` + V8/V9 fold `fded8f0`) — 6 Settings fields (`VOLUME_SPIKES_RETENTION_DAYS`=45, `MOMENTUM_7D_RETENTION_DAYS`=30, `TRENDING_SNAPSHOTS_RETENTION_DAYS`=30, `LEARN_LOGS_RETENTION_DAYS`=90, `CHAIN_MATCHES_RETENTION_DAYS`=45, `HOLDER_SNAPSHOTS_RETENTION_DAYS`=14), `@model_validator(mode='after')` enforcing 30d floor on trending/chain/volume, 6 `Database.prune_*` methods, extension of cycle 1's `_migrate_scanned_at_index` helper to accept a `column` kwarg, 5 new index migrations (`volume_spikes_detected_at_idx_v1`, `momentum_7d_detected_at_idx_v1`, `trending_snapshots_snapshot_at_idx_v1`, `learn_logs_created_at_idx_v1`, `holder_snapshots_scanned_at_idx_v1`), hourly wiring in `_run_hourly_maintenance`, deletion of `_run_extra_table_prune` from `scout/narrative/agent.py`, structured log events `{table}_pruned` and `{table}_prune_failed` per table.

# Design: BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION

**Plan reference:** `tasks/plan_narrative_prune_scope_expansion.md` (commit `fded8f0`)
**Pattern source:** PR #136's `tasks/design_score_volume_pruning_harden.md` (cycle 1, merged `00abaa7`)
**Plan reviews folded:** V8 (per-table reader-window verification) + V9 (per-table index coverage) — see commit `fded8f0` body.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| SQLite multi-table retention enforcement | None — Hermes skill hub 2026-05-16 WebFetch (DevOps category): "no skills match SQLite multi-table retention policy enforcement, time-series DB cleanup, or pipeline-internal data lifecycle management" | Build in-tree. |
| Cross-field Pydantic validators for cohort-floor enforcement | N/A — project pattern; reuse `_validate_live_caps_relation` shape from `config.py:766-781` | Extend existing. |

awesome-hermes-agent: 404 on 2026-05-16 (consistent with cycle 1 probe). **Verdict:** mirror PR #136 custom-code path × 6 tables.

---

## Design decisions

### D1. Per-table retention defaults (V8 plan-review fold)

| Table | Default | Reason |
|---|---|---|
| `volume_spikes` | **45d** | Was 30d hardcoded. Backtest CLI default `--days=30` at `scripts/backtest_conviction_lock.py:298,886` — 15d headroom prevents boundary-coincidence silent truncation |
| `momentum_7d` | 30d (unchanged) | All readers ≤7d (`scout/spikes/detector.py:300,488`); 30d generous |
| `trending_snapshots` | **30d** | Was 7d hardcoded. **V8 MUST-FIX:** `scripts/backtest_conviction_lock.py:894` reads `--days` default 30 — 7d retention silently truncated cohort. Bump to match backtest expectation |
| `learn_logs` | 90d (unchanged) | LIMIT-N reads (`scout/narrative/learner.py:375`, `dashboard/db.py:380`); 90d over-provisioned, kept defensive |
| `chain_matches` | **45d** | Was 30d hardcoded. **V8 MUST-FIX:** `scout/backtest.py:161` + `scripts/backtest_v1_signal_stacking.py:237,279` read 30d — equality boundary; 15d headroom |
| `holder_snapshots` | 14d (unchanged) | Single LIMIT-1 reader (`scout/db.py:4198`); writer dormant per memory `findings_silent_failure_audit_2026_05_11.md §2.5` |

### D2. `_validate_backtest_cli_retention_floor` model_validator (V8 plan-review fold)

Pydantic v2 `@model_validator(mode='after')` enforcing `TRENDING_SNAPSHOTS_RETENTION_DAYS`, `CHAIN_MATCHES_RETENTION_DAYS`, `VOLUME_SPIKES_RETENTION_DAYS` ≥ 30. The 30d threshold is the backtest CLI default; any lower value silently truncates analytical cohorts.

**Why not all 6 tables in the validator?** `momentum_7d` / `learn_logs` / `holder_snapshots` have no analytical reader requiring a floor. Per-table operator override via `.env` remains the knob.

**Precedent:** mirrors `_validate_live_caps_relation` (`config.py:766-781`) and `_validate_retention_covers_secondwave_window` (added in cycle 1 PR #136).

### D3. Index migrations — extend cycle 1's helper (V9 plan-review fold)

Cycle 1 added `_migrate_scanned_at_index(table, index_name, migration_name)` at `db.py:3677`. The helper hardcodes `ON {table}(scanned_at)` at line 3690 — limited to tables whose timestamp column is literally `scanned_at`. Of the 5 new tables to index, only `holder_snapshots` has `scanned_at`; the other 4 use `detected_at`/`snapshot_at`/`created_at`.

**Refactor:** add `column: str = "scanned_at"` kwarg to the helper. Existing 2 callers (score_history, volume_snapshots) use the default — backward compatible.

5 new migrations added (split per-table per V4#3 fold from cycle 1 — disk-pressure failure on one shouldn't roll back the others):

| Migration name | Index | Table size estimate |
|---|---|---|
| `volume_spikes_detected_at_idx_v1` | `idx_volume_spikes_detected_at` | ~1K-10K rows (spike detector ~daily, top-N) |
| `momentum_7d_detected_at_idx_v1` | `idx_momentum_7d_detected_at` | ~1K-10K rows (momentum detector daily) |
| `trending_snapshots_snapshot_at_idx_v1` | `idx_trending_snapshots_snapshot_at` | ~15K-50K rows (top-15 trending × per-cycle) |
| `learn_logs_created_at_idx_v1` | `idx_learn_logs_created_at` | ~100 rows (daily narrative cycle, 90d retention) — zero existing index |
| `holder_snapshots_scanned_at_idx_v1` | `idx_holder_snapshots_scanned_at` | ~0 rows (writer dormant per BL-020) |

**Skipped:** `chain_matches` per V9 NICE-TO-HAVE — slow-growth table; PR-stage `EXPLAIN QUERY PLAN` check; promote only if SCAN row count > few hundred.

**Migration deploy cost:** all 5 tables much smaller than cycle 1's 6M-row tables. Total migration time estimate: 5-15s aggregate (vs cycle 1's ~45s). Single short EXCLUSIVE lock per migration; `PRAGMA busy_timeout = 90000` inherited from helper.

### D4. Prune method per table

6 methods on `Database`. Each mirrors `prune_perp_anomalies` (`db.py:4692`) signature `*, keep_days: int -> int`. Each hardcodes its timestamp column (5 distinct columns across 6 tables). `<=` boundary semantic per cycle 1's V1#3 convention.

### D5. Hourly wiring as a tight loop (engineer judgment from plan)

Inside `_run_hourly_maintenance`, the 6 new prune calls go after the existing score/volume blocks. The plan offered two options (explicit blocks × 6 vs tight loop × 1); design chooses the **tight loop** for ~24 LOC savings vs 6 explicit blocks:

```python
for prune_name, retention_attr in [
    ("prune_volume_spikes", "VOLUME_SPIKES_RETENTION_DAYS"),
    ("prune_momentum_7d", "MOMENTUM_7D_RETENTION_DAYS"),
    ("prune_trending_snapshots", "TRENDING_SNAPSHOTS_RETENTION_DAYS"),
    ("prune_learn_logs", "LEARN_LOGS_RETENTION_DAYS"),
    ("prune_chain_matches", "CHAIN_MATCHES_RETENTION_DAYS"),
    ("prune_holder_snapshots", "HOLDER_SNAPSHOTS_RETENTION_DAYS"),
]:
    try:
        keep_days = getattr(settings, retention_attr)
        rows = await getattr(db, prune_name)(keep_days=keep_days)
        if rows:
            logger.info(
                f"{prune_name.removeprefix('prune_')}_pruned",
                rows_deleted=rows,
                keep_days=keep_days,
            )
    except Exception:
        logger.exception(f"{prune_name.removeprefix('prune_')}_prune_failed")
```

Event names match cycle 1's convention: `{table}_pruned` / `{table}_prune_failed`.

### D6. Narrative agent helper deletion

`_run_extra_table_prune` in `scout/narrative/agent.py:68-103` becomes empty (all 6 entries migrated out). Delete the helper entirely + replace its call site with a single comment. Update or delete `tests/test_narrative_agent_prune.py` accordingly.

### D7. Log-level discipline (inherited from cycle 1 V4#4)

`info`-when-rows>0, silent-when-zero. No `debug` (structlog config has no `filter_by_level`). Matches `prune_cryptopanic_posts` precedent at `main.py:1747-1748` + cycle 1's score/volume prune.

### D8. Boundary semantic preserved (`<=` cutoff)

All 6 prune methods use `<= cutoff` (matches cycle 1's V1#11 tie-on-cutoff lock-in test). Tie-on-cutoff test added per-table to lock in semantic.

### D9. Migration registration order in `initialize()`

```python
        await self._migrate_score_history_scanned_at_index()       # cycle 1
        await self._migrate_volume_snapshots_scanned_at_index()    # cycle 1
        await self._migrate_volume_spikes_detected_at_index()      # cycle 2
        await self._migrate_momentum_7d_detected_at_index()        # cycle 2
        await self._migrate_trending_snapshots_snapshot_at_index() # cycle 2
        await self._migrate_learn_logs_created_at_index()          # cycle 2
        await self._migrate_holder_snapshots_scanned_at_index()    # cycle 2
```

Order: alphabetical by table name within cycle 2 group. Each migration is independent via its own `paper_migrations` entry — order doesn't matter functionally.

---

## Cross-file invariants

| Invariant | Where enforced | Test |
|---|---|---|
| `TRENDING_SNAPSHOTS_RETENTION_DAYS >= 30` | model_validator in `scout/config.py` | `test_backtest_cli_retention_floor_validator` |
| Same for `CHAIN_MATCHES_*` and `VOLUME_SPIKES_*` | Same validator (per-field loop) | Same test |
| Each of 6 tables pruned hourly when `_run_hourly_maintenance` runs | Tight loop in `scout/main.py` | 6 `test_run_hourly_maintenance_calls_*_prune` tests |
| Each of 5 new indexes used by prune DELETE | Migration + index covers WHERE clause | 5 EXPLAIN QUERY PLAN tests |
| `_run_extra_table_prune` does NOT exist post-migration | Helper deleted from `scout/narrative/agent.py` | `test_run_extra_table_prune_helper_is_removed` |

---

## Commit sequence

10 commits, bisect-friendly:

1. `feat(config): 6 narrative-table retention Settings + backtest-CLI-floor validator`
2. `refactor(db): extend _migrate_scanned_at_index to accept column kwarg`
3. `feat(db): index migration — volume_spikes(detected_at)`
4. `feat(db): index migration — momentum_7d(detected_at)`
5. `feat(db): index migration — trending_snapshots(snapshot_at)`
6. `feat(db): index migration — learn_logs(created_at)`
7. `feat(db): index migration — holder_snapshots(scanned_at)`
8. `feat(db): 6 prune methods for narrative-owned tables`
9. `feat(main): hourly prune of 6 narrative-owned tables via Settings`
10. `refactor(narrative): delete _run_extra_table_prune (migration complete) + close BL-NEW backlog entry`

Total estimated LOC: ~250 added, ~50 removed (helper deletion).

---

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Migration cost on srilu | Low | Low | All 5 tables are tiny vs cycle 1's 6M rows. Aggregate <15s estimated. Single short EXCLUSIVE per migration; `PRAGMA busy_timeout=90000` inherited |
| Validator breaks operator's `.env` if explicit override below 30d | Low | Medium | All new defaults ≥ 30d on the 3 validated fields. Validator surfaces clear error via cycle 1's `load_settings()` guard (visible in journalctl) |
| `momentum_7d` writer rate ≠ estimated → larger table than expected | Low | Low | Index migration is `IF NOT EXISTS`; can re-run if perf degrades; existing prune already keeps table ≤30d |
| Backtest scripts use `--days > 45` → silent truncation | Medium | Low | Operator-visible at script run time. Document in code comment next to retention defaults; reviewer may suggest doc-string for backtest scripts |
| Codex session's in-flight baseline-test fixes conflict with this PR | Medium | Low | Codex working on `tests/test_bl064_*`, `tests/conftest.py`, etc. — different files from this PR. Verify post-merge via `git diff codex/fix-baseline-test-failures..feat/narrative-prune-scope-expansion -- tests/` to confirm no overlap |
| CRLF regression on test files (cycle 1 fold-time hit) | Medium | Low | Operator caught it on cycle 1 PR review. This cycle: `git -c core.autocrlf=false` on every Edit-then-stage operation; `git ls-files --eol` spot-check before push |

---

## Out of scope (explicit non-goals)

- `chain_matches` `completed_at` index — slow-growth table; PR-stage EXPLAIN check; promote to migration only if SCAN row count > few hundred
- Activating the dormant `holder_snapshots` writer (BL-020 follow-up) — retention setting applies for when writer eventually fires
- Per-table active-push alert on prune failure — covered by `BL-NEW-SCORE-VOLUME-PRUNE-ALERT` already filed (cycle 1 V4#6)
- Backtest-script `--days > retention` doc strings — separate cosmetic PR; out of scope here
- VPS deployment — operator-gated per "do not deploy until PR is reviewed by me" rule

---

## Deployment verification (operator-gated)

After merge + deploy:

1. `journalctl -u gecko-pipeline | grep -E "(volume_spikes|momentum_7d|trending_snapshots|learn_logs|holder_snapshots)_scanned_at_idx_migrated|_detected_at_idx_migrated|_created_at_idx_migrated|_snapshot_at_idx_migrated"` — expect 5 migration entries
2. `sqlite3 scout.db ".indexes <table>"` for each of the 5 tables — confirm new index present
3. Wait 1-2h for first hourly maintenance
4. `journalctl -u gecko-pipeline --since "2 hours ago" | grep -E "(volume_spikes|momentum_7d|trending_snapshots|learn_logs|chain_matches|holder_snapshots)_pruned"` — expect rows_deleted=0 silent for first pass at the new (potentially higher) retention defaults; info-emit if existing rows exceed the new retention
5. `sqlite3 scout.db "SELECT COUNT(*) FROM <table>"` for each table — confirm bounded growth at new defaults
6. Verify validator wired: operator runs `python -m scout.main --check-config` — should succeed; if they set `TRENDING_SNAPSHOTS_RETENTION_DAYS=7` in `.env` and re-run, should fail with `settings_validation_failed` (cycle 1's load_settings guard) + clear ValidationError message
7. Revert path: per-table `.env` `<TABLE>_RETENTION_DAYS=365` (must keep trending/chain/volume ≥ 30d per validator); full rollback = revert PR
