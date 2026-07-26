# Capacity artifact — detection_decision_receipts under the two-identity model

**New primitives introduced:** NONE (measurement + design-options doc; the
two-identity write model ships in `scout/db.py` + `scout/trading/detection_alert.py`).

> Filename `capacity_*.md` matches no gated plan/design/spec pattern; marker
> line above included for hygiene only. This doc DESCRIBES storage options and
> reports measurements; it does not implement any storage change.

**Status: BLOCKING.** Under the reviewer-mandated **evaluation-identity** model
(each poll of a candidate is a new receipt row), the receipts table grows
per-evaluation. Measured production shakedown + a calibrated benchmark show a
single-file 120-day retention is **not sustainable** on the current VPS. The
two-identity write model ships as specified; the storage design below is
DESCRIBED for reviewer ruling, and the replacement cohort does not start until a
storage design is approved (prereg §3 gate).

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

## 7. Disk headroom + proposed fail-closed disk-pressure threshold (DESCRIBE)

Headroom: **36 GB free / 75 GB (51% used)**. Proposed guard (fail-closed,
consistent with LOCK 4) — **describe, do not implement here**:

- When free disk `< max(10 GB, 15%)`, the receipts writer **stops writing new
  receipts**; the **send path is unaffected**.
- The gap is surfaced: a `detection_receipt_disk_pressure` structured event + a
  new `skipped_disk_pressure_n` counter in `detection_receipt_summary`, so the
  coverage gap is explicit and the affected cohort window is flagged invalid (a
  window with skipped receipts cannot be reconciled).
- Fail-closed = stop writing rather than fill the disk and take down the pipeline.

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

## 10. Storage-design options (DESCRIBE — for reviewer ruling; NOT implemented)

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
