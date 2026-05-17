**New primitives introduced:** Same set as `tasks/plan_sqlite_wal_profile.md` (cycle 4, commits `b4df2d6` + `8c1b451`) — `Database.probe_wal_state()` method returning `wal_size_bytes/wal_pages/shm_size_bytes/db_size_bytes/page_count/page_size/freelist_count/journal_mode/wal_autocheckpoint`, structured log events `sqlite_wal_probe` (debug) / `sqlite_wal_bloat_observed` (warning) / `sqlite_wal_probe_failed` (exception), `SQLITE_WAL_PROFILE_ENABLED: bool = True` + `SQLITE_WAL_BLOAT_BYTES: int = 50_000_000` Settings fields, hook in `_run_hourly_maintenance`, `scripts/wal_summary.sh` (with longest-consecutive-run aggregator + Week-1 baseline calibration suggester) + `scripts/wal_archive.sh` (weekly cron, dated filename rotation, .N suffix for same-day re-runs, filename-date-based 8w retention), filed `BL-NEW-SQLITE-WAL-TUNING-DECISION` follow-up, memory checkpoint `project_sqlite_wal_tuning_checkpoint_2026_06_14.md`.

# Design: BL-NEW-SQLITE-WAL-PROFILE

**Plan reference:** `tasks/plan_sqlite_wal_profile.md` (commit `8c1b451`)
**Pattern source:** cycle 3's `tasks/design_tg_burst_profile.md` (measurement-layer, structured logs + archive + pre-registered criteria, evidence-gated follow-up).
**Plan reviews folded:** V20 (SQLite probe correctness) + V21 (decision-bearing criteria + data interpretability) — see commit `8c1b451`.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| SQLite WAL size / autocheckpoint monitoring | None (DevOps + MLOps probed 2026-05-17) | Build in-tree. |
| Generic PRAGMA helpers | None — aiosqlite + raw PRAGMA suffice | Build in-tree. |

awesome-hermes-agent: 404 (consistent). **Verdict:** custom; mirror cycle 3's pattern.

## Design decisions

### D1. `probe_wal_state()` is read-only — `pragma_wal_autocheckpoint` table-valued form (V20#M1 fold)

`PRAGMA wal_autocheckpoint` (no arg) has a documented side effect: triggers a passive checkpoint if page-count threshold currently exceeded. The plan claims measurement-only — read via the table-valued function `SELECT * FROM pragma_wal_autocheckpoint` which is a pure read. All other PRAGMAs in the probe (`journal_mode`, `page_count`, `page_size`, `freelist_count`) are pure reads when called with no argument.

### D2. WAL file size from filesystem, not PRAGMA (V20#3 fold completeness)

`os.path.getsize(<db>-wal)` returns the on-disk WAL sidecar size atomically (single stat syscall). Similarly `<db>-shm` (shared memory file — typically 32KB but can grow). Both sidecars reported so operators have full disk-bloat picture during TUNE analysis. WAL file size is near-real-time, may lag pending writes by ms-scale (documented in docstring).

### D3. Defensive `journal_mode` normalization + assertion (V20#M2 fold)

SQLite normalizes `PRAGMA journal_mode` return value to lowercase per docs, but driver normalization may vary. Apply `.lower()` defensively. Test asserts `state["journal_mode"] == "wal"` with a clear error message — catches silent WAL-mode rejection on filesystems without shared-memory support (e.g., NFS, certain container mounts).

### D4. Log-level discipline (V21 fold — cycle 3 parity)

- `sqlite_wal_probe` (per-hour, routine) → **DEBUG** (default-INFO journalctl filters; opt-in via `-p debug` or via archive script)
- `sqlite_wal_bloat_observed` (threshold breach) → **WARNING** (actionable signal)
- `sqlite_wal_probe_failed` (probe itself raised) → **exception** (`logger.exception(...)`)

Mirrors cycle 3's `tg_dispatch_observed` (debug) / `tg_burst_observed` (warning) / `tg_dispatch_rejected_429` (warning) split.

### D5. Week-1 baseline calibration is documented, not coded (V21#M2 fold)

Threshold `SQLITE_WAL_BLOAT_BYTES=50MB` is a starting point, not empirically derived. The plan's deployment-verification step #6 instructs operator to run `wal_summary.sh 168` after 7 days, read suggested-threshold value from output (`~1.5×p95` rounded to 5MB), set `.env` override, restart. This avoids building a complex "baseline mode then promote" state machine for what is a one-time calibration. Memory checkpoint file includes the calibration reminder.

**V23 M2 fold — post-crash WAL inheritance contamination.** If the process restarts during Week 1 (deploy, OOM, host reboot), the first probe after restart may report a WAL inherited from pre-crash state — SQLite replays+truncates on open, but if the crash left 100-200MB unreplayed, the first probe captures it, polluting p95 and skewing the suggested threshold upward. **Mitigation:** `wal_summary.sh` Week-1 baseline section drops the first probe-event after each process start (detect via gap >90min between consecutive probes — same heuristic as the consecutive-run detector). Operator-facing message: "if any restart occurred during week 1, re-run after 168 clean probes" (data-bound, not calendar-bound per CLAUDE.md §11).

### D5b. `shm_size_bytes` is informational only (V23 M1 fold)

`shm_size_bytes` reports the `-shm` shared-memory sidecar. SQLite grows it in 32KB increments tracking concurrent reader count; it is NOT WAL bloat. The bloat trigger (`sqlite_wal_bloat_observed`) gates only on `wal_size_bytes`. Operator reading raw probe output during incident must not conflate `shm_size_bytes ≠ 0` with bloat. The `probe_wal_state` docstring carries an explicit warning; `wal_summary.sh` header output prints the same caveat.

### D6. Decision criteria are strict + single-event-aware (V21#M1, M3 folds)

- WAL bloat: ≥12 **STRICTLY consecutive** hourly probes above threshold → TUNE (any dip resets the streak)
- Runaway WAL: any **single** probe > 500MB → TUNE-IMMEDIATELY
- DB fragmentation: `freelist_count > 0.10 × page_count` on **any single probe** → schedule VACUUM (freelist is monotonic-until-VACUUM; "sustained" was undefined; single-event is the right shape)
- Zero events for 4 weeks → ACCEPT

`wal_summary.sh` includes an awk-aggregator computing max consecutive-run-length so operator gets a one-line answer to the streak criterion.

### D7. Single hook in `_run_hourly_maintenance` — 13th SQL hop, post-prune (V22 fold)

V22 M1 fold: precise count — current hourly maintenance issues **12 SQL hops** today (check_outcomes + db_size stat + 3 cycle-0 prunes + 2 cycle-1 prunes + 6 cycle-2 narrative prunes). Adding `probe_wal_state` makes the **13th SQL hop**.

V22 S1 fold: probe runs AFTER all 12 prunes so the captured `wal_size_bytes` reflects peak WAL pressure within the hour. DELETEs write tombstones into the WAL; probing AFTER captures the realistic peak that drives the bloat threshold. A pre-prune probe would miss the DELETE-driven WAL growth that's the most relevant signal. Single post-prune probe matches cycle 3's single-hook pattern.

V22 S2 fold: total `_run_hourly_maintenance` cost post-cycle-4 is well under 50ms at srilu scale — each cycle-2 prune is sub-ms (verified via cycle 2 acceptance), and cycle 4 contributes 5 PRAGMA reads + 2 `os.path.getsize` syscalls = ~1ms total.

---

## Cross-file invariants

| Invariant | Where enforced | Test |
|---|---|---|
| WAL mode is active after `initialize()` | `Database.initialize()` PRAGMA journal_mode=WAL | `test_probe_wal_state_returns_required_fields` asserts `journal_mode == "wal"` |
| `wal_autocheckpoint` PRAGMA read has no side effects | Use of `pragma_wal_autocheckpoint` table-valued function | (No direct test; documented in docstring + plan §D1) |
| Probe runs at most once per hour | `_run_hourly_maintenance` is hourly-gated at `outcome_check_interval` (cycle 2) | `test_run_hourly_maintenance_emits_wal_probe_when_enabled` |
| Bloat event fires only above `SQLITE_WAL_BLOAT_BYTES` | Hourly hook conditional | `test_run_hourly_maintenance_emits_bloat_observed_above_threshold` |
| Flag default is True | `scout/config.py` field default | `test_sqlite_wal_profile_enabled_default_true` |
| All probe fields are typed | Type assertions in test | `test_probe_wal_state_returns_required_fields` |
| `shm_size_bytes` is NOT part of bloat-trigger calc | Hourly hook reads `wal_size_bytes` only | Implicit via the threshold test |
| WAL-file-missing branch returns 0 (V23 SHOULD-FIX fold) | `os.path.exists()` guard in `probe_wal_state` | `test_probe_wal_state_wal_file_missing_returns_zero` (new) |
| structlog emits ordered within hourly block (V22 N2 fold) | Single event loop; emits are sequential | Implicit via cycle 1+2+3+4 hourly tests all passing |

---

## Commit sequence

5 commits, bisect-friendly:

1. `feat(config): SQLITE_WAL_PROFILE_ENABLED + SQLITE_WAL_BLOAT_BYTES settings`
2. `feat(db): probe_wal_state() observability method`
3. `feat(main): hourly WAL probe via _run_hourly_maintenance`
4. `feat(scripts): wal_summary.sh + wal_archive.sh operator tools` **— includes creating the memory checkpoint file `project_sqlite_wal_tuning_checkpoint_2026_06_14.md` per V22 M2 fold (not assumed extant)**
5. `docs(backlog): close BL-NEW-SQLITE-WAL-PROFILE + file BL-NEW-SQLITE-WAL-TUNING-DECISION`

---

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Default threshold (50MB) too loose → false-negative TUNE signal | Medium | Low | Week-1 baseline calibration documented; operator tunes to ~1.5×p95 |
| Default threshold too tight → false-positive WARNING noise | Medium | Low | Same calibration step; default 50MB is well above expected (~4MB autocheckpoint × 12.5× headroom) |
| `wal_autocheckpoint` PRAGMA side effect (V20#M1) | Refuted | — | Use `pragma_wal_autocheckpoint` table-valued function (pure read) |
| Probe latency on hot path | Low | Low | 5 PRAGMA reads + 2 `os.path.getsize` syscalls = sub-ms |
| WAL probe runs concurrently with scorer writes | Low | None | Single `self._conn` (no aiosqlite race); `os.path.getsize` is atomic syscall — values are near-real-time |
| journalctl rotation under burst load | Low | Low | `wal_archive.sh` weekly cron + 8w retention + filename-date rotation (mirrors cycle 3 V16 folds) |
| Operator forgets Week-1 calibration | Medium | Low | Memory checkpoint `project_sqlite_wal_tuning_checkpoint_2026_06_14.md` includes the reminder; default 50MB stays safe for the soak |
| Threshold-flip-flop hides sustained bloat | Low | Low | Decision criterion is STRICT consecutive (V21#M3); `wal_summary.sh` aggregator surfaces longest-run-length |
| WAL mode silently rejected on srilu filesystem | Very low | Medium | Test asserts `state["journal_mode"] == "wal"` post-initialize with explicit failure message (V20#M2) |

---

## Out of scope

- Active WAL tuning — measurement first; decision per `BL-NEW-SQLITE-WAL-TUNING-DECISION`
- VACUUM scheduling — `freelist_count` exposes the need; separate scope as `BL-NEW-SQLITE-VACUUM-SCHEDULE`
- §12a watchdog on the probe — gated on §12a daemon
- DB-side compaction / split — not in scope of any current backlog
- Persistent counter state across restarts — measurement is journalctl-based (cycle 3 precedent)

---

## Deployment verification (autonomous post-3-reviewer-fold)

Identical to plan §Deployment. Key sequence:

1. journalctl retention probe (informational)
2. Install `wal_archive.sh` cron unconditionally + mkdir/chmod
3. Restart + verify `sqlite_wal_probe` DEBUG emits via `journalctl ... -p debug | grep sqlite_wal_probe | head -3`
4. `wal_summary.sh 1` smoke test
5. Memory checkpoint already filed pre-merge
6. Week-1 calibration reminder at 2026-05-24
7. Pre-registered review at 2026-06-14
