# Capacity artifact — detection_decision_receipts under the two-identity model

**New primitives introduced:** NONE (measurement + design-options doc; the
two-identity write model ships in `scout/db.py` + `scout/trading/detection_alert.py`).

> Filename `capacity_*.md` matches no gated plan/design/spec pattern; marker
> line above included for hygiene only. This doc DESCRIBES storage options and
> reports measurements; it does not implement any storage change.

**Status: architecture APPROVED + IMPLEMENTED (2026-07-26).** Under the
evaluation-identity model each poll is a new receipt row, so a single-file
120-day retention is not sustainable (measurements below). The approved
**lifecycle-tiered hot SQLite + time-partitioned compressed cold archive +
queryable integrity manifest** is now implemented in
`scout/trading/receipt_archive.py` (+ migration `20260727` in `scout/db.py`,
config in `scout/config.py`). §11 records the backup-multiplier clarification and
the off-host destination finding. The replacement cohort still does not start
until the storage design is confirmed operationally + an off-host destination is
provisioned (prereg §3 gate + §11).

All prod reads were `sqlite3 -readonly`; the benchmark ran on a `/tmp` scratch
file. scout.db was never written.

---

## 0. Environment (measured, srilu `ubuntu-4gb-hel1-1`, 2026-07-26)

| Item | Value |
|---|---|
| Disk (`/dev/sda1`) | 75 GB total, **36 GB used, 36 GB avail (51%)** |
| `scout.db` size | 4,019,429,376 B (**4.0 GB**) |
| Daily backups (keep-3, full-file copies) | 3.46 GB, 3.74 GB, 4.02 GB (`scout.db.bak.*`) |
| WAL / shm now | 28.9 MB / 64 KB |
| SQLite | 3.45.1 · page_size **4096** · journal_mode **WAL** · wal_autocheckpoint **1000** pages |
| CG candidate population (`candidates WHERE chain='coingecko'`) | **992** (grows over time) |

---

## 1. Receipts per cycle and per day (MEASURED from shakedown)

Shakedown table: **733 rows** in `scout.db`, `decided_at` 2026-07-26T02:49:25.9Z →
03:12:41.1Z. Per-cycle `evaluated_n` from the 10 `detection_receipt_summary`
journal lines: **721, 724, 722, 724, 721, 723, 722, 724, 722, 722** → mean
**722.5 evaluations/cycle** (very stable; `filtered_non_cg_source ≈ 28–31/cycle`
are excluded and write no receipt). Outcome mix: **too_old = 732, gate_fail_quality = 1**
(≈99.9% `too_old` — the same aged candidates re-evaluated every cycle).

Cadence: 10 summaries over 02:49:26Z → 03:15:28Z = 1562 s / 9 intervals =
**173.6 s ≈ 2.9 min/cycle**.

> **KEY:** the shakedown table shows only 721/9/1/1/1 rows/cycle because the
> PRE-fix key omitted the evaluation instance, so cycles 2–10's re-polls
> `INSERT OR IGNORE`-collapsed (and were mis-counted as 715+ conflicts). The
> journal `evaluated_n ≈ 722/cycle` is the TRUE evaluation rate. **Under the
> two-identity fix, every cycle writes ≈ 722 NEW rows** (`newly_written ≈ evaluated`).

**Rows/day** (722 rows/cycle):

| Cycle interval | Cycles/day | Rows/day |
|---|--:|--:|
| 2.0 min | 720 | **519,840** |
| 2.9 min (measured) | 496.6 | **358,545** |
| 4.0 min | 360 | **259,920** |

---

## 2. Stored bytes per evaluation (MEASURED, 733 real rows)

Per-row concatenated text length (all TEXT columns): **avg 496.3 B, median 494 B,
p95 520 B, min 453 B, max 574 B**. Dominant field is `raw_inputs` (the JSON
decision blob).

Actual on-disk storage via `dbstat` (page-accurate, incl. row overhead + b-tree):

| btree | bytes | pages | B/row (n=733) |
|---|--:|--:|--:|
| `detection_decision_receipts` (table) | 434,176 | 106 | **592.3** |
| `idx_ddr_idempotency` (UNIQUE) | 65,536 | 16 | 89.4 |
| `idx_ddr_token_decided` | 49,152 | 12 | 67.1 |
| **all-in (table + 2 indexes)** | **548,864** | 134 | **748.8** |

Cross-check: the scratch benchmark at 100,722 rows measured **726.1 B/row**
all-in. **Projection uses 749 B/row** (the conservative prod figure).

---

## 3. Table vs index growth (MEASURED)

Indexes are **20.9%** of all-in bytes (156.5 / 748.8 B/row). At 120 days /
2.9-min cadence (≈43.0M rows): table ≈ **25.5 GB**, indexes ≈ **6.7 GB**.

---

## 4. WAL growth + checkpoint behavior (MEASURED, scratch, calibrated payloads)

One 722-row cycle (per-row `INSERT OR IGNORE` + commit, matching the app) grows
the WAL by **4,152,992 B ≈ 4.15 MB** before checkpoint. `wal_autocheckpoint=1000`
pages = 4.0 MB, so a checkpoint fires roughly **once per cycle** and the WAL stays
bounded near ~4 MB — WAL growth is **not** a concern at this write rate.

---

## 5. Projected storage at 7 / 30 / 120 days (749 B/row all-in)

| Horizon | Rows (2.9-min) | Storage (2.9-min) | 120d range (2–4 min) |
|---|--:|--:|--:|
| 7 days | 2,509,815 | **1.88 GB** | 1.36–2.73 GB |
| 30 days | 10,756,350 | **8.06 GB** | 5.84–11.7 GB |
| **120 days** | **43,025,400** | **32.2 GB** | **23.4–46.7 GB** |

Storage/day: **194.7 MB (4-min) · 268.6 MB (2.9-min) · 389.4 MB (2-min)**.

**Verdict — single-file 120d is UNSUSTAINABLE.** A ~32 GB receipts payload lands
on top of the existing 4 GB `scout.db` → a ~36 GB single file, on a disk with
**36 GB free**. The **keep-3 daily full-file backup rotation** would then need
≈ 3 × 36 = **108 GB** — larger than the entire 75 GB disk. Even **30-day**
single-file (+8 GB → 12 GB db, ×3 backups = 36 GB) consumes essentially all
current free space. The blocking constraint is **storage growth + backup
amplification**, not write performance.

---

## 6. Insert latency + lock impact (MEASURED, scratch, calibrated)

| Metric | Value |
|---|---|
| 722 per-row-commit inserts (one cycle), synchronous=NORMAL | **0.073 s total → 0.101 ms/insert** |
| Bulk throughput (commit/1000) | **31,666 rows/s** |

The app commits per receipt under `_txn_lock`, held ~0.1 ms/insert. At
synchronous=FULL (fsync per commit) budget ~1–3 ms/insert → ≤ ~2.2 s for a
722-row cycle — still far below the ~2.9-min cycle interval, and the lock is the
lane's own DB connection. **Write latency / lock contention is not a blocker at
projected volume.** (Bench calibrated to the real `raw_inputs` shape + the real
2-index set; scratch `/tmp` file, never scout.db.)

---

## 7. Disk headroom + fail-closed disk-pressure guard (IMPLEMENTED)

Headroom: **36 GB free / 75 GB (51% used)**. Guard (fail-closed, consistent with
LOCK 4) — now implemented (`check_disk_pressure` + the lane guard, gated behind
`DETECTION_RECEIPT_DISK_GUARD_ENABLED`, default OFF):

- When free disk `< max(DETECTION_RECEIPT_DISK_MIN_FREE_GB=10 GB,
  DETECTION_RECEIPT_DISK_MIN_FREE_PCT=15%)`, receipt **accrual is suspended** for
  the cycle; the **send path is unaffected**.
- The gap is surfaced: a `detection_receipt_disk_pressure` structured event + an
  operator alert (parse_mode=None, §12b dispatched/delivered logs) + a
  `skipped_disk_pressure_n` counter and `coverage_healthy=false` in
  `detection_receipt_summary`, so the reconciliation identity intentionally does
  NOT close and the window is flagged invalid.
- Fail-closed = stop accruing rather than fill the disk; never prune/sample/reduce
  detail. Test: `test_disk_pressure_suspends_accrual_send_unaffected`.

---

## 8. Prune / archive throughput + protected-cohort evidence

Bulk delete/insert throughput is ≥ 31k rows/s (§6), so pruning a matured cohort
is I/O-cheap relative to accumulation. Protected cohort data **cannot** be removed
prematurely: `prune_detection_decision_receipts` is double-guarded (config floor
`ge=120` + the `DETECTION_RECEIPTS_COHORT_CLOSED_AT` completeness marker), proven
by `test_prune_blocked_until_cohort_closed`, `test_prune_respects_cohort_close_marker`,
`test_retention_floor_rejects_below_120`. Shakedown rows are excluded by
documented timestamp-range only — never deleted/mutated (prereg §3).

---

## 9. Recovery behavior (SQLite WAL guarantees + fail-soft)

- **Partial write / crash mid-INSERT:** WAL commit is atomic per SQLite; an
  uncommitted frame is rolled back on next open — no torn/half receipt row.
- **Restart:** the writer resumes on the next cycle; a missed cycle's receipts
  are simply absent and show as a coverage gap in per-cycle reconciliation
  (evaluated vs rows), never as silent success.
- **Full disk (`SQLITE_FULL`):** the INSERT raises; the fail-soft wrapper catches
  it (`detection_receipt_write_failed` + `write_failures_n`), and the **send path
  is unaffected** (LOCK 4). The proposed §7 guard stops attempts before the disk
  fills.

---

## 10. Storage-design options (APPROVED choice C+D+B IMPLEMENTED; §12)

120 days is an **evidence-retention** requirement, not a single-file mandate. All
options below preserve the COMPLETE evidence model (every evaluation reconstructable;
index-decisions/conflicts/failures/manifests retained; both horizons + closure +
final analysis + audit buffer survive ≥120 days). None drops observations, treats
later polls as replays, stores only changed outcomes, samples, or pre-aggregates
before lifecycle completion (all PROHIBITED).

- **A. Hot SQLite index + append-only compressed cold archive.** Keep a small hot
  table (recent + all index-decisions/conflicts/failures) in `scout.db`; append
  each cycle's full receipts to a compressed, append-only cold store (e.g. daily
  gzip/zstd NDJSON or a separate rotated SQLite) with a queryable manifest. Full
  payloads reconstructable from cold; hot stays small → backups stay small.
- **B. Normalized storage.** Factor the highly-repetitive stable fields
  (`gate_version`, `code_version`, `comparator`, `threshold_value`, per-token
  `source_observation_ts`) into reference rows; the per-evaluation row stores keys
  + the small varying part. Full payload reconstructable by join. Cuts B/row
  materially (the JSON `raw_inputs` is the bulk; much is constant per token/gate).
- **C. Partitioned / rotated immutable stores + queryable manifest.** One
  immutable store per day/window (separate file, never rewritten); a manifest
  table indexes windows → files for query and integrity. Backups copy only new
  partitions, eliminating the full-file re-copy amplification that breaks the
  keep-3 rotation.
- **D. Lifecycle-tiered retention.** Retain **index-decisions, conflicts,
  write-failures, and manifests** at full fidelity for the long horizon; ordinary
  **post-index** evaluations (the ≈99.9% `too_old` re-polls) are retained through
  reconciliation + both horizons + cohort closure + final analysis + audit buffer,
  then rolled into the immutable archive rather than kept hot. Still ≥120-day
  evidence, but not ≥120 days of hot rows.

**Recommended for review:** combine **C** (partitioned immutable + manifest, fixes
backup amplification) with **D** (lifecycle tiering, fixes hot-row count), and
optionally **B** (normalization) to cut per-row cost. This keeps `scout.db` and
its backups small while preserving the full 120-day reproducible evidence lifecycle.
Final choice is the reviewer's; the two-identity write model is independent of it.

---

## 11. Backup topology + off-host destination finding (DISCOVERED, srilu 2026-07-26)

**Backup mechanism (read-only discovery).** `gecko-backup.service` (systemd
`gecko-backup.timer`, daily 03:00) runs `/usr/local/bin/gecko-backup-create.sh`
(sqlite3 online `.backup` + `PRAGMA integrity_check`) then
`/usr/local/bin/gecko-backup-rotate.sh` (keep top-N by mtime).
`gecko-backup-watchdog.timer` alerts on staleness. Env:
`GECKO_BACKUP_DIR=/root/gecko-alpha`, `GECKO_BACKUP_KEEP=3`.

**The gecko backup is LOCAL-ONLY.** Neither script does any off-host transfer —
no `rsync`/`scp`/`s3`/`rclone` step. `rclone`/`restic`/`borg`/`aws`/`b2` are NOT
installed (`rsync` + `scp` binaries exist but no destination is configured). The
only S3 path on the box is in a DIFFERENT project's `shift-agent-backup.sh`
(optional `aws s3 sync`, gated on `S3_BUCKET` + `aws`, both currently absent).

**⇒ Off-host destination = operator-provisioned dependency.** No reachable
off-host destination is currently discoverable for gecko. Per the reviewer's
directive, everything else is implemented and the archival transaction's **step 6
fails closed**: with `DETECTION_RECEIPT_OFFHOST_DIR` empty (the default), the
archiver publishes the partition + manifest but **holds the hot rows** (status
`held_hot_no_offhost`) and never deletes them until an operator provisions a
genuine off-host / replicated destination. When set, the archiver copies +
fsyncs + hash-verifies the partition at that destination before any hot delete;
the operator must also arrange manifest+hash verification after transfer,
missing/corrupt-partition monitoring, and a periodic scratch restore-and-query
test (a local-only dir is INSUFFICIENT for durability).

**Backup multiplier — clarified.** `GECKO_BACKUP_KEEP=3` means **3 RETAINED
backup files PLUS the live DB = 4 full copies**, ALL on the same host/disk
(`/dev/sda1`). Projected totals (2.9-min cadence, 749 B/row all-in):

| Component | Now | + 30d receipts (single-file) | + 120d receipts (single-file) |
|---|--:|--:|--:|
| Live scout.db | 4.0 GB | ~12.1 GB | ~36.2 GB |
| WAL + headroom | ~0.03 GB | ~0.03 GB | ~0.03 GB |
| Retained backup ×1 | 4.0 GB | ~12.1 GB | ~36.2 GB |
| Retained backup ×2 | 3.7 GB | ~12.1 GB | ~36.2 GB |
| Retained backup ×3 | 3.5 GB | ~12.1 GB | ~36.2 GB |
| **Total (4 copies)** | **~15.2 GB** | **~48.4 GB** | **~144.8 GB** |
| Disk capacity | 75 GB | 75 GB | 75 GB |
| **Fits?** | yes | **NO (>75 GB)** | **NO (>75 GB)** |

Single-file retention breaks the keep-3 rotation at ~30 days and catastrophically
at 120 days. The implemented hot/cold split keeps the **hot** table bounded to
index receipts + in-lifecycle rows (bench: 24 hot rows → 4 after archival), so the
live DB + its 3 rotated backups stay near today's ~15 GB, while the ~99.9%
post-index `too_old` re-polls live in immutable compressed cold partitions that
sit OUTSIDE the keep-3 rotation and are replicated once to the operator-provisioned
off-host destination.

---

## 12. Implementation map (what the next head added)

| Reviewer requirement | Where | Test(s) |
|---|---|---|
| Archive/lifecycle (hot+cold, manifest) | `receipt_archive.ReceiptArchiver.archive_once` (7-step); migration `20260727` | `test_archive_once_publishes_and_deletes_hot`, `test_hot_storage_stays_bounded_after_archival` |
| Unique analytical-index guarantee | `receipt_archive.index_decisions` (ROW_NUMBER over `decided_at,id`, cohort+shakedown scoped) | `test_index_decisions_unique_per_token_even_with_ties`, `test_index_excludes_shakedown_and_pre_cohort` |
| Evaluation-instance id before persistence + reused on retry | `detection_alert._receipt_idempotency_key` (+ frozen `decided_at`) | `test_evaluation_instance_key_reused_not_regenerated`, `test_receipt_intra_cycle_exact_retry_is_replay` |
| Backup + restore controls | `restore_and_reconcile`, `ColdArchiveReader`, `verify_partition`, off-host copy+verify | `test_restore_and_reconcile_after_archive`, `test_verify_and_reader_detect_corruption` |
| Archive atomicity + corruption | 7-step fail-closed (verify/reconcile/off-host gates) | `test_no_offhost_holds_rows_hot`, `test_reconcile_mismatch_leaves_hot_untouched`, `test_verify_and_reader_detect_corruption` |
| Disk-pressure fail-closed | `check_disk_pressure` + lane guard (suspend accrual, alert, coverage unhealthy, send path intact) | `test_disk_pressure_suspends_accrual_send_unaffected`, `test_disk_pressure_flags_when_below_threshold` |
| Hot storage stays bounded | archival deletes post-index rows only after off-host confirm | `test_hot_storage_stays_bounded_after_archival` (bench: 24→4) |
| End-to-end restore-and-reconcile | `restore_and_reconcile` (verify + id-hash + no-duplication) | `test_restore_and_reconcile_after_archive` |
| Migration idempotency | `_migrate_detection_receipt_archive_v1` | `test_archive_migration_idempotent` |
