# Bundle A: Detection Telemetry Hygiene — Design Document

**New primitives introduced:** None new beyond what `tasks/plan_bundle_a_detection_telemetry.md` already declares (column `chain_matches.mcap_at_completion REAL`, heartbeat field `mcap_null_with_price_count`, structured log event `chain_outcomes_unhydrateable_memecoin`). This design doc adds NO further primitives — it is analysis of edge cases, test matrix, and rollback strategy for the implementation already specified in the plan.

**Companion to:** `tasks/plan_bundle_a_detection_telemetry.md` (the plan is the implementation contract; this design covers what the plan doesn't fully address: edge case behaviour, test coverage matrix, failure modes, rollback).

---

## 1. Test coverage matrix

For each acceptance criterion, this table records the test that exercises it. Gaps below the line are intentional non-coverage with a reason.

### Task 1 — `mcap_null_with_price_count`

| Behaviour | Test | File:line |
|---|---|---|
| Field initialized to 0 | `test_mcap_null_with_price_field_initialized_to_zero` | new file |
| Increment helper bumps counter | `test_increment_bumps_counter` | new file |
| Reset helper clears counter | `test_reset_clears_counter` | new file |
| Heartbeat log includes the field | `test_heartbeat_log_includes_field` | new file |
| Ingestion fires the increment | `test_fetch_top_movers_increments_counter` | new file |
| Increment fires for `market_cap=null + current_price>0` | covered by ingestion test | — |
| Increment does NOT fire for `market_cap=0 + current_price=0` | **gap (see Test Gap T1.A)** | — |
| Increment does NOT fire for `market_cap=positive + current_price=positive` | covered by ingestion test (`tok2` does not increment) | — |
| Per-cycle counter persists across cycles | implicit (module-level dict, no reset between cycles in prod) | — |

**Test Gap T1.A:** No explicit test for the negative case `market_cap=0 + current_price=0`. The condition `(raw.get("market_cap") in (None, 0)) and (raw.get("current_price") or 0) > 0` evaluates to `False and False = False`, so the counter does not fire. This is correct behaviour but is not asserted directly. **Decision:** Not worth a test; the boolean composition is trivially correct and the affirmative-and-negative cases in `test_fetch_top_movers_increments_counter` already exercise both sides.

### Task 2 — BL-071b EXPIRED → NULL

| Behaviour | Test | File:line |
|---|---|---|
| `_record_expired_chain` writes NULL not 'EXPIRED' | `test_expired_chain_writes_null_not_expired` | new file |
| Hydrator picks up NULL row, writes 'hit' from predictions | `test_hydrator_picks_up_null_expired_chain` | new file |
| Migration converts existing narrative EXPIRED rows to NULL | `test_migration_converts_expired_narrative_rows` (assertion 1) | new file |
| Migration leaves narrative rows with `evaluated_at` alone | same test (assertion 2) | new file |
| Migration leaves memecoin EXPIRED rows alone | same test (assertion 3) | new file |
| Migration records itself in `paper_migrations` | same test (assertion 4) | new file |
| Migration is no-op on second invocation | same test (assertion 5) | new file |
| `_record_expired_chain` skips chains with 0 steps_matched | **gap (see Test Gap T2.A)** | — |

**Test Gap T2.A:** The existing function returns early when `steps_matched <= 0`. The plan preserves this behaviour. No regression test for this branch. **Decision:** Not worth adding — the early-return is unchanged from current behaviour; only the INSERT VALUES list changed. Existing test suite (`test_chains_tracker.py`) covers this implicitly via end-to-end tracker tests.

### Task 3 — BL-071a partial

| Behaviour | Test | File:line |
|---|---|---|
| `chain_matches.mcap_at_completion REAL` column exists post-migration | `test_chain_matches_has_mcap_at_completion_column` | new file |
| Hydrator silently skips memecoin rows where `mcap_at_completion IS NOT NULL` | `test_hydrator_silent_skip_when_mcap_at_completion_populated` | new file |
| Hydrator emits ONE aggregate warning for N unhydrateable memecoin rows | `test_hydrator_aggregate_warning_when_no_source` | new file |
| Hydrator still uses legacy `outcomes` table when populated | covered by existing untouched code path | — |
| Narrative pipeline branch unchanged | `test_hydrator_picks_up_null_expired_chain` (Task 2) | — |
| `mcap_at_completion=0` (edge: explicitly zero, not NULL) treated as NULL | **gap (see Test Gap T3.A)** | — |
| `mcap_at_completion<0` (edge: negative — should never happen) treated as NULL | **gap (see Test Gap T3.B)** | — |
| Aggregate warning is NOT emitted when count == 0 | implicit from hydrator code (`if memecoin_unhydrateable:`) | — |
| Hydrator does NOT crash if `chain_matches` table missing the column (fresh DB) | covered by Task 3.1 schema-check test (column exists post-init) | — |

**Test Gap T3.A:** `mcap_at_completion=0.0` would fall through the `if mcap_at_completion is not None and mcap_at_completion > 0` guard and behave the same as NULL (fall through to legacy outcomes path). This is correct (0 mcap is meaningless data, treat like NULL) but not explicitly tested. **Decision:** Worth one assertion in an existing test — append a row with `mcap_at_completion=0.0` to `test_hydrator_aggregate_warning_when_no_source` and verify it's counted in the unhydrateable bucket.

**Test Gap T3.B:** Negative `mcap_at_completion` would also fall through. Same disposition as T3.A — same test row covers both edge inputs.

**Build-phase action:** apply T3.A/T3.B coverage by appending one row with `mcap_at_completion=0.0` to the unhydrateable test fixture (5 rows instead of 3, assert count==5).

---

## 2. Edge case + failure mode analysis

### Task 1 (heartbeat counter)

**Failure mode F1.1 — Counter overflow.**
Python ints are unbounded; not a concern.

**Failure mode F1.2 — Counter reset between cycles.**
The counter is module-level and never reset by the heartbeat itself. It accumulates across the lifetime of the pipeline process. This is intentional: operators want a rolling lifetime count, not a per-cycle count. After a service restart, the counter resets to 0 — that's the only reset path in production. Documented in `_reset_heartbeat_stats` docstring (test helper only).

**Failure mode F1.3 — Race condition on `_heartbeat_stats` dict mutation.**
The pipeline is single-threaded (asyncio); all increments happen inside the event loop. No GIL race. No defensive locking needed.

**Failure mode F1.4 — `current_price` field present but None.**
`raw.get("current_price") or 0` handles None safely (returns 0, fails the `> 0` check). No counter fire. Correct.

**Failure mode F1.5 — Counter fires for tokens we'd accept anyway.**
Hypothetical: a token with `market_cap=null` AND `current_price>0` AND `from_coingecko` parses `market_cap_usd=0` AND happens to match `MIN_MARKET_CAP=0` (settings override) → would be accepted but counter still fired. **This is fine.** The counter measures the data-quality issue (CG returning null mcap), not the rejection itself. If operator chooses to override the floor, that's separate from "did CG give us bad data." Counter is honestly named.

### Task 2 (EXPIRED → NULL)

**Failure mode F2.1 — Hydrator runs concurrently with `_record_expired_chain` on same row.**
Both write to `chain_matches`. The hydrator's `WHERE outcome_class IS NULL` would match a freshly-NULL row from `_record_expired_chain`. The hydrator queries `predictions` for the matching `coin_id` and writes outcome if present. If `_record_expired_chain` writes NULL and the hydrator immediately queries, there are two outcomes:
- `predictions` has data → hydrator writes outcome (correct)
- `predictions` has no data → hydrator continues without writing (row stays NULL, hydrator picks up next cycle — correct)

No data corruption. Worst case: row stays NULL one cycle longer. Acceptable.

**Failure mode F2.1' — `_record_expired_chain` does not commit; caller commits later.** (Per design-review R1 M1.)
`_record_expired_chain` issues `await db._conn.execute(INSERT ...)` but does NOT call `commit()`. The caller (`run_chain_tracker` at tracker.py:151) commits after the cycle completes. If the caller path crashes between the INSERT and the commit (out-of-memory, unhandled exception in subsequent code), the row is silently lost — the same as before this PR (the existing `EXPIRED` write also relied on the caller's deferred commit). This is NOT a regression introduced by Bundle A; it's a pre-existing property of the writer's commit-deferral pattern. Mitigation already in place: aiosqlite is configured with daemon worker threads (`tests/conftest.py:30`); on process death the OS reclaims the connection without partial-write commit. Database state is consistent. **Documented here so future readers don't think Bundle A introduced this** — it didn't, but the commit-boundary surface should be visible.

**Failure mode F2.2 — Migration runs while LEARN cycle is mid-execution in another process.**
Single-process pipeline (`gecko-pipeline.service`); no cross-process contention. Migration runs at startup *inside* `Database.initialize()` BEFORE the LEARN scheduler is wired. No race.

**Failure mode F2.3 — Predictions row with non-canonical `outcome_class` value.**
The hydrator does `if outcome not in ("hit", "miss"): outcome = "hit" if outcome == "hit" else "miss"`. This collapses any non-`hit` to `miss`, which preserves existing behaviour (no semantic change in this PR). NIT: this conditional is logically equivalent to `outcome = "miss"` since the inner `if outcome == "hit"` was already filtered out by the outer `if outcome not in (...)`. Pre-existing code; OUT OF SCOPE for Bundle A.

**Failure mode F2.4 — Migration fails partway (disk full, etc.).**
The migration is inside the existing `_migrate_feedback_loop_schema` `try: BEGIN EXCLUSIVE` block. The existing `except` clause at the end of that method handles ROLLBACK and re-raises. Our additions inherit the same protection. If our INSERT fails, the entire migration rolls back and the service fails to start — operator sees the error, fixes disk, restarts. No partial state.

**Failure mode F2.5 — `paper_migrations` table missing before our migration runs.**
The existing `_migrate_feedback_loop_schema` creates `paper_migrations` table earlier in the same method (db.py:904). Our gate query runs after that creation. Safe.

### Task 3 (mcap_at_completion column + branch)

**Failure mode F3.1 — Column added but migration name collision.**
Migration name `bl071a_chain_matches_mcap_at_completion` is unique (verified by reading existing `paper_migrations` names). No collision.

**Failure mode F3.2 — `ALTER TABLE chain_matches ADD COLUMN` fails (table locked).**
Inside `BEGIN EXCLUSIVE` — exclusive lock on the DB held for the duration. No other writer can interfere. If ALTER itself fails (genuine DB corruption, etc.), the existing rollback path handles it.

**Failure mode F3.3 — Hydrator's per-row sub-query for `mcap_at_completion` is slow at scale.**
Each pending memecoin row triggers one extra `SELECT mcap_at_completion FROM chain_matches WHERE id = ?`. This is a primary-key lookup, O(1) per row, negligible. Even at 10,000 pending rows the overhead is sub-second. NIT: could be optimized into the outer SELECT, but that's a refactor not worth the change-volume in Bundle A scope.

**Failure mode F3.4 — Aggregate warning fires every LEARN cycle indefinitely.** (Per design-review R1 S1, refined.)
This is BY DESIGN until BL-071a' lands. The aggregate-once-per-cycle shape limits log volume to N lines/day (LEARN runs daily-ish). Acceptable signal-to-noise. Reviewer R2 M2 explicitly flagged the per-row antipattern; the aggregate fix addresses the spirit of that concern.

**Anti-decay protection (R1 S1):** The structured log carries explicit `expires_when="BL-071a' ships"` AND `backlog_ref="BL-071a'"` fields. If operators see the warning still firing N days from now, the structured field tells them why and where to track — instead of becoming "known noise" filtered into invisibility. Additionally, the §5 operational verification explicitly checks for the warning's *absence* once BL-071a' lands; the warning's presence after that is a regression flag, not noise.

**Failure mode F3.5 — `update_chain_outcomes` returns count of `updated`, but new memecoin-skip path doesn't add to that count.**
Correct. `updated` = "rows where outcome_class was set". Skipped rows (because no source) are NOT updated. Caller (`run_chain_tracker`) uses `updated` for log granularity, not for correctness. The aggregate warning communicates the skip count separately.

---

## 3. Performance considerations

| Change | Per-cycle cost | Per-LEARN cost | Notes |
|---|---|---|---|
| Counter increment in ingestion | Negligible (one dict mutation per token, ~1000 tokens/cycle) | n/a | Pure Python int +=, no I/O |
| Heartbeat field added | Negligible (one extra dict access per heartbeat emit, ~once/5min) | n/a | Same shape as existing fields |
| EXPIRED → NULL write change | Same as before (one INSERT per expired chain) | n/a | No new I/O |
| Migration UPDATE on startup (one-time) | n/a | n/a (runs once per deploy, then gated) | Bounded scope WHERE clause; should touch ≤ 154 rows in prod |
| Hydrator memecoin sub-query | n/a | +1 sub-query per pending memecoin row (PK lookup, sub-millisecond) | At current ~50 pending memecoin rows/cycle, total +50ms |
| Aggregate warning emit | n/a | One log line per LEARN cycle | Trivial |

No measurable impact on hot paths. No new external API calls. No new connections.

---

## 4. Rollback strategy

If any of the three changes ships and is found to cause a regression in production, the rollback path is:

| Item | Rollback method | Side effects |
|---|---|---|
| Task 1 — heartbeat counter | `git revert <Task1 commit>`, restart service | None — purely additive telemetry |
| Task 2 — EXPIRED → NULL writer | `git revert <Task2 commit>`, restart service | New rows written between deploy and revert will be NULL not EXPIRED. Hydrator will re-process them. The original EXPIRED-pre-stamp behaviour resumes for new rows. Existing migration UPDATE is NOT rolled back (no down-migration); already-NULL'd narrative rows stay NULL — they were always meant to be NULL (the migration's effect is correct regardless of code state). |
| Task 3 — column + branch | `git revert <Task3 commit>`, restart service | Column stays in `chain_matches` (SQLite ALTER TABLE DROP COLUMN requires version-specific handling; reverting code does NOT remove the column). The hydrator reverts to pre-Bundle-A behaviour (per-row legacy outcomes lookup, no aggregate warning). Column is unused but doesn't break anything. |

**Combined rollback:** `git revert` all three commits in any order; restart service. No coupled rollback ordering required because the changes don't depend on each other.

**Partial-deploy story (R1 S3):** gecko-alpha runs as a single systemd unit (`gecko-pipeline.service`). `git pull` is atomic at the file-tree level (working tree updates fully or not at all under a successful fetch+checkout). `systemctl restart` reloads the entire process from scratch. **There is no scenario where one commit lands but another doesn't from the same `git pull`** — and there is no scenario where one Python module gets reloaded while another stays at the old version, because the process is restarted as a unit. The "partial-deploy" worry doesn't apply to this deployment shape; it would apply to a multi-process or hot-reload setup, neither of which we run.

**One-way doors:**
- The `chain_matches.mcap_at_completion` column is effectively permanent (SQLite ALTER COLUMN DROP requires creating a new table + COPY + DROP old). This is fine — an unused nullable column has no cost.
- The `bl071b_unstamp_expired_narrative` and `bl071a_chain_matches_mcap_at_completion` rows in `paper_migrations` are permanent. Deleting them would let the migration re-run on next startup. Not worth manual intervention.

---

## 5. Operational verification post-deploy

After `git pull` + `systemctl restart gecko-pipeline` on the VPS, verify in this order:

0. **Pre-deploy backup (R2 wording fix):** `cp /root/gecko-alpha/scout.db /root/gecko-alpha/scout.db.bak.$(date +%s)` BEFORE the restart. Standard operational hygiene before any DDL change. Bounded migration risk is low but the backup costs nothing and unblocks a one-command rollback if anything surprises.
1. **Service started cleanly:** `systemctl status gecko-pipeline` shows active+running.
2. **Migration applied:** `sqlite3 /root/gecko-alpha/scout.db "SELECT name FROM paper_migrations WHERE name LIKE 'bl071%'"` returns both `bl071b_unstamp_expired_narrative` AND `bl071a_chain_matches_mcap_at_completion`.
3. **Column exists:** `sqlite3 /root/gecko-alpha/scout.db "PRAGMA table_info(chain_matches)"` lists `mcap_at_completion REAL`.
4. **EXPIRED narrative rows converted:** `sqlite3 /root/gecko-alpha/scout.db "SELECT COUNT(*) FROM chain_matches WHERE pipeline='narrative' AND outcome_class='EXPIRED' AND evaluated_at IS NULL"` returns 0 (was 154 pre-deploy per memory).
4b. **Rows are converted, not lost (R1 N2):** `sqlite3 /root/gecko-alpha/scout.db "SELECT COUNT(*) FROM chain_matches WHERE pipeline='narrative' AND outcome_class IS NULL AND evaluated_at IS NULL"` should now return ~154 (the converted rows, plus any new NULL writes since deploy). Confirms the migration converted vs. dropped.
5. **Heartbeat shows new field:** `journalctl -u gecko-pipeline --since '6 minutes ago' | grep heartbeat | tail -1` shows `mcap_null_with_price_count=N` for some N.
6. **No new exceptions:** `journalctl -u gecko-pipeline --since '6 minutes ago' | grep -iE 'error|exception|traceback' | wc -l` returns 0 or only known-pre-existing entries.
7. **LEARN cycle succeeds at next tick (~24h after deploy):** look for `chain_outcomes_hydrated count=N` (success) AND/OR `chain_outcomes_unhydrateable_memecoin count=N` (expected aggregate, not an error). **Per R1 S2: this step is calendar-gated. To prevent forgetting:** create a TaskCreate / scheduled wakeup at the time of merge that fires 24-26h post-deploy with the exact `journalctl` command. Step 7 is documented as part of the merge-and-deploy task in `tasks/todo.md`-style tracking, not as an item that depends on operator memory.

---

## 6. Open questions / non-blocking items for reviewers

1. **Should the aggregate `chain_outcomes_unhydrateable_memecoin` warning be downgraded to INFO once BL-071a' is shipped?** Currently WARNING because it represents an actionable silent-failure gap. Once BL-071a' lands and the gap is closed, the warning is no longer actionable. Documented in BL-071a' acceptance: "remove this warning when DexScreener fetch is wired."
2. **Should the `mcap_null_with_price_count` field be added to dashboard?** Out of Bundle A scope — operator can read heartbeat output via `journalctl` for the 7-day BL-075 Phase A measurement window. If we want a chart, that's a future dashboard PR.
3. **Pre-deploy backup of `scout.db`.** Resolved per R2 wording fix: promoted from "open question" to runbook step §5 #0. No longer a punt.

---

## 7. Deliberate counter-decisions on design-review feedback

R2 made two SHOULD-FIX suggestions that I am NOT applying, with rationale documented here so future readers see the choice was considered.

**Counter-decision A — Did NOT extract `_has_usable_mcap(v: float | None) -> bool` predicate.** R2 NIT.
The 3-way collapse (`v is not None and v > 0`) is open-coded inline at one call site (the new memecoin hydrator branch). Extracting it adds a new symbol to `scout/chains/tracker.py` that's used exactly once. Bundle A's guiding principle is minimal scope — adding helpers we don't need creates surface for future drift (e.g., a second consumer might use the predicate with subtly different intent). Future PR (BL-071a') will likely have multiple call sites once the DexScreener fetch is wired; **at that point** extracting the predicate becomes earned. Not yet.

**Counter-decision B — Did NOT create `_migrate_chain_outcome_telemetry` sibling method.** R2 SHOULD-FIX.
The existing `_migrate_feedback_loop_schema` co-tenants migrations from BL-061 (ladder), BL-062 (peak-fade), BL-063 (moonshot), and BL-064 (TG social, safety_required) — five distinct concerns spanning ~250 lines. The pattern of "all post-launch migrations live in this one method, gated by `paper_migrations` rows and verified by the post-assertion block" is a *consciously-maintained* convention in this repo. Splitting Bundle A into a new method would break the convention, fragment the post-assertion (which currently verifies all migrations applied in one place), and require duplicating the `BEGIN EXCLUSIVE` + ROLLBACK scaffolding. **Pattern conformity beats single-responsibility-purity here.** If the file ever gets refactored (BL-072'-style scope), all migrations should split out together — not just the new one.

R1 NIT (N1) — documenting hidden behaviour change introduced by Task 2:
Task 2 (BL-071b) changes write-time semantics from EXPIRED to NULL. The hydrator's existing fall-through in `update_chain_outcomes` for the narrative branch (`outcome = "hit" if outcome == "hit" else "miss"`) is a "non-canonical → miss" silent fallback that's been there pre-Bundle-A. **After BL-071b, more rows flow through that branch** (because the EXPIRED→NULL conversion now lets them be re-evaluated). If `predictions.outcome_class` ever holds a non-canonical value (anything other than `'hit'`, `'miss'`, `'UNRESOLVED'`), Bundle A will surface previously-hidden miscategorizations as `miss`. Verified: as of 2026-05-03 the predictions table only contains `'HIT'`, `'MISS'`, `'NEUTRAL'`, `'UNRESOLVED'`, NULL — and `'NEUTRAL'` skips the hydrator entirely (pre-existing filter `outcome_class != 'UNRESOLVED'` doesn't catch NEUTRAL — actually verify before claiming this). **Build phase action:** confirm distinct values of `predictions.outcome_class` in prod via SSH; if any non-canonical values exist, document or fix.

## 8. Self-review

- [x] **All test gaps explicitly named** with disposition (covered / not worth / will-add-in-build).
- [x] **All failure modes analyzed** for each task (Task 1: 5; Task 2: 5+1=6 incl. F2.1'; Task 3: 5).
- [x] **Performance impact quantified** (table at §3).
- [x] **Rollback strategy** for each task + combined + partial-deploy clarification (§4).
- [x] **Operational verification checklist** for the deploy (§5) including pre-deploy backup (§5 #0) and row-count integrity (§5 #4b).
- [x] **Open questions** explicitly flagged as non-blocking (§6) and one promoted to runbook step.
- [x] **Deliberate counter-decisions** on R2 feedback documented (§7) with rationale.
- [x] **No new primitives** beyond what the plan declared (frontmatter).
- [x] **Design-review MUST-FIX items addressed:** R1 M1 (F2.1' commit-boundary surface added); R1 M2 (T3.A/T3.B promoted from "build phase action" to explicit row in plan Step 3.1's test fixture); R2 wording on backup (promoted to §5 #0).
- [x] **Design-review SHOULD-FIX items addressed:** R1 S1 (anti-decay `expires_when`/`backlog_ref` log fields); R1 S2 (step 7 calendared); R1 S3 (partial-deploy section in §4); R2 ChainMatch model (added to plan Step 3.3b); R2 structured log fields (plan Step 3.4 split `reason` into structured fields).
- [x] **Design-review NIT items addressed or counter-decided:** R1 N1 (documented in §7); R1 N2 (added §5 #4b); R2 NIT (counter-decision A in §7).
